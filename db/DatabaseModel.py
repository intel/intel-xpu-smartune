# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
import json
import uuid
from peewee import (
    AutoField,
    BigIntegerField,
    BooleanField,
    CharField,
    DateTimeField,
    FloatField,
    IntegerField,
    IntegrityError,
    Model,
    OperationalError,
    SQL,
    SqliteDatabase,
    TextField,
)
from threading import Lock
import time
from datetime import datetime, timezone
from enum import Enum

from utils.logger import get_logger
logger = get_logger(__name__)

class DBStatus(Enum):
    SUCCESS = "SUCCESS"
    ALREADY_EXISTING = "ALREADY_EXISTING"
    FAILED = "FAILED"
    NO_PERMISSION = "NO_PERMISSION"
    NOT_FOUND = "NOT_FOUND"

# Create a global synchronization lock
db_lock = Lock()

# Database connection object
_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'my_database.db')
db = SqliteDatabase(_DB_PATH)
db.execute_sql('PRAGMA journal_mode=WAL;')

# Per-model write counter, bumped whenever a row actually changes.  Read-side
# caches compare it to tell whether their in-memory copy still matches the
# table, so a hot path can be served without touching the database at all --
# every access below takes the process-wide db_lock, which the monitor's
# snapshot writer also holds, and a reader that blocks on it while the disk is
# saturated is exactly the reader that needed to run.
_write_epochs: dict[str, int] = {}


def get_write_epoch(model_cls) -> int:
    """Return the current write counter for ``model_cls``."""
    return _write_epochs.get(model_cls.__name__, 0)


def _bump_write_epoch(model_cls) -> None:
    """Record a write against ``model_cls``.  Called with ``db_lock`` held."""
    name = model_cls.__name__
    _write_epochs[name] = _write_epochs.get(name, 0) + 1


class DataBaseModel(Model):
    create_time = BigIntegerField()
    create_date = DateTimeField()
    update_time = BigIntegerField()
    update_date = DateTimeField()

    class Meta:
        database = db

    @classmethod
    def query(cls, *query, **kwargs):
        """Query the database with a thread-safe approach and return all matching records."""
        with db_lock:
            try:
                with db.atomic():
                    return cls.select(*query, **kwargs)
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error querying {cls.__name__}: {e}")
                return []

    @classmethod
    def insert_record(cls, **data):
        """Insert data into the table in a thread-safe manner."""
        with db_lock:
            timestamp = int(time.time())
            now = datetime.now()
            data.update({
                'create_time': timestamp,
                'create_date': now,
                'update_time': timestamp,
                'update_date': now
            })
            # Try to create a new record or fetch the existing one
            try:
                with db.atomic():
                    instance, created = cls.get_or_create(id=data['id'], defaults=data)
                    if created:
                        _bump_write_epoch(cls)
                        return DBStatus.SUCCESS  # True stands for success/already existing
                    else:
                        logger.debug(f"Record with ID {data['id']} already exists.")
                        return DBStatus.ALREADY_EXISTING
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error inserting {cls.__name__} id={data.get('id')}: {e}")
                return DBStatus.FAILED

    @classmethod
    def update_all_records(cls, **data):
        """Update all records in a thread-safe manner."""
        with db_lock:
            timestamp = int(time.time())
            now = datetime.now()
            data.update({
                'update_time': timestamp,
                'update_date': now
            })

            try:
                with db.atomic():
                    updated_count = cls.update(**cls.normalize_data(data)).execute()
                    if updated_count:
                        _bump_write_epoch(cls)
                    return updated_count
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error batch updating {cls.__name__}: {e}")
                return 0

    @classmethod
    def update_record(cls, id, **data):
        """Update a record by ID in a thread-safe manner."""
        with db_lock:
            timestamp = int(time.time())
            now = datetime.now()
            data.update({
                'update_time': timestamp,
                'update_date': now
            })

            try:
                with db.atomic():
                    updated_count = cls.update(**cls.normalize_data(data)).where(cls.id == id).execute()
                    if updated_count == 0:
                        exists = cls.select().where(cls.id == id).exists()
                        return DBStatus.NOT_FOUND if not exists else DBStatus.SUCCESS
                    _bump_write_epoch(cls)
                    return DBStatus.SUCCESS
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error updating {cls.__name__} id={id}: {e}")
                return None

    @classmethod
    def delete_record(cls, id):
        """Delete a record by ID in a thread-safe manner."""
        with db_lock:
            try:
                with db.atomic():
                    deleted_count = cls.delete().where(cls.id == id).execute()
                    if deleted_count:
                        _bump_write_epoch(cls)
                    return deleted_count
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error deleting {cls.__name__} id={id}: {e}")
                return None

    @classmethod
    def normalize_data(cls, data):
        """Normalize data before inserting or updating."""
        return data

    @classmethod
    def to_dict(cls, instance):
        """Convert a model instance to a dictionary."""
        return {field: getattr(instance, field) for field in cls._meta.sorted_field_names}

    @classmethod
    def to_json(cls, instance):
        """Convert a model instance to a JSON string."""
        import json
        return json.dumps(cls.to_dict(instance))


