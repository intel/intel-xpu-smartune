# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for L0 swap execution (P3: the path that touches the system).

Everything destructive is exercised against a temp directory with a fake
command runner, so mkswap/swapon/swapoff are never actually invoked.  The two
properties worth most here are the ones that cost real data if wrong:

* nothing outside the managed directory, and nothing not matching swap-NN.img,
  is ever a candidate for swapoff or rm -- /swap.img belongs to the distro;
* the retry wraps allocate -> mkswap -> swapon as one chain, because the field
  failure is fallocate succeeding and swapon then refusing the file.

Run: PYTHONDONTWRITEBYTECODE=1 python3 balancer/test/test_swap_execute.py
"""

import logging
import os
import stat
import sys
import tempfile
import types
import unittest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BALANCER_ROOT = os.path.join(_REPO_ROOT, "balancer")
for _p in (_REPO_ROOT, _BALANCER_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_logger_module = types.ModuleType("utils.logger")
_test_logger = logging.getLogger("test_swap_execute")
_logger_module.logger = _test_logger
_logger_module.get_logger = lambda name=None: _test_logger
_logger_module.current_log_context = lambda: {}
sys.modules["utils.logger"] = _logger_module

from controller.swap import (  # noqa: E402
    GB,
    MB,
    SWAPOFF_TIMEOUT_SEC,
    GrowthPlan,
    MemorySnapshot,
    SwapController,
)


class FakeRunner:
    """Records argv lists and replays scripted exit codes.

    ``fail`` maps a command name to the number of leading invocations that
    should fail, which is how the "fallocate worked but swapon refused" case is
    posed.
    """

    def __init__(self, fail=None):
        self.calls = []
        self.fail = dict(fail or {})

    def __call__(self, argv, timeout=60):
        self.calls.append((list(argv), timeout))
        name = argv[0]
        if self.fail.get(name, 0) > 0:
            self.fail[name] -= 1
            return 1, "", f"{name}: scripted failure"
        return 0, "", ""

    def names(self):
        return [call[0][0] for call in self.calls]


class Harness(SwapController):
    """SwapController pinned to a temp dir on a pretend ext4 volume."""

    def __init__(self, directory, runner, fstype="ext4", **overrides):
        cfg = {"enabled": True, "path": directory}
        cfg.update(overrides)
        super().__init__(
            types.SimpleNamespace(limit_policy={"memory": {"swap_growth": cfg}}),
            runner=runner)
        self._fstype = fstype

    def filesystem_type(self, target, mounts_path=None):
        return self._fstype


def plan_for(nbytes, reserve=0):
    return GrowthPlan(should_grow=True, grow_bytes=nbytes,
                      reason="test", fstype="ext4", fs_supported=True,
                      details={"disk_reserve_bytes": reserve})


def snapshot(mem_available_mb, swap_total_mb=8 * 1024, swap_used_mb=0):
    total = 32 * 1024 * MB
    return MemorySnapshot(
        mem_total=total, mem_available=mem_available_mb * MB,
        mem_free=mem_available_mb * MB, anon_pages=swap_used_mb * MB,
        swap_total=swap_total_mb * MB,
        swap_free=(swap_total_mb - swap_used_mb) * MB)


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self._tmp.name, "swap")
        self.addCleanup(self._tmp.cleanup)

    def touch(self, name, size=0):
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(b"\0" * size)
        return path


class NamingTests(TempDirTest):
    def test_first_file_is_swap_01(self):
        ctl = Harness(self.dir, FakeRunner())
        self.assertEqual(os.path.basename(ctl.next_managed_path()), "swap-01.img")

    def test_names_fill_the_first_gap(self):
        self.touch("swap-01.img")
        self.touch("swap-03.img")
        ctl = Harness(self.dir, FakeRunner())
        self.assertEqual(os.path.basename(ctl.next_managed_path()), "swap-02.img")

    def test_foreign_files_are_not_ours(self):
        """Only swap-NN.img is ours.  Anything else an admin dropped in the
        directory must survive both the naming and the cleanup logic."""
        self.touch("swap.img")
        self.touch("notes.txt")
        self.touch("swap-1.img")        # one digit: not the pattern
        ctl = Harness(self.dir, FakeRunner())
        self.assertEqual(ctl.managed_files_on_disk(), [])
        self.assertEqual(os.path.basename(ctl.next_managed_path()), "swap-01.img")


class ManagedDeviceTests(TempDirTest):
    def write_swaps(self, *rows):
        path = os.path.join(self._tmp.name, "swaps")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n")
            for row in rows:
                handle.write(row + "\n")
        return path

    def test_distro_swap_is_never_ours(self):
        """The single most damaging possible bug in this module would be
        swapoff'ing /swap.img, which the installer created and fstab mounts."""
        swaps = self.write_swaps(
            "/swap.img\t\t\t\tfile\t\t8388604\t\t1310720\t-1",
            f"{self.dir}/swap-01.img\tfile\t\t8388604\t\t0\t\t-2",
        )
        ctl = Harness(self.dir, FakeRunner())
        ours = ctl.managed_devices(swaps)
        self.assertEqual([d.path for d in ours], [f"{self.dir}/swap-01.img"])

    def test_matching_name_in_a_foreign_directory_is_not_ours(self):
        swaps = self.write_swaps("/tmp/elsewhere/swap-01.img\tfile\t1024\t0\t-2")
        ctl = Harness(self.dir, FakeRunner())
        self.assertEqual(ctl.managed_devices(swaps), [])


