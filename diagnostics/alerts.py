# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# An alert is NOT a second log: it is a `severity >= warning` operational event
# put through dedup / cooldown / acknowledge. Every alert points back to the
# event that produced it (last_event_id), so the UI can inspect the supporting
# evidence without duplicating event storage.
#
# The dedup key groups same-kind noise: event_type plus the most specific scope
# id in play (app_id / job_id). Within the cooldown window repeat fires only bump
# fire_count; the first fire (or one past the window) is what should notify.

from db.DatabaseModel import AlertState
from utils.logger import get_logger

logger = get_logger(__name__)

# Severities that graduate an event into an alert.
_ALERTABLE = ("warning", "error", "critical")

# Default cooldown window (seconds) before the same dedup_key notifies again.
_COOLDOWN_SECONDS = 300


def is_alertable(severity):
    return (severity or "").lower() in _ALERTABLE


def dedup_key_for(event):
    """Build the dedup key from an event dict: event_type + narrowest scope."""
    scope = event.get("job_id") or event.get("app_id") or event.get("service") or "-"
    return f"{event.get('event_type', 'event')}::{scope}"


def evaluate(event, cooldown_seconds=_COOLDOWN_SECONDS):
    """Fold an event into alert_state. Returns (should_notify, fire_count).

    Best-effort: alerting must never break event recording, so any error is
    swallowed and reported as "do not notify"."""
    if not is_alertable(event.get("severity")):
        return False, 0
    try:
        return AlertState.record_fire(
            dedup_key=dedup_key_for(event),
            ts_utc=event.get("ts_utc"),
            event_id=event.get("event_id"),
            severity=event.get("severity"),
            event_type=event.get("event_type"),
            summary=event.get("summary"),
            cooldown_seconds=cooldown_seconds,
        )
    except Exception as exc:
        logger.warning("Alert evaluate failed: %s", exc)
        return False, 0
