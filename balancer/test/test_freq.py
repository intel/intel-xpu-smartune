# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for device frequency limits (controller/freq.py).

A fake sysfs in a temp dir stands in for cpufreq and the Xe / i915 drivers.

Run:  python3 balancer/test/test_freq.py
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
_test_logger = logging.getLogger("test_freq")
_logger_module.logger = _test_logger
_logger_module.get_logger = lambda name=None: _test_logger
sys.modules["utils.logger"] = _logger_module

from controller.freq import FreqController  # noqa: E402

GROUPS = {"p_cores": [0, 1], "e_cores": [], "lpe_cores": [2, 3]}


def _w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(text) + "\n")


def _r(path):
    with open(path) as f:
        return int(f.read())


class Sysfs:
    """cpu0/cpu1 are P cores with different tops; policy2 is shared by cpu2-3 (LPE)."""

    def __init__(self, root):
        self.cpu = os.path.join(root, "cpu")
        self.drm = os.path.join(root, "drm")
        for name, cpus, top in (("policy0", "0", 4600000), ("policy1", "1", 4800000),
                                ("policy2", "2 3", 2400000)):
            base = os.path.join(self.cpu, "cpufreq", name)
            _w(os.path.join(base, "related_cpus"), cpus)
            _w(os.path.join(base, "cpuinfo_min_freq"), 400000)
            _w(os.path.join(base, "cpuinfo_max_freq"), top)
            _w(os.path.join(base, "scaling_min_freq"), 400000)
            _w(os.path.join(base, "scaling_max_freq"), top)
            _w(os.path.join(base, "scaling_cur_freq"), 1200000)
            _w(os.path.join(base, "scaling_driver"), "intel_pstate")
        for gt in ("gt0", "gt1"):
            base = os.path.join(self.drm, "card0", "device", "tile0", gt, "freq0")
            _w(os.path.join(base, "rpn_freq"), 300)
            _w(os.path.join(base, "rp0_freq"), 2200)
            _w(os.path.join(base, "min_freq"), 300)
            _w(os.path.join(base, "max_freq"), 2200)
            _w(os.path.join(base, "cur_freq"), 900)
        # connector entries must not be taken for cards
        os.makedirs(os.path.join(self.drm, "card0-DP-1"))

    def cpu_file(self, policy, name):
        return os.path.join(self.cpu, "cpufreq", policy, name)

    def gpu_file(self, gt, name):
        return os.path.join(self.drm, "card0", "device", "tile0", gt, "freq0", name)


class FreqTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fs = Sysfs(self._tmp.name)
        self.freq = FreqController(self.fs.cpu, self.fs.drm)

    def units(self):
        return {u["id"]: u for u in self.freq.list_units(GROUPS)["units"]}

    def test_discovery(self):
        units = self.units()
        self.assertEqual(sorted(units), ["cpu:policy0", "cpu:policy1", "cpu:policy2",
                                         "gpu:card0/gt0", "gpu:card0/gt1"])
        self.assertEqual(units["cpu:policy1"]["hw_max_mhz"], 4800)
        self.assertEqual(units["cpu:policy1"]["core_class"], "p")
        self.assertEqual(units["cpu:policy2"]["cpus"], [2, 3])
        self.assertEqual(units["cpu:policy2"]["core_class"], "lpe")
        self.assertEqual(units["gpu:card0/gt1"]["group"], "card0")
        self.assertFalse(units["cpu:policy0"]["limited"])

    def test_cpu_set_writes_khz_and_marks_limited(self):
        ok, msg, applied = self.freq.set_range("cpu:policy0", 800, 2000)
        self.assertTrue(ok, msg)
        self.assertEqual(applied, {"min_mhz": 800, "max_mhz": 2000})
        self.assertEqual(_r(self.fs.cpu_file("policy0", "scaling_min_freq")), 800000)
        self.assertEqual(_r(self.fs.cpu_file("policy0", "scaling_max_freq")), 2000000)
        unit = self.units()["cpu:policy0"]
        self.assertTrue(unit["limited"])
        self.assertEqual((unit["original_min_mhz"], unit["original_max_mhz"]), (400, 4600))

    def test_gpu_set_writes_mhz(self):
        ok, msg, _ = self.freq.set_range("gpu:card0/gt0", 600, 1500)
        self.assertTrue(ok, msg)
        self.assertEqual(_r(self.fs.gpu_file("gt0", "min_freq")), 600)
        self.assertEqual(_r(self.fs.gpu_file("gt0", "max_freq")), 1500)
        self.assertEqual(_r(self.fs.gpu_file("gt1", "max_freq")), 2200)   # other gt untouched

    def test_rejects_bad_ranges(self):
        for lo, hi in ((2000, 1000), (100, 2000), (800, 9000)):
            ok, _, _ = self.freq.set_range("cpu:policy0", lo, hi)
            self.assertFalse(ok, (lo, hi))
        self.assertFalse(self.units()["cpu:policy0"]["limited"])
        self.assertEqual(_r(self.fs.cpu_file("policy0", "scaling_max_freq")), 4600000)

    def test_unknown_unit_is_refused_not_turned_into_a_path(self):
        ok, msg, _ = self.freq.set_range("cpu:../../etc/passwd", 800, 2000)
        self.assertFalse(ok)
        self.assertIn("unknown", msg)

    def test_clamp_pulls_into_each_units_hardware_range(self):
        ok, msg, applied = self.freq.set_range("cpu:policy0", 4700, 4800, clamp=True)
        self.assertTrue(ok, msg)
        self.assertEqual(applied, {"min_mhz": 4600, "max_mhz": 4600})
        ok, _, applied = self.freq.set_range("cpu:policy1", 4700, 4800, clamp=True)
        self.assertTrue(ok)
        self.assertEqual(applied, {"min_mhz": 4700, "max_mhz": 4800})

    def test_order_never_leaves_min_above_max(self):
        """Raising min above the old max must write max first (the driver refuses min > max)."""
        order = []
        real_open = open

        def spy(path, mode="r", *a, **kw):
            if "w" in mode and "policy0" in str(path):
                order.append(os.path.basename(path))
            return real_open(path, mode, *a, **kw)

        self.freq.set_range("cpu:policy0", 400, 1000)       # narrow first
        order.clear()
        import builtins
        builtins.open, saved = spy, builtins.open
        try:
            self.freq.set_range("cpu:policy0", 2000, 3000)  # min 2000 > old max 1000
        finally:
            builtins.open = saved
        self.assertEqual(order, ["scaling_max_freq", "scaling_min_freq"])

    def test_restore_puts_back_first_original(self):
        self.freq.set_range("cpu:policy0", 800, 2000)
        self.freq.set_range("cpu:policy0", 1000, 1500)      # second set must not become the original
        ok, _ = self.freq.restore("cpu:policy0")
        self.assertTrue(ok)
        self.assertEqual(_r(self.fs.cpu_file("policy0", "scaling_min_freq")), 400000)
        self.assertEqual(_r(self.fs.cpu_file("policy0", "scaling_max_freq")), 4600000)
        self.assertFalse(self.units()["cpu:policy0"]["limited"])

    def test_restore_leaves_value_changed_elsewhere(self):
        self.freq.set_range("gpu:card0/gt0", 600, 1500)
        _w(self.fs.gpu_file("gt0", "max_freq"), 1800)       # someone else
        ok, msg = self.freq.restore("gpu:card0/gt0")
        self.assertTrue(ok)
        self.assertIn("changed outside", msg)
        self.assertEqual(_r(self.fs.gpu_file("gt0", "max_freq")), 1800)
        self.assertEqual(self.freq.limited_units(), [])

    def test_restore_of_untouched_unit_is_a_noop(self):
        ok, msg = self.freq.restore("cpu:policy1")
        self.assertTrue(ok)
        self.assertIn("not limited", msg)

    def test_restore_all(self):
        self.freq.set_range("cpu:policy0", 800, 2000)
        self.freq.set_range("gpu:card0/gt0", 600, 1500)
        self.freq.set_range("gpu:card0/gt1", 600, 1500)
        self.assertEqual(self.freq.restore_all(), 0)
        self.assertEqual(self.freq.limited_units(), [])
        self.assertEqual(_r(self.fs.cpu_file("policy0", "scaling_max_freq")), 4600000)
        self.assertEqual(_r(self.fs.gpu_file("gt0", "max_freq")), 2200)
        self.assertEqual(_r(self.fs.gpu_file("gt1", "min_freq")), 300)

    def test_device_gone_is_forgotten_on_restore(self):
        self.freq.set_range("cpu:policy2", 800, 1500)
        for name in os.listdir(self.fs.cpu_file("policy2", "")):
            os.remove(self.fs.cpu_file("policy2", name))
        os.rmdir(self.fs.cpu_file("policy2", ""))
        ok, msg = self.freq.restore("cpu:policy2")
        self.assertTrue(ok)
        self.assertIn("gone", msg)
        self.assertEqual(self.freq.limited_units(), [])

    def test_failed_write_is_rolled_back_and_not_tracked(self):
        os.remove(self.fs.cpu_file("policy0", "scaling_max_freq"))
        os.makedirs(self.fs.cpu_file("policy0", "scaling_max_freq"))   # open() for write now fails
        ok, _, _ = self.freq.set_range("cpu:policy0", 800, 2000)
        self.assertFalse(ok)
        self.assertEqual(self.freq.limited_units(), [])

    def test_gpu_i915_layout(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = os.path.join(tmp.name, "card1", "gt", "gt0")
        for name, value in (("rps_min_freq_mhz", 300), ("rps_max_freq_mhz", 1100),
                            ("rps_RPn_freq_mhz", 300), ("rps_RP0_freq_mhz", 1100),
                            ("rps_cur_freq_mhz", 500)):
            _w(os.path.join(base, name), value)
        freq = FreqController(os.path.join(tmp.name, "nocpu"), tmp.name)
        ok, msg, _ = freq.set_range("gpu:card1/gt0", 400, 900)
        self.assertTrue(ok, msg)
        self.assertEqual(_r(os.path.join(base, "rps_max_freq_mhz")), 900)

    def test_no_devices(self):
        freq = FreqController("/nonexistent/cpu", "/nonexistent/drm")
        self.assertEqual(freq.list_units()["units"], [])
        self.assertEqual(freq.restore_all(), 0)


if __name__ == "__main__":
    unittest.main()
