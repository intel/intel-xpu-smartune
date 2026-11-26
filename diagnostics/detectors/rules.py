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
# hang, thermal) register here the same way -- reusing the same alert
# dedup/cooldown (alert_state) so nothing re-implements throttling.
#
# Both scanners read bounded batches from a saved position (journald __CURSOR, byte offset per
# SmarTune log file) and advance only past what was processed, so a burst is delayed, never
# skipped. The event ledger's dedup_key drops duplicates from a re-read record.

import collections
import json
import re
import threading
import time

from diagnostics import custom_rules, emit_event
from diagnostics import detector_state, error_signatures, event_store
from diagnostics.detectors import kernel_signatures
from diagnostics.sources import get_source, kernel_blocks
from diagnostics.sources.base import level_rank
from utils.logger import get_logger

from utils.logger import diagnostics_internal

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


# Category of events from SmarTune's own error logs (the logging service goes in ``service``).
SERVICE_LOG_CATEGORY = "platform.service_log"

# Batch sizes and batches per tick; leftover backlog waits for the next tick.
_SMARTUNE_SCAN_LIMIT = 500
_SMARTUNE_MAX_BATCHES = 10
_JOURNAL_BATCH_LIMIT = 2000
_JOURNAL_MAX_BATCHES = 5
_SEEN_MAX = 8192


# A journal cursor older than the retention window is moved up to it: the next cleanup
# would delete earlier events anyway.
def _journal_catchup_seconds():
    from diagnostics import retention

    return retention.configured_days() * 86400

# journalctl matches for what built-in rules read: kernel, systemd, journald, oomd, coredump
# and err-level lines (``+`` is OR). Custom journal rules may match anything: a full read.
_JOURNAL_BUILTIN_MATCHES = ("_TRANSPORT=kernel", "+", "SYSLOG_IDENTIFIER=systemd",
                            "+", "SYSLOG_IDENTIFIER=systemd-journald",
                            "+", "SYSLOG_IDENTIFIER=systemd-oomd",
                            "+", "SYSLOG_IDENTIFIER=systemd-coredump",
                            "+", "PRIORITY=0", "+", "PRIORITY=1", "+", "PRIORITY=2",
                            "+", "PRIORITY=3")
_ERROR_RANK = level_rank("error")

_CURSOR_LOCK = threading.Lock()
_cursor_epoch = 0.0  # smartune: newest record processed (informational)
# smartune: log file name -> saved read position (None before the first scan). Lines older
# than _smartune_since are skipped until the first backfill has been read to the end.
_smartune_positions = None
_smartune_since = None
_journal_cursor_epoch = 0.0
_journal_cursor_text = None  # journald __CURSOR of the last processed entry
_journal_seen = _BoundedSeen(_SEEN_MAX)
# Repeat limiting for journal signatures (kernel faults, restart loops), on event time.
_journal_gate = kernel_signatures.SignatureGate()
# Re-emit interval of an ongoing restart loop; must stay below its alert lease (ttl 300 s).
_RESTART_LOOP_HEARTBEAT_SECONDS = 60
# New / bursting error templates across both sources (error_signatures.py).
_error_signatures = error_signatures.SignatureTracker()

# A record whose events failed to store is re-read by later scans, up to this many attempts,
# so one poisoned write cannot stall the scan.
_WRITE_RETRY_LIMIT = 10
_write_retries = collections.OrderedDict()
# Repeat-gate verdict per event identity, reused when a record is re-read: a second admit
# would find the burst already spent by the failed write and drop the retry.
_gate_verdicts = collections.OrderedDict()


# Per-source read health, as reported by /diag/sources.
_HEALTH_LOCK = threading.Lock()
_source_health = {}


def _record_health(source, error=None):
    now = time.time()
    with _HEALTH_LOCK:
        state = _source_health.setdefault(source, {
            "source": source, "last_ok": None, "failing_since": None,
            "last_error": None, "consecutive_failures": 0})
        if error is None:
            state.update(last_ok=now, failing_since=None, last_error=None, consecutive_failures=0)
        else:
            state["consecutive_failures"] += 1
            state["last_error"] = str(error)[:300]
            state["failing_since"] = state["failing_since"] or now