class AIAppPriority(DataBaseModel):
    id = CharField(max_length=32, primary_key=True)
    app_id = CharField(max_length=32, null=False, index=True)
    name = CharField(max_length=128, null=False, help_text="app name", index=True)
    priority = IntegerField(default=0, help_text="app priority", index=True)
    network_priority = CharField(max_length=32, null=True, help_text="network QoS priority", index=True)
    oom_score = IntegerField(default=0, help_text="set app oom_score_adj", index=True)
    controlled = BooleanField(default=False, help_text="whether this app is controlled", index=True)
    cgroup = CharField(max_length=255, null=True, help_text=" where does it manage in cgroup", index=True)
    cmdline = TextField(null=True, help_text="app launch cmdline", index=True)
    remark = CharField(max_length=255, null=True, help_text="remark for this app", index=True)
    up_time = DateTimeField(null=True, index=True)
    status = CharField(default="NA", max_length=32, null=True, help_text="app status, NA, running, pending, stopped", index=True)
    limit_overrides_json = TextField(null=True, help_text="per-app manual resource limit overrides (JSON)")
    # Snapshot of this app's config.yaml ``controlled_apps`` entry (bpf_name /
    # process_names / commandline). Those fields live only in the YAML, so this
    # copy is what lets the startup reconciliation print the exact block to
    # paste back after a hand-deleted entry.
    config_meta_json = TextField(null=True, help_text="snapshot of the config.yaml controlled_apps entry (JSON)")


class MonitorSnapshot(DataBaseModel):
    id = AutoField()
    snapshot_type = CharField(max_length=16, null=False, help_text="snapshot category, e.g. static/dynamic", index=True)
    source = CharField(max_length=64, null=False, default="monitor.system_info", help_text="snapshot source", index=True)
    collected_at = CharField(max_length=32, null=True, help_text="origin collect timestamp", index=True)
    data_json = TextField(null=False, help_text="serialized snapshot payload")

    @classmethod
    def insert_snapshot(cls, snapshot_type: str, data: dict, source: str = "monitor.system_info", collected_at: str = None):
        with db_lock:
            timestamp = int(time.time())
            now = datetime.now()
            payload = json.dumps(data, ensure_ascii=False, default=str)
            try:
                with db.atomic():
                    cls.create(
                        snapshot_type=snapshot_type,
                        source=source,
                        collected_at=collected_at,
                        data_json=payload,
                        create_time=timestamp,
                        create_date=now,
                        update_time=timestamp,
                        update_date=now,
                    )
                    return DBStatus.SUCCESS
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error inserting monitor snapshot: {e}")
                return DBStatus.FAILED

    @classmethod
    def query_recent(
        cls,
        snapshot_type: str = None,
        limit: int = 100,
        start_time: int = None,
        end_time: int = None,
    ):
        with db_lock:
            try:
                with db.atomic():
                    query = cls.select()
                    if snapshot_type:
                        query = query.where(cls.snapshot_type == snapshot_type)
                    if isinstance(start_time, int):
                        query = query.where(cls.create_time >= start_time)
                    if isinstance(end_time, int):
                        query = query.where(cls.create_time <= end_time)
                    query = query.order_by(cls.id.desc()).limit(max(1, limit))
                    return list(query)
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error querying monitor snapshots: {e}")
                return []

    @classmethod
    def delete_older_than(cls, days: int) -> int:
        """Delete snapshots whose ``create_time`` is older than ``days`` days.

        Returns the number of rows deleted, or 0 on error.
        """
        cutoff = int(time.time()) - max(1, int(days)) * 86400
        with db_lock:
            try:
                with db.atomic():
                    return cls.delete().where(cls.create_time < cutoff).execute()
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error deleting old monitor snapshots: {e}")
                return 0


