# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# An alert is NOT a second log: an explicit policy promotes an operational event
# to an active alert, which later resolves. Every alert points back to the event
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
from diagnostics import custom_rules
from utils.logger import get_logger

logger = get_logger(__name__)

# Default cooldown window (seconds) before the same dedup_key notifies again.
COOLDOWN_SECONDS = 300

# Only an explicitly declared operational condition can create an alert. Other
# warning/error events remain evidence in the event ledger without paging users.
# An alert opens on the first matching event; producers filter transient observations.
# Each policy closes one way: ``close_on_ack`` (acknowledged), ``ttl_seconds`` (a lease,
# closed once detections stop that long) or ``resolved_by`` (a recovery event of that
# type and scope). ``description`` and ``closes_when`` are shown by the dashboard.
ALERT_POLICIES = {
    "PLATFORM_KERNEL_PANIC": {
        "severity": "critical", "close_on_ack": True,
        "description": "Kernel panic. A one-off fault with no recovery event: investigate, "
                       "then acknowledge it to close it.",
    },
    "RESOURCE_MEMORY_OOM_KILL": {
        "severity": "critical", "close_on_ack": True,
        "description": "Out-of-memory kill by the kernel or systemd-oomd, one alert per killed "
                       "process or scope. A one-off fault with no recovery event: investigate, "
                       "then acknowledge it to close it.",
    },
    "PLATFORM_HOST_UNCLEAN_SHUTDOWN": {
        "severity": "error", "close_on_ack": True,
        "description": "The host stopped without a clean shutdown before this boot (crash, hang, "
                       "reset or power loss), found from a journal file left open. Investigate, "
                       "then acknowledge it to close it.",
    },
    "DEVICE_GPU_HANG": {
        "severity": "critical", "close_on_ack": True,
        "description": "GPU hang. A one-off fault with no recovery event: investigate, then "
                       "acknowledge it to close it.",
    },
    # Kernel fault signatures (diagnostics/detectors/kernel_signatures.py). WARN, corrected
    # hardware errors, user-space crashes and thermal throttling are too frequent to alert on.
    "PLATFORM_KERNEL_OOPS": {
        "severity": "critical", "close_on_ack": True,
        "description": "Kernel BUG / Oops / general protection fault. The host may be unstable "
                       "until it reboots. A one-off fault: investigate, then acknowledge it to "
                       "close it.",
    },
    "PLATFORM_KERNEL_LOCKUP": {
        "severity": "critical", "close_on_ack": True,
        "description": "Kernel soft/hard lockup, RCU stall or workqueue lockup: a CPU stopped "
                       "making progress. A one-off fault: investigate, then acknowledge it to "
                       "close it.",
    },
    "DEVICE_HARDWARE_ERROR_UNCORRECTED": {
        "severity": "critical", "close_on_ack": True,
        "description": "Uncorrected machine check, PCIe AER or memory (EDAC) error, one alert "
                       "per component. Investigate, then acknowledge it to close it.",
    },
    "PLATFORM_KERNEL_HUNG_TASK": {
        "severity": "error", "close_on_ack": True,
        "description": "A task was blocked in the kernel past the hung-task timeout, one alert "
                       "per task. Repeated kernel reports for the same task fold into it. "
                       "Acknowledge it to close it.",
    },
    "DEVICE_STORAGE_IO_ERROR": {
        "severity": "error", "close_on_ack": True,
        "description": "Block device, NVMe/ATA or filesystem I/O error, one alert per device. "
                       "Check the device health, then acknowledge it to close it.",
    },
    "DEVICE_GPU_THROTTLED": {
        "severity": "warning", "resolved_by": "DEVICE_GPU_THROTTLE_CLEARED",
        "closes_when": "Closes when GPU throttling clears",
        "description": "GPU throttled: the driver reports the GPU is held below its requested "
                       "frequency, with the cause (PL1, PL2, thermal, ...). Closes when "
                       "throttling clears, confirmed over 3 consecutive samples.",
    },
    # Closed on ack, not by the restart: systemd restarts the service within seconds,
    # so the alert would close before anyone saw it.
    "PLATFORM_SERVICE_CRASHED": {
        "severity": "critical", "close_on_ack": True,
        "description": "A SmarTune service crashed: an uncaught exception, or -- found at the "
                       "next start -- it ended without shutting down (killed, OOM-killed or "
                       "crashed in native code). One alert per service; investigate, then "
                       "acknowledge it to close it.",
    },
    "PLATFORM_SYSTEMD_RESTART_LOOP": {
        "severity": "error", "ttl_seconds": 300,
        "description": "systemd restart loop, one alert per unit. Recorded at most once a "
                       "minute while the loop lasts; closes after 5 minutes without a new "
                       "detection; escalates to critical when the restart counter reaches 10 or "
                       "the loop is still running after 2 minutes (3 detections in 5 minutes).",
    },
    # Error signatures (diagnostics/error_signatures.py). A never-seen error
    # (LOG_ERROR_SIGNATURE_NEW) is recorded as evidence only.
    "LOG_ERROR_SIGNATURE_BURST": {
        "severity": "error", "ttl_seconds": 600,
        "description": "An error message repeating far more often than its own history "
                       "predicts: at least 10 times in 5 minutes and 5x its usual rate. Recorded "
                       "at most every 5 minutes while it lasts; closes after 10 minutes without "
                       "a new detection.",
    },
    # Closed on ack: no RECOVERED event shares a failed apply's protection id, and a
    # failed restore may never be retried. A reboot also clears it.
    "CONTROL_CPU_LIMIT_FAILED": {
        "severity": "error", "close_on_ack": True,
        "description": "Applying or restoring a CPU limit failed. A one-off fault: check the "
                       "app's cgroup, then acknowledge it to close it. A reboot also closes it.",
    },
    "CONTROL_MEMORY_LIMIT_FAILED": {
        "severity": "error", "close_on_ack": True,
        "description": "Applying or restoring a memory limit failed. A one-off fault: check the "
                       "app's cgroup, then acknowledge it to close it. A reboot also closes it.",
    },
    "CONTROL_DISK_IO_LIMIT_FAILED": {
        "severity": "error", "close_on_ack": True,
        "description": "Applying or restoring a disk I/O limit failed. A one-off fault: check the "
                       "app's cgroup, then acknowledge it to close it. A reboot also closes it.",
    },
}


