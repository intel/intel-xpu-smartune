# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for per-cgroup core affinity (controller/cpu.py, cgroup v2 cpuset).

A fake cgroupfs in a temp dir stands in for the kernel: the stubbed
``write_cgroup_file`` mirrors what cgroupfs does (``+cpuset`` joins
``subtree_control``; ``cpuset.cpus`` is reflected in ``cpuset.cpus.effective``).

Run:  python3 balancer/test/test_cpu_affinity.py
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
_test_logger = logging.getLogger("test_cpu_affinity")
_logger_module.logger = _test_logger
_logger_module.get_logger = lambda name=None: _test_logger
sys.modules["utils.logger"] = _logger_module

WRITES = []
ALL = "0-5"


def _fake_write(content, target):
    WRITES.append((target, content))
    directory, name = os.path.split(target)
    if name == "cgroup.subtree_control":
        with open(target) as f:
            current = f.read().split()
        with open(target, "w") as f:
            f.write(" ".join(current + ["cpuset"]) + "\n")
    elif name == "cpuset.cpus":
        with open(target, "w") as f:
            f.write(content + "\n")
        with open(os.path.join(directory, "cpuset.cpus.effective"), "w") as f:
            f.write((content or ALL) + "\n")
    else:
        raise OSError(13, "Permission denied")


_app_utils = types.ModuleType("utils.app_utils")
_app_utils.write_cgroup_file = _fake_write
sys.modules["utils.app_utils"] = _app_utils

from controller.cpu import (  # noqa: E402
    CPUController, core_class_options, format_cpu_list, parse_cpu_list, resolve_cpus)

GROUPS = {"p_cores": [0, 1], "e_cores": [], "lpe_cores": [2, 3, 4, 5]}
ONLINE = set(range(6))


def _w(path, text):
    with open(path, "w") as f:
        f.write(text)