class OperationalEvent(DataBaseModel):
    """Structured operational-event ledger (diagnostics plan §3).

    The single high-value event account: pressure transitions, control actions,
    benchmark lifecycle, detected exceptions. Raw application/system logs are NOT
    stored here -- they stay in their own sources and are joined by
    a time window. ``create_time`` (inherited, epoch seconds at
    insert) backs the cheap time-window range query; ``ts_utc`` keeps the precise
    timezone-aware origin timestamp for display.
    """

    event_id = CharField(max_length=32, primary_key=True)
    ts_utc = CharField(max_length=40, null=False, help_text="timezone-aware ISO 8601", index=True)
    severity = CharField(max_length=16, null=False, help_text="info/warning/error/critical", index=True)
    severity_origin = CharField(max_length=16, default="producer", null=True, help_text="producer | rule_escalated", index=True)
    category = CharField(max_length=32, null=False, help_text="service/pressure/control/benchmark/kernel", index=True)
    # event_type IS the normalized reason_code: <DOMAIN>_<SUBJECT>_<STATE>, per the
    # §A.1.4 registry (e.g. CONTROL_CPU_LIMIT_APPLIED / RESOURCE_MEMORY_OOM_KILL).
    event_type = CharField(max_length=64, null=False, help_text="normalized reason_code <DOMAIN>_<SUBJECT>_<STATE>", index=True)
    summary = TextField(null=False)
    source = CharField(max_length=32, null=True, help_text="producer: balancer/monitor/benchmark/detector", index=True)
    service = CharField(max_length=32, null=True, index=True)
    app_id = CharField(max_length=64, null=True, index=True)
    job_id = CharField(max_length=64, null=True, index=True)
    # Read-model columns (diagnostics plan §3 / §8.1.1). impact = verified outcome;
    # resource_type/protection_id drive the "Active resource protections" read model;
    # episode_id pairs a Health Episode's OPEN with its RESOLVED (§8.1.2).
    impact = CharField(max_length=16, null=True, help_text="none/degraded/failed/unavailable", index=True)
    resource_type = CharField(max_length=24, null=True, help_text="cpu_mem/disk_io/network/memory/system", index=True)
    protection_id = CharField(max_length=96, null=True, index=True)
    episode_id = CharField(max_length=32, null=True, index=True)
    attributes_json = TextField(null=True, help_text="structured extra fields (JSON)")
    acknowledged_at = CharField(max_length=40, null=True)
    # boot_id scopes dedup_key to the current boot so a legitimate recurrence
    # after a reboot is never treated as the same event. dedup_key is the
    # write-level idempotency key (distinct from AlertState.dedup_key, which
    # only throttles notifications): a unique index (see _create_extra_indexes)
    # rejects a duplicate insert instead of the producer's in-memory seen-set
    # having to catch every case, which it cannot across a service restart.
    boot_id = CharField(max_length=64, null=True, index=True)
    dedup_key = CharField(max_length=160, null=True)
    config_revision_id = CharField(max_length=32, null=True, index=True)

    @classmethod
    def insert_event(cls, *, event_id, ts_utc, severity, category, event_type,
                     summary, source=None, service=None, app_id=None, job_id=None,
                     impact=None, resource_type=None,
                     protection_id=None, episode_id=None, attributes=None,
                     boot_id=None, dedup_key=None, config_revision_id=None):
        with db_lock:
            timestamp = int(time.time())
            now = datetime.now()
            attrs = json.dumps(attributes, ensure_ascii=False, default=str) if attributes else None
            try:
                with db.atomic():
                    cls.create(
                        event_id=event_id, ts_utc=ts_utc, severity=severity,
                        severity_origin="producer",
                        category=category, event_type=event_type, summary=summary,
                        source=source, service=service, app_id=app_id, job_id=job_id,
                        impact=impact,
                        resource_type=resource_type, protection_id=protection_id,
                        episode_id=episode_id, attributes_json=attrs,
                        boot_id=boot_id, dedup_key=dedup_key,
                        config_revision_id=config_revision_id,
                        create_time=timestamp, create_date=now,
                        update_time=timestamp, update_date=now,
                    )
                    _bump_write_epoch(cls)
                    return DBStatus.SUCCESS
            except IntegrityError as e:
                if dedup_key and "dedup_key" in str(e).lower():
                    return DBStatus.ALREADY_EXISTING
                logger.error(f"Error inserting operational event {event_type!r}: {e}")
                return DBStatus.FAILED
            except OperationalError as e:
                logger.error(f"Error inserting operational event {event_type!r}: {e}")
                return DBStatus.FAILED

    @classmethod
    def query_events(cls, *, event_id=None, severity=None, category=None, source=None,
                     event_type=None, event_types=None, app_id=None, job_id=None,
                     impact=None, resource_type=None, protection_id=None, episode_id=None,
                     boot_id=None, config_revision_id=None,
                     keyword=None, start_time=None, end_time=None,
                     limit=200, offset=0):
        """Filtered, newest-first event query. ``start_time``/``end_time`` are
        epoch seconds matched against the inherited ``create_time``;
        ``event_types`` keeps events whose type is any of the given ones."""
        with db_lock:
            try:
                with db.atomic():
                    query = cls.select()
                    for field, value in (
                        (cls.event_id, event_id),
                        (cls.severity, severity), (cls.category, category),
                        (cls.source, source), (cls.event_type, event_type),
                        (cls.app_id, app_id), (cls.job_id, job_id),
                        (cls.impact, impact),
                        (cls.resource_type, resource_type),
                        (cls.protection_id, protection_id), (cls.episode_id, episode_id),
                        (cls.boot_id, boot_id),
                        (cls.config_revision_id, config_revision_id),
                    ):
                        if value:
                            query = query.where(field == value)
                    if event_types:
                        query = query.where(cls.event_type.in_(list(event_types)))
                    if keyword:
                        query = query.where(
                            cls.summary.contains(keyword) | cls.event_type.contains(keyword))
                    if isinstance(start_time, int):
                        query = query.where(cls.create_time >= start_time)
                    if isinstance(end_time, int):
                        query = query.where(cls.create_time <= end_time)
                    query = query.order_by(cls.create_time.desc())
                    query = query.limit(max(1, limit)).offset(max(0, offset))
                    return list(query)
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error querying operational events: {e}")
                return []

    @classmethod
    def delete_older_than(cls, days: int) -> int:
        """Delete operational events outside the retention window, but keep the
        full history of any control protection that is still unresolved.

        A one-shot event (``protection_id`` NULL: OOM kill, log error, panic)
        expires normally. A control lifecycle must not: control_lifecycle.summarize()
        and reboot reconciliation rebuild a *live* protection from its original
        ``*_APPLIED`` event, so pruning that event -- even when it predates the
        window -- would erase a still-active resource limit from the dashboard and
        make the reboot check unable to classify it. Protections retained here are
        deleted whole on a later pass once they resolve and age out together."""
        cutoff = int(time.time()) - max(1, int(days)) * 86400
        with db_lock:
            try:
                with db.atomic():
                    retain = cls._protection_ids_to_retain(cutoff)
                    query = cls.delete().where(cls.create_time < cutoff)
                    if retain:
                        query = query.where(
                            cls.protection_id.is_null(True)
                            | cls.protection_id.not_in(list(retain)))
                    return query.execute()
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error deleting old operational events: {e}")
                return 0

    @classmethod
    def _scan_control_lifecycles(cls) -> dict:
        """One pass over control events -> per-protection tallies
        ``{applied, closed, rebooted, latest}``, shared by retention and the
        active-protection read model. Lock-free: callers that need isolation hold
        ``db_lock`` (``delete_older_than`` does)."""
        stats = {}
        rows = cls.select(cls.protection_id, cls.event_type, cls.create_time).where(
            cls.protection_id.is_null(False) & (cls.category == "platform.control"))
        for row in rows:
            state = stats.get(row.protection_id)
            if state is None:
                state = stats[row.protection_id] = {
                    "applied": 0, "closed": 0, "rebooted": False, "latest": 0}
            event_type = row.event_type or ""
            if event_type.endswith("_APPLIED"):
                state["applied"] += 1
            elif event_type.endswith(("_RECOVERED", "_FAILED")):
                state["closed"] += 1
            elif event_type == "CONTROL_LIFECYCLE_CLEARED_BY_REBOOT":
                state["rebooted"] = True
            state["latest"] = max(state["latest"], row.create_time or 0)
        return stats

    @staticmethod
    def _is_unresolved(state) -> bool:
        """A protection with an APPLIED that has no matching terminal
        ``*_RECOVERED``/``*_FAILED`` and was not cleared by a reboot -- the
        balancer emits one terminal per resource, so the counts pair up -- i.e. a
        resource limit that is still live."""
        return not state["rebooted"] and state["applied"] > state["closed"]

    @classmethod
    def _protection_ids_to_retain(cls, cutoff: int) -> set:
        """Control ``protection_id``s whose events must survive a pruning pass:
        unresolved (still live), or having at least one event newer than ``cutoff``
        (deleting its older events would leave a partial lifecycle -- a phantom
        RECOVERED without its APPLIED)."""
        stats = cls._scan_control_lifecycles()
        return {
            pid for pid, state in stats.items()
            if cls._is_unresolved(state) or state["latest"] >= cutoff
        }

    @classmethod
    def unresolved_protection_ids(cls) -> set:
        """protection_ids of still-active resource limits, regardless of age or
        event volume. Lets the lifecycle read model surface a live protection even
        when its APPLIED event predates a bounded newest-N event query."""
        with db_lock:
            try:
                with db.atomic():
                    return {pid for pid, state in cls._scan_control_lifecycles().items()
                            if cls._is_unresolved(state)}
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error scanning control lifecycles: {e}")
                return set()

    @classmethod
    def events_for_protections(cls, protection_ids, app_id=None):
        """All events for the given protection_ids, newest-first. Bounded by the
        number of protections asked for, not by the total ledger size."""
        ids = [pid for pid in (protection_ids or []) if pid]
        if not ids:
            return []
        with db_lock:
            try:
                with db.atomic():
                    query = cls.select().where(cls.protection_id.in_(ids))
                    if app_id:
                        query = query.where(cls.app_id == app_id)
                    return list(query.order_by(cls.create_time.desc()))
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error querying protection events: {e}")
                return []