def _recovery_types(policy):
    resolved_by = policy.get("resolved_by") or ()
    return (resolved_by,) if isinstance(resolved_by, str) else tuple(resolved_by)


def _invert_resolved_by(policies):
    """recovery event type -> the alert types it closes."""
    resolves = {}
    for alert_type, policy in policies.items():
        for recovery_type in _recovery_types(policy):
            resolves.setdefault(recovery_type, []).append(alert_type)
    return resolves


_RESOLVES = _invert_resolved_by(ALERT_POLICIES)


def resolved_alert_types(event_type):
    """The alert types an event of ``event_type`` closes (within its scope)."""
    return tuple(_RESOLVES.get(event_type or "", ()))

# Escalation rules: keyed by event_type. A rule fires if fire_count >= threshold
# within window_seconds of first_fired_at, or when one event's attributes[attribute]
# reaches attribute_threshold, escalating severity to escalate_to.
#
# Restart loops are emitted once a minute per unit, so 3 fires in 5 minutes means still
# looping after ~2 minutes; systemd's restart counter catches a fast loop.
_ESCALATION_RULES = {
    "PLATFORM_SYSTEMD_RESTART_LOOP": {
        "rule_id": "restart_storm_v1",
        "rule_version": 2,
        "window_seconds": 300,
        "threshold": 3,
        "attribute": "restart_count",
        "attribute_threshold": 10,
        "escalate_to": "critical",
    },
}