def source_health():
    """``[{source, last_ok, failing_since, last_error, consecutive_failures}]``
    for each source the detector has tried to read (epoch seconds)."""
    with _HEALTH_LOCK:
        return [dict(state) for state in _source_health.values()]


def _gate(identity, key, ts, **kwargs):
    """``_journal_gate.admit`` once per event identity; replays after that."""
    verdict = _gate_verdicts.get(identity)
    if verdict is None:
        verdict = _gate_verdicts[identity] = _journal_gate.admit(key, ts, **kwargs)
        while len(_gate_verdicts) > _SEEN_MAX:
            _gate_verdicts.popitem(last=False)
    return verdict


def _processed(identity, failures_before):
    """True once all events of record ``identity`` are stored or its retries are spent."""
    if event_store.write_failures() == failures_before:
        _write_retries.pop(identity, None)
        return True
    attempts = _write_retries.pop(identity, 0) + 1
    if attempts >= _WRITE_RETRY_LIMIT:
        logger.warning("detector gave up on a log record after %d failed event writes", attempts)
        return True
    _write_retries[identity] = attempts
    while len(_write_retries) > 256:
        _write_retries.popitem(last=False)
    return False

_LOOP_STARTED = False
_LOOP_START_LOCK = threading.Lock()
_SCAN_INTERVAL_SEC = 30.0
_INITIAL_DELAY_SEC = 20.0
_INITIAL_BACKFILL_SECONDS = 15 * 60


def initialize_cursors(now=None):
    """Restore source cursors or select a bounded first-deployment backfill."""
    global _cursor_epoch, _journal_cursor_epoch, _journal_cursor_text
    global _smartune_positions, _smartune_since
    now = time.time() if now is None else now
    fallback = max(0.0, now - _INITIAL_BACKFILL_SECONDS)
    journal_epoch, journal_text = detector_state.load_position("journal")
    smartune_epoch, smartune_text = detector_state.load_position("smartune")
    with _CURSOR_LOCK:
        _cursor_epoch = smartune_epoch
        _smartune_positions = _decode_positions(smartune_text)
        _smartune_since = (fallback if _smartune_positions is None
                           else _decode_since(smartune_text))
        _journal_cursor_epoch = journal_epoch or fallback
        _journal_cursor_text = journal_text if journal_epoch else None
    return {"smartune": _cursor_epoch, "smartune_files": _smartune_positions,
            "journal": _journal_cursor_epoch, "journal_cursor": _journal_cursor_text}


def _encode_positions(positions, since=None):
    state = {"files": positions}
    if since is not None:
        state["since"] = since
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def _decode_since(text):
    """The backfill floor saved with the positions, or None once it was read."""
    try:
        since = json.loads(text).get("since")
        return float(since) if since is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _decode_positions(text):
    """Saved SmarTune log positions, or None when absent or unreadable."""
    if not text:
        return None
    try:
        files = json.loads(text)["files"]
        return {str(name): [int(inode), int(offset), int(head)]
                for name, (inode, offset, head) in files.items()}
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return None


def run_once(min_level="error"):
    """Scan sources once and emit abnormal events. Returns emitted count.
    Its own log lines are marked diagnostics-internal so later scans skip them."""
    with diagnostics_internal():
        emitted = 0
        emitted += _scan_smartune(min_level=min_level)
        emitted += _scan_journal()
        try:
            _error_signatures.flush()
        except Exception as exc:
            logger.warning("error signature flush failed: %s", exc)
        return emitted


# --- smartune application log -------------------------------------------------

