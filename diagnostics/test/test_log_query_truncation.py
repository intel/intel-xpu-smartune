# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""/diag/logs says when sources had more records, and which ones could not be read."""

import json
import os
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics import log_query  # noqa: E402
from diagnostics.sources import journal  # noqa: E402
from diagnostics.sources.base import LogQueryFilter, LogRecord, LogSourceError  # noqa: E402


class _Source:
    """Returns the newest ``flt.limit`` of ``total`` records, like the real sources."""

    def __init__(self, name, total=0, error=None):
        self.name, self.total, self.error = name, total, error

    def available(self):
        return True

    def query(self, flt):
        if self.error:
            raise self.error
        records = [LogRecord(ts_epoch=100 + i, ts_iso="", level="ERROR", source=self.name,
                             logger="x", message=f"line {i}") for i in range(self.total)]
        return records[-flt.limit:]


def _run(*sources, limit=5):
    with mock.patch.object(log_query, "iter_sources", return_value=list(sources)):
        return log_query.query(limit=limit)


class LogQueryTruncationTest(unittest.TestCase):

    def test_single_source_with_more_matches_is_truncated(self):
        result = _run(_Source("smartune", total=50))
        self.assertEqual((result["count"], result["truncated"]), (5, True))

    def test_source_with_exactly_the_limit_is_not_truncated(self):
        result = _run(_Source("smartune", total=5))
        self.assertEqual((result["count"], result["truncated"]), (5, False))

    def test_unreadable_source_is_reported_not_silent(self):
        result = _run(_Source("smartune", total=2),
                      _Source("journal", error=LogSourceError("journalctl did not answer")))
        self.assertEqual((result["sources"], result["failed_sources"]), (["smartune"], ["journal"]))


class JournalReadFailureTest(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.object(journal.shutil, "which", return_value="/usr/bin/journalctl")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = journal.JournalLogSource()

    def test_timeout_raises_source_error(self):
        timeout = subprocess.TimeoutExpired(cmd="journalctl", timeout=10)
        with mock.patch.object(journal.subprocess, "run", side_effect=timeout):
            with self.assertRaises(LogSourceError):
                self.source.query(LogQueryFilter(start_time=1, end_time=200))

    def test_nonzero_exit_raises_source_error(self):
        done = SimpleNamespace(returncode=1, stdout="", stderr="boom")
        with mock.patch.object(journal.subprocess, "run", return_value=done):
            with self.assertRaises(LogSourceError):
                self.source.query(LogQueryFilter(start_time=1, end_time=200))

    def test_kernel_view_that_filled_its_read_cap_is_truncated(self):
        flt = LogQueryFilter(start_time=1, end_time=10_000, kernel_only=True, limit=10)
        lines = "\n".join(json.dumps({"__REALTIME_TIMESTAMP": str((i + 1) * 1_000_000),
                                      "MESSAGE": f"usb {i}: noise", "_TRANSPORT": "kernel",
                                      "PRIORITY": "6"}) for i in range(1000))
        done = SimpleNamespace(returncode=0, stdout=lines + "\n", stderr="")
        with mock.patch.object(journal.subprocess, "run", return_value=done):
            records = self.source.query(flt)
        self.assertTrue(records.truncated)

    def test_kernel_view_with_room_left_is_not_truncated(self):
        flt = LogQueryFilter(start_time=1, end_time=10_000, kernel_only=True, limit=10)
        line = json.dumps({"__REALTIME_TIMESTAMP": "5000000", "MESSAGE": "usb 1: noise",
                           "_TRANSPORT": "kernel", "PRIORITY": "6"})
        done = SimpleNamespace(returncode=0, stdout=line + "\n", stderr="")
        with mock.patch.object(journal.subprocess, "run", return_value=done):
            records = self.source.query(flt)
        self.assertFalse(records.truncated)


if __name__ == "__main__":
    unittest.main()
