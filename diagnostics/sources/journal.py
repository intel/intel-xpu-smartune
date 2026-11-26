# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Read-only journald source. It deliberately does not persist a second copy of
# the journal: query windows are translated to journalctl JSON output, then
# normalized into LogRecord for the shared facade.

import json
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone

from diagnostics.sources import kernel_blocks
from diagnostics.sources.base import (LogQueryFilter, LogRecord, LogSource, LogSourceError,
                                     SourceRecords)
from utils.logger import get_logger

logger = get_logger(__name__)

_PRIORITY_LEVELS = {
    "0": "CRITICAL", "1": "CRITICAL", "2": "CRITICAL", "3": "ERROR",
    "4": "WARNING", "5": "INFO", "6": "INFO", "7": "DEBUG",
}
_LEVEL_PRIORITY = {"debug": "7", "info": "6", "warning": "4", "error": "3", "critical": "2"}
_DEFAULT_WINDOW_SECONDS = 60 * 60
_BOOT_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
# Max raw lines read for the kernel-only view, which is read unfiltered so a report's
# INFO-priority trace lines stay with its ERR header.
_KERNEL_VIEW_RAW_LINES = 5000
# Extra lines a forward scan may read past its batch limit to finish an open kernel report
# (or the report's own line cap, if larger).
_SCAN_BLOCK_OVERRUN = 500
# "xe 0000:00:02.0: [drm] ..." -> driver xe, device 0000:00:02.0
_KERNEL_DEVICE_PREFIX_RE = re.compile(
    r"^(?P<driver>[\w.-]+) (?P<device>[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]): ")


def _us_to_epoch(value):
    """journald microsecond timestamp -> epoch seconds (int), or None."""
    try:
        return int(int(value) / 1_000_000)
    except (TypeError, ValueError):
        return None


def _journal_timestamp(entry):
    try:
        return float(entry.get("__REALTIME_TIMESTAMP", 0)) / 1_000_000
    except (TypeError, ValueError):
        return 0.0


def _decode_value(value):
    """Text of a journald JSON value: decodes byte arrays, takes the last of repeated values."""
    if isinstance(value, list):
        if value and all(isinstance(b, int) for b in value):
            try:
                return bytes(b & 0xFF for b in value).decode("utf-8", errors="replace")
            except (TypeError, ValueError):
                return ""
        return _decode_value(value[-1]) if value else None
    return value


def _field_text(entry, name, default=""):
    value = _decode_value(entry.get(name, default))
    return str(value) if value is not None else default


# Fields naming the unit a systemd manager line is about, not the unit that logged it.
_SUBJECT_UNIT_FIELDS = ("UNIT", "USER_UNIT", "OBJECT_SYSTEMD_UNIT")

# --all keeps fields over 4096 bytes (long coredump traces) instead of nulling them; the
# field list (what _record() reads) keeps it from also dumping COREDUMP= core data.
_JSON_OUTPUT = [
    "--output=json", "--all",
    "--output-fields=" + ",".join((
        "MESSAGE", "PRIORITY", "SYSLOG_IDENTIFIER", "SYSLOG_PID", "_PID", "_COMM",
        "_TRANSPORT", "_SYSTEMD_UNIT", *_SUBJECT_UNIT_FIELDS, "_BOOT_ID",
        "_SOURCE_MONOTONIC_TIMESTAMP", "_KERNEL_DEVICE", "_KERNEL_SUBSYSTEM",
        "_UDEV_SYSNAME", "COREDUMP_PID", "COREDUMP_COMM", "COREDUMP_EXE",
        "COREDUMP_SIGNAL", "COREDUMP_SIGNAL_NAME", "COREDUMP_UNIT",
        "COREDUMP_USER_UNIT", "COREDUMP_CGROUP")),
]


