# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Tests for benchmark quiet mode -- utils/quiet_mode.py and every gate on it.

Quiet mode exists so that the background load a benchmark run sees is a
*constant*, independent of which dashboard page happens to be open, of how
``monitored_sections`` is configured, and of whether the balancer decides to cap
an app halfway through a case.  Two properties have to hold for that to be true,
and both are easy to break by accident later:

* **Nothing periodic survives the gate.**  Not just the collector loop -- also
  the on-demand paths behind it.  ``/dynamic_info`` and the per-app stats
  endpoints all fall through to a synchronous collection on a cold cache, so a
  second browser tab parked on System Overview would go on forking
  xpu-smi/npu-smi at its own poll rate with the loop dutifully idle.  Those
  bypasses are tested here by name.

* **The gate always comes back down.**  A SmarTune wedged in quiet mode has
  stopped being a monitor.  Three independent mechanisms undo it -- the job
  listener on every terminal status, the TTL lease renewed by the sampler
  thread, and the flag being memory-only so a restart cannot inherit it -- and
  each is tested separately, because the whole point is that no single one of
  them is load-bearing.

Not covered automatically:

* The retention sweep's gate (``cleanup_loop`` in monitor_api) and the app-stats
  refresher's *park* branch both live in closures whose loops begin with a
  30 s / unbounded wait, and neither can be driven without patching
  ``time.sleep`` process-wide -- which would perturb every other thread in the
  test.  The app-stats bypass that a user can actually reach (the endpoint) is
  tested; the refresher park is a one-line reuse of the existing idle-park path.
* The end-to-end question quiet mode was built to answer -- does run-to-run
  dispersion actually narrow -- is a measurement, not an assertion.  The
  protocol, and the decision rule for a null result, are in
  docs/quiet_mode_verification.md; it has not been run.

