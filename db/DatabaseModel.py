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
    fn,
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

# Database connection object. SMARTUNE_DB_PATH points a test run or second instance
# at its own file, since the connection is opened at import time.
_DB_PATH = (os.environ.get("SMARTUNE_DB_PATH")
            or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'my_database.db'))
db = SqliteDatabase(_DB_PATH)
db.execute_sql('PRAGMA journal_mode=WAL;')

# Per-model write counter, bumped whenever a row actually changes.  Read-side
# caches compare it to tell whether their in-memory copy still matches the
# table, so a hot path can be served without touching the database at all --
# every access below takes the process-wide db_lock, which the monitor's
# snapshot writer also holds, and a reader that blocks on it while the disk is
# saturated is exactly the reader that needed to run.
_write_epochs: dict[str, int] = {}


def _boot_key(boot_id):
    """A boot id as stored: journald's 32 hex digits, also when given dashed."""
    return boot_id.strip().replace("-", "").lower() if isinstance(boot_id, str) else boot_id


def get_write_epoch(model_cls) -> int:
    """Return the current write counter for ``model_cls``."""
    return _write_epochs.get(model_cls.__name__, 0)


def _bump_write_epoch(model_cls) -> None:
    """Record a write against ``model_cls``.  Called with ``db_lock`` held."""
    name = model_cls.__name__
    _write_epochs[name] = _write_epochs.get(name, 0) + 1


_DELETE_BATCH_ROWS = 2000


def _delete_in_batches(model, condition, key=None, batch=_DELETE_BATCH_ROWS):
    """Delete the rows matching ``condition`` in batches, each its own transaction under db_lock,
    so other threads get the lock in between. Returns the count deleted; stops at the first error."""
    key = key if key is not None else model._meta.primary_key
    deleted = 0
    while True:
        with db_lock:
            try:
                with db.atomic():
                    ids = model.select(key).where(condition).limit(batch)
                    count = model.delete().where(key.in_(ids)).execute()
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error deleting old {model.__name__} rows: {e}")
                return deleted
        deleted += count
        if count < batch:
            return deleted
        time.sleep(0)  # yield the lock to waiting threads


