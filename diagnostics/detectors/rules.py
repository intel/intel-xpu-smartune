# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Application-log detectors. These read the `smartune` source -- which the
# logging layer already made structured -- and turn SmartTune's own ERROR /
# CRITICAL lines (including tracebacks) into operational_events. That is the
# "read own logs to recognize an abnormality" half of event generation; the
# other half is business code calling emit_event() directly.
#
# System-level detectors that need journald / platform sources (kernel OOM, GPU
# hang, thermal) are stage 3 and will register here the same way -- reusing the
# same alert dedup/cooldown (alert_state) so nothing re-implements throttling.
#
# Idempotency: a monotonic cursor (last seen ts_epoch) skips lines already turned
# into events across runs, and emit_event's severity>=warning path already folds
# repeats into alert_state, so a flapping error does not spam the ledger.

import collections
import re
import threading
import time

from diagnostics import custom_rules, emit_event
from diagnostics import detector_state
from diagnostics.sources import LogQueryFilter, get_source
from utils.logger import get_logger

logger = get_logger(__name__)


class _BoundedSeen:
    """FIFO-bounded membership set. The dedup signatures only need to survive long
    enough for the monotonic cursor to move past a record, so an unbounded set is
    pure leak on a long-running host: cap it and evict oldest-first."""

    def __init__(self, maxlen):
        self._max = maxlen
        self._order = collections.deque()
        self._set = set()

    def __contains__(self, key):
        return key in self._set

    def add(self, key):
        if key in self._set:
            return
        self._set.add(key)
        self._order.append(key)
        if len(self._order) > self._max:
            self._set.discard(self._order.popleft())


# Per-source scan caps. A saturated scan is logged (never silently truncated) so a
# burst that exceeds the cap is visible rather than mistaken for full coverage.
_SMARTUNE_SCAN_LIMIT = 500
_JOURNAL_SCAN_LIMIT = 500
_SEEN_MAX = 8192

_CURSOR_LOCK = threading.Lock()
_cursor_epoch = 0.0  # only smartune lines strictly newer than this are considered
_smartune_seen = _BoundedSeen(_SEEN_MAX)
_journal_cursor_epoch = 0.0
_journal_seen = _BoundedSeen(_SEEN_MAX)

_LOOP_STARTED = False
_LOOP_START_LOCK = threading.Lock()
_SCAN_INTERVAL_SEC = 30.0
_INITIAL_DELAY_SEC = 20.0
_INITIAL_BACKFILL_SECONDS = 15 * 60


def initialize_cursors(now=None):
    """Restore source cursors or select a bounded first-deployment backfill."""
    global _cursor_epoch, _journal_cursor_epoch
    now = time.time() if now is None else now
    fallback = max(0.0, now - _INITIAL_BACKFILL_SECONDS)
    with _CURSOR_LOCK:
        _cursor_epoch = detector_state.load_cursor("smartune") or fallback
        _journal_cursor_epoch = detector_state.load_cursor("journal") or fallback
    return {"smartune": _cursor_epoch, "journal": _journal_cursor_epoch}


def run_once(min_level="error"):
    """Scan sources once and emit abnormal events. Returns emitted count."""
    emitted = 0
    emitted += _scan_smartune(min_level=min_level)
    emitted += _scan_journal()
    return emitted


def _scan_smartune(min_level="error"):
    """Scan the smartune source once for new ERROR/CRITICAL lines and emit one
    event per line. Returns the number of events emitted. Best-effort."""
    global _cursor_epoch
    src = get_source("smartune")
    if src is None or not src.available():
        return 0

    with _CURSOR_LOCK:
        since = _cursor_epoch

    flt = LogQueryFilter(min_level=min_level, start_time=int(since) if since else None,
                         limit=_SMARTUNE_SCAN_LIMIT)
    try:
        records = src.query(flt) or []
    except Exception as exc:
        logger.warning("detector scan failed: %s", exc)
        return 0

    if len(records) >= _SMARTUNE_SCAN_LIMIT:
        logger.warning("smartune detector scan hit the %d-record cap; a burst larger "
                       "than one scan window may be partially deferred", _SMARTUNE_SCAN_LIMIT)
    # Oldest-first so the cursor advances monotonically. Dedup by a per-record
    # signature rather than a strict ts > cursor test: the source clamps to
    # second granularity, so several ERROR lines can share one timestamp and a
    # ``<= since`` test would silently drop the boundary siblings.
    records.sort(key=lambda r: r.ts_epoch)
    emitted = 0
    rules = custom_rules.configured_rules("smartune")
    high_water = since
    for rec in records:
        if rec.ts_epoch < since:
            continue
        signature = _smartune_signature(rec)
        if signature in _smartune_seen:
            continue
        _smartune_seen.add(signature)
        high_water = max(high_water, rec.ts_epoch)
        emitted += custom_rules.emit_matches(rec, rules, str(signature))
        if _emit_for_record(rec, identity=signature):
            emitted += 1

    with _CURSOR_LOCK:
        _cursor_epoch = max(_cursor_epoch, high_water)
        detector_state.save_cursor("smartune", _cursor_epoch)
    return emitted