def policy_for(event_type):
    """The alert policy of an event type, or None. A user rule's policy is read from the
    current configuration, so changes apply without a restart."""
    event_type = event_type or ""
    policy = ALERT_POLICIES.get(event_type)
    if policy is not None:
        return policy
    if event_type.startswith(custom_rules.EVENT_TYPE_PREFIX):
        return custom_rules.alert_policy(event_type[len(custom_rules.EVENT_TYPE_PREFIX):])
    return None


def alertable_event_types():
    """Every event type that can currently create an alert."""
    return list(ALERT_POLICIES) + custom_rules.alerting_event_types()


def _policy_close_mode(policy):
    if policy.get("close_on_ack"):
        return "ack"
    if policy.get("ttl_seconds"):
        return "quiet"
    if _recovery_types(policy):
        return "recovery"
    return None


def close_mode(event_type):
    """How an alert of this type closes: "ack", "quiet" (lease), "recovery" or None."""
    policy = policy_for(event_type)
    if policy is None:
        return "ack" if (event_type or "").startswith(custom_rules.EVENT_TYPE_PREFIX) else None
    return _policy_close_mode(policy)


def _duration_text(seconds):
    seconds = int(seconds)
    if seconds and seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    if seconds and seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def _close_texts(policy, *, quiet_means="recurrence"):
    """(closes_when, closed_because): one line each on how the alert ends."""
    from diagnostics import event_catalog

    mode = _policy_close_mode(policy)
    if mode == "ack":
        return "Closes when acknowledged", "Acknowledged"
    if mode == "quiet":
        quiet = _duration_text(policy["ttl_seconds"])
        return f"Closes after {quiet} without {quiet_means}", f"No {quiet_means} for {quiet}"
    if mode == "recovery":
        label = event_catalog.label(_recovery_types(policy)[0])
        return policy.get("closes_when") or f"Closes on: {label}", label
    return "Closes when closed by hand", "Closed"


def _escalation_summary(event_type):
    rule = _ESCALATION_RULES.get(event_type)
    if not rule:
        return None
    return {key: rule.get(key) for key in (
        "escalate_to", "threshold", "window_seconds", "attribute", "attribute_threshold")}


def _summary(event_type, policy, *, label, source, description, quiet_means="recurrence"):
    closes_when, closed_because = _close_texts(policy, quiet_means=quiet_means)
    return {
        "event_type": event_type,
        "label": label,
        "source": source,
        "severity": policy.get("severity"),
        "close_mode": _policy_close_mode(policy),
        "ttl_seconds": policy.get("ttl_seconds"),
        "resolved_by": list(_recovery_types(policy)),
        "escalation": _escalation_summary(event_type),
        "description": description,
        "closes_when": closes_when,
        "closed_because": closed_because,
    }


def policy_summaries():
    """Every policy that can create an alert now: built-in ones in ALERT_POLICIES order,
    then user rules whose alert is on."""
    from diagnostics import event_catalog

    summaries = [
        _summary(event_type, policy, label=event_catalog.label(event_type), source="builtin",
                 description=policy.get("description") or event_catalog.description(event_type))
        for event_type, policy in ALERT_POLICIES.items()
    ]
    for rule_id, name, source, policy in custom_rules.alerting_rules():
        ends = ("Acknowledge it to close it." if policy.get("close_on_ack") else
                f"Closes after {_duration_text(policy['ttl_seconds'])} without a new match.")
        summaries.append(_summary(
            custom_rules.event_type_for(rule_id), policy, label=name, source="custom_rule",
            description=f'Custom rule "{name}" matched a {source} log line. {ends}',
            quiet_means="a match"))
    return summaries


def scope_for(event):
    """Return the narrowest stable scope shared by fault and recovery events.
    ``attributes.subject`` separates host-level facts by entity (each OOM-killed process)."""
    attributes = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
    return (event.get("protection_id") or event.get("job_id") or event.get("app_id")
            or event.get("service") or attributes.get("subject")
            or event.get("source") or "-")


# Events of one type read to find an alert's own detections among them.
_OCCURRENCE_SCAN = 2000