class ParsingTest(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(parse_cpu_list("0-1,4"), {0, 1, 4})
        self.assertEqual(format_cpu_list({4, 0, 1}), "0-1,4")
        self.assertEqual(parse_cpu_list(""), set())

    def test_malformed(self):
        for bad in ("a", "3-1", "1-"):
            with self.assertRaises(ValueError):
                parse_cpu_list(bad)


class ResolveTest(unittest.TestCase):
    def test_classes(self):
        self.assertEqual(resolve_cpus("p", None, GROUPS, ONLINE)[0], "0-1")
        self.assertEqual(resolve_cpus("LPE", None, GROUPS, ONLINE)[0], "2-5")

    def test_empty_class_refused(self):
        value, msg = resolve_cpus("e", None, GROUPS, ONLINE)
        self.assertIsNone(value)
        self.assertIn("no E cores", msg)
        self.assertEqual(set(core_class_options(GROUPS)), {"p", "lpe"})

    def test_custom(self):
        self.assertEqual(resolve_cpus("custom", "1,3-4", GROUPS, ONLINE)[0], "1,3-4")
        self.assertIsNone(resolve_cpus("custom", "9", GROUPS, ONLINE)[0])
        self.assertIsNone(resolve_cpus("custom", "", GROUPS, ONLINE)[0])
        self.assertIsNone(resolve_cpus("bogus", None, GROUPS, ONLINE)[0])


class ControllerTest(unittest.TestCase):
    def setUp(self):
        WRITES.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.ctl = CPUController(self.root)
        # root already passes cpuset down; user.slice and app.slice do not.
        self.user = os.path.join(self.root, "user.slice")
        self.app_slice = os.path.join(self.user, "app.slice")
        self.scope = os.path.join(self.app_slice, "app-demo.scope")
        os.makedirs(self.scope)
        _w(os.path.join(self.root, "cgroup.subtree_control"), "cpuset cpu io\n")
        _w(os.path.join(self.user, "cgroup.subtree_control"), "cpu io\n")
        _w(os.path.join(self.app_slice, "cgroup.subtree_control"), "cpu io\n")
        _w(os.path.join(self.scope, "cpuset.cpus"), "\n")
        _w(os.path.join(self.scope, "cpuset.cpus.effective"), ALL + "\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _cpus(self):
        with open(os.path.join(self.scope, "cpuset.cpus")) as f:
            return f.read().strip()

    def test_enable_skips_levels_that_already_pass_cpuset(self):
        self.assertTrue(self.ctl.enable_for(self.scope))
        targets = [os.path.dirname(t) for t, _ in WRITES]
        self.assertEqual(targets, [self.user, self.app_slice])   # root untouched
        WRITES.clear()
        self.assertTrue(self.ctl.enable_for(self.scope))
        self.assertEqual(WRITES, [])                             # idempotent

    def test_set_then_switch_then_restore(self):
        ok, msg = self.ctl.set_cpus(self.scope, "0-1")
        self.assertTrue(ok, msg)
        self.assertEqual(self.ctl.read_cpus(self.scope), "0-1")
        ok, msg = self.ctl.set_cpus(self.scope, "2-5")
        self.assertTrue(ok, msg)
        self.assertTrue(self.ctl.is_bound(self.scope))
        ok, msg = self.ctl.restore(self.scope)
        self.assertTrue(ok, msg)
        self.assertEqual(self._cpus(), "")            # original (inherit), not "0-1"
        self.assertFalse(self.ctl.is_bound(self.scope))

    def test_restore_when_never_bound_is_a_noop(self):
        self.assertEqual(self.ctl.restore(self.scope), (True, "not bound"))
        self.assertEqual(WRITES, [])

    def test_missing_cpuset_file_is_refused(self):
        os.remove(os.path.join(self.scope, "cpuset.cpus"))
        ok, msg = self.ctl.set_cpus(self.scope, "0-1")
        self.assertFalse(ok)
        self.assertFalse(self.ctl.is_bound(self.scope))

    def test_effective_mismatch_reports_failure_but_stays_restorable(self):
        _w(os.path.join(self.scope, "cpuset.cpus.effective"), ALL + "\n")
        import controller.cpu as mod
        original = mod.write_cgroup_file
        def stuck(content, target):               # cpuset.cpus written, effective not updated
            if target.endswith("cpuset.cpus"):
                _w(target, content + "\n")
            else:
                original(content, target)
        mod.write_cgroup_file = stuck
        try:
            ok, msg = self.ctl.set_cpus(self.scope, "0-1")
        finally:
            mod.write_cgroup_file = original
        self.assertFalse(ok)
        self.assertIn("effective", msg)
        self.assertTrue(self.ctl.is_bound(self.scope))
        self.assertTrue(self.ctl.restore(self.scope)[0])

    def test_gone_cgroup(self):
        self.ctl.set_cpus(self.scope, "0-1")
        import shutil
        shutil.rmtree(self.scope)
        self.assertEqual(self.ctl.restore(self.scope), (True, "cgroup already gone"))
        self.assertFalse(self.ctl.is_bound(self.scope))
        self.assertFalse(self.ctl.set_cpus(self.scope, "0-1")[0])

    def test_last_choice_is_per_app_and_shared_by_every_caller(self):
        self.assertIsNone(self.ctl.last_choice("app1"))
        self.ctl.remember_choice("app1", "lpe", "2-5")
        self.ctl.remember_choice("app2", "custom", "0,3")
        self.ctl.remember_choice("app1", "p", "0-1")          # a later success replaces the earlier one
        self.assertEqual(self.ctl.last_choice("app1"), {"class": "p", "cpus": "0-1"})
        self.assertEqual(self.ctl.last_choice("app2"), {"class": "custom", "cpus": "0,3"})
        self.ctl.last_choice("app1")["class"] = "x"            # callers get a copy
        self.assertEqual(self.ctl.last_choice("app1")["class"], "p")

    def _with_populated_kernel(self, errno_code=28):
        """Make writing "" to cpuset.cpus fail like a pre-7.0 kernel on a populated cgroup."""
        import controller.cpu as mod
        original = mod.write_cgroup_file
        def refuse_empty(content, target):
            if target.endswith("cpuset.cpus") and not content:
                raise OSError(errno_code, os.strerror(errno_code))
            original(content, target)
        mod.write_cgroup_file = refuse_empty
        self.addCleanup(setattr, mod, "write_cgroup_file", original)

    def test_restore_falls_back_to_parent_effective_on_enospc(self):
        _w(os.path.join(self.app_slice, "cpuset.cpus.effective"), "0-3\n")
        self.ctl.set_cpus(self.scope, "0-1")
        self._with_populated_kernel()
        ok, msg = self.ctl.restore(self.scope)
        self.assertTrue(ok, msg)
        self.assertEqual(self._cpus(), "0-3")
        self.assertFalse(self.ctl.is_bound(self.scope))

    def test_restore_reports_enospc_when_parent_effective_unreadable(self):
        self.ctl.set_cpus(self.scope, "0-1")
        self._with_populated_kernel()
        ok, msg = self.ctl.restore(self.scope)
        self.assertFalse(ok)
        self.assertIn("No space left", msg)
        self.assertTrue(self.ctl.is_bound(self.scope))      # kept for a later retry

    def test_restore_does_not_fall_back_on_other_errors(self):
        _w(os.path.join(self.app_slice, "cpuset.cpus.effective"), "0-3\n")
        self.ctl.set_cpus(self.scope, "0-1")
        self._with_populated_kernel(errno_code=13)
        self.assertFalse(self.ctl.restore(self.scope)[0])
        self.assertEqual(self._cpus(), "0-1")

    def test_restore_all_survives_enospc(self):
        _w(os.path.join(self.app_slice, "cpuset.cpus.effective"), ALL + "\n")
        self.ctl.set_cpus(self.scope, "2-5")
        self._with_populated_kernel()
        self.assertEqual(self.ctl.restore_all(), 0)
        self.assertEqual(self.ctl.bound_cgroups(), [])

    def test_restore_all(self):
        self.ctl.set_cpus(self.scope, "2-5")
        self.assertEqual(self.ctl.restore_all(), 0)
        self.assertEqual(self.ctl.bound_cgroups(), [])


if __name__ == "__main__":
    unittest.main()
