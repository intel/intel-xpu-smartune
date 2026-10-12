# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the L2 ladder wiring in the balancer (P4).

Two layers are covered:

* the arithmetic on ControlManager -- the system deficit and the PSI-driven
  gain.  The property that matters is that free memory is only ever the *gate*
  and PSI only ever the *gain*: a box that is 85% full but not stalling must be
  pulled gently, and a box under target must not be pulled at all no matter how
  hard any single app is pressing on its own memory.high;
* the round assembly on DynamicBalancer -- who is eligible, how the round
  budget is split, and what happens when swap turns out to be full.

The balancer is instantiated with ``__new__`` and given only the collaborators
these methods touch.  Its real constructor starts prefetcher threads and opens
the pressure monitor, none of which the decision logic needs.

Run: PYTHONDONTWRITEBYTECODE=1 python3 balancer/test/test_reclaim_ladder.py
"""

import errno
import logging
import os
import sys
import threading
import types
import unittest
from collections import OrderedDict
from concurrent.futures import Future

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BALANCER_ROOT = os.path.join(_REPO_ROOT, "balancer")
for _p in (_REPO_ROOT, _BALANCER_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_logger_module = types.ModuleType("utils.logger")
_test_logger = logging.getLogger("test_reclaim_ladder")
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


# Everything the balancer imports that the ladder does not use.  Stubbed rather
# than required so the suite runs without the service venv (no psutil, peewee
# or yaml on a plain checkout).
_stub("psutil", Process=object, NoSuchProcess=Exception, AccessDenied=Exception)
_stub("utils.app_utils", write_cgroup_file=lambda *a, **k: None)
_stub("utils.quiet_mode", is_active=lambda: False)
_stub("config.config", b_config=types.SimpleNamespace(
    cgroup_mount="/sys/fs/cgroup", limit_policy={}))
_stub("monitor.monitor_api", SystemPressureMonitor=object)
_stub("monitor.disk_pressure", media_for_disk=lambda *a, **k: "nvme",
      MEDIA_CLASSES=())
_stub("controller.controller", Controller=object)
_stub("controller.governor", GovernorController=object)
_stub("controller.cpu", CPUController=object)
_stub("controller.app_intercept", AppIntercept=object)
_stub("controller.network", NetworkController=object)
_stub("controller.io", IOController=object)

from controller.control_manager import RECLAIM_DEFAULTS, ControlManager  # noqa: E402
from controller.memory import ReclaimOutcome  # noqa: E402
from controller.swap import MemorySnapshot, SwapController  # noqa: E402
from balancer.balancer import (  # noqa: E402
    _MB,
    _RECLAIM_MAX_BACKOFF,
    DynamicBalancer,
    LimitedApp,
    LimitRegistry,
)

GB = 1024 * _MB


def manager_with(**reclaim) -> ControlManager:
    """A ControlManager shell carrying only the config the maths reads."""
    cfg = dict(RECLAIM_DEFAULTS)
    cfg.update(reclaim)
    manager = ControlManager.__new__(ControlManager)
    manager.config = types.SimpleNamespace(
        limit_policy={"memory": {"reclaim": cfg}})
    return manager


class DeficitTests(unittest.TestCase):
    def manager_seeing(self, mem_total, mem_available, anon=0, swap_used=0,
                       **reclaim):
        manager = manager_with(**reclaim)
        # A real SwapController, not a stub: system_memory_deficit runs the
        # reading through worst_recent, and a stub would let that path rot.
        # The window starts empty, so the deficit is the snapshot's own.
        swap = SwapController(types.SimpleNamespace(limit_policy={}))
        swap.read_meminfo = lambda: MemorySnapshot(
            mem_total=mem_total, mem_available=mem_available,
            mem_free=mem_available, anon_pages=anon,
            swap_total=8 * GB, swap_free=8 * GB - swap_used)
        manager.swap = swap
        return manager

    def test_deficit_is_sized_on_the_window_trough_not_the_instant(self):
        """L2 has to agree with L0 about how full the box is.  Sizing the
        deficit off a peak means reclaiming nothing on the tick that mattered,
        and the allocator that caused the crisis digs its troughs between the
        minute-cadence checks that look."""
        manager = self.manager_seeing(32 * GB, mem_available=13 * GB)  # 59%
        self.assertEqual(manager.system_memory_deficit()[0], 0)

        manager.swap.sample(MemorySnapshot(
            mem_total=32 * GB, mem_available=1 * GB, mem_free=1 * GB,
            swap_total=8 * GB, swap_free=0))
        deficit, facts = manager.system_memory_deficit()
        self.assertEqual(deficit, 31 * GB - int(32 * GB * 0.80))
        self.assertAlmostEqual(facts["used_fraction"], 31 / 32, places=3)

    def test_under_target_is_zero_deficit(self):
        """The gate is system-level.  An app hammering its own memory.high while
        the box sits at 60% is not a reason to reclaim anything."""
        manager = self.manager_seeing(32 * GB, mem_available=13 * GB)  # 59% used
        deficit, facts = manager.system_memory_deficit()
        self.assertEqual(deficit, 0)
        self.assertAlmostEqual(facts["used_fraction"], 19 / 32, places=3)

    def test_deficit_is_the_distance_to_the_target(self):
        manager = self.manager_seeing(32 * GB, mem_available=2 * GB)  # 93.75%
        deficit, _ = manager.system_memory_deficit()
        self.assertEqual(deficit, 30 * GB - int(32 * GB * 0.80))

    def test_target_is_configurable_and_relative(self):
        """Same 90%-full box, two targets: the deficit has to track MemTotal,
        never an absolute byte count baked in for one machine."""
        for total_gb in (4, 8, 32, 128):
            total = total_gb * GB
            manager = self.manager_seeing(total, mem_available=total // 10,
                                          target_mem_pct=70)
            deficit, _ = manager.system_memory_deficit()
            self.assertEqual(deficit, (total - total // 10) - int(total * 0.70))

    def test_movable_anon_is_reported_so_the_caller_can_stand_down(self):
        manager = self.manager_seeing(32 * GB, mem_available=2 * GB,
                                      anon=1 * GB, swap_used=1 * GB)
        _, facts = manager.system_memory_deficit()
        self.assertEqual(facts["movable_anon_bytes"], 0)


class GainTests(unittest.TestCase):
    def test_no_stall_gets_the_gentle_end(self):
        """85% full and nothing is stalling: MemFree is not pressure."""
        self.assertAlmostEqual(manager_with().reclaim_gain(0.0), 0.01)

    def test_full_stall_gets_the_hard_end(self):
        self.assertAlmostEqual(manager_with().reclaim_gain(0.5), 0.05)

    def test_gain_interpolates_between_the_two(self):
        gain = manager_with().reclaim_gain(0.05)  # half of psi_full_scale
        self.assertAlmostEqual(gain, 0.03)

    def test_gain_never_leaves_its_band(self):
        manager = manager_with()
        for psi in (-1.0, 0.0, 0.0001, 0.1, 1.0, 99.0):
            gain = manager.reclaim_gain(psi)
            self.assertGreaterEqual(gain, 0.01)
            self.assertLessEqual(gain, 0.05)

    def test_bounds_are_configurable(self):
        manager = manager_with(gain_min=0.02, gain_max=0.10, psi_full_scale=0.2)
        self.assertAlmostEqual(manager.reclaim_gain(0.0), 0.02)
        self.assertAlmostEqual(manager.reclaim_gain(0.2), 0.10)
        self.assertAlmostEqual(manager.reclaim_gain(0.1), 0.06)


class FakeControlManager:
    """Stands in for ControlManager across one reclaim round.

    Records what was asked for instead of touching a cgroup, and lets each test
    decide the deficit, the PSI reading and which cgroups count as idle.
    """

    def __init__(self, deficit, psi=0.10, current=None, busy=0.0,
                 gate=None, outcome=None, enabled=True, swap_enabled=True):
        self._deficit = deficit
        self._psi = psi
        self._current = current or {}
        self._busy = busy
        self._gate = gate or {}
        self._outcome = outcome
        self.reclaim_enabled = enabled
        self.requests = []
        self.swap_growth_calls = 0
        self.swap_shrink_calls = 0
        self.swap = types.SimpleNamespace(enabled=swap_enabled)
        self.memory = types.SimpleNamespace(
            read_current=lambda cg: self._current.get(cg, 512 * _MB))

    def reclaim_config(self):
        return dict(RECLAIM_DEFAULTS)

    def system_memory_deficit(self):
        return self._deficit, {"mem_total_bytes": 32 * GB,
                               "used_effective_bytes": 30 * GB,
                               "used_fraction": 0.93,
                               "movable_anon_bytes": 10 * GB}

    def memory_pressure(self):
        return self._psi

    def reclaim_gain(self, psi=None):
        return ControlManager.reclaim_gain(manager_with(), psi)

    def cpu_busy_fraction(self, cgroup, prev, elapsed):
        return self._busy, 1234

    def should_reclaim(self, cgroup, busy):
        return self._gate.get(cgroup, (True, "ok"))

    def reclaim_memory_async(self, cgroup, nbytes):
        self.requests.append((cgroup, nbytes))
        future = Future()
        future.set_result(self._outcome or ReclaimOutcome(
            ok=True, requested_bytes=nbytes, reclaimed_bytes=nbytes,
            elapsed_ms=nbytes / _MB))
        return future

    def reclaim_memory_to_target(self, cgroup, target_bytes, growth_gate=None):
        """The manual path's synchronous form (:meth:`_reclaim_to_manual_targets`)."""
        self.requests.append((cgroup, target_bytes))
        return self._outcome or ReclaimOutcome(
            ok=True, requested_bytes=target_bytes, reclaimed_bytes=target_bytes,
            elapsed_ms=1.0)

    def ensure_swap_headroom_async(self, dry_run=True):
        self.swap_growth_calls += 1
        return Future()

    def shrink_swap_if_idle_async(self, dry_run=False):
        self.swap_shrink_calls += 1
        return Future()

    def shrink_swap_if_idle(self, dry_run=False):
        # The synchronous form. swapoff blocks until the kernel has faulted
        # every page of the file back in, so the tick must never reach this.
        raise AssertionError("shrink ran on the tick thread")


