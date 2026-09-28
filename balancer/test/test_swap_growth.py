# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for L0 swap headroom planning (P2: plan only, never execute).

The sizing rules are asserted across four machine shapes (4/8/32/128 GB) on
purpose.  Every threshold is a ratio, and a ratio that was quietly tuned against
one box is the failure mode these tests exist to catch.

Run: PYTHONDONTWRITEBYTECODE=1 python3 balancer/test/test_swap_growth.py
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
_test_logger = logging.getLogger("test_swap_growth")
_logger_module.logger = _test_logger
_logger_module.get_logger = lambda name=None: _test_logger
_logger_module.current_log_context = lambda: {}
sys.modules["utils.logger"] = _logger_module

from controller.swap import (  # noqa: E402
    GB,
    GROW_LEAD_SEC,
    MB,
    MEM_RISE_TRIGGER,
    MIN_SLOPE_SPAN_SEC,
    SHRINK_HYSTERESIS,
    SHRINK_IDLE_SEC,
    SWAP_GROWTH_DEFAULTS,
    MemorySnapshot,
    SwapController,
)


def controller(**overrides) -> SwapController:
    cfg = {"enabled": True}
    cfg.update(overrides)
    return SwapController(types.SimpleNamespace(
        limit_policy={"memory": {"swap_growth": cfg}}))


def snapshot(total_mb, used_pct, swap_total_mb, swap_used_pct, anon_mb=None) -> MemorySnapshot:
    """A machine at *used_pct* of memory with swap *swap_used_pct* full.

    anon defaults to the whole shortfall being anonymous, i.e. the case swap can
    actually fix; pass it explicitly to model a slab/page-table shortfall.
    """
    total = total_mb * MB
    used = int(total * used_pct)
    swap_total = swap_total_mb * MB
    swap_used = int(swap_total * swap_used_pct)
    anon = used if anon_mb is None else anon_mb * MB
    return MemorySnapshot(
        mem_total=total, mem_available=total - used, mem_free=total - used,
        anon_pages=anon, swap_total=swap_total, swap_free=swap_total - swap_used,
    )


# A roomy disk, so disk_cap never becomes the binding constraint by accident.
BIG_DISK = dict(disk_total=4000 * GB, disk_available=3000 * GB, fstype="ext4")


class SnapshotTests(unittest.TestCase):
    def test_used_effective_is_based_on_memavailable_not_memfree(self):
        """This is why drop_caches is not in the design.

        MemAvailable already counts reclaimable page cache as available, so a
        box with 6 GB of cache is *not* 80% used.  Had used_effective been built
        on MemFree, every cache-warm NAS would look permanently starved and
        growth would fire constantly.
        """
        total = 32 * GB
        snap = MemorySnapshot(mem_total=total, mem_free=4 * GB,
                              mem_available=26 * GB, anon_pages=4 * GB)
        self.assertEqual(snap.used_effective, 6 * GB)
        self.assertAlmostEqual(snap.used_fraction, 6 / 32)

    def test_no_swap_reads_as_zero_percent_free_not_a_hundred(self):
        """No swap is the most swap-starved state there is; a 1.0 here would
        make the watermark permanently un-crossable on a swapless box."""
        snap = MemorySnapshot(mem_total=8 * GB, mem_available=1 * GB,
                              swap_total=0, swap_free=0)
        self.assertEqual(snap.swap_free_fraction, 0.0)

    def test_movable_anon_discounts_what_is_already_swapped(self):
        snap = MemorySnapshot(mem_total=32 * GB, mem_available=2 * GB,
                              anon_pages=20 * GB,
                              swap_total=8 * GB, swap_free=3 * GB)
        self.assertEqual(snap.movable_anon, 15 * GB)  # 20 GB anon - 5 GB in swap

    def test_movable_anon_floors_at_zero(self):
        snap = MemorySnapshot(mem_total=8 * GB, anon_pages=1 * GB,
                              swap_total=8 * GB, swap_free=0)
        self.assertEqual(snap.movable_anon, 0)


