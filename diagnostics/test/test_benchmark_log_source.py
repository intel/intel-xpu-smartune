# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""A job's run log reaches its context: last lines kept, not crowded out."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics import log_query  # noqa: E402
from diagnostics.sources import benchmark  # noqa: E402
from diagnostics.sources.base import LogQueryFilter, LogRecord  # noqa: E402


def _record(source, ts, line=None):
    return LogRecord(ts_epoch=ts, ts_iso="", level="INFO", source=source, logger=source,
                     message=f"{source}@{ts}", fields={} if line is None else {"line": line})


class BenchmarkSourceTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        path = os.path.join(self._dir.name, "run_r1.log")
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(f"step {i}\n" for i in range(500))
            handle.write("Traceback (most recent call last):\n")
            handle.write("RuntimeError: device lost\n")
        os.utime(path, (2_000_000, 2_000_000))
        patches = [mock.patch.object(benchmark, "_runs_dir", return_value=self._dir.name),
                   mock.patch("diagnostics.metrics.run_name_for_job", return_value="run_r1")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self._dir.cleanup()

    def test_job_query_keeps_the_end_of_the_log(self):
        records = benchmark.BenchmarkLogSource().query(LogQueryFilter(job_id="j", limit=50))
        self.assertEqual(len(records), 50)
        self.assertEqual(records[-1].message, "RuntimeError: device lost")

    def test_job_query_ignores_a_window_the_mtime_falls_outside(self):
        flt = LogQueryFilter(job_id="j", start_time=1_000_000, end_time=1_000_100, limit=10)
        self.assertEqual(len(benchmark.BenchmarkLogSource().query(flt)), 10)


class CapFairlyTest(unittest.TestCase):

    def test_newer_source_cannot_crowd_out_another(self):
        bench = [_record("benchmark", 2_000.0, line=i) for i in range(200)]
        smartune = [_record("smartune", 1_000.0 + i) for i in range(50)]
        kept = log_query._cap_fairly([smartune, bench], 100)
        self.assertEqual(len(kept), 100)
        self.assertEqual(sum(1 for r in kept if r.source == "smartune"), 50)
        self.assertEqual(max(r.fields["line"] for r in kept if r.source == "benchmark"), 199)

    def test_under_the_limit_everything_is_kept(self):
        kept = log_query._cap_fairly([[_record("a", 1.0)], [_record("b", 2.0)]], 10)
        self.assertEqual(len(kept), 2)


if __name__ == "__main__":
    unittest.main()