def balancer_with(manager, entries) -> DynamicBalancer:
    """A DynamicBalancer carrying only what the ladder methods read."""
    instance = DynamicBalancer.__new__(DynamicBalancer)
    instance.control_manager = manager
    instance.all_limits = LimitRegistry()
    instance.all_limits.apps = OrderedDict(entries)
    instance._last_swap_growth_at = 0.0
    instance._swap_growth_lock = threading.Lock()
    instance._last_swap_release_at = 0.0
    instance._reclaim_quiet_until = 0.0
    return instance


def limited(name, cgroup, source="auto", cpu_mem=True, priority="undefined"):
    return name, LimitedApp(
        public_app_id=name, app_name=name, source=source,
        limit_rates={}, limit_parts={'cpu_mem_limited': cpu_mem,
                                     'io_limited': False},
        priority=priority, cgroups=[cgroup])


def state_at(now=1000.0):
    return types.SimpleNamespace(current_time=now)


class CandidateTests(unittest.TestCase):
    def test_only_cpu_mem_limited_apps_are_eligible(self):
        """The cap is what L2 reclaims against, so an app without one has nothing
        for this rung to aim at. Source is not a filter: a manual limit is still a
        memory.high, and the apps a user bothers to limit by hand are exactly the
        big holders L2 most needs to reach."""
        manager = FakeControlManager(deficit=4 * GB)
        balancer = balancer_with(manager, [
            limited("auto-sys", "a.scope"),
            limited("manual", "b.scope", source="manual"),
            limited("io-only", "c.scope", cpu_mem=False),
        ])
        keys = [k for k, _, _ in balancer._reclaim_candidates()]
        self.assertEqual(keys, ["auto-sys", "manual"])

    def test_an_io_capped_app_is_not_reclaimed_from(self):
        """Reclaim pushes anon pages to swap over the block device io.max is
        throttling. Reclaiming from a throttled app buys stalls, not memory."""
        manager = FakeControlManager(deficit=4 * GB)
        key, entry = limited("throttled", "a.scope")
        entry.limit_parts['io_limited'] = True
        balancer = balancer_with(manager, [(key, entry), limited("free", "b.scope")])
        balancer._tick_memory_reclaim(state_at())
        self.assertEqual([c for c, _ in manager.requests], ["b.scope"])

    def test_critical_priority_apps_are_never_touched(self):
        manager = FakeControlManager(deficit=4 * GB)
        balancer = balancer_with(manager, [
            limited("db", "a.scope", priority="critical"),
            limited("other", "b.scope"),
        ])
        keys = [k for k, _, _ in balancer._reclaim_candidates()]
        self.assertEqual(keys, ["other"])

    def test_an_inflight_reclaim_excludes_its_app(self):
        """A 4 GB round split into 200 MB chunks takes seconds; queuing a second
        round for the same cgroup would double-charge the deficit."""
        manager = FakeControlManager(deficit=4 * GB)
        key, entry = limited("busy", "a.scope")
        entry.reclaim_inflight = True
        balancer = balancer_with(manager, [(key, entry), limited("free", "b.scope")])
        self.assertEqual([k for k, _, _ in balancer._reclaim_candidates()], ["free"])

    def test_weight_grows_with_how_recently_the_app_was_limited(self):
        manager = FakeControlManager(deficit=4 * GB)
        balancer = balancer_with(manager, [
            limited("first", "a.scope"),
            limited("second", "b.scope"),
            limited("third", "c.scope"),
        ])
        orders = {k: o for k, _, o in balancer._reclaim_candidates()}
        self.assertEqual(orders, {"first": 1, "second": 2, "third": 3})


