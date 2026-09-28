# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the cgroup v2 MemoryController and the L2 reclaim gates.

Everything runs against a temporary directory standing in for cgroupfs, so no
root and no real cgroup is needed.

The behaviour pinned down here was measured on kernel 7.0 against a cgroup
holding 304 MB anon + 300 MB page cache, asked to reclaim 200 MB:

    echo 209715200                   -> anon 304->304, file 300->100, swap 0
    echo "209715200 swappiness=200"  -> anon 304->201, file  98->0,   swap 0->103

i.e. omitting swappiness reclaims page cache *only*.  test_bare_reclaim_* and
test_unsupported_* exist to stop that default ever coming back.

Run: PYTHONDONTWRITEBYTECODE=1 python3 balancer/test/test_memory_reclaim.py
"""

import errno
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BALANCER_ROOT = os.path.join(_REPO_ROOT, "balancer")
for _p in (_REPO_ROOT, _BALANCER_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# The module under test pulls in app_utils (psutil, peewee) and the pressure
# monitor (yaml) transitively.  None of that is on the reclaim path, so it is
# stubbed out rather than required -- this keeps the test runnable without the
# service venv.  write_cgroup_file's own path-containment guard is covered by
# test_cgroup_file_write.py; every test below patches the write anyway.
_logger_module = types.ModuleType("utils.logger")
_test_logger = logging.getLogger("test_memory_reclaim")
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


def _passthrough_write(content, target_file, allowed_roots=None):
    with open(target_file, "w", encoding="utf-8") as handle:
        handle.write(f"{content}\n")


_stub("utils.app_utils", write_cgroup_file=_passthrough_write)
_stub("config.config", b_config=types.SimpleNamespace(
    cgroup_mount="/sys/fs/cgroup", limit_policy={}))
_stub("monitor.monitor_api", SystemPressureMonitor=object)
_stub("controller.controller", Controller=object)
_stub("controller.governor", GovernorController=object)
_stub("controller.cpu", CPUController=object)

from controller.memory import (  # noqa: E402
    DEFAULT_RECLAIM_SWAPPINESS,
    SWAPPINESS_MAX,
    MemoryController,
    ReclaimOutcome,
)
from controller.control_manager import RECLAIM_DEFAULTS, ControlManager  # noqa: E402


class FakeCgroup:
    """A temporary directory laid out like a cgroup v2 leaf."""

    def __init__(self, root: Path, name: str = "app.slice/demo.scope"):
        self.root = root
        self.name = name
        self.path = root / name
        self.path.mkdir(parents=True, exist_ok=True)

    def write(self, param: str, content: str) -> None:
        (self.path / param).write_text(content)

    def set_state(self, anon_mb=0, file_mb=0, current_mb=None, swap_mb=0,
                  usage_usec=0, high_events=0):
        mb = 1024 * 1024
        if current_mb is None:
            current_mb = anon_mb + file_mb
        self.write("memory.stat", f"anon {anon_mb * mb}\nfile {file_mb * mb}\nslab 0\n")
        self.write("memory.current", str(current_mb * mb))
        self.write("memory.swap.current", str(swap_mb * mb))
        self.write("cpu.stat", f"usage_usec {usage_usec}\nuser_usec 0\nsystem_usec 0\n")
        self.write("memory.events", f"low 0\nhigh {high_events}\nmax 0\noom 0\n")


def make_controller(mount: str) -> MemoryController:
    """MemoryController without the systemctl call its __init__ normally makes."""
    with mock.patch.object(MemoryController, "set_managed_oom_pressure", return_value=True):
        return MemoryController(mount)


def make_manager(controller: MemoryController, reclaim_cfg: dict) -> ControlManager:
    """A ControlManager with only the pieces L2 touches.

    ControlManager.__init__ spins up the pressure monitor and every other
    controller; bypassing it keeps these tests to the reclaim path.
    """
    manager = ControlManager.__new__(ControlManager)
    manager.memory = controller
    manager.config = types.SimpleNamespace(
        limit_policy={"memory": {"reclaim": reclaim_cfg}}
    )
    manager._executor = None  # only reclaim_memory_async needs it
    return manager


class PathLayoutTests(unittest.TestCase):
    def test_full_path_has_no_controller_segment(self):
        """v2 has a unified hierarchy: /sys/fs/cgroup/<cgroup>, not .../memory/<cgroup>.

        ControllerBase.get_full_path still builds the v1 layout, so
        MemoryController overrides it.  If that override is ever dropped every
        read here silently returns None instead of failing loudly.
        """
        controller = make_controller("/sys/fs/cgroup")
        self.assertEqual(
            controller.get_full_path("app.slice/demo.scope"),
            "/sys/fs/cgroup/app.slice/demo.scope",
        )
        self.assertNotIn("/memory/", controller.get_full_path("app.slice/demo.scope"))

    def test_leading_slash_is_tolerated(self):
        controller = make_controller("/sys/fs/cgroup")
        self.assertEqual(
            controller.get_full_path("/app.slice/demo.scope"),
            "/sys/fs/cgroup/app.slice/demo.scope",
        )


class PathLookupTests(unittest.TestCase):
    """A bare unit name does not encode where the unit lives.

    A terminal scope sits at user.slice/user-1000.slice/user@1000.service/
    app.slice/app-org.gnome.Terminal.slice/<unit>.scope.  Joining the bare name
    straight onto the mount produced a path that never existed, so every L2
    read missed and reclaim never once ran -- it only ever logged "cgroup gone".
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mount = self.tmp.name
        self.nested = os.path.join(
            self.mount, "user.slice", "user-1000.slice", "demo.scope")
        os.makedirs(self.nested)

    def test_bare_name_is_found_below_the_mount(self):
        controller = make_controller(self.mount)
        self.assertEqual(controller.get_full_path("demo.scope"), self.nested)

    def test_lookup_is_cached_and_reverified(self):
        """The walk is ~13 ms; the cache makes repeat ticks ~5 us.  A stale
        entry must not outlive the directory, or a recycled unit name would be
        written through to a path that no longer exists."""
        controller = make_controller(self.mount)
        self.assertEqual(controller.get_full_path("demo.scope"), self.nested)

        with mock.patch.object(controller, "_locate",
                               side_effect=AssertionError("walked again")):
            self.assertEqual(controller.get_full_path("demo.scope"), self.nested)

        os.rmdir(self.nested)
        self.assertEqual(controller.get_full_path("demo.scope"),
                         os.path.join(self.mount, "demo.scope"))

    def test_missing_name_stays_under_the_mount(self):
        """Callers read a non-existent path as 'this cgroup is gone', which by
        then is the truthful answer."""
        controller = make_controller(self.mount)
        path = controller.get_full_path("ghost.scope")
        self.assertEqual(path, os.path.join(self.mount, "ghost.scope"))
        self.assertFalse(os.path.exists(path))

    def test_traversal_is_refused_before_the_join(self):
        """os.path.join(mount, '../../etc') resolves to a real directory, and
        exists() would then report True for /etc -- so the id has to be refused
        before it is joined, not after.  Note '..' matches CGROUP_ID_RE on its
        own (both dots are in the class), hence the by-name check."""
        controller = make_controller(self.mount)
        for bad in ("../../etc", "foo/../../etc", "..", ".", "", "a b", "x\ny"):
            path = controller.get_full_path(bad)
            self.assertEqual(path, os.path.join(self.mount, "!invalid"),
                             f"{bad!r} escaped the guard")
            self.assertFalse(os.path.exists(path))

    def test_relative_path_is_not_searched_for(self):
        """An id that already carries a path is a location, not a name."""
        controller = make_controller(self.mount)
        with mock.patch.object(controller, "_locate",
                               side_effect=AssertionError("walked")):
            self.assertEqual(
                controller.get_full_path("user.slice/user-1000.slice/demo.scope"),
                self.nested,
            )


class ReadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cg = FakeCgroup(self.root)
        self.controller = make_controller(str(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_current_swap_stat_events_and_cpu(self):
        self.cg.set_state(anon_mb=300, file_mb=100, swap_mb=50,
                          usage_usec=1_500_000, high_events=7)
        mb = 1024 * 1024
        self.assertEqual(self.controller.read_current(self.cg.name), 400 * mb)
        self.assertEqual(self.controller.read_swap_current(self.cg.name), 50 * mb)
        self.assertEqual(self.controller.read_memory_stat(self.cg.name)["anon"], 300 * mb)
        self.assertEqual(self.controller.read_events(self.cg.name)["high"], 7)
        self.assertEqual(self.controller.read_cpu_usec(self.cg.name), 1_500_000)

    def test_missing_files_return_none_not_zero(self):
        """A missing file must not read as 0: that would look like an idle, empty
        cgroup and wave the reclaim gates straight through."""
        empty = FakeCgroup(self.root, "app.slice/empty.scope")
        self.assertIsNone(self.controller.read_current(empty.name))
        self.assertIsNone(self.controller.read_cpu_usec(empty.name))
        self.assertEqual(self.controller.read_memory_stat(empty.name), {})
        self.assertIsNone(self.controller.file_cache_fraction(empty.name))

    def test_malformed_values_are_skipped_not_fatal(self):
        self.cg.set_state(anon_mb=100, file_mb=100)
        self.cg.write("memory.stat", "anon notanumber\nfile 1048576\ngarbage\n")
        stat = self.controller.read_memory_stat(self.cg.name)
        self.assertEqual(stat, {"file": 1048576})

    def test_file_cache_fraction(self):
        self.cg.set_state(anon_mb=300, file_mb=100)
        self.assertAlmostEqual(self.controller.file_cache_fraction(self.cg.name), 0.25)
        self.cg.set_state(anon_mb=0, file_mb=0)
        self.assertEqual(self.controller.file_cache_fraction(self.cg.name), 0.0)


class ReclaimTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cg = FakeCgroup(self.root)
        self.cg.set_state(anon_mb=400, file_mb=100)
        self.controller = make_controller(str(self.root))
        self.controller._supports_swappiness = True  # skip the probe
        self.writes = []

    def tearDown(self):
        self._tmp.cleanup()

    def _patch_write(self, side_effect=None, frees_mb=0):
        """Record memory.reclaim payloads, optionally shrinking memory.current."""
        def fake_write(content, target_file, allowed_roots=None):
            self.writes.append(content)
            if side_effect is not None:
                raise side_effect
            if frees_mb:
                before = int((self.cg.path / "memory.current").read_text())
                after = max(0, before - frees_mb * 1024 * 1024)
                self.cg.write("memory.current", str(after))
        return mock.patch("controller.memory.write_cgroup_file", side_effect=fake_write)

    def test_swappiness_is_included_in_the_payload(self):
        with self._patch_write(frees_mb=200):
            out = self.controller.reclaim(self.cg.name, 200 * 1024 * 1024, swappiness=180)
        self.assertTrue(out.ok)
        self.assertEqual(self.writes, [f"{200 * 1024 * 1024} swappiness=180"])
        self.assertEqual(out.reclaimed_bytes, 200 * 1024 * 1024)

    def test_default_swappiness_is_anon_biased(self):
        """The default must never be None: a bare write reclaims page cache only."""
        self.assertGreater(DEFAULT_RECLAIM_SWAPPINESS, 100)
        self.assertLessEqual(DEFAULT_RECLAIM_SWAPPINESS, SWAPPINESS_MAX)
        with self._patch_write(frees_mb=10):
            out = self.controller.reclaim(self.cg.name, 10 * 1024 * 1024)
        self.assertIn(f"swappiness={DEFAULT_RECLAIM_SWAPPINESS}", self.writes[0])
        self.assertEqual(out.swappiness, DEFAULT_RECLAIM_SWAPPINESS)

    def test_bare_reclaim_requires_an_explicit_opt_in(self):
        """swappiness=None is allowed but must be asked for, never defaulted to."""
        with self._patch_write(frees_mb=10):
            self.controller.reclaim(self.cg.name, 10 * 1024 * 1024, swappiness=None)
        self.assertEqual(self.writes, [str(10 * 1024 * 1024)])
        self.assertNotIn("swappiness", self.writes[0])

    def test_out_of_range_swappiness_is_rejected_without_writing(self):
        """201 and above are EINVAL in the kernel; catch it before the write."""
        for bad in (-1, 201, 1000):
            self.writes.clear()
            with self._patch_write():
                out = self.controller.reclaim(self.cg.name, 1024, swappiness=bad)
            self.assertFalse(out.ok, bad)
            self.assertEqual(out.errno, errno.EINVAL, bad)
            self.assertEqual(self.writes, [], f"{bad} must not reach the kernel")

    def test_zero_request_is_a_no_op(self):
        with self._patch_write():
            out = self.controller.reclaim(self.cg.name, 0)
        self.assertTrue(out.ok)
        self.assertEqual(self.writes, [])

    def test_eagain_is_distinguished_from_other_errors(self):
        """EAGAIN means "no reclaim headroom" -- measured when swap is full or
        memory.swap.max=0.  The caller must back off and grow swap, so it cannot
        be lumped in with a generic failure."""
        with self._patch_write(side_effect=OSError(errno.EAGAIN, "Resource temporarily unavailable")):
            out = self.controller.reclaim(self.cg.name, 200 * 1024 * 1024)
        self.assertFalse(out.ok)
        self.assertTrue(out.is_eagain)

        with self._patch_write(side_effect=OSError(errno.EACCES, "Permission denied")):
            out = self.controller.reclaim(self.cg.name, 200 * 1024 * 1024)
        self.assertFalse(out.ok)
        self.assertFalse(out.is_eagain)

    def test_partial_progress_before_eagain_is_still_counted(self):
        """The kernel can reclaim part of the request and *then* return EAGAIN, and
        it reports no byte count, so the delta is measured from memory.current."""
        def fake_write(content, target_file, allowed_roots=None):
            self.writes.append(content)
            self.cg.write("memory.current", str(430 * 1024 * 1024))  # 500 -> 430
            raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

        with mock.patch("controller.memory.write_cgroup_file", side_effect=fake_write):
            out = self.controller.reclaim(self.cg.name, 200 * 1024 * 1024)
        self.assertFalse(out.ok)
        self.assertTrue(out.is_eagain)
        self.assertEqual(out.reclaimed_bytes, 70 * 1024 * 1024)

    def test_growth_is_not_reported_as_negative_reclaim(self):
        """An active target can grow during the call; freed bytes must floor at 0."""
        def fake_write(content, target_file, allowed_roots=None):
            self.cg.write("memory.current", str(600 * 1024 * 1024))  # grew
        with mock.patch("controller.memory.write_cgroup_file", side_effect=fake_write):
            out = self.controller.reclaim(self.cg.name, 200 * 1024 * 1024)
        self.assertEqual(out.reclaimed_bytes, 0)


class SwappinessProbeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cg = FakeCgroup(self.root)
        self.cg.set_state(anon_mb=400, file_mb=0)
        self.controller = make_controller(str(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    def test_eagain_during_probe_means_supported(self):
        """EAGAIN proves the kernel *parsed* swappiness= and merely had nothing
        to reclaim, so it must not be read as "unsupported"."""
        with mock.patch("controller.memory.write_cgroup_file",
                        side_effect=OSError(errno.EAGAIN, "try again")):
            self.assertTrue(self.controller.supports_swappiness(self.cg.name))

    def test_einval_during_probe_means_unsupported(self):
        with mock.patch("controller.memory.write_cgroup_file",
                        side_effect=OSError(errno.EINVAL, "Invalid argument")):
            self.assertFalse(self.controller.supports_swappiness(self.cg.name))

    def test_probe_result_is_cached(self):
        with mock.patch("controller.memory.write_cgroup_file") as write:
            self.controller.supports_swappiness(self.cg.name)
            self.controller.supports_swappiness(self.cg.name)
            self.controller.supports_swappiness(self.cg.name)
        self.assertEqual(write.call_count, 1)

    def test_unsupported_kernel_refuses_rather_than_falling_back(self):
        """On a kernel without swappiness= the safe move is to do nothing.

        Falling back to a bare write would "succeed" while reclaiming page cache
        only -- on a NAS that is worse than leaving the memory alone.
        """
        with mock.patch("controller.memory.write_cgroup_file",
                        side_effect=OSError(errno.EINVAL, "Invalid argument")) as write:
            out = self.controller.reclaim(self.cg.name, 200 * 1024 * 1024)
            probe_calls = write.call_count
        self.assertFalse(out.ok)
        self.assertTrue(out.unsupported)
        self.assertEqual(probe_calls, 1, "only the probe should have been written")


class GateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cg = FakeCgroup(self.root)
        self.controller = make_controller(str(self.root))
        self.controller._supports_swappiness = True

    def tearDown(self):
        self._tmp.cleanup()

    def _manager(self, **overrides):
        cfg = {"enabled": True}
        cfg.update(overrides)
        return make_manager(self.controller, cfg)

    def test_reclaim_ships_disabled(self):
        self.assertFalse(RECLAIM_DEFAULTS["enabled"])
        manager = make_manager(self.controller, {})
        self.assertFalse(manager.reclaim_enabled)
        self.cg.set_state(anon_mb=400, file_mb=0)
        allowed, reason = manager.should_reclaim(self.cg.name, busy_fraction=0.0)
        self.assertFalse(allowed)
        self.assertIn("disabled", reason)

    def test_config_overrides_defaults_and_ignores_unknown_keys(self):
        manager = self._manager(chunk_mb=64, bogus_key=1)
        cfg = manager.reclaim_config()
        self.assertEqual(cfg["chunk_mb"], 64)
        self.assertEqual(cfg["swappiness"], RECLAIM_DEFAULTS["swappiness"])
        self.assertNotIn("bogus_key", cfg)

    def test_idle_target_passes(self):
        self.cg.set_state(anon_mb=400, file_mb=50)
        allowed, reason = self._manager().should_reclaim(self.cg.name, busy_fraction=0.01)
        self.assertTrue(allowed, reason)

    def test_busy_target_is_skipped(self):
        """Measured: a busy cgroup climbed 230 MB -> 402 MB two seconds after a
        reclaim that cost the caller 450 ms.  Pure loss."""
        self.cg.set_state(anon_mb=400, file_mb=0)
        allowed, reason = self._manager().should_reclaim(self.cg.name, busy_fraction=0.9)
        self.assertFalse(allowed)
        self.assertIn("busy", reason)

    def test_unknown_cpu_usage_is_not_treated_as_idle(self):
        self.cg.set_state(anon_mb=400, file_mb=0)
        allowed, reason = self._manager().should_reclaim(self.cg.name, busy_fraction=None)
        self.assertFalse(allowed)
        self.assertIn("unknown", reason)

    def test_cache_heavy_target_is_skipped(self):
        """swappiness cannot protect page cache -- measured, swappiness=200 still
        drained the remaining cache before touching anon -- so the gate is here."""
        self.cg.set_state(anon_mb=100, file_mb=400)
        allowed, reason = self._manager().should_reclaim(self.cg.name, busy_fraction=0.0)
        self.assertFalse(allowed)
        self.assertIn("cache-heavy", reason)

    def test_target_without_anon_is_skipped(self):
        """Nothing to move to swap: the caller would pay the block for nothing.

        anon and file are both zero so the cache gate (which would otherwise see
        a file fraction of 1.0) stays out of the way and this really does
        exercise the anon check.
        """
        self.cg.set_state(anon_mb=0, file_mb=0, current_mb=8)
        allowed, reason = self._manager().should_reclaim(self.cg.name, busy_fraction=0.0)
        self.assertFalse(allowed)
        self.assertIn("anonymous", reason)

    def test_vanished_cgroup_is_skipped(self):
        manager = self._manager()
        allowed, reason = manager.should_reclaim("app.slice/gone.scope", busy_fraction=0.0)
        self.assertFalse(allowed)
        self.assertIn("gone", reason)

    def test_cpu_busy_fraction(self):
        self.cg.set_state(anon_mb=10, file_mb=0, usage_usec=2_000_000)
        manager = self._manager()
        # First sample has no baseline, so it cannot claim the target is idle.
        fraction, now = manager.cpu_busy_fraction(self.cg.name, None, 1.0)
        self.assertIsNone(fraction)
        self.assertEqual(now, 2_000_000)
        # 1.0 s of CPU over 2.0 s of wall clock = half a core.
        fraction, now = manager.cpu_busy_fraction(self.cg.name, 1_000_000, 2.0)
        self.assertAlmostEqual(fraction, 0.5)

    def test_counter_reset_does_not_produce_a_bogus_fraction(self):
        """A restarted app gets a fresh cgroup and a counter that goes backwards."""
        self.cg.set_state(anon_mb=10, file_mb=0, usage_usec=1_000)
        fraction, now = self._manager().cpu_busy_fraction(self.cg.name, 9_000_000, 1.0)
        self.assertIsNone(fraction)
        self.assertEqual(now, 1_000)


class ChunkingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cg = FakeCgroup(self.root)
        self.cg.set_state(anon_mb=1000, file_mb=0)
        self.controller = make_controller(str(self.root))
        self.controller._supports_swappiness = True
        self.manager = make_manager(self.controller, {"enabled": True, "chunk_mb": 200})

    def tearDown(self):
        self._tmp.cleanup()

    def test_request_is_split_into_chunks(self):
        """A 200 MB chunk blocks the caller ~200 ms, so a large request is split
        to keep each blocking step bounded."""
        requested = []

        def fake_write(content, target_file, allowed_roots=None):
            requested.append(int(content.split()[0]))
        with mock.patch("controller.memory.write_cgroup_file", side_effect=fake_write):
            out = self.manager.reclaim_memory(self.cg.name, 500 * 1024 * 1024)
        mb = 1024 * 1024
        self.assertEqual(requested, [200 * mb, 200 * mb, 100 * mb])
        self.assertTrue(out.ok)
        self.assertEqual(out.requested_bytes, 500 * mb)

    def test_chunking_stops_on_eagain_and_keeps_the_errno(self):
        calls = []

        def fake_write(content, target_file, allowed_roots=None):
            calls.append(content)
            if len(calls) == 2:
                raise OSError(errno.EAGAIN, "Resource temporarily unavailable")
        with mock.patch("controller.memory.write_cgroup_file", side_effect=fake_write):
            out = self.manager.reclaim_memory(self.cg.name, 1000 * 1024 * 1024)
        self.assertEqual(len(calls), 2, "must not keep hammering a cgroup out of headroom")
        self.assertFalse(out.ok)
        self.assertTrue(out.is_eagain)

    def test_freed_bytes_accumulate_across_chunks(self):
        def fake_write(content, target_file, allowed_roots=None):
            before = int((self.cg.path / "memory.current").read_text())
            self.cg.write("memory.current", str(before - 200 * 1024 * 1024))
        with mock.patch("controller.memory.write_cgroup_file", side_effect=fake_write):
            out = self.manager.reclaim_memory(self.cg.name, 600 * 1024 * 1024)
        self.assertEqual(out.reclaimed_bytes, 600 * 1024 * 1024)

    def test_cgroup_disappearing_mid_reclaim_aborts(self):
        """The app can exit between chunks; the writes must stop, not error out
        on a dead path."""
        calls = []

        def fake_write(content, target_file, allowed_roots=None):
            calls.append(content)
            for child in self.cg.path.iterdir():
                child.unlink()
            self.cg.path.rmdir()
        with mock.patch("controller.memory.write_cgroup_file", side_effect=fake_write):
            out = self.manager.reclaim_memory(self.cg.name, 1000 * 1024 * 1024)
        self.assertEqual(len(calls), 1)
        self.assertFalse(out.ok)
        self.assertIn("gone", out.reason)


class OutcomeTests(unittest.TestCase):
    def test_eagain_and_unsupported_are_disjoint(self):
        self.assertTrue(ReclaimOutcome(ok=False, errno=errno.EAGAIN).is_eagain)
        self.assertFalse(ReclaimOutcome(ok=False, errno=errno.EAGAIN).unsupported)
        self.assertTrue(ReclaimOutcome(ok=False, errno=errno.EINVAL).unsupported)
        self.assertFalse(ReclaimOutcome(ok=False, errno=errno.EINVAL).is_eagain)
        self.assertFalse(ReclaimOutcome(ok=True).is_eagain)
        self.assertFalse(ReclaimOutcome(ok=True).unsupported)


if __name__ == "__main__":
    unittest.main(verbosity=2)
