# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# An alert is NOT a second log: an explicit policy promotes an operational event
# through pending / firing / resolved state. Every alert points back to the event
# that produced it (last_event_id), so the UI can inspect supporting evidence
# without duplicating event storage.
#
# The dedup key groups same-kind noise: event_type plus the most specific scope
# id in play (app_id / job_id). Within the cooldown window repeat fires only bump
# fire_count; the first fire (or one past the window) is what should notify.
#
# Severity escalation rules: derived from event frequency/pattern, not from the
# producer. Each rule specifies a judgment window and threshold; if met, the Alert
# severity escalates and severity_origin shifts to "rule_escalated" for audit.

import time
from datetime import datetime, timezone
from db.DatabaseModel import AlertState
from utils.logger import get_logger

logger = get_logger(__name__)

# Default cooldown window (seconds) before the same dedup_key notifies again.
_COOLDOWN_SECONDS = 300

# Only an explicitly declared operational condition can create an alert. Other
# warning/error events remain evidence in the event ledger without paging users.
# ``for_seconds`` filters transient observations; a TTL is only used for sampled
# conditions that can safely resolve when their heartbeat stops.
ALERT_POLICIES = {
    "PLATFORM_KERNEL_PANIC": {"severity": "critical", "for_seconds": 0},
    "RESOURCE_MEMORY_OOM_KILL": {"severity": "critical", "for_seconds": 0},
    "DEVICE_GPU_HANG": {"severity": "critical", "for_seconds": 0},
    "PLATFORM_SERVICE_CRASHED": {"severity": "critical", "for_seconds": 0},
    "PLATFORM_SYSTEMD_RESTART_LOOP": {
        "severity": "error", "for_seconds": 0, "ttl_seconds": 120,
    },
    "CONTROL_CPU_LIMIT_FAILED": {"severity": "error", "for_seconds": 0},
    "CONTROL_MEMORY_LIMIT_FAILED": {"severity": "error", "for_seconds": 0},
    "CONTROL_DISK_IO_LIMIT_FAILED": {"severity": "error", "for_seconds": 0},
}

# Escalation rules: keyed by event_type. A rule fires if fire_count >= threshold
# within window_seconds of first_fired_at, escalating severity to escalate_to.
_ESCALATION_RULES = {
    "PLATFORM_SYSTEMD_RESTART_LOOP": {
        "rule_id": "restart_storm_v1",
        "rule_version": 1,
        "window_seconds": 300,
        "threshold": 3,
        "escalate_to": "critical",
    },
}


def is_alertable(event):
    """Whether an event has an explicit alert policy."""
    return (event.get("event_type") or "") in ALERT_POLICIES


def scope_for(event):
    """Return the narrowest stable scope shared by fault and recovery events."""
    return (event.get("protection_id") or event.get("job_id") or event.get("app_id")
            or event.get("service") or event.get("source") or "-")


def dedup_key_for(event):
    """Build the dedup key from an event dict: event_type + narrowest scope."""
    scope = scope_for(event)
    return f"{event.get('event_type', 'event')}::{scope}"


def _recovery_target(event_type):
    if event_type == "PLATFORM_SERVICE_STARTED":
        return "PLATFORM_SERVICE_CRASHED"
    if event_type.endswith("_LIMIT_RECOVERED"):
        return event_type.replace("_LIMIT_RECOVERED", "_LIMIT_FAILED")
    return None