class GrowTests(TempDirTest):
    SIZE = 2 * MB   # tiny: these tests really do allocate the file

    def test_dry_run_creates_nothing(self):
        runner = FakeRunner()
        ctl = Harness(self.dir, runner)
        result = ctl.grow(plan_for(self.SIZE), dry_run=True)
        self.assertTrue(result.ok)
        self.assertTrue(result.dry_run)
        self.assertEqual(runner.calls, [])
        self.assertFalse(os.path.exists(self.dir))

    def test_live_run_allocates_then_mkswap_then_swapon(self):
        runner = FakeRunner()
        ctl = Harness(self.dir, runner)
        result = ctl.grow(plan_for(self.SIZE), dry_run=False)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.allocator, "fallocate")
        self.assertEqual(runner.names(), ["mkswap", "swapon"])
        self.assertEqual(runner.calls[1][0], ["swapon", result.path])
        self.assertEqual(os.path.getsize(result.path), self.SIZE)

    def test_swapon_is_not_given_a_priority(self):
        """`--priority -2` was silently ignored by swapon (it only honours a
        non-negative priority), so all managed files came up at -1 alongside the
        distro's.  Asking for a non-negative one instead would be worse: it
        would put ours *ahead* of the distro's swap."""
        runner = FakeRunner()
        ctl = Harness(self.dir, runner)
        ctl.grow(plan_for(self.SIZE), dry_run=False)
        self.assertNotIn("--priority", runner.calls[1][0])

    def test_file_is_never_world_readable_even_briefly(self):
        """Created 0600 by open(), not chmod'ed after the fact: a swapfile that
        was briefly 0644 had its contents briefly readable by everyone."""
        ctl = Harness(self.dir, FakeRunner())
        result = ctl.grow(plan_for(self.SIZE), dry_run=False)
        self.assertEqual(stat.S_IMODE(os.stat(result.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)

    def test_swapon_failure_retries_the_whole_chain_not_just_the_allocator(self):
        """The real field failure: fallocate succeeds, swapon rejects the
        unwritten extents.  Retrying the allocation alone would never recover,
        so the fallback has to redo allocate -> mkswap -> swapon.
        """
        runner = FakeRunner(fail={"swapon": 1})
        ctl = Harness(self.dir, runner)
        result = ctl.grow(plan_for(self.SIZE), dry_run=False)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.allocator, "write")
        self.assertEqual(runner.names(), ["mkswap", "swapon", "mkswap", "swapon"])
        self.assertTrue(os.path.exists(result.path))

    def test_both_allocators_failing_leaves_no_file_behind(self):
        runner = FakeRunner(fail={"swapon": 2})
        ctl = Harness(self.dir, runner)
        result = ctl.grow(plan_for(self.SIZE), dry_run=False)
        self.assertFalse(result.ok)
        self.assertFalse(os.path.exists(result.path))
        self.assertEqual(len(result.steps), 2)

    def test_mkswap_failure_also_falls_back(self):
        runner = FakeRunner(fail={"mkswap": 1})
        ctl = Harness(self.dir, runner)
        result = ctl.grow(plan_for(self.SIZE), dry_run=False)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(runner.names(), ["mkswap", "mkswap", "swapon"])

    def test_unsupported_filesystem_is_refused_at_execution_time(self):
        """The plan may predate the directory existing, so the fstype answer can
        change between planning and acting.  zfs swapfiles deadlock the pool."""
        runner = FakeRunner()
        ctl = Harness(self.dir, runner, fstype="zfs")
        result = ctl.grow(plan_for(self.SIZE), dry_run=False)
        self.assertFalse(result.ok)
        self.assertIn("zfs", result.reason)
        self.assertEqual(runner.calls, [])

    def test_disk_filling_up_between_plan_and_act_is_refused(self):
        runner = FakeRunner()
        ctl = Harness(self.dir, runner)
        # A reserve larger than the whole volume: nothing can satisfy it.
        result = ctl.grow(plan_for(self.SIZE, reserve=10 * 1024 * GB), dry_run=False)
        self.assertFalse(result.ok)
        self.assertIn("disk shrank", result.reason)
        self.assertEqual(runner.calls, [])

    def test_a_plan_that_says_no_is_not_second_guessed(self):
        runner = FakeRunner()
        ctl = Harness(self.dir, runner)
        result = ctl.grow(GrowthPlan(should_grow=False, reason="swap still roomy"),
                          dry_run=False)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "swap still roomy")
        self.assertEqual(runner.calls, [])

    def test_a_second_growth_does_not_clobber_the_first(self):
        ctl = Harness(self.dir, FakeRunner())
        first = ctl.grow(plan_for(self.SIZE), dry_run=False)
        second = ctl.grow(plan_for(self.SIZE), dry_run=False)
        self.assertNotEqual(first.path, second.path)
        self.assertTrue(os.path.exists(first.path))
        self.assertTrue(os.path.exists(second.path))


