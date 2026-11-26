# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Error-signature bursts are reported for as long as they last."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics.error_signatures import BURST, SignatureTracker  # noqa: E402

T0 = 1_000_000.0


class _MemoryStore:
    def lookup(self, _fingerprint):
        return None

    def upsert_many(self, _rows):
        return True

    def prune(self, _before):
        return None


def _burst_minutes(interval_seconds, minutes, stop_after_minutes=None):
    tracker = SignatureTracker(learning_seconds=0, store=_MemoryStore(), clock=lambda: T0)
    tracker._loaded = True  # nothing persisted, no learning period
    tracker._learning_until = 0.0
    fired = []
    ts = T0
    while ts < T0 + minutes * 60:
        if stop_after_minutes is None or ts < T0 + stop_after_minutes * 60:
            findings = tracker.observe(source="smartune", origin="worker",
                                       message="connection refused", ts_epoch=ts)
            fired.extend(round((ts - T0) / 60, 1) for f in findings if f.event_type == BURST)
        ts += interval_seconds
    return fired


class BurstTest(unittest.TestCase):

    def test_sustained_storm_keeps_heartbeating(self):
        fired = _burst_minutes(5, 60)
        self.assertGreaterEqual(len(fired), 11)
        gaps = [b - a for a, b in zip(fired, fired[1:])]
        self.assertTrue(all(4.9 <= gap <= 5.1 for gap in gaps), gaps)

    def test_storm_that_stops_is_no_longer_reported(self):
        fired = _burst_minutes(5, 60, stop_after_minutes=10)
        self.assertTrue(fired)
        self.assertTrue(all(minute < 10 for minute in fired), fired)

    def test_steady_rate_is_not_a_burst(self):
        self.assertEqual(_burst_minutes(60, 120), [])


if __name__ == "__main__":
    unittest.main()