def _iso_to_epoch(value):
    """ISO 8601 -> epoch seconds (int, floored), or None; a naive value is read as local time."""
    if not value or not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError, OverflowError, OSError):
        return None


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
    def query_sampled(cls, snapshot_type: str, start_time: int, end_time: int,
                      max_points: int = 2000):
        """Snapshots spread evenly across [start_time, end_time], oldest first.

        Keeps the newest row of each of ``max_points`` time buckets. Returns
        ``(rows, step_seconds)``; ``step_seconds`` is 0 when no downsampling was needed.
        """
        start_time, end_time = int(start_time), int(end_time)
        max_points = max(1, int(max_points))
        with db_lock:
            try:
                with db.atomic():
                    base = cls.select().where(
                        (cls.snapshot_type == snapshot_type)
                        & (cls.create_time >= start_time)
                        & (cls.create_time <= end_time))
                    if base.count() <= max_points:
                        return list(base.order_by(cls.create_time.asc(), cls.id.asc())), 0
                    # Ceiling division so the bucket count never exceeds max_points.
                    step = max(1, -(-(end_time - start_time + 1) // max_points))
                    newest_per_bucket = (
                        cls.select(fn.MAX(cls.id))
                        .where((cls.snapshot_type == snapshot_type)
                               & (cls.create_time >= start_time)
                               & (cls.create_time <= end_time))
                        .group_by(SQL("(create_time - ?) / ?", (start_time, step))))
                    rows = list(cls.select().where(cls.id.in_(newest_per_bucket))
                                .order_by(cls.create_time.asc(), cls.id.asc()))
                    return rows, step
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error sampling monitor snapshots: {e}")
                return [], 0

    @classmethod
    def delete_older_than(cls, days: int) -> int:
        """Delete snapshots whose ``create_time`` is older than ``days`` days.

        Returns the number of rows deleted, or 0 on error.
        """
        cutoff = int(time.time()) - max(1, int(days)) * 86400
        return _delete_in_batches(cls, cls.create_time < cutoff)


class OperationalEvent(DataBaseModel):
    """Structured operational-event ledger (diagnostics plan §3).

    The single high-value event account: pressure transitions, control actions,
    benchmark lifecycle, detected exceptions. Raw application/system logs are NOT
    stored here -- they stay in their own sources and are joined by
    a time window. ``create_time`` (inherited) is the epoch second the event
    occurred, from ``ts_utc`` (scanners may insert it much later; ``update_time``
    is the insert time), and backs every time-window query, sort and retention
    pass. ``ts_utc`` keeps the precise timezone-aware origin timestamp for display.
    """

    event_id = CharField(max_length=32, primary_key=True)
    # No index on ts_utc, severity_origin or category alone (unfiltered, or covered by the
    # (category, create_time) index): each index costs every insert and retention delete.
    ts_utc = CharField(max_length=40, null=False, help_text="timezone-aware ISO 8601")
    severity = CharField(max_length=16, null=False, help_text="info/warning/error/critical", index=True)
    severity_origin = CharField(max_length=16, default="producer", null=True, help_text="producer | rule_escalated")
    category = CharField(max_length=32, null=False, help_text="service/pressure/control/benchmark/kernel")
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
            occurred = _iso_to_epoch(ts_utc)
            if occurred is None:
                occurred = timestamp
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
                        create_time=occurred,
                        create_date=datetime.fromtimestamp(occurred),
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
                     severities=None, limit=200, offset=0):
        """Filtered, newest-first event query. ``start_time``/``end_time`` are
        epoch seconds matched against the occurrence time (``create_time``);
        ``event_types`` / ``severities`` keep events matching any given value."""
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
                        (cls.boot_id, _boot_key(boot_id)),
                        (cls.config_revision_id, config_revision_id),
                    ):
                        if value:
                            query = query.where(field == value)
                    if event_types:
                        query = query.where(cls.event_type.in_(list(event_types)))
                    if severities:
                        query = query.where(cls.severity.in_(list(severities)))
                    if keyword:
                        query = query.where(
                            cls.summary.contains(keyword) | cls.event_type.contains(keyword))
                    if isinstance(start_time, int):
                        query = query.where(cls.create_time >= start_time)
                    if isinstance(end_time, int):
                        query = query.where(cls.create_time <= end_time)
                    # rowid breaks same-second ties so paging stays stable.
                    query = query.order_by(cls.create_time.desc(), SQL("rowid").desc())
                    query = query.limit(max(1, limit)).offset(max(0, offset))
                    return list(query)
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error querying operational events: {e}")
                return []

    @classmethod
    def delete_older_than(cls, days: int, keep_event_ids=()) -> int:
        """Delete operational events outside the retention window, but keep the
        full history of any control protection that is still unresolved, and
        the events named in ``keep_event_ids`` (evidence live state points at).

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
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error deleting old operational events: {e}")
                return 0
        condition = cls.create_time < cutoff
        if retain:
            # A protection that turns live between batches does so through a
            # new APPLIED, which is inside the window and kept anyway.
            condition &= cls.protection_id.is_null(True) | cls.protection_id.not_in(list(retain))
        keep_event_ids = [event_id for event_id in keep_event_ids if event_id]
        if keep_event_ids:
            condition &= cls.event_id.not_in(keep_event_ids)
        return _delete_in_batches(cls, condition)

    @classmethod
    def newest_event_ids_per_source(cls, event_types) -> set:
        """The newest event of ``event_types`` from each source, in query_events' order."""
        event_types = list(event_types)
        with db_lock:
            try:
                sources = [row.source for row in cls.select(cls.source).distinct()
                           .where(cls.event_type.in_(event_types))]
                newest = set()
                for source in sources:
                    row = (cls.select(cls.event_id)
                           .where(cls.event_type.in_(event_types)
                                  & (cls.source.is_null(True) if source is None else cls.source == source))
                           .order_by(cls.create_time.desc(), SQL("rowid").desc()).first())
                    if row is not None:
                        newest.add(row.event_id)
                return newest
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error reading newest events per source: {e}")
                return set()

    @classmethod
    def _scan_control_lifecycles(cls) -> dict:
        """One pass over lifecycle events (event_catalog.LIFECYCLE_EVENTS) -> per-protection state
        ``{live, cleared, latest}``, shared by retention and the active-protection read model.
        ``live`` holds the resources whose latest action is APPLIED, as control_lifecycle.summarize()
        decides (not APPLIED counted against terminals: re-caps reuse the protection id).
        Lock-free: callers that need isolation hold ``db_lock`` (``delete_older_than`` does)."""
        from diagnostics import event_catalog

        definitions = {event.event_type: event for event in event_catalog.LIFECYCLE_EVENTS}
        stats = {}
        rows = (cls.select(cls.protection_id, cls.event_type, cls.resource_type, cls.create_time)
                .where(cls.protection_id.is_null(False) & cls.event_type.in_(list(definitions)))
                .order_by(cls.create_time, SQL("rowid")))
        for row in rows:
            state = stats.get(row.protection_id)
            if state is None:
                state = stats[row.protection_id] = {"live": set(), "cleared": False, "latest": 0}
            definition = definitions[row.event_type]
            if definition.resource is not None:
                resource = row.resource_type or definition.resource
                if definition.action == "applied":
                    state["live"].add(resource)
                else:  # recovered / failed
                    state["live"].discard(resource)
            elif definition.action in event_catalog.LIFECYCLE_CLEARED_ACTIONS:
                state["cleared"] = True
            state["latest"] = max(state["latest"], row.create_time or 0)
        return stats

    @staticmethod
    def _is_unresolved(state) -> bool:
        """A resource limit that is still live: some resource's latest action is
        APPLIED and no reboot or operator clear ended the lifecycle."""
        return not state["cleared"] and bool(state["live"])

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
    def severity_counts(cls, *, start_time=None, end_time=None, boot_id=None, exclude_category=None):
        """Exact per-severity counts of the matching events and the occurrence
        span they cover: ``(counts, first_epoch, last_epoch)``."""
        with db_lock:
            try:
                query = cls.select(cls.severity, fn.COUNT(cls.event_id),
                                   fn.MIN(cls.create_time), fn.MAX(cls.create_time))
                if start_time is not None:
                    query = query.where(cls.create_time >= start_time)
                if end_time is not None:
                    query = query.where(cls.create_time <= end_time)
                if boot_id:
                    query = query.where(cls.boot_id == _boot_key(boot_id))
                if exclude_category:
                    query = query.where(cls.category != exclude_category)
                rows = list(query.group_by(cls.severity).tuples())
            except OperationalError as e:
                logger.error(f"Error counting operational events: {e}")
                return {}, None, None
        counts = {severity: count for severity, count, _, _ in rows}
        firsts = [first for _, _, first, _ in rows if first is not None]
        lasts = [last for _, _, _, last in rows if last is not None]
        return counts, (min(firsts) if firsts else None), (max(lasts) if lasts else None)

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


