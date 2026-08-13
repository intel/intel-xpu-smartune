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

from utils.logger import logger

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


def _apply_migrations():
    """Apply incremental schema migrations for existing databases."""
    migrations = [
        "ALTER TABLE aiapppriority ADD COLUMN limit_overrides_json TEXT",
        "ALTER TABLE aiapppriority ADD COLUMN network_priority VARCHAR(32)",
    ]
    for sql in migrations:
        try:
            db.execute_sql(sql)
        except OperationalError as e:
            if "duplicate column" not in str(e).lower():
                logger.warning(f"Migration warning ({sql!r}): {e}")


def init_database():
    db.create_tables([AIAppPriority, MonitorSnapshot])  # Add other tables as needed
    _apply_migrations()


if __name__ == "__main__":
    db.connect()
    db.create_tables([AIAppPriority, MonitorSnapshot])