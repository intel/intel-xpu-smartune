# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Record debounced system-pressure transitions from persisted MonitorSnapshot
# data. Events are point-in-time records; consumers can derive durations from
# adjacent transitions. Reading persisted data also supports monitor-only mode.
#
# PressureLevelTracker contains the debounce logic; the driver reads snapshots
# and emits confirmed transitions.

import json
import threading
import time

from utils.logger import get_logger

logger = get_logger(__name__)

# Pressure bands. low/medium collapse to "normal"; only high/critical are recorded
# as elevated (monitor/pressure.py vocabulary is low/medium/high/critical).
_NORMAL = "normal"
_BAND_RANK = {_NORMAL: 0, "high": 1, "critical": 2}
_BAND_SEVERITY = {_NORMAL: "info", "high": "warning", "critical": "critical"}

# Dwell times debounce rising and falling transitions.
TRIGGER_SECONDS = 30.0   # dwell before confirming a rise (to a more severe band)
CLEAR_SECONDS = 60.0     # dwell before confirming a fall (to a less severe band)

_SCAN_INTERVAL_SEC = 15.0
_INITIAL_DELAY_SEC = 20.0


def _band(level):
    """Map a raw pressure level to its recorded band; unknown/low/medium -> normal."""
    level = (level or "").lower()
    return level if level in ("high", "critical") else _NORMAL


class PressureLevelTracker:
    """Pure debounced level-change detector for one pressure subject. Feed
    observe(level, score, ts) samples in time order; it returns an action dict when
    the confirmed band changes, otherwise None. No IO -- the driver does emit."""

    def __init__(self, trigger_seconds=TRIGGER_SECONDS, clear_seconds=CLEAR_SECONDS):
        self.trigger_seconds = trigger_seconds
        self.clear_seconds = clear_seconds
        # Start at normal so sustained pressure after startup is recorded.
        self._confirmed = _NORMAL
        self._pending_dir = 0         # +1 diverging up, -1 diverging down, 0 settled
        self._pending_since = None    # ts divergence in this direction began

    @property
    def level(self):
        return self._confirmed

    def observe(self, level, score, ts):
        band = _band(level)

        if band == self._confirmed:
            self._pending_dir = 0
            self._pending_since = None
            return None

        # Track dwell by direction so continued movement in the same direction
        # does not restart the timer; confirm the current band when it expires.
        direction = 1 if _BAND_RANK[band] > _BAND_RANK[self._confirmed] else -1
        if self._pending_dir != direction:
            self._pending_dir = direction
            self._pending_since = ts

        dwell = self.trigger_seconds if direction > 0 else self.clear_seconds
        if ts - self._pending_since < dwell:
            return None

        from_level = self._confirmed
        self._confirmed = band
        self._pending_dir = 0
        self._pending_since = None
        return {
            # raw_level carries the true monitor level (low/medium/high/critical);
            # to_level is the collapsed band, so summaries can name the real level.
            "from_level": from_level, "to_level": band, "raw_level": (level or "").lower(),
            "score": score, "severity": _BAND_SEVERITY[band],
        }


_tracker = PressureLevelTracker()

_LOOP_STARTED = False
_LOOP_START_LOCK = threading.Lock()


def _latest_pressure():
    """Return (level, score, ts_epoch) from the newest dynamic MonitorSnapshot, or
    None when unavailable (no snapshot / no pressure section)."""
    try:
        from db.DatabaseModel import MonitorSnapshot
    except Exception as exc:
        logger.debug("pressure read unavailable: %s", exc)
        return None
    rows = MonitorSnapshot.query_recent(snapshot_type="dynamic", limit=1)
    if not rows:
        return None
    row = rows[0]
    try:
        payload = json.loads(row.data_json) if row.data_json else {}
    except (ValueError, TypeError):
        return None
    pressure = payload.get("pressure") or {}
    level = pressure.get("level")
    if not level:
        return None
    return level, pressure.get("score"), int(row.create_time or time.time())


def run_once():
    """Read the latest pressure sample, advance the tracker, emit any confirmed
    level change. Best-effort; returns the action dict or None."""
    reading = _latest_pressure()
    if reading is None:
        return None
    level, score, ts = reading
    action = _tracker.observe(level, score, ts)
    if action is None:
        return None
    try:
        _emit_change(action)
    except Exception as exc:  # never let a persistence hiccup kill the loop
        logger.warning("pressure change persistence failed: %s", exc)
    return action


def _summary(from_level, to_level, score, raw_level=None):
    # Prefer the true monitor level so a fall below "high" still reads as medium/low.
    level = raw_level or to_level
    score_txt = f" (score={score})" if score is not None else ""
    verb = "rose to" if _BAND_RANK[to_level] > _BAND_RANK[from_level] else "fell to"
    return f"System pressure {verb} {level}{score_txt}"


def _emit_change(action):
    from diagnostics import emit_event

    from_level, to_level = action["from_level"], action["to_level"]
    raw_level = action.get("raw_level")
    emit_event(
        "RESOURCE_SYSTEM_PRESSURE_CHANGED", severity=action["severity"],
        category="resource.system",
        summary=_summary(from_level, to_level, action.get("score"), raw_level),
        source="monitor",
        attributes={"from_level": from_level, "to_level": to_level,
                    "raw_level": raw_level, "score": action.get("score")},
    )


def start_pressure_loop():
    """Start the background pressure-tracking thread. Idempotent daemon thread that
    mirrors the detector loop: settle, then scan the pressure snapshot on a cadence.
    Works in every mode that mounts diagnostics (incl. monitor-only)."""
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
                logger.warning("pressure loop iteration failed: %s", exc)
            time.sleep(_SCAN_INTERVAL_SEC)

    threading.Thread(target=loop, daemon=True, name="diag-pressure").start()
