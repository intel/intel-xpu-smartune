# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the disk-IO pressure model (monitor/disk_pressure.py +
PressureAnalyzer.classify_disk_pressure).

Uses synthetic per-disk stats and injected device profiles so the tests are
deterministic and do not depend on real disks or kernel PSI triggers.

Run:  python3 balancer/test/test_disk_pressure.py
  or: python3 -m unittest balancer.test.test_disk_pressure
"""

import os
import sys
import unittest

# Allow running the file directly from anywhere.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from monitor.disk_pressure import (
    DiskIOMonitor,
    compute_disk_pressure,
    _classify_media,
    _sigmoid_pressure,
    _MEDIA_CLASSES,
)
from monitor.pressure import PressureAnalyzer


class _Cfg:
    """Minimal stand-in for b_config."""
    thresholds = {"low": 0.4, "medium": 0.6, "high": 0.8, "critical": 1.0}
    # Disk bands are a separate config block with no fallback to `thresholds`; the same
    # values here keep the existing expectations meaningful while exercising the real
    # lookup path.
    disk_thresholds = {"low": 0.4, "medium": 0.6, "high": 0.8, "critical": 1.0}
    disk_psi_weights = None  # exercise the in-code weight defaults
    weights = {"cpu": 1, "memory": 8, "io": 1}

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _stats(util=0.0, await_ms=0.0, aqu=0.0):
    """A per-disk stats row as produced by _collect_disk_io_stats."""
    return {
        "utilization": util, "read_kb_per_sec": 0.0, "write_kb_per_sec": 0.0,
        "read_iops": 0.0, "write_iops": 0.0, "await_ms": await_ms, "aqu": aqu,
    }


def _monitor(disk_io, profiles, cfg=None):
    """A DiskIOMonitor whose sampling is replaced by fixed ``disk_io`` and whose
    device *probes* are pre-seeded (so no sysfs / psutil sampling happens).

    A probe is only what sysfs reports -- ``{media, queue_depth}``. The sigmoid
    half-points are no longer part of it: they come from ``config.disk_pressure_model``
    on every call, so tests exercise the same resolution path production uses."""
    m = DiskIOMonitor(cfg or _Cfg())
    m._profiles = dict(profiles)
    m._collect_disk_io_stats = lambda: {"disk_io": disk_io}
    return m


class SigmoidTests(unittest.TestCase):
    def test_half_point_is_half(self):
        self.assertAlmostEqual(_sigmoid_pressure(20, 20), 0.5, places=9)

    def test_monotone_and_bounds(self):
        self.assertGreater(_sigmoid_pressure(40, 20), 0.9)
        self.assertLess(_sigmoid_pressure(5, 20), 0.1)
        self.assertEqual(_sigmoid_pressure(5, 0), 0.0)  # guard: non-positive half-point


class PerDiskPressureTests(unittest.TestCase):
    HDD = {"media": "hdd", "queue_depth": None}
    NVME = {"media": "nvme", "queue_depth": None}

    def test_media_aware_latency(self):
        """20 ms await is normal for an HDD but catastrophic for an NVMe (both disks busy,
        so the activity gate is fully open and latency drives the difference)."""
        m = _monitor({"sda": _stats(util=90.0, await_ms=20.0),
                      "nvme0n1": _stats(util=90.0, await_ms=20.0)},
                     {"sda": self.HDD, "nvme0n1": self.NVME})
        d = m.evaluate()["details"]
        # HDD at its half-point -> ~0.55*0.5 ≈ 0.27; NVMe far past -> latency term saturates.
        self.assertLess(d["sda"]["pressure"], 0.4)
        self.assertGreater(d["nvme0n1"]["pressure"], 0.5)

    def test_idle_is_near_zero(self):
        m = _monitor({"sda": _stats(util=1.0, await_ms=0.1, aqu=0.0)}, {"sda": self.HDD})
        self.assertLess(m.evaluate()["details"]["sda"]["pressure"], 0.05)


class MediaClassificationTests(unittest.TestCase):
    """Media class decides every half-point, so a misclassified device is judged by the
    wrong yardstick. Tested as a pure function -- no machine has all four media classes."""

    def test_nvme_by_name(self):
        # NVMe has no `device/queue_depth` and reports rotational=0; the name is the signal.
        self.assertEqual(_classify_media("nvme0n1", 0, 0, "/devices/pci0000:00/.../nvme0"), "nvme")

    def test_rotational_is_hdd(self):
        self.assertEqual(_classify_media("sda", 1, 0, "/devices/pci0000:00/.../ata1"), "hdd")

    def test_plain_sata_ssd(self):
        self.assertEqual(_classify_media("sdb", 0, 0, "/devices/pci0000:00/.../ata2"), "sata_ssd")

    def test_usb_wins_over_rotational_flag(self):
        """The misclassification that matters: USB mass storage reports rotational=0, so a
        rotational-first check would call it a SATA SSD and hold a thumb drive to a 5 ms
        await -- an order of magnitude off, reading a healthy device as permanently saturated."""
        usb_path = "/devices/pci0000:00/0000:00:14.0/usb2/2-1/2-1:1.0/host6/target6:0:0/6:0:0:0/block/sdc"
        self.assertEqual(_classify_media("sdc", 0, 0, usb_path), "usb")
        self.assertEqual(_classify_media("sdc", 0, 1, "/devices/whatever"), "usb")

    def test_unreadable_sysfs_falls_back_to_ssd(self):
        # None everywhere (container / restricted sysfs): the modern-default class, not a crash.
        self.assertEqual(_classify_media("vda", None, None, ""), "sata_ssd")

    def test_every_class_has_all_half_points(self):
        """A class present in the taxonomy but missing from a half-point map would KeyError
        on the pressure tick for whoever owns that hardware."""
        m = DiskIOMonitor(_Cfg())._model()
        for key in ("await_half_ms", "queue_half", "util_half_pct", "activity_util_pct"):
            for media in _MEDIA_CLASSES:
                self.assertIn(media, m[key], f"{key} missing {media}")


class MediaAwareUtilTests(unittest.TestCase):
    def test_same_util_reads_lower_on_a_parallel_device(self):
        """%util only says "at least one IO in flight", so it means very different things per
        media: 90% on a single-actuator HDD is near saturation, on an NVMe it is routine."""
        m = _monitor({"sda": _stats(util=90.0), "nvme0n1": _stats(util=90.0)},
                     {"sda": {"media": "hdd", "queue_depth": None},
                      "nvme0n1": {"media": "nvme", "queue_depth": None}})
        d = m.evaluate()["details"]
        self.assertGreater(d["sda"]["pressure"], d["nvme0n1"]["pressure"])

    def test_advertised_depth_of_one_is_honoured(self):
        """A USB bridge / virtual disk really can hold one request in flight. Ignoring a
        depth of 1 would grant it the class default (16) -- 16x the headroom it has."""
        m = _monitor({}, {"sdc": {"media": "sata_ssd", "queue_depth": 1}})
        self.assertEqual(m._disk_profile("sdc")["queue_half"], 1.0)

    def test_advertised_depth_never_raises_the_ceiling(self):
        m = _monitor({}, {"nvme0n1": {"media": "nvme", "queue_depth": 1023}})
        self.assertEqual(m._disk_profile("nvme0n1")["queue_half"], 32.0)

    def test_scalar_config_applies_to_every_media(self):
        """The half-point keys used to be device-agnostic scalars. One number must still
        mean that number for every class rather than silently reverting to the defaults."""
        cfg = _Cfg(disk_pressure_model={"util_half_pct": 50.0})
        m = DiskIOMonitor(cfg)._model()
        self.assertEqual(set(m["util_half_pct"].values()), {50.0})


class AggregateTests(unittest.TestCase):
    HDD = {"media": "hdd", "queue_depth": None}
    NVME = {"media": "nvme", "queue_depth": None}
    SSD = {"media": "sata_ssd", "queue_depth": None}

    def test_one_saturated_disk_among_many_dominates(self):
        """The original bug: 1 saturated disk out of 8 must NOT be averaged away."""
        disk_io = {}
        profiles = {}
        # 7 idle disks + 1 saturated HDD (high await + deep queue).
        for i in range(3):
            disk_io[f"nvme{i}"] = _stats(util=10.0, await_ms=0.1, aqu=1.0); profiles[f"nvme{i}"] = self.NVME
        for i in range(2):
            disk_io[f"sd{chr(ord('a')+i)}"] = _stats(util=5.0, await_ms=0.5, aqu=1.0); profiles[f"sd{chr(ord('a')+i)}"] = self.SSD
        for i in range(2):
            disk_io[f"sd{chr(ord('c')+i)}"] = _stats(util=0.0); profiles[f"sd{chr(ord('c')+i)}"] = self.HDD
        disk_io["sdz"] = _stats(util=100.0, await_ms=200.0, aqu=32.0); profiles["sdz"] = self.HDD

        ev = _monitor(disk_io, profiles).evaluate()
        self.assertGreater(ev["max_p"], 0.9)
        self.assertGreater(ev["disk_combined"], 0.75)   # ~0.8 despite 7/8 idle
        self.assertEqual(ev["stressed_disks"], ["sdz"])
        self.assertLess(ev["avg_p"], 0.2)               # mean alone would hide it

    def test_no_disks(self):
        ev = _monitor({}, {}).evaluate()
        self.assertEqual(ev["disk_combined"], 0.0)
        self.assertEqual(ev["stressed_disks"], [])


class GateTests(unittest.TestCase):
    """Continuous PSI gating of disk_combined -> final level.

    raw = disk_combined + (1 - disk_combined) * stall * sat, where
      stall = min(1, some*0.5 + full*3.0)   and   sat = clamp((combined - 0.4) / (0.8 - 0.4)).
    The gate must climb medium -> high -> critical smoothly (no medium->critical cliff), and
    only a disk that is BOTH saturated and stalling the whole system may reach critical.
    """
    TH = _Cfg.disk_thresholds

    def _settle(self, combined, some, full, frac=None, n=8):
        a = PressureAnalyzer(_Cfg())
        out = None
        for _ in range(n):
            out = a.classify_disk_pressure(combined, some, full, frac, self.TH)
        return out  # (level, score, is_stressed)

    def test_idle_stays_low(self):
        lvl, _, stressed = self._settle(0.10, some=0.0, full=0.0)
        self.assertEqual(lvl, "low")
        self.assertFalse(stressed)

    def test_unsaturated_heavy_stall_stays_low(self):
        # Heavy PSI but the disk itself is barely saturated (combined < low) -> sat=0, so the
        # stall is NOT attributed to disk (e.g. a network fs). This is the real-world case that
        # motivated the redesign: 100% util NVMe reading ~0.33 combined must not go critical.
        lvl, _, stressed = self._settle(0.33, some=0.58, full=0.48)
        self.assertEqual(lvl, "low")
        self.assertFalse(stressed)

    def test_partial_saturation_heavy_stall_is_medium(self):
        # combined=0.50 -> sat=0.25; stall saturates -> raw=0.625 -> medium, not critical.
        lvl, _, stressed = self._settle(0.50, some=0.58, full=0.48)
        self.assertEqual(lvl, "medium")
        self.assertFalse(stressed)

    def test_saturated_no_stall_is_high_not_critical(self):
        # USE-saturated but no task stall -> high (armed / top-consumer identified), never
        # throttled: throttling happens only at critical, which requires real stall.
        lvl, _, stressed = self._settle(0.85, some=0.0, full=0.0)
        self.assertEqual(lvl, "high")
        self.assertTrue(stressed)

    def test_moderate_saturation_moderate_stall_is_high(self):
        # combined=0.62 with saturating stall -> raw~0.83 -> high, NOT an instant critical.
        lvl, _, stressed = self._settle(0.62, some=0.20, full=0.30)
        self.assertEqual(lvl, "high")
        self.assertTrue(stressed)

    def test_fully_saturated_and_stalling_is_critical(self):
        # Fast-attack: fully saturated (sat=1) AND system-wide stall (stall=1) -> critical
        # without settling.
        a = PressureAnalyzer(_Cfg())
        lvl, _, stressed = a.classify_disk_pressure(0.85, 0.30, 0.40, None, self.TH)
        self.assertEqual(lvl, "critical")
        self.assertTrue(stressed)

    def test_climbs_through_high_before_critical(self):
        # No medium->critical cliff: with a fixed heavy stall, rising saturation passes through
        # high before it can reach critical.
        levels = []
        for combined in (0.55, 0.62, 0.75, 0.85):
            a = PressureAnalyzer(_Cfg())
            lvl, _, _ = a.classify_disk_pressure(combined, 0.30, 0.40, None, self.TH)
            levels.append(lvl)
        self.assertEqual(levels, ["medium", "high", "high", "critical"])

    def test_self_inflicted_discount_demotes(self):
        # combined=0.70, full=0.30. Undiscounted -> high; a fully self-inflicted io stall is
        # discounted (full*0.3) so the stall shrinks and the level drops to medium.
        lvl, _, stressed = self._settle(0.70, some=0.0, full=0.30)
        self.assertEqual(lvl, "high")
        self.assertTrue(stressed)
        lvl, _, stressed = self._settle(0.70, some=0.0, full=0.30, frac={"io": 1.0})
        self.assertEqual(lvl, "medium")
        self.assertFalse(stressed)

    def test_critical_is_released_as_soon_as_the_score_drops(self):
        """Critical is latched on the RAW score topping out, so it must be released the
        moment the score is no longer at the top -- the downgrade hysteresis that protects
        low/medium/high would report "critical" at 0.96, a level the score never reached,
        and would keep the balancer's throttle armed for another tick on expired evidence."""
        a = PressureAnalyzer(_Cfg())
        lvl, _, _ = a.classify_disk_pressure(0.85, 0.30, 0.40, None, self.TH)
        self.assertEqual(lvl, "critical")
        # stall=0.733 -> raw = 0.85 + 0.15*0.733 = 0.96, i.e. below critical.
        lvl, score, _ = a.classify_disk_pressure(0.85, 0.0, 0.2444, None, self.TH)
        self.assertEqual(lvl, "high")
        self.assertLess(score, self.TH["critical"])

    def test_lower_levels_keep_their_downgrade_hysteresis(self):
        """The exemption is critical-only: high must still resist chattering at its edge."""
        a = PressureAnalyzer(_Cfg())
        for _ in range(8):
            lvl, _, _ = a.classify_disk_pressure(0.85, 0.0, 0.0, None, self.TH)
        self.assertEqual(lvl, "high")
        # Drift just under the 0.8 entry but within the 0.05 hysteresis band -> still high.
        for _ in range(6):
            lvl, score, _ = a.classify_disk_pressure(0.78, 0.0, 0.0, None, self.TH)
        self.assertEqual(lvl, "high")
        self.assertLess(score, self.TH["high"])

    def test_disk_channel_does_not_perturb_system_score(self):
        a = PressureAnalyzer(_Cfg())
        s1, _ = a.classify_level(0.5, self.TH)
        a.classify_disk_pressure(0.9, 0.3, 0.40, None, self.TH)
        s2, _ = a.classify_level(0.5, self.TH)
        self.assertEqual(s1, s2)

    def test_disk_bands_are_independent_of_system_bands(self):
        """A disk-only band change must move the disk level and nothing else."""
        disk_bands = {"low": 0.4, "medium": 0.6, "high": 0.8, "critical": 0.9}
        a = PressureAnalyzer(_Cfg())
        # combined=0.85, stall=0.333 -> raw=0.90: critical under the 0.9 disk band...
        lvl, _, _ = a.classify_disk_pressure(0.85, 0.0, 0.111, None, disk_bands)
        self.assertEqual(lvl, "critical")
        # ...and only high under the shipped 1.0 band, from the identical inputs.
        lvl, _, _ = PressureAnalyzer(_Cfg()).classify_disk_pressure(
            0.85, 0.0, 0.111, None, self.TH)
        self.assertEqual(lvl, "high")

    def test_psi_weights_come_from_config(self):
        """Raising the `some` weight lets io.some alone drive the gate.

        With the shipped weights `some` maxes out at 0.5 of the stall, so reaching
        critical requires io.full -- the case that makes the throttle hard to trigger on
        async/direct workloads. The weights must therefore be tunable without a code change.
        """
        base = PressureAnalyzer(_Cfg())
        lvl, _, _ = base.classify_disk_pressure(0.85, 0.9, 0.0, None, self.TH)
        self.assertEqual(lvl, "high")  # some*0.5 = 0.45 -> raw 0.92, short of critical

        tuned = PressureAnalyzer(_Cfg(disk_psi_weights={"some": 1.2, "full": 3.0}))
        lvl, _, _ = tuned.classify_disk_pressure(0.85, 0.9, 0.0, None, self.TH)
        self.assertEqual(lvl, "critical")  # some*1.2 = 1.08 -> stall saturates


