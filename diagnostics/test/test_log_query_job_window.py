# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""A job-scoped log query reads the job's time range from its events."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics import context, log_query  # noqa: E402


class _Source:
    name = "journal"

    def __init__(self):
        self.filters = []

    def available(self):
        return True

    def query(self, flt):
        self.filters.append(flt)
        return []


class JobLogWindowTest(unittest.TestCase):

    def _query(self, window, **kwargs):
        src = _Source()
        with mock.patch.object(log_query, "get_source", return_value=src), \
                mock.patch.object(context, "job_window", return_value=window):
            log_query.query(sources=["journal"], job_id="j1", **kwargs)
        flt = src.filters[0]
        return flt.start_time, flt.end_time

    def test_missing_bounds_come_from_the_job(self):
        self.assertEqual(self._query((100, 200)), (100, 200))

    def test_explicit_bound_wins(self):
        self.assertEqual(self._query((100, 200), start_time=150), (150, 200))

    def test_job_without_events_keeps_the_default(self):
        self.assertEqual(self._query(None), (None, None))


if __name__ == "__main__":
    unittest.main()