class AlertState(DataBaseModel):
    """Derived alert state for policy-promoted events, never a second log.

    Pending candidates are kept internal until their sustained window completes;
    active and resolved rows retain the alert lifecycle shown to users. Every row
    points back to its most recent evidence via ``last_event_id``. Severity may
    be escalated by rules; escalation is retained for audit.
    """

    dedup_key = CharField(max_length=128, primary_key=True)
    first_fired_at = CharField(max_length=40, null=False)
    last_fired_at = CharField(max_length=40, null=False)
    fire_count = IntegerField(default=0)
    last_event_id = CharField(max_length=32, null=True)
    acknowledged_at = CharField(max_length=40, null=True)
    silenced_until = CharField(max_length=40, null=True)
    silence_reason = CharField(max_length=256, null=True)
    status = CharField(max_length=16, default="active", null=False, index=True)
    pending_since = CharField(max_length=40, null=True)
    last_seen_at = CharField(max_length=40, null=True)
    resolved_at = CharField(max_length=40, null=True)
    resolved_event_id = CharField(max_length=32, null=True)
    scope = CharField(max_length=96, null=True, index=True)
    severity = CharField(max_length=16, null=True, index=True)
    severity_origin = CharField(max_length=16, default="producer", null=True, help_text="producer | rule_escalated", index=True)
    baseline_severity = CharField(max_length=16, null=True, help_text="severity before escalation (snapshot at first escalation)")
    escalation_rule_id = CharField(max_length=64, null=True)
    escalation_rule_version = IntegerField(null=True)
    escalation_reason = TextField(null=True)
    escalation_evidence_json = TextField(null=True, help_text="escalation judgment data (JSON): trigger_event_id, fire_count, window_seconds, first_fired_at")
    escalated_at = CharField(max_length=40, null=True)
    event_type = CharField(max_length=64, null=True, index=True)
    summary = TextField(null=True)

    @classmethod
    def record_fire(cls, *, dedup_key, ts_utc, event_id, severity=None,
                    event_type=None, summary=None,
                    scope=None,
                    cooldown_seconds=300):
        """Register that ``dedup_key`` fired at ``ts_utc``. Returns
        ``(should_notify, fire_count)``. Within ``cooldown_seconds`` of the last
        fire the count is aggregated and ``should_notify`` is False (suppress the
        duplicate); a first fire or one past the cooldown returns True. The whole
        get-then-update runs under one lock/transaction to avoid races."""
        with db_lock:
            now = int(time.time())
            dt = datetime.now()
            try:
                with db.atomic():
                    row = cls.get_or_none(cls.dedup_key == dedup_key)
                    if row is None:
                        cls.create(
                            dedup_key=dedup_key, first_fired_at=ts_utc,
                            last_fired_at=ts_utc, fire_count=1, last_event_id=event_id,
                            last_seen_at=ts_utc,
                            severity=severity,
                            event_type=event_type, summary=summary, scope=scope,
                            create_time=now, create_date=dt,
                            update_time=now, update_date=dt,
                        )
                        _bump_write_epoch(cls)
                        return True, 1
                    should_notify = row.status != "active" or (now - int(row.update_time or 0)) >= max(0, cooldown_seconds)
                    became_active = row.status != "active"
                    # A new occurrence counts from 1, like first_fired_at below;
                    # the previous occurrence's count lives on in AlertEpisode.
                    count = 1 if became_active else int(row.fire_count or 0) + 1
                    fields = dict(
                        last_fired_at=ts_utc, fire_count=count, last_event_id=event_id,
                        severity=(row.severity if row.status == "active" and row.severity_origin == "rule_escalated" else severity),
                        event_type=event_type, summary=summary,
                        scope=scope, status="active", pending_since=None, last_seen_at=ts_utc,
                        resolved_at=None, resolved_event_id=None,
                    )
                    if should_notify:
                        fields["update_time"] = now
                        fields["update_date"] = dt
                    if became_active:
                        fields["first_fired_at"] = ts_utc
                        fields["acknowledged_at"] = None
                        fields["silenced_until"] = None
                        fields["silence_reason"] = None
                    (cls.update(**fields).where(cls.dedup_key == dedup_key).execute())
                    _bump_write_epoch(cls)
                    return should_notify, count
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error recording alert fire: {e}")
                return False, 0

    @classmethod
    def record_pending(cls, *, dedup_key, ts_utc, event_id, severity=None,
                       event_type=None, summary=None, scope=None):
        """Persist a candidate anomaly without exposing it as an active alert."""
        with db_lock:
            now = int(time.time())
            dt = datetime.now()
            try:
                with db.atomic():
                    row = cls.get_or_none(cls.dedup_key == dedup_key)
                    if row is None:
                        cls.create(
                            dedup_key=dedup_key, first_fired_at=ts_utc,
                            last_fired_at=ts_utc, last_event_id=event_id,
                            pending_since=ts_utc, last_seen_at=ts_utc,
                            severity=severity, event_type=event_type,
                            summary=summary, scope=scope, status="pending",
                            create_time=now, create_date=dt,
                            update_time=now, update_date=dt,
                        )
                        _bump_write_epoch(cls)
                        return ts_utc
                    if row.status == "active":
                        return None
                    pending_since = row.pending_since or ts_utc
                    (cls.update(
                        last_fired_at=ts_utc, last_seen_at=ts_utc, last_event_id=event_id,
                        severity=severity, summary=summary, scope=scope, event_type=event_type,
                        status="pending", pending_since=pending_since,
                        resolved_at=None, resolved_event_id=None,
                        update_time=now, update_date=dt,
                    ).where(cls.dedup_key == dedup_key).execute())
                    _bump_write_epoch(cls)
                    return pending_since
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error recording pending alert: {e}")
                return None

    @classmethod
    def active_alerts(cls):
        with db_lock:
            try:
                return list(cls.select().where(cls.status == "active"))
            except OperationalError as e:
                logger.error(f"Error reading active alerts: {e}")
                return []

    @classmethod
    def acknowledge(cls, dedup_key, ts_utc):
        with db_lock:
            try:
                with db.atomic():
                    changed = (cls.update(
                        acknowledged_at=ts_utc,
                        update_time=int(time.time()), update_date=datetime.now(),
                    ).where(
                        (cls.dedup_key == dedup_key) & (cls.status == "active")
                    ).execute())
                    if changed:
                        _bump_write_epoch(cls)
                    return bool(changed)
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error acknowledging alert: {e}")
                return False

    @classmethod
    def close_acknowledged(cls, dedup_key, ts_utc):
        """Acknowledge and close an active alert in one step, for alerts with no
        recovery signal (an operator having seen it is what ends it). The
        occurrence is archived like any other resolution, with no
        ``resolved_event_id``."""
        with db_lock:
            try:
                with db.atomic():
                    row = cls.get_or_none((cls.dedup_key == dedup_key) & (cls.status == "active"))
                    if row is None:
                        return False
                    row.acknowledged_at = ts_utc
                    AlertEpisode.archive(row, resolved_at=ts_utc)
                    (cls.update(
                        acknowledged_at=ts_utc,
                        status="resolved", resolved_at=ts_utc, resolved_event_id=None,
                        update_time=int(time.time()), update_date=datetime.now(),
                    ).where(cls.dedup_key == dedup_key).execute())
                    _bump_write_epoch(cls)
                    _bump_write_epoch(AlertEpisode)
                    return True
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error closing acknowledged alert: {e}")
                return False

    @classmethod
    def silence(cls, dedup_key, until, reason=None):
        with db_lock:
            try:
                with db.atomic():
                    changed = (cls.update(
                        silenced_until=until, silence_reason=reason,
                        update_time=int(time.time()), update_date=datetime.now(),
                    ).where(
                        (cls.dedup_key == dedup_key) & (cls.status == "active")
                    ).execute())
                    if changed:
                        _bump_write_epoch(cls)
                    return bool(changed)
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error silencing alert: {e}")
                return False

    @classmethod
    def escalate(cls, dedup_key, *, to_severity, rule_id, rule_version, reason, evidence, ts_utc):
        """Escalate an alert's severity (derived from a rule judgment, not an event producer).
        Idempotent: if already escalated and current severity >= to_severity, skip.
        If not yet escalated, snapshot baseline_severity on first escalation only.
        Returns True if escalation occurred, False if skipped/already at target."""
        with db_lock:
            timestamp = int(time.time())
            now = datetime.now()
            try:
                with db.atomic():
                    row = cls.get_or_none(cls.dedup_key == dedup_key)
                    if row is None:
                        return False
                    current_sev = row.severity or "info"
                    sev_order = {"info": 0, "warning": 1, "error": 2, "critical": 3}
                    current_sev_val = sev_order.get(current_sev, -1)
                    to_sev_val = sev_order.get(to_severity, -1)
                    # If already escalated and target is not higher, skip (idempotent).
                    if (row.severity_origin == "rule_escalated" and current_sev_val >= to_sev_val):
                        return False
                    # On first escalation, snapshot the baseline.
                    baseline = row.baseline_severity if row.baseline_severity else current_sev
                    updates = {
                        "severity": to_severity,
                        "severity_origin": "rule_escalated",
                        "baseline_severity": baseline,
                        "escalation_rule_id": rule_id,
                        "escalation_rule_version": rule_version,
                        "escalation_reason": reason,
                        "escalation_evidence_json": json.dumps(evidence, ensure_ascii=False, default=str) if evidence else None,
                        "escalated_at": ts_utc,
                        # A higher severity is new information: drop the prior
                        # acknowledgement so the alert needs attention again.
                        "acknowledged_at": None,
                        "update_time": timestamp,
                        "update_date": now,
                    }
                    (cls.update(**updates).where(cls.dedup_key == dedup_key).execute())
                    _bump_write_epoch(cls)
                    return True
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error escalating alert: {e}")
                return False

    @classmethod
    def resolve(cls, *, event_type, scope, ts_utc, resolved_event_id=None):
        """Resolve active alerts only when a matching recovery fact is recorded.
        Each resolved occurrence is archived as an AlertEpisode first, because
        this row is reused (and overwritten) if the alert fires again."""
        with db_lock:
            try:
                with db.atomic():
                    condition = ((cls.event_type == event_type)
                                 & (cls.scope == scope)
                                 & (cls.status == "active"))
                    for row in cls.select().where(condition):
                        AlertEpisode.archive(row, resolved_at=ts_utc, resolved_event_id=resolved_event_id)
                    changed = (cls.update(
                        status="resolved", resolved_at=ts_utc,
                        resolved_event_id=resolved_event_id,
                        update_time=int(time.time()), update_date=datetime.now(),
                    ).where(condition).execute())
                    if changed:
                        _bump_write_epoch(cls)
                        _bump_write_epoch(AlertEpisode)
                    return changed
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error resolving alert state: {e}")
                return 0

    @classmethod
    def query_alerts(cls, *, active_only=False, notifyable_only=False, limit=200,
                     start_time=None, end_time=None):
        """Active alerts come from this table; with ``active_only=False`` the
        resolved history comes from AlertEpisode (one row per occurrence), since
        a resolved row here only remembers the latest occurrence.

        ``start_time``/``end_time`` (epoch seconds) keep only occurrences whose
        span [first_fired_at, resolved_at or now] overlaps the window, so the row
        limit applies after the range filter instead of before it."""
        # Timestamps are stored as uniform UTC ISO strings, so string comparison
        # orders them correctly.
        start_iso = (datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat(timespec="seconds")
                     if start_time is not None else None)
        end_iso = (datetime.fromtimestamp(end_time, tz=timezone.utc).isoformat(timespec="seconds")
                   if end_time is not None else None)
        limit = max(1, limit)
        with db_lock:
            try:
                with db.atomic():
                    query = cls.select().where(cls.status == "active")
                    if end_iso is not None:
                        query = query.where(cls.first_fired_at <= end_iso)
                    if notifyable_only:
                        now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        query = query.where(
                            cls.acknowledged_at.is_null(True)
                            & (cls.silenced_until.is_null(True) | (cls.silenced_until <= now_utc))
                        )
                    rows = list(query.order_by(cls.last_fired_at.desc()).limit(limit))
                    # Closed occurrences are never notifyable.
                    if active_only or notifyable_only:
                        return rows
                    episodes = AlertEpisode.select()
                    if end_iso is not None:
                        episodes = episodes.where(AlertEpisode.first_fired_at <= end_iso)
                    if start_iso is not None:
                        episodes = episodes.where(AlertEpisode.resolved_at >= start_iso)
                    rows += list(episodes.order_by(AlertEpisode.last_fired_at.desc()).limit(limit))
                    rows.sort(key=lambda row: row.last_fired_at or "", reverse=True)
                    return rows[:limit]
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error querying alerts: {e}")
                return []

    @classmethod
    def delete_resolved_older_than(cls, days: int) -> int:
        """Delete resolved alerts outside the diagnostics retention window."""
        cutoff = int(time.time()) - max(1, int(days)) * 86400
        with db_lock:
            try:
                with db.atomic():
                    return (cls.delete()
                            .where((cls.status == "resolved") & (cls.update_time < cutoff))
                            .execute())
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error deleting old resolved alerts: {e}")
                return 0