class ReclaimQuietWindowTests(unittest.TestCase):
    """The disk arm must not act on disk pressure our own reclaim produced.

    Reclaim writes anon pages to swap, which is real device traffic -- a
    measured 5.3 GB reclaim put nvme0n1 at 717 MB/s and drove the disk channel
    to critical.  Nothing downstream can attribute it: ``/proc/<pid>/io`` only
    counts process-context IO, so kernel writeback never appears against the app
    that caused it, and the arm would cap whichever innocent app happens to be
    top of the list for traffic it did not generate and that disappears on its
    own within seconds.
    """

    def _balancer(self):
        return balancer_with(FakeControlManager(deficit=0), [])

    def test_the_whole_arm_stands_down_during_the_window(self):
        balancer = self._balancer()
        balancer._mark_reclaim_noise()
        should_adjust, _, app_id, rates, target, idx = \
            balancer._handle_disk_io_stressed([{"app": {"id": "hog"}}])
        self.assertFalse(should_adjust)
        self.assertIsNone(app_id)
        self.assertIsNone(idx)

    def test_the_window_expires(self):
        balancer = self._balancer()
        balancer._mark_reclaim_noise()
        self.assertTrue(balancer._in_reclaim_quiet_window())
        balancer._reclaim_quiet_until = 0.0
        self.assertFalse(balancer._in_reclaim_quiet_window())

    def test_submitting_a_reclaim_round_opens_the_window(self):
        manager = FakeControlManager(deficit=4 * GB)
        balancer = balancer_with(manager, [limited("app", "a.scope")])
        self.assertFalse(balancer._in_reclaim_quiet_window())
        balancer._tick_memory_reclaim(state_at())
        self.assertTrue(manager.requests)
        self.assertTrue(balancer._in_reclaim_quiet_window())

    def test_a_manual_reclaim_opens_the_window_too(self):
        """The manual path reclaims synchronously in the REST thread, outside
        the L2 round, so it has to arm the window itself."""
        manager = FakeControlManager(deficit=0)
        balancer = balancer_with(manager, [])
        balancer._claim_swap_growth_slot = lambda *a, **k: False
        balancer._reclaim_to_manual_targets([("a.scope", 512)])
        self.assertEqual([c for c, _ in manager.requests], ["a.scope"])
        self.assertTrue(balancer._in_reclaim_quiet_window())

    def test_the_window_opens_even_when_the_manual_reclaim_throws(self):
        """A reclaim that raised still wrote to swap up to the point it failed,
        so the window is armed in a ``finally``, not on the success path."""
        manager = FakeControlManager(deficit=0)
        manager.reclaim_memory_to_target = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("cgroup gone"))
        balancer = balancer_with(manager, [])
        balancer._reclaim_to_manual_targets([("a.scope", 512)])
        self.assertTrue(balancer._in_reclaim_quiet_window())


