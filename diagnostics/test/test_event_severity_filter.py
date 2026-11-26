# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""/diag/events min_severity keeps that level and above (scratch database from conftest)."""

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Flask  # noqa: E402

from diagnostics import diag_api, emit_event, event_store  # noqa: E402


class MinSeverityTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        event_store.ensure_tables()
        app = Flask(__name__)
        app.register_blueprint(diag_api.diag_bp)
        cls.client = app.test_client()
        cls.prefix = diag_api.diag_bp.url_prefix or ""

    def setUp(self):
        self.app_id = f"app-{uuid.uuid4().hex[:8]}"
        for severity in event_store.SEVERITY_ORDER:
            emit_event("CONTROL_APP_FROZEN", severity=severity, category="platform.control",
                       source="balancer", app_id=self.app_id, summary=severity,
                       attributes={"trigger": "manual", "boot_id": "a" * 32})

    def _events(self, **params):
        response = self.client.get(f"{self.prefix}/events", query_string={"app_id": self.app_id, **params})
        return response.get_json()

    def _severities(self, **params):
        return sorted(event["severity"] for event in self._events(**params)["data"]["events"])

    def test_min_severity_includes_higher_levels(self):
        self.assertEqual(self._severities(min_severity="error"), ["critical", "error"])
        self.assertEqual(self._severities(min_severity="warning"), ["critical", "error", "warning"])

    def test_severity_stays_an_exact_match(self):
        self.assertEqual(self._severities(severity="error"), ["error"])

    def test_min_severity_with_keep_important_reports_truncation(self):
        data = self._events(min_severity="warning", keep_important="true", limit=2)["data"]
        self.assertEqual((data["count"], data["truncated"]), (2, True))

    def test_keep_important_without_severity_still_works(self):
        data = self._events(keep_important="true", limit=1)["data"]
        self.assertEqual(sorted(e["severity"] for e in data["events"]), ["critical", "error", "warning"])
        self.assertTrue(data["truncated"])

    def test_min_severity_with_keep_important_pages_by_offset(self):
        first = self._events(min_severity="warning", keep_important="true", limit=2)["data"]
        rest = self._events(min_severity="warning", keep_important="true", limit=2, offset=2)["data"]
        ids = [e["event_id"] for e in first["events"] + rest["events"]]
        self.assertEqual((len(ids), len(set(ids)), rest["truncated"]), (3, 3, False))

    def test_keep_important_without_severity_rejects_offset(self):
        self.assertNotEqual(self._events(keep_important="true", offset=1)["retcode"], 0)

    def test_unknown_min_severity_is_rejected(self):
        self.assertNotEqual(self._events(min_severity="warn")["retcode"], 0)


if __name__ == "__main__":
    unittest.main()