# AlertState columns that describe one occurrence and are copied on archive.
_ALERT_EPISODE_FIELDS = (
    "dedup_key", "first_fired_at", "last_fired_at", "fire_count", "last_event_id",
    "acknowledged_at", "silenced_until", "silence_reason", "scope", "severity",
    "severity_origin", "baseline_severity", "escalation_rule_id",
    "escalation_rule_version", "escalation_reason", "escalation_evidence_json",
    "escalated_at", "event_type", "summary",
)


class AlertEpisode(DataBaseModel):
    """One closed occurrence (active -> resolved) of an alert.

    AlertState keeps a single row per dedup_key and reuses it when the alert
    fires again, which overwrites the previous occurrence. Archiving every
    resolved occurrence here keeps alert history queryable for past ranges.
    """

    episode_id = CharField(max_length=32, primary_key=True)
    dedup_key = CharField(max_length=128, index=True)
    first_fired_at = CharField(max_length=40, index=True)
    last_fired_at = CharField(max_length=40)
    resolved_at = CharField(max_length=40, index=True)
    resolved_event_id = CharField(max_length=32, null=True)
    fire_count = IntegerField(default=0)
    last_event_id = CharField(max_length=32, null=True)
    acknowledged_at = CharField(max_length=40, null=True)
    silenced_until = CharField(max_length=40, null=True)
    silence_reason = CharField(max_length=256, null=True)
    scope = CharField(max_length=96, null=True)
    severity = CharField(max_length=16, null=True)
    severity_origin = CharField(max_length=16, null=True)
    baseline_severity = CharField(max_length=16, null=True)
    escalation_rule_id = CharField(max_length=64, null=True)
    escalation_rule_version = IntegerField(null=True)
    escalation_reason = TextField(null=True)
    escalation_evidence_json = TextField(null=True)
    escalated_at = CharField(max_length=40, null=True)
    event_type = CharField(max_length=64, null=True)
    summary = TextField(null=True)

    status = "resolved"

    @staticmethod
    def episode_id_for(dedup_key, first_fired_at):
        """Deterministic id: an occurrence is identified by its key and start,
        so archiving the same occurrence twice is a no-op."""
        return uuid.uuid5(uuid.NAMESPACE_URL, f"{dedup_key}|{first_fired_at}").hex

    @classmethod
    def archive(cls, state, *, resolved_at, resolved_event_id=None):
        """Copy an AlertState row as a closed occurrence. Caller holds
        ``db_lock`` inside a transaction."""
        now = int(time.time())
        dt = datetime.now()
        fields = {name: getattr(state, name) for name in _ALERT_EPISODE_FIELDS}
        (cls.insert(
            episode_id=cls.episode_id_for(state.dedup_key, state.first_fired_at),
            resolved_at=resolved_at, resolved_event_id=resolved_event_id,
            create_time=now, create_date=dt, update_time=now, update_date=dt,
            **fields,
        ).on_conflict_ignore().execute())

    @classmethod
    def delete_older_than(cls, days: int) -> int:
        """Delete occurrences outside the diagnostics retention window."""
        cutoff = int(time.time()) - max(1, int(days)) * 86400
        with db_lock:
            try:
                with db.atomic():
                    return cls.delete().where(cls.update_time < cutoff).execute()
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error deleting old alert episodes: {e}")
                return 0

