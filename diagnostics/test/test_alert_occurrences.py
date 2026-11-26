# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""/diag/alert-occurrences lists when each detection of an alert happened
(scratch database from conftest)."""

import os
import sys
import time
import unittest
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Flask  # noqa: E402

from diagnostics import alerts, diag_api, emit_event, event_store  # noqa: E402

NOW = int(time.time())


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="milliseconds")


class AlertOccurrencesTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        event_store.ensure_tables()
        app = Flask(__name__)
        app.register_blueprint(diag_api.diag_bp)
        cls.client = app.test_client()
        cls.prefix = diag_api.diag_bp.url_prefix or ""

    def _oom(self, process, epoch):
        event = emit_event("RESOURCE_MEMORY_OOM_KILL", severity="error", category="resource.memory",
                           source="detector", summary=f"OOM kill of {process}", ts_utc=_iso(epoch),
                           attributes={"subject": process, "pid": 4242, "process": process})
        alerts.evaluate(event)
        return event

    def _get(self, **params):
        return self.client.get(f"{self.prefix}/alert-occurrences", query_string=params).get_json()

    def test_lists_each_detection_of_the_alert_only(self):
        process, other = f"p-{uuid.uuid4().hex[:6]}", f"q-{uuid.uuid4().hex[:6]}"
        mine = [self._oom(process, NOW - age) for age in (300, 200, 100)]
        self._oom(other, NOW - 150)
        alert = next(a for a in event_store.query_alerts(active_only=True) if a["scope"] == process)
        data = self._get(event_type=alert["event_type"], scope=alert["scope"], **{"from": NOW - 400, "to": NOW})["data"]
        self.assertEqual([e["event_id"] for e in data["events"]], [e["event_id"] for e in reversed(mine)])
        self.assertEqual((data["count"], data["truncated"]), (3, False))

    def test_limit_marks_truncation(self):
        process = f"p-{uuid.uuid4().hex[:6]}"
        for age in (300, 200, 100):
            self._oom(process, NOW - age)
        data = self._get(event_type="RESOURCE_MEMORY_OOM_KILL", scope=process, limit=2)["data"]
        self.assertEqual((data["count"], data["truncated"]), (2, True))

    def test_scope_is_required(self):
        self.assertNotEqual(self._get(event_type="RESOURCE_MEMORY_OOM_KILL")["retcode"], 0)


if __name__ == "__main__":
    unittest.main()