class ReleaseTests(TempDirTest):
    def write_swaps(self, *rows):
        path = os.path.join(self._tmp.name, "swaps")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("Filename\tType\tSize\tUsed\tPriority\n")
            for row in rows:
                handle.write(row + "\n")
        return path

    def controller_with(self, rows, snap, fail=None):
        swaps = self.write_swaps(*rows)
        runner = FakeRunner(fail=fail)
        ctl = Harness(self.dir, runner)
        ctl.managed_devices = lambda swaps_path=swaps: \
            SwapController.managed_devices(ctl, swaps_path)
        ctl.read_meminfo = lambda path=None: snap
        return ctl, runner

    def test_unsafe_release_is_skipped(self):
        """swapoff faults every page back into RAM.  Doing that with 1 GB free
        and 4 GB parked in swap turns a shrink into an OOM."""
        self.touch("swap-01.img")
        ctl, runner = self.controller_with(
            [f"{self.dir}/swap-01.img\tfile\t8388604\t4194304\t-2"],
            snapshot(mem_available_mb=1024))
        results = ctl.release(dry_run=False)
        self.assertFalse(results[0].ok)
        self.assertIn("unsafe", results[0].reason)
        self.assertEqual(runner.calls, [])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "swap-01.img")))

    def test_safe_release_swaps_off_and_deletes(self):
        self.touch("swap-01.img")
        ctl, runner = self.controller_with(
            [f"{self.dir}/swap-01.img\tfile\t8388604\t1048576\t-2"],  # 1 GB used
            snapshot(mem_available_mb=16 * 1024))
        results = ctl.release(dry_run=False)
        self.assertTrue(results[0].ok, results[0].reason)
        self.assertEqual(runner.names(), ["swapoff"])
        self.assertEqual(runner.calls[0][1], SWAPOFF_TIMEOUT_SEC)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "swap-01.img")))

    def test_headroom_is_budgeted_across_all_files_not_per_file(self):
        """Three 1 GB files with 1.5 GB free: the first fits, the rest must not
        each be checked against the same untouched MemAvailable."""
        for name in ("swap-01.img", "swap-02.img", "swap-03.img"):
            self.touch(name)
        rows = [f"{self.dir}/{n}\tfile\t2097152\t1048576\t-2"
                for n in ("swap-01.img", "swap-02.img", "swap-03.img")]
        ctl, runner = self.controller_with(rows, snapshot(mem_available_mb=1536))
        results = ctl.release(dry_run=False)
        released = [r for r in results if r.ok and r.path.endswith(".img")
                    and "unsafe" not in r.reason]
        self.assertEqual(runner.names(), ["swapoff"])
        self.assertEqual(sum(1 for r in results if "unsafe" in r.reason), 2)
        self.assertEqual(len(released), 1)

    def test_failed_swapoff_keeps_the_file(self):
        self.touch("swap-01.img")
        ctl, runner = self.controller_with(
            [f"{self.dir}/swap-01.img\tfile\t2097152\t1048576\t-2"],
            snapshot(mem_available_mb=16 * 1024), fail={"swapoff": 1})
        results = ctl.release(dry_run=False)
        self.assertFalse(results[0].ok)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "swap-01.img")))

    def test_orphan_files_are_removed(self):
        """What a reboot leaves behind: the file is still on disk but no longer
        swapped on, because we deliberately never wrote it to /etc/fstab."""
        self.touch("swap-07.img", size=16)
        ctl, _ = self.controller_with([], snapshot(mem_available_mb=16 * 1024))
        results = ctl.release(dry_run=False)
        self.assertTrue(all(r.ok for r in results))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "swap-07.img")))

    def test_orphan_cleanup_leaves_foreign_files_alone(self):
        self.touch("swap.img", size=16)
        self.touch("README", size=4)
        ctl, _ = self.controller_with([], snapshot(mem_available_mb=16 * 1024))
        ctl.release(dry_run=False)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "swap.img")))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "README")))

    def test_dry_run_releases_nothing(self):
        self.touch("swap-01.img")
        ctl, runner = self.controller_with(
            [f"{self.dir}/swap-01.img\tfile\t2097152\t1048576\t-2"],
            snapshot(mem_available_mb=16 * 1024))
        results = ctl.release(dry_run=True)
        self.assertTrue(results[0].ok)
        self.assertEqual(runner.calls, [])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "swap-01.img")))

    def test_only_unused_takes_the_empty_file_and_leaves_the_held_one(self):
        """The shutdown rule.  Plenty of headroom, so the in-use file is skipped
        on the only_unused test alone and not because it would be unsafe."""
        self.touch("swap-01.img")
        self.touch("swap-02.img")
        ctl, runner = self.controller_with(
            [f"{self.dir}/swap-01.img\tfile\t8388604\t16260\t-2",
             f"{self.dir}/swap-02.img\tfile\t8388604\t0\t-2"],
            snapshot(mem_available_mb=24 * 1024))
        results = ctl.release(dry_run=False, only_unused=True)
        by_path = {os.path.basename(r.path): r for r in results}
        self.assertTrue(by_path["swap-02.img"].ok)
        self.assertFalse(by_path["swap-01.img"].ok)
        self.assertIn("in use", by_path["swap-01.img"].reason)
        self.assertEqual(runner.names(), ["swapoff"])
        self.assertEqual(runner.calls[0][0][1],
                         os.path.join(self.dir, "swap-02.img"))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "swap-01.img")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "swap-02.img")))

    def test_only_unused_with_everything_held_does_nothing_at_all(self):
        self.touch("swap-01.img")
        ctl, runner = self.controller_with(
            [f"{self.dir}/swap-01.img\tfile\t8388604\t1048576\t-2"],
            snapshot(mem_available_mb=24 * 1024))
        results = ctl.release(dry_run=False, only_unused=True)
        self.assertEqual(runner.calls, [])
        self.assertFalse(any(r.ok for r in results))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "swap-01.img")))

    def test_limit_stops_after_one_file(self):
        """The idle path hands back one file per round rather than starting a
        multi-gigabyte fault storm in a single call."""
        for name in ("swap-01.img", "swap-02.img", "swap-03.img"):
            self.touch(name)
        rows = [f"{self.dir}/{n}\tfile\t2097152\t1048576\t-2"
                for n in ("swap-01.img", "swap-02.img", "swap-03.img")]
        ctl, runner = self.controller_with(rows, snapshot(mem_available_mb=16 * 1024))
        results = ctl.release(dry_run=False, limit=1)
        self.assertEqual(runner.names(), ["swapoff"])
        self.assertEqual(sum(1 for r in results if "deferred" in r.reason), 2)
        remaining = sorted(n for n in os.listdir(self.dir))
        self.assertEqual(remaining, ["swap-02.img", "swap-03.img"])

    def test_a_stuck_file_does_not_block_the_cheap_one(self):
        """8 GB parked in one file and 1 GB in another with 4 GB free: the big
        one can never be faulted back, and must not stop the small one going.

        Emptiest-first is what guarantees this, and it also means an unsafe file
        can never consume the round's limit -- being unsafe requires holding the
        most pages, which puts it last in the order.
        """
        self.touch("swap-01.img")
        self.touch("swap-02.img")
        ctl, runner = self.controller_with(
            [f"{self.dir}/swap-01.img\tfile\t16777216\t8388608\t-2",
             f"{self.dir}/swap-02.img\tfile\t2097152\t1048576\t-2"],
            snapshot(mem_available_mb=4 * 1024))
        results = ctl.release(dry_run=False, limit=1)
        self.assertEqual(runner.names(), ["swapoff"])
        self.assertEqual(runner.calls[0][0][1],
                         os.path.join(self.dir, "swap-02.img"))
        held = next(r for r in results if r.path.endswith("swap-01.img"))
        self.assertFalse(held.ok)
        self.assertIn("deferred", held.reason)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "swap-01.img")))

    def test_emptiest_file_goes_first(self):
        """Taking the cheap one first leaves the most headroom for the next."""
        self.touch("swap-01.img")
        self.touch("swap-02.img")
        ctl, runner = self.controller_with(
            [f"{self.dir}/swap-01.img\tfile\t2097152\t1048576\t-2",
             f"{self.dir}/swap-02.img\tfile\t2097152\t0\t-2"],
            snapshot(mem_available_mb=16 * 1024))
        ctl.release(dry_run=False)
        self.assertEqual([call[0][1] for call in runner.calls],
                         [os.path.join(self.dir, "swap-02.img"),
                          os.path.join(self.dir, "swap-01.img")])

    def test_only_unused_still_cleans_orphans(self):
        """A reboot leaves files on disk that nothing swapped back on.  Those
        cost no fault-in at all, so the shutdown rule must not spare them."""
        self.touch("swap-07.img", size=16)
        ctl, _ = self.controller_with([], snapshot(mem_available_mb=16 * 1024))
        ctl.release(dry_run=False, only_unused=True)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "swap-07.img")))


