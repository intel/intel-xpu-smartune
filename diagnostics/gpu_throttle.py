# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Record debounced GPU throttle transitions from persisted MonitorSnapshot data.
# The kernel reports whether a GT is currently held below its requested
# frequency and why (power limits, thermal, ...), so this records a fact the
# hardware already confirmed rather than inferring one from power or frequency.
# The snapshot's ``throttled`` already excludes GTs that are asleep (see
# monitor.metrics.gpu_perf.gt_effectively_throttled), so an idle GT sharing a
# package-wide limit never reaches this tracker.
#
# GpuThrottleTracker holds the debounce logic; the driver reads snapshots and
# emits confirmed transitions. Reading persisted data also supports
# monitor-only mode, like the pressure tracker in episodes.py.

import json
import threading
import time

from utils.logger import get_logger

logger = get_logger(__name__)

# A transition is confirmed only after this many consecutive *distinct* samples
# agree. Short PL1/PL2 excursions under bursty load would otherwise flap.
CONFIRM_SAMPLES = 3

_SCAN_INTERVAL_SEC = 5.0
_INITIAL_DELAY_SEC = 25.0

# Short wording for the xe throttle reason flags. Kept brief because the alert
# center shows the summary on one ellipsized line.
_REASON_LABELS = {
    "pl1": "PL1",
    "pl2": "PL2",
    "pl4": "PL4",
    "thermal": "thermal",
    "prochot": "PROCHOT",
    "ratl": "RATL",
    "vr_tdc": "VR TDC",
    "vr_thermalert": "VR thermal alert",
}


def reason_label(reason):
    return _REASON_LABELS.get(reason, reason)


class GpuThrottleTracker:
    """Pure debounced throttle detector for one GT. Feed observe(throttled,
    reasons, ts) in time order; a repeated ts is ignored so a scan that re-reads
    the same snapshot does not count twice. Returns an action dict when the
    confirmed state changes, else None. No IO -- the driver does the emit."""

    def __init__(self, confirm_samples=CONFIRM_SAMPLES):
        self.confirm_samples = confirm_samples
        self._confirmed = False
        self._pending = 0          # consecutive samples disagreeing with confirmed
        self._last_ts = None
        self._last_reasons = []

    @property
    def throttled(self):
        return self._confirmed

    def observe(self, throttled, reasons, ts):
        if ts is not None and ts == self._last_ts:
            return None
        self._last_ts = ts
        throttled = bool(throttled)
        reasons = list(reasons or [])

        if throttled == self._confirmed:
            self._pending = 0
            if throttled:
                # Keep the freshest cause list so the clear event is accurate.
                self._last_reasons = reasons
            return None

        self._pending += 1
        if throttled:
            self._last_reasons = reasons
        if self._pending < self.confirm_samples:
            return None

        self._confirmed = throttled
        self._pending = 0
        if throttled:
            return {"state": "throttled", "reasons": reasons}
        previous = self._last_reasons
        self._last_reasons = []
        return {"state": "cleared", "reasons": previous}


_trackers = {}

_LOOP_STARTED = False
_LOOP_START_LOCK = threading.Lock()


def _latest_snapshot():
    """Return (devices, ts_epoch) from the newest dynamic MonitorSnapshot, or
    None when unavailable."""
    try:
        from db.DatabaseModel import MonitorSnapshot
    except Exception as exc:
        logger.debug("gpu throttle read unavailable: %s", exc)
        return None
    rows = MonitorSnapshot.query_recent(snapshot_type="dynamic", limit=1)
    if not rows:
        return None
    row = rows[0]
    try:
        payload = json.loads(row.data_json) if row.data_json else {}
    except (ValueError, TypeError):
        return None
    parsed = ((payload.get("gpu") or {}).get("gpu_usage") or {}).get("parsed") or {}
    devices = parsed.get("devices") or []
    return devices, int(row.create_time or time.time())


def _subject(device, freq):
    return f"gpu:{device.get('pci_dev') or device.get('drv_name') or '-'}/{freq.get('name') or 'gt'}"


def _summary(subject, action):
    gt = subject.split("/", 1)[-1]
    if action["state"] == "throttled":
        causes = ", ".join(reason_label(r) for r in action["reasons"]) or "unknown cause"
        return f"GPU {gt} throttled: {causes}"
    return f"GPU {gt} throttling cleared"


def _emit(subject, action):
    from diagnostics import emit_event, event_catalog

    if action["state"] == "throttled":
        emit_event(
            "DEVICE_GPU_THROTTLED", severity="warning", category="device.gpu",
            summary=_summary(subject, action), source=subject,
            attributes={"reasons": action["reasons"]},
        )
        return
    # Only close a throttle that was recorded; if the event type is disabled
    # there is nothing to recover from.
    if event_catalog.enabled("DEVICE_GPU_THROTTLED"):
        emit_event(
            "DEVICE_GPU_THROTTLE_CLEARED", severity="info", category="device.gpu",
            summary=_summary(subject, action), source=subject,
            attributes={"reasons": action["reasons"]},
        )


def run_once():
    """Read the latest snapshot, advance each GT's tracker, emit confirmed
    transitions. Best-effort; returns the list of (subject, action) emitted."""
    snap = _latest_snapshot()
    if snap is None:
        return []
    devices, ts = snap
    emitted = []
    for device in devices:
        for freq in device.get("freqs") or []:
            subject = _subject(device, freq)
            tracker = _trackers.setdefault(subject, GpuThrottleTracker())
            action = tracker.observe(
                freq.get("throttled"), freq.get("throttle_reasons"), ts)
            if action is None:
                continue
            try:
                _emit(subject, action)
            except Exception as exc:  # never let a persistence hiccup kill the loop
                logger.warning("gpu throttle persistence failed: %s", exc)
            emitted.append((subject, action))
    return emitted


def start_gpu_throttle_loop():
    """Start the background throttle-tracking thread. Idempotent daemon thread
    that mirrors start_pressure_loop in episodes.py."""
    global _LOOP_STARTED
    with _LOOP_START_LOCK:
        if _LOOP_STARTED:
            return
        _LOOP_STARTED = True

    def loop():
        time.sleep(_INITIAL_DELAY_SEC)
        while True:
            try:
                run_once()
            except Exception as exc:
                logger.warning("gpu throttle loop iteration failed: %s", exc)
            time.sleep(_SCAN_INTERVAL_SEC)

    threading.Thread(target=loop, daemon=True, name="diag-gpu-throttle").start()