def _scan_smartune(min_level="error"):
    """Drain new ERROR/CRITICAL smartune lines in batches and emit one event
    per line. Returns the number of events emitted. Best-effort."""
    src = get_source("smartune")
    if src is None or not src.available() or not hasattr(src, "scan_forward"):
        _record_health("smartune", "SmarTune log directory is not available")
        return 0
    emitted = 0
    for _ in range(_SMARTUNE_MAX_BATCHES):
        batch_emitted, saturated = _scan_smartune_batch(src, min_level)
        emitted += batch_emitted
        if not saturated:
            return emitted
    logger.warning("smartune detector still has backlog after %d batches of %d; "
                   "continuing next scan", _SMARTUNE_MAX_BATCHES, _SMARTUNE_SCAN_LIMIT)
    return emitted


def _scan_smartune_batch(src, min_level):
    """One batch of newly appended lines. Returns ``(emitted, saturated)``."""
    global _cursor_epoch, _smartune_positions, _smartune_since
    with _CURSOR_LOCK:
        positions, since = _smartune_positions, _smartune_since
    try:
        scan = src.scan_forward(positions, min_level=min_level, since_epoch=since,
                                max_records=_SMARTUNE_SCAN_LIMIT)
    except Exception as exc:
        logger.warning("detector scan failed: %s", exc)
        _record_health("smartune", exc)
        return 0, False
    _record_health("smartune")

    emitted = 0
    rules = custom_rules.configured_rules("smartune")
    high_water = 0.0
    processed = 0
    stalled = False
    for rec in scan.records:
        if (rec.fields or {}).get("diag_internal"):
            # Diagnostics' own error: an event for it would only feed a failing store.
            processed += 1
            continue
        signature = _smartune_signature(rec)
        failures = event_store.write_failures()
        try:
            emitted += _process_smartune_record(rec, signature, rules)
        except Exception as exc:  # a bad record must not stop the scan
            logger.warning("detector skipped a log record: %s", exc)
        if not _processed(signature, failures):
            # Stop here: the position stays before this record, so the next scan re-reads it.
            stalled = True
            break
        processed += 1
        high_water = max(high_water, rec.ts_epoch)

    new_positions = scan.positions(processed)
    with _CURSOR_LOCK:
        _smartune_positions = new_positions
        if scan.complete and not stalled:
            _smartune_since = None  # the backlog is read: later lines are all new
        _cursor_epoch = max(_cursor_epoch, high_water)
        detector_state.save_cursor("smartune", _cursor_epoch,
                                   _encode_positions(new_positions, _smartune_since))
    return emitted, not scan.complete and not stalled


def _process_smartune_record(rec, signature, rules):
    """Custom rules, error signatures and the LOG_ERROR event for one line."""
    emitted = custom_rules.emit_matches(rec, rules, str(signature))
    fingerprint = None
    if not emitted:
        fingerprint, findings = _observe_error(
            rec, source="smartune", origin=rec.logger or rec.service, key=signature)
        for finding in findings:
            emitted += int(_emit_error_signature(finding, rec, (rec.message or "")[:2000]))
    if _emit_for_record(rec, identity=signature, fingerprint=fingerprint):
        emitted += 1
    return emitted


def _smartune_signature(rec):
    """Stable per-line identity (file, offset, content), the same when a line is re-read."""
    fields = rec.fields or {}
    return (fields.get("file"), fields.get("offset"), round(rec.ts_epoch, 3), rec.logger,
            (rec.message or "")[:200])


def _emit_for_record(rec, identity=None, fingerprint=None):
    """Turn one ERROR/CRITICAL smartune record into an event. Detector-sourced,
    so ``source='detector'`` distinguishes it from a direct business emit.
    ``fingerprint`` is its error signature, so repeats of one error group."""
    severity = "critical" if (rec.level or "").upper() == "CRITICAL" else "error"
    first_line = (rec.message or "").strip().splitlines()[0] if rec.message else rec.level
    ev = emit_event(
        "LOG_EXCEPTION" if "Traceback" in (rec.message or "") else "LOG_ERROR",
        severity=severity,
        category=SERVICE_LOG_CATEGORY,
        service=rec.service,
        summary=f"[{rec.logger}] {first_line}",
        source="detector",
        app_id=rec.app_id,
        job_id=rec.job_id,
        attributes={"logger": rec.logger, "level": rec.level,
                    "excerpt": (rec.message or "")[:2000],
                    **({"fingerprint": fingerprint} if fingerprint else {})},
        ts_utc=rec.ts_iso or None,
        identity=identity,
    )
    return ev is not None