class RoundTests(unittest.TestCase):
    def test_no_deficit_means_no_reclaim_at_all(self):
        manager = FakeControlManager(deficit=0)
        balancer = balancer_with(manager, [limited("app", "a.scope")])
        balancer._tick_memory_reclaim(state_at())
        self.assertEqual(manager.requests, [])

    def test_disabled_is_a_no_op(self):
        manager = FakeControlManager(deficit=4 * GB, enabled=False)
        balancer = balancer_with(manager, [limited("app", "a.scope")])
        balancer._tick_memory_reclaim(state_at())
        self.assertEqual(manager.requests, [])

    def test_busy_targets_are_skipped(self):
        """Measured: an active cgroup faults the pages back within two seconds,
        so the ~200 ms per chunk the caller pays buys nothing."""
        manager = FakeControlManager(
            deficit=4 * GB, gate={"a.scope": (False, "target busy (0.90 > 0.05)")})
        balancer = balancer_with(manager, [limited("app", "a.scope")])
        balancer._tick_memory_reclaim(state_at())
        self.assertEqual(manager.requests, [])

    def test_shortfall_outside_anonymous_memory_stands_down(self):
        manager = FakeControlManager(deficit=4 * GB)
        manager.system_memory_deficit = lambda: (
            4 * GB, {"movable_anon_bytes": 0, "used_fraction": 0.95})
        balancer = balancer_with(manager, [limited("app", "a.scope")])
        balancer._tick_memory_reclaim(state_at())
        self.assertEqual(manager.requests, [])

    def test_round_is_capped_by_the_gain_not_by_the_deficit(self):
        """A 20 GB deficit does not license a 20 GB round.  At full PSI the gain
        is 5% of the candidate's usage, and that is what gets asked for."""
        manager = FakeControlManager(deficit=20 * GB, psi=1.0,
                                     current={"a.scope": 8 * GB})
        balancer = balancer_with(manager, [limited("app", "a.scope")])
        balancer._tick_memory_reclaim(state_at())
        self.assertEqual(len(manager.requests), 1)
        self.assertAlmostEqual(manager.requests[0][1] / (8 * GB), 0.05, places=3)

    def test_round_is_capped_by_the_deficit_when_that_is_smaller(self):
        """Filling the deficit is the whole objective; over-reclaiming past it
        just buys refaults later."""
        manager = FakeControlManager(deficit=64 * _MB, psi=1.0,
                                     current={"a.scope": 8 * GB})
        balancer = balancer_with(manager, [limited("app", "a.scope")])
        balancer._tick_memory_reclaim(state_at())
        self.assertEqual(manager.requests[0][1], 64 * _MB)

    def test_budget_splits_by_usage_times_seniority(self):
        """Equal RSS, different seniority: the app limited last gives up more,
        so a newcomer yields before a workload that was already settled."""
        manager = FakeControlManager(
            deficit=20 * GB, psi=1.0,
            current={"a.scope": 4 * GB, "b.scope": 4 * GB})
        balancer = balancer_with(manager, [
            limited("older", "a.scope"),
            limited("newer", "b.scope"),
        ])
        balancer._tick_memory_reclaim(state_at())
        asked = dict(manager.requests)
        self.assertLess(asked["a.scope"], asked["b.scope"])
        self.assertAlmostEqual(asked["b.scope"] / asked["a.scope"], 2.0, places=2)

    def test_total_asked_never_exceeds_the_deficit(self):
        manager = FakeControlManager(
            deficit=1 * GB, psi=1.0,
            current={"a.scope": 8 * GB, "b.scope": 8 * GB, "c.scope": 8 * GB})
        balancer = balancer_with(manager, [
            limited("a", "a.scope"), limited("b", "b.scope"),
            limited("c", "c.scope")])
        balancer._tick_memory_reclaim(state_at())
        self.assertLessEqual(sum(n for _, n in manager.requests), 1 * GB + 3)

    def test_cooldown_blocks_a_second_round(self):
        manager = FakeControlManager(deficit=4 * GB, current={"a.scope": 4 * GB})
        balancer = balancer_with(manager, [limited("app", "a.scope")])
        balancer._tick_memory_reclaim(state_at(1000.0))
        self.assertEqual(len(manager.requests), 1)
        balancer._tick_memory_reclaim(state_at(1010.0))   # cooldown is 30 s
        self.assertEqual(len(manager.requests), 1)
        balancer._tick_memory_reclaim(state_at(1040.0))
        self.assertEqual(len(manager.requests), 2)