class WatermarkTests(unittest.TestCase):
    def test_either_watermark_alone_is_not_enough(self):
        """80% memory with a healthy page cache is ordinary on a NAS, and swap
        at half is ordinary too; crossing one watermark is not news."""
        ctl = controller()
        # Memory high, swap still roomy -> no growth.
        plan = ctl.plan_growth(snapshot(32 * 1024, 0.90, 8 * 1024, 0.10), **BIG_DISK)
        self.assertFalse(plan.should_grow)
        self.assertIn("roomy", plan.reason)
        # Swap half gone, memory fine -> no growth.
        plan = ctl.plan_growth(snapshot(32 * 1024, 0.40, 8 * 1024, 0.60), **BIG_DISK)
        self.assertFalse(plan.should_grow)
        self.assertIn("below watermark", plan.reason)
        # Both -> growth.
        plan = ctl.plan_growth(snapshot(32 * 1024, 0.85, 8 * 1024, 0.60), **BIG_DISK)
        self.assertTrue(plan.should_grow, plan.reason)

    def test_swap_almost_gone_grows_on_its_own(self):
        """The AND gate this replaces was unsatisfiable exactly when it mattered.

        Measured: swap hit 0.0% free at 8191, 16118 and 24044 MB and growth was
        refused all three times because used_eff read 59-74%.  That is not bad
        luck, it is the definition of the two signals: every anon page swap
        accepts is a page that leaves MemAvailable's deficit, so the fuller swap
        gets the *healthier* memory looks.  Waiting for both is waiting forever.
        """
        ctl = controller()
        plan = ctl.plan_growth(snapshot(32 * 1024, 0.70, 24044, 0.999), **BIG_DISK)
        self.assertTrue(plan.should_grow, plan.reason)
        self.assertIn("swap critically low", plan.reason)

    def test_swap_critical_threshold_is_configurable(self):
        ctl = controller(grow_swap_critical=0.25)
        # 20% free: below the configured 25% critical mark, memory still fine.
        plan = ctl.plan_growth(snapshot(32 * 1024, 0.50, 8 * 1024, 0.80), **BIG_DISK)
        self.assertTrue(plan.should_grow, plan.reason)
        # 30% free: above it, and memory is below its own watermark.
        plan = ctl.plan_growth(snapshot(32 * 1024, 0.50, 8 * 1024, 0.70), **BIG_DISK)
        self.assertFalse(plan.should_grow)
        self.assertIn("below watermark", plan.reason)

    def test_growth_fires_well_before_swap_is_exhausted(self):
        """The point of pre-emptive growth: at 50% swap free there is still
        plenty left, and fallocate is instant now but useless mid-crisis."""
        plan = controller().plan_growth(
            snapshot(32 * 1024, 0.82, 8 * 1024, 0.50), **BIG_DISK)
        self.assertTrue(plan.should_grow, plan.reason)

    def test_swapless_machine_trips_the_swap_watermark(self):
        plan = controller().plan_growth(snapshot(8 * 1024, 0.85, 0, 0.0), **BIG_DISK)
        self.assertTrue(plan.should_grow, plan.reason)
        self.assertEqual(plan.grow_bytes, 2 * GB)  # 8 GB * 0.25

    def test_disabled_by_default(self):
        self.assertFalse(SWAP_GROWTH_DEFAULTS["enabled"])
        ctl = SwapController(types.SimpleNamespace(limit_policy={}))
        self.assertFalse(ctl.enabled)
        plan = ctl.plan_growth(snapshot(32 * 1024, 0.99, 8 * 1024, 0.99), **BIG_DISK)
        self.assertFalse(plan.should_grow)
        self.assertIn("disabled", plan.reason)

    def test_plan_is_still_computed_when_it_will_not_act(self):
        """The dry-run log has to show the numbers even when nothing fires,
        otherwise there is nothing to compare against before P3."""
        plan = controller(enabled=False).plan_growth(
            snapshot(32 * 1024, 0.99, 8 * 1024, 0.99), **BIG_DISK)
        self.assertFalse(plan.should_grow)
        self.assertEqual(plan.step_bytes, 8 * GB)
        self.assertGreater(plan.ratio_cap_bytes, 0)
        self.assertGreater(plan.disk_cap_bytes, 0)


class SizingAcrossMachinesTests(unittest.TestCase):
    def test_step_scales_with_memtotal(self):
        """A fixed step would be either useless on a 128 GB box or ruinous on a
        4 GB one.  4/8/32/128 GB with the shipped 0.25 ratio:"""
        expected = {4: 1 * GB, 8: 2 * GB, 32: 8 * GB, 128: 32 * GB}
        for total_gb, step in expected.items():
            plan = controller().plan_growth(
                snapshot(total_gb * 1024, 0.85, total_gb * 256, 0.60), **BIG_DISK)
            self.assertTrue(plan.should_grow, f"{total_gb} GB: {plan.reason}")
            self.assertEqual(plan.grow_bytes, step, f"{total_gb} GB box")

    def test_total_swap_is_capped_at_the_ratio(self):
        """Already at 2x MemTotal: nothing more, however bad it looks."""
        plan = controller().plan_growth(
            snapshot(32 * 1024, 0.99, 64 * 1024, 0.99), **BIG_DISK)
        self.assertFalse(plan.should_grow)
        self.assertIn("cap", plan.reason)
        self.assertEqual(plan.ratio_cap_bytes, 0)

    def test_final_step_is_clipped_to_the_remaining_cap(self):
        """8 GB step, but only 3 GB of ratio headroom left -> grow 3 GB."""
        plan = controller().plan_growth(
            snapshot(32 * 1024, 0.99, 61 * 1024, 0.99), **BIG_DISK)
        self.assertTrue(plan.should_grow, plan.reason)
        self.assertEqual(plan.grow_bytes, 3 * GB)

    def test_disk_reserve_is_the_larger_of_the_share_and_the_floor(self):
        """A flat 20 GB reserve is reasonable on a 500 GB disk and far too thin
        on a 4 TB NAS, so the reserve is max(pct, floor)."""
        big = controller().plan_growth(
            snapshot(32 * 1024, 0.85, 8 * 1024, 0.60),
            disk_total=4000 * GB, disk_available=100 * GB, fstype="ext4")
        self.assertEqual(int(big.details["disk_reserve_bytes"]), 400 * GB)  # 10% wins
        self.assertFalse(big.should_grow)  # 100 GB free < 400 GB reserve
        self.assertIn("disk reserve", big.reason)

        small = controller().plan_growth(
            snapshot(32 * 1024, 0.85, 8 * 1024, 0.60),
            disk_total=100 * GB, disk_available=60 * GB, fstype="ext4")
        self.assertEqual(int(small.details["disk_reserve_bytes"]), 20 * GB)  # floor wins
        self.assertTrue(small.should_grow, small.reason)

    def test_growth_is_clipped_by_free_disk(self):
        plan = controller().plan_growth(
            snapshot(32 * 1024, 0.85, 8 * 1024, 0.60),
            disk_total=100 * GB, disk_available=25 * GB, fstype="ext4")
        self.assertTrue(plan.should_grow, plan.reason)
        self.assertEqual(plan.grow_bytes, 5 * GB)  # 25 GB free - 20 GB reserve


