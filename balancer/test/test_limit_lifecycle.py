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
from monitor.res_monitor import ResourceMonitor  # noqa: E402
from balancer.balancer import (  # noqa: E402
    DynamicBalancer,
    LimitedApp,
    LimitRegistry,
    _MonitorLoopState,
)

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

    def adjust_resources(self, app_id, policy, **kwargs):
        self.adjust_calls.append((app_id, policy, kwargs))
        return True

    def set_limited_app_dominant(self, dominant, cgroups=None):
        pass


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


class AutoToManualScopeTests(unittest.TestCase):
    """Taking control must retain exactly the cgroups Auto Control limited."""

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

        with mock.patch.object(balancer_mod.app_utils, 'update_app_status'), \
                mock.patch.object(balancer_mod.app_utils, 'callback_manager'):
            ok, message = b.lock_to_manual('bootstrap.id')

        self.assertTrue(ok, message)
        snapshot = b.get_limit_snapshot('bootstrap.id')
        self.assertEqual(snapshot['control_status'], 'MANUAL_LIMITED')
        self.assertTrue(snapshot['adopted_from_auto'])
        self.assertEqual(snapshot['cgroups'], ['bootstrap-one.scope', 'bootstrap-two.scope'])

    def test_adopt_auto_limit_marks_the_auto_cgroup_scope_for_the_dashboard(self):
        b = _balancer()
        entry = _limited('bootstrap-one.scope', cpu_mem=True)
        entry.cgroups = ['bootstrap-one.scope', 'bootstrap-two.scope']
        b.all_limits.apps['bootstrap-one.scope'] = entry

        with mock.patch.object(balancer_mod.app_utils, 'update_app_status'), \
                mock.patch.object(balancer_mod.app_utils, 'callback_manager'):
            ok, message = b.adopt_auto_limit(
                'bootstrap-one.scope', 'bootstrap.id', 'bootstrap', 'low')

        self.assertTrue(ok, message)
        snapshot = b.get_limit_snapshot('bootstrap.id')
        self.assertEqual(snapshot['control_status'], 'MANUAL_LIMITED')
        self.assertTrue(snapshot['adopted_from_auto'])
        self.assertEqual(snapshot['cgroups'], ['bootstrap-one.scope', 'bootstrap-two.scope'])


class SeparatedRestoreChannelTests(unittest.TestCase):
    """Separated policy means separated recovery: one timer, one channel."""

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
