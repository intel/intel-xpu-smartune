# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Retention keeps old events that live state still reads (scratch database from conftest)."""

import os
import sys
import time
import unittest
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics import alerts, emit_event, event_store, retention, service_lifecycle  # noqa: E402

DAY = 86400


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="milliseconds")


def _exists(event_id):
    return bool(event_store.query_events(event_id=event_id))


class RetentionLiveStateTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        event_store.ensure_tables()

    def _oom(self, age_seconds):
        process = f"proc-{uuid.uuid4().hex[:8]}"
        event = emit_event("RESOURCE_MEMORY_OOM_KILL", severity="error", category="resource.memory",
                           source="detector", summary=f"OOM kill of {process}",
                           ts_utc=_iso(time.time() - age_seconds),
                           attributes={"subject": process, "pid": 4242, "process": process})
        alerts.evaluate(event)
        return event

    def test_evidence_of_an_active_alert_survives(self):
        event = self._oom(4 * DAY)
        retention.run_cleanup(3)
        self.assertTrue(_exists(event["event_id"]))

    def test_evidence_of_a_closed_alert_expires(self):
        event = self._oom(4 * DAY)
        dedup_key = next(a["dedup_key"] for a in event_store.query_alerts(active_only=True)
                         if a.get("last_event_id") == event["event_id"])
        alerts.acknowledge(dedup_key)
        retention.run_cleanup(3)
        self.assertFalse(_exists(event["event_id"]))

    def test_unrelated_old_event_expires(self):
        event = emit_event("CONTROL_APP_FROZEN", severity="info", category="platform.control",
                           source="balancer", summary="frozen", ts_utc=_iso(time.time() - 4 * DAY),
                           attributes={"trigger": "manual", "boot_id": "a" * 32})
        retention.run_cleanup(3)
        self.assertFalse(_exists(event["event_id"]))

    def test_long_running_service_killed_is_still_reported_as_crashed(self):
        source = f"svc{uuid.uuid4().hex[:6]}"
        old_start = emit_event(service_lifecycle.STARTED, severity="info", category="platform.availability",
                               source=source, summary="started", ts_utc=_iso(time.time() - 4 * DAY))
        retention.run_cleanup(3)
        self.assertTrue(_exists(old_start["event_id"]))
        crash = service_lifecycle.record_unclean_exit(source)
        self.assertIsNotNone(crash)
        self.assertEqual(crash["event_type"], service_lifecycle.CRASHED)

    def test_only_the_newest_lifecycle_record_is_kept(self):
        source = f"svc{uuid.uuid4().hex[:6]}"
        older = emit_event(service_lifecycle.STARTED, severity="info", category="platform.availability",
                           source=source, summary="started", ts_utc=_iso(time.time() - 5 * DAY))
        newer = emit_event(service_lifecycle.STOPPED, severity="info", category="platform.availability",
                           source=source, summary="stopped", ts_utc=_iso(time.time() - 4 * DAY))
        retention.run_cleanup(3)
        self.assertEqual((_exists(older["event_id"]), _exists(newer["event_id"])), (False, True))


if __name__ == "__main__":
    unittest.main()