class JournalScan:
    """Result of one forward journal read."""

    __slots__ = ("records", "last_cursor", "last_epoch", "complete", "error")

    def __init__(self, records=None, last_cursor=None, last_epoch=None,
                 complete=True, error=None):
        self.records = records or []
        self.last_cursor = last_cursor
        self.last_epoch = last_epoch
        self.complete = complete   # False: more entries remain after last_cursor
        self.error = error         # None | "cursor_invalid" | "failed"


class JournalLogSource(LogSource):
    """Adapt local journald records without indexing or copying them."""

    name = "journal"

    def available(self) -> bool:
        return shutil.which("journalctl") is not None

    def query(self, flt: LogQueryFilter):
        if not self.available():
            return []

        end_time = flt.end_time
        start_time = flt.start_time
        if start_time is None and end_time is None and not flt.allow_unscoped:
            return []
        if end_time is None:
            end_time = int(datetime.now(timezone.utc).timestamp())
        if start_time is None:
            start_time = end_time - _DEFAULT_WINDOW_SECONDS

        # The kernel view filters level and keyword after merging lines into reports, so
        # a matching header keeps its lower-priority trace lines.
        whole_reports = flt.kernel_only
        if whole_reports:
            # Reports average several lines each; a larger -n makes journalctl
            # walk further back, so scale with the request instead of always max.
            raw_lines = min(_KERNEL_VIEW_RAW_LINES, max(1000, flt.limit * 4))
        else:
            raw_lines = max(1, min(flt.limit, 5000))
        command = [
            "journalctl", "--no-pager", *_JSON_OUTPUT,
            f"--since=@{int(start_time)}", f"--until=@{int(end_time)}",
            "-n", str(raw_lines),
        ]
        # Scope to a boot session when asked. Validate the id so a malformed
        # value can never reach the argv; accepts() still enforces it.
        if flt.boot_id and _BOOT_ID_RE.match(flt.boot_id):
            command.append(f"--boot={flt.boot_id}")
        priority = _LEVEL_PRIORITY.get((flt.min_level or "").lower())
        if priority is not None and not whole_reports:
            command.extend(["--priority", f"0..{priority}"])
        # Push the keyword down to journalctl so the -n cap applies AFTER matching:
        # filtering only in accepts() would search just the newest -n entries and
        # silently miss older matches in the window. --grep is PCRE, so escape the
        # text to keep plain-substring semantics and force case-insensitive to match
        # accepts() (which still re-checks as a safety net).
        if flt.keyword and not whole_reports:
            command.extend([f"--grep={re.escape(flt.keyword)}", "--case-sensitive=false"])
        # Kernel-only lens: OOM kills, GPU resets, driver faults and panics live in
        # the kernel ring buffer -- that is journal's diagnostic value next to
        # SmartTune's own logs. Use the _TRANSPORT=kernel MATCH, never -k/--dmesg:
        # -k implies -b (current boot) and would silently drop pre-reboot kernel
        # logs when the window crosses a restart -- exactly when they matter most.
        # The match keeps --since/--until working across boots and leaves room to OR
        # in _SYSTEMD_UNIT=... later. Matches must come last on the argv.
        if flt.kernel_only:
            command.append("_TRANSPORT=kernel")
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, check=False, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("journald query unavailable: %s", exc)
            raise LogSourceError(f"journalctl did not answer: {exc}") from exc
        if result.returncode != 0:
            logger.warning("journald query failed: %s", result.stderr.strip())
            raise LogSourceError(f"journalctl exited with {result.returncode}")

        raw = []
        lines = result.stdout.splitlines()
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            record = self._record(entry)
            if record is not None:
                raw.append(record)
        # One record per kernel report; level / keyword then see the whole report.
        records = [rec for rec in kernel_blocks.coalesce(raw) if flt.accepts(rec)]
        # A full -n read means older entries in the window were never seen.
        return SourceRecords(records[-flt.limit:],
                             truncated=len(lines) >= raw_lines or len(records) > flt.limit)

    def list_boots(self, limit=15):
        """Enumerate recent boot sessions, newest first.

        Prefers ``journalctl --list-boots -o json`` (systemd >= 240) which carries
        first/last entry timestamps; falls back to parsing the plain-text listing
        (boot_id + offset only) on older systemd. Best-effort: returns []."""
        if not self.available():
            return []
        boots = self._list_boots_json()
        if boots is None:
            boots = self._list_boots_text()
        return (boots or [])[:limit]

    def _list_boots_json(self):
        try:
            result = subprocess.run(
                ["journalctl", "--list-boots", "--no-pager", "-o", "json"],
                capture_output=True, text=True, check=False, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("journald list-boots unavailable: %s", exc)
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            entries = json.loads(result.stdout)
        except ValueError:
            return None  # older journalctl ignores -o json here -> text fallback
        boots = []
        for entry in entries:
            boot_id = entry.get("boot_id")
            if not boot_id:
                continue
            index = entry.get("index")
            boots.append({
                "boot_id": boot_id,
                "index": index,
                "first_ts": _us_to_epoch(entry.get("first_entry")),
                "last_ts": _us_to_epoch(entry.get("last_entry")),
                "running": index == 0,
            })
        boots.sort(key=lambda b: b["index"] if b["index"] is not None else 0,
                   reverse=True)
        return boots

    def _list_boots_text(self):
        try:
            result = subprocess.run(
                ["journalctl", "--list-boots", "--no-pager"],
                capture_output=True, text=True, check=False, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("journald list-boots (text) unavailable: %s", exc)
            return None
        if result.returncode != 0:
            return None
        boots = []
        for line in result.stdout.splitlines():
            parts = line.split()
            # "<offset> <boot_id> <first date...> — <last date...>"; locale-
            # formatted dates are unreliable to parse, so keep id + offset only.
            if len(parts) >= 2 and re.match(r"^-?\d+$", parts[0]) and _BOOT_ID_RE.match(parts[1]):
                index = int(parts[0])
                boots.append({
                    "boot_id": parts[1], "index": index,
                    "first_ts": None, "last_ts": None, "running": index == 0,
                })
        boots.sort(key=lambda b: b["index"], reverse=True)
        return boots

    def scan_forward(self, *, after_cursor=None, since_epoch=None, max_records=2000,
                     matches=None, timeout=15.0):
        """Read up to ``max_records`` entries oldest-first, after ``after_cursor`` or from
        ``since_epoch``. A batch cut short has ``complete=False``; resume from ``last_cursor``.
        ``matches`` are journalctl match terms (``FIELD=value``, ``+`` for OR)."""
        if not self.available():
            return JournalScan(error="failed")
        command = ["journalctl", "--no-pager", *_JSON_OUTPUT]
        if after_cursor:
            command.append(f"--after-cursor={after_cursor}")
        elif since_epoch is not None:
            command.append(f"--since=@{int(since_epoch)}")
        command.extend(matches or [])
        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, errors="replace")
        except OSError as exc:
            logger.warning("journald scan unavailable: %s", exc)
            return JournalScan(error="failed")
        watchdog = threading.Timer(timeout, proc.kill)
        watchdog.daemon = True
        watchdog.start()
        scan = JournalScan()
        read = 0
        # Never end a batch inside a kernel report: past max_records, read on only while
        # lines continue the open report.
        blocks = kernel_blocks.KernelBlockTracker()
        try:
            for line in proc.stdout:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                epoch = _journal_timestamp(entry)
                record = self._record(entry)
                overrun = max(_SCAN_BLOCK_OVERRUN, kernel_blocks.max_lines(blocks.open_kind()))
                if read >= max_records and (
                        record is None or not blocks.continues(record)
                        or read >= max_records + overrun):
                    # Not consumed: the cursor stays before this entry.
                    scan.complete = False
                    break
                read += 1
                scan.last_cursor = _field_text(entry, "__CURSOR") or scan.last_cursor
                scan.last_epoch = epoch or scan.last_epoch
                if record is not None:
                    blocks.push(record)
                    scan.records.append(record)
        finally:
            watchdog.cancel()
            if proc.poll() is None:
                proc.kill()
            try:
                _, stderr = proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                stderr = ""
        if scan.complete and proc.returncode == -9:
            # The watchdog fired mid-read: resume from last_cursor next batch.
            scan.complete = False
            logger.warning("journald scan exceeded %.0fs after %d entries; resuming next batch",
                           timeout, read)
        elif scan.complete and proc.returncode not in (0, None):
            message = (stderr or "").strip()
            scan.error = "cursor_invalid" if "cursor" in message.lower() else "failed"
            logger.warning("journald scan failed (rc=%s): %s", proc.returncode, message)
        return scan

    @staticmethod
    def _record(entry):
        timestamp = _journal_timestamp(entry)
        message = _field_text(entry, "MESSAGE")
        if not timestamp or not message:
            return None
        priority = _field_text(entry, "PRIORITY", "6")
        transport = _field_text(entry, "_TRANSPORT") or None
        own_unit = _field_text(entry, "_SYSTEMD_UNIT")
        subject_unit = next((_field_text(entry, name) for name in _SUBJECT_UNIT_FIELDS
                             if _field_text(entry, name)), "")
        unit = subject_unit or own_unit
        identifier = _field_text(entry, "SYSLOG_IDENTIFIER")
        monotonic = _field_text(entry, "_SOURCE_MONOTONIC_TIMESTAMP") or _field_text(
            entry, "__MONOTONIC_TIMESTAMP")
        try:
            monotonic_s = int(monotonic) / 1_000_000 if monotonic else None
        except ValueError:
            monotonic_s = None
        driver = device = None
        if transport == "kernel":
            prefix = _KERNEL_DEVICE_PREFIX_RE.match(message)
            if prefix:
                driver, device = prefix.group("driver"), prefix.group("device").lower()
        coredump = {}
        if identifier == "systemd-coredump" and entry.get("COREDUMP_PID"):
            # Unlike the kernel's segfault line, this names the crashed process's unit / cgroup.
            coredump = {
                "coredump_pid": _field_text(entry, "COREDUMP_PID") or None,
                "coredump_comm": _field_text(entry, "COREDUMP_COMM") or None,
                "coredump_exe": _field_text(entry, "COREDUMP_EXE") or None,
                "coredump_signal": (_field_text(entry, "COREDUMP_SIGNAL_NAME")
                                    or _field_text(entry, "COREDUMP_SIGNAL") or None),
                "coredump_unit": (_field_text(entry, "COREDUMP_UNIT")
                                  or _field_text(entry, "COREDUMP_USER_UNIT") or None),
                "coredump_cgroup": _field_text(entry, "COREDUMP_CGROUP") or None,
            }
        return LogRecord(
            ts_epoch=timestamp,
            ts_iso=datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            level=_PRIORITY_LEVELS.get(priority, "INFO"),
            source="journal",
            logger=identifier or unit or "journal",
            message=message,
            service=unit or None,
            fields={
                "priority": priority,
                "transport": transport,
                "unit": unit or None,
                "emitter_unit": own_unit or None,
                "boot_id": _field_text(entry, "_BOOT_ID") or None,
                "cursor": _field_text(entry, "__CURSOR") or None,
                "identifier": identifier or None,
                "pid": _field_text(entry, "_PID") or _field_text(entry, "SYSLOG_PID") or None,
                "comm": _field_text(entry, "_COMM") or None,
                "monotonic_s": monotonic_s,
                "kernel_subsystem": _field_text(entry, "_KERNEL_SUBSYSTEM") or None,
                "kernel_device": _field_text(entry, "_KERNEL_DEVICE") or None,
                "udev_sysname": _field_text(entry, "_UDEV_SYSNAME") or None,
                "kernel_driver": driver,
                "kernel_pci": device,
                **coredump,
            },
        )