class AlertChanged(Exception):
    """The alert gained detections after the operator loaded it."""

    def __init__(self, dedup_key, fire_count, last_event_id):
        super().__init__(f"alert {dedup_key} changed since it was loaded")
        self.dedup_key = dedup_key
        self.fire_count = fire_count
        self.last_event_id = last_event_id


class AlertState(DataBaseModel):
    """Derived alert state for policy-promoted events, never a second log.

    Active and resolved rows retain the alert lifecycle shown to users. Rows left in
    the legacy "pending" status by older releases are never shown and age out. Every row
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
    # What closed the occurrence (the policy may change later): acknowledged / recovered /
    # expired / operator_closed / cleared_by_reboot.
    resolved_reason = CharField(max_length=32, null=True)
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
                    became_active = row.status != "active"
                    should_notify = became_active or (now - int(row.update_time or 0)) >= max(0, cooldown_seconds)
                    # A new occurrence counts from 1, like first_fired_at below;
                    # the previous occurrence's count lives on in AlertEpisode.
                    count = 1 if became_active else int(row.fire_count or 0) + 1
                    keeps_escalation = not became_active and row.severity_origin == "rule_escalated"
                    fields = dict(
                        last_fired_at=ts_utc, fire_count=count, last_event_id=event_id,
                        severity=(row.severity if keeps_escalation else severity),
                        event_type=event_type, summary=summary,
                        scope=scope, status="active", pending_since=None, last_seen_at=ts_utc,
                        resolved_at=None, resolved_event_id=None, resolved_reason=None,
                    )
                    if should_notify:
                        fields["update_time"] = now
                        fields["update_date"] = dt
                    if became_active:
                        fields["first_fired_at"] = ts_utc
                        # A new occurrence needs a new acknowledgement.
                        fields["acknowledged_at"] = None
                        # A silence runs until its deadline, across occurrences of a flapping alert.
                        silenced_until = _iso_to_epoch(row.silenced_until)
                        if silenced_until is None or silenced_until <= now:
                            fields["silenced_until"] = None
                            fields["silence_reason"] = None
                        # Escalation applied to the previous occurrence; a new one starts unescalated.
                        fields.update(
                            severity_origin="producer", baseline_severity=None,
                            escalation_rule_id=None, escalation_rule_version=None,
                            escalation_reason=None, escalation_evidence_json=None,
                            escalated_at=None,
                        )
                    (cls.update(**fields).where(cls.dedup_key == dedup_key).execute())
                    _bump_write_epoch(cls)
                    return should_notify, count
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error recording alert fire: {e}")
                return False, 0

    @classmethod
    def active_alerts(cls):
        with db_lock:
            try:
                return list(cls.select().where(cls.status == "active"))
            except OperationalError as e:
                logger.error(f"Error reading active alerts: {e}")
                return []

    @staticmethod
    def _check_seen(row, seen_event_id):
        """Raise AlertChanged if ``last_event_id`` moved on since the operator loaded the alert."""
        if seen_event_id is not None and row.last_event_id != seen_event_id:
            raise AlertChanged(row.dedup_key, row.fire_count, row.last_event_id)

    @classmethod
    def acknowledge(cls, dedup_key, ts_utc, seen_event_id=None):
        """Mark an active alert as handled. ``seen_event_id`` is the
        ``last_event_id`` the operator saw; raises AlertChanged when it is stale."""
        with db_lock:
            try:
                with db.atomic():
                    row = cls.get_or_none((cls.dedup_key == dedup_key) & (cls.status == "active"))
                    if row is None:
                        return False
                    cls._check_seen(row, seen_event_id)
                    (cls.update(
                        acknowledged_at=ts_utc,
                        update_time=int(time.time()), update_date=datetime.now(),
                    ).where(cls.dedup_key == dedup_key).execute())
                    _bump_write_epoch(cls)
                    return True
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error acknowledging alert: {e}")
                return False

    @classmethod
    def close_acknowledged(cls, dedup_key, ts_utc, reason="acknowledged", seen_event_id=None):
        """Acknowledge and close an active alert in one step: for alerts with no
        recovery signal (an operator having seen it is what ends it), and for manual
        closes (``reason="operator_closed"``). The occurrence is archived like any other
        resolution, with no ``resolved_event_id``. ``seen_event_id``: see acknowledge()."""
        with db_lock:
            try:
                with db.atomic():
                    row = cls.get_or_none((cls.dedup_key == dedup_key) & (cls.status == "active"))
                    if row is None:
                        return False
                    cls._check_seen(row, seen_event_id)
                    acknowledged_at = row.acknowledged_at or ts_utc
                    row.acknowledged_at = acknowledged_at
                    AlertEpisode.archive(row, resolved_at=ts_utc, resolved_reason=reason)
                    (cls.update(
                        acknowledged_at=acknowledged_at,
                        status="resolved", resolved_at=ts_utc, resolved_event_id=None,
                        resolved_reason=reason,
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
    def live_evidence_event_ids(cls) -> set:
        """Event ids active alerts point at: their latest occurrence and escalation trigger."""
        with db_lock:
            try:
                rows = list(cls.select(cls.last_event_id, cls.escalation_evidence_json)
                            .where(cls.status == "active"))
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error reading active alert evidence: {e}")
                return set()
        ids = set()
        for row in rows:
            ids.add(row.last_event_id)
            try:
                evidence = json.loads(row.escalation_evidence_json or "{}")
            except ValueError:
                evidence = {}
            if isinstance(evidence, dict):
                ids.add(evidence.get("trigger_event_id"))
        ids.discard(None)
        ids.discard("")
        return ids

    @classmethod
    def resolve(cls, *, event_type, scope, ts_utc, resolved_event_id=None,
                reason="recovered", last_seen_before=None):
        """Resolve active alerts only when a matching recovery fact is recorded.
        Each resolved occurrence is archived as an AlertEpisode first, because
        this row is reused (and overwritten) if the alert fires again.

        ``last_seen_before`` (UTC ISO) closes only alerts not seen since, so one
        renewed during a lease sweep stays open.
        """
        with db_lock:
            try:
                with db.atomic():
                    condition = ((cls.event_type == event_type)
                                 & (cls.scope == scope)
                                 & (cls.status == "active"))
                    if last_seen_before is not None:
                        condition &= cls.last_seen_at.is_null(False) & (cls.last_seen_at <= last_seen_before)
                    for row in cls.select().where(condition):
                        AlertEpisode.archive(row, resolved_at=ts_utc, resolved_event_id=resolved_event_id,
                                             resolved_reason=reason)
                    changed = (cls.update(
                        status="resolved", resolved_at=ts_utc,
                        resolved_event_id=resolved_event_id, resolved_reason=reason,
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
        """Delete resolved and legacy "pending" alerts outside the diagnostics retention window."""
        cutoff = int(time.time()) - max(1, int(days)) * 86400
        with db_lock:
            try:
                with db.atomic():
                    return (cls.delete()
                            .where(cls.status.in_(("resolved", "pending")) & (cls.update_time < cutoff))
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
    resolved_reason = CharField(max_length=32, null=True)
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
    def archive(cls, state, *, resolved_at, resolved_event_id=None, resolved_reason=None):
        """Copy an AlertState row as a closed occurrence. Caller holds
        ``db_lock`` inside a transaction."""
        now = int(time.time())
        dt = datetime.now()
        fields = {name: getattr(state, name) for name in _ALERT_EPISODE_FIELDS}
        (cls.insert(
            episode_id=cls.episode_id_for(state.dedup_key, state.first_fired_at),
            resolved_at=resolved_at, resolved_event_id=resolved_event_id,
            resolved_reason=resolved_reason,
            create_time=now, create_date=dt, update_time=now, update_date=dt,
            **fields,
        ).on_conflict_ignore().execute())

    @classmethod
    def delete_older_than(cls, days: int) -> int:
        """Delete occurrences outside the diagnostics retention window."""
        cutoff = int(time.time()) - max(1, int(days)) * 86400
        return _delete_in_batches(cls, cls.update_time < cutoff)

class DetectorCursor(DataBaseModel):
    """Persistent scan position for a diagnostics event source."""

    source = CharField(max_length=64, primary_key=True)
    cursor_epoch = FloatField(default=0.0)
    # Opaque source-native position (journald __CURSOR) to resume exactly after the last
    # entry. Added by _ensure_columns() on old DBs.
    cursor_text = TextField(null=True)

    @classmethod
    def get_position(cls, source: str):
        """Return ``(cursor_epoch, cursor_text)``; ``(0.0, None)`` when unset."""
        with db_lock:
            try:
                with db.atomic():
                    record = cls.get_or_none(cls.source == source)
                    if record is None:
                        return 0.0, None
                    return float(record.cursor_epoch or 0.0), (record.cursor_text or None)
            except (IntegrityError, OperationalError, TypeError, ValueError) as e:
                logger.error(f"Error reading detector cursor: {e}")
                return 0.0, None

    @classmethod
    def save_cursor(cls, source: str, cursor_epoch: float, cursor_text: str = None) -> bool:
        """Persist the scan position; ``cursor_text`` is only overwritten when given."""
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
                            cursor_text=cursor_text,
                            create_time=timestamp, create_date=now,
                            update_time=timestamp, update_date=now,
                        )
                    else:
                        fields = {"cursor_epoch": cursor_epoch,
                                  "update_time": timestamp, "update_date": now}
                        if cursor_text is not None:
                            fields["cursor_text"] = cursor_text
                        cls.update(**fields).where(cls.source == source).execute()
                    _bump_write_epoch(cls)
                    return True
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error saving detector cursor: {e}")
                return False


class ErrorSignature(DataBaseModel):
    """One normalized error message template (diagnostics.error_signatures), kept across restarts.

    ``first_seen`` / ``last_seen`` are event times (epoch seconds), not row write times.
    """

    fingerprint = CharField(max_length=32, primary_key=True)
    source = CharField(max_length=32, null=True)
    origin = CharField(max_length=128, null=True)
    template = TextField(null=True)
    sample = TextField(null=True)
    first_seen = FloatField(default=0.0)
    last_seen = FloatField(default=0.0, index=True)
    total_count = IntegerField(default=0)

    @classmethod
    def load_recent(cls, limit: int):
        """Most recently seen signatures, newest first; [] on a read failure."""
        with db_lock:
            try:
                with db.atomic():
                    return list(cls.select().order_by(cls.last_seen.desc()).limit(limit))
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error reading error signatures: {e}")
                return []

    @classmethod
    def lookup(cls, fingerprint: str):
        with db_lock:
            try:
                with db.atomic():
                    return cls.get_or_none(cls.fingerprint == fingerprint)
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error reading error signature: {e}")
                return None

    @classmethod
    def upsert_many(cls, rows) -> bool:
        """Insert or replace whole signature rows (dicts of the columns above)."""
        if not rows:
            return True
        timestamp = int(time.time())
        now = datetime.now()
        with db_lock:
            try:
                with db.atomic():
                    for row in rows:
                        cls.insert(
                            **row, create_time=timestamp, create_date=now,
                            update_time=timestamp, update_date=now,
                        ).on_conflict(
                            conflict_target=[cls.fingerprint],
                            preserve=[cls.source, cls.origin, cls.template, cls.last_seen,
                                      cls.total_count, cls.update_time, cls.update_date],
                        ).execute()
                    _bump_write_epoch(cls)
                    return True
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error saving error signatures: {e}")
                return False

    @classmethod
    def prune(cls, older_than_epoch: float) -> int:
        with db_lock:
            try:
                with db.atomic():
                    deleted = cls.delete().where(cls.last_seen < older_than_epoch).execute()
                    if deleted:
                        _bump_write_epoch(cls)
                    return deleted
            except (IntegrityError, OperationalError) as e:
                logger.error(f"Error pruning error signatures: {e}")
                return 0

    # The learning-period state lives in DetectorCursor, the detectors' small
    # key/value store: the end time as the epoch, the rest as JSON.
    _LEARNING_KEY = "error_signatures.learning"

    @classmethod
    def load_learning(cls):
        """The persisted learning state, or None before the first save."""
        until, text = DetectorCursor.get_position(cls._LEARNING_KEY)
        if text is None:
            return None
        try:
            state = json.loads(text)
        except (TypeError, ValueError):
            return None
        if not isinstance(state, dict):
            return None
        state["until"] = until
        return state

    @classmethod
    def save_learning(cls, state):
        extra = {key: value for key, value in state.items() if key != "until"}
        return DetectorCursor.save_cursor(cls._LEARNING_KEY, float(state.get("until") or 0.0),
                                          json.dumps(extra, sort_keys=True))


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
    """Indexes the model definitions cannot express: DESC ordering, composite
    keys and the partial UNIQUE index on dedup_key that write-level event
    idempotency relies on. Run after create_tables(), under db_lock."""
    with db_lock:
        for sql in (
            "CREATE INDEX IF NOT EXISTS operationalevent_create_time ON operationalevent (create_time DESC)",
            "CREATE INDEX IF NOT EXISTS operationalevent_category_create_time ON operationalevent (category, create_time DESC)",
            # Partial: events without a dedup_key are never deduplicated.
            "CREATE UNIQUE INDEX IF NOT EXISTS operationalevent_dedup_key ON operationalevent (dedup_key) WHERE dedup_key IS NOT NULL",
            # Snapshot reads select one type in a time range; retention deletes by time.
            "CREATE INDEX IF NOT EXISTS monitorsnapshot_type_create_time ON monitorsnapshot (snapshot_type, create_time)",
            "CREATE INDEX IF NOT EXISTS monitorsnapshot_create_time ON monitorsnapshot (create_time)",
        ):
            db.execute_sql(sql)


# Columns added after a table first shipped. create_tables() never alters an
# existing table, so an upgraded install needs them added explicitly.
_ADDED_COLUMNS = (
    ("detectorcursor", "cursor_text", "TEXT"),
    ("alertstate", "resolved_reason", "VARCHAR(32)"),
    ("alertepisode", "resolved_reason", "VARCHAR(32)"),
)
# The DDL is built once from the constants above, never at call time (SDL SAV-DB-01).
_ADD_COLUMN_SQL = tuple((table, column, f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
                        for table, column, sql_type in _ADDED_COLUMNS)


def _ensure_columns():
    for table, column, statement in _ADD_COLUMN_SQL:
        if column not in {meta.name for meta in db.get_columns(table)}:
            db.execute_sql(statement)


def _backfill_event_occurrence():
    """Set create_time from ts_utc on rows from older releases, which stored the insert time.

    Only timezone-qualified values are converted (SQLite reads a naive one as UTC, the writer
    meant local time); a no-op once every row is aligned.
    """
    with db_lock:
        db.execute_sql(
            "UPDATE operationalevent SET create_time = CAST(strftime('%s', ts_utc) AS INTEGER) "
            "WHERE (ts_utc LIKE '%+__:__' OR ts_utc LIKE '%-__:__' OR ts_utc LIKE '%Z') "
            "AND strftime('%s', ts_utc) IS NOT NULL "
            "AND create_time != CAST(strftime('%s', ts_utc) AS INTEGER)")


# ISO 8601 columns compared as strings; values older releases wrote with a local
# offset ("+08:00") are rewritten to UTC.
_UTC_COLUMNS = (
    ("operationalevent", ("ts_utc", "acknowledged_at")),
    ("alertstate", ("first_fired_at", "last_fired_at", "acknowledged_at", "silenced_until",
                    "pending_since", "last_seen_at", "resolved_at", "escalated_at")),
    ("alertepisode", ("first_fired_at", "last_fired_at", "resolved_at", "acknowledged_at",
                      "silenced_until", "escalated_at")),
)
# Built once from the constants above, like _ADD_COLUMN_SQL; no-ops once every row is UTC.
_UTC_REWRITE_SQL = tuple(
    f"UPDATE {table} SET {column} = strftime('%Y-%m-%dT%H:%M:%f', {column}) || '+00:00' "
    f"WHERE ({column} LIKE '%+__:__' OR {column} LIKE '%-__:__' OR {column} LIKE '%Z') "
    f"AND {column} NOT LIKE '%+00:00' AND strftime('%s', {column}) IS NOT NULL"
    for table, columns in _UTC_COLUMNS for column in columns)


# Boot ids written before journald's dashless form was adopted. Built once.
_LEGACY_BOOT_ID_PROBE_SQL = "SELECT 1 FROM operationalevent WHERE boot_id LIKE '%-%' LIMIT 1"
_BOOT_ID_REWRITE_SQL = (
    "UPDATE operationalevent SET boot_id = lower(replace(boot_id, '-', '')) WHERE boot_id LIKE '%-%'",
    "UPDATE configrevision SET boot_id = lower(replace(boot_id, '-', '')) WHERE boot_id LIKE '%-%'",
) + tuple(
    f"UPDATE operationalevent SET attributes_json = json_set(attributes_json, '$.{key}', "
    f"lower(replace(json_extract(attributes_json, '$.{key}'), '-', ''))) "
    f"WHERE attributes_json LIKE '%\"{key}\"%' AND json_valid(attributes_json) "
    f"AND json_extract(attributes_json, '$.{key}') LIKE '%-%'"
    for key in ("boot_id", "previous_boot_id"))


def _normalize_boot_ids():
    """Rewrite dashed boot ids once, so one boot is never listed as two."""
    with db_lock:
        if db.execute_sql(_LEGACY_BOOT_ID_PROBE_SQL).fetchone() is None:
            return
        with db.atomic():
            for statement in _BOOT_ID_REWRITE_SQL:
                db.execute_sql(statement)


def _normalize_utc_timestamps():
    with db_lock:
        with db.atomic():
            for statement in _UTC_REWRITE_SQL:
                db.execute_sql(statement)


def init_database():
    db.create_tables([AIAppPriority, MonitorSnapshot, OperationalEvent, AlertState, AlertEpisode, DetectorCursor, ConfigRevision, ErrorSignature])
    _ensure_columns()
    _create_extra_indexes()
    try:
        _backfill_event_occurrence()
    except OperationalError as e:  # never block startup on a best-effort re-key
        logger.warning(f"Operational event occurrence backfill skipped: {e}")
    try:
        _normalize_utc_timestamps()
    except OperationalError as e:
        logger.warning(f"UTC timestamp normalization skipped: {e}")
    try:
        _normalize_boot_ids()
    except OperationalError as e:
        logger.warning(f"Boot id normalization skipped: {e}")


if __name__ == "__main__":
    db.connect()
    init_database()