class FilesystemRoutingTests(unittest.TestCase):
    def test_ext4_and_xfs_allow_dynamic_growth(self):
        for fstype in ("ext4", "xfs"):
            plan = controller().plan_growth(
                snapshot(32 * 1024, 0.85, 8 * 1024, 0.60),
                disk_total=4000 * GB, disk_available=3000 * GB, fstype=fstype)
            self.assertTrue(plan.should_grow, f"{fstype}: {plan.reason}")

    def test_btrfs_is_refused_with_the_nodatacow_hint(self):
        plan = controller().plan_growth(
            snapshot(32 * 1024, 0.85, 8 * 1024, 0.60),
            disk_total=4000 * GB, disk_available=3000 * GB, fstype="btrfs")
        self.assertFalse(plan.should_grow)
        self.assertIn("nodatacow", plan.reason)

    def test_zfs_and_network_filesystems_are_refused(self):
        """A zfs swapfile can deadlock the pool under exactly the memory
        pressure we would be reacting to; nfs is worse."""
        for fstype in ("zfs", "nfs4", "cifs", "overlay", ""):
            plan = controller().plan_growth(
                snapshot(32 * 1024, 0.85, 8 * 1024, 0.60),
                disk_total=4000 * GB, disk_available=3000 * GB, fstype=fstype)
            self.assertFalse(plan.should_grow, fstype)
            self.assertFalse(plan.fs_supported, fstype)

    def test_allow_fs_cannot_widen_past_the_safe_set(self):
        """Config narrows the safe set, it must not be able to add to it: a user
        putting zfs in allow_fs would be opting into a deadlock."""
        plan = controller(allow_fs=["ext4", "xfs", "zfs", "btrfs"]).plan_growth(
            snapshot(32 * 1024, 0.85, 8 * 1024, 0.60),
            disk_total=4000 * GB, disk_available=3000 * GB, fstype="zfs")
        self.assertFalse(plan.should_grow)

    def test_allow_fs_can_narrow_the_safe_set(self):
        plan = controller(allow_fs=["ext4"]).plan_growth(
            snapshot(32 * 1024, 0.85, 8 * 1024, 0.60),
            disk_total=4000 * GB, disk_available=3000 * GB, fstype="xfs")
        self.assertFalse(plan.should_grow)


class AnonCeilingTests(unittest.TestCase):
    def test_shortfall_outside_anonymous_memory_is_flagged(self):
        """Slab and page tables cannot be swapped.  Growth still proceeds --
        it costs almost nothing -- but L2 is told not to bother."""
        plan = controller().plan_growth(
            snapshot(32 * 1024, 0.95, 8 * 1024, 0.60, anon_mb=1024), **BIG_DISK)
        self.assertTrue(plan.swap_cannot_help)
        self.assertTrue(plan.should_grow, "the flag must not block growth")

    def test_anon_heavy_shortfall_is_not_flagged(self):
        plan = controller().plan_growth(
            snapshot(32 * 1024, 0.95, 8 * 1024, 0.60, anon_mb=28 * 1024), **BIG_DISK)
        self.assertFalse(plan.swap_cannot_help)


class ParsingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _file(self, name: str, content: str) -> str:
        path = self.root / name
        path.write_text(content)
        return str(path)

    def test_meminfo_is_parsed_from_kb_into_bytes(self):
        path = self._file("meminfo", (
            "MemTotal:       32464896 kB\n"
            "MemFree:        21240832 kB\n"
            "MemAvailable:   27842560 kB\n"
            "Buffers:          817152 kB\n"
            "AnonPages:       2907136 kB\n"
            "SwapTotal:       8388604 kB\n"
            "SwapFree:        7885824 kB\n"
        ))
        snap = SwapController().read_meminfo(path)
        self.assertEqual(snap.mem_total, 32464896 * 1024)
        self.assertEqual(snap.mem_available, 27842560 * 1024)
        self.assertEqual(snap.anon_pages, 2907136 * 1024)
        self.assertEqual(snap.swap_used, (8388604 - 7885824) * 1024)

    def test_missing_meminfo_returns_an_empty_snapshot_not_a_crash(self):
        snap = SwapController().read_meminfo(str(self.root / "nope"))
        self.assertEqual(snap.mem_total, 0)
        self.assertEqual(snap.used_fraction, 0.0)
        self.assertEqual(snap.swap_free_fraction, 0.0)

    def test_empty_meminfo_does_not_trigger_growth(self):
        """A zeroed snapshot must not read as "0 bytes total, therefore 100%
        used" and fire growth off a failed read."""
        plan = controller().plan_growth(MemorySnapshot(), **BIG_DISK)
        self.assertFalse(plan.should_grow)
        self.assertIn("unavailable", plan.reason)

    def test_swaps_table_is_parsed(self):
        path = self._file("swaps", (
            "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
            "/swap.img                               file            8388604 502016  -2\n"
            "/dev/nvme0n1p3                          partition       2097148 0       -3\n"
        ))
        devices = SwapController().read_swap_devices(path)
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0].path, "/swap.img")
        self.assertEqual(devices[0].kind, "file")
        self.assertEqual(devices[0].size_bytes, 8388604 * 1024)
        self.assertEqual(devices[0].priority, -2)
        self.assertEqual(devices[1].kind, "partition")

    def test_filesystem_type_uses_longest_prefix(self):
        """/var/lib on its own mount must not be answered with /'s type."""
        path = self._file("mounts", (
            "/dev/nvme0n1p2 / ext4 rw,relatime 0 0\n"
            "/dev/nvme0n1p9 /var/lib xfs rw,relatime 0 0\n"
            "tmpfs /run tmpfs rw,nosuid 0 0\n"
        ))
        ctl = SwapController()
        self.assertEqual(ctl.filesystem_type("/var/lib/smartune/swap", path), "xfs")
        self.assertEqual(ctl.filesystem_type("/home/nas/x", path), "ext4")
        self.assertEqual(ctl.filesystem_type("/run/user/1000", path), "tmpfs")

    def test_filesystem_type_does_not_match_a_partial_directory_name(self):
        """/var must not claim /variant."""
        path = self._file("mounts", (
            "/dev/a / ext4 rw 0 0\n"
            "/dev/b /var xfs rw 0 0\n"
        ))
        self.assertEqual(SwapController().filesystem_type("/variant/x", path), "ext4")

    def test_disk_space_walks_up_to_an_existing_ancestor(self):
        """The configured swap directory may not exist yet."""
        total, avail = SwapController().disk_space(str(self.root / "a" / "b" / "c"))
        self.assertGreater(total, 0)
        self.assertGreaterEqual(avail, 0)


class ZswapTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.params = Path(self._tmp.name) / "zswap"
        self.params.mkdir()
        for key, value in {"enabled": "N", "compressor": "lzo",
                           "max_pool_percent": "20", "zpool": "zsmalloc"}.items():
            (self.params / key).write_text(value + "\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_plan_orders_compressor_and_pool_before_enabled(self):
        """Enabling first would allocate the pool with the old compressor and
        then force a resize."""
        plan = controller(zswap=True, zswap_compressor="zstd",
                          zswap_pool_percent=40).plan_zswap(str(self.params))
        keys = [os.path.basename(path) for path, _ in plan["writes"]]
        self.assertEqual(keys, ["compressor", "max_pool_percent", "enabled"])

    def test_plan_skips_parameters_already_correct(self):
        plan = controller(zswap=True, zswap_compressor="lzo",
                          zswap_pool_percent=20).plan_zswap(str(self.params))
        keys = [os.path.basename(path) for path, _ in plan["writes"]]
        self.assertEqual(keys, ["enabled"])

    def test_plan_writes_nothing_when_disabled(self):
        plan = controller(zswap=False).plan_zswap(str(self.params))
        self.assertEqual(plan["writes"], [])
        self.assertIn("disabled", plan["reason"])

    def test_kernel_without_zswap_is_reported_not_crashed(self):
        plan = controller(zswap=True).plan_zswap(str(self.params / "absent"))
        self.assertFalse(plan["available"])
        self.assertEqual(plan["writes"], [])

    def test_planning_never_writes(self):
        """P2 is plan-only; the module must not have touched the parameters."""
        before = {p.name: p.read_text() for p in self.params.iterdir()}
        controller(zswap=True, zswap_compressor="zstd").plan_zswap(str(self.params))
        after = {p.name: p.read_text() for p in self.params.iterdir()}
        self.assertEqual(before, after)


class WorstCaseWindowTests(unittest.TestCase):
    """Judging on the instant is judging on a coin flip.

    `stress --vm` without `--vm-keep` loops malloc/touch/free, so MemAvailable
    on the test box swung 43% -> 1% -> 47% inside twenty seconds.  The growth
    check runs once a minute; whether it sees the crisis is pure luck.  Only the
    troughs kill anything, so the window keeps the worst of them.
    """

    def _clocked(self, **overrides):
        now = [1000.0]
        ctl = SwapController(
            types.SimpleNamespace(
                limit_policy={"memory": {"swap_growth": dict({"enabled": True},
                                                             **overrides)}}),
            clock=lambda: now[0])
        return ctl, now

    def test_window_keeps_the_trough_not_the_latest(self):
        ctl, now = self._clocked()
        ctl.sample(snapshot(32 * 1024, 0.95, 8 * 1024, 0.95))  # the trough
        now[0] += 5
        calm = snapshot(32 * 1024, 0.40, 8 * 1024, 0.20)
        worst = ctl.worst_recent(calm)
        self.assertLess(worst.mem_available, calm.mem_available)
        self.assertLess(worst.swap_free, calm.swap_free)
        self.assertEqual(worst.mem_total, calm.mem_total)

    def test_samples_older_than_the_window_are_dropped(self):
        ctl, now = self._clocked(pressure_window_sec=30)
        ctl.sample(snapshot(32 * 1024, 0.95, 8 * 1024, 0.95))
        now[0] += 31
        calm = snapshot(32 * 1024, 0.40, 8 * 1024, 0.20)
        self.assertEqual(ctl.worst_recent(calm), calm)

    def test_an_empty_window_changes_nothing(self):
        ctl, _ = self._clocked()
        calm = snapshot(32 * 1024, 0.40, 8 * 1024, 0.20)
        self.assertEqual(ctl.worst_recent(calm), calm)

    def test_growth_fires_on_a_trough_the_instant_would_have_missed(self):
        """The whole point: the tick that decides lands on a peak, and without
        the window the box sails past the crisis it was sampling through."""
        ctl, now = self._clocked()
        calm = snapshot(32 * 1024, 0.40, 8 * 1024, 0.20)
        self.assertFalse(ctl.plan_growth(calm, **BIG_DISK).should_grow)

        ctl.sample(snapshot(32 * 1024, 0.95, 8 * 1024, 0.95))
        now[0] += 2
        plan = ctl.plan_growth(calm, **BIG_DISK)
        self.assertTrue(plan.should_grow, plan.reason)

    def test_a_degenerate_snapshot_is_not_recorded(self):
        """meminfo unreadable reads as mem_total 0; letting that into the window
        would make every later worst_recent report zero available."""
        ctl, _ = self._clocked()
        ctl.sample(MemorySnapshot(mem_total=0, mem_available=0))
        calm = snapshot(32 * 1024, 0.40, 8 * 1024, 0.20)
        self.assertEqual(ctl.worst_recent(calm), calm)


class ShrinkGateTests(unittest.TestCase):
    """When a grown file may be handed back.

    Only the "has the pressure been gone long enough" half lives here; whether
    an individual file can be faulted back into RAM is release()'s headroom
    gate, covered in test_swap_execute.
    """

    def _clocked(self, **overrides):
        now = [1000.0]
        ctl = SwapController(
            types.SimpleNamespace(
                limit_policy={"memory": {"swap_growth": dict({"enabled": True},
                                                             **overrides)}}),
            clock=lambda: now[0])
        # One grown file, so the gate is not short-circuited by "nothing to do".
        ctl.managed_devices = lambda swaps_path=None: [object()]
        return ctl, now

    def test_shrink_watermark_sits_a_band_below_the_growth_one(self):
        """Shrinking where growth fires would oscillate: grow, drop under the
        line, shrink, cross it again."""
        ctl, _ = self._clocked(grow_mem_watermark=0.80)
        self.assertAlmostEqual(ctl.shrink_watermark(), 0.80 - SHRINK_HYSTERESIS)
        self.assertLess(ctl.shrink_watermark(), 0.80)

    def test_shrink_watermark_follows_a_reconfigured_growth_watermark(self):
        ctl, _ = self._clocked(grow_mem_watermark=0.90)
        self.assertAlmostEqual(ctl.shrink_watermark(), 0.90 - SHRINK_HYSTERESIS)

    def test_watermark_never_goes_negative(self):
        ctl, _ = self._clocked(grow_mem_watermark=0.05)
        self.assertEqual(ctl.shrink_watermark(), 0.0)

    def test_idle_clock_starts_on_the_first_relaxed_sample(self):
        ctl, now = self._clocked(grow_mem_watermark=0.80)   # shrink below 0.65
        self.assertEqual(ctl.relaxed_for(), 0.0)
        ctl.sample(snapshot(32 * 1024, 0.30, 8 * 1024, 0.10))
        now[0] += 120
        self.assertAlmostEqual(ctl.relaxed_for(), 120.0)

    def test_a_single_spike_restarts_the_clock(self):
        """"Relaxed for ten minutes" has to mean uninterrupted, so the timer is
        tested against the raw sample and not the window's worst -- the window
        would keep reporting the spike long after it passed."""
        ctl, now = self._clocked(grow_mem_watermark=0.80)
        ctl.sample(snapshot(32 * 1024, 0.30, 8 * 1024, 0.10))
        now[0] += SHRINK_IDLE_SEC - 1
        ctl.sample(snapshot(32 * 1024, 0.85, 8 * 1024, 0.10))   # one bad reading
        self.assertEqual(ctl.relaxed_for(), 0.0)
        now[0] += 10
        ctl.sample(snapshot(32 * 1024, 0.30, 8 * 1024, 0.10))
        self.assertFalse(ctl.should_shrink()[0])

    def test_shrink_is_refused_until_the_idle_time_is_served(self):
        ctl, now = self._clocked(grow_mem_watermark=0.80)
        ctl.sample(snapshot(32 * 1024, 0.30, 8 * 1024, 0.10))
        now[0] += SHRINK_IDLE_SEC - 1
        allowed, reason = ctl.should_shrink()
        self.assertFalse(allowed)
        self.assertIn("need", reason)

    def test_shrink_is_allowed_once_it_is(self):
        ctl, now = self._clocked(grow_mem_watermark=0.80)
        ctl.sample(snapshot(32 * 1024, 0.30, 8 * 1024, 0.10))
        now[0] += SHRINK_IDLE_SEC
        allowed, reason = ctl.should_shrink()
        self.assertTrue(allowed, reason)

    def test_memory_just_under_the_growth_line_is_not_relaxed(self):
        """The dead band between the two watermarks: growth has stopped firing
        but that is not the same as the pressure being over."""
        ctl, now = self._clocked(grow_mem_watermark=0.80)
        ctl.sample(snapshot(32 * 1024, 0.75, 8 * 1024, 0.10))
        now[0] += SHRINK_IDLE_SEC * 2
        self.assertEqual(ctl.relaxed_for(), 0.0)
        self.assertFalse(ctl.should_shrink()[0])

    def test_nothing_managed_means_nothing_to_shrink(self):
        ctl, now = self._clocked(grow_mem_watermark=0.80)
        ctl.managed_devices = lambda swaps_path=None: []
        ctl.sample(snapshot(32 * 1024, 0.30, 8 * 1024, 0.10))
        now[0] += SHRINK_IDLE_SEC * 2
        allowed, reason = ctl.should_shrink()
        self.assertFalse(allowed)
        self.assertIn("nothing managed", reason)


class StressScenarioTests(unittest.TestCase):
    """`stress --cpu 6 --io 3 --vm 15 --vm-bytes 2g` on the 32 GB test box.

    15 workers x 2 GB = 30 GB of anonymous memory against 31704 MB of RAM, so
    this is the scenario the design is actually aimed at.
    """

    MEM_TOTAL_MB = 31704

    def test_stress_load_triggers_growth_of_one_step(self):
        # ~30 GB resident of 31704 MB, and the existing 8 GB swap nearly full.
        snap = snapshot(self.MEM_TOTAL_MB, 0.95, 8 * 1024, 0.90,
                        anon_mb=30 * 1024)
        plan = controller().plan_growth(
            snap, disk_total=467 * GB, disk_available=318 * GB, fstype="ext4")
        self.assertTrue(plan.should_grow, plan.reason)
        # step = 31704 MB * 0.25; disk reserve = max(46.7 GB, 20 GB) = 46.7 GB,
        # leaving ~271 GB, so the step is the binding constraint.
        self.assertEqual(plan.grow_bytes, int(self.MEM_TOTAL_MB * MB * 0.25))
        self.assertAlmostEqual(plan.grow_bytes / GB, 7.74, places=1)
        # The load is pure anon, so swap is exactly the right tool.
        self.assertFalse(plan.swap_cannot_help)

    def test_repeated_growth_converges_on_the_ratio_cap(self):
        """Six steps of 7.74 GB would overshoot 2x MemTotal; the cap must hold."""
        swap_mb = 8 * 1024
        steps = []
        for _ in range(12):
            snap = snapshot(self.MEM_TOTAL_MB, 0.95, swap_mb, 0.90,
                            anon_mb=30 * 1024)
            plan = controller().plan_growth(
                snap, disk_total=467 * GB, disk_available=318 * GB, fstype="ext4")
            if not plan.should_grow:
                break
            steps.append(plan.grow_bytes)
            swap_mb += plan.grow_bytes // MB
        self.assertGreater(len(steps), 1)
        self.assertLessEqual(swap_mb * MB, self.MEM_TOTAL_MB * MB * 2.0)
        self.assertIn("cap", plan.reason)


class SlopeTests(unittest.TestCase):
    """swap_eta() and mem_rise_rate(): the same 30 s window, read as a rate.

    Both level arms in plan_growth ask "is swap nearly gone yet", which is a
    question that can only be answered too late -- on the incident run swap
    went from 96% free to 4% free in the thirty seconds it took the kernel to
    reach for the OOM killer.  These two ask "how fast is this moving".
    """

    MEM_TOTAL_MB = 31704
    SWAP_MB = 8 * 1024

    def _clocked(self, **overrides):
        now = [1000.0]
        ctl = SwapController(
            types.SimpleNamespace(
                limit_policy={"memory": {"swap_growth": dict({"enabled": True},
                                                             **overrides)}}),
            clock=lambda: now[0])
        return ctl, now

    def _drain(self, ctl, now, *, mem_pct, from_free_mb, to_free_mb, seconds,
               step=1.0):
        """Feed *seconds* of samples with swap free walking linearly.

        Returns the last snapshot, which is what the tick would have handed to
        plan_growth.  Memory is held flat so that the memory watermark is a
        constant the test controls rather than a second moving part.
        """
        ticks = int(seconds / step)
        last = None
        for i in range(ticks + 1):
            free_mb = from_free_mb + (to_free_mb - from_free_mb) * i / ticks
            used_pct = 1.0 - free_mb / self.SWAP_MB
            last = snapshot(self.MEM_TOTAL_MB, mem_pct, self.SWAP_MB, used_pct)
            ctl.sample(last)
            if i < ticks:
                now[0] += step
        return last

    # -- swap_eta ----------------------------------------------------------

    def test_two_adjacent_samples_are_not_a_slope(self):
        """Extrapolating off a 1 s span would make every momentary dip an
        emergency; MIN_SLOPE_SPAN_SEC is the floor that stops it."""
        ctl, now = self._clocked()
        self.assertIsNone(ctl.swap_eta())          # nothing sampled at all
        ctl.sample(snapshot(self.MEM_TOTAL_MB, 0.90, self.SWAP_MB, 0.10))
        self.assertIsNone(ctl.swap_eta())          # one sample: no span
        now[0] += MIN_SLOPE_SPAN_SEC - 1
        ctl.sample(snapshot(self.MEM_TOTAL_MB, 0.90, self.SWAP_MB, 0.30))
        self.assertIsNone(ctl.swap_eta())          # span still under the floor

    def test_swap_filling_back_up_never_runs_out(self):
        """A rate of zero or below extrapolates to never, not to a huge eta."""
        ctl, now = self._clocked()
        self._drain(ctl, now, mem_pct=0.90,
                    from_free_mb=4096, to_free_mb=6144, seconds=10)
        self.assertIsNone(ctl.swap_eta())

    def test_a_steady_drain_matches_the_hand_calculation(self):
        ctl, now = self._clocked()
        # 4096 MB gone in 10 s = 409.6 MB/s, with 4096 MB left.
        self._drain(ctl, now, mem_pct=0.90,
                    from_free_mb=8192, to_free_mb=4096, seconds=10)
        self.assertAlmostEqual(ctl.swap_eta(), 10.0, places=1)

    def test_the_eta_is_pessimistic_about_a_pause(self):
        """Same direction as worst_recent(): the numerator is the *lowest*
        SwapFree in the window, so a drain that briefly gives some back still
        reports the short answer.  Being early costs a 91 ms fallocate."""
        ctl, now = self._clocked()
        self._drain(ctl, now, mem_pct=0.90,
                    from_free_mb=8192, to_free_mb=2048, seconds=8)
        now[0] += 2
        ctl.sample(snapshot(self.MEM_TOTAL_MB, 0.90, self.SWAP_MB, 1 - 3072 / self.SWAP_MB))
        # Rate is read first-to-last (5120 MB over 10 s = 512 MB/s) but the
        # remaining figure is the 2048 MB trough, not the 3072 MB present.
        self.assertAlmostEqual(ctl.swap_eta(), 2048 / 512, places=1)

    def test_a_swapless_machine_has_no_eta(self):
        """Dividing a nonexistent reserve by a drain rate answers nothing; the
        swapless case is already handled by the swap watermark arm."""
        ctl, now = self._clocked()
        for _ in range(11):
            ctl.sample(MemorySnapshot(mem_total=self.MEM_TOTAL_MB * MB,
                                      mem_available=1 * GB, anon_pages=8 * GB,
                                      swap_total=0, swap_free=0))
            now[0] += 1
        self.assertIsNone(ctl.swap_eta())

    # -- arm C -------------------------------------------------------------

    def test_memory_full_and_swap_going_fast_grows_while_swap_is_still_roomy(self):
        """The arm that would have saved the incident: at 80% swap free both
        level arms say "roomy", and the slope says 40 s to empty."""
        ctl, now = self._clocked()
        last = self._drain(ctl, now, mem_pct=0.85,
                           from_free_mb=self.SWAP_MB,
                           to_free_mb=int(self.SWAP_MB * 0.80), seconds=10)
        eta = ctl.swap_eta()
        self.assertLess(eta, GROW_LEAD_SEC)
        plan = ctl.plan_growth(last, **BIG_DISK)
        self.assertTrue(plan.should_grow, plan.reason)
        self.assertIn("draining", plan.reason)

    def test_a_slow_drain_is_left_alone(self):
        """A NAS quietly paging a cold working set out to a large swap must not
        keep growing the file: hours of eta is not a crisis."""
        ctl, now = self._clocked()
        last = self._drain(ctl, now, mem_pct=0.85,
                           from_free_mb=self.SWAP_MB,
                           to_free_mb=self.SWAP_MB - 64, seconds=10)
        self.assertGreater(ctl.swap_eta(), GROW_LEAD_SEC)
        plan = ctl.plan_growth(last, **BIG_DISK)
        self.assertFalse(plan.should_grow)
        self.assertIn("roomy", plan.reason)

    def test_a_fast_drain_with_memory_free_is_left_alone(self):
        """Swap emptying quickly while RAM has room is a working set being
        faulted back in, which ends by itself.  The arm is deliberately an AND:
        it is memory full *and* swap going that has no ending but a kill."""
        ctl, now = self._clocked()
        last = self._drain(ctl, now, mem_pct=0.40,
                           from_free_mb=self.SWAP_MB,
                           to_free_mb=int(self.SWAP_MB * 0.80), seconds=10)
        self.assertLess(ctl.swap_eta(), GROW_LEAD_SEC)
        plan = ctl.plan_growth(last, **BIG_DISK)
        self.assertFalse(plan.should_grow)
        self.assertIn("below watermark", plan.reason)

    def test_the_incident_replays_as_a_growth_thirty_seconds_before_the_kill(self):
        """22:34:56, the tick that did nothing.

        mem was 94%, swap 8 GB and ~89% free, and both level arms refused --
        arm A wants <= 10% free, arm B wants <= 50%.  Swap was draining at the
        90 MB/s it averaged over the whole episode (560 MB -> 7844 MB used in
        81 s), which is ~81 s to empty.  The oom-killer fired at 22:35:25.
        """
        ctl, now = self._clocked()
        last = self._drain(ctl, now, mem_pct=0.94,
                           from_free_mb=8192, to_free_mb=8192 - 900, seconds=10)
        # Precondition: this is genuinely the state the level arms called safe.
        self.assertGreater(last.swap_free_fraction, 0.50)
        self.assertAlmostEqual(ctl.swap_eta(), (8192 - 900) / 90.0, delta=1.0)
        plan = ctl.plan_growth(last, disk_total=467 * GB,
                               disk_available=318 * GB, fstype="ext4")
        self.assertTrue(plan.should_grow, plan.reason)
        self.assertIn("draining", plan.reason)
        self.assertEqual(plan.grow_bytes, int(self.MEM_TOTAL_MB * MB * 0.25))

    # -- mem_rise_rate -----------------------------------------------------

    def test_mem_rise_rate_needs_a_span_too(self):
        ctl, now = self._clocked()
        self.assertIsNone(ctl.mem_rise_rate())
        ctl.sample(snapshot(self.MEM_TOTAL_MB, 0.20, self.SWAP_MB, 0.0))
        now[0] += MIN_SLOPE_SPAN_SEC - 1
        ctl.sample(snapshot(self.MEM_TOTAL_MB, 0.60, self.SWAP_MB, 0.0))
        self.assertIsNone(ctl.mem_rise_rate())

    def test_memory_being_given_back_reads_negative(self):
        """Callers compare against a positive threshold, so a release simply
        fails it -- no special case needed at either end."""
        ctl, now = self._clocked()
        ctl.sample(snapshot(self.MEM_TOTAL_MB, 0.80, self.SWAP_MB, 0.0))
        now[0] += 10
        ctl.sample(snapshot(self.MEM_TOTAL_MB, 0.30, self.SWAP_MB, 0.0))
        self.assertLess(ctl.mem_rise_rate(), 0)

    def test_the_incident_replays_as_a_warning_fifteen_seconds_early(self):
        """22:34:35 -> 22:34:40, memory 16% -> 55%.

        The pressure *score* read 0.00 then 0.01 across this step, because its
        memory term is a deliberately sharp gate; raw usage had already moved
        39 points.  That is the whole reason this helper exists.
        """
        ctl, now = self._clocked()
        ctl.sample(snapshot(self.MEM_TOTAL_MB, 0.16, self.SWAP_MB, 0.0))
        now[0] += 5
        ctl.sample(snapshot(self.MEM_TOTAL_MB, 0.55, self.SWAP_MB, 0.0))
        rise = ctl.mem_rise_rate()
        self.assertGreaterEqual(rise, MEM_RISE_TRIGGER)
        self.assertAlmostEqual(rise, 0.39, places=2)

    def test_an_ordinary_climb_stays_under_the_trigger(self):
        """A NAS filling its page cache over half a minute is not news."""
        ctl, now = self._clocked()
        ctl.sample(snapshot(self.MEM_TOTAL_MB, 0.50, self.SWAP_MB, 0.0))
        now[0] += 20
        ctl.sample(snapshot(self.MEM_TOTAL_MB, 0.55, self.SWAP_MB, 0.0))
        self.assertLess(ctl.mem_rise_rate(), MEM_RISE_TRIGGER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