def _epoch(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def _now_iso(now_epoch=None):
    if now_epoch is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    return datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat(timespec="seconds")


def evaluate(event, cooldown_seconds=_COOLDOWN_SECONDS):
    """Fold an event into alert_state. Returns (should_notify, fire_count).

    Best-effort: alerting must never break event recording, so any error is
    swallowed and reported as "do not notify". After dedup/cooldown, check
    escalation rules; if a rule matches, escalate the alert severity and mark
    severity_origin=rule_escalated for audit."""
    try:
        recovery_target = _recovery_target(event.get("event_type") or "")
        if recovery_target:
            AlertState.resolve(
                event_type=recovery_target,
                scope=scope_for(event),
                ts_utc=event.get("ts_utc"),
                resolved_event_id=event.get("event_id"),
            )
        policy = ALERT_POLICIES.get(event.get("event_type") or "")
        if policy is None:
            return False, 0
        dedup_key = dedup_key_for(event)
        event_time = _epoch(event.get("ts_utc"))
        if event_time is None:
            logger.warning("Alert event %s has no usable timestamp", event.get("event_id"))
            return False, 0
        pending_since = AlertState.record_pending(
            dedup_key=dedup_key,
            ts_utc=event["ts_utc"],
            event_id=event.get("event_id"),
            severity=policy.get("severity", event.get("severity")),
            event_type=event.get("event_type"),
            summary=event.get("summary"),
            scope=scope_for(event),
        )
        if pending_since is not None:
            pending_time = _epoch(pending_since)
            if pending_time is None or event_time - pending_time < policy.get("for_seconds", 0):
                return False, 0
        should_notify, fire_count = AlertState.record_fire(
            dedup_key=dedup_key,
            ts_utc=event.get("ts_utc"),
            event_id=event.get("event_id"),
            severity=policy.get("severity", event.get("severity")),
            event_type=event.get("event_type"),
            summary=event.get("summary"),
            scope=scope_for(event),
            cooldown_seconds=cooldown_seconds,
        )
        # Check escalation rules after the alert has fired.
        event_type = event.get("event_type")
        if event_type in _ESCALATION_RULES:
            _try_escalate(dedup_key, event, fire_count, _ESCALATION_RULES[event_type])
        return should_notify, fire_count
    except Exception as exc:
        logger.warning("Alert evaluate failed: %s", exc)
        return False, 0


def resolve_expired(now_epoch=None):
    """Resolve active alerts whose policy lease has not been renewed in time."""
    now_epoch = time.time() if now_epoch is None else now_epoch
    resolved = 0
    for alert in AlertState.active_alerts():
        policy = ALERT_POLICIES.get(alert.event_type or "")
        ttl_seconds = policy.get("ttl_seconds") if policy else None
        last_seen = _epoch(alert.last_seen_at)
        if not ttl_seconds or last_seen is None or now_epoch - last_seen <= ttl_seconds:
            continue
        resolved += AlertState.resolve(
            event_type=alert.event_type,
            scope=alert.scope,
            ts_utc=_now_iso(now_epoch),
        )
    return resolved


def start_expiry_loop(interval_seconds=30):
    """Start the alert lease sweeper once per process."""
    import threading

    if getattr(start_expiry_loop, "started", False):
        return
    start_expiry_loop.started = True

    def loop():
        while True:
            try:
                resolve_expired()
            except Exception as exc:
                logger.warning("Alert expiry sweep failed: %s", exc)
            time.sleep(interval_seconds)

    threading.Thread(target=loop, daemon=True, name="diag-alert-expiry").start()


def _try_escalate(dedup_key, event, fire_count, rule):
    """Attempt to escalate an alert per the given rule. Idempotent: failures
    are logged but never propagate. Reads the alert's current state to check
    if escalation criteria are met (fire_count >= threshold, within window)."""
    try:
        # The fire_count returned from record_fire is the post-dedup value.
        # Fetch the alert row to get first_fired_at for windowing.
        from db.DatabaseModel import AlertState as AS
        row = AS.get_or_none(AS.dedup_key == dedup_key)
        if row is None:
            return
        # Evaluate the rule: threshold count within window.
        if fire_count < rule["threshold"]:
            return
        first_fired_epoch = _epoch(row.first_fired_at)
        event_epoch = _epoch(event.get("ts_utc"))
        if first_fired_epoch is None or event_epoch is None:
            logger.warning("Could not parse first_fired_at %s for escalation", row.first_fired_at)
            return
        if (event_epoch - first_fired_epoch) > rule["window_seconds"]:
            return
        # Rule criteria met: escalate.
        evidence = {
            "trigger_event_id": event.get("event_id"),
            "fire_count": fire_count,
            "window_seconds": rule["window_seconds"],
            "first_fired_at": row.first_fired_at,
        }
        AS.escalate(
            dedup_key=dedup_key,
            to_severity=rule["escalate_to"],
            rule_id=rule["rule_id"],
            rule_version=rule["rule_version"],
            reason=f"Threshold {rule['threshold']} fires within {rule['window_seconds']}s reached",
            evidence=evidence,
            ts_utc=event.get("ts_utc"),
        )
    except Exception as exc:
        logger.warning("Alert escalation failed: %s", exc)
