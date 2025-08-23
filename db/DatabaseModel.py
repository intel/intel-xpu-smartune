#
#  Copyright (C) 2025 Intel Corporation
#
#  This software and the related documents are Intel copyrighted materials, 
#  and your use of them is governed by the express license under which they 
#  were provided to you ("License"). Unless the License provides otherwise, 
#  you may not use, modify, copy, publish, distribute, disclose or transmit 
#  his software or the related documents without Intel's prior written permission.
#
#  This software and the related documents are provided as is, with no express 
#  or implied warranties, other than those that are expressly stated in the License.
#

import os
from peewee import *
from threading import Lock
import time
from datetime import datetime
from enum import Enum

class DBStatus(Enum):
    SUCCESS = "SUCCESS"
    ALREADY_EXISTING = "ALREADY_EXISTING"
    FAILED = "FAILED"
    NO_PERMISSION = "NO_PERMISSION"
    NOT_FOUND = "NOT_FOUND"

# Create a global synchronization lock
db_lock = Lock()

# Database connection object
db = SqliteDatabase('my_database.db')
db.execute_sql('PRAGMA journal_mode=WAL;')

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
                print(f"Error Query data: {e}")
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
                        return DBStatus.SUCCESS  # True stands for success/already existing
                    else:
                        print(f"User with ID {data['id']} already exists.")
                        return DBStatus.ALREADY_EXISTING  # Handle as needed (e.g., return the existing instance)
            except  (IntegrityError, OperationalError) as e:
                print(f"Error inserting data: {e}")
                return DBStatus.FAILED

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
                        # 明确检查记录是否存在
                        exists = cls.select().where(cls.id == id).exists()
                        return DBStatus.NOT_FOUND if not exists else DBStatus.SUCCESS
                    return DBStatus.SUCCESS
            except  (IntegrityError, OperationalError) as e:
                print(f"Error updating data: {e}")
                return None

    @classmethod
    def delete_record(cls, id):
        """Delete a record by ID in a thread-safe manner."""
        with db_lock:
            try:
                with db.atomic():
                    deleted_count = cls.delete().where(cls.id == id).execute()
                    return deleted_count
            except  (IntegrityError, OperationalError) as e:
                print(f"Error deleting data: {e}")
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
    controlled = BooleanField(default=False, help_text="whether this app is controlled", index=True)
    cgroup = CharField(max_length=255, null=True, help_text=" where does it manage in cgroup", index=True)
    cmdline = TextField(null=True, help_text="app launch cmdline", index=True)
    up_time = DateTimeField(null=True, index=True)
    status = BooleanField(default=False, help_text="app status, true means running", index=True)


def init_database():
    db.create_tables([AIAppPriority])  # Add other tables as needed


if __name__ == "__main__":
    print("test*****************")
    db.connect()
    db.create_tables([AIAppPriority])