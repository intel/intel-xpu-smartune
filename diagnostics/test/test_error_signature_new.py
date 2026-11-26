# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Error signatures: templates, NEW reports, the learning period, the hourly
cap and replayed records."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics import error_signatures  # noqa: E402
from diagnostics.error_signatures import NEW, SignatureTracker, normalize  # noqa: E402

T0 = 1_000_000.0


class _MemoryStore:
    def __init__(self):
        self.learning = None

    def load_recent(self, _limit):
        return []

    def load_learning(self):
        return self.learning

    def save_learning(self, state):
        self.learning = dict(state)

    def lookup(self, _fingerprint):
        return None

    def upsert_many(self, _rows):
        return True

    def prune(self, _before):
        return None


def _tracker(learning_seconds=0):
    return SignatureTracker(learning_seconds=learning_seconds, store=_MemoryStore(),
                            clock=lambda: T0)


def _new(tracker, message, ts, key=None):
    findings = tracker.observe(source="smartune", origin="worker", message=message,
                               ts_epoch=ts, key=key)
    return [f for f in findings if f.event_type == NEW]


class NormalizeTest(unittest.TestCase):

    def test_variable_parts_become_placeholders(self):
        self.assertEqual(normalize("open /var/lib/app/db.sqlite failed after 3 tries"),
                         "open <PATH> failed after <N> tries")
        self.assertEqual(normalize("worker pid12345 died"), "worker pid<N> died")

    def test_short_numbers_in_names_stay(self):
        self.assertEqual(normalize("sqlite3 error on nvme0n1"), "sqlite3 error on nvme0n1")

    def test_traceback_keeps_the_exception_line(self):
        message = ("Unhandled error\nTraceback (most recent call last):\n"
                   "  File \"x.py\", line 1\nValueError: bad value 42")
        self.assertEqual(normalize(message), "Unhandled error | ValueError: bad value <N>")


class NewSignatureTest(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.object(error_signatures, "_current_boot", return_value="boot1")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_new_template_is_reported_once(self):
        tracker = _tracker()
        self.assertEqual(len(_new(tracker, "disk 1 failed", T0)), 1)
        self.assertEqual(_new(tracker, "disk 2 failed", T0 + 1), [])

    def test_errors_seen_while_learning_are_not_reported(self):
        tracker = _tracker(learning_seconds=3600)
        self.assertEqual(_new(tracker, "known error", T0 + 10), [])
        self.assertEqual(_new(tracker, "known error", T0 + 7200), [])
        self.assertEqual(len(_new(tracker, "fresh error", T0 + 7200)), 1)

    def test_hourly_cap_holds_back_and_counts_the_rest(self):
        tracker = _tracker()
        reported = sum(len(_new(tracker, f"error kind {chr(97 + i)}", T0 + i)) for i in range(11))
        self.assertEqual(reported, error_signatures._NEW_PER_HOUR)
        later = _new(tracker, "error kind z", T0 + 3700)
        self.assertEqual(later[0].attributes["new_signatures_suppressed"], 1)
        # The held-back template was never announced: it is reported when it recurs.
        self.assertEqual(len(_new(tracker, "error kind k", T0 + 3701)), 1)

    def test_replayed_record_is_not_counted_twice(self):
        tracker = _tracker()
        first = _new(tracker, "boom", T0, key="rec-1")
        again = _new(tracker, "boom", T0, key="rec-1")
        self.assertEqual((len(first), len(again)), (1, 1))
        fp = first[0].fingerprint
        self.assertEqual(tracker._sigs[fp].total, 1)


if __name__ == "__main__":
    unittest.main()