# --- journald ----------------------------------------------------------------------

_OOM_KILL_RE = re.compile(
    r"(?:out of memory:\s*)?killed process\s+(?P<pid>\d+)\s+\((?P<process>[^)]+)\)",
    re.IGNORECASE,
)
_SYSTEMD_RESTART_RE = re.compile(
    r"scheduled restart job, restart counter is at\s+(?P<count>\d+)", re.IGNORECASE,
)
# systemd-oomd killing a cgroup under memory pressure, or because swap ran out.
_OOMD_KILL_RE = re.compile(r"^Killed (?P<cgroup>/\S+) due to (?P<reason>memory pressure|memory used)")
# journald found a journal file still open from before: the host stopped without a clean
# shutdown (crash, hang, reset or power loss).
_UNCLEAN_JOURNAL_RE = re.compile(r"\buncleanly shut down\b")
_KERNEL_PANIC_RE = re.compile(r"\b(?:kernel panic|panic\s*-\s*not syncing)\b", re.IGNORECASE)
# The "---[ end Kernel panic ... ]---" trailer of a panic report raises no event of its own.
_PANIC_END_RE = re.compile(r"^\s*---\[ end ")
# i915 prints "GPU HANG"; xe never does, so its timed-out job, engine reset, failed GT reset
# and wedged-device lines are matched instead.
_GPU_HANG_RE = re.compile(r"\bgpu\s+hang\b", re.IGNORECASE)
_XE_GPU_FAULT_RE = re.compile(
    r"\btimedout job\b|\bengine reset\b|\bas wedged\b|\bdevice wedged\b"
    r"|\bGT\d+: reset failed\b",
    re.IGNORECASE,
)
_DRM_DRIVER_RE = re.compile(r"^(?:xe|i915)\s|\[drm\]")
_PCI_ADDRESS_RE = re.compile(r"\b[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]\b", re.IGNORECASE)
_RESTART_LOOP_THRESHOLD = 3


def _journal_matches():
    if custom_rules.configured_rules("journal"):
        return []
    return list(_JOURNAL_BUILTIN_MATCHES)


def _scan_journal():
    """Drain new journal entries in cursor-ordered batches and emit events for
    high-confidence kernel faults and repeated systemd restarts.

    Matching keeps to narrow, format-anchored patterns rather than broad text search.
    """
    src = get_source("journal")
    if src is None or not src.available() or not hasattr(src, "scan_forward"):
        _record_health("journal", "journalctl is not available")
        return 0
    emitted = 0
    for _ in range(_JOURNAL_MAX_BATCHES):
        scan = _read_journal_batch(src)
        if scan is None:
            return emitted
        batch_emitted, complete = _process_journal_records(scan.records)
        emitted += batch_emitted
        if not complete:
            # A write failed: keep the cursor so the next scan re-reads this batch.
            return emitted
        _advance_journal_cursor(scan)
        if scan.complete:
            return emitted
    logger.warning("journal detector still has backlog after %d batches of %d; "
                   "continuing next scan", _JOURNAL_MAX_BATCHES, _JOURNAL_BATCH_LIMIT)
    return emitted