class OutcomeTests(unittest.TestCase):
    def entry(self):
        _, entry = limited("app", "a.scope")
        return entry

    def done(self, outcome):
        future = Future()
        future.set_result(outcome)
        return future

    def test_eagain_backs_off_and_asks_for_more_swap(self):
        """EAGAIN means swap is full.  Retrying costs another 4 ms and changes
        nothing; only more swap can make the next round work."""
        manager = FakeControlManager(deficit=4 * GB)
        balancer = balancer_with(manager, [])
        entry = self.entry()
        balancer._on_reclaim_done(
            self.done(ReclaimOutcome(ok=False, errno=errno.EAGAIN,
                                     reason="no reclaim headroom")),
            entry, "app", "a.scope", 200 * _MB)
        self.assertEqual(entry.reclaim_backoff, 30.0)
        self.assertEqual(manager.swap_growth_calls, 1)

    def test_backoff_doubles_but_is_capped(self):
        manager = FakeControlManager(deficit=4 * GB)
        balancer = balancer_with(manager, [])
        entry = self.entry()
        seen = []
        for _ in range(12):
            balancer._on_reclaim_done(
                self.done(ReclaimOutcome(ok=False, errno=errno.EAGAIN)),
                entry, "app", "a.scope", 200 * _MB)
            seen.append(entry.reclaim_backoff)
        self.assertEqual(seen[:3], [30.0, 60.0, 120.0])
        self.assertEqual(max(seen), _RECLAIM_MAX_BACKOFF)

    def test_a_good_round_clears_the_backoff(self):
        manager = FakeControlManager(deficit=4 * GB)
        balancer = balancer_with(manager, [])
        entry = self.entry()
        entry.reclaim_backoff = 240.0
        balancer._on_reclaim_done(
            self.done(ReclaimOutcome(ok=True, reclaimed_bytes=200 * _MB,
                                     elapsed_ms=210.0)),
            entry, "app", "a.scope", 200 * _MB)
        self.assertEqual(entry.reclaim_backoff, 0.0)
        self.assertEqual(manager.swap_growth_calls, 0)

    def test_inflight_is_always_cleared(self):
        """If it were not, one failed round would take the app out of every
        future round for the lifetime of the limit."""
        manager = FakeControlManager(deficit=4 * GB)
        balancer = balancer_with(manager, [])
        for outcome in (ReclaimOutcome(ok=True),
                        ReclaimOutcome(ok=False, errno=errno.EAGAIN),
                        ReclaimOutcome(ok=False, reason="cgroup gone")):
            entry = self.entry()
            entry.reclaim_inflight = True
            balancer._on_reclaim_done(self.done(outcome), entry, "app",
                                      "a.scope", 200 * _MB)
            self.assertFalse(entry.reclaim_inflight)

    def test_swap_growth_is_not_requested_when_disabled(self):
        manager = FakeControlManager(deficit=4 * GB, swap_enabled=False)
        balancer = balancer_with(manager, [])
        balancer._on_reclaim_done(
            self.done(ReclaimOutcome(ok=False, errno=errno.EAGAIN)),
            self.entry(), "app", "a.scope", 200 * _MB)
        self.assertEqual(manager.swap_growth_calls, 0)


