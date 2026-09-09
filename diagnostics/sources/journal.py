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
from datetime import datetime, timezone

from diagnostics.sources.base import LogQueryFilter, LogRecord, LogSource, level_rank
from utils.logger import get_logger

logger = get_logger(__name__)

_PRIORITY_LEVELS = {
    "0": "CRITICAL", "1": "CRITICAL", "2": "CRITICAL", "3": "ERROR",
    "4": "WARNING", "5": "INFO", "6": "INFO", "7": "DEBUG",
}
_LEVEL_PRIORITY = {"debug": "7", "info": "6", "warning": "4", "error": "3", "critical": "2"}
_DEFAULT_WINDOW_SECONDS = 60 * 60
_BOOT_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


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


def _field_text(entry, name, default=""):
    value = entry.get(name, default)
    if isinstance(value, list):
        value = value[-1] if value else default
    return str(value) if value is not None else default


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

        command = [
            "journalctl", "--no-pager", "--output=json",
            f"--since=@{int(start_time)}", f"--until=@{int(end_time)}",
            "-n", str(max(1, min(flt.limit, 5000))),
        ]
        # Scope to a boot session when asked. Validate the id so a malformed
        # value can never reach the argv; accepts() still enforces it.
        if flt.boot_id and _BOOT_ID_RE.match(flt.boot_id):
            command.append(f"--boot={flt.boot_id}")
        priority = _LEVEL_PRIORITY.get((flt.min_level or "").lower())
        if priority is not None:
            command.extend(["--priority", f"0..{priority}"])
        # Push the keyword down to journalctl so the -n cap applies AFTER matching:
        # filtering only in accepts() would search just the newest -n entries and
        # silently miss older matches in the window. --grep is PCRE, so escape the
        # text to keep plain-substring semantics and force case-insensitive to match
        # accepts() (which still re-checks as a safety net).
        if flt.keyword:
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
            return []
        if result.returncode != 0:
            logger.warning("journald query failed: %s", result.stderr.strip())
            return []

        records = []
        for line in result.stdout.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            record = self._record(entry)
            if record is not None and flt.accepts(record):
                records.append(record)
        return records[-flt.limit:]

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

    @staticmethod
    def _record(entry):
        timestamp = _journal_timestamp(entry)
        message = _field_text(entry, "MESSAGE")
        if not timestamp or not message:
            return None
        priority = _field_text(entry, "PRIORITY", "6")
        unit = _field_text(entry, "_SYSTEMD_UNIT")
        identifier = _field_text(entry, "SYSLOG_IDENTIFIER")
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
                "unit": unit or None,
                "boot_id": _field_text(entry, "_BOOT_ID") or None,
                "cursor": _field_text(entry, "__CURSOR") or None,
                "identifier": identifier or None,
            },
        )