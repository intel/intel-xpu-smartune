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
from unittest import mock
from pathlib import Path

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_logger_module = types.ModuleType("utils.logger")
_test_logger = logging.getLogger("test_cgroup_file_write")
_logger_module.logger = _test_logger
# app_utils now acquires its logger via get_logger(__name__); the fake module
# must provide it too, or importing app_utils below fails.
_logger_module.get_logger = lambda name=None: _test_logger
_logger_module.current_log_context = lambda: {}
sys.modules["utils.logger"] = _logger_module

from utils import app_utils  # noqa: E402
from utils.app_utils import get_pids_in_cgroup, write_cgroup_file  # noqa: E402


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

    def test_get_pids_in_cgroup_resolves_scope_basename_from_cgroupfs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = root / "user.slice" / "session-7782.scope"
            scope.mkdir(parents=True)
            (scope / "cgroup.procs").write_text("101\n202\n", encoding="utf-8")

            completed = types.SimpleNamespace(stdout="", stderr="invalid", returncode=1)
            process = mock.Mock()
            process.cmdline.return_value = ["python", "optimum-cli"]
            with mock.patch.object(app_utils.b_config, "cgroup_mount", str(root), create=True), \
                    mock.patch.object(app_utils.subprocess, "run", return_value=completed), \
                    mock.patch.object(app_utils.psutil, "Process", return_value=process):
                self.assertEqual(get_pids_in_cgroup("session-7782.scope"), [101, 202])


if __name__ == "__main__":
    unittest.main()