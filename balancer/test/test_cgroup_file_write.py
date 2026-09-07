# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for shell-safe cgroup control-file writes.

Run: PYTHONDONTWRITEBYTECODE=1 python3 balancer/test/test_cgroup_file_write.py
"""

import os
import sys
import tempfile
import unittest
import logging
import types
from pathlib import Path

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_logger_module = types.ModuleType("utils.logger")
_logger_module.logger = logging.getLogger("test_cgroup_file_write")
sys.modules["utils.logger"] = _logger_module

from utils.app_utils import write_cgroup_file  # noqa: E402


class WriteCgroupFileTests(unittest.TestCase):
    def test_shell_metacharacters_in_target_path_are_not_executed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executed_marker = root / "command-executed"
            target_file = root / "x;touch command-executed;$(id)#" / "io.max"
            target_file.parent.mkdir()

            write_cgroup_file("8:0 wbps=500000", str(target_file), allowed_roots=(str(root),))

            self.assertEqual(target_file.read_text(encoding="utf-8"), "8:0 wbps=500000\n")
            self.assertFalse(executed_marker.exists())

    def test_write_outside_allowed_roots_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            allowed = root / "cgroup"
            allowed.mkdir()
            outside = root / "secret"
            outside.write_text("original\n", encoding="utf-8")

            with self.assertRaises(PermissionError):
                write_cgroup_file("pwned", str(outside), allowed_roots=(str(allowed),))

            # The refused write must leave the target untouched.
            self.assertEqual(outside.read_text(encoding="utf-8"), "original\n")

    def test_path_traversal_out_of_allowed_root_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            allowed = root / "cgroup"
            allowed.mkdir()
            outside = root / "escape"
            outside.write_text("original\n", encoding="utf-8")

            traversal = str(allowed / ".." / "escape")
            with self.assertRaises(PermissionError):
                write_cgroup_file("pwned", traversal, allowed_roots=(str(allowed),))

            self.assertEqual(outside.read_text(encoding="utf-8"), "original\n")


if __name__ == "__main__":
    unittest.main()