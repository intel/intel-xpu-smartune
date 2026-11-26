# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""journalctl is asked for full-length fields, limited to the ones the parser reads."""

import inspect
import io
import json
import os
import re
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics.sources import journal  # noqa: E402
from diagnostics.sources.base import LogQueryFilter  # noqa: E402


def _output_fields(argv):
    value = next(arg for arg in argv if arg.startswith("--output-fields="))
    return set(value.split("=", 1)[1].split(","))


class JournalOutputTest(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.object(journal.shutil, "which", return_value="/usr/bin/journalctl")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = journal.JournalLogSource()

    def _query_argv(self):
        done = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(journal.subprocess, "run", return_value=done) as run:
            self.source.query(LogQueryFilter(start_time=1, end_time=200))
        return run.call_args.args[0]

    def _scan_argv(self):
        proc = mock.MagicMock(stdout=io.StringIO(""), stderr=io.StringIO(""), returncode=0)
        proc.wait.return_value = 0
        with mock.patch.object(journal.subprocess, "Popen", return_value=proc) as popen:
            self.source.scan_forward(since_epoch=1)
        return popen.call_args.args[0]

    def test_query_and_scan_request_full_length_fields(self):
        for argv in (self._query_argv(), self._scan_argv()):
            self.assertIn("--all", argv)
            self.assertIn("--output=json", argv)

    def test_stored_core_is_never_requested(self):
        for argv in (self._query_argv(), self._scan_argv()):
            fields = _output_fields(argv)
            self.assertNotIn("COREDUMP", fields)
            self.assertIn("COREDUMP_PID", fields)

    def test_every_field_the_parser_reads_is_requested(self):
        source = inspect.getsource(journal.JournalLogSource._record)
        read = set(re.findall(r'(?:_field_text\(\s*entry,\s*|entry\.get\()"([A-Z_][A-Z0-9_]*)"', source))
        read |= set(journal._SUBJECT_UNIT_FIELDS)
        always_printed = {name for name in read if name.startswith("__")}
        self.assertEqual(read - always_printed - _output_fields(self._query_argv()), set())

    def test_long_coredump_message_becomes_a_record(self):
        message = "Process 4211 (python3) of user 1000 dumped core.\n" + "#0 frame\n" * 1000
        line = json.dumps({"__REALTIME_TIMESTAMP": "100000000", "MESSAGE": message,
                           "SYSLOG_IDENTIFIER": "systemd-coredump", "COREDUMP_PID": "4211",
                           "COREDUMP_COMM": "python3"})
        done = SimpleNamespace(returncode=0, stdout=line + "\n", stderr="")
        with mock.patch.object(journal.subprocess, "run", return_value=done):
            records = self.source.query(LogQueryFilter(start_time=1, end_time=200))
        self.assertEqual(len(records), 1)
        self.assertGreater(len(records[0].message), 4096)


if __name__ == "__main__":
    unittest.main()
