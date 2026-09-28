# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the pre-OOM band published by SystemPressureMonitor.

The band was already being computed on every refresh -- it is what decides
whether the monitor polls itself every 5 s or every 2 s -- but the answer was
thrown away.  `is_memory_pre_oom` publishes it, because it is the earliest
warning the monitor has and it is earlier than the pressure level:

| time     | mem% | score | level    |
|----------|------|-------|----------|
| 22:34:35 | 16%  | 0.00  | low      |
| 22:34:45 | 71%  | 0.04  | low      | <- band entered here
| 22:34:52 | 78%  | 0.19  | low      |
| 22:34:55 | 94%  | 1.00  | critical |

The level jumped low -> critical in one refresh, so "high" never happened and
anything gated on it never ran.  The score is no better: its memory term is a
deliberately sharp gate (0.027 / 0.119 / 0.310 / 0.996), which is why it reads
0.19 two seconds before it reads 1.00.  Raw usage is the only smooth signal.

Run:  python3 balancer/test/test_pressure_pre_oom.py
  or: python3 -m unittest balancer.test.test_pressure_pre_oom
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from monitor.monitor_api import (  # noqa: E402
    _FAST_PRESSURE_UPDATE,
    _FAST_ZONE_MEM_MARGIN,
    SystemPressureMonitor,
)


class _Psi:
    def get_current_pressure(self):
        return {"cpu": 0.0, "memory": 0.0, "io": 0.0}

    def get_io_full_pressure(self):
        return 0.0


class _Res:
    """Reports whatever memory usage percentage the test poses."""

    def __init__(self):
        self.mem_usage = 0.0

    def get_resource_usage(self):
        return {"memory": {"usage": self.mem_usage}, "cpu": {"usage": 0.0}}

    def is_disk_io_stressed(self):
        return {"disk_combined": 0.0, "details": {}}


class _Analyzer:
    """The level is pinned to "low" on purpose.

    Every test here is about the band warning *while the level says nothing is
    wrong* -- that gap is the entire reason the flag exists.
    """

    def calculate_pressure_score(self, *a, **kw):
        return 0.0

    def classify_disk_pressure(self, *a, **kw):
        return "low", 0.0, False

    def classify_level(self, raw, thresholds):
        return "low", raw


class _Cfg:
    thresholds = {"low": 0.4, "medium": 0.6, "high": 0.8, "critical": 1.0}
    disk_thresholds = dict(thresholds)
    regular_update_sys_pressure_time = 5.0

    def __init__(self, memory_busy_threshold=90):
        self.memory_busy_threshold = memory_busy_threshold


def _monitor(memory_busy_threshold=90):
    """A SystemPressureMonitor with no auto-refresh thread and no real /proc.

    __init__ is bypassed because it starts the refresh loop; the handful of
    attributes _update_pressure_level actually touches are set by hand.
    """
    mon = SystemPressureMonitor.__new__(SystemPressureMonitor)
    mon.config = _Cfg(memory_busy_threshold)
    mon.psi = _Psi()
    mon.res = _Res()
    mon.analyzer = _Analyzer()
    mon._CACHE_TTL = mon.config.regular_update_sys_pressure_time
    mon._next_interval = mon._CACHE_TTL
    mon._in_fast_zone = False
    mon._is_limited_app_dominant = False
    mon._dominant_cgroups = []
    mon._last_update_time = 0
    return mon


class PreOomBandTests(unittest.TestCase):
    def test_a_cold_monitor_is_not_in_the_band(self):
        """False before the first refresh: nobody should prefetch at boot on
        the strength of a value nothing has measured yet."""
        self.assertFalse(_monitor().is_memory_pre_oom)

    def test_the_band_opens_twenty_points_below_the_busy_threshold(self):
        mon = _monitor(memory_busy_threshold=90)
        for usage, expected in ((69.0, False), (70.0, True), (94.0, True)):
            mon.res.mem_usage = usage
            mon._update_pressure_level()
            self.assertIs(mon.is_memory_pre_oom, expected, f"at {usage}% used")

    def test_the_band_moves_with_the_configured_threshold(self):
        """It is a margin below memory_busy_threshold, not a hard-coded 70."""
        mon = _monitor(memory_busy_threshold=80)
        mon.res.mem_usage = 61.0
        mon._update_pressure_level()
        self.assertTrue(mon.is_memory_pre_oom)
        self.assertEqual(80 - _FAST_ZONE_MEM_MARGIN, 60.0)

    def test_the_flag_falls_again_when_memory_is_released(self):
        """Deliberately not hysteretic: the one consumer is edge-triggered by
        its own caller, so a sticky flag would just delay the next warning."""
        mon = _monitor()
        mon.res.mem_usage = 85.0
        mon._update_pressure_level()
        self.assertTrue(mon.is_memory_pre_oom)
        mon.res.mem_usage = 30.0
        mon._update_pressure_level()
        self.assertFalse(mon.is_memory_pre_oom)

    def test_the_flag_and_the_refresh_cadence_are_the_same_decision(self):
        """One band, two uses -- the value was already being computed here to
        pick the polling interval and was simply discarded afterwards."""
        mon = _monitor()
        mon.res.mem_usage = 85.0
        mon._update_pressure_level()
        self.assertTrue(mon.is_memory_pre_oom)
        self.assertEqual(mon._next_interval, _FAST_PRESSURE_UPDATE)
        mon.res.mem_usage = 30.0
        mon._update_pressure_level()
        self.assertFalse(mon.is_memory_pre_oom)
        self.assertEqual(mon._next_interval, mon._CACHE_TTL)

    def test_the_incident_timeline_warns_ten_seconds_before_the_level(self):
        """Replay of 22:34:35 -> 22:34:55.  The level is pinned to "low" by the
        analyzer stub throughout, which is what actually happened until the
        very last refresh -- and the band is already true two samples earlier.
        """
        mon = _monitor()
        warned_at = []
        for stamp, usage in ((35, 16.0), (40, 55.0), (45, 71.0),
                             (50, 76.0), (52, 78.0)):
            mon.res.mem_usage = usage
            level, _, _, _ = mon._update_pressure_level()
            self.assertEqual(level, "low")
            if mon.is_memory_pre_oom:
                warned_at.append(stamp)
        self.assertEqual(warned_at[0], 45, "band should open at 71% used")

    def test_a_broken_refresh_leaves_the_last_answer_alone(self):
        """_update_pressure_level swallows its own exceptions and returns
        "unknown"; the flag must not silently flip on a failed read."""
        mon = _monitor()
        mon.res.mem_usage = 85.0
        mon._update_pressure_level()
        self.assertTrue(mon.is_memory_pre_oom)

        def _boom():
            raise OSError("meminfo unreadable")

        mon.res.get_resource_usage = _boom
        level, _, _, _ = mon._update_pressure_level()
        self.assertEqual(level, "unknown")
        self.assertTrue(mon.is_memory_pre_oom)


if __name__ == "__main__":
    unittest.main(verbosity=2)
