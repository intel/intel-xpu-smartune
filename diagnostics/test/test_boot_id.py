# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Boot ids in journald's form (32 hex digits)."""

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics import system_info  # noqa: E402
from diagnostics.sources import journal  # noqa: E402
from diagnostics.sources.base import LogQueryFilter  # noqa: E402

DASHED = "ddd13f42-d69b-45df-847e-0f43688a536a"
HEX32 = "ddd13f42d69b45df847e0f43688a536a"
OTHER = "7f9951d6a01f4ce387ff2032b503d43d"


class ReadBootIdTest(unittest.TestCase):

    def test_proc_value_is_returned_as_32_hex_digits(self):
        with mock.patch("builtins.open", mock.mock_open(read_data=DASHED.upper() + "\n")):
            self.assertEqual(system_info.read_boot_id(), HEX32)

    def test_unreadable_gives_none(self):
        with mock.patch("builtins.open", side_effect=OSError):
            self.assertIsNone(system_info.read_boot_id())


class JournalBootTest(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.object(journal.shutil, "which", return_value="/usr/bin/journalctl")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = journal.JournalLogSource()

    def test_boot_filter_reaches_journalctl_and_matches_records(self):
        line = json.dumps({"__REALTIME_TIMESTAMP": "100000000", "MESSAGE": "oom", "_BOOT_ID": HEX32})
        done = SimpleNamespace(returncode=0, stdout=line + "\n", stderr="")
        with mock.patch.object(journal.subprocess, "run", return_value=done) as run:
            records = self.source.query(LogQueryFilter(start_time=1, end_time=200, boot_id=HEX32))
        self.assertIn(f"--boot={HEX32}", run.call_args.args[0])
        self.assertEqual([r.fields["boot_id"] for r in records], [HEX32])

    def test_other_boot_is_filtered_out(self):
        line = json.dumps({"__REALTIME_TIMESTAMP": "100000000", "MESSAGE": "oom", "_BOOT_ID": OTHER})
        done = SimpleNamespace(returncode=0, stdout=line + "\n", stderr="")
        with mock.patch.object(journal.subprocess, "run", return_value=done):
            self.assertEqual(self.source.query(LogQueryFilter(start_time=1, end_time=200, boot_id=HEX32)), [])

    def test_list_boots_matches_read_boot_id(self):
        listing = json.dumps([{"index": 0, "boot_id": HEX32, "first_entry": 1, "last_entry": 2}])
        done = SimpleNamespace(returncode=0, stdout=listing, stderr="")
        with mock.patch.object(journal.subprocess, "run", return_value=done), \
                mock.patch("builtins.open", mock.mock_open(read_data=DASHED + "\n")):
            self.assertEqual(self.source.list_boots()[0]["boot_id"], system_info.read_boot_id())


if __name__ == "__main__":
    unittest.main()