def occurrences(*, event_type, scope, start_time=None, end_time=None, limit=200):
    """The detections behind one alert: events of its type and scope in its
    span, newest first. ``{"events": [...], "truncated": bool}``."""
    from diagnostics import event_store

    rows = event_store.query_events(event_type=event_type, start_time=start_time,
                                    end_time=end_time, limit=_OCCURRENCE_SCAN)
    matching = [event for event in rows if scope_for(event) == scope]
    limit = max(1, int(limit))
    return {"events": matching[:limit],
            "truncated": len(matching) > limit or len(rows) >= _OCCURRENCE_SCAN}


def dedup_key_for(event):
    """Build the dedup key from an event dict: event_type + narrowest scope."""
    scope = scope_for(event)
    return f"{event.get('event_type', 'event')}::{scope}"


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


def evaluate(event, cooldown_seconds=COOLDOWN_SECONDS):
    """Fold an event into alert_state. Returns (should_notify, fire_count).

    Best-effort: alerting must never break event recording, so any error is
    swallowed and reported as "do not notify". After dedup/cooldown, check
    escalation rules; if a rule matches, escalate the alert severity and mark
    severity_origin=rule_escalated for audit."""
    try:
        for resolved_type in resolved_alert_types(event.get("event_type")):
            AlertState.resolve(
                event_type=resolved_type,
                scope=scope_for(event),
                ts_utc=event.get("ts_utc"),
                resolved_event_id=event.get("event_id"),
            )
        policy = policy_for(event.get("event_type"))
        if policy is None:
            return False, 0
        dedup_key = dedup_key_for(event)
        if _epoch(event.get("ts_utc")) is None:
            logger.warning("Alert event %s has no usable timestamp", event.get("event_id"))
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
        escalated = False
        if event_type in _ESCALATION_RULES:
            escalated = _try_escalate(dedup_key, event, fire_count, _ESCALATION_RULES[event_type])
        # A raised severity is new information: notify even inside the cooldown.
        should_notify = should_notify or escalated
        if should_notify:
            _notify(event, dedup_key, fire_count)
        return should_notify, fire_count
    except Exception as exc:
        logger.warning("Alert evaluate failed: %s", exc)
        return False, 0


# Push channels the service registers. _notify() skips acknowledged or silenced alerts;
# an escalation or a new occurrence clears the acknowledgement.
_notification_sinks = []


def add_notification_sink(sink):
    """Call ``sink(payload)`` for every alert that should notify. Idempotent."""
    if sink not in _notification_sinks:
        _notification_sinks.append(sink)


def _notify(event, dedup_key, fire_count):
    if not _notification_sinks:
        return
    row = AlertState.get_or_none(AlertState.dedup_key == dedup_key)
    if row is None or row.status != "active" or row.acknowledged_at:
        return
    silenced_until = _epoch(row.silenced_until)
    if silenced_until is not None and silenced_until > time.time():
        return
    from diagnostics import event_catalog

    attributes = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
    payload = {
        # The keys of the stream's other notifications, plus the alert itself.
        "purpose": "notify",
        "status": "diag_alert",
        "app_id": event.get("app_id") or "",
        "app_name": attributes.get("app_name") or "",
        "message": row.summary or event.get("summary") or "",
        "alert": {
            "dedup_key": dedup_key,
            "event_type": row.event_type,
            "label": attributes.get("custom_rule_name") or event_catalog.label(row.event_type),
            "severity": row.severity,
            "summary": row.summary,
            "scope": row.scope,
            "fire_count": fire_count,
            "last_event_id": row.last_event_id,
            "last_fired_at": row.last_fired_at,
        },
    }
    for sink in list(_notification_sinks):
        try:
            sink(payload)
        except Exception as exc:  # one broken channel must not stop the others
            logger.warning("Alert notification sink failed: %s", exc)


def closes_on_ack(event_type):
    """Whether acknowledging an alert of this type also closes it (also true for a deleted
    or no longer alerting user rule, which has no other way to close)."""
    return close_mode(event_type) == "ack"