def _smartune_signature(rec):
    """Stable per-line identity for boundary dedup (same-second siblings)."""
    line_no = (rec.fields or {}).get("line")
    return (round(rec.ts_epoch, 3), rec.logger, line_no, (rec.message or "")[:200])


def _emit_for_record(rec, identity=None):
    """Turn one ERROR/CRITICAL smartune record into an event. Detector-sourced,
    so ``source='detector'`` distinguishes it from a direct business emit."""
    severity = "critical" if (rec.level or "").upper() == "CRITICAL" else "error"
    first_line = (rec.message or "").strip().splitlines()[0] if rec.message else rec.level
    ev = emit_event(
        "LOG_EXCEPTION" if "Traceback" in (rec.message or "") else "LOG_ERROR",
        severity=severity,
        category=(rec.service or "service"),
        summary=f"[{rec.logger}] {first_line}",
        source="detector",
        app_id=rec.app_id,
        job_id=rec.job_id,
        attributes={"logger": rec.logger, "level": rec.level,
                    "excerpt": (rec.message or "")[:2000]},
        ts_utc=rec.ts_iso or None,
        identity=identity,
    )
    return ev is not None


_OOM_KILL_RE = re.compile(
    r"(?:out of memory:\s*)?killed process\s+(?P<pid>\d+)\s+\((?P<process>[^)]+)\)",
    re.IGNORECASE,
)
_SYSTEMD_RESTART_RE = re.compile(
    r"scheduled restart job, restart counter is at\s+(?P<count>\d+)", re.IGNORECASE,
)
_KERNEL_PANIC_RE = re.compile(r"\b(?:kernel panic|panic\s*-\s*not syncing)\b", re.IGNORECASE)
_GPU_HANG_RE = re.compile(r"\bgpu\s+hang\b", re.IGNORECASE)
_RESTART_LOOP_THRESHOLD = 3