class SaturationReportTests(unittest.TestCase):
    """The USE-only path reports saturation, never a throttle verdict."""

    NVME = {"media": "nvme", "queue_depth": None}

    def test_reports_is_saturated_not_is_stressed(self):
        m = _monitor({"nvme0n1": _stats(util=100.0, await_ms=20.0, aqu=64.0)},
                     {"nvme0n1": self.NVME})
        r = m.is_disk_io_stressed()
        self.assertTrue(r["is_saturated"])
        # The gated verdict is added later by the pressure tick; this path must not
        # pretend to supply one.
        self.assertNotIn("is_stressed", r)

    def test_idle_disk_is_not_saturated(self):
        m = _monitor({"nvme0n1": _stats(util=1.0, await_ms=0.1)}, {"nvme0n1": self.NVME})
        self.assertFalse(m.is_disk_io_stressed()["is_saturated"])


class UiAggregationTests(unittest.TestCase):
    def test_severity_and_breadth_separated(self):
        details = {"sda": {"is_busy": True}, "sdb": {"is_busy": False}, "nvme0n1": {"is_busy": False}}
        r = compute_disk_pressure({"disk_io": details, "level": "critical", "score": 0.98})
        self.assertEqual(r["busy_level"], "CRITICAL")   # severity from gated level
        self.assertEqual(r["busy_pct"], 33.33)          # breadth: only 1/3 disks busy
        self.assertEqual(r["pressure_pct"], 98.0)

    def test_fallback_to_ratio_when_no_gated_level(self):
        details = {"sda": {"is_busy": True}, "sdb": {"is_busy": False}, "nvme0n1": {"is_busy": False}}
        r = compute_disk_pressure({"disk_io": details})
        self.assertEqual(r["busy_level"], "LOW")        # 1/3 < low band -> LOW

    def test_no_data(self):
        r = compute_disk_pressure({})
        self.assertEqual(r["busy_level"], "NO DATA")


if __name__ == "__main__":
    unittest.main(verbosity=2)