def acknowledge(dedup_key, seen_event_id=None):
    """Acknowledge an active alert; one-off faults are closed at the same time.
    Returns ``(found, closed)``. Raises AlertChanged, changing nothing, when
    ``seen_event_id`` is no longer the alert's ``last_event_id``."""
    alert = AlertState.get_or_none((AlertState.dedup_key == dedup_key) & (AlertState.status == "active"))
    if alert is None:
        return False, False
    if closes_on_ack(alert.event_type):
        closed = AlertState.close_acknowledged(dedup_key, _now_iso(), seen_event_id=seen_event_id)
        return closed, closed
    return AlertState.acknowledge(dedup_key, _now_iso(), seen_event_id=seen_event_id), False


def close(dedup_key, seen_event_id=None):
    """Close an active alert by hand, whatever its policy, archived as ``operator_closed``.
    Returns whether an active alert was found. ``seen_event_id``: see acknowledge()."""
    return AlertState.close_acknowledged(dedup_key, _now_iso(), reason="operator_closed",
                                         seen_event_id=seen_event_id)


def resolve_expired(now_epoch=None):
    """Resolve active alerts whose policy lease has not been renewed in time."""
    now_epoch = time.time() if now_epoch is None else now_epoch
    resolved = 0
    for alert in AlertState.active_alerts():
        policy = policy_for(alert.event_type)
        ttl_seconds = policy.get("ttl_seconds") if policy else None
        last_seen = _epoch(alert.last_seen_at)
        if not ttl_seconds or last_seen is None or now_epoch - last_seen <= ttl_seconds:
            continue
        resolved += AlertState.resolve(
            event_type=alert.event_type,
            scope=alert.scope,
            # When the lease ran out, not when this sweep noticed (hours later after downtime).
            ts_utc=_now_iso(last_seen + ttl_seconds),
            reason="expired",
            # Re-checked in the write, so a detection since the read above keeps the alert open.
            last_seen_before=datetime.fromtimestamp(now_epoch - ttl_seconds, tz=timezone.utc)
            .isoformat(timespec="milliseconds"),
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
    if escalation criteria are met (fire_count >= threshold, within window).
    Returns whether the severity was raised by this call."""
    try:
        # The fire_count returned from record_fire is the post-dedup value.
        # Fetch the alert row to get first_fired_at for windowing.
        from db.DatabaseModel import AlertState as AS
        row = AS.get_or_none(AS.dedup_key == dedup_key)
        if row is None:
            return False
        attributes = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
        attribute = rule.get("attribute")
        try:
            attribute_value = float(attributes.get(attribute)) if attribute else None
        except (TypeError, ValueError):
            attribute_value = None
        by_attribute = (attribute_value is not None
                        and attribute_value >= rule.get("attribute_threshold", float("inf")))
        first_fired_epoch = _epoch(row.first_fired_at)
        event_epoch = _epoch(event.get("ts_utc"))
        by_count = (fire_count >= rule["threshold"] and first_fired_epoch is not None
                    and event_epoch is not None
                    and (event_epoch - first_fired_epoch) <= rule["window_seconds"])
        if not (by_count or by_attribute):
            return False
        # Rule criteria met: escalate.
        evidence = {
            "trigger_event_id": event.get("event_id"),
            "fire_count": fire_count,
            "window_seconds": rule["window_seconds"],
            "first_fired_at": row.first_fired_at,
        }
        if by_attribute:
            evidence[attribute] = attribute_value
        return AS.escalate(
            dedup_key=dedup_key,
            to_severity=rule["escalate_to"],
            rule_id=rule["rule_id"],
            rule_version=rule["rule_version"],
            reason=(f"{attribute} reached {rule['attribute_threshold']}" if by_attribute and not by_count
                    else f"Threshold {rule['threshold']} fires within {rule['window_seconds']}s reached"),
            evidence=evidence,
            ts_utc=event.get("ts_utc"),
        )
    except Exception as exc:
        logger.warning("Alert escalation failed: %s", exc)
        return False
