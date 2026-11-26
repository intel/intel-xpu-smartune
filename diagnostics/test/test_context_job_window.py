# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""The evidence window of a benchmark job covers the whole run, also while it runs."""

import os
import sys
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics import context, event_store  # noqa: E402

NOW = int(time.time())


def _event(event_type, epoch, **attributes):
    return {"event_type": event_type, "job_id": "j1", "attributes": attributes,
            "ts_utc": datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()}


def _window(rows, start_time=None, end_time=None):
    with mock.patch.object(event_store, "query_events", return_value=rows):
        return context._resolve_window("job_id", "j1", start_time, end_time)


class JobWindowTest(unittest.TestCase):

    def test_running_job_window_ends_now(self):
        started = NOW - 1200
        start, end = _window([_event("WORKLOAD_BENCHMARK_STARTED", started,
                                     status="running", started_at=started)])
        self.assertEqual(start, started - 60)
        self.assertGreaterEqual(end, NOW)

    def test_finished_job_window_ends_after_it_finished(self):
        started, finished = NOW - 3600, NOW - 600
        rows = [_event("WORKLOAD_BENCHMARK_DONE", finished, status="done",
                       started_at=started, finished_at=finished),
                _event("WORKLOAD_BENCHMARK_STARTED", started, status="running", started_at=started)]
        self.assertEqual(_window(rows), (started - 60, finished + 60))

    def test_finished_job_without_started_event_uses_its_started_at(self):
        started, finished = NOW - 3600, NOW - 600
        rows = [_event("WORKLOAD_BENCHMARK_DONE", finished, status="done",
                       started_at=started, finished_at=finished)]
        self.assertEqual(_window(rows), (started - 60, finished + 60))

    def test_orphaned_job_window_is_bounded(self):
        started = NOW - 3 * 86400
        start, end = _window([_event("WORKLOAD_BENCHMARK_STARTED", started,
                                     status="running", started_at=started)])
        self.assertEqual(end - (start + 60), 24 * 3600)

    def test_explicit_bounds_win(self):
        rows = [_event("WORKLOAD_BENCHMARK_STARTED", NOW - 100, status="running", started_at=NOW - 100)]
        self.assertEqual(_window(rows, start_time=10, end_time=20), (10, 20))


if __name__ == "__main__":
    unittest.main()
