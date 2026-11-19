# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the self-cgroup guard (utils/self_guard.py).

Run:  python3 balancer/test/test_self_guard.py
"""

import logging
import os
import sys
import tempfile
import types
import unittest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "balancer")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_logger_module = types.ModuleType("utils.logger")
_test_logger = logging.getLogger("test_self_guard")
_logger_module.logger = _test_logger
_logger_module.get_logger = lambda name=None: _test_logger
sys.modules["utils.logger"] = _logger_module

_app_utils = types.ModuleType("utils.app_utils")
_app_utils.write_cgroup_file = lambda content, target: (_ for _ in ()).throw(AssertionError("must not write"))
sys.modules["utils.app_utils"] = _app_utils

from utils import self_guard  # noqa: E402
from controller.cpu import CPUController  # noqa: E402

OWN = ["user.slice", "user-1000.slice", "user@1000.service", "app.slice",
       "app-org.gnome.Terminal.slice", "vte-spawn-abc.scope"]


class GuardTest(unittest.TestCase):
    def setUp(self):
        self_guard.set_own_units_for_test(OWN)
        self.addCleanup(self_guard.set_own_units_for_test, None)

    def test_own_cgroup_is_protected(self):
        self.assertTrue(self_guard.is_protected("vte-spawn-abc.scope"))

    def test_every_ancestor_is_protected(self):
        for name in OWN[:-1]:
            self.assertTrue(self_guard.is_protected(name), name)

    def test_siblings_and_unrelated_are_not(self):
        for name in ("vte-spawn-other.scope", "app-org.gnome.Nautilus.slice",
                     "system.slice", "smartune-bench-run-1.scope", "tmux-spawn-1.scope"):
            self.assertFalse(self_guard.is_protected(name), name)

    def test_descendants_are_not(self):
        self_guard.set_own_units_for_test(["user.slice", "app.slice"])
        self.assertFalse(self_guard.is_protected("child.scope"))

    def test_paths_use_their_last_component(self):
        self.assertTrue(self_guard.is_protected("/sys/fs/cgroup/user.slice/x/vte-spawn-abc.scope/"))
        self.assertFalse(self_guard.is_protected("/sys/fs/cgroup/user.slice/x/vte-spawn-other.scope"))

    def test_empty_and_none_are_not_protected(self):
        for value in ("", None, "/"):
            self.assertFalse(self_guard.is_protected(value))

    def test_root_cgroup_protects_nothing(self):
        self_guard.set_own_units_for_test([])
        self.assertFalse(self_guard.is_protected("user.slice"))

    def test_protected_among_keeps_order_and_drops_repeats(self):
        got = self_guard.protected_among(
            ["x.scope", "vte-spawn-abc.scope", "app.slice", "vte-spawn-abc.scope"])
        self.assertEqual(got, ["vte-spawn-abc.scope", "app.slice"])

    def test_refusal_names_the_cgroup(self):
        text = self_guard.refusal(["vte-spawn-abc.scope"])
        self.assertIn("vte-spawn-abc.scope", text)
        self.assertIn("not allowed", text)

    def test_reads_the_real_own_cgroup(self):
        self_guard.set_own_units_for_test(None)
        expected = self_guard._read_cgroup_path(os.getpid())
        units = self_guard.own_units(refresh=True)
        self.assertEqual("/" + "/".join(units) if units else "", (expected or "").rstrip("/"))


class CpuAffinityRefusesSelfTest(unittest.TestCase):
    def test_set_cpus_refuses_the_services_own_cgroup(self):
        self_guard.set_own_units_for_test(OWN)
        self.addCleanup(self_guard.set_own_units_for_test, None)
        with tempfile.TemporaryDirectory() as tmp:
            own_dir = os.path.join(tmp, "vte-spawn-abc.scope")
            os.makedirs(own_dir)
            ok, msg = CPUController(tmp).set_cpus(own_dir, "0-1")
        self.assertFalse(ok)
        self.assertIn("not allowed", msg)

    def test_other_cgroup_is_not_refused_by_the_guard(self):
        self_guard.set_own_units_for_test(OWN)
        self.addCleanup(self_guard.set_own_units_for_test, None)
        with tempfile.TemporaryDirectory() as tmp:
            other = os.path.join(tmp, "vte-spawn-other.scope")
            os.makedirs(other)
            ok, msg = CPUController(tmp).set_cpus(other, "0-1")
        self.assertNotIn("not allowed", msg)   # fails later (no cpuset in a temp dir), not on the guard


if __name__ == "__main__":
    unittest.main()