class DetectorCursor(DataBaseModel):
    """Persistent scan position for a diagnostics event source."""

    source = CharField(max_length=64, primary_key=True)
    cursor_epoch = FloatField(default=0.0)

    @classmethod
    def get_cursor(cls, source: str) -> float:
        with db_lock:
            try:
                with db.atomic():
                    record = cls.get_or_none(cls.source == source)
                    return float(record.cursor_epoch) if record is not None else 0.0
            except (IntegrityError, OperationalError, TypeError, ValueError) as e:
                logger.error(f"Error reading detector cursor: {e}")
                return 0.0

    @classmethod
    def save_cursor(cls, source: str, cursor_epoch: float) -> bool:
        try:
            cursor_epoch = float(cursor_epoch)
        except (TypeError, ValueError):
            return False

        with db_lock:
            timestamp = int(time.time())
            now = datetime.now()
            try:
                with db.atomic():
                    record = cls.get_or_none(cls.source == source)
                    if record is None:
                        cls.create(
                            source=source, cursor_epoch=cursor_epoch,
                            create_time=timestamp, create_date=now,
                            update_time=timestamp, update_date=now,
                        )
                    else:
                        (cls.update(
                            cursor_epoch=cursor_epoch,
                            update_time=timestamp,
                            update_date=now,
                        ).where(cls.source == source).execute())
                    _bump_write_epoch(cls)
                    return True
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error saving detector cursor: {e}")
                return False