class SwapHeadroomTickTests(unittest.TestCase):
    def test_growth_is_rate_limited(self):
        manager = FakeControlManager(deficit=0)
        balancer = balancer_with(manager, [])
        balancer._tick_swap_headroom(state_at(1000.0), "low")
        balancer._tick_swap_headroom(state_at(1030.0), "low")
        self.assertEqual(manager.swap_growth_calls, 1)
        balancer._tick_swap_headroom(state_at(1061.0), "low")
        self.assertEqual(manager.swap_growth_calls, 2)

    def test_disabled_growth_is_never_attempted(self):
        manager = FakeControlManager(deficit=0, swap_enabled=False)
        balancer = balancer_with(manager, [])
        balancer._tick_swap_headroom(state_at(), "low")
        self.assertEqual(manager.swap_growth_calls, 0)

    def test_critical_looks_again_far_sooner_than_the_normal_cadence(self):
        """The incident: the loop bypasses its own idle interval on the critical
        edge so critical is served in ~1s, and then the 60s limiter refused the
        bypassed tick because only 50.2s had passed since the last *look*."""
        manager = FakeControlManager(deficit=0)
        balancer = balancer_with(manager, [])
        balancer._tick_swap_headroom(state_at(1000.0), "low")
        self.assertEqual(manager.swap_growth_calls, 1)
        # 50.2s later, exactly as in the log: refused at the normal cadence...
        balancer._tick_swap_headroom(state_at(1050.2), "low")
        self.assertEqual(manager.swap_growth_calls, 1)
        # ...and allowed once the level says the machine is in trouble.
        balancer._tick_swap_headroom(state_at(1050.2), "critical")
        self.assertEqual(manager.swap_growth_calls, 2)

    def test_the_urgent_interval_still_rate_limits(self):
        """10s is a shorter leash, not no leash: a critical episode lasting
        minutes must not queue a fallocate on every tick."""
        manager = FakeControlManager(deficit=0)
        balancer = balancer_with(manager, [])
        balancer._tick_swap_headroom(state_at(1000.0), "critical")
        balancer._tick_swap_headroom(state_at(1005.0), "critical")
        self.assertEqual(manager.swap_growth_calls, 1)
        balancer._tick_swap_headroom(state_at(1011.0), "critical")
        self.assertEqual(manager.swap_growth_calls, 2)

    def test_the_pre_oom_flag_is_a_passthrough_not_a_second_opinion(self):
        """The balancer reads this once a second, so it has to stay a cached
        bool read off the monitor -- no recomputation, no second threshold."""
        manager = manager_with()
        manager.system_pressure_monitor = types.SimpleNamespace(
            is_memory_pre_oom=False)
        self.assertFalse(manager.is_memory_pre_oom)
        manager.system_pressure_monitor.is_memory_pre_oom = True
        self.assertTrue(manager.is_memory_pre_oom)

    def test_the_two_paths_share_one_slot(self):
        """The listener runs on the pressure monitor's refresh thread and the
        tick on the loop thread; both fire on the same critical edge.  Whoever
        gets there first wins, so the machine gets one fallocate, not two."""
        manager = FakeControlManager(deficit=0)
        balancer = balancer_with(manager, [])
        self.assertTrue(balancer._claim_swap_growth_slot(2000.0, urgent=True))
        self.assertFalse(balancer._claim_swap_growth_slot(2000.5, urgent=True))
        balancer._tick_swap_headroom(state_at(2001.0), "critical")
        self.assertEqual(manager.swap_growth_calls, 0)


