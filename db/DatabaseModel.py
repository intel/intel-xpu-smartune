# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
import json
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
    SqliteDatabase,
    TextField,
)
from threading import Lock
import time
from datetime import datetime
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
                logger.error(f"Error querying data: {e}")
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
                logger.error(f"Error inserting data: {e}")
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
                logger.error(f"Error batch updating data: {e}")
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
                logger.error(f"Error updating data: {e}")
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
                logger.error(f"Error deleting data: {e}")
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
    # process_names / commandline).  Those fields live only in the YAML, so a
    # hand-deleted entry used to be unrecoverable; keeping a copy here lets the
    # startup reconciliation print the exact block to paste back.
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

    @classmethod
    def insert_event(cls, *, event_id, ts_utc, severity, category, event_type,
                     summary, source=None, service=None, app_id=None, job_id=None,
                     impact=None, resource_type=None,
                     protection_id=None, episode_id=None, attributes=None):
        with db_lock:
            timestamp = int(time.time())
            now = datetime.now()
            attrs = json.dumps(attributes, ensure_ascii=False, default=str) if attributes else None
            try:
                with db.atomic():
                    cls.create(
                        event_id=event_id, ts_utc=ts_utc, severity=severity,
                        category=category, event_type=event_type, summary=summary,
                        source=source, service=service, app_id=app_id, job_id=job_id,
                        impact=impact,
                        resource_type=resource_type, protection_id=protection_id,
                        episode_id=episode_id, attributes_json=attrs,
                        create_time=timestamp, create_date=now,
                        update_time=timestamp, update_date=now,
                    )
                    _bump_write_epoch(cls)
                    return DBStatus.SUCCESS
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error inserting operational event: {e}")
                return DBStatus.FAILED

    @classmethod
    def query_events(cls, *, event_id=None, severity=None, category=None, source=None,
                     event_type=None, app_id=None, job_id=None,
                     impact=None, resource_type=None, protection_id=None, episode_id=None,
                     keyword=None, start_time=None, end_time=None,
                     limit=200, offset=0):
        """Filtered, newest-first event query. ``start_time``/``end_time`` are
        epoch seconds matched against the inherited ``create_time``."""
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
                    ):
                        if value:
                            query = query.where(field == value)
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
    """Derived alert state -- an alert is a ``severity>=warning`` event plus
    dedup/cooldown/ack, never a second log (diagnostics plan §3). Always points
    back to the event that produced it via ``last_event_id``."""

    dedup_key = CharField(max_length=128, primary_key=True)
    first_fired_at = CharField(max_length=40, null=False)
    last_fired_at = CharField(max_length=40, null=False)
    fire_count = IntegerField(default=0)
    last_event_id = CharField(max_length=32, null=True)
    acknowledged_at = CharField(max_length=40, null=True)
    severity = CharField(max_length=16, null=True, index=True)
    event_type = CharField(max_length=64, null=True, index=True)
    summary = TextField(null=True)

    @classmethod
    def record_fire(cls, *, dedup_key, ts_utc, event_id, severity=None,
                    event_type=None, summary=None,
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
                            severity=severity,
                            event_type=event_type, summary=summary,
                            create_time=now, create_date=dt,
                            update_time=now, update_date=dt,
                        )
                        _bump_write_epoch(cls)
                        return True, 1
                    should_notify = (now - int(row.update_time or 0)) >= max(0, cooldown_seconds)
                    count = int(row.fire_count or 0) + 1
                    (cls.update(
                        last_fired_at=ts_utc, fire_count=count, last_event_id=event_id,
                        severity=severity, event_type=event_type, summary=summary,
                        update_time=now, update_date=dt,
                    ).where(cls.dedup_key == dedup_key).execute())
                    _bump_write_epoch(cls)
                    return should_notify, count
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error recording alert fire: {e}")
                return False, 0

    @classmethod
    def query_alerts(cls, *, unacknowledged_only=False, limit=200):
        with db_lock:
            try:
                with db.atomic():
                    query = cls.select()
                    if unacknowledged_only:
                        query = query.where(cls.acknowledged_at.is_null(True))
                    query = query.order_by(cls.last_fired_at.desc()).limit(max(1, limit))
                    return list(query)
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error querying alerts: {e}")
                return []

    @classmethod
    def delete_acknowledged_older_than(cls, days: int) -> int:
        """Delete acknowledged alert state outside the diagnostics retention window."""
        cutoff = int(time.time()) - max(1, int(days)) * 86400
        with db_lock:
            try:
                with db.atomic():
                    return (cls.delete()
                            .where((cls.acknowledged_at.is_null(False)) & (cls.update_time < cutoff))
                            .execute())
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error deleting old acknowledged alerts: {e}")
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


def _apply_migrations():
    """Apply incremental schema migrations for existing databases."""
    migrations = [
        "ALTER TABLE aiapppriority ADD COLUMN limit_overrides_json TEXT",
        "ALTER TABLE aiapppriority ADD COLUMN network_priority VARCHAR(32)",
        "ALTER TABLE aiapppriority ADD COLUMN config_meta_json TEXT",
        "ALTER TABLE operationalevent ADD COLUMN impact VARCHAR(16)",
        "ALTER TABLE operationalevent ADD COLUMN resource_type VARCHAR(24)",
        "ALTER TABLE operationalevent ADD COLUMN protection_id VARCHAR(96)",
        "ALTER TABLE operationalevent ADD COLUMN episode_id VARCHAR(32)",
    ]
    for sql in migrations:
        try:
            db.execute_sql(sql)
        except OperationalError as e:
            if "duplicate column" not in str(e).lower():
                logger.warning(f"Migration warning ({sql!r}): {e}")

    indexes = [
        "CREATE INDEX IF NOT EXISTS operationalevent_impact ON operationalevent (impact)",
        "CREATE INDEX IF NOT EXISTS operationalevent_resource_type ON operationalevent (resource_type)",
        "CREATE INDEX IF NOT EXISTS operationalevent_protection_id ON operationalevent (protection_id)",
        "CREATE INDEX IF NOT EXISTS operationalevent_episode_id ON operationalevent (episode_id)",
    ]
    for sql in indexes:
        try:
            db.execute_sql(sql)
        except OperationalError as e:
            logger.warning(f"Migration warning ({sql!r}): {e}")


def init_database():
    db.create_tables([AIAppPriority, MonitorSnapshot, AlertState, DetectorCursor])
    # Peewee creates indexes while processing create_tables(). Upgrade an
    # existing event table before that step so indexes never target a column
    # that is absent from a legacy schema.
    if "operationalevent" in db.get_tables():
        _apply_migrations()
    OperationalEvent.create_table(safe=True)


if __name__ == "__main__":
    db.connect()
    init_database()