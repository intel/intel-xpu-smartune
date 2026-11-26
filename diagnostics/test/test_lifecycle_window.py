# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Windowed lifecycle queries summarize whole histories and keep live limits
(scratch database from conftest)."""

import os
import sys
import time
import unittest
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics import control_lifecycle, emit_event, event_store  # noqa: E402

HOUR = 3600
NOW = int(time.time())


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="milliseconds")


class LifecycleWindowTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        event_store.ensure_tables()

    def setUp(self):
        self.app = f"app-{uuid.uuid4().hex[:8]}"

    def _control(self, event_type, protection_id, age_seconds, resource="cpu"):
        emit_event(event_type, severity="info", category="platform.control", source="balancer",
                   app_id=self.app, protection_id=protection_id, resource_type=resource,
                   summary=event_type, ts_utc=_iso(NOW - age_seconds),
                   attributes={"boot_id": "a" * 32, "trigger": "pressure"})

    def _query(self, **kwargs):
        return control_lifecycle.query(app_id=self.app, **kwargs)

    def _last_day(self, **kwargs):
        return self._query(start_time=NOW - 24 * HOUR, end_time=NOW, **kwargs)

    def test_lifecycle_begun_before_the_window_keeps_its_start(self):
        pid = uuid.uuid4().hex[:12]
        self._control("CONTROL_CPU_LIMIT_APPLIED", pid, 48 * HOUR)
        self._control("CONTROL_CPU_LIMIT_RECOVERED", pid, HOUR)
        [item] = self._last_day()
        self.assertEqual((item["status"], item["applied_resources"]), ("recovered", ["cpu"]))
        self.assertEqual(item["started_at"], _iso(NOW - 48 * HOUR))

    def test_live_limit_without_events_in_the_window_is_listed(self):
        pid = uuid.uuid4().hex[:12]
        self._control("CONTROL_CPU_LIMIT_APPLIED", pid, 30 * HOUR)
        self.assertEqual([(i["protection_id"], i["status"]) for i in self._last_day()], [(pid, "active")])

    def test_live_limit_begun_after_the_window_is_not_listed(self):
        self._control("CONTROL_CPU_LIMIT_APPLIED", uuid.uuid4().hex[:12], HOUR)
        self.assertEqual(self._query(start_time=NOW - 24 * HOUR, end_time=NOW - 2 * HOUR), [])

    def test_closed_lifecycle_outside_the_window_is_not_listed(self):
        pid = uuid.uuid4().hex[:12]
        self._control("CONTROL_CPU_LIMIT_APPLIED", pid, 48 * HOUR)
        self._control("CONTROL_CPU_LIMIT_RECOVERED", pid, 47 * HOUR)
        self.assertEqual(self._last_day(), [])

    def test_limit_pages_closed_lifecycles_only(self):
        live = uuid.uuid4().hex[:12]
        self._control("CONTROL_CPU_LIMIT_APPLIED", live, 20 * HOUR)
        for age in (3, 2, 1):
            pid = uuid.uuid4().hex[:12]
            self._control("CONTROL_CPU_LIMIT_APPLIED", pid, age * HOUR)
            self._control("CONTROL_CPU_LIMIT_RECOVERED", pid, age * HOUR - 60)
        result = self._query(limit=1)
        self.assertEqual(len(result), 2)
        self.assertIn(live, [item["protection_id"] for item in result])

    def test_single_protection_returns_its_whole_history(self):
        pid = uuid.uuid4().hex[:12]
        self._control("CONTROL_CPU_LIMIT_APPLIED", pid, 48 * HOUR)
        self._control("CONTROL_CPU_LIMIT_RECOVERED", pid, HOUR)
        [item] = self._query(protection_id=pid, start_time=NOW - HOUR // 2, end_time=NOW)
        self.assertEqual(len(item["events"]), 2)


if __name__ == "__main__":
    unittest.main()