class ShrinkIfIdleTests(unittest.TestCase):
    """What ControlManager adds on top of should_shrink: one file per round."""

    def manager(self, allowed, reason="test"):
        manager = manager_with()
        self.asked = []

        def release(**kwargs):
            self.asked.append(kwargs)
            # release_swap logs every result, so the double has to look like one.
            return [types.SimpleNamespace(path="/tmp/swap-01.img", reason="ok")]

        manager.swap = types.SimpleNamespace(
            should_shrink=lambda: (allowed, reason), release=release)
        return manager

    def test_a_refused_gate_releases_nothing(self):
        manager = self.manager(False, "memory relaxed for 12s, need 600s")
        self.assertEqual(manager.shrink_swap_if_idle(), [])
        self.assertEqual(self.asked, [])

    def test_an_open_gate_releases_exactly_one_file(self):
        """Not all of them: each swapoff faults its file's pages back into RAM,
        and doing that for the whole set at once is the fault storm the whole
        design is trying to avoid."""
        manager = self.manager(True)
        manager.shrink_swap_if_idle(dry_run=False)
        self.assertEqual(self.asked, [{"dry_run": False, "only_unused": False,
                                       "limit": 1}])

    def test_the_idle_path_does_not_restrict_itself_to_unused_files(self):
        """Opposite of the shutdown rule: the idle path waited for a moment
        with room, so it is allowed to pay the fault-in that shutdown is not."""
        manager = self.manager(True)
        manager.shrink_swap_if_idle()
        self.assertFalse(self.asked[0]["only_unused"])


