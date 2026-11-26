# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""GPU throttle detection: sysfs parsing and the debounced event tracker."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics import gpu_throttle  # noqa: E402
from diagnostics.gpu_throttle import GpuThrottleTracker, _summary  # noqa: E402
from monitor.metrics import gpu_perf  # noqa: E402
from monitor.metrics.gpu_perf import gt_effectively_throttled  # noqa: E402
from monitor import gpu_monitor  # noqa: E402


def _make_throttle(root, card, gt, status, reasons):
    d = Path(root) / card / "device" / "tile0" / gt / "freq0" / "throttle"
    d.mkdir(parents=True)
    (d / "status").write_text(f"{status}\n")
    for name in ("pl1", "pl2", "pl4", "thermal", "prochot", "ratl", "vr_tdc", "vr_thermalert"):
        (d / f"reason_{name}").write_text("1\n" if name in reasons else "0\n")
    (d / "reasons").write_text(("none" if not reasons else " ".join(reasons)) + "\n")


class ReadSysfsThrottleTest(unittest.TestCase):
    def _read(self, root, card="card0"):
        real_path = gpu_monitor.Path
        with mock.patch.object(
                gpu_monitor, "Path",
                side_effect=lambda p: real_path(str(p).replace("/sys/class/drm", root))):
            return gpu_monitor.read_sysfs_throttle(card)

    def test_not_throttled(self):
        with tempfile.TemporaryDirectory() as root:
            _make_throttle(root, "card0", "gt0", 0, [])
            self.assertEqual(self._read(root), {"gt0": {"throttled": False, "reasons": []}})

    def test_multiple_reasons(self):
        with tempfile.TemporaryDirectory() as root:
            _make_throttle(root, "card0", "gt0", 1, ["pl1", "thermal"])
            self.assertEqual(
                self._read(root),
                {"gt0": {"throttled": True, "reasons": ["pl1", "thermal"]}})

    def test_interface_absent(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "card0" / "device").mkdir(parents=True)
            self.assertEqual(self._read(root), {})


class GpuThrottleTrackerTest(unittest.TestCase):
    def test_requires_three_distinct_samples_to_confirm(self):
        t = GpuThrottleTracker()
        self.assertIsNone(t.observe(True, ["pl1"], 1))
        self.assertIsNone(t.observe(True, ["pl1"], 2))
        action = t.observe(True, ["pl1"], 3)
        self.assertEqual(action, {"state": "throttled", "reasons": ["pl1"]})
        self.assertTrue(t.throttled)

    def test_same_snapshot_is_not_counted_twice(self):
        t = GpuThrottleTracker()
        for _ in range(10):
            self.assertIsNone(t.observe(True, ["pl1"], 5))
        self.assertFalse(t.throttled)

    def test_short_burst_is_ignored(self):
        t = GpuThrottleTracker()
        t.observe(True, ["pl2"], 1)
        t.observe(True, ["pl2"], 2)
        self.assertIsNone(t.observe(False, [], 3))
        self.assertIsNone(t.observe(True, ["pl2"], 4))
        self.assertIsNone(t.observe(True, ["pl2"], 5))
        self.assertFalse(t.throttled)

    def test_clear_is_debounced_and_reports_last_reasons(self):
        t = GpuThrottleTracker()
        for ts in (1, 2, 3):
            t.observe(True, ["thermal"], ts)
        self.assertIsNone(t.observe(False, [], 4))
        self.assertIsNone(t.observe(False, [], 5))
        action = t.observe(False, [], 6)
        self.assertEqual(action, {"state": "cleared", "reasons": ["thermal"]})
        self.assertFalse(t.throttled)

    def test_summary_names_reasons(self):
        text = _summary("gpu:0000:00:02.0/gt0",
                        {"state": "throttled", "reasons": ["pl1", "thermal"]})
        self.assertIn("gt0", text)
        self.assertIn("PL1", text)
        self.assertIn("thermal", text)


class GtEffectivelyThrottledTest(unittest.TestCase):
    """Values are the ones measured on WCL at PL1 (see ACTIVE_RC6_MAX_PCT)."""

    def test_active_gt_held_below_request_counts(self):
        self.assertTrue(gt_effectively_throttled(True, 0.0))
        self.assertTrue(gt_effectively_throttled(True, 7.0))

    def test_idle_gt_sharing_package_limit_does_not_count(self):
        self.assertFalse(gt_effectively_throttled(True, 92.0))
        self.assertFalse(gt_effectively_throttled(True, 94.0))

    def test_not_throttled_never_counts(self):
        self.assertFalse(gt_effectively_throttled(False, 0.0))

    def test_unknown_activity_is_not_reported(self):
        self.assertFalse(gt_effectively_throttled(True, None))


def _converted_freqs(per_gt):
    """Run raw per-GT readings through the real conversion, as the monitor does."""
    result = {"driver": "xe", "pci_slot": "0000:00:02.0", "frequency": per_gt}
    return gpu_perf._convert_monitor_results([result])["devices"][0]["freqs"]


class ConversionTest(unittest.TestCase):
    def test_idle_gt_is_not_flagged_and_loses_its_reasons(self):
        freqs = _converted_freqs({
            "gt0": {"throttled": True, "throttle_reasons": ["pl1"], "rc6_pct": 0.0},
            "gt1": {"throttled": True, "throttle_reasons": ["pl1"], "rc6_pct": 93.0},
        })
        by = {f["name"]: f for f in freqs}
        self.assertTrue(by["gt0"]["throttled"])
        self.assertEqual(by["gt0"]["throttle_reasons"], ["pl1"])
        self.assertFalse(by["gt1"]["throttled"])
        self.assertEqual(by["gt1"]["throttle_reasons"], [])


class RunOnceTest(unittest.TestCase):
    def setUp(self):
        gpu_throttle._trackers.clear()
        self.addCleanup(gpu_throttle._trackers.clear)

    def _run(self, per_gt, samples):
        freqs = _converted_freqs(per_gt)
        emitted = []
        for ts in range(1, samples + 1):
            dev = {"pci_dev": "0000:00:02.0", "drv_name": "xe", "freqs": freqs}
            with mock.patch.object(gpu_throttle, "_latest_snapshot", return_value=([dev], ts)), \
                    mock.patch.object(gpu_throttle, "_emit"):
                for subject, action in gpu_throttle.run_once():
                    emitted.append((subject, action["state"]))
        return emitted

    def test_only_the_active_gt_is_reported(self):
        emitted = self._run({
            "gt0": {"throttled": True, "throttle_reasons": ["pl1"], "rc6_pct": 0.0},
            "gt1": {"throttled": True, "throttle_reasons": ["pl1"], "rc6_pct": 93.0},
        }, 4)
        self.assertEqual(emitted, [("gpu:0000:00:02.0/gt0", "throttled")])

    def test_cpu_only_load_reports_nothing(self):
        emitted = self._run({
            "gt0": {"throttled": False, "throttle_reasons": [], "rc6_pct": 83.0},
            "gt1": {"throttled": True, "throttle_reasons": ["pl1"], "rc6_pct": 93.0},
        }, 10)
        self.assertEqual(emitted, [])


if __name__ == "__main__":
    unittest.main()
