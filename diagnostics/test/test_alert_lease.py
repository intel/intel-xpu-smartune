# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Lease alerts: renewed by detections, closed when the lease runs out
(scratch database from conftest)."""

import os
import sys
import unittest
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from db.DatabaseModel import AlertEpisode, AlertState  # noqa: E402
from diagnostics import alerts, event_store  # noqa: E402

LOOP = "PLATFORM_SYSTEMD_RESTART_LOOP"  # lease of 300 s
T0 = 1_900_000_000


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="milliseconds")


class LeaseTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        event_store.ensure_tables()

    def setUp(self):
        self.unit = f"u{uuid.uuid4().hex[:8]}.service"
        self.key = f"{LOOP}::{self.unit}"

    def _fire(self, epoch, restart_count=1):
        return alerts.evaluate({
            "event_type": LOOP, "event_id": uuid.uuid4().hex[:12], "ts_utc": _iso(epoch),
            "source": "journal", "summary": "restart loop",
            "attributes": {"subject": self.unit, "restart_count": restart_count},
        })

    def _expire(self, epoch):
        alerts.resolve_expired(now_epoch=epoch)

    def _row(self):
        return AlertState.get(AlertState.dedup_key == self.key)

    def _episodes(self):
        return list(AlertEpisode.select().where(AlertEpisode.dedup_key == self.key))

    def test_detection_within_the_lease_keeps_one_occurrence(self):
        self._fire(T0)
        self._expire(T0 + 240)
        _, count = self._fire(T0 + 240)
        row = self._row()
        self.assertEqual((row.status, count, row.first_fired_at), ("active", 2, _iso(T0)))
        self.assertEqual(self._episodes(), [])

    def test_detection_after_expiry_opens_a_new_occurrence(self):
        self._fire(T0)
        self._fire(T0 + 60)
        self._expire(T0 + 361)
        notified, count = self._fire(T0 + 400)
        self.assertTrue(notified)
        self.assertEqual(count, 1)
        self.assertEqual([e.fire_count for e in self._episodes()], [2])

    def test_lease_closes_when_it_ran_out_not_when_swept(self):
        self._fire(T0)
        self._expire(T0 + 8 * 3600)
        self.assertEqual(alerts._epoch(self._row().resolved_at), T0 + 300)

    def test_detection_after_downtime_opens_a_new_occurrence(self):
        self._fire(T0)
        alerts.acknowledge(self.key)
        self._expire(T0 + 8 * 3600)
        notified, count = self._fire(T0 + 8 * 3600 + 30)
        row = self._row()
        self.assertTrue(notified)
        self.assertEqual((count, row.first_fired_at), (1, _iso(T0 + 8 * 3600 + 30)))
        self.assertIsNone(row.acknowledged_at)

    def test_operator_close_starts_a_new_occurrence(self):
        self._fire(T0)
        alerts.close(self.key)
        _, count = self._fire(T0 + 30)
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