def _read_journal_batch(src):
    """Next batch after the stored cursor (or its epoch when the cursor is unusable), as a
    JournalScan; None on failure."""
    with _CURSOR_LOCK:
        since, cursor = _journal_cursor_epoch, _journal_cursor_text
    catchup = _journal_catchup_seconds()
    floor = time.time() - catchup
    if since < floor:
        logger.warning("journal detector was idle past the %dh retention window; "
                       "scanning from %s", catchup // 3600, int(floor))
        since, cursor = floor, None
    matches = _journal_matches()
    try:
        scan = src.scan_forward(after_cursor=cursor, since_epoch=since,
                                max_records=_JOURNAL_BATCH_LIMIT, matches=matches)
        if cursor and scan.error == "cursor_invalid":
            logger.warning("journal cursor unusable (%s); resuming from epoch %s",
                           scan.error, int(since))
            scan = src.scan_forward(since_epoch=since, max_records=_JOURNAL_BATCH_LIMIT,
                                    matches=matches)
    except Exception as exc:
        logger.warning("journal detector scan failed: %s", exc)
        _record_health("journal", exc)
        return None
    if scan.error:
        _record_health("journal", f"journalctl scan failed ({scan.error})")
        return None
    _record_health("journal")
    return scan


def _advance_journal_cursor(scan):
    global _journal_cursor_epoch, _journal_cursor_text
    if not scan.last_cursor:
        return
    with _CURSOR_LOCK:
        _journal_cursor_text = scan.last_cursor
        # The cursor entry's own time, not a high-water mark, which a clock stepped back
        # would leave in the future.
        _journal_cursor_epoch = scan.last_epoch or _journal_cursor_epoch
        detector_state.save_cursor("journal", _journal_cursor_epoch, _journal_cursor_text)


def _process_journal_records(records):
    """Match rules per line, with the whole kernel report as evidence. Returns
    ``(emitted, complete)``; ``complete`` is False when a report's events failed to store."""
    emitted = 0
    rules = custom_rules.configured_rules("journal")
    for group in kernel_blocks.group_records(records):
        failures = event_store.write_failures()
        identities = []
        try:
            emitted += _process_journal_group(group, rules, identities)
        except Exception as exc:  # a bad record must not stop the scan
            logger.warning("journal detector skipped a record: %s", exc)
        if identities and not _processed(identities[0], failures):
            return emitted, False
        for identity in identities:
            _journal_seen.add(identity)
    return emitted, True


def _process_journal_group(group, rules, identities):
    """One kernel report (or a lone line). Appends the identities it handled to
    ``identities``; the caller marks them seen once the events are stored."""
    emitted = 0
    block = kernel_blocks.merge_block(group) if len(group) > 1 else None
    excerpt = kernel_blocks.block_excerpt(block.message) if block else None
    head_identity = None
    # A report a rule recognised is not fed to the error-signature tracker.
    recognised = False
    for index, rec in enumerate(group):
        fields = rec.fields or {}
        # The journal cursor is the natural identity; fall back to metadata + a
        # truncated message so dedup_key stays bounded.
        msg_trunc = (rec.message or "")[:120]
        identity = fields.get("cursor") or f"{rec.ts_epoch}:{rec.logger}:{msg_trunc}"
        if identity in _journal_seen:
            continue
        identities.append(identity)
        if index == 0:
            head_identity = identity
        matched = custom_rules.emit_matches(rec, rules, str(identity), excerpt=excerpt)
        emitted += matched
        emitted += int(_emit_journal_builtin(rec, identity, block=block, excerpt=excerpt))
        line_signals = kernel_signatures.classify_line(rec)
        for signal in line_signals:
            emitted += int(_emit_signal(signal, str(identity), excerpt or rec.message))
        crash_signals = (kernel_signatures.classify_coredump(rec)
                         + kernel_signatures.classify_unit_exit(rec))
        for signal in crash_signals:
            emitted += int(_emit_signal(signal, str(identity), rec.message))
        recognised = recognised or bool(matched or line_signals or crash_signals
                                        or _is_builtin_fact(rec))
    # Report-level signals once per report, only when its first line is new, so a report
    # straddling a rescan is not classified twice.
    if head_identity is not None:
        text = excerpt or group[0].message
        block_signals = kernel_signatures.classify_block(group)
        for signal in block_signals:
            emitted += int(_emit_signal(signal, f"block:{head_identity}", text))
        if not (recognised or block_signals):
            emitted += _observe_journal_error(group, block, excerpt, key=head_identity)
    return emitted


def _is_builtin_fact(rec):
    """Whether a built-in journal rule recognises ``rec``, even if its gate held the event back."""
    message = rec.message or ""
    if kernel_blocks.is_kernel(rec):
        return bool(_KERNEL_PANIC_RE.search(message) or _is_gpu_fault(message)
                    or _OOM_KILL_RE.search(message))
    return bool(_SYSTEMD_RESTART_RE.search(message) or _oomd_kill(rec) or _unclean_journal(rec))


def _oomd_kill(rec):
    return _OOMD_KILL_RE.search(rec.message or "") if rec.logger == "systemd-oomd" else None


def _unclean_journal(rec):
    return rec.logger == "systemd-journald" and bool(_UNCLEAN_JOURNAL_RE.search(rec.message or ""))


# --- error signatures ----------------------------------------------------------

def _observe_error(rec, *, source, origin, message=None, key=None):
    """Feed one error to the signature tracker; ``key`` keeps a re-read record from counting
    twice. Returns ``(fingerprint, findings)``, or ``(None, [])`` when it yields no template."""
    message = rec.message if message is None else message
    try:
        template = error_signatures.normalize(message)
        if not template:
            return None, []
        findings = _error_signatures.observe(
            source=source, origin=origin, message=message, ts_epoch=rec.ts_epoch, key=key,
            template=template)
        return error_signatures.fingerprint(source, origin, template), findings
    except Exception as exc:
        logger.warning("error signature tracking failed: %s", exc)
        return None, []


def _observe_journal_error(group, block, excerpt, key=None):
    """An err-level journal line (or kernel report) no rule recognised."""
    worst = block if block is not None else group[0]
    if level_rank(worst.level) < _ERROR_RANK:
        return 0
    head = group[0]
    message = kernel_blocks.block_title(group) if block is not None else head.message
    if kernel_blocks.is_kernel(head):
        driver = (head.fields or {}).get("kernel_driver")
        origin = f"kernel:{driver}" if driver else "kernel"
    elif head.logger == "systemd" and head.service and head.service != "init.scope" \
            and not head.service.startswith("user@"):
        # systemd reporting on a unit (UNIT= / USER_UNIT=): the unit is the
        # component, or every failing unit shares one alert and burst budget.
        origin = f"systemd:{error_signatures.normalize_unit(head.service)}"
    else:
        origin = head.logger or head.service or "journal"
    _, findings = _observe_error(head, source="journal", origin=origin, message=message, key=key)
    evidence = excerpt or head.message or ""
    return sum(int(_emit_error_signature(finding, head, evidence)) for finding in findings)


def _emit_error_signature(finding, rec, evidence):
    """Emit a NEW / BURST error-signature event (one per template). Only BURST alerts, one
    alert per logging component (attributes.subject); NEW is evidence for later analysis."""
    fields = rec.fields or {}
    attributes = {
        "fingerprint": finding.fingerprint,
        "subject": f"{finding.source}:{finding.origin}",
        "origin": finding.origin,
        "log_source": finding.source,
        "template": finding.template,
        "sample": finding.sample,
        "excerpt": kernel_blocks.block_excerpt(evidence or ""),
        "app_id": rec.app_id,
        "job_id": rec.job_id,
        "boot_id": fields.get("boot_id"),
        "journal_cursor": fields.get("cursor"),
        **finding.attributes,
    }
    attributes = {k: v for k, v in attributes.items() if v is not None}
    where = f"{finding.origin}: " if finding.origin else ""
    if finding.event_type == error_signatures.NEW:
        severity = "warning"
        summary = f"New error from {where}{finding.sample[:200]}"
        identity = f"sig-new:{finding.fingerprint}"
    else:
        severity = "error"
        summary = (f"Error burst: {finding.attributes['count_in_window']} in "
                   f"{finding.attributes['window_seconds'] // 60} min "
                   f"(usually {finding.attributes['expected_in_window']:g}) from {where}"
                   f"{finding.sample[:160]}")
        identity = f"sig-burst:{finding.fingerprint}:{int(finding.ts_epoch)}"
    return emit_event(
        finding.event_type, severity=severity,
        # No service field: it would replace attributes.subject (source and
        # logger) as the alert scope, folding every logger of a service into one alert.
        category="platform.availability" if finding.source == "journal" else SERVICE_LOG_CATEGORY,
        summary=summary, source="detector", attributes=attributes,
        ts_utc=rec.ts_iso or None, identity=identity,
        boot_id=fields.get("boot_id")) is not None


def _emit_signal(signal, identity, evidence):
    """Emit one kernel_signatures.Signal through the repeat gate."""
    rec = signal.record
    fields = rec.fields or {}
    boot = fields.get("boot_id")
    event_identity = f"{signal.event_type}:{identity}"
    admitted, counts = _gate(
        event_identity, (signal.event_type, boot, signal.subject), rec.ts_epoch,
        burst=signal.burst, window=signal.window)
    if not admitted:
        return False
    attributes = {
        "source_event_code": f"journal.{signal.event_type.lower()}",
        "subject": signal.subject,
        "excerpt": kernel_blocks.block_excerpt(evidence or ""),
        "boot_id": boot,
        "journal_cursor": fields.get("cursor"),
        **counts,
        **{k: v for k, v in signal.attributes.items() if v is not None},
    }
    return emit_event(
        signal.event_type, severity=signal.severity, category=signal.category,
        summary=signal.summary, resource_type=signal.resource_type, impact=signal.impact,
        service=signal.service, attributes=attributes,
        source="journal", ts_utc=rec.ts_iso or None, identity=event_identity,
        boot_id=boot) is not None


def _emit_journal_builtin(rec, identity, block=None, excerpt=None):
    """Apply the built-in journal rules to one record; True if an event was written.
    ``block`` is the merged kernel report holding ``rec`` (used as evidence), or None."""
    fields = rec.fields or {}
    message = rec.message or ""
    kernel = fields.get("transport") == "kernel"
    evidence = excerpt or message
    common = {
        "boot_id": fields.get("boot_id"),
        "journal_cursor": fields.get("cursor"),
    }
    if block is not None:
        common.update({
            "kernel_block": block.fields.get("block"),
            "block_lines": block.fields.get("line_count"),
            "block_cursor": block.fields.get("cursor"),
        })
    # Replayed facts belong to the boot they happened in, not the current one.
    emit_kwargs = {"source": "journal", "ts_utc": rec.ts_iso or None,
                   "identity": identity, "boot_id": fields.get("boot_id")}

    if kernel and _KERNEL_PANIC_RE.search(message):
        if _PANIC_END_RE.match(message):
            return False
        return emit_event(
            "PLATFORM_KERNEL_PANIC", severity="critical", category="platform.availability",
            impact="failed", summary="Kernel panic recorded by the system journal",
            attributes={"source_event_code": "journal.kernel_panic",
                        "raw_message": evidence[:4000], **common},
            **emit_kwargs) is not None

    if kernel and _is_gpu_fault(message):
        device = _gpu_device(rec)
        # One hang logs a timed-out job per stuck context plus the reset lines.
        admitted, counts = _gate(
            f"DEVICE_GPU_HANG:{identity}",
            ("DEVICE_GPU_HANG", fields.get("boot_id"), device), rec.ts_epoch, burst=5, window=600)
        if not admitted:
            return False
        common.update(counts)
        return emit_event(
            "DEVICE_GPU_HANG", severity="error", category="device.gpu",
            resource_type="gpu", impact="degraded",
            summary=f"GPU hang/reset recorded by the system journal{f' on {device}' if device else ''}",
            attributes={"source_event_code": "journal.gpu_hang",
                        "raw_message": evidence[:4000], "device": device,
                        "subject": device, **common},
            **emit_kwargs) is not None

    oom = _OOM_KILL_RE.search(message) if kernel else None
    if oom:
        process = oom.group("process")
        return emit_event(
            "RESOURCE_MEMORY_OOM_KILL", severity="error", category="resource.memory",
            resource_type="memory", impact="failed",
            summary=f"Kernel OOM killed process {process} (pid {oom.group('pid')})",
            attributes={"pid": oom.group("pid"), "process": process, "killer": "kernel",
                        "cgroup_oom": "memory cgroup out of memory" in message.lower(),
                        "subject": process, "excerpt": evidence[:4000], **common},
            **emit_kwargs) is not None

    oomd = _oomd_kill(rec)
    if oomd:
        cgroup = oomd.group("cgroup")
        process = cgroup.rstrip("/").rsplit("/", 1)[-1]
        reason = "memory pressure" if oomd.group("reason") == "memory pressure" else "swap exhausted"
        return emit_event(
            "RESOURCE_MEMORY_OOM_KILL", severity="error", category="resource.memory",
            resource_type="memory", impact="failed",
            summary=f"systemd-oomd killed {process} ({reason})",
            attributes={"process": process, "cgroup": cgroup, "killer": "systemd-oomd",
                        "reason": reason, "cgroup_oom": False, "subject": process,
                        "excerpt": message[:4000], **common},
            **emit_kwargs) is not None

    if _unclean_journal(rec):
        # One event per boot, however many journal files were left open.
        return emit_event(
            "PLATFORM_HOST_UNCLEAN_SHUTDOWN", severity="error", category="platform.availability",
            impact="failed",
            summary="The host stopped without a clean shutdown before this boot "
                    "(crash, hang, reset or power loss)",
            attributes={"source_event_code": "journal.unclean_shutdown",
                        "excerpt": message[:500], **common},
            **{**emit_kwargs, "identity": "unclean-shutdown"}) is not None

    restart = _SYSTEMD_RESTART_RE.search(message) if not kernel else None
    if restart and int(restart.group("count")) >= _RESTART_LOOP_THRESHOLD:
        # fields["unit"] is the unit the line is about (UNIT= / USER_UNIT=), not its logger.
        unit = fields.get("unit") or rec.service or _unit_from_message(message) or rec.logger
        count = int(restart.group("count"))
        # One event per loop per heartbeat, not per restart. A counter that went down
        # (manual restart) starts a new loop at once.
        admitted, counts = _gate(
            f"PLATFORM_SYSTEMD_RESTART_LOOP:{identity}",
            ("PLATFORM_SYSTEMD_RESTART_LOOP", fields.get("boot_id"), unit), rec.ts_epoch,
            burst=1, window=_RESTART_LOOP_HEARTBEAT_SECONDS, value=count)
        if not admitted:
            return False
        return emit_event(
            "PLATFORM_SYSTEMD_RESTART_LOOP", severity="error", category="platform.availability",
            summary=f"Service {unit} restarted {count} times",
            service=unit,
            attributes={"unit": unit, "restart_count": count,
                        "restarts_since_last_event": counts["suppressed_since_last"] + 1,
                        "excerpt": message[:500], **common},
            **emit_kwargs) is not None
    return False


def _is_gpu_fault(message):
    if _GPU_HANG_RE.search(message):
        return True
    return bool(_DRM_DRIVER_RE.search(message) and _XE_GPU_FAULT_RE.search(message))


def _gpu_device(rec):
    """Best stable name for the GPU a fault line is about: the PCI address."""
    fields = rec.fields or {}
    for candidate in (fields.get("udev_sysname"), fields.get("kernel_device"), rec.message or ""):
        match = _PCI_ADDRESS_RE.search(str(candidate or ""))
        if match:
            return match.group(0).lower()
    return None


def _unit_from_message(message):
    """``foo.service: Scheduled restart job ...`` -> ``foo.service``."""
    head, sep, _ = message.partition(": ")
    return head if sep and " " not in head else None


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
                started = time.monotonic()
                emitted = run_once()
                elapsed = time.monotonic() - started
                if elapsed > 5:
                    logger.info("detector scan took %.1fs (%d events)", elapsed, emitted)
            except Exception as exc:
                logger.warning("detector loop iteration failed: %s", exc)
            time.sleep(_SCAN_INTERVAL_SEC)

    t = threading.Thread(target=loop, daemon=True, name="diag-detector")
    t.start()
