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
                 episode_id=None, attributes=None, ts_utc=None):
    """Persist one operational event and return its dict form (or None on
    failure). Callers (emitter, detectors) are expected to have already scrubbed
    ``summary`` / ``attributes``. ``event_type`` is the normalized reason_code."""
    severity = (severity or "info").lower()
    if severity not in _VALID_SEVERITY:
        severity = "info"
    event_id = new_event_id()
    ts_utc = ts_utc or now_iso()
    status = OperationalEvent.insert_event(
        event_id=event_id, ts_utc=ts_utc, severity=severity, category=category,
        event_type=event_type, summary=summary, source=source, service=service,
        app_id=app_id, job_id=job_id, impact=impact,
        resource_type=resource_type, protection_id=protection_id,
        episode_id=episode_id, attributes=attributes,
    )
    if status != DBStatus.SUCCESS:
        return None
    return {
        "event_id": event_id, "ts_utc": ts_utc, "severity": severity,
        "category": category, "event_type": event_type, "summary": summary,
        "source": source, "service": service, "app_id": app_id, "job_id": job_id,
        "impact": impact, "resource_type": resource_type,
        "protection_id": protection_id, "episode_id": episode_id,
        "attributes": attributes,
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


def query_alerts(*, unacknowledged_only=False, limit=200):
    return [alert_to_dict(r) for r in AlertState.query_alerts(
        unacknowledged_only=unacknowledged_only, limit=limit)]


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
        "category": row.category, "event_type": row.event_type, "summary": row.summary,
        "source": row.source, "service": row.service, "app_id": row.app_id,
        "job_id": row.job_id,
        "impact": row.impact, "resource_type": row.resource_type,
        "protection_id": row.protection_id, "episode_id": row.episode_id,
        "attributes": attrs, "acknowledged_at": row.acknowledged_at,
    }


def alert_to_dict(row):
    return {
        "dedup_key": row.dedup_key, "first_fired_at": row.first_fired_at,
        "last_fired_at": row.last_fired_at, "fire_count": row.fire_count,
        "last_event_id": row.last_event_id,
        "acknowledged_at": row.acknowledged_at, "severity": row.severity,
        "event_type": row.event_type, "summary": row.summary,
    }
