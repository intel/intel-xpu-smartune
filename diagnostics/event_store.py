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
import threading
import uuid
from datetime import datetime, timezone

from db.DatabaseModel import (
    AlertState,
    DBStatus,
    OperationalEvent,
    init_database,
)
from diagnostics import event_catalog
from diagnostics.system_info import normalize_boot_id, read_boot_id
from utils.logger import get_logger

logger = get_logger(__name__)

_VALID_SEVERITY = ("info", "warning", "error", "critical")


def _lifecycle_action(event_type, protection_id):
    """The event's effect on its protection's limit lifecycle (applied, recovered, ...), or None."""
    return event_catalog.lifecycle_action(event_type) if protection_id else None

# Per-thread count of failed event writes, duplicates excluded: a log scanner reads it around
# one record to tell a lost write from a dedup, as both return None.
_write_failures = threading.local()

# The same failures across all threads, for /diag/sources.
_storage_lock = threading.Lock()
_storage = {"failed": 0, "last_failed_at": None, "last_failed_type": None, "last_status": None}


def write_failures():
    """How many event writes have failed on the calling thread so far."""
    return getattr(_write_failures, "count", 0)


def storage_stats():
    """Event writes that failed to store since the process started, any thread."""
    with _storage_lock:
        return dict(_storage)


def _record_storage_failure(event_type, status):
    _write_failures.count = write_failures() + 1
    with _storage_lock:
        _storage["failed"] += 1
        _storage["last_failed_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        _storage["last_failed_type"] = event_type
        _storage["last_status"] = str(status)


def new_event_id():
    return uuid.uuid4().hex[:12]


def now_iso():
    """Current UTC time as ISO 8601 with milliseconds, strictly increasing (see writer.now_iso)."""
    from diagnostics import writer

    return writer.now_iso()


def utc_iso(value):
    """Normalize an ISO 8601 timestamp to UTC (``+00:00``), keeping the instant.

    Stored times are compared as strings, so all must share one offset. A naive value is read as
    local time; an unparsable one is returned as is.
    """
    if not value or not isinstance(value, str):
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")


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
    ts_utc = utc_iso(ts_utc) if ts_utc else now_iso()
    boot_id = normalize_boot_id(boot_id) if boot_id is not None else read_boot_id()
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
        _record_storage_failure(event_type, status)
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
        "lifecycle_action": _lifecycle_action(event_type, protection_id),
        "attributes": attributes, "boot_id": boot_id,
        "config_revision_id": config_revision_id,
    }


def query_events(**filters):
    """Thin pass-through to the model query; returns a list of dicts."""
    rows = OperationalEvent.query_events(**filters)
    return [event_to_dict(r) for r in rows]


SEVERITY_ORDER = ("info", "warning", "error", "critical")
# Severities a bounded window view must never drop in favour of newer noise.
IMPORTANT_SEVERITIES = ("warning", "error", "critical")


def severities_at_least(level):
    """The severities at or above ``level``, or None for an unknown level."""
    level = (level or "").lower()
    if level not in SEVERITY_ORDER:
        return None
    return list(SEVERITY_ORDER[SEVERITY_ORDER.index(level):])


# Hard ceiling on the important set so a pathological storm stays bounded.
_IMPORTANT_CAP = 2000


def query_window_events(*, limit=200, keep_severities=IMPORTANT_SEVERITIES, **filters):
    """Window query keeping every ``keep_severities`` event (up to a cap); ``limit`` bounds the rest.

    Returns ``{"events": [...newest first], "truncated": bool}``. With a severity filter it is a plain
    query paged by ``offset``; the merged view takes no ``offset``.
    """
    offset = filters.pop("offset", None) or 0
    limit = max(1, int(limit or 200))
    if filters.get("severity") or filters.get("severities"):
        rows = OperationalEvent.query_events(limit=limit + 1, offset=offset, **filters)
        return {"events": [event_to_dict(r) for r in rows[:limit]],
                "truncated": len(rows) > limit}
    if offset:
        raise ValueError("offset needs severity or min_severity with keep_important")
    filters.pop("severities", None)
    important = OperationalEvent.query_events(
        severities=list(keep_severities), limit=_IMPORTANT_CAP + 1, **filters)
    recent = OperationalEvent.query_events(limit=limit + 1, **filters)
    truncated = len(recent) > limit or len(important) > _IMPORTANT_CAP
    merged = {row.event_id: row for row in important[:_IMPORTANT_CAP]}
    for row in recent[:limit]:
        merged.setdefault(row.event_id, row)
    rows = sorted(merged.values(), key=lambda r: (r.create_time or 0, r.ts_utc or ""),
                  reverse=True)
    return {"events": [event_to_dict(r) for r in rows], "truncated": truncated}


def unresolved_protection_ids():
    """protection_ids of still-active resource limits (age/volume independent)."""
    return OperationalEvent.unresolved_protection_ids()


def events_for_protections(protection_ids, app_id=None):
    """Full event history for the given protections, as dicts (newest-first)."""
    rows = OperationalEvent.events_for_protections(protection_ids, app_id=app_id)
    return [event_to_dict(r) for r in rows]


def query_alerts(*, active_only=False, notifyable_only=False, limit=200,
                 start_time=None, end_time=None):
    return [alert_to_dict(r) for r in AlertState.query_alerts(
        active_only=active_only, notifyable_only=notifyable_only, limit=limit,
        start_time=start_time, end_time=end_time)]


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
        "lifecycle_action": _lifecycle_action(row.event_type, row.protection_id),
        "domain": event_catalog.domain_of(row.event_type, attrs),
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
        # Set only for a closed occurrence (AlertEpisode); one dedup_key can
        # have many of those, so clients key history rows by this id.
        "episode_id": getattr(row, "episode_id", None),
        "dedup_key": row.dedup_key, "first_fired_at": row.first_fired_at,
        "last_fired_at": row.last_fired_at, "fire_count": row.fire_count,
        "last_event_id": row.last_event_id,
        "acknowledged_at": row.acknowledged_at, "status": row.status,
        "silenced_until": row.silenced_until, "silence_reason": row.silence_reason,
        "resolved_at": row.resolved_at, "resolved_event_id": row.resolved_event_id,
        # What closed it; None while open and on rows closed by older releases.
        "resolved_reason": getattr(row, "resolved_reason", None),
        "scope": row.scope, "severity": row.severity,
        "severity_origin": row.severity_origin, "baseline_severity": row.baseline_severity,
        "escalation_rule_id": row.escalation_rule_id,
        "escalation_rule_version": row.escalation_rule_version,
        "escalation_reason": row.escalation_reason,
        "escalation_evidence": evidence,
        "escalated_at": row.escalated_at,
        "event_type": row.event_type, "summary": row.summary,
        "close_mode": _close_mode(row.event_type),
    }


def _close_mode(event_type):
    try:
        from diagnostics.alerts import close_mode

        return close_mode(event_type)
    except Exception:
        return None