class SwapReleaseTickTests(unittest.TestCase):
    """L0 in reverse.  The tick only offers; SwapController.should_shrink and
    release()'s headroom gate decide whether anything actually happens."""

    def test_release_is_rate_limited(self):
        manager = FakeControlManager(deficit=0)
        balancer = balancer_with(manager, [])
        balancer._tick_swap_release(state_at(1000.0))
        balancer._tick_swap_release(state_at(1030.0))
        self.assertEqual(manager.swap_shrink_calls, 1)
        balancer._tick_swap_release(state_at(1061.0))
        self.assertEqual(manager.swap_shrink_calls, 2)

    def test_release_still_runs_with_growth_switched_off(self):
        """Turning swap_growth off must hand back what an earlier run
        allocated, not strand it on disk forever."""
        manager = FakeControlManager(deficit=0, swap_enabled=False)
        balancer = balancer_with(manager, [])
        balancer._tick_swap_release(state_at())
        self.assertEqual(manager.swap_shrink_calls, 1)

    def test_release_is_never_run_on_the_tick_thread(self):
        """swapoff blocks until the kernel has faulted every page of the file
        back in -- seconds to minutes.  The tick may only queue it.

        The double raises from the synchronous form, so calling it is the
        failure rather than something this test has to assert about.
        """
        manager = FakeControlManager(deficit=0)
        balancer = balancer_with(manager, [])
        balancer._tick_swap_release(state_at())
        self.assertEqual(manager.swap_shrink_calls, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