class ZswapApplyTests(TempDirTest):
    def params(self, **values):
        directory = os.path.join(self._tmp.name, "zswap")
        os.makedirs(directory, exist_ok=True)
        for key, value in values.items():
            with open(os.path.join(directory, key), "w", encoding="utf-8") as handle:
                handle.write(value)
        return directory

    def read(self, directory, key):
        with open(os.path.join(directory, key), "r", encoding="utf-8") as handle:
            return handle.read().strip()

    def test_writes_compressor_and_pool_before_enabling(self):
        """The pool is allocated when enabled flips; setting the compressor
        afterwards would mean allocating it with lzo and then rebuilding."""
        directory = self.params(compressor="lzo", max_pool_percent="20", enabled="N")
        ctl = Harness(self.dir, FakeRunner(), zswap=True,
                      zswap_compressor="zstd", zswap_pool_percent=40)
        result = ctl.apply_zswap(dry_run=False, params_dir=directory)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.read(directory, "compressor"), "zstd")
        self.assertEqual(self.read(directory, "max_pool_percent"), "40")
        self.assertEqual(self.read(directory, "enabled"), "Y")
        self.assertEqual([s.split(":")[0] for s in result.steps],
                         ["compressor", "max_pool_percent", "enabled"])

    def test_previous_values_are_recorded_for_reversal(self):
        directory = self.params(compressor="lzo", max_pool_percent="20", enabled="N")
        ctl = Harness(self.dir, FakeRunner(), zswap=True, zswap_compressor="zstd")
        result = ctl.apply_zswap(dry_run=True, params_dir=directory)
        self.assertIn("compressor: lzo -> zstd", result.steps)
        self.assertEqual(self.read(directory, "compressor"), "lzo")

    def test_disabled_in_config_writes_nothing(self):
        directory = self.params(compressor="lzo", enabled="N")
        ctl = Harness(self.dir, FakeRunner(), zswap=False)
        result = ctl.apply_zswap(dry_run=False, params_dir=directory)
        self.assertTrue(result.ok)
        self.assertEqual(self.read(directory, "enabled"), "N")

    def test_kernel_without_zswap_is_not_an_error(self):
        ctl = Harness(self.dir, FakeRunner(), zswap=True)
        result = ctl.apply_zswap(
            dry_run=False, params_dir=os.path.join(self._tmp.name, "absent"))
        self.assertTrue(result.ok)
        self.assertIn("not available", result.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
