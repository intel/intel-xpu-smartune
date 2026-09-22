# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# EventStore -- the single writer + reader for the diagnostics event ledger.
# Both event sources funnel through here:
#   * business components calling emit_event() (pressure / control / benchmark /
#     service lifecycle), and
#   * detectors/rules scraping the smartune / benchmark log sources,
# so there is exactly one place that shapes an event before it hits SQLite.
#
# Storage lives in the existing my_database.db via the peewee models in
# db/DatabaseModel.py (OperationalEvent / AlertState); this module is
# a thin façade that owns id/timestamp generation and serialization, leaving the
# db_lock + db.atomic() discipline to the model methods.

import json
import uuid
from datetime import datetime

from db.DatabaseModel import (
    AlertState,
    DBStatus,
    OperationalEvent,
    init_database,
)
from diagnostics.system_info import read_boot_id
from utils.logger import get_logger

logger = get_logger(__name__)

_VALID_SEVERITY = ("info", "warning", "error", "critical")


def new_event_id():
    return uuid.uuid4().hex[:12]


def now_iso():
    """Timezone-aware ISO 8601 with millisecond precision (matches logger)."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def ensure_tables():
    """Idempotently create the diagnostics tables. Safe to call even when a
    service already ran init_database() (create_tables is a no-op then)."""
    try:
        init_database()
    except Exception as exc:  # never let table setup abort a mount
        logger.warning("Diagnostics ensure_tables failed: %s", exc)


def record_event(*, event_type, severity, category, summary, source=None,
                 service=None, app_id=None, job_id=None,
                 impact=None, resource_type=None, protection_id=None,
                 episode_id=None, attributes=None, ts_utc=None,
                 identity=None, boot_id=None, config_revision_id=None):
    """Persist one operational event and return its dict form (or None on
    failure, or on a benign duplicate suppressed by the dedup_key unique
    index). Callers (emitter, detectors) are expected to have already
    scrubbed ``summary`` / ``attributes``. ``event_type`` is the normalized
    reason_code.

    ``identity`` is a producer-supplied stable anchor for this exact fact
    (e.g. a journal cursor, a job/status pair) used to build ``dedup_key``
    together with ``boot_id`` -- this is the write-level idempotency guard,
    independent of AlertState's notification-throttling dedup_key. Producers
    that only ever call this once per real occurrence (lifecycle events,
    one-off actions) have nothing to gain from a key and can omit ``identity``;
    only a producer that might replay the same fact (log/journal scanners,
    reconciliation loops) needs to pass one."""
    severity = (severity or "info").lower()
    if severity not in _VALID_SEVERITY:
        severity = "info"
    event_id = new_event_id()
    ts_utc = ts_utc or now_iso()
    boot_id = boot_id if boot_id is not None else read_boot_id()
    dedup_key = f"{source or '-'}:{boot_id}:{identity}:{event_type}" if identity is not None else None
    status = OperationalEvent.insert_event(
        event_id=event_id, ts_utc=ts_utc, severity=severity, category=category,
        event_type=event_type, summary=summary, source=source, service=service,
        app_id=app_id, job_id=job_id, impact=impact,
        resource_type=resource_type, protection_id=protection_id,
        episode_id=episode_id, attributes=attributes,
        boot_id=boot_id, dedup_key=dedup_key, config_revision_id=config_revision_id,
    )
    if status == DBStatus.ALREADY_EXISTING:
        return None
    if status != DBStatus.SUCCESS:
        logger.error(
            "Operational event storage failed: type=%s source=%s status=%s",
            event_type, source, status,
        )
        return None
    return {
        "event_id": event_id, "ts_utc": ts_utc, "severity": severity,
        "category": category, "event_type": event_type, "summary": summary,
        "source": source, "service": service, "app_id": app_id, "job_id": job_id,
        "impact": impact, "resource_type": resource_type,
        "protection_id": protection_id, "episode_id": episode_id,
        "attributes": attributes, "boot_id": boot_id,
        "config_revision_id": config_revision_id,
    }


def query_events(**filters):
    """Thin pass-through to the model query; returns a list of dicts."""
    rows = OperationalEvent.query_events(**filters)
    return [event_to_dict(r) for r in rows]


def unresolved_protection_ids():
    """protection_ids of still-active resource limits (age/volume independent)."""
    return OperationalEvent.unresolved_protection_ids()


def events_for_protections(protection_ids, app_id=None):
    """Full event history for the given protections, as dicts (newest-first)."""
    rows = OperationalEvent.events_for_protections(protection_ids, app_id=app_id)
    return [event_to_dict(r) for r in rows]


def query_alerts(*, active_only=False, notifyable_only=False, limit=200):
    return [alert_to_dict(r) for r in AlertState.query_alerts(
        active_only=active_only, notifyable_only=notifyable_only, limit=limit)]


# --- serialization helpers -------------------------------------------------

def event_to_dict(row):
    attrs = None
    if row.attributes_json:
        try:
            attrs = json.loads(row.attributes_json)
        except (ValueError, TypeError):
            attrs = None
    return {
        "event_id": row.event_id, "ts_utc": row.ts_utc, "severity": row.severity,
        "severity_origin": row.severity_origin,
        "category": row.category, "event_type": row.event_type, "summary": row.summary,
        "source": row.source, "service": row.service, "app_id": row.app_id,
        "job_id": row.job_id,
        "impact": row.impact, "resource_type": row.resource_type,
        "protection_id": row.protection_id, "episode_id": row.episode_id,
        "attributes": attrs, "acknowledged_at": row.acknowledged_at,
        "boot_id": row.boot_id, "config_revision_id": row.config_revision_id,
    }


def alert_to_dict(row):
    evidence = None
    if row.escalation_evidence_json:
        try:
            evidence = json.loads(row.escalation_evidence_json)
        except (ValueError, TypeError):
            evidence = None
    return {
        "dedup_key": row.dedup_key, "first_fired_at": row.first_fired_at,
        "last_fired_at": row.last_fired_at, "fire_count": row.fire_count,
        "last_event_id": row.last_event_id,
        "acknowledged_at": row.acknowledged_at, "status": row.status,
        "silenced_until": row.silenced_until, "silence_reason": row.silence_reason,
        "resolved_at": row.resolved_at, "resolved_event_id": row.resolved_event_id,
        "scope": row.scope, "severity": row.severity,
        "severity_origin": row.severity_origin, "baseline_severity": row.baseline_severity,
        "escalation_rule_id": row.escalation_rule_id,
        "escalation_rule_version": row.escalation_rule_version,
        "escalation_reason": row.escalation_reason,
        "escalation_evidence": evidence,
        "escalated_at": row.escalated_at,
        "event_type": row.event_type, "summary": row.summary,
    }
