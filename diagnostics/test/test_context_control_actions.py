# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Context control actions: what SmarTune did, not what it refused."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics import context, event_store  # noqa: E402


def _event(event_id, event_type, category, app_id="a1", impact="none", second=0):
    return {"event_id": event_id, "event_type": event_type, "category": category,
            "app_id": app_id, "impact": impact, "ts_utc": f"2026-11-23T08:00:{second:02d}+00:00"}


EVENTS = [
    _event("limit", "CONTROL_CPU_LIMIT_APPLIED", "platform.control", impact="degraded", second=1),
    _event("affinity", "CONTROL_CPU_AFFINITY_SET", "platform.control", second=2),
    _event("reclaim", "RESOURCE_MEMORY_RECLAIM_APPLIED", "resource.memory", second=3),
    _event("refused", "APP_RESOURCE_LIMIT_REFUSED", "platform.control", impact="refused", second=4),
    _event("pressure", "RESOURCE_MEMORY_PRESSURE_HIGH", "resource.memory", second=5),
    _event("other-app", "CONTROL_CPU_LIMIT_APPLIED", "platform.control", app_id="a2", second=6),
]


def _query_events(*, category=None, event_types=None, app_id=None, **_):
    return [event for event in reversed(EVENTS)
            if (not category or event["category"] == category)
            and (not event_types or event["event_type"] in event_types)
            and (not app_id or event["app_id"] == app_id)]


class ControlActionsTest(unittest.TestCase):

    def test_app_scope_keeps_actions_and_reclaim(self):
        with mock.patch.object(event_store, "query_events", side_effect=_query_events):
            rows = context._control_actions(event_store, 0, 100, app_id="a1")
        self.assertEqual([row["event_id"] for row in rows], ["reclaim", "affinity", "limit"])

    def test_window_scope_keeps_every_app(self):
        with mock.patch.object(event_store, "query_events", side_effect=_query_events):
            rows = context._control_actions(event_store, 0, 100)
        self.assertEqual([row["event_id"] for row in rows],
                         ["other-app", "reclaim", "affinity", "limit"])

    def test_findings_context_uses_the_same_selection(self):
        self.assertEqual([e["event_id"] for e in EVENTS if context._is_control_action(e)],
                         ["limit", "affinity", "reclaim", "other-app"])


if __name__ == "__main__":
    unittest.main()
