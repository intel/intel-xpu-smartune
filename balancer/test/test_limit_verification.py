# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for cgroupfs-based limit verification (P5).

``systemctl set-property`` exiting 0 means systemd accepted the property, not
that the kernel file holds the value.  And ``systemctl show`` cannot be used to
check: it replays systemd's own record, which stays stale after anything writes
the cgroup file directly -- so a direct write can clear a limit while the
property still reports it, and nothing notices.  These tests pin the reader to
cgroupfs and to nothing else.

Run: PYTHONDONTWRITEBYTECODE=1 python3 balancer/test/test_limit_verification.py
"""

import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BALANCER_ROOT = os.path.join(_REPO_ROOT, "balancer")
for _p in (_REPO_ROOT, _BALANCER_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_logger_module = types.ModuleType("utils.logger")
_test_logger = logging.getLogger("test_limit_verification")
_logger_module.logger = _test_logger
_logger_module.get_logger = lambda name=None: _test_logger
_logger_module.current_log_context = lambda: {}
sys.modules["utils.logger"] = _logger_module


def _stub(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


_stub("psutil")
_stub("utils.app_utils", write_cgroup_file=lambda *a, **k: None)
_stub("config.config", b_config=types.SimpleNamespace(cgroup_mount="/sys/fs/cgroup"))

from controller.controller import Controller  # noqa: E402

PERIOD = 100000
UNIT = "app-demo.scope"


class VerificationTest(unittest.TestCase):
    CPUS = 8

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mount = Path(self._tmp.name)
        self.controller = Controller.__new__(Controller)
        self.controller.cgroup_mount = str(self.mount)
        self.controller.cpus = self.CPUS

    def make_cgroup(self, cpu_max="max 100000", memory_high="max",
                    unit=UNIT, under="system.slice"):
        path = self.mount / under / unit
        path.mkdir(parents=True, exist_ok=True)
        (path / "cpu.max").write_text(cpu_max + "\n")
        (path / "memory.high").write_text(str(memory_high) + "\n")
        return path

    # -- locating ----------------------------------------------------------

    def test_unit_is_found_wherever_it_sits(self):
        """A scope can be under system.slice or buried in the user session; the
        unit name alone never tells you which."""
        self.make_cgroup(under="user.slice/user-1000.slice/user@1000.service/app.slice")
        found = self.controller.find_cgroup_dir(UNIT)
        self.assertIsNotNone(found)
        self.assertTrue(found.endswith(UNIT))

    def test_missing_unit_is_none_not_a_failure(self):
        self.assertIsNone(self.controller.find_cgroup_dir("ghost.scope"))

    def test_path_traversal_in_the_unit_name_is_rejected(self):
        self.assertIsNone(self.controller.find_cgroup_dir("../../etc"))
        self.assertIsNone(self.controller.find_cgroup_dir("a/b"))

    # -- reading -----------------------------------------------------------

    def test_unlimited_reads_as_none_not_zero(self):
        self.make_cgroup()
        applied = self.controller.read_applied_limits(UNIT)
        self.assertIsNone(applied["cpu_quota_usec"])
        self.assertIsNone(applied["memory_high_bytes"])

    def test_values_are_read_verbatim(self):
        self.make_cgroup(cpu_max="320000 100000", memory_high=150 * 1024 * 1024)
        applied = self.controller.read_applied_limits(UNIT)
        self.assertEqual(applied["cpu_quota_usec"], 320000)
        self.assertEqual(applied["memory_high_bytes"], 150 * 1024 * 1024)

    def test_a_missing_cgroup_reads_as_unknown(self):
        self.assertIsNone(self.controller.read_applied_limits("ghost.scope"))

    # -- verifying ---------------------------------------------------------

    def verify(self, cpu=None, mem=None, is_restore=False):
        return self.controller.verify_applied_limits(
            UNIT, cpu, mem, is_restore, attempts=1, delay=0)

    def test_matching_limits_verify(self):
        # 40% of the machine on 8 CPUs = CPUQuota=320% = 320000 us per period.
        self.make_cgroup(cpu_max=f"{320000} {PERIOD}", memory_high=150 * 1024 * 1024)
        self.assertTrue(self.verify(cpu=40, mem=150)["ok"])

    def test_a_cleared_limit_that_is_still_in_force_is_caught(self):
        """The restore case.  A cap we believe we lifted but which is still in
        the file would throttle the app forever with nobody looking."""
        self.make_cgroup(cpu_max=f"{320000} {PERIOD}", memory_high=150 * 1024 * 1024)
        result = self.verify(is_restore=True)
        self.assertFalse(result["ok"])

    def test_a_limit_we_set_that_never_landed_is_caught(self):
        """The silent-drift case: something wrote the cgroup file directly, so
        systemd's property is stale and only cgroupfs shows the truth."""
        self.make_cgroup(cpu_max="max 100000", memory_high="max")
        result = self.verify(cpu=40, mem=150)
        self.assertFalse(result["ok"])
        self.assertEqual(result["expected_memory_high_bytes"], 150 * 1024 * 1024)

    def test_the_wrong_value_is_caught_not_just_a_missing_one(self):
        self.make_cgroup(cpu_max=f"{720000} {PERIOD}", memory_high=150 * 1024 * 1024)
        self.assertFalse(self.verify(cpu=40, mem=150)["ok"])

    def test_page_rounding_on_memory_high_is_tolerated(self):
        self.make_cgroup(cpu_max="max 100000",
                         memory_high=150 * 1024 * 1024 - 4096)
        self.assertTrue(self.verify(mem=150)["ok"])

    def test_a_non_default_cpu_period_is_handled(self):
        """cpu.max is a quota/period pair.  Comparing the raw quota against a
        hardcoded 100000 us period would misread any unit systemd gave a
        different period."""
        self.make_cgroup(cpu_max=f"{2000000} {500000}")     # 400% of one CPU
        self.assertTrue(self.verify(cpu=50)["ok"])          # 50% x 8 CPUs

    def test_restore_verifies_when_both_are_actually_cleared(self):
        self.make_cgroup()
        self.assertTrue(self.verify(is_restore=True)["ok"])

    def test_an_unfindable_cgroup_is_unknown_not_a_mismatch(self):
        """The app may have exited between the write and the check.  Reporting
        that as drift would cry wolf on every short-lived process."""
        self.assertIsNone(self.verify(cpu=40, mem=150))

    def test_verification_retries_while_systemd_catches_up(self):
        """set-property returns when systemd accepts the property, not when the
        kernel file has been written, so a first read can legitimately lose."""
        path = self.make_cgroup()
        reads = {"n": 0}
        original = self.controller.read_applied_limits

        def slow_apply(cgroup_id):
            reads["n"] += 1
            if reads["n"] >= 3:
                (path / "memory.high").write_text(str(150 * 1024 * 1024))
            return original(cgroup_id)

        self.controller.read_applied_limits = slow_apply
        result = self.controller.verify_applied_limits(
            UNIT, None, 150, False, attempts=5, delay=0)
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(reads["n"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