Run:  /usr/bin/python3 balancer/test/test_quiet_mode.py
      (needs psutil + flask; the repo's .venv-build does not carry them)
"""

import os
import re
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "balancer")):
    if path not in sys.path:
        sys.path.insert(0, path)

import flask  # noqa: E402

from utils import quiet_mode  # noqa: E402
from utils.quiet_mode import QuietModeManager  # noqa: E402

import monitor.monitor_api as monitor_api  # noqa: E402
from monitor import network_pressure as netpress  # noqa: E402
from monitor.metrics import history  # noqa: E402

import balancer.balancer as balancer_mod  # noqa: E402
import balance_service as balance_service_mod  # noqa: E402
from balancer.balancer import DynamicBalancer  # noqa: E402

from benchmark.service import jobs, runner, sampler  # noqa: E402

# Long enough that no test trips it by accident, short enough that the expiry
# tests do not pad the suite.
_TEST_TTL_S = 0.4
_TEST_POLL_S = 0.05

OWNER = "run-abc123"


class _GateCase(unittest.TestCase):
    """Base case: every test gets its own manager.

    The gate is a process-wide singleton by design (one machine, one quiet
    mode), so tests have to replace it rather than reset it -- a leaked hold
    would otherwise silently gate the *next* test's collectors and turn a
    failure into a pass.
    """

    def setUp(self):
        manager = QuietModeManager()
        manager.ttl_s = _TEST_TTL_S
        manager.poll_interval = _TEST_POLL_S
        patcher = mock.patch.object(quiet_mode, "_manager", manager)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Stop the watchdog thread with the test, so a suite-worth of managers
        # does not leave a suite-worth of pollers behind.
        self.addCleanup(manager._stop.set)
        self.addCleanup(manager.exit)
        self.manager = manager


class GateSemanticsTests(_GateCase):
    """The two-flag model: who holds quiet mode, and is it up right now."""

    def test_enter_makes_the_gate_active_and_is_idempotent(self):
        quiet_mode.enter(OWNER)
        self.assertTrue(quiet_mode.is_active())
        quiet_mode.enter(OWNER)  # a second start event, or a re-entry
        self.assertTrue(quiet_mode.is_active())
        self.assertEqual(quiet_mode.state()["owner"], OWNER)

    def test_exit_ignores_a_non_matching_owner(self):
        # A late callback from a finished run must not cancel the hold of the
        # run that started after it.
        quiet_mode.enter(OWNER)
        self.assertFalse(quiet_mode.exit("run-older"))
        self.assertTrue(quiet_mode.is_active())
        self.assertTrue(quiet_mode.exit(OWNER))
        self.assertFalse(quiet_mode.is_active())

    def test_gate_can_be_dropped_and_raised_without_ending_the_hold(self):
        quiet_mode.enter(OWNER)

        self.assertFalse(quiet_mode.set_gate(False))
        self.assertFalse(quiet_mode.is_active())          # collectors come back
        self.assertTrue(quiet_mode.state()["held"])       # but the run still owns it
        self.assertFalse(quiet_mode.held_clean())         # and its data is dirtied

        self.assertTrue(quiet_mode.set_gate(True))
        self.assertTrue(quiet_mode.is_active())
        # Latched: raising the gate again does not un-dirty the run.
        self.assertFalse(quiet_mode.held_clean())

    def test_set_gate_outside_a_hold_does_nothing(self):
        # There is nothing to toggle outside a run, and pretending otherwise
        # would let the UI believe it had changed something.
        self.assertFalse(quiet_mode.set_gate(True))
        self.assertFalse(quiet_mode.is_active())
        self.assertFalse(quiet_mode.state()["held"])

    def test_an_expired_lease_reads_inactive_before_the_watchdog_runs(self):
        quiet_mode.enter(OWNER, ttl=0.05)
        self.manager._stop.set()          # no watchdog: is_active must decide alone
        time.sleep(0.12)
        self.assertFalse(quiet_mode.is_active())
        self.assertFalse(quiet_mode.state()["held"])

    def test_the_watchdog_reclaims_an_expired_lease(self):
        quiet_mode.enter(OWNER, ttl=0.05)
        deadline = time.monotonic() + 2.0
        while quiet_mode.state()["owner"] is not None and time.monotonic() < deadline:
            time.sleep(_TEST_POLL_S)
        self.assertIsNone(quiet_mode.state()["owner"])

    def test_heartbeat_renews_a_live_lease(self):
        quiet_mode.enter(OWNER)
        for _ in range(6):
            time.sleep(_TEST_TTL_S / 3)
            quiet_mode.heartbeat(OWNER)
        self.assertTrue(quiet_mode.is_active())

    def test_heartbeat_does_not_revive_an_expired_lease(self):
        # Monitoring has already been restored underneath the sampler; raising
        # the gate again on its next tick would make the background load flap,
        # which is the one thing quiet mode exists to prevent.
        quiet_mode.enter(OWNER, ttl=0.05)
        self.manager._stop.set()
        time.sleep(0.12)
        quiet_mode.heartbeat(OWNER)
        self.assertFalse(quiet_mode.is_active())

    def test_the_hold_is_never_written_anywhere(self):
        # The "a restart necessarily clears it" guarantee is exactly the absence
        # of persistence, so it is asserted as such: a future edit that reaches
        # for the config module to remember the gate fails here.
        source = Path(_REPO_ROOT, "utils", "quiet_mode.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^\s*(import|from)\s+config", source, re.M))
        # And a fresh manager -- what a restarted process gets -- holds nothing.
        self.assertFalse(QuietModeManager().is_active())


class BlockerRegistryTests(_GateCase):
    """Preflight checks: what refuses a run, and what must never refuse one."""

    def test_no_registrations_means_nothing_blocks(self):
        # The monitor-only deployment: no balancer, so no blocker, so no
        # special-casing anywhere else.
        self.assertEqual(quiet_mode.check_blockers(), [])

    def test_a_blocker_reports_its_reason_and_apps(self):
        quiet_mode.register_blocker("auto_limited_apps", lambda: {
            "reason": "The balancer is currently auto-limiting one or more apps.",
            "apps": [{"app_id": "hog.scope", "app_name": "hog"}],
        })
        hits = quiet_mode.check_blockers()
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["name"], "auto_limited_apps")
        self.assertEqual(hits[0]["apps"][0]["app_id"], "hog.scope")

    def test_a_string_reason_is_accepted(self):
        quiet_mode.register_blocker("something", lambda: "not right now")
        self.assertEqual(quiet_mode.check_blockers()[0]["reason"], "not right now")

    def test_registering_the_same_name_replaces_rather_than_accumulates(self):
        quiet_mode.register_blocker("dup", lambda: "first")
        quiet_mode.register_blocker("dup", lambda: "second")
        hits = quiet_mode.check_blockers()
        self.assertEqual([h["reason"] for h in hits], ["second"])

    def test_a_raising_blocker_is_treated_as_clear(self):
        # This gate guards data quality, not safety. A broken check must not
        # ground every benchmark run on the machine.
        def boom():
            raise RuntimeError("balancer state unreadable")

        quiet_mode.register_blocker("broken", boom)
        self.assertEqual(quiet_mode.check_blockers(), [])


class AutoLimitBlockerTests(unittest.TestCase):
    """The balancer's own contribution to preflight."""

    def _service(self, apps):
        service = balance_service_mod.DynamicService.__new__(
            balance_service_mod.DynamicService)
        service.balancer = mock.Mock()
        service.balancer.get_auto_limited_apps.return_value = {"apps": apps}
        return service

    def test_clear_when_nothing_is_auto_limited(self):
        self.assertFalse(self._service([])._auto_limit_blocker())

    def test_blocks_and_names_the_apps(self):
        hit = self._service([
            {"app_id": "hog.scope", "app_name": "hog"},
            {"app_id": "chat.scope", "app_name": "chat"},
        ])._auto_limit_blocker()
        self.assertEqual([a["app_name"] for a in hit["apps"]], ["hog", "chat"])
        # The user has to be told where to go: neither automatic escape is
        # acceptable (releasing stampedes the load back, converting to manual
        # leaves limits that never restore), so the message must point at the
        # page where a human decides.
        self.assertIn("Balance page", hit["action"])

    def test_a_manual_limit_does_not_block(self):
        # A manual limit is the user's own decision, not balancer interference,
        # and get_auto_limited_apps is what draws that line.
        service = self._service([])
        self.assertFalse(service._auto_limit_blocker())
        service.balancer.get_auto_limited_apps.assert_called_once_with()


class MonitorCollectorGateTests(_GateCase):
    """The periodic collectors: silent while the gate is up, back afterwards."""

    def _run_collector(self, seconds=0.35):
        """Run the dynamic-info collector loop for a moment, then stop it."""
        monitor_api._dynamic_info_stop_event.clear()
        thread = threading.Thread(target=monitor_api._dynamic_info_collector_loop,
                                  daemon=True)
        thread.start()
        try:
            time.sleep(seconds)
        finally:
            monitor_api._dynamic_info_stop_event.set()
            thread.join(timeout=2)
            monitor_api._dynamic_info_stop_event.clear()

    def test_dynamic_info_collector_queries_no_hardware_under_quiet_mode(self):
        collect = mock.Mock(return_value={"cpu": {}})
        with (
            mock.patch.object(monitor_api, "collect_dynamic_info", collect),
            mock.patch.object(monitor_api, "_get_monitored_sections", return_value=["cpu"]),
            mock.patch.object(monitor_api, "_get_resource_monitor", mock.Mock()),
            mock.patch.object(monitor_api, "_get_system_pressure_monitor", mock.Mock()),
            mock.patch.object(monitor_api, "_DYNAMIC_INFO_REFRESH_INTERVAL_SEC", 0.05),
        ):
            quiet_mode.enter(OWNER)
            self._run_collector()
            collect.assert_not_called()

            # ... and the same loop, with the gate down, is the normal collector.
            quiet_mode.set_gate(False)
            self._run_collector()
            self.assertGreater(collect.call_count, 0)

    def test_network_pressure_collector_publishes_nothing_under_quiet_mode(self):
        publish = mock.Mock()
        monitors = {"eth0": mock.Mock()}
        monitors["eth0"].get_current_pressure.return_value = {}

        netpress._network_pressure_stop_event.clear()
        with (
            mock.patch.object(netpress, "publish_network_pressure_snapshot", publish),
            mock.patch.object(netpress, "_build_network_monitors", return_value=monitors),
            mock.patch.object(netpress, "_NETWORK_PRESSURE_REFRESH_INTERVAL_SEC", 0.05),
            mock.patch.object(netpress.os.path, "exists", return_value=True),
        ):
            quiet_mode.enter(OWNER)
            thread = threading.Thread(target=netpress._network_pressure_collector_loop,
                                      daemon=True)
            thread.start()
            try:
                time.sleep(0.3)
                publish.assert_not_called()
                monitors["eth0"].sample_network_pressure.assert_not_called()

                quiet_mode.set_gate(False)
                time.sleep(0.3)
                self.assertGreater(publish.call_count, 0)
            finally:
                netpress._network_pressure_stop_event.set()
                thread.join(timeout=2)
                netpress._network_pressure_stop_event.clear()

    def test_history_writes_stop_outright(self):
        # Gated inside persist_dynamic_snapshot_if_due, not only in the
        # collector, because collect_dynamic_info(persist=True) is reachable
        # from any on-demand path.
        with (
            mock.patch.object(history, "persist_monitor_snapshot") as persist,
            mock.patch.object(history, "_build_dynamic_history_payload", return_value={}),
        ):
            quiet_mode.enter(OWNER)
            history.persist_dynamic_snapshot_if_due({"cpu": {}})
            persist.assert_not_called()

            quiet_mode.set_gate(False)
            history._DYNAMIC_SNAPSHOT_STATE["last_persist_ts"] = 0.0
            history.persist_dynamic_snapshot_if_due({"cpu": {}})
            persist.assert_called_once()

    def test_system_pressure_monitor_is_stopped_not_slowed(self):
        # It reads PSI plus a full ResourceMonitor pass, and the only consumer
        # that acts on the result -- the balancer's loop -- is gated too.
        spm = monitor_api.SystemPressureMonitor.__new__(monitor_api.SystemPressureMonitor)
        spm._next_interval = 0.05
        with mock.patch.object(spm, "_safe_update") as update:
            quiet_mode.enter(OWNER)
            spm._start_auto_refresh()
            try:
                time.sleep(0.3)
                update.assert_not_called()

                quiet_mode.set_gate(False)
                time.sleep(0.3)
                self.assertGreater(update.call_count, 0)
            finally:
                # Park the refresh thread for the rest of the process: it is a
                # daemon with no stop flag, and 0.05 s of spinning per tick is
                # not something the remaining tests should have to carry.
                spm._next_interval = 3600
                time.sleep(0.1)


class MonitorEndpointBypassTests(_GateCase):
    """The on-demand paths -- the ones a second browser tab can reach.

    Gating the collector loop alone would leave every one of these free to fork
    xpu-smi/npu-smi at the dashboard's poll rate, which is precisely the
    UI-dependent background load quiet mode is supposed to remove.
    """

    def setUp(self):
        super().setUp()
        app = flask.Flask(__name__)
        app.register_blueprint(monitor_api.monitor_bp)
        self.client = app.test_client()
        # A cold cache is what makes each of these collect synchronously, so
        # every test below starts from one.
        with monitor_api._DYNAMIC_INFO_CACHE_LOCK:
            monitor_api._DYNAMIC_INFO_CACHE["data"] = None
            monitor_api._DYNAMIC_INFO_CACHE["ts"] = 0.0
        with monitor_api._DYNAMIC_SECTION_CACHE_LOCK:
            monitor_api._DYNAMIC_SECTION_CACHE.clear()
        with monitor_api._APP_STATS_CACHE_LOCK:
            monitor_api._APP_STATS_CACHE["resource"] = None
            monitor_api._APP_STATS_CACHE["disk_io"] = None

    def test_full_snapshot_request_collects_nothing(self):
        collect = mock.Mock(return_value={"cpu": {}})
        with (
            mock.patch.object(monitor_api, "collect_dynamic_info", collect),
            mock.patch.object(monitor_api, "_start_dynamic_info_auto_refresh"),
            mock.patch.object(monitor_api, "_get_monitored_sections",
                              return_value=list(monitor_api.DYNAMIC_INFO_SECTIONS)),
            # The cold-cache branch builds both monitors before collecting, and
            # a real SystemPressureMonitor cannot read PSI in a container -- so
            # the ungated half of this test would fail for reasons unrelated to
            # the gate.
            mock.patch.object(monitor_api, "_get_resource_monitor", mock.Mock()),
            mock.patch.object(monitor_api, "_get_system_pressure_monitor", mock.Mock()),
        ):
            quiet_mode.enter(OWNER)
            body = self.client.get("/monitor/dynamic_info").get_json()
            self.assertEqual(body["retcode"], 0)
            self.assertTrue(body["data"]["quiet_mode"]["active"])
            collect.assert_not_called()

            quiet_mode.set_gate(False)
            body = self.client.get("/monitor/dynamic_info").get_json()
            self.assertNotIn("quiet_mode", body["data"])
            collect.assert_called_once()

    def test_section_request_does_not_reach_the_on_demand_collector(self):
        # The System Overview tab's request, with the section missing from the
        # cache -- the exact shape that used to bypass the gate.
        collect = mock.Mock(return_value={"gpu": {}})
        with (
            mock.patch.object(monitor_api, "collect_dynamic_info", collect),
            mock.patch.object(monitor_api, "_get_monitored_sections", return_value=[]),
            mock.patch.object(monitor_api, "_get_resource_monitor", mock.Mock()),
            mock.patch.object(monitor_api, "_get_system_pressure_monitor", mock.Mock()),
        ):
            quiet_mode.enter(OWNER)
            body = self.client.get("/monitor/dynamic_info?sections=gpu").get_json()
            self.assertTrue(body["data"]["quiet_mode"]["active"])
            collect.assert_not_called()

            # The single-section sub-resource is the same code path; assert it
            # rather than assume it.
            body = self.client.get("/monitor/dynamic_info/gpu").get_json()
            self.assertTrue(body["data"]["quiet_mode"]["active"])
            collect.assert_not_called()

            quiet_mode.set_gate(False)
            self.client.get("/monitor/dynamic_info?sections=gpu")
            self.assertGreater(collect.call_count, 0)

    def test_cached_sections_are_still_served_while_the_gate_is_up(self):
        # Stale, and labelled stale. The tiles keep their last values instead of
        # going blank, and the dashboard can say why they stopped moving.
        with monitor_api._DYNAMIC_INFO_CACHE_LOCK:
            monitor_api._DYNAMIC_INFO_CACHE["data"] = {"cpu": {"utilization": 12.5}}
            monitor_api._DYNAMIC_INFO_CACHE["ts"] = 1_700_000_000.0
        with mock.patch.object(monitor_api, "collect_dynamic_info") as collect:
            quiet_mode.enter(OWNER)
            body = self.client.get("/monitor/dynamic_info?sections=cpu").get_json()
            self.assertEqual(body["data"]["cpu"]["utilization"], 12.5)
            self.assertEqual(body["data"]["quiet_mode"]["cached_at"], 1_700_000_000.0)
            collect.assert_not_called()

    def test_per_app_stats_endpoints_collect_nothing(self):
        monitor = mock.Mock()
        with (
            mock.patch.object(monitor_api, "_get_resource_monitor", return_value=monitor),
            mock.patch.object(monitor_api, "_start_app_stats_auto_refresh"),
        ):
            quiet_mode.enter(OWNER)
            for path in ("/monitor/app_resource_stats", "/monitor/app_disk_io_stats"):
                body = self.client.get(f"{path}?n=5").get_json()
                self.assertEqual(body["data"]["apps"], [], path)
                self.assertTrue(body["data"]["quiet_mode"]["active"], path)
            monitor.get_app_resource_stats.assert_not_called()
            monitor.get_app_disk_io_stats.assert_not_called()

            quiet_mode.set_gate(False)
            monitor.get_app_resource_stats.return_value = [{"app_name": "x"}]
            self.client.get("/monitor/app_resource_stats?n=5")
            monitor.get_app_resource_stats.assert_called_once()


class BalancerTickTests(_GateCase):
    """The pressure loop: decides nothing under quiet mode, still releases.

    A cgroup cap dropped on the run mid-measurement would invalidate it, but a
    balancer that stopped releasing would leave a user's app suspended for the
    length of a run -- so the two halves are asserted separately.

    The block is skipped rather than neutered because SystemPressureMonitor is
    stopped too: every level it could read is frozen at the last pre-run tick.
    """

    def _balancer(self):
        b = DynamicBalancer.__new__(DynamicBalancer)  # no __init__: no BPF, no cgroups
        b.config = mock.Mock()
        b.config.limit_policy = {"policy": "separated", "disk_io": {}}
        b.config.passive_resource_control = {"enabled": True}
        b.config.monitor_idle_check_interval = 10
        b.config.regular_update_sys_pressure_time = 5
        b.config.limit_reap_interval = 2
        b.is_running = True

        b.control_manager = mock.Mock()
        b.control_manager.current_level = "high"
        b.control_manager.consume_peak_pressure_level.return_value = ("high", 0.8, "low")
        b.all_limits = mock.Mock()
        b.all_limits.is_limited_app_dominant = False
        b.app_priority_queue = mock.Mock()
        b.app_priority_queue.empty.return_value = True

        for name in ("_maybe_trigger_prefetch", "_tick_separated_policy",
                     "_tick_combined_policy", "_run_network_tick",
                     "_reap_closed_apps", "lock_all_auto_to_manual",
                     "_drain_pending_app_queue"):
            setattr(b, name, mock.Mock())
        # The loop's last step before its 1 s sleep, so this is where one
        # iteration ends. Driving the real loop (rather than the tick methods
        # directly) is deliberate: the gate lives in the loop, and a test that
        # called the ticks itself would pass even if the gate were deleted.
        b._reap_closed_apps.side_effect = lambda *a, **k: setattr(b, "is_running", False)
        return b

    def test_quiet_mode_decides_nothing_but_still_reaps(self):
        b = self._balancer()
        quiet_mode.enter(OWNER)
        b._run_monitor_resource_loop()

        # Nothing in the decision block runs at all -- not with passive control
        # forced off, not with a stale pressure level, not even the read.
        b._maybe_trigger_prefetch.assert_not_called()
        b._tick_separated_policy.assert_not_called()
        b._tick_combined_policy.assert_not_called()
        # Network sampling reads every interface; the handling side can install
        # tc classes on the run's own traffic. Both have to go.
        b._run_network_tick.assert_not_called()
        # A closed app's stale limit is still lifted.
        b._reap_closed_apps.assert_called_once()

    def test_quiet_mode_does_not_read_the_pressure_latch(self):
        # The observable half of the above: consuming the peak each tick logs a
        # pressure level nobody sampled, at the cadence of idle_check_interval.
        # That log line is how the leak was reported in the first place.
        b = self._balancer()
        quiet_mode.enter(OWNER)
        b._run_monitor_resource_loop()
        b.control_manager.consume_peak_pressure_level.assert_not_called()

    def test_quiet_mode_still_drains_pending_launches(self):
        # Pure release: SIGCONT plus bookkeeping. Skipping it would leave an app
        # the user launched suspended for the length of the benchmark.
        b = self._balancer()
        b.app_priority_queue.empty.return_value = False
        quiet_mode.enter(OWNER)
        b._run_monitor_resource_loop()
        b._drain_pending_app_queue.assert_called_once_with(mock.ANY)

    def test_quiet_mode_drains_nothing_when_no_launch_is_pending(self):
        b = self._balancer()
        quiet_mode.enter(OWNER)
        b._run_monitor_resource_loop()
        b._drain_pending_app_queue.assert_not_called()

    def test_quiet_mode_never_converts_auto_limits_to_manual(self):
        # The reason quiet mode does not reuse passive_resource_control: that
        # switch's falling edge hands every auto limit to the operator as a
        # manual one that never restores itself.
        b = self._balancer()
        quiet_mode.enter(OWNER)
        b._run_monitor_resource_loop()
        b.lock_all_auto_to_manual.assert_not_called()

    def test_a_normal_tick_is_unchanged(self):
        # The positive control for every assert_not_called above: without it they
        # would all still pass if the loop simply stopped doing anything.
        b = self._balancer()
        b._run_monitor_resource_loop()
        self.assertIs(b._maybe_trigger_prefetch.call_args.args[3], True)
        self.assertIs(b._tick_separated_policy.call_args.args[3], True)
        b.control_manager.consume_peak_pressure_level.assert_called()
        b._run_network_tick.assert_called_once()
        b.lock_all_auto_to_manual.assert_not_called()

    def test_dropping_the_gate_restores_automatic_control(self):
        b = self._balancer()
        quiet_mode.enter(OWNER)
        quiet_mode.set_gate(False)
        b._run_monitor_resource_loop()
        self.assertIs(b._tick_separated_policy.call_args.args[3], True)
        b._run_network_tick.assert_called_once()


class RunLifecycleTests(_GateCase):
    """Entering on start, and the release that must never be missed."""

    def _job(self, status, meta=None, kind="run"):
        job = jobs.Job.__new__(jobs.Job)
        job.kind = kind
        job.status = status
        job.meta = meta if meta is not None else {"quiet_owner": OWNER}
        return job

    def test_a_terminal_status_releases_the_gate(self):
        for status in (jobs.STATUS_DONE, jobs.STATUS_FAILED, jobs.STATUS_CANCELLED):
            with self.subTest(status=status):
                quiet_mode.enter(OWNER)
                job = self._job(status)
                runner._release_quiet_mode(job)
                self.assertFalse(quiet_mode.is_active())
                self.assertIsNone(quiet_mode.state()["owner"])
                # Recorded before the release, since exit() clears user_exited
                # along with the hold.
                self.assertIs(job.meta["quiet_held"], True)

    def test_a_run_the_user_dirtied_is_recorded_as_such(self):
        quiet_mode.enter(OWNER)
        quiet_mode.set_gate(False)
        job = self._job(jobs.STATUS_DONE)
        runner._release_quiet_mode(job)
        self.assertIs(job.meta["quiet_held"], False)

    def test_the_start_event_does_not_release(self):
        # The listener fires on start as well as on every terminal status.
        quiet_mode.enter(OWNER)
        runner._release_quiet_mode(self._job(jobs.STATUS_RUNNING))
        self.assertTrue(quiet_mode.is_active())

    def test_a_stale_job_cannot_release_the_current_run(self):
        quiet_mode.enter("run-new")
        runner._release_quiet_mode(self._job(jobs.STATUS_DONE, {"quiet_owner": "run-old"}))
        self.assertTrue(quiet_mode.is_active())
        self.assertEqual(quiet_mode.state()["owner"], "run-new")

    def test_a_setup_job_is_ignored(self):
        quiet_mode.enter(OWNER)
        runner._release_quiet_mode(self._job(jobs.STATUS_DONE, kind="setup"))
        self.assertTrue(quiet_mode.is_active())

    def test_the_listener_is_registered(self):
        # Registration is what makes "the gate never outlives the run" a
        # property of the job lifecycle rather than of each exit path.
        self.assertIn(runner._release_quiet_mode, jobs.manager._listeners)


class PreflightRefusalTests(_GateCase):
    """A measured run is refused while the machine cannot go quiet."""

    class _Rendered(RuntimeError):
        """Sentinel: start_run got past the preflight check."""

    def _patch_start_run(self, stage):
        """Stub out everything start_run does after the preflight check.

        render_script raises the sentinel, so "the run started" and "the run was
        refused" are two different exceptions rather than a side effect to
        inspect -- and a start_run that stopped calling the check at all would
        fail the refusal test rather than quietly pass it.
        """
        stack = mock.patch.multiple(
            runner,
            normalize_request=mock.DEFAULT,
            render_script=mock.DEFAULT,
        )
        patched = stack.start()
        self.addCleanup(stack.stop)
        patched["normalize_request"].return_value = (
            [{"id": "m"}], stage, ["cpu"], "2026.2.0")
        patched["render_script"].side_effect = self._Rendered

        probe = mock.patch.object(runner.env, "probe",
                                  return_value={"enabled": True, "ready": True})
        probe.start()
        self.addCleanup(probe.stop)

    def test_a_measured_run_is_refused_and_says_which_apps(self):
        self._patch_start_run("benchmark")
        quiet_mode.register_blocker("auto_limited_apps", lambda: {
            "reason": "The balancer is currently auto-limiting one or more apps.",
            "apps": [{"app_id": "hog.scope", "app_name": "hog"}],
            "action": "Resolve them on the Balance page (restore or lock to manual), then start the run.",
        })
        with self.assertRaises(runner.QuietModeBlocked) as caught:
            runner.start_run([{"id": "m"}], "benchmark", ["cpu"], "2026.2.0")
        self.assertEqual(caught.exception.blockers[0]["apps"][0]["app_name"], "hog")
        # The message the REST layer relays has to name the reason, not just the
        # check that fired.
        self.assertIn("auto-limiting", str(caught.exception))
        # Nothing was taken: a refused run must not leave the gate held.
        self.assertFalse(quiet_mode.state()["held"])

    def test_a_clear_machine_starts_the_run(self):
        self._patch_start_run("benchmark")
        with self.assertRaises(self._Rendered):
            runner.start_run([{"id": "m"}], "benchmark", ["cpu"], "2026.2.0")

    def test_a_build_is_not_blocked(self):
        # A download's result is the same file whatever the machine was doing at
        # the time, so it is not worth suspending the operator's monitoring for
        # -- and it must not be refused for a state that cannot affect it.
        self._patch_start_run("build")
        quiet_mode.register_blocker("auto_limited_apps", lambda: {"reason": "blocked"})
        with self.assertRaises(self._Rendered):
            runner.start_run([{"id": "m"}], "build", None, None)


class SamplerLeaseTests(_GateCase):
    """The sampler thread is what keeps the lease alive -- and what fails.

    Self-healing scenario (a): the sampler dies without its job reaching a
    terminal status. Nothing renews, and monitoring has to come back on its own.
    """

    def _sampler(self, tmp_path):
        s = sampler.RunSampler(tmp_path, period_s=0.05, quiet_owner=OWNER)
        s._writer = None          # nothing to write: the CSV is not under test
        s._sample = lambda: {"timestamp_s": time.time()}
        return s

    def test_the_thread_holds_the_gate_open_past_the_ttl(self):
        s = self._sampler(Path("/tmp/quiet-mode-test-metrics.csv"))
        quiet_mode.enter(OWNER)
        thread = threading.Thread(target=s._loop, daemon=True)
        thread.start()
        try:
            time.sleep(_TEST_TTL_S * 2.5)
            self.assertTrue(quiet_mode.is_active())
        finally:
            s._stop.set()
            thread.join(timeout=2)

    def test_a_dead_sampler_lets_the_lease_lapse(self):
        s = self._sampler(Path("/tmp/quiet-mode-test-metrics.csv"))
        quiet_mode.enter(OWNER)
        thread = threading.Thread(target=s._loop, daemon=True)
        thread.start()
        time.sleep(_TEST_TTL_S / 2)
        s._stop.set()                      # the thread dies; the job does not end
        thread.join(timeout=2)

        deadline = time.monotonic() + 2.0
        while quiet_mode.is_active() and time.monotonic() < deadline:
            time.sleep(_TEST_POLL_S)
        self.assertFalse(quiet_mode.is_active())

    def test_the_last_row_is_kept_for_the_live_tiles(self):
        # Quiet mode leaves this thread's 2 Hz data as the only live reading in
        # the process, and it costs nothing to keep: _sample() already produced
        # it for the CSV.
        s = self._sampler(Path("/tmp/quiet-mode-test-metrics.csv"))
        self.assertIsNone(s.latest())
        thread = threading.Thread(target=s._loop, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 2.0
            while s.latest() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIsNotNone(s.latest())
            self.assertIn("timestamp_s", s.latest())
        finally:
            s._stop.set()
            thread.join(timeout=2)

    def test_the_heartbeat_survives_a_manual_gate_drop(self):
        # The lease exists to stop the hold from leaking, not to record whether
        # the gate is up -- so a user watching live data mid-run must not cause
        # the hold to lapse and be re-taken by nothing.
        s = self._sampler(Path("/tmp/quiet-mode-test-metrics.csv"))
        quiet_mode.enter(OWNER)
        quiet_mode.set_gate(False)
        thread = threading.Thread(target=s._loop, daemon=True)
        thread.start()
        try:
            time.sleep(_TEST_TTL_S * 2.5)
            self.assertTrue(quiet_mode.state()["held"])
            self.assertTrue(quiet_mode.set_gate(True))
            self.assertTrue(quiet_mode.is_active())
        finally:
            s._stop.set()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