def _scan_journal():
    """Turn high-confidence kernel faults and repeated systemd restarts into events.

    This intentionally starts with narrow patterns. GPU/NPU, thermal and storage
    rules require hardware-specific source data and remain separate stage-three
    additions rather than broad text matching here.
    """
    global _journal_cursor_epoch
    src = get_source("journal")
    if src is None or not src.available():
        return 0
    with _CURSOR_LOCK:
        since = _journal_cursor_epoch
    try:
        query_filter = LogQueryFilter(
            start_time=int(since) if since else None,
            end_time=int(time.time()), allow_unscoped=True,
            limit=_JOURNAL_SCAN_LIMIT)
        # Scan kernel records separately so a busy userspace journal cannot
        # push OOM kills, panics, or GPU hangs beyond the generic result cap.
        kernel_records = src.query(LogQueryFilter(
            start_time=query_filter.start_time,
            end_time=query_filter.end_time,
            allow_unscoped=True,
            kernel_only=True,
            limit=_JOURNAL_SCAN_LIMIT)) or []
        records = kernel_records + (src.query(query_filter) or [])
    except Exception as exc:
        logger.warning("journal detector scan failed: %s", exc)
        return 0

    if len(records) >= _JOURNAL_SCAN_LIMIT:
        logger.warning("journal detector scan hit the %d-record cap; a burst of kernel "
                       "faults larger than one scan window may be deferred", _JOURNAL_SCAN_LIMIT)
    emitted = 0
    rules = custom_rules.configured_rules("journal")
    high_water = since
    for rec in sorted(records, key=lambda record: record.ts_epoch):
        high_water = max(high_water, rec.ts_epoch)
        # Use cursor if available, otherwise build an identity from metadata + truncated message
        # to keep dedup_key bounded (rest of codebase truncates excerpts to [:500] or [:2000]).
        msg_trunc = (rec.message or "")[:120] if rec.message else ""
        identity = (rec.fields or {}).get("cursor") or f"{rec.ts_epoch}:{rec.logger}:{msg_trunc}"
        if identity in _journal_seen:
            continue
        _journal_seen.add(identity)
        emitted += custom_rules.emit_matches(rec, rules, str(identity))
        message = rec.message or ""
        panic = _KERNEL_PANIC_RE.search(message)
        if panic:
            event = emit_event(
                "PLATFORM_KERNEL_PANIC", severity="critical", category="platform.availability",
                impact="failed", summary="Kernel panic recorded by the system journal",
                source="journal", attributes={
                    "source_event_code": "journal.kernel_panic",
                    "raw_message": message[:2000],
                    "boot_id": (rec.fields or {}).get("boot_id"),
                    "journal_cursor": (rec.fields or {}).get("cursor"),
                }, ts_utc=rec.ts_iso or None, identity=identity,
            )
            emitted += int(event is not None)
            continue
        gpu_hang = _GPU_HANG_RE.search(message)
        if gpu_hang:
            event = emit_event(
                "DEVICE_GPU_HANG", severity="error", category="device.gpu",
                resource_type="gpu",
                impact="degraded", summary="GPU hang recorded by the system journal",
                source="journal", attributes={
                    "source_event_code": "journal.gpu_hang",
                    "raw_message": message[:2000],
                    "boot_id": (rec.fields or {}).get("boot_id"),
                    "journal_cursor": (rec.fields or {}).get("cursor"),
                }, ts_utc=rec.ts_iso or None, identity=identity,
            )
            emitted += int(event is not None)
            continue
        oom = _OOM_KILL_RE.search(message)
        if oom:
            event = emit_event(
                "RESOURCE_MEMORY_OOM_KILL", severity="error", category="resource.memory",
                resource_type="memory", impact="failed",
                summary=f"Kernel OOM killed process {oom.group('process')} (pid {oom.group('pid')})",
                source="journal", attributes={
                    "pid": oom.group("pid"), "process": oom.group("process"),
                    "boot_id": (rec.fields or {}).get("boot_id"),
                    "journal_cursor": (rec.fields or {}).get("cursor"),
                    "excerpt": (rec.message or "")[:500],
                }, ts_utc=rec.ts_iso or None, identity=identity,
            )
            emitted += int(event is not None)
            continue
        restart = _SYSTEMD_RESTART_RE.search(rec.message or "")
        if restart and int(restart.group("count")) >= _RESTART_LOOP_THRESHOLD:
            unit = rec.service or rec.logger
            event = emit_event(
                "PLATFORM_SYSTEMD_RESTART_LOOP", severity="error", category="platform.availability",
                summary=f"Service {unit} restarted {restart.group('count')} times",
                source="journal", attributes={
                    "unit": unit, "restart_count": int(restart.group("count")),
                    "boot_id": (rec.fields or {}).get("boot_id"),
                    "journal_cursor": (rec.fields or {}).get("cursor"),
                    "excerpt": (rec.message or "")[:500],
                }, ts_utc=rec.ts_iso or None, identity=identity,
            )
            emitted += int(event is not None)

    with _CURSOR_LOCK:
        _journal_cursor_epoch = max(_journal_cursor_epoch, high_water)
        detector_state.save_cursor("journal", _journal_cursor_epoch)
    return emitted


def start_detector_loop():
    """Start the background detector thread. Idempotent (mirrors the monitor's
    snapshot-cleanup task pattern): a short settle delay, then a periodic scan."""
    global _LOOP_STARTED
    with _LOOP_START_LOCK:
        if _LOOP_STARTED:
            return
        _LOOP_STARTED = True

    def loop():
        time.sleep(_INITIAL_DELAY_SEC)
        initialize_cursors()
        while True:
            try:
                run_once()
            except Exception as exc:
                logger.warning("detector loop iteration failed: %s", exc)
            time.sleep(_SCAN_INTERVAL_SEC)

    t = threading.Thread(target=loop, daemon=True, name="diag-detector")
    t.start()
