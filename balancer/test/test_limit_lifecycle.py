# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the auto-limit lifecycle in balancer/balancer/balancer.py.

Two things the pressure loop has to keep straight, both of which have gone wrong
against real hardware:

* **Whose candidate batch is this?**  The sys (CPU/memory) and disk-IO channels
  park their top-consumer candidates in the same ``_MonitorLoopState`` slot, and
  the disk channel deliberately *holds* an unconsumed batch while it sits at
  "high".  A batch spent by the other channel -- or by a later episode of its own
  channel -- caps an app that nobody measured as a hog for the pressure at hand;
  in the field this showed up as a CPU/memory critical trying to cap a scope from
  a disk-IO test that had exited hours earlier.

* **Whose limit is being lifted?**  Under the separated policy the two channels
  recover on independent timers (``STABLE_PERIOD`` / ``STABLE_DISK_IO_PERIOD``),
  so a restore driven by one timer must not hand back the other channel's cap,
  and must act on an app that actually carries the channel it is restoring.

Both are exercised on a stub instance -- constructing a real ``DynamicBalancer``
would start BPF and cgroup machinery unrelated to the decisions under test.

Run:  python3 balancer/test/test_limit_lifecycle.py
"""

import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "balancer")):
    if path not in sys.path:
        sys.path.insert(0, path)

import balancer.balancer as balancer_mod  # noqa: E402
import balance_service as balance_service_mod  # noqa: E402
import controller.app_intercept as app_intercept_mod  # noqa: E402
from controller.app_intercept import AppIntercept  # noqa: E402
from diagnostics import control_events, control_lifecycle  # noqa: E402
from monitor.res_monitor import ResourceMonitor, _is_identifiable_representative  # noqa: E402
from balancer.balancer import (  # noqa: E402
    DynamicBalancer,
    LimitedApp,
    LimitRegistry,
    TopConsumerPrefetcher,
    _MonitorLoopState,
)
from controller.swap import MEM_RISE_TRIGGER  # noqa: E402

NOW = 1_000_000.0


class _Cfg:
    limit_policy = {'policy': 'separated', 'disk_io': {}}
    passive_resource_control = {'enabled': True}
    cgroup_mount = '/sys/fs/cgroup'
    cooldown_time = 60


class _MergeCfg:
    def __init__(self):
        self.controlled_apps = [{
            'id': 'existing.scope',
            'name': 'existing-app',
            'commandline': '/usr/bin/existing-app',
            'process_names': ['existing-app', 'shared-worker'],
            'bpf_name': ['existing-app'],
        }]
        self.writes = []

    def set_list_section(self, section, entries):
        self.writes.append((section, entries))
        self.controlled_apps = entries
        return True


class _Prefetcher:
    """Stand-in for TopConsumerPrefetcher: records how often it was resolved."""

    def __init__(self, apps, reach_threshold=True, stale=False):
        self.apps = apps
        self.reach_threshold = reach_threshold
        self._stale = stale
        self.resolved = 0
        self.started = []

    def resolve_for_critical(self):
        self.resolved += 1
        return list(self.apps), self.reach_threshold

    def is_stale(self):
        return self._stale

    def start(self, reason):
        self.started.append(reason)


class _IoCtl:
    def __init__(self):
        self.set_calls = []
        self.restore_calls = []

    def get_disk_id(self, disk_filter=None):
        return {"nvme0n1": "259:0"}

    def set_disk_io_throttle(self, app_id, limits=None, disk_filter=None):
        self.set_calls.append((app_id, disk_filter))
        return True

    def restore_disk_io_throttle(self, app_id, disk_filter=None):
        self.restore_calls.append((app_id, disk_filter))
        return True


class _ControlManager:
    def __init__(self):
        self.adjust_calls = []
        # L2 is off in every test here: these tests are about which app gets
        # limited on which channel, and reclaim is a separate ladder rung with
        # its own suite (test_reclaim_ladder).  The attribute has to exist
        # though -- _tick_memory_reclaim reads it on every separated tick.
        self.reclaim_enabled = False
        self.sample_calls = 0
        # Read on every tick by the early-prefetch check.  False plus a flat
        # window means "nothing is brewing", which is what these tests want:
        # they are about the disk channel, not about the memory warning path.
        self.is_memory_pre_oom = False
        self.swap = types.SimpleNamespace(enabled=False, mem_rise_rate=lambda: None)

    def adjust_resources(self, app_id, policy, **kwargs):
        self.adjust_calls.append((app_id, policy, kwargs))
        return True

    def set_limited_app_dominant(self, dominant, cgroups=None):
        pass

    def sample_memory(self):
        self.sample_calls += 1


def _candidate(app_id, pids=(111,)):
    """One top-consumer sample, in the shape both channels produce."""
    return {
        'app': {'id': app_id},
        'process': {'name': app_id.split('.')[0], 'pid': next(iter(pids), None)},
        'pids': list(pids),
        'extra_cgroups': [],
        'per_cgroup_mem_rss': {},
        'per_cgroup_cpu': {},
    }


def _balancer(sys_apps=None, disk_apps=None, disk_stale=False):
    b = DynamicBalancer.__new__(DynamicBalancer)  # no __init__: no BPF, no cgroups
    b.config = _Cfg()
    b.is_running = True
    b.io_ctl = _IoCtl()
    b.control_manager = _ControlManager()
    b.all_limits = LimitRegistry()
    b.top_prefetcher = _Prefetcher(sys_apps if sys_apps is not None else [_candidate('sys-app.scope')])
    b.disk_top_prefetcher = _Prefetcher(
        disk_apps if disk_apps is not None else [_candidate('hi-io.scope')], stale=disk_stale)
    b.resource_monitor = mock.Mock()
    b.resource_monitor.get_total_memory.return_value = 32000
    return b


def _state(**kwargs):
    state = _MonitorLoopState(default_idle_check_interval=10, idle_check_interval=10)
    state.current_time = NOW
    for key, value in kwargs.items():
        setattr(state, key, value)
    return state


def _limited(app_id, cpu_mem=False, io=False, name=None, public_id=None):
    return LimitedApp(
        public_app_id=public_id or app_id,
        app_name=name or app_id.split('.')[0],
        source="auto",
        limit_rates={'cpu_rate': 0.3, 'mem_rate': 0.1,
                     'disk_io_rate': {'read': 20, 'write': 10,
                                      'read_iops': 8000, 'write_iops': 1000}},
        limit_parts={'cpu_mem_limited': cpu_mem, 'io_limited': io},
        cgroups=[app_id],
        limit_disks=['nvme0n1'] if io else [],
        pids={111},
        limited_at=NOW,
    )


class CandidateBatchOwnershipTests(unittest.TestCase):
    """A held batch belongs to the channel and the episode that sampled it."""

    def _tick(self, b, state, pressure, disk_level):
        """Run one separated tick with the decision helpers stubbed out, so the
        assertions are about which candidates reach the apply step."""
        with mock.patch.object(b, '_update_dominant_flag_from_top'), \
                mock.patch.object(b, '_apply_resource_limits') as apply_mock, \
                mock.patch.object(b, '_target_still_present', return_value=True), \
                mock.patch.object(b, '_handle_critical_pressure',
                                  side_effect=lambda apps, reached: (
                                      True, False, apps[0]['app']['id'], {'cpu_rate': 0.3})), \
                mock.patch.object(b, '_handle_disk_io_stressed',
                                  side_effect=lambda apps: (
                                      True, False, apps[0]['app']['id'], {}, apps[0], 0)):
            b._tick_separated_policy(state, pressure, disk_level, True)
        return apply_mock

    def test_disk_batch_is_never_spent_on_a_sys_critical(self):
        """The field regression: candidates warmed for a disk-IO episode were still in
        the slot when CPU/memory went critical, so the sys path capped a disk-IO app
        (one that had since exited) instead of the process actually eating the CPU."""
        b = _balancer()
        state = _state(top_consume_apps=[_candidate('hi-io.scope')],
                       top_source='disk_io', top_fetched_at=NOW - 1, reach_threshold=True)

        apply_mock = self._tick(b, state, pressure="critical", disk_level="low")

        self.assertEqual(b.top_prefetcher.resolved, 1, "sys channel must sample its own batch")
        self.assertEqual(apply_mock.call_args.args[1], 'sys-app.scope')

    def test_sys_batch_is_never_spent_on_a_disk_critical(self):
        """The mirror image: the sys ranking is by CPU/memory, so its #1 need not be
        doing any IO at all."""
        b = _balancer()
        state = _state(top_consume_apps=[_candidate('sys-app.scope')],
                       top_source='sys', top_fetched_at=NOW - 1, reach_threshold=False)

        apply_mock = self._tick(b, state, pressure="high", disk_level="critical")

        self.assertEqual(b.disk_top_prefetcher.resolved, 1)
        self.assertEqual(apply_mock.call_args.args[1], 'hi-io.scope')

    def test_batch_older_than_the_ttl_is_resampled(self):
        """Same channel, but the pressure that justified the batch is long gone."""
        b = _balancer()
        state = _state(top_consume_apps=[_candidate('stale-app.scope')],
                       top_source='sys', reach_threshold=True,
                       top_fetched_at=NOW - _MonitorLoopState.TOP_BATCH_TTL - 1)

        apply_mock = self._tick(b, state, pressure="critical", disk_level="low")

        self.assertEqual(apply_mock.call_args.args[1], 'sys-app.scope')

    def test_fresh_batch_of_the_same_channel_is_reused(self):
        """The batch is held on purpose -- resampling is a multi-second pipeline."""
        b = _balancer()
        state = _state(top_consume_apps=[_candidate('sys-app.scope'), _candidate('second.scope')],
                       top_source='sys', top_fetched_at=NOW - 1, reach_threshold=True)

        self._tick(b, state, pressure="critical", disk_level="low")

        self.assertEqual(b.top_prefetcher.resolved, 0, "must not resample a fresh batch")

    def test_disk_high_holds_its_batch_within_the_episode(self):
        """At "high" nothing is throttled; the batch is kept for the critical tick it
        was warmed for."""
        b = _balancer()
        state = _state()

        self._tick(b, state, pressure="high", disk_level="high")

        self.assertEqual([c['app']['id'] for c in state.top_consume_apps], ['hi-io.scope'])
        self.assertEqual(state.top_source, 'disk_io')

    def test_held_disk_batch_is_dropped_when_the_stress_ends(self):
        """Once the disk calms down the held batch describes an episode that is over."""
        b = _balancer()
        state = _state(top_consume_apps=[_candidate('hi-io.scope')],
                       top_source='disk_io', top_fetched_at=NOW - 1, reach_threshold=True,
                       disk_high_since=NOW - 60)

        b._maybe_trigger_prefetch(state, pressure="low", disk_level="low", passive_enabled=True)

        self.assertEqual(state.top_consume_apps, [])
        self.assertIsNone(state.top_source)
        self.assertFalse(state.reach_threshold)

    def test_reach_threshold_does_not_survive_a_dropped_batch(self):
        """The disk channel pins reach_threshold to True; leaking that into the sys
        channel is what let a below-threshold sample be limited anyway."""
        state = _state(top_consume_apps=[_candidate('hi-io.scope')],
                       top_source='disk_io', top_fetched_at=NOW, reach_threshold=True)

        self.assertIsNotNone(state.stale_top_batch_reason('sys', NOW))
        state.drop_top_batch()
        self.assertFalse(state.reach_threshold)

    def test_system_critical_adds_cpu_mem_limit_to_io_limited_target(self):
        """An I/O-only cap must not block CPU/mem protection for the same cgroup."""
        b = _balancer()
        b.all_limits.apps['sys-app.scope'] = _limited('sys-app.scope', io=True)
        state = _state()

        with mock.patch.object(b, '_apply_resource_limits') as apply_mock, \
                mock.patch.object(b, '_target_still_present', return_value=True), \
                mock.patch.object(b, '_handle_critical_pressure',
                                  return_value=(True, False, 'sys-app.scope', {'cpu_rate': 0.3})):
            b._tick_separated_policy(state, 'critical', 'low', True)

        apply_mock.assert_called_once()
        self.assertFalse(apply_mock.call_args.kwargs['is_disk_io_stressed'])

    def test_system_critical_does_not_repeat_existing_cpu_mem_limit(self):
        """The system channel still skips a cgroup that already has its own CPU/mem cap."""
        b = _balancer()
        b.all_limits.apps['sys-app.scope'] = _limited('sys-app.scope', cpu_mem=True)
        state = _state()

        with mock.patch.object(b, '_apply_resource_limits') as apply_mock, \
                mock.patch.object(b, '_target_still_present', return_value=True), \
                mock.patch.object(b, '_handle_critical_pressure',
                                  return_value=(True, False, 'sys-app.scope', {'cpu_rate': 0.3})):
            b._tick_separated_policy(state, 'critical', 'low', True)

        apply_mock.assert_not_called()

    def test_system_critical_is_not_blocked_by_disk_high(self):
        """Disk high only holds an IO candidate; system critical still limits memory/CPU."""
        b = _balancer()
        state = _state()

        with mock.patch.object(b, '_apply_resource_limits') as apply_mock, \
                mock.patch.object(b, '_target_still_present', return_value=True), \
                mock.patch.object(b, '_handle_critical_pressure',
                                  return_value=(True, False, 'sys-app.scope', {'cpu_rate': 0.3})):
            b._tick_separated_policy(state, 'critical', 'high', True)

        apply_mock.assert_called_once()
        self.assertFalse(apply_mock.call_args.kwargs['is_disk_io_stressed'])
        self.assertEqual(b.disk_top_prefetcher.resolved, 0)

    def test_unmanaged_system_candidate_uses_sampled_cgroup_for_restoration(self):
        """A bare process name must not be retained as the cgroup restore target."""
        b = _balancer()
        b.resource_monitor.get_resource_usage.return_value = {
            'cpu': {'is_busy': False},
            'memory': {'is_busy': False},
        }
        candidate = _candidate('optimum-cli')
        candidate['cgroup'] = '/sys/fs/cgroup/user.slice/user-1000.slice/session-6215.scope'

        with mock.patch.object(balancer_mod.app_utils, 'get_app_control_info',
                               return_value=(False, None)):
            should_adjust, is_controlled, target_id, _ = b._handle_critical_pressure(
                [candidate], True)

        self.assertTrue(should_adjust)
        self.assertFalse(is_controlled)
        self.assertEqual(target_id, 'session-6215.scope')
        self.assertEqual(candidate['public_app_id'], 'optimum-cli')


class EarlyPrefetchTests(unittest.TestCase):
    """Warming the memory cache before the pressure *level* moves.

    On the incident run the level went low -> critical in a single refresh, so
    the "entering_high" edge never fired once and the first critical tick paid
    a 6.45 s synchronous fetch while the OOM killer was already running.  Two
    signals catch that climb earlier: the pre-OOM band (70% used, true 10 s
    before the level moved) and the memory slope (true 15 s before).
    """

    def _b(self, pre_oom=False, rise=None):
        b = _balancer()
        b.control_manager.is_memory_pre_oom = pre_oom
        b.control_manager.swap = types.SimpleNamespace(
            enabled=False, mem_rise_rate=lambda: rise)
        return b

    def _tick(self, b, state, pressure="low"):
        b._maybe_trigger_prefetch(state, pressure=pressure, disk_level="low",
                                  passive_enabled=True)
        return b.top_prefetcher.started

    def test_entering_the_band_warms_the_cache_while_the_level_says_low(self):
        b = self._b(pre_oom=True)
        started = self._tick(b, _state())
        self.assertEqual(started, ["entering_pre_oom"])
        # And specifically *not* via the edge that never fires in practice.
        self.assertNotIn("entering_high", started)

    def test_sitting_in_the_band_does_not_resample(self):
        """Edge-triggered: a NAS that simply runs warm must not resample every
        tick.  Only the crossing is news."""
        b = self._b(pre_oom=True)
        state = _state()
        self._tick(b, state)
        self._tick(b, state)
        self.assertEqual(b.top_prefetcher.started, ["entering_pre_oom"])

    def test_leaving_and_re_entering_the_band_warms_again(self):
        b = self._b(pre_oom=True)
        state = _state()
        self._tick(b, state)
        b.control_manager.is_memory_pre_oom = False
        self._tick(b, state)
        b.control_manager.is_memory_pre_oom = True
        self._tick(b, state)
        self.assertEqual(b.top_prefetcher.started,
                         ["entering_pre_oom", "entering_pre_oom"])

    def test_a_fast_climb_warms_the_cache_below_the_band(self):
        """The backstop for the worst case: the band leaves ~10 s of lead and a
        contended fetch was measured at 9.44 s.  16% -> 55% in five seconds is
        39 points, well past the trigger, and that happened 15 s earlier."""
        b = self._b(pre_oom=False, rise=0.39)
        self.assertEqual(self._tick(b, _state()), ["memory_climbing"])

    def test_the_trigger_is_the_configured_threshold(self):
        b = self._b(pre_oom=False, rise=MEM_RISE_TRIGGER)
        self.assertEqual(self._tick(b, _state()), ["memory_climbing"])
        b = self._b(pre_oom=False, rise=MEM_RISE_TRIGGER - 0.01)
        self.assertEqual(self._tick(b, _state()), [])

    def test_memory_being_released_is_not_a_trigger(self):
        b = self._b(pre_oom=False, rise=-0.30)
        self.assertEqual(self._tick(b, _state()), [])

    def test_too_little_history_is_not_a_trigger(self):
        """mem_rise_rate() returns None on a cold start; nobody prefetches
        because the process has only just come up."""
        b = self._b(pre_oom=False, rise=None)
        self.assertEqual(self._tick(b, _state()), [])

    def test_the_band_wins_over_the_slope(self):
        """Both true at once is one fetch, not two -- the band edge returns."""
        b = self._b(pre_oom=True, rise=0.39)
        self.assertEqual(self._tick(b, _state()), ["entering_pre_oom"])

    def test_passive_control_off_suppresses_both(self):
        """Nothing consumes the sampling when passive control is off, so paying
        several seconds for it is pure waste."""
        b = self._b(pre_oom=True, rise=0.39)
        b._maybe_trigger_prefetch(_state(), pressure="low", disk_level="low",
                                  passive_enabled=False)
        self.assertEqual(b.top_prefetcher.started, [])


class CriticalResolveCacheTests(unittest.TestCase):
    """What counts as "the cache has an answer".

    The bug: validity was tested as ``if apps:``, so "a fetch completed and
    found nobody above the threshold" -- the normal state of an idle machine --
    was indistinguishable from "no fetch has ever run".  Every critical event
    on such a box therefore paid the full multi-second blocking fetch to be
    told ``[]`` again, on the one code path where seconds cost the most.
    """

    def _settle(self, prefetcher, timeout=2.0):
        deadline = time.time() + timeout
        while prefetcher._inflight.is_set() and time.time() < deadline:
            time.sleep(0.005)
        self.assertFalse(prefetcher._inflight.is_set(), "prefetch never finished")

    def test_a_completed_empty_fetch_is_an_answer_not_a_cold_start(self):
        calls = []

        def fetch():
            calls.append(1)
            return [], False

        p = TopConsumerPrefetcher(fetch)
        p.start("warm")
        self._settle(p)
        self.assertEqual(len(calls), 1)

        apps, threshold = p.resolve_for_critical()
        self.assertEqual(apps, [])
        self.assertFalse(threshold)
        self.assertEqual(len(calls), 1, "must not resample to be told [] again")

    def test_a_real_cold_start_still_falls_back_to_a_blocking_fetch(self):
        """The fallback has to survive: the first critical event after boot
        must not proceed with no idea who is eating the machine."""
        calls = []

        def fetch():
            calls.append(1)
            return [_candidate('sys-app.scope')], True

        apps, threshold = TopConsumerPrefetcher(fetch).resolve_for_critical()
        self.assertEqual(len(calls), 1)
        self.assertEqual([c['app']['id'] for c in apps], ['sys-app.scope'])
        self.assertTrue(threshold)

    def test_a_stale_empty_cache_is_returned_now_and_refreshed_behind(self):
        """Same rule past MAX_CACHE_AGE: answer from the cache immediately,
        resample in the background.  Blocking is never the right trade here."""
        calls = []

        def fetch():
            calls.append(1)
            return [], False

        p = TopConsumerPrefetcher(fetch)
        p.start("warm")
        self._settle(p)
        with p._lock:
            p._cache["fetched_at"] = time.time() - p.MAX_CACHE_AGE - 1

        apps, _ = p.resolve_for_critical()
        self.assertEqual(apps, [])
        self._settle(p)
        self.assertEqual(len(calls), 2, "background refresh should have run")


class DiskPrefetchPriorityTests(unittest.TestCase):
    """Memory outranks disk when both channels are loaded.

    Asymmetric costs: the memory fetch stretches from 1.17 s to 9.44 s under
    CPU contention and has an OOM kill at the end of it, while disk pressure
    has no equivalent cliff -- the worst case there is a few seconds of slow.
    """

    def _b(self):
        b = _balancer()
        b.control_manager.is_memory_pre_oom = False
        b.control_manager.swap = types.SimpleNamespace(
            enabled=False, mem_rise_rate=lambda: None)
        return b

    def test_the_disk_edge_is_deferred_while_memory_is_critical(self):
        b = self._b()
        b._maybe_trigger_prefetch(_state(prev_disk_level="low"),
                                  pressure="critical", disk_level="high",
                                  passive_enabled=True)
        self.assertEqual(b.disk_top_prefetcher.started, [])

    def test_the_disk_edge_fires_normally_when_memory_is_merely_high(self):
        b = self._b()
        b._maybe_trigger_prefetch(_state(prev_disk_level="low"),
                                  pressure="high", disk_level="high",
                                  passive_enabled=True)
        self.assertEqual(b.disk_top_prefetcher.started, ["entering_disk_high"])

    def test_the_episode_timer_keeps_running_while_deferred(self):
        """The deferral gates the fetch only.  Clearing disk_high_since here
        would tell the channel the episode ended, and it would drop a batch it
        is still holding."""
        b = self._b()
        state = _state(prev_disk_level="low",
                       top_consume_apps=[_candidate('hi-io.scope')],
                       top_source='disk_io', top_fetched_at=NOW - 1)
        b._maybe_trigger_prefetch(state, pressure="critical", disk_level="high",
                                  passive_enabled=True)
        self.assertEqual(state.disk_high_since, NOW)
        self.assertEqual([c['app']['id'] for c in state.top_consume_apps],
                         ['hi-io.scope'])

    def test_a_deferred_sustained_recheck_retries_on_the_next_calm_tick(self):
        """The timer is deliberately left alone when the recheck is skipped, so
        it happens as soon as memory is out of the way rather than waiting
        another full SUSTAINED_CRITICAL_REFRESH_SEC."""
        b = self._b()
        due = NOW - TopConsumerPrefetcher.SUSTAINED_CRITICAL_REFRESH_SEC - 1
        state = _state(prev_disk_level="high", disk_high_since=due,
                       disk_last_recheck_time=due)
        b._maybe_trigger_prefetch(state, pressure="critical", disk_level="high",
                                  passive_enabled=True)
        self.assertEqual(b.disk_top_prefetcher.started, [])
        self.assertEqual(state.disk_last_recheck_time, due, "timer must not advance")

        state.current_time = NOW + 1
        b._maybe_trigger_prefetch(state, pressure="high", disk_level="high",
                                  passive_enabled=True)
        self.assertEqual(b.disk_top_prefetcher.started, ["sustained_disk_recheck"])
        self.assertEqual(state.disk_last_recheck_time, NOW + 1)

    def test_a_disk_only_stress_is_untouched_by_any_of_this(self):
        """The common case has to be byte-for-byte what it was: memory calm,
        disk climbing, both the edge and the sustained recheck fire."""
        b = self._b()
        due = NOW - TopConsumerPrefetcher.SUSTAINED_CRITICAL_REFRESH_SEC - 1
        state = _state(prev_disk_level="low")
        b._maybe_trigger_prefetch(state, pressure="low", disk_level="critical",
                                  passive_enabled=True)
        state.prev_disk_level = "critical"
        state.disk_last_recheck_time = due
        b._maybe_trigger_prefetch(state, pressure="low", disk_level="critical",
                                  passive_enabled=True)
        self.assertEqual(b.disk_top_prefetcher.started,
                         ["entering_disk_high", "sustained_disk_recheck"])


class AutoLimitRestoreFailureTests(unittest.TestCase):
    """A failed auto-limit restore must remain retryable."""

    def test_failed_restore_keeps_auto_limit_registered(self):
        b = _balancer()
        entry = _limited('session-8740.scope', cpu_mem=True)
        b.all_limits.apps['session-8740.scope'] = entry

        with mock.patch.object(b, '_restore_entry', return_value=False):
            ok, message = b.restore_auto_limited('session-8740.scope')

        self.assertFalse(ok)
        self.assertEqual(message, 'Failed to restore resources for this app')
        self.assertIs(b.all_limits.apps['session-8740.scope'], entry)
        self.assertEqual(b.all_limits.list_exclusions(), [])


class GoneTargetTests(unittest.TestCase):
    """A candidate is a snapshot; the app may have exited before we act on it."""

    def test_dead_target_is_not_limited(self):
        b = _balancer()
        state = _state()
        with mock.patch.object(b, '_update_dominant_flag_from_top'), \
                mock.patch.object(b, '_apply_resource_limits') as apply_mock, \
                mock.patch.object(b, '_pid_gone_or_dying', return_value=True), \
                mock.patch.object(b, '_cgroup_exists', return_value=False), \
                mock.patch.object(b, '_handle_critical_pressure',
                                  return_value=(True, False, 'sys-app.scope', {'cpu_rate': 0.3})):
            b._tick_separated_policy(state, "critical", "low", True)
        apply_mock.assert_not_called()

    def test_a_live_pid_is_enough(self):
        b = _balancer()
        with mock.patch.object(b, '_pid_gone_or_dying', return_value=False):
            self.assertTrue(b._target_still_present(_candidate('x.scope'), 'x.scope'))

    def test_surviving_cgroup_covers_a_churned_pid_snapshot(self):
        """A long-lived scope whose sampled workers have exited is still limitable."""
        b = _balancer()
        with mock.patch.object(b, '_pid_gone_or_dying', return_value=True), \
                mock.patch.object(b, '_cgroup_exists', return_value=True):
            self.assertTrue(b._target_still_present(_candidate('x.scope'), 'x.scope'))

    def test_reaper_records_recovery_when_a_manual_limited_app_exits(self):
        b = _balancer()
        entry = _limited('exited.scope', cpu_mem=True, io=True, public_id='app-1')
        entry.source = 'manual'
        entry.protection_id = 'protect-1'
        b.all_limits.apps['exited.scope'] = entry

        with mock.patch.object(b, '_pid_gone_or_dying', return_value=True), \
                mock.patch.object(b, '_cgroup_exists', return_value=False), \
                mock.patch.object(balancer_mod, '_emit_control_events') as emit, \
                mock.patch.object(balancer_mod.app_utils, 'check_app_running_status', return_value='stopped'), \
                mock.patch.object(balancer_mod.app_utils, 'update_app_status'), \
                mock.patch.object(balancer_mod.app_utils.callback_manager, 'send_callback_notification'):
            b._reap_closed_apps()

        self.assertNotIn('exited.scope', b.all_limits.apps)
        recovered = [call for call in emit.call_args_list if call.args[0] == 'RECOVERED']
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].kwargs['protection_id'], 'protect-1')
        self.assertEqual(
            recovered[0].args[1],
            {'cpu_mem_limited': True, 'io_limited': True},
        )

    def test_reaper_restores_auto_handoff_without_pid_snapshot_when_app_is_stopped(self):
        b = _balancer()
        entry = _limited('optimum.scope', cpu_mem=True, public_id='optimum-app')
        entry.source = 'manual'
        entry.adopted_from_auto = True
        entry.pids = set()
        b.all_limits.apps['optimum.scope'] = entry

        with mock.patch.object(b, '_cgroup_exists', return_value=False), \
                mock.patch.object(balancer_mod.app_utils, 'check_app_running_status', return_value='stopped'), \
                mock.patch.object(balancer_mod.app_utils, 'update_app_status'), \
                mock.patch.object(balancer_mod.app_utils.callback_manager, 'send_callback_notification'):
            b._reap_closed_apps()

        self.assertNotIn('optimum.scope', b.all_limits.apps)

    def test_reaper_keeps_no_pid_limit_when_app_still_runs(self):
        b = _balancer()
        entry = _limited('active.scope', cpu_mem=True, public_id='active-app')
        entry.source = 'manual'
        entry.pids = set()
        b.all_limits.apps['active.scope'] = entry

        with mock.patch.object(balancer_mod.app_utils, 'check_app_running_status', return_value='running'):
            b._reap_closed_apps()

        self.assertIn('active.scope', b.all_limits.apps)

    def test_reaper_restores_auto_discovery_when_representative_exits_in_shared_scope(self):
        b = _balancer()
        entry = _limited('session-7782.scope', io=True, public_id='session-7782.scope')
        entry.source = 'auto'
        entry.app_name = 'optimum-cli'
        entry.pids = {2007693, 1974386}
        entry.representative_pid = 2007693
        b.all_limits.apps['session-7782.scope'] = entry

        def gone(pid):
            return pid == 2007693

        with mock.patch.object(b, '_pid_gone_or_dying', side_effect=gone), \
                mock.patch.object(balancer_mod.app_utils, 'check_app_running_status', return_value='stopped'), \
                mock.patch.object(balancer_mod.app_utils, 'update_app_status'), \
                mock.patch.object(balancer_mod.app_utils.callback_manager, 'send_callback_notification'):
            b._reap_closed_apps()

        self.assertNotIn('session-7782.scope', b.all_limits.apps)

    def test_reaper_keeps_shared_scope_limit_when_representative_app_still_runs(self):
        b = _balancer()
        entry = _limited('session-7782.scope', io=True, public_id='session-7782.scope')
        entry.source = 'auto'
        entry.app_name = 'optimum-cli'
        entry.pids = {2007693, 1974386}
        entry.representative_pid = 2007693
        b.all_limits.apps['session-7782.scope'] = entry

        def gone(pid):
            return pid == 2007693

        with mock.patch.object(b, '_pid_gone_or_dying', side_effect=gone), \
                mock.patch.object(balancer_mod.app_utils, 'check_app_running_status', return_value='running'):
            b._reap_closed_apps()

        self.assertIn('session-7782.scope', b.all_limits.apps)


class AutoToManualScopeTests(unittest.TestCase):
    """Taking control must retain exactly the cgroups Auto Control limited."""

    def test_manual_limit_reclaims_successful_cgroup_to_its_memory_high(self):
        b = _balancer()
        usage = {
            'cgroup_paths': ['/user.slice/user-1000.slice/app-test.scope'],
            'pids': [111],
            'cpu_percent': 100,
            'mem_current': 1000,
            'mem_swap_current': 0,
            'per_cgroup_mem': {'app-test.scope': 1000},
            'per_cgroup_cpu_delta': {'app-test.scope': 100},
            'io_read_mb': 0,
            'io_write_mb': 0,
            'io_read_iops': 0,
            'io_write_iops': 0,
        }
        b.control_manager.adjust_resources = mock.Mock(return_value=True)
        b.control_manager.reclaim_memory_to_target = mock.Mock(
            return_value=types.SimpleNamespace(
                reclaimed_bytes=100 * 1024 * 1024, elapsed_ms=100,
                ok=True, reason="target reached",
            )
        )
        b.get_limited_rates = mock.Mock(return_value={
            'cpu_rate': 0.3,
            'mem_rate': 0.5,
            'disk_io_rate': {},
        })

        with mock.patch.object(balancer_mod.app_utils, 'get_app_resource_usage', return_value=usage), \
                mock.patch.object(balancer_mod.app_utils, 'get_cgroup_path_by_pid',
                                  return_value=usage['cgroup_paths'][0]), \
                mock.patch.object(b, '_is_io_limit_reached', return_value=False), \
                mock.patch.object(balancer_mod.app_utils, 'update_app_status'), \
                mock.patch.object(balancer_mod.app_utils.callback_manager,
                                  'send_callback_notification'), \
                mock.patch.object(balancer_mod, '_emit_control_events'):
            result = b.set_resource_limit('test-app', 'test app', 'low')

        self.assertTrue(result)
        (cgroup, target), kwargs = b.control_manager.reclaim_memory_to_target.call_args
        self.assertEqual((cgroup, target), ('app-test.scope', 500 * 1024 * 1024))
        # The growth gate must be the balancer's own rate limiter, or a manual
        # limit can allocate a swapfile a periodic L0 check just allocated.
        b._last_swap_growth_at = 0.0
        b._swap_growth_lock = threading.Lock()
        self.assertTrue(kwargs['growth_gate']())
        self.assertFalse(kwargs['growth_gate']())
        entry = b.all_limits.apps['app-test.scope']
        self.assertEqual(entry.source, 'manual')

    def test_manual_limit_reclaims_with_the_operation_lock_released(self):
        """A reclaim blocking for seconds must not stall the pressure loop."""
        b = _balancer()
        usage = {
            'cgroup_paths': ['/user.slice/user-1000.slice/app-test.scope'],
            'pids': [111],
            'cpu_percent': 100,
            'mem_current': 1000,
            'mem_swap_current': 0,
            'per_cgroup_mem': {'app-test.scope': 1000},
            'per_cgroup_cpu_delta': {'app-test.scope': 100},
            'io_read_mb': 0,
            'io_write_mb': 0,
            'io_read_iops': 0,
            'io_write_iops': 0,
        }
        b.control_manager.adjust_resources = mock.Mock(return_value=True)
        held_during_reclaim = []

        def lock_is_held():
            # acquire(blocking=False) on this thread would succeed on an RLock we
            # already own, so a second thread has to do the asking -- and the same
            # thread must release it, or RLock raises on the foreign release.
            probe = []

            def attempt():
                acquired = b.all_limits.operation_lock.acquire(blocking=False)
                probe.append(acquired)
                if acquired:
                    b.all_limits.operation_lock.release()

            thread = threading.Thread(target=attempt)
            thread.start()
            thread.join()
            return not (probe and probe[0])

        def observe(cgroup, target, growth_gate=None):
            held_during_reclaim.append(lock_is_held())
            return types.SimpleNamespace(
                reclaimed_bytes=0, elapsed_ms=0, ok=True, reason="noop")

        b.control_manager.reclaim_memory_to_target = mock.Mock(side_effect=observe)
        b.get_limited_rates = mock.Mock(return_value={
            'cpu_rate': 0.3, 'mem_rate': 0.5, 'disk_io_rate': {},
        })

        with mock.patch.object(balancer_mod.app_utils, 'get_app_resource_usage', return_value=usage), \
                mock.patch.object(balancer_mod.app_utils, 'get_cgroup_path_by_pid',
                                  return_value=usage['cgroup_paths'][0]), \
                mock.patch.object(b, '_is_io_limit_reached', return_value=False), \
                mock.patch.object(b, '_claim_swap_growth_slot', return_value=False), \
                mock.patch.object(balancer_mod.app_utils, 'update_app_status'), \
                mock.patch.object(balancer_mod.app_utils.callback_manager,
                                  'send_callback_notification'), \
                mock.patch.object(balancer_mod, '_emit_control_events'):
            b.set_resource_limit('test-app', 'test app', 'low')

        self.assertEqual(held_during_reclaim, [False])

    def test_manual_limit_skips_reclaim_when_no_memory_cap_was_applied(self):
        b = _balancer()
        usage = {
            'cgroup_paths': ['/user.slice/user-1000.slice/app-test.scope'],
            'pids': [111],
            'cpu_percent': 100,
            'mem_current': 1000,
            'mem_swap_current': 0,
            'per_cgroup_mem': {'app-test.scope': 1000},
            'per_cgroup_cpu_delta': {'app-test.scope': 100},
            'io_read_mb': 0,
            'io_write_mb': 0,
            'io_read_iops': 0,
            'io_write_iops': 0,
        }
        b.control_manager.adjust_resources = mock.Mock(return_value=True)
        b.control_manager.reclaim_memory_to_target = mock.Mock()
        b.get_limited_rates = mock.Mock(return_value={
            'cpu_rate': 0.3, 'mem_rate': None, 'disk_io_rate': {},
        })

        with mock.patch.object(balancer_mod.app_utils, 'get_app_resource_usage', return_value=usage), \
                mock.patch.object(balancer_mod.app_utils, 'get_cgroup_path_by_pid',
                                  return_value=usage['cgroup_paths'][0]), \
                mock.patch.object(b, '_is_io_limit_reached', return_value=False), \
                mock.patch.object(balancer_mod.app_utils, 'update_app_status'), \
                mock.patch.object(balancer_mod.app_utils.callback_manager,
                                  'send_callback_notification'), \
                mock.patch.object(balancer_mod, '_emit_control_events'):
            self.assertTrue(b.set_resource_limit('test-app', 'test app', 'low'))

        b.control_manager.reclaim_memory_to_target.assert_not_called()

    def test_manual_limit_rejects_an_auto_limited_target_cgroup(self):
        b = _balancer()
        b.all_limits.apps['session-8740.scope'] = _limited('session-8740.scope', cpu_mem=True)
        usage = {
            'cgroup_paths': ['/user.slice/user-1000.slice/session-8740.scope'],
            'pids': [111],
        }

        with mock.patch.object(balancer_mod.app_utils, 'get_app_resource_usage', return_value=usage), \
                mock.patch.object(balancer_mod.app_utils, 'get_cgroup_path_by_pid', return_value=usage['cgroup_paths'][0]), \
                mock.patch.object(b.control_manager, 'adjust_resources') as adjust, \
                mock.patch.object(b.io_ctl, 'set_disk_io_throttle') as set_io:
            result = b.set_resource_limit('optimum-app', 'optimum-cli', 'low')

        self.assertIn('already auto-limited', result['skipped'])
        adjust.assert_not_called()
        set_io.assert_not_called()
        self.assertEqual(b.all_limits.apps['session-8740.scope'].source, 'auto')

    def test_manual_limit_rejects_an_extra_cgroup_of_an_auto_limit(self):
        b = _balancer()
        entry = _limited('primary.scope', cpu_mem=True)
        entry.cgroups = ['primary.scope', 'session-8740.scope']
        b.all_limits.apps['primary.scope'] = entry
        usage = {
            'cgroup_paths': ['/user.slice/user-1000.slice/session-8740.scope'],
            'pids': [111],
        }

        with mock.patch.object(balancer_mod.app_utils, 'get_app_resource_usage', return_value=usage), \
                mock.patch.object(balancer_mod.app_utils, 'get_cgroup_path_by_pid', return_value=usage['cgroup_paths'][0]), \
                mock.patch.object(b.control_manager, 'adjust_resources') as adjust:
            result = b.set_resource_limit('optimum-app', 'optimum-cli', 'low')

        self.assertIn('session-8740.scope', result['skipped'])
        adjust.assert_not_called()
        self.assertIs(b.all_limits.apps['primary.scope'], entry)

    def test_auto_limit_skips_a_manual_limited_target_cgroup(self):
        b = _balancer()
        entry = _limited('session-8740.scope', cpu_mem=True)
        entry.source = 'manual'
        b.all_limits.apps['session-8740.scope'] = entry
        target = {
            'app': {'name': 'optimum-cli'},
            'process': {'name': 'optimum-cli'},
            'extra_cgroups': [],
        }

        with mock.patch.object(b.control_manager, 'adjust_resources') as adjust, \
                mock.patch.object(b.io_ctl, 'set_disk_io_throttle') as set_io:
            b._apply_resource_limits(
                target, 'session-8740.scope', {'cpu_rate': 0.3, 'mem_rate': 0.1},
                is_controlled=True,
            )

        adjust.assert_not_called()
        set_io.assert_not_called()
        self.assertIs(b.all_limits.apps['session-8740.scope'], entry)

    def test_bare_shell_yields_to_a_script_representative(self):
        self.assertFalse(_is_identifiable_representative('bash', 'bash'))
        self.assertTrue(_is_identifiable_representative(
            'bash', 'bash /opt/tools/optimum-cli export openvino'))

    def test_bare_python_yields_to_a_script_representative(self):
        self.assertFalse(_is_identifiable_representative('python3', 'python3 -i'))
        self.assertTrue(_is_identifiable_representative(
            'python3', 'python3 /opt/workloads/run.py --fast'))

    def test_disk_candidate_uses_the_dominant_pid_not_an_arbitrary_scope_member(self):
        monitor = ResourceMonitor.__new__(ResourceMonitor)
        monitor._get_top_processes = mock.Mock(return_value=[{
            'pids': {101, 202},
            'cgroup': '/user.slice/user-1000.slice/session-5383.scope',
            'names': {'bash', 'MainThread'},
            'cmdlines': {
                '/bin/bash --init-file shellIntegration-bash.sh',
                'node bootstrap-fork --type=extensionHost',
            },
            'dominant_pid': 202,
            'dominant_name': 'MainThread',
            'dominant_cmdline': 'node bootstrap-fork --type=extensionHost',
            'score': 10,
            'io_read_rate': 1,
            'io_write_rate': 2,
            'io_read_iops': 3,
            'io_write_iops': 4,
            'io_per_disk': {},
            'extra_cgroups': [],
        }])
        monitor.try_match_app = mock.Mock(return_value={
            'id': 'bootstrap-fork', 'name': 'bootstrap-fork',
        })

        candidate = monitor.get_top_disk_io_consumers()[0]

        self.assertEqual(candidate['process']['pid'], 202)
        self.assertEqual(candidate['process']['cmdline'], 'node bootstrap-fork --type=extensionHost')
        self.assertEqual(candidate['cgroup'], '/user.slice/user-1000.slice/session-5383.scope')

    def test_auto_limit_row_exposes_the_representative_process_pid(self):
        b = _balancer()
        b._upsert_auto_limited(
            'tmux-spawn.scope', 'tmux-spawn.scope', 'Tmux Spawn', {'cpu_rate': 0.3},
            resource_limited=True,
            io_limited=False,
            priority='low',
            is_controlled=False,
            limit_reason='system_pressure',
            pressure_level='critical',
            cgroups=['tmux-spawn.scope'],
            pids=[101, 102],
            representative_pid=101,
        )

        rows = b.get_auto_limited_apps()['apps']
        self.assertEqual(rows[0]['representative_pid'], 101)

    def test_auto_limit_row_lists_live_processes_for_each_limited_scope(self):
        b = _balancer()
        b._upsert_auto_limited(
            'tmux-spawn.scope', 'tmux-spawn.scope', 'Tmux Spawn', {'cpu_rate': 0.3},
            resource_limited=True,
            io_limited=False,
            priority='low',
            is_controlled=False,
            limit_reason='system_pressure',
            pressure_level='critical',
            cgroups=['tmux-spawn.scope'],
        )
        process = mock.Mock()
        process.is_running.return_value = True
        process.status.return_value = 'running'
        process.name.return_value = 'worker'
        process.cmdline.return_value = ['worker', '--batch']

        with mock.patch.object(balancer_mod.app_utils, 'get_pids_in_cgroup', return_value=[101]), \
                mock.patch.object(balancer_mod.psutil, 'Process', return_value=process):
            rows = b.get_auto_limited_apps()['apps']

        self.assertEqual(rows[0]['scope_processes'], {
            'tmux-spawn.scope': [{
                'pid': 101,
                'process_name': 'worker',
                'cmdline': 'worker --batch',
            }],
        })

    def test_auto_limit_row_orders_representative_process_first(self):
        b = _balancer()
        b._upsert_auto_limited(
            'session-7782.scope', 'session-7782.scope', 'optimum-cli', {'disk_io_rate': {}},
            resource_limited=False,
            io_limited=True,
            priority='undefined',
            is_controlled=False,
            limit_reason='disk_pressure',
            pressure_level='critical',
            cgroups=['session-7782.scope'],
            pids=[1974342, 2014356],
            representative_pid=2014356,
        )

        def process_for(pid):
            process = mock.Mock()
            process.is_running.return_value = True
            process.status.return_value = 'running'
            if pid == 2014356:
                process.name.return_value = 'python'
                process.cmdline.return_value = ['python', 'optimum-cli']
            else:
                process.name.return_value = 'sshd'
                process.cmdline.return_value = ['sshd: user [priv]']
            return process

        with mock.patch.object(balancer_mod.app_utils, 'get_pids_in_cgroup', return_value=[1974342, 2014356]), \
                mock.patch.object(balancer_mod.psutil, 'Process', side_effect=process_for):
            rows = b.get_auto_limited_apps()['apps']

        self.assertEqual(rows[0]['scope_processes']['session-7782.scope'][0]['pid'], 2014356)

    def test_bpf_exit_persists_stopped_status(self):
        intercept = AppIntercept.__new__(AppIntercept)
        intercept.handled_processes = {197}
        intercept.app_live_pids = {'bench': {197}}
        intercept.monitored_app_launched = {197: ('bench.scope', 'bench', '', '')}
        intercept.pending_exit_events = {}
        intercept.is_process_alive = mock.Mock(return_value=False)

        with mock.patch.object(app_intercept_mod.app_utils.callback_manager,
                               'send_callback_notification') as notify:
            intercept.handle_exit_event(197, 'bench.scope', 'bench', '', '')

        notify.assert_called_once_with({
            'app_id': 'bench.scope',
            'app_name': 'bench',
            'status': 'stopped',
            'purpose': 'app',
        }, store=True)

    def test_processes_in_one_cgroup_render_as_one_control_instance(self):
        with mock.patch.object(
                balance_service_mod, 'get_app_processes_for_app',
                side_effect=lambda name, **_: {
                    'bootstrap-fork': [101], 'server-main.js': [102],
                }.get(name, [])), \
                mock.patch.object(balance_service_mod, '_runtime_status_for_pid', return_value='Running'), \
                mock.patch.object(balance_service_mod, '_cgroup_for_pid', return_value='session-5383.scope'), \
                mock.patch.object(balance_service_mod, '_exact_cgroup_member_pids', return_value=[101, 102, 103]), \
                mock.patch.object(balance_service_mod, '_process_name_for_pid',
                                  side_effect=lambda pid, fallback: {101: 'bootstrap-fork', 102: 'server-main.js', 103: 'worker'}.get(pid, fallback)), \
                mock.patch.object(balance_service_mod, '_cmdline_for_pid', return_value='bootstrap-fork'):
            rows, summary, runtime = balance_service_mod._build_process_scope_snapshot(
                app_id='bootstrap.id',
                app_name='bootstrap',
                process_names=['bootstrap-fork', 'server-main.js'],
                app_status='a_limited',
                limited_pid_snapshot=[101],
                limited_cgroup_snapshot=['session-5383.scope'],
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['cgroup'], 'session-5383.scope')
        self.assertEqual(rows[0]['process_name'], 'bootstrap-fork')
        self.assertEqual(rows[0]['scope_processes'], [
            {'pid': 101, 'process_name': 'bootstrap-fork', 'cmdline': 'bootstrap-fork'},
            {'pid': 102, 'process_name': 'server-main.js', 'cmdline': 'bootstrap-fork'},
            {'pid': 103, 'process_name': 'worker', 'cmdline': 'bootstrap-fork'},
        ])
        self.assertEqual(rows[0]['limit_status'], 'Limited')
        self.assertEqual((summary, runtime), ('Limited', 'Running'))

    def test_scope_member_with_an_older_pid_does_not_replace_matched_representative(self):
        with mock.patch.object(
                balance_service_mod, 'get_app_processes_for_app', return_value=[197]), \
                mock.patch.object(balance_service_mod, '_runtime_status_for_pid', return_value='Running'), \
                mock.patch.object(balance_service_mod, '_cgroup_for_pid', return_value='session-5613.scope'), \
                mock.patch.object(balance_service_mod, '_exact_cgroup_member_pids', return_value=[104, 197]), \
                mock.patch.object(balance_service_mod, '_cmdline_for_pid',
                                  side_effect=lambda pid, fallback: 'benchmark_app --model model.xml' if pid == 197 else 'sshd: user [priv]'):
            rows, _, _ = balance_service_mod._build_process_scope_snapshot(
                app_id='session-5613.scope',
                app_name='bench',
                process_names=['benchmark_app'],
                app_status='running',
                limited_pid_snapshot=[],
                limited_cgroup_snapshot=[],
            )

        self.assertEqual(rows[0]['pid'], 197)
        self.assertEqual(rows[0]['cmdline'], 'benchmark_app --model model.xml')
        self.assertEqual([process['pid'] for process in rows[0]['scope_processes']], [104, 197])

    def test_later_matching_cgroup_stays_visible_without_inheriting_the_limit(self):
        with mock.patch.object(
                balance_service_mod, 'get_app_processes_for_app', return_value=[101, 102]), \
                mock.patch.object(balance_service_mod, '_runtime_status_for_pid', return_value='Running'), \
                mock.patch.object(
                    balance_service_mod, '_cgroup_for_pid',
                    side_effect=lambda pid, _fallback: {
                        101: 'bootstrap-limited.scope', 102: 'bootstrap-new.scope',
                    }[pid]), \
                mock.patch.object(balance_service_mod, '_cmdline_for_pid', return_value='bootstrap-fork'):
            rows, summary, runtime = balance_service_mod._build_process_scope_snapshot(
                app_id='bootstrap.id',
                app_name='bootstrap',
                process_names=['bootstrap-fork'],
                app_status='a_limited',
                limited_pid_snapshot=[101],
                limited_cgroup_snapshot=['bootstrap-limited.scope'],
            )

        self.assertEqual([row['cgroup'] for row in rows], [
            'bootstrap-limited.scope', 'bootstrap-new.scope',
        ])
        self.assertEqual([row['limit_status'] for row in rows], ['Limited', 'Not Limited'])
        self.assertEqual((summary, runtime), ('Partial Limited', 'Running'))

    def test_replacement_pid_in_limited_cgroup_remains_limited(self):
        """A cgroup limit outlives the PID that was present when it was applied."""
        with mock.patch.object(
                balance_service_mod, 'get_app_processes_for_app', return_value=[102]), \
                mock.patch.object(balance_service_mod, '_runtime_status_for_pid', return_value='Running'), \
                mock.patch.object(balance_service_mod, '_cgroup_for_pid', return_value='benchmark.scope'), \
                mock.patch.object(balance_service_mod, '_exact_cgroup_member_pids', return_value=[102]), \
                mock.patch.object(balance_service_mod, '_cmdline_for_pid', return_value='benchmark_app'):
            rows, summary, runtime = balance_service_mod._build_process_scope_snapshot(
                app_id='benchmark.id',
                app_name='benchmark',
                process_names=['benchmark_app'],
                app_status='a_limited',
                limited_pid_snapshot=[101],
                limited_cgroup_snapshot=['benchmark.scope'],
            )

        self.assertEqual(rows[0]['pid'], 102)
        self.assertEqual(rows[0]['limit_status'], 'Limited')
        self.assertEqual((summary, runtime), ('Limited', 'Running'))

    def test_stopped_configured_processes_remain_visible_for_identity_edits(self):
        with mock.patch.object(balance_service_mod, 'get_app_processes_for_app', return_value=[]):
            rows, summary, runtime = balance_service_mod._build_process_scope_snapshot(
                app_id='bench.scope',
                app_name='bench',
                process_names=['benchmark_app', 'code-server'],
                app_status='normal',
                limited_pid_snapshot=[],
                limited_cgroup_snapshot=[],
                app_cmdline='benchmark_app --model model.xml',
            )

        self.assertEqual([row['process_name'] for row in rows], ['benchmark_app', 'code-server'])
        self.assertTrue(all(row['runtime_status'] == 'Stopped' for row in rows))
        self.assertTrue(all(row['cmdline'] == 'benchmark_app --model model.xml' for row in rows))
        self.assertEqual((summary, runtime), ('No Running Process', 'Stopped'))

    def test_lock_to_manual_marks_the_auto_cgroup_scope_for_the_dashboard(self):
        b = _balancer()
        entry = _limited('bootstrap-one.scope', cpu_mem=True, public_id='bootstrap.id')
        entry.is_controlled = True
        entry.cgroups = ['bootstrap-one.scope', 'bootstrap-two.scope']
        b.all_limits.apps['bootstrap-one.scope'] = entry

        with mock.patch.object(balancer_mod, '_emit_control_events') as emit, \
                mock.patch.object(balancer_mod.app_utils, 'update_app_status'), \
                mock.patch.object(balancer_mod.app_utils, 'callback_manager'):
            ok, message = b.lock_to_manual('bootstrap.id')

        self.assertTrue(ok, message)
        snapshot = b.get_limit_snapshot('bootstrap.id')
        self.assertEqual(snapshot['control_status'], 'MANUAL_LIMITED')
        self.assertTrue(snapshot['adopted_from_auto'])
        self.assertEqual(snapshot['cgroups'], ['bootstrap-one.scope', 'bootstrap-two.scope'])
        self.assertEqual([call.args[0] for call in emit.call_args_list], ['RECOVERED', 'APPLIED'])
        self.assertNotEqual(
            emit.call_args_list[0].kwargs['protection_id'],
            emit.call_args_list[1].kwargs['protection_id'],
        )

    def test_adopt_auto_limit_marks_the_auto_cgroup_scope_for_the_dashboard(self):
        b = _balancer()
        entry = _limited('bootstrap-one.scope', cpu_mem=True)
        entry.cgroups = ['bootstrap-one.scope', 'bootstrap-two.scope']
        b.all_limits.apps['bootstrap-one.scope'] = entry

        with mock.patch.object(balancer_mod, '_emit_control_events') as emit, \
                mock.patch.object(balancer_mod.app_utils, 'update_app_status'), \
                mock.patch.object(balancer_mod.app_utils, 'callback_manager'):
            ok, message = b.adopt_auto_limit(
                'bootstrap-one.scope', 'bootstrap.id', 'bootstrap', 'low')

        self.assertTrue(ok, message)
        snapshot = b.get_limit_snapshot('bootstrap.id')
        self.assertEqual(snapshot['control_status'], 'MANUAL_LIMITED')
        self.assertTrue(snapshot['adopted_from_auto'])
        self.assertEqual(snapshot['cgroups'], ['bootstrap-one.scope', 'bootstrap-two.scope'])
        self.assertEqual([call.args[0] for call in emit.call_args_list], ['RECOVERED', 'APPLIED'])
        self.assertNotEqual(
            emit.call_args_list[0].kwargs['protection_id'],
            emit.call_args_list[1].kwargs['protection_id'],
        )


class SeparatedRestoreChannelTests(unittest.TestCase):
    """Separated policy means separated recovery: one timer, one channel."""

    def test_lifecycle_records_added_and_recovered_channels_symmetrically(self):
        b = _balancer()
        with mock.patch.object(control_events, 'emit_event') as emit, \
                mock.patch.object(control_events, 'read_boot_id', return_value='boot-1'), \
                mock.patch.object(balancer_mod.app_utils, 'update_app_status'), \
                mock.patch.object(balancer_mod.app_utils, 'callback_manager'):
            b._upsert_auto_limited(
                'app.scope', 'app-1', 'demo', {'disk_io_rate': {}},
                resource_limited=False, io_limited=True, priority='low',
                is_controlled=False, limit_reason='disk_pressure', pressure_level='critical',
                cgroups=['app.scope'], resource_parts={},
            )
            b._upsert_auto_limited(
                'app.scope', 'app-1', 'demo', {'cpu_rate': 0.3, 'mem_rate': 0.1},
                resource_limited=True, io_limited=False, priority='low',
                is_controlled=False, limit_reason='system_pressure', pressure_level='critical',
                cgroups=['app.scope'], resource_parts={'cpu': True, 'memory': True},
            )
            b._restore_channel('disk_io', 'full', 'test')
            b._restore_channel('sys', 'full', 'test')

        events = [
            {
                'event_id': str(index),
                'ts_utc': f'2026-01-01T00:00:0{index}+00:00',
                'event_type': call.args[0],
                'protection_id': call.kwargs['protection_id'],
                'resource_type': call.kwargs['resource_type'],
                'app_id': call.kwargs['app_id'],
                'source': call.kwargs['source'],
            }
            for index, call in enumerate(emit.call_args_list, start=1)
        ]

        lifecycle = control_lifecycle.summarize(events)[0]
        self.assertEqual(lifecycle['applied_resources'], ['cpu', 'disk_io', 'memory'])
        self.assertEqual(lifecycle['recovered_resources'], ['cpu', 'disk_io', 'memory'])
        self.assertEqual(lifecycle['active_resources'], [])
        self.assertEqual(lifecycle['status'], 'recovered')

    def test_combined_auto_limit_recovers_cpu_memory_after_sustained_recap(self):
        """Combined policy caps CPU+memory+IO together and records no cpu/memory
        split. A sustained-critical re-cap must not turn that missing split into a
        {cpu:False, memory:False} value that drops CPU/MEMORY from the RECOVERED
        ledger and leaves them stuck 'active' after the user restores the app."""
        b = _balancer()
        with mock.patch.object(control_events, 'emit_event') as emit, \
                mock.patch.object(control_events, 'read_boot_id', return_value='boot-1'):
            for _ in range(2):  # second tick exercises the merge branch
                b._upsert_auto_limited(
                    'app.scope', 'app-1', 'demo',
                    {'cpu_rate': 0.3, 'mem_rate': 0.1, 'disk_io_rate': {}},
                    resource_limited=True, io_limited=True, priority='low',
                    is_controlled=False, limit_reason='system_pressure',
                    pressure_level='critical', cgroups=['app.scope'])
            entry = b.all_limits.apps.pop('app.scope')  # caller pops before restore
            with mock.patch.object(b, '_cgroup_exists', return_value=True):
                self.assertTrue(b._restore_entry(entry, notify=False))

        events = [
            {
                'event_id': str(index),
                'ts_utc': f'2026-01-01T00:00:0{index}+00:00',
                'event_type': call.args[0],
                'protection_id': call.kwargs['protection_id'],
                'resource_type': call.kwargs['resource_type'],
                'app_id': call.kwargs['app_id'],
                'source': call.kwargs['source'],
            }
            for index, call in enumerate(emit.call_args_list, start=1)
        ]

        lifecycle = control_lifecycle.summarize(events)[0]
        self.assertEqual(lifecycle['recovered_resources'], ['cpu', 'disk_io', 'memory'])
        self.assertEqual(lifecycle['active_resources'], [])
        self.assertEqual(lifecycle['status'], 'recovered')

    def test_disk_pressure_apply_records_only_disk_io(self):
        """Disk-pressure limiting caps only IO. The priority limit_rates still carry
        cpu_rate/mem_rate and a caller may hand a resource_parts that reflects the
        config rather than what was applied -- but resource_limited is False, so
        APPLIED must claim DISK_IO only. Otherwise CPU/memory show up applied, never
        recover, and stick as 'active'."""
        b = _balancer()
        with mock.patch.object(control_events, 'emit_event') as emit, \
                mock.patch.object(control_events, 'read_boot_id', return_value='boot-1'):
            b._upsert_auto_limited(
                'app.scope', 'app-1', 'demo',
                {'cpu_rate': 0.3, 'mem_rate': 0.1, 'disk_io_rate': {'read': 20}},
                resource_limited=False, io_limited=True, priority='low',
                is_controlled=False, limit_reason='disk_pressure',
                pressure_level='critical', cgroups=['app.scope'],
                resource_parts={'cpu': True, 'memory': True})
        self.assertEqual(
            [c.args[0] for c in emit.call_args_list],
            ['CONTROL_DISK_IO_LIMIT_APPLIED'])

    def test_sys_restore_leaves_the_disk_cap_alone(self):
        """CPU/memory being calm for 30 minutes says nothing about the disk, which has
        its own (much shorter) stability window."""
        b = _balancer()
        b.all_limits.apps['app.scope'] = _limited('app.scope', cpu_mem=True, io=True)

        b._restore_channel('sys', 'full', 'test')

        entry = b.all_limits.apps['app.scope']
        self.assertFalse(entry.limit_parts['cpu_mem_limited'])
        self.assertTrue(entry.limit_parts['io_limited'], "disk cap must survive a sys restore")
        self.assertEqual(b.io_ctl.restore_calls, [])

    def test_disk_restore_leaves_the_sys_cap_alone(self):
        b = _balancer()
        b.all_limits.apps['app.scope'] = _limited('app.scope', cpu_mem=True, io=True)

        b._restore_channel('disk_io', 'full', 'test')

        entry = b.all_limits.apps['app.scope']
        self.assertTrue(entry.limit_parts['cpu_mem_limited'])
        self.assertFalse(entry.limit_parts['io_limited'])
        self.assertEqual(b.io_ctl.restore_calls, [('app.scope', ['nvme0n1'])])
        self.assertEqual(b.control_manager.adjust_calls, [])

    def test_entry_leaves_the_registry_only_when_both_channels_are_free(self):
        b = _balancer()
        b.all_limits.apps['app.scope'] = _limited(
            'app.scope', cpu_mem=True, io=True, public_id='app-42')

        with mock.patch.object(balancer_mod.app_utils, 'update_app_status') as status, \
                mock.patch.object(balancer_mod.app_utils, 'callback_manager'):
            b._restore_channel('sys', 'full', 'test')
            self.assertIn('app.scope', b.all_limits.apps)
            status.assert_not_called()

            b._restore_channel('disk_io', 'full', 'test')
            self.assertNotIn('app.scope', b.all_limits.apps)
            # The DB row is keyed by the public id, not by the cgroup we addressed.
            status.assert_called_once_with('app-42', 'running')

    def test_restore_picks_an_app_that_carries_the_channel(self):
        """first_auto() would return the oldest entry regardless of channel and relax a
        cap whose own stability window has not elapsed."""
        b = _balancer()
        b.all_limits.apps['io-only.scope'] = _limited('io-only.scope', io=True)
        b.all_limits.apps['sys-only.scope'] = _limited('sys-only.scope', cpu_mem=True)

        with mock.patch.object(balancer_mod.app_utils, 'update_app_status'), \
                mock.patch.object(balancer_mod.app_utils, 'callback_manager'):
            b._restore_channel('sys', 'full', 'test')

        self.assertIn('io-only.scope', b.all_limits.apps)
        self.assertNotIn('sys-only.scope', b.all_limits.apps)
        self.assertEqual(b.io_ctl.restore_calls, [])

    def test_partial_restore_is_tracked_per_channel(self):
        """A single "partially_restored" flag let whichever channel relaxed first block
        the other one for good."""
        b = _balancer()
        b.all_limits.apps['app.scope'] = _limited('app.scope', cpu_mem=True, io=True)

        b._restore_channel('sys', 'partial', 'test')
        entry = b.all_limits.apps['app.scope']
        self.assertEqual(entry.partial_parts, {'sys': True, 'disk_io': False})
        self.assertEqual(entry.state, 'partially_restored')

        b._restore_channel('disk_io', 'partial', 'test')
        self.assertEqual(entry.partial_parts, {'sys': True, 'disk_io': True})
        # Both channels relaxed, each exactly once.
        self.assertEqual(len(b.io_ctl.set_calls), 1)
        self.assertEqual([c[1] for c in b.control_manager.adjust_calls], ['medium'])

    def test_partial_restore_advances_to_the_next_app(self):
        b = _balancer()
        first = _limited('first.scope', cpu_mem=True)
        first.partial_parts['sys'] = True
        b.all_limits.apps['first.scope'] = first
        b.all_limits.apps['second.scope'] = _limited('second.scope', cpu_mem=True)

        b._restore_channel('sys', 'partial', 'test')

        self.assertTrue(b.all_limits.apps['second.scope'].partial_parts['sys'])

    def test_nothing_to_restore_is_not_an_error(self):
        b = _balancer()
        b.all_limits.apps['io-only.scope'] = _limited('io-only.scope', io=True)
        self.assertFalse(b._restore_channel('sys', 'full', 'test'))
        self.assertIn('io-only.scope', b.all_limits.apps)


class CombinedPolicyTests(unittest.TestCase):
    """Combined policy has a single channel and restores both caps together."""

    def test_failed_full_restore_emits_terminal_failure_events(self):
        b = _balancer()
        entry = _limited('app.scope', cpu_mem=True, io=True)
        entry.protection_id = 'protect-1'
        b.all_limits.apps['app.scope'] = entry
        b.control_manager.adjust_resources = mock.Mock(return_value=False)
        state = _state(
            pressure_start_time=NOW - _MonitorLoopState.STABLE_PERIOD,
            current_pressure='low',
        )

        with mock.patch.object(balancer_mod, '_emit_control_events') as emit, \
                mock.patch.object(b, '_notify_auto_limit_changed'):
            b._tick_combined_restore(state, 'low')

        emit.assert_called_once()
        action, parts = emit.call_args.args
        assert action == 'FAILED'
        assert parts == {'cpu_mem_limited': True, 'io_limited': True}
        assert emit.call_args.kwargs['protection_id'] == 'protect-1'
        assert emit.call_args.kwargs['attributes']['restore_type'] == 'full'
        assert emit.call_args.kwargs['attributes']['cgroups'] == ['app.scope']

    def test_stale_batch_is_resampled(self):
        b = _balancer()
        state = _state(top_consume_apps=[_candidate('old.scope')],
                       top_source='sys', reach_threshold=True,
                       top_fetched_at=NOW - _MonitorLoopState.TOP_BATCH_TTL - 1)

        with mock.patch.object(b, '_update_dominant_flag_from_top'), \
                mock.patch.object(b, '_apply_combined_critical_limits') as apply_mock, \
                mock.patch.object(b, '_target_still_present', return_value=True), \
                mock.patch.object(b, '_handle_critical_pressure',
                                  side_effect=lambda apps, reached: (
                                      True, False, apps[0]['app']['id'], {'cpu_rate': 0.3})):
            b._tick_combined_policy(state, "critical", True)

        self.assertEqual(b.top_prefetcher.resolved, 1)
        self.assertEqual(apply_mock.call_args.args[1], 'sys-app.scope')

    def test_dead_target_is_not_limited(self):
        b = _balancer()
        state = _state()
        with mock.patch.object(b, '_update_dominant_flag_from_top'), \
                mock.patch.object(b, '_apply_combined_critical_limits') as apply_mock, \
                mock.patch.object(b, '_pid_gone_or_dying', return_value=True), \
                mock.patch.object(b, '_cgroup_exists', return_value=False), \
                mock.patch.object(b, '_handle_critical_pressure',
                                  return_value=(True, False, 'sys-app.scope', {'cpu_rate': 0.3})):
            b._tick_combined_policy(state, "critical", True)
        apply_mock.assert_not_called()


class EffectiveLimitDetailsTests(unittest.TestCase):
    def test_snapshot_exposes_all_active_io_limit_values(self):
        b = _balancer()
        b.all_limits.apps['io.scope'] = _limited('io.scope', cpu_mem=True, io=True)

        effective = b.get_limit_snapshot('io.scope')['effective']

        self.assertEqual(effective['cpu_mem'], {
            'limited': True, 'cpu_rate': 0.3, 'mem_rate': 0.1,
        })
        self.assertEqual(effective['disk_io'], {
            'limited': True,
            'disks': ['nvme0n1'],
            'read_mb_s': 20,
            'write_mb_s': 10,
            'read_iops': 8000,
            'write_iops': 1000,
        })


class MergeControlledAppProcessesTests(unittest.TestCase):
    def test_merge_adds_only_new_process_identities(self):
        cfg = _MergeCfg()
        service = mock.Mock()
        response_payload = {
            'id': 'existing.scope',
            'process_names': ['shared-worker', 'new-worker'],
            'bpf_name': ['existing-app', 'new-worker'],
        }

        with (
            mock.patch('config.config.b_config', cfg),
            mock.patch('smartune_api._token_is_valid', return_value=True),
            mock.patch.object(balance_service_mod, 'AIAppPriority') as db_model,
            mock.patch.object(balance_service_mod, '_service', service),
        ):
            response = balance_service_mod.app.test_client().post(
                '/app/merge_controlled_app_processes', json=response_payload,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['retcode'], 0)
        self.assertEqual(len(cfg.controlled_apps), 1)
        merged = cfg.controlled_apps[0]
        self.assertEqual(merged['commandline'], '/usr/bin/existing-app')
        self.assertEqual(merged['process_names'], ['existing-app', 'shared-worker', 'new-worker'])
        self.assertEqual(merged['bpf_name'], ['existing-app', 'new-worker'])
        db_model.update_record.assert_called_once()
        service.rebuild_controlled_map.assert_called_once()


class PurgeControlledAppTests(unittest.TestCase):
    def test_purge_keeps_config_when_active_manual_limit_cannot_restore(self):
        cfg = _MergeCfg()
        service = mock.Mock()
        service.get_limit_snapshot.return_value = {'limited': True, 'source': 'manual'}
        service.restore_resource.return_value = False

        with (
            mock.patch('config.config.b_config', cfg),
            mock.patch('smartune_api._token_is_valid', return_value=True),
            mock.patch.object(balance_service_mod, '_service', service),
        ):
            response = balance_service_mod.app.test_client().post(
                '/app/purge_controlled_app', json={'id': 'existing.scope'},
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.get_json()['retcode'], 0)
        self.assertEqual(len(cfg.controlled_apps), 1)
        self.assertEqual(cfg.writes, [])
        service.restore_resource.assert_called_once_with('existing.scope')


class SwapHandbackTests(unittest.TestCase):
    """Stopping smartune undoes what smartune did -- swap included.

    Swap is the one resource where "undo it" can cost more than leaving it:
    swapoff faults every page of the file back into RAM, so only the files
    holding nothing are taken on the way out.  Hence only_unused on both the
    shutdown and the start-up call.
    """

    def _balancer(self):
        b = _balancer()
        b.control_manager.release_swap = mock.Mock(return_value=[])
        b.restore_all_limited_apps_resources = mock.Mock()
        b.network_controller = mock.Mock()
        return b

    def test_shutdown_hands_back_only_the_files_holding_nothing(self):
        b = self._balancer()
        b.shutdown()
        b.control_manager.release_swap.assert_called_once_with(
            dry_run=False, only_unused=True)

    def test_shutdown_releases_swap_after_restoring_limits(self):
        """Order matters: a restored app may fault its pages straight back in,
        so the limits come off first and swap goes last."""
        b = self._balancer()
        order = []
        b.restore_all_limited_apps_resources.side_effect = \
            lambda *a, **k: order.append("limits")
        b.control_manager.release_swap.side_effect = \
            lambda *a, **k: order.append("swap") or []
        b.shutdown()
        self.assertEqual(order, ["limits", "swap"])

    def test_a_failed_release_does_not_stop_the_service_exiting(self):
        b = self._balancer()
        b.control_manager.release_swap.side_effect = OSError("swapoff: busy")
        b.shutdown()   # must not raise
        self.assertFalse(b.is_running)

    def test_an_already_stopped_service_releases_nothing(self):
        b = self._balancer()
        b.is_running = False
        b.shutdown()
        b.control_manager.release_swap.assert_not_called()

    def test_startup_reclaims_what_a_reboot_left_behind(self):
        """We never write /etc/fstab, so after a reboot the files are still on
        disk but nothing swapped them on -- gigabytes nobody owns."""
        b = self._balancer()
        b._reclaim_orphaned_swap()
        b.control_manager.release_swap.assert_called_once_with(
            dry_run=False, only_unused=True)

    def test_a_failed_startup_reclaim_does_not_stop_the_service_starting(self):
        b = self._balancer()
        b.control_manager.release_swap.side_effect = OSError("no such dir")
        b._reclaim_orphaned_swap()   # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
