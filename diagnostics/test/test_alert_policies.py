# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Built-in alert policies: one close mode each, and which events alert."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics.alerts import ALERT_POLICIES  # noqa: E402
from diagnostics.event_catalog import EVENT_CATALOG  # noqa: E402

_CLOSE_KEYS = ("close_on_ack", "ttl_seconds", "resolved_by")


class AlertPolicyTest(unittest.TestCase):

    def test_each_policy_has_exactly_one_close_mode(self):
        for event_type, policy in ALERT_POLICIES.items():
            with self.subTest(event_type=event_type):
                modes = [key for key in _CLOSE_KEYS if policy.get(key)]
                self.assertEqual(len(modes), 1, modes)

    def test_a_never_seen_error_is_evidence_and_a_burst_alerts(self):
        self.assertNotIn("LOG_ERROR_SIGNATURE_NEW", ALERT_POLICIES)
        self.assertIn("LOG_ERROR_SIGNATURE_BURST", ALERT_POLICIES)

    def test_every_policy_names_a_catalogued_event(self):
        catalogued = {entry[0] for entry in EVENT_CATALOG}
        for event_type in ALERT_POLICIES:
            with self.subTest(event_type=event_type):
                self.assertIn(event_type, catalogued)


if __name__ == "__main__":
    unittest.main()