class ConfigRevision(DataBaseModel):
    """A versioned snapshot of the host's hardware/software inventory
    (diagnostics config-revision chain). A new row is created only when
    ``diagnostics.config_revision`` decides the fingerprint actually changed
    -- most periodic rechecks find no change and just touch ``checked_at``.
    """

    revision_id = CharField(max_length=32, primary_key=True)
    host_id = CharField(max_length=64, null=True, index=True)
    previous_revision_id = CharField(max_length=32, null=True, index=True)
    boot_id = CharField(max_length=64, null=True, index=True)
    hw_fingerprint = CharField(max_length=64, null=True, index=True)
    sw_fingerprint = CharField(max_length=64, null=True, index=True)
    full_inventory_json = TextField(null=True, help_text="normalized inventory at this revision (JSON)")
    change_summary_json = TextField(null=True, help_text="field-level previous/current diff vs. previous_revision_id (JSON); null for the baseline revision")
    field_status_json = TextField(null=True, help_text="per-field present/unavailable/unknown/removed classification (JSON)")
    checked_at = CharField(max_length=40, null=True, help_text="last time this revision was reconfirmed current")

    @classmethod
    def insert_revision(cls, *, revision_id, host_id=None, previous_revision_id=None,
                        boot_id=None, hw_fingerprint=None, sw_fingerprint=None,
                        full_inventory=None, change_summary=None, field_status=None,
                        ts_utc=None):
        with db_lock:
            timestamp = int(time.time())
            now = datetime.now()
            try:
                with db.atomic():
                    cls.create(
                        revision_id=revision_id, host_id=host_id,
                        previous_revision_id=previous_revision_id, boot_id=boot_id,
                        hw_fingerprint=hw_fingerprint, sw_fingerprint=sw_fingerprint,
                        full_inventory_json=json.dumps(full_inventory, ensure_ascii=False, default=str) if full_inventory is not None else None,
                        change_summary_json=json.dumps(change_summary, ensure_ascii=False, default=str) if change_summary is not None else None,
                        field_status_json=json.dumps(field_status, ensure_ascii=False, default=str) if field_status is not None else None,
                        checked_at=ts_utc,
                        create_time=timestamp, create_date=now,
                        update_time=timestamp, update_date=now,
                    )
                    _bump_write_epoch(cls)
                    return DBStatus.SUCCESS
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error inserting config revision: {e}")
                return DBStatus.FAILED

    @classmethod
    def latest(cls):
        with db_lock:
            try:
                with db.atomic():
                    # create_time is second-granularity, so two revisions written
                    # in the same second would tie; SQLite's rowid increases with
                    # every insert and breaks that tie deterministically to the
                    # genuinely-latest row (the head of the revision chain).
                    return cls.select().order_by(
                        cls.create_time.desc(), SQL("rowid").desc()).first()
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error reading latest config revision: {e}")
                return None

    @classmethod
    def query_range(cls, start_time=None, end_time=None, limit=200):
        """Return revisions created within an inclusive epoch time range."""
        with db_lock:
            try:
                with db.atomic():
                    query = cls.select()
                    if isinstance(start_time, int):
                        query = query.where(cls.create_time >= start_time)
                    if isinstance(end_time, int):
                        query = query.where(cls.create_time <= end_time)
                    return list(query.order_by(cls.create_time.asc(), SQL("rowid").asc()).limit(
                        max(1, limit)))
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error querying config revisions: {e}")
                return []

    @classmethod
    def touch_checked_at(cls, revision_id: str, ts_utc: str, field_status: dict = None) -> bool:
        """Reconfirm ``revision_id`` is still current -- no new row, no fingerprint
        change. ``field_status`` is re-persisted even on a no-change recheck so an
        in-progress miss-streak (a field seen absent once, awaiting a second
        confirmation before it is called ``removed``) survives to the next check."""
        with db_lock:
            try:
                with db.atomic():
                    fields = {"checked_at": ts_utc, "update_time": int(time.time()),
                             "update_date": datetime.now()}
                    if field_status is not None:
                        fields["field_status_json"] = json.dumps(field_status, ensure_ascii=False, default=str)
                    (cls.update(**fields).where(cls.revision_id == revision_id).execute())
                    _bump_write_epoch(cls)
                    return True
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error touching config revision: {e}")
                return False


def _create_extra_indexes():
    """Indexes the model definitions cannot express: DESC ordering and the
    partial UNIQUE index on dedup_key that write-level event idempotency relies
    on. Run after create_tables()."""
    for sql in (
        "CREATE INDEX IF NOT EXISTS operationalevent_create_time ON operationalevent (create_time DESC)",
        "CREATE INDEX IF NOT EXISTS operationalevent_category_create_time ON operationalevent (category, create_time DESC)",
        # Partial: events without a dedup_key are never deduplicated.
        "CREATE UNIQUE INDEX IF NOT EXISTS operationalevent_dedup_key ON operationalevent (dedup_key) WHERE dedup_key IS NOT NULL",
    ):
        db.execute_sql(sql)


def init_database():
    db.create_tables([AIAppPriority, MonitorSnapshot, OperationalEvent, AlertState, AlertEpisode, DetectorCursor, ConfigRevision])
    _create_extra_indexes()


if __name__ == "__main__":
    db.connect()
    init_database()