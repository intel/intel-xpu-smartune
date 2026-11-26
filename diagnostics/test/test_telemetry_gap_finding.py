# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""A telemetry gap is advice only when SmarTune ran through it."""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics import insights, service_lifecycle  # noqa: E402

T0 = 1_900_000_000


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _context(sample_times, lifecycle=()):
    return {
        "window": {"from": sample_times[0], "to": sample_times[-1]},
        "metrics": {"monitor": {"series": [{"ts_epoch": ts, "data": {}} for ts in sample_times],
                                "step_seconds": 0}},
        "service_lifecycle": [{"event_id": f"e{i}", "event_type": event_type, "ts_utc": _iso(ts)}
                              for i, (event_type, ts) in enumerate(lifecycle)],
    }


def _evaluate(context):
    return insights.IncompleteDiagnosticsWindow().evaluate(context)


class TelemetryGapFindingTest(unittest.TestCase):

    def test_gap_from_a_clean_stop_gives_no_advice(self):
        context = _context([T0, T0 + 5, T0 + 3600, T0 + 3605],
                           [(service_lifecycle.STOPPED, T0 + 10), (service_lifecycle.STARTED, T0 + 3590)])
        self.assertIsNone(_evaluate(context))

    def test_gap_from_a_crash_gives_no_advice(self):
        context = _context([T0, T0 + 3600], [(service_lifecycle.CRASHED, T0 + 3590)])
        self.assertIsNone(_evaluate(context))

    def test_gap_the_service_ran_through_is_a_warning(self):
        finding = _evaluate(_context([T0, T0 + 5, T0 + 3600]))
        self.assertEqual((finding.severity, finding.id), ("warning", "incomplete_diagnostics_window"))

    def test_mixed_gaps_report_the_unexplained_one(self):
        context = _context([T0, T0 + 600, T0 + 630, T0 + 660, T0 + 4300],
                           [(service_lifecycle.STOPPED, T0 + 670), (service_lifecycle.STARTED, T0 + 4290)])
        finding = _evaluate(context)
        self.assertIn("1 unexplained", finding.observation)
        self.assertIn("1 SmarTune was stopped", finding.observation)


if __name__ == "__main__":
    unittest.main()
