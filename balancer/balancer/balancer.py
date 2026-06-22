# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import json
import os, signal, time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

from collections import OrderedDict
from monitor import AppIntercept

from utils.logger import logger
from utils import app_utils
from config.config import b_config
import threading
from multiprocessing import JoinableQueue
import queue
import heapq
from controller.network import NetworkController
from controller.io import IOController


g_limited_apps = OrderedDict()  # tracks rate-limited apps
g_limited_apps_manual = OrderedDict()  # tracks manually rate-limited apps
g_app_id_mapping = {}  # {app_id: list[effective_app_id] | str} – list for multi-cgroup apps (set by set_resource_limit), str fallback for legacy paths
g_extra_cgroup_ids: dict = {}  # {primary_effective_app_id: [extra_id, ...]} for multi-cgroup apps
g_manual_limit_baseline: dict = {}  # {effective_app_id: peak usage snapshot} – persists across restore→limit cycles
                                    # to prevent an artificially low second sample (caused by an active limit)
                                    # from computing an even tighter cap on subsequent limit invocations
is_limited_app_dominant = False  # whether the current top process is a previously-limited app
IO_LIMIT_MBPS_THRESHOLD = 100
IO_LIMIT_IOPS_THRESHOLD = 1000

@dataclass
class WorkloadGroup:
    name: str
    priority: int
    cpu_weight: int
    memory_min: int = 0
    io_weight: int = 100


@dataclass
class WorkloadTask:
    workload: WorkloadGroup
    params: Dict
    pid: Optional[int] = None
    task_id: str = ""


class MaxPriorityQueue:
    def __init__(self):
        self._queue = queue.PriorityQueue()
        self._index = 0  # tie-breaker for equal-priority items

    def put(self, item):
        # Store negated priorities for max-heap; tuples are (neg_priority, index, data)
        priority = -item[1]
        heapq.heappush(self._queue.queue, (priority, self._index, item))
        self._index += 1

    def get(self):
        # Restore the original data on pop
        return heapq.heappop(self._queue.queue)[-1]

    def remove_if(self, condition_func):
        """
        Remove items that satisfy a condition (generic; no business logic).
        :param condition_func: callable receiving (data, priority) tuple, returns bool
        :return: list of removed items
        """
        removed_items = []
        new_queue = []

        for priority, idx, item in self._queue.queue:
            if condition_func(item):
                removed_items.append(item)
            else:
                new_queue.append((priority, idx, item))

        self._queue.queue = new_queue
        heapq.heapify(self._queue.queue)  # restore heap invariant
        return removed_items

    def empty(self):
        """Return True if the queue is empty."""
        return len(self._queue.queue) == 0

    def __str__(self):
        # Display in descending priority order (stored ascending internally)
        items = sorted(((-priority, data) for priority, _, data in self._queue.queue), reverse=True)
        return str([(k, v) for (_, (k, v)) in items])

    def __len__(self):
        """Return the current number of items in the queue."""
        return len(self._queue.queue)


def _split_proportionally(total_budget, all_ids: list, per_cg_usage: dict) -> dict:
    """Distribute *total_budget* across *all_ids* proportionally to each entry in
    *per_cg_usage* ({basename: raw_value}).

    :param total_budget: Total budget to distribute (int MB or CPU%), or ``None``
        meaning "no limit for this resource".  When ``None``, every entry in the
        returned dict is also ``None`` so callers can safely forward the value to
        ``adjust_resources`` which treats ``None`` as "no limit".
    :param all_ids: Ordered list of cgroup basenames to distribute across.
    :param per_cg_usage: {basename: raw usage value} used for proportional weights.

    When *per_cg_usage* is missing or all values are zero, the budget is split
    equally so that single-cgroup apps (empty *all_ids* or no per-cgroup data)
    are never affected and multi-cgroup apps at worst receive equal shares rather
    than N times the intended cap.

    :returns: {basename: allocated_budget} where each value mirrors the type of
              *total_budget* (int >= 1 when a positive budget is given, or None).
              Values sum to approximately *total_budget*.
    """
    if total_budget is None or total_budget == 0:
        return {cg: total_budget for cg in all_ids}
    total_usage = sum(per_cg_usage.get(cg, 0) for cg in all_ids)
    if total_usage <= 0:
        n = len(all_ids) or 1
        each = max(1, total_budget // n)
        return {cg: each for cg in all_ids}
    return {
        cg: max(1, int(total_budget * per_cg_usage.get(cg, 0) / total_usage))
        for cg in all_ids
    }


class DynamicBalancer:
    def __init__(self):
        self.bpf_monitor = AppIntercept("monitor/bpf_event.c")
        self.config = b_config
        self.controlManager = self.bpf_monitor.controlManager
        self.resource_monitor = self.controlManager.res
        self.io_ctl = IOController()

        # Resource management
        self.workload_groups = {}  # registered workload types
        self.running_tasks = {}  # pid -> WorkloadTask
        self.known_pids = set()  # set of already-identified PIDs

        self.is_running = False
        self.app_detect_queue = JoinableQueue(1000000)
        self.app_priority_queue = MaxPriorityQueue()

        self._init_default_workloads()

        # Network controller
        self.network_controller = NetworkController()

    def _init_default_workloads(self):
        default_groups = [
            WorkloadGroup("critical", 100, 300, 2<<30, 500),
            WorkloadGroup("high", 80, 200, 1<<30, 300),
            WorkloadGroup("normal", 50, 100, 0, 200),
            WorkloadGroup("best-effort", 20, 50, 0, 100)
        ]
        # for group in default_groups:
        #     self.register_workload_group(group)

    def start(self):
        """
        Start the service, including the worker thread that processes the task queue.
        """
        self.network_controller.setup_tc_classes_and_filters()
        self.is_running = True

        self.monitor_thread = threading.Thread(target=self._run_monitor_resource_loop, daemon=True)
        self.monitor_thread.start()

        self.handle_thread = threading.Thread(target=self._run_handle_loop, daemon=True)
        self.handle_thread.start()

        self.app_intercept_thread = threading.Thread(target=self._run_app_intercept_loop, daemon=True)
        self.app_intercept_thread.start()

        logger.info("Service started; worker threads are running")

    def _run_monitor_resource_loop(self):
        logger.info("Monitor resource service started")
        global g_limited_apps, g_extra_cgroup_ids, is_limited_app_dominant
        _MIN_IDLE_CHECK = 2.0   # seconds – below this polling is too aggressive
        _MAX_IDLE_CHECK = 30.0  # seconds – above this response latency becomes unacceptable
        _raw_idle = float(getattr(self.config, "monitor_idle_check_interval", 10))
        _pressure_update = float(getattr(self.config, "regular_update_sys_pressure_time", 5))
        # monitor_idle_check_interval must not be shorter than the pressure-data refresh period
        # to avoid making decisions on stale data, and must stay within [2, 30] seconds.
        default_idle_check_interval = max(
            _MIN_IDLE_CHECK,
            min(_MAX_IDLE_CHECK, max(_raw_idle, _pressure_update))
        )
        if default_idle_check_interval != _raw_idle:
            logger.warning(
                "monitor_idle_check_interval=%.1fs clamped to %.1fs "
                "(allowed range [%.0fs, %.0fs], min=regular_update_sys_pressure_time=%.1fs)",
                _raw_idle, default_idle_check_interval, _MIN_IDLE_CHECK, _MAX_IDLE_CHECK, _pressure_update,
            )
        idle_check_interval = default_idle_check_interval
        last_check_time = 0
        last_network_sample_time = 0
        network_sample_interval = 5  # network sampling interval (seconds)
        top_consume_apps = []  # list of top-consuming apps
        reach_threshold = False  # some apps may have negligible resource usage; skip limiting them
        restore_pending = False  # True when there are apps waiting to be restored
        pressure_start_time = None  # timestamp when pressure entered medium/low
        current_pressure = None  # current pressure level; used to detect stability
        STABLE_PERIOD = 1800  # 30-minute stability period (seconds)
        disk_io_not_stressed_start_time = None  # timestamp when disk IO pressure was relieved
        STABLE_DISK_IO_PERIOD = 300  # 5-minute disk IO stability period (seconds)
        policy = self.config.limit_policy['policy']
        # Top-consumer prefetch: only fired on rising edges (low/medium → high) and on
        # critical-state listener entry. Sustained high/critical does NOT re-fetch.
        top_consumer_cache = {"apps": [], "reach_threshold": False, "fetched_at": 0.0}
        prefetch_lock = threading.Lock()
        prefetch_inflight = threading.Event()
        # Debounce window for back-to-back prefetch triggers (e.g. rising-edge fires
        # then listener fires milliseconds later). NOT a validity TTL for the cached
        # data — critical resolve uses the cache as long as it has data; explicit
        # refresh comes from rising-edge / listener / sustained-critical recheck.
        PREFETCH_CACHE_TTL = 5.0
        CRITICAL_PREFETCH_WAIT = 0.35
        # Sustained-critical recheck: after N consecutive critical iters, refresh top
        # in background to detect a new dominant app (the originally-limited top1 may
        # have settled but pressure persists because another app took over).
        SUSTAINED_CRITICAL_REFRESH_ITERS = 5
        sustained_critical_iters = 0
        prev_pressure = None

        def reset_state():
            nonlocal top_consume_apps, idle_check_interval, pressure_start_time
            # logger.debug("reset_state called")
            top_consume_apps = []
            idle_check_interval = default_idle_check_interval
            pressure_start_time = None  # reset timer

        def _start_top_prefetch(reason):
            # In-flight check first; cheap and avoids redundant fetch storms
            if prefetch_inflight.is_set():
                logger.debug(f"Top-consumer prefetch skipped ({reason}): fetch already in flight")
                return
            with prefetch_lock:
                age = time.time() - top_consumer_cache["fetched_at"]
                if top_consumer_cache["apps"] and age < PREFETCH_CACHE_TTL:
                    logger.debug(f"Top-consumer prefetch skipped ({reason}): cache fresh, age={age:.2f}s")
                    return
            prefetch_inflight.set()
            t0 = time.time()
            logger.debug(f"Top-consumer prefetch started ({reason})")

            def _worker():
                try:
                    apps, threshold = self.resource_monitor.get_top_resource_consumers()
                    with prefetch_lock:
                        top_consumer_cache["apps"] = list(apps or [])
                        top_consumer_cache["reach_threshold"] = bool(threshold)
                        top_consumer_cache["fetched_at"] = time.time()
                    logger.debug(
                        f"Top-consumer prefetch completed ({reason}): apps={len(apps)}, "
                        f"reach_threshold={threshold}, took={time.time() - t0:.2f}s"
                    )
                except Exception as e:
                    logger.warning(f"Top-consumer prefetch failed ({reason}): {e}")
                finally:
                    prefetch_inflight.clear()

            threading.Thread(target=_worker, daemon=True).start()

        def _resolve_top_for_critical():
            # Cache is consumed without TTL — refresh is event-driven (rising edge,
            # critical listener, sustained-critical recheck). Empty cache only at
            # cold-start before any trigger fired, in which case wait for in-flight
            # or fall back to synchronous fetch.
            with prefetch_lock:
                apps = list(top_consumer_cache["apps"])
                threshold = bool(top_consumer_cache["reach_threshold"])
                age = time.time() - top_consumer_cache["fetched_at"]
            if apps:
                logger.debug(f"Critical resolve: using cached top (age={age:.2f}s, apps={len(apps)})")
                return apps, threshold

            if prefetch_inflight.is_set():
                logger.debug(f"Critical resolve: waiting up to {CRITICAL_PREFETCH_WAIT}s for in-flight prefetch")
                prefetch_inflight.wait(CRITICAL_PREFETCH_WAIT)
                with prefetch_lock:
                    apps = list(top_consumer_cache["apps"])
                    threshold = bool(top_consumer_cache["reach_threshold"])
                if apps:
                    logger.debug(f"Critical resolve: got cache after wait (apps={len(apps)})")
                    return apps, threshold

            logger.debug("Critical resolve: cache empty, falling back to synchronous fetch")
            return self.resource_monitor.get_top_resource_consumers()

        def _on_critical_state_changed(is_critical):
            if is_critical:
                # Skip the prefetch when passive control is off — _get_top_resource_consumers
                # is a multi-second CPU+IO+GPU sampling pipeline whose only purpose is to
                # feed the auto-limit decision, which is itself disabled.
                prc = self.config.passive_resource_control or {}
                if not bool(prc.get('enabled', True)):
                    logger.debug("Critical-state listener fired but passive control disabled: skipping prefetch")
                    return
                logger.debug("Critical-state listener fired: triggering top-consumer prefetch")
                _start_top_prefetch("critical_listener")

        self.controlManager.register_critical_state_listener(_on_critical_state_changed)

        def handle_network_operations():
            nonlocal last_network_sample_time, current_time
            if self.network_controller.enable_network_control:
                self.network_controller.update_app_network_control()
                self.network_controller.network.get_tc_class_stats(self.network_controller.IFB_DEV,
                                                                   self.network_controller.handle_id + 1,
                                                                   classids=self.network_controller.ingress_classids,
                                                                   direction="ingress")
                self.network_controller.network.get_tc_class_stats(self.network_controller.dev,
                                                                   self.network_controller.handle_id,
                                                                   classids=self.network_controller.egress_classids,
                                                                   direction="egress")
            self.network_controller.network.sample_network_pressure()
            if current_time - last_network_sample_time >= network_sample_interval:
                # Sample ingress traffic
                last_network_sample_time = current_time
                network_data = self.network_controller.network.get_current_pressure()
                tx_pressure, rx_pressure, *_ = self.controlManager.update_network_pressure_level(network_data)
                tx_total_bw = self.network_controller.total_bw * network_data['tx']
                rx_total_bw = self.network_controller.total_bw * network_data['rx']
                logger.debug(
                    f"NetworkMonitor {self.network_controller.dev} TX level: {tx_pressure} (pressure: {network_data['tx']:.2f}),"
                    f" RX level: {rx_pressure} (pressure: {network_data['rx']:.2f})")
                if self.network_controller.enable_network_control:
                    ingress_rates = self.network_controller.network.get_tc_class_stats_rate_ingress()
                    egress_rates = self.network_controller.network.get_tc_class_stats_rate_egress()
                    rates = self.network_controller.get_rates(self.network_controller.handle_id, egress_rates,
                                                              ingress_rates)
                    logger.debug(
                        f"NetworkMonitor {self.network_controller.dev} TX_total_BW={tx_total_bw:,.2f}kbit/s (App Class BW: System - {rates['egress_system']:,.2f},"
                        f" Critical - {rates['egress_critical']:,.2f} , High - {rates['egress_high']:,.2f}, Low - {rates['egress_low']:,.2f}),"
                        f" RX_total_BW={rx_total_bw:,.2f}kbit/s (App Class BW: System - {rates['ingress_system']:,.2f},"
                        f" Critical - {rates['ingress_critical']:,.2f} , High - {rates['ingress_high']:,.2f}, Low - {rates['ingress_low']:,.2f})")
                    self.network_controller.handle_network_pressure(tx_pressure, rx_pressure, ingress_rates,
                                                                    egress_rates, network_data)
        while self.is_running:
            try:
                current_time = time.time()

                # Re-read every iteration so the UI Switch toggle takes effect without
                # restarting the service.  When disabled, only the "find a new top
                # consumer and limit it" branches are skipped; pressure sampling,
                # already-limited-app restoration, pending-queue processing, and
                # network shaping all keep running so previously limited apps still
                # converge back to normal as system pressure subsides.
                _prc = self.config.passive_resource_control or {}
                passive_enabled = bool(_prc.get('enabled', True))

                # Process immediately when the queue is non-empty; poll every 10s otherwise
                if not self.app_priority_queue.empty() or (current_time - last_check_time) >= idle_check_interval:
                    # Use consume_peak_pressure_level() instead of get_current_pressure_level()
                    # so that transient "critical" spikes that occurred while the
                    # idle_check_interval gate was closed are never silently dropped.
                    if policy == "separated":
                        pressure, _, is_disk_io_stressed = self.controlManager.consume_peak_pressure_level()
                    else:  # policy == "combined"
                        pressure, *_ = self.controlManager.consume_peak_pressure_level()
                        is_disk_io_stressed = False

                    last_check_time = current_time
                    # Top-consumer prefetch / recheck only exist to warm the cache for the
                    # auto-limit path.  When passive control is off we are not going to
                    # apply any auto-limit, so skip the multi-second sampling pipeline.
                    if passive_enabled:
                        # Edge trigger: prefetch whenever pressure enters the high band from
                        # any other state (low/medium below, critical above). This is the
                        # core mechanism — by the time we reach critical the cache is warm.
                        # Sustained high stays cached. The critical-state listener is a
                        # backstop for non-high→critical direct jumps.
                        if pressure == "high" and prev_pressure != "high":
                            logger.debug(
                                f"Pressure edge {prev_pressure}→high: triggering top-consumer prefetch"
                            )
                            _start_top_prefetch("entering_high")

                        # Sustained-critical recheck: if critical persists for N iters, the
                        # original top1 has had ample time to settle under its limit. Refresh
                        # top in background to catch a new dominant app that may have taken
                        # over. Counter resets whenever pressure drops out of critical.
                        if pressure == "critical":
                            sustained_critical_iters += 1
                            if sustained_critical_iters >= SUSTAINED_CRITICAL_REFRESH_ITERS:
                                logger.debug(
                                    f"Sustained critical for {sustained_critical_iters} iters: "
                                    f"triggering background top-consumer recheck"
                                )
                                _start_top_prefetch("sustained_critical_recheck")
                                sustained_critical_iters = 0
                        else:
                            sustained_critical_iters = 0
                    else:
                        sustained_critical_iters = 0

                    if policy == "separated":
                        # When passive control is disabled we deliberately fall through to
                        # the restore / queue-drain branches below, even if pressure is
                        # critical: skipping the expensive top-consumer fetch + auto-limit
                        # is the whole point.  Restore only acts on medium/low pressure,
                        # so a genuinely-critical system simply leaves already-limited
                        # apps alone until pressure subsides — same end state, just no
                        # new auto-limits.
                        if passive_enabled and (pressure == "critical" or is_disk_io_stressed):
                            # Reset the low-pressure timer
                            restore_pending = False

                            if not is_disk_io_stressed:
                                pressure_start_time = None
                                if not top_consume_apps:
                                    top_consume_apps, reach_threshold = _resolve_top_for_critical()
                            else:
                                disk_io_not_stressed_start_time = None
                                top_consume_apps = self.resource_monitor.get_top_disk_io_consumers()
                                reach_threshold = True  # IO pressure always counts as threshold-crossing
                            # logger.debug(f"Top resource consumers(currently = 1): {top_consume_apps}")
                            """
                                Top resource consumers:[
                                   {
                                      "process":{
                                         "pid":5508,
                                         "name":"stress",
                                         "cmdline":"stress --cpu 25 --io 30 --vm 3 --vm-bytes 21G",
                                         "score":469.53,
                                         "cpu_avg":96.2,
                                         "mem_rss":16.11,
                                         "io_read_rate":0.0
                                      },
                                      "app":{
                                         "type":"cgroup",
                                         "id":"vte-spawn-4573f009-2887-47a4-a7d8-f573b6965109.scope",
                                         "name":"CGroup: vte-spawn-4573f009-2887-47a4-a7d8-f573b6965109.scope"
                                      }
                                   },
                                   {  # Only top1 currently.
                                     ...
                                   },
                                   ...
                                ]
                            """
                            if top_consume_apps:
                                # Check whether this process has already been rate-limited
                                for app_info in top_consume_apps:
                                    current_app_id = (app_info.get('app') or {}).get('id')

                                    if current_app_id in g_limited_apps:
                                        _, _, _, state = g_limited_apps[current_app_id]
                                        is_limited_app_dominant = (state != "partially_restored")  # only consider fully-limited apps as dominant
                                        break
                                    else:
                                        is_limited_app_dominant = False

                                logger.debug(f"Balance- was the process limited before? {is_limited_app_dominant}")
                                self.controlManager.set_limited_app_dominant(is_limited_app_dominant)

                                if not is_disk_io_stressed:
                                    should_adjust, is_controlled, app_id, limit_rates = self._handle_critical_pressure(
                                        top_consume_apps, reach_threshold)
                                else:
                                    should_adjust, is_controlled, app_id, limit_rates = self._handle_disk_io_stressed(
                                        top_consume_apps)

                                if not is_limited_app_dominant and reach_threshold and should_adjust and app_id:
                                    self._apply_resource_limits(
                                        top_consume_apps[0],
                                        app_id,
                                        limit_rates,
                                        is_controlled,
                                        is_disk_io_stressed=is_disk_io_stressed
                                    )

                                # Remove the checked app regardless of whether it was handled
                                top_consume_apps.pop(0)

                            else:
                                reset_state()

                        elif not self.app_priority_queue.empty():
                            # Process apps in the task queue
                            app_data, priority = self.app_priority_queue.get()
                            logger.info(
                                f"Starting app: {app_data['app_name']} (PID: {app_data['pid']}, Priority: {priority})")
                            os.kill(app_data['pid'], signal.SIGCONT)
                            app_utils.update_app_status(app_data['app_id'], "running")
                            app_utils.callback_manager.send_callback_notification({
                                'app_id': app_data['app_id'],
                                'app_name': app_data['app_name'],
                                'status': "running",
                                'purpose': "app"
                            }, True)
                            # Reset top-app state after draining the queue
                            reset_state()
                        else:
                            if g_limited_apps and not restore_pending:
                                should_check_pressure = (pressure in ("medium", "low") and
                                                         any(app_data[2].get('cpu_mem_limited', False) for app_data in
                                                             g_limited_apps.values()))
                                should_check_io = (not is_disk_io_stressed and
                                                   any(app_data[2].get('io_limited', False) for app_data in
                                                       g_limited_apps.values()))
                                if should_check_pressure or should_check_io:
                                    # Pressure level is unstable; reset the timer
                                    logger.info(f"pressure_start_time: {pressure_start_time}, "
                                                f"current_pressure: {current_pressure}, pressure: {pressure}")
                                    if should_check_pressure:
                                        if (pressure_start_time is None) or (current_pressure != pressure):
                                            pressure_start_time = current_time
                                            current_pressure = pressure
                                            logger.info(
                                                f"Pressure level changed to {pressure}. "
                                                f"Will restore resources after {STABLE_PERIOD} sec if it remains stable.")

                                    if should_check_io:
                                        if disk_io_not_stressed_start_time is None:
                                            disk_io_not_stressed_start_time = current_time
                                            logger.info(
                                                f"Disk IO stress resolved. Will consider for restoration after {STABLE_DISK_IO_PERIOD} sec if it remains stable.")

                                    pressure_stable = (should_check_pressure and
                                                       (current_time - pressure_start_time >= STABLE_PERIOD))
                                    io_stable = (should_check_io and
                                                 (current_time - disk_io_not_stressed_start_time >= STABLE_DISK_IO_PERIOD))
                                    io_double_stable = (should_check_io and
                                                 (current_time - disk_io_not_stressed_start_time >= STABLE_DISK_IO_PERIOD * 2))

                                    logger.info(f"pressure_stable: {pressure_stable}, io_stable: {io_stable}, io_double_stable: {io_double_stable}")

                                    # Check whether the stability period has elapsed
                                    if pressure_stable and pressure == "medium":
                                        restore_pending = True
                                        app_id, (app_name, limit_rates, limit_parts, state) = next(
                                            iter(g_limited_apps.items()))
                                        if state != "partially_restored":
                                            logger.info(
                                                f"Pressure remained at 'medium' for {STABLE_PERIOD} sec. "
                                                f"Partially restoring app {app_id}.")
                                            if self.restore_resources(app_id, app_name, limit_rates, limit_parts, "partial"):
                                                g_limited_apps[app_id] = (
                                                app_name, limit_rates, limit_parts, "partially_restored")
                                            else:
                                                logger.warning(f"Partial restore failed for {app_name}")
                                            g_limited_apps.move_to_end(app_id)
                                    elif io_stable and not io_double_stable:
                                        restore_pending = True
                                        app_id, (app_name, limit_rates, limit_parts, state) = next(
                                            iter(g_limited_apps.items()))
                                        if state != "partially_restored":
                                            logger.info(f"Disk IO stress resolved. Partially restoring app {app_id}.")
                                            if self.restore_resources(app_id, app_name, limit_rates, limit_parts, "partial"):
                                                g_limited_apps[app_id] = (
                                                app_name, limit_rates, limit_parts, "partially_restored")
                                            else:
                                                logger.warning(f"Partial restore failed for {app_name}")
                                            g_limited_apps.move_to_end(app_id)
                                    elif (pressure_stable and pressure == "low") or io_double_stable:
                                        restore_pending = True
                                        app_id, (app_name, limit_rates, limit_parts, state) = next(
                                            iter(g_limited_apps.items()))

                                        # Perform the restore operation
                                        success = self.restore_resources(app_id, app_name, limit_rates, limit_parts,
                                                                         "full")
                                        if success:
                                            updated_limits = g_limited_apps[app_id][2]
                                            is_fully_restored = not (
                                                        updated_limits.get('cpu_mem_limited') or updated_limits.get('io_limited'))
                                            if is_fully_restored:
                                                app_utils.update_app_status(app_id, "running")
                                                app_utils.callback_manager.send_callback_notification({
                                                    'app_id': app_id,
                                                    'app_name': app_name,
                                                    'status': "running",
                                                    'purpose': "app"
                                                }, False)
                                                g_limited_apps.pop(app_id, None)
                                                logger.info(f"Fully restored app {app_id}, removed from limited apps")

                                                # Reset the timer only after full restore and IO stability
                                                if io_double_stable:
                                                    disk_io_not_stressed_start_time = None
                                                    logger.debug("Reset IO stress timer after full restoration")
                                            else:
                                                g_limited_apps.move_to_end(app_id)
                                                logger.info(f"Partial restore for app {app_id}, moved to end of queue")
                                        else:
                                            logger.error(f"Failed to restore resources for app {app_id}")
                                            g_limited_apps.move_to_end(app_id)
                                    restore_pending = False
                                else:
                                    reset_state()
                    elif policy == "combined":
                        # See the matching note in the "separated" branch: when passive
                        # control is off we skip the expensive top fetch + auto-limit and
                        # let the restore / queue-drain branches handle the iteration.
                        if passive_enabled and pressure == "critical":
                            # Reset the low-pressure timer
                            pressure_start_time = None
                            # First time entering critical state: obtain the top-app list
                            restore_pending = False
                            if not top_consume_apps:
                                top_consume_apps, reach_threshold = _resolve_top_for_critical()

                            if top_consume_apps:
                                # Check whether this process has already been rate-limited
                                for app_info in top_consume_apps:
                                    current_app_id = (app_info.get('app') or {}).get('id')

                                    if current_app_id in g_limited_apps:
                                        _, _, _, state = g_limited_apps[current_app_id]
                                        is_limited_app_dominant = (state != "partially_restored")  # only consider fully-limited apps as dominant
                                        break
                                    else:
                                        is_limited_app_dominant = False

                                logger.debug(f"Balance- was the process limited before? {is_limited_app_dominant}")
                                self.controlManager.set_limited_app_dominant(is_limited_app_dominant)
                                # Invoke the dedicated handler
                                should_adjust, is_controlled, app_id, limit_rates = self._handle_critical_pressure(
                                    top_consume_apps, reach_threshold)

                                if not is_limited_app_dominant and reach_threshold and should_adjust and app_id:
                                    # Apply resource adjustments
                                    target = top_consume_apps[0]
                                    app_name = target.get('process', {}).get('name') or ''
                                    total_mem = self.resource_monitor.get_total_memory()
                                    logger.info(f"Adjusting resources for app: {app_id}")
                                    extra_cgroup_ids = target.get('extra_cgroups', [])
                                    per_cg_mem_rss = target.get('per_cgroup_mem_rss', {})
                                    per_cg_cpu = target.get('per_cgroup_cpu', {})

                                    # Initialise limit result flags
                                    resource_limited = False
                                    io_limited = False

                                    # CPU/memory limit – distribute proportionally for multi-cgroup apps
                                    cpu_rate = int(100 * limit_rates["cpu_rate"]) if limit_rates.get("cpu_rate") else None
                                    mem_rate = int(total_mem * limit_rates["mem_rate"]) if limit_rates.get(
                                        "mem_rate") else None

                                    if (cpu_rate is not None or mem_rate is not None) and self.is_running:
                                        if extra_cgroup_ids:
                                            all_ids = [app_id] + extra_cgroup_ids
                                            mem_dist = _split_proportionally(mem_rate, all_ids, per_cg_mem_rss)
                                            cpu_dist = _split_proportionally(cpu_rate, all_ids, per_cg_cpu)
                                            auto_limit = self.controlManager.adjust_resources(
                                                app_id, "critical",
                                                cpu_quota=cpu_dist.get(app_id, cpu_rate),
                                                mem_high=mem_dist.get(app_id, mem_rate),
                                            )
                                            if auto_limit:
                                                resource_limited = True
                                                logger.info(f"Successfully limited CPU/Memory for {app_name} ({app_id})")
                                            else:
                                                logger.warning(f"Failed to limit CPU/Memory for {app_name} ({app_id})")
                                            for extra_id in extra_cgroup_ids:
                                                ok = self.controlManager.adjust_resources(
                                                    extra_id, "critical",
                                                    cpu_quota=cpu_dist.get(extra_id, cpu_rate),
                                                    mem_high=mem_dist.get(extra_id, mem_rate),
                                                )
                                                logger.info(
                                                    f"{'Successfully limited' if ok else 'Failed to limit'} "
                                                    f"CPU/Memory for extra cgroup {extra_id}"
                                                )
                                        else:
                                            auto_limit = self.controlManager.adjust_resources(
                                                app_id,
                                                "critical",
                                                cpu_quota=cpu_rate,
                                                mem_high=mem_rate,
                                            )
                                            if auto_limit:
                                                resource_limited = True
                                                logger.info(f"Successfully limited CPU/Memory for {app_name}")
                                            else:
                                                logger.warning(f"Failed to limit CPU/Memory for {app_name}")

                                    # Disk IO limit
                                    io_limits = limit_rates.get("disk_io_rate", {})
                                    if io_limits and self.is_running:
                                        limits = {
                                            "default": {  # add per-disk entries (e.g. "nvme0n1": {...}) for fine-grained control
                                                "rbps": io_limits['read'] * 1024 ** 2,
                                                "wbps": io_limits['write'] * 1024 ** 2,
                                                "wiops": io_limits['write_iops'],
                                                "riops": io_limits['read_iops']
                                            }
                                        }
                                        io_limited = self.io_ctl.set_disk_io_throttle(
                                            app_id,
                                            limits=limits
                                        )
                                        if not io_limited:
                                            logger.error(f"Failed to set write IO limit for {app_name}")
                                        for extra_id in extra_cgroup_ids:
                                            self.io_ctl.set_disk_io_throttle(extra_id, limits=limits)

                                    # Record in g_limited_apps as long as at least one limit succeeded
                                    if resource_limited or io_limited:
                                        g_limited_apps[app_id] = (app_name, limit_rates, {
                                            'cpu_mem_limited': resource_limited,
                                            'io_limited': io_limited
                                        }, None)  # None indicates fully limited
                                        if extra_cgroup_ids:
                                            g_extra_cgroup_ids[app_id] = extra_cgroup_ids

                                        if is_controlled:
                                            app_utils.update_app_status(app_id, "limited")

                                        app_utils.callback_manager.send_callback_notification({
                                            'app_id': app_id,
                                            'app_name': app_name,
                                            'status': "limited",
                                            'purpose': "app"
                                        }, False)
                                    else:
                                        logger.warning(f"No resource limits successfully applied for {app_name}")

                                # Remove the checked app regardless of whether it was handled
                                top_consume_apps.pop(0)

                            else:
                                reset_state()
                        elif not self.app_priority_queue.empty():
                            # Process apps in the task queue
                            app_data, priority = self.app_priority_queue.get()
                            logger.info(
                                f"Starting app: {app_data['app_name']} (PID: {app_data['pid']}, Priority: {priority})")
                            os.kill(app_data['pid'], signal.SIGCONT)
                            app_utils.update_app_status(app_data['app_id'], "running")
                            app_utils.callback_manager.send_callback_notification({
                                'app_id': app_data['app_id'],
                                'app_name': app_data['app_name'],
                                'status': "running",
                                'purpose': "app"
                            }, True)
                            # Reset top-app state after draining the queue
                            reset_state()
                        else:
                            # Non-critical state: wait for STABLE_PERIOD before restoring
                            if g_limited_apps and not restore_pending:
                                if pressure in ("medium", "low"):
                                    # Pressure is unstable; reset timer — act only after it stabilises
                                    if (pressure_start_time is None) or (current_pressure != pressure):
                                        pressure_start_time = current_time
                                        current_pressure = pressure
                                        logger.info(
                                            f"Pressure level changed to {pressure}. "
                                            f"Will restore resources after {STABLE_PERIOD} sec if it remains stable."
                                        )

                                    # Check whether the stability period has elapsed
                                    elif current_time - pressure_start_time >= STABLE_PERIOD:
                                        restore_pending = True

                                        # Choose restore strategy based on pressure level
                                        if pressure == "medium":
                                            # Cannot remove yet; restore is only partial
                                            app_id, (app_name, limit_rates, limit_parts, state) = next(iter(g_limited_apps.items()))
                                            if state != "partially_restored":
                                                total_mem = self.resource_monitor.get_total_memory()
                                                logger.info(
                                                    f"Pressure remained at 'medium' for {STABLE_PERIOD} sec. "
                                                    f"Partially restoring app {app_id} (twice the rate of limited resources)."
                                                )
                                                # Extra cgroups that were limited alongside the primary
                                                extra_ids = g_extra_cgroup_ids.get(app_id, [])

                                                restore_success = True

                                                # Restore CPU/memory (if previously limited)
                                                if limit_parts.get('cpu_mem_limited', False):
                                                    cpu_restore = int(100 * limit_rates[
                                                        "cpu_rate"] * 2) if "cpu_rate" in limit_rates else None
                                                    mem_restore = int(total_mem * limit_rates[
                                                        "mem_rate"] * 2) if "mem_rate" in limit_rates else None

                                                    if (cpu_restore is not None or mem_restore is not None) and self.is_running:
                                                        cpu_mem_restored = self.controlManager.adjust_resources(
                                                            app_id,
                                                            "medium",
                                                            cpu_quota=cpu_restore,
                                                            mem_high=mem_restore,
                                                            is_restore=False,
                                                        )
                                                        if not cpu_mem_restored:
                                                            logger.error(
                                                                f"Failed to partially restore CPU/Memory for {app_name}")
                                                            restore_success = False
                                                        for extra_id in extra_ids:
                                                            self.controlManager.adjust_resources(
                                                                extra_id, "medium",
                                                                cpu_quota=cpu_restore,
                                                                mem_high=mem_restore,
                                                                is_restore=False,
                                                            )

                                                # Restore IO limits (if previously limited)
                                                if (limit_parts.get('io_limited', False) and "disk_io_rate" in limit_rates) and self.is_running:
                                                    io_restored = True
                                                    io_limits = limit_rates["disk_io_rate"]

                                                    limits = {
                                                        "default": {  # add per-disk entries (e.g. "nvme0n1": {...}) for fine-grained control
                                                            "rbps": io_limits['read'] * 2 * 1024 ** 2,
                                                            "wbps": io_limits['write'] * 2 * 1024 ** 2,
                                                            "wiops": io_limits['write_iops'] * 2,
                                                            "riops": io_limits['read_iops'] * 2
                                                        }
                                                    }
                                                    io_limited = self.io_ctl.set_disk_io_throttle(
                                                        app_id,
                                                        limits=limits
                                                    )

                                                    if not io_limited:
                                                        logger.error(
                                                            f"Failed to partially restore disk IO for {app_name}")
                                                        io_restored = False
                                                    for extra_id in extra_ids:
                                                        self.io_ctl.set_disk_io_throttle(extra_id, limits=limits)

                                                    if not io_restored:
                                                        restore_success = False

                                                # Update state
                                                if restore_success:
                                                    g_limited_apps[app_id] = (
                                                    app_name, limit_rates, limit_parts, "partially_restored")
                                                else:
                                                    logger.warning(f"Partial restore failed for {app_name}")

                                                g_limited_apps.move_to_end(app_id)  # move to end to avoid re-limiting the same app
                                        else:  # pressure == "low"
                                            # Fully restored; remove from tracking
                                            app_id, (app_name, _, limit_parts, _) = g_limited_apps.popitem()
                                            logger.info(
                                                f"Pressure remained at 'low' for {STABLE_PERIOD} sec. "
                                                f"Fully restoring app {app_id} (100% resources)."
                                            )

                                            restore_success = True
                                            extra_ids = g_extra_cgroup_ids.pop(app_id, [])

                                            # Restore CPU/memory (if previously limited)
                                            if limit_parts.get('cpu_mem_limited', False) and self.is_running:
                                                if not self.controlManager.adjust_resources(app_id, "low"):
                                                    logger.error(f"Failed to fully restore CPU/Memory for {app_name}")
                                                    restore_success = False
                                                for extra_id in extra_ids:
                                                    self.controlManager.adjust_resources(extra_id, "low")

                                            # Restore IO limits (if previously limited)
                                            if limit_parts.get('io_limited', False) and self.is_running:
                                                io_restored = True

                                                # Remove IO limits
                                                if not self.io_ctl.restore_disk_io_throttle(app_id):
                                                    logger.error(f"Failed to remove IO limits for {app_name}")
                                                    io_restored = False
                                                for extra_id in extra_ids:
                                                    self.io_ctl.restore_disk_io_throttle(extra_id)

                                                if not io_restored:
                                                    restore_success = False

                                            # Notify the user after full resource restore
                                            if restore_success:
                                                app_utils.update_app_status(app_id, "running")
                                                app_utils.callback_manager.send_callback_notification({
                                                    'app_id': app_id,
                                                    'app_name': app_name,
                                                    'status': "running",
                                                    'purpose': "app"
                                                }, False)
                                            else:
                                                logger.error(f"Failed to fully restore resources for {app_name}")

                                        restore_pending = False
                                        reset_state()  # reset timer and current pressure state
                                else:
                                    reset_state()
                    prev_pressure = pressure
                handle_network_operations()
                time.sleep(1)
            except Exception as e:
                logger.error(f"Error in monitor loop: {str(e)}", exc_info=True)
                reset_state()
                time.sleep(1)

        logger.info("Monitor resource service stopped")

    def _run_handle_loop(self):
        logger.info("Resource handle service is wait for processing")
        while self.is_running:
            try:
                # Dequeue and process tasks from app_detect_queue
                coming_app = self.bpf_monitor.app_pending_queue.get(block=True, timeout=5)
                logger.info(f"_run_handle_loop: Processing app {coming_app}")

                # Look up the app priority in the DB; default to low if not configured
                # priority = "1000"  # critical
                # priority_value = {"Calculator": 1000, "test2": 1500, "test3": 1300}
                priority = app_utils.get_app_priority(app_name=coming_app["app_name"])
                logger.info(f"_run_handle_loop: App {coming_app['app_name']} priority is {priority}")

                # Enqueue the task for processing
                priority_num = app_utils.get_priority_value(priority)
                logger.debug(f"_run_handle_loop: priority value is {priority_num}")
                self.app_priority_queue.put((coming_app, priority_num))
                logger.info(f"_run_handle_loop: Resource insufficient, {coming_app} app added to pending queue")

            except:
                time.sleep(2)
        logger.debug("Exiting _run_handle_loop")

    def _run_app_intercept_loop(self):
        logger.info("Resource app intercept service is wait for processing")

        # Open the perf buffer
        self.bpf_monitor.bpf["events"].open_perf_buffer(self.bpf_monitor.print_event)
        logger.debug("Ctrl+C to exit")

        monitor_apps = app_utils.get_controlled_apps()

        if monitor_apps:
            # Add controlled apps to the BPF monitor list (filter out empty names)
            monitored_names = [app["app_name"] for app in monitor_apps if app.get("app_name") and app["app_name"].strip()]
            self.bpf_monitor.add_to_monitorlist(monitored_names)
            logger.info(f"Monitoring execve() for: {', '.join(monitored_names)}")

            # Adjust OOM priority for critical apps
            logger.debug(f"monitor_apps: {monitor_apps}")
            for app in monitor_apps:
                app_utils.adjust_oom_priority(app["app_id"], app["app_name"], app["priority"], app.get("cmdline", ""))
        else:
            logger.warning("No controlled apps to monitor")

        while self.is_running:
            try:
                # Monitor launch events
                self.bpf_monitor.bpf.perf_buffer_poll(timeout=100)
            except KeyboardInterrupt:
                logger.debug("Exiting...")
                break
            except Exception as e:
                logger.error(f"App intercept error: {str(e)}")
                time.sleep(3)
                break

    def _apply_resource_limits(self, target_app, app_id, limit_rates, is_controlled, is_disk_io_stressed=False):
        """Apply resource limits (common logic)."""
        global g_extra_cgroup_ids
        app_name = target_app.get('process', {}).get('name') or ''
        total_mem = self.resource_monitor.get_total_memory()
        logger.info(f"Adjusting resources for app: {app_id}")

        # Extra cgroups for multi-process apps (e.g. hs_vlm.service alongside hs_agent.service).
        # These are populated by the monitor when it merges multiple cgroups into one entry.
        extra_cgroup_ids = target_app.get('extra_cgroups', [])
        # Per-cgroup breakdown (basename -> raw bytes/cpu_total) for proportional distribution.
        per_cg_mem_rss = target_app.get('per_cgroup_mem_rss', {})
        per_cg_cpu = target_app.get('per_cgroup_cpu', {})

        # Initialise limit result flags
        resource_limited = False
        io_limited = False

        # CPU/memory limits (only applied outside IO-pressure scenarios)
        if not is_disk_io_stressed:
            cpu_rate = int(100 * limit_rates["cpu_rate"]) if limit_rates.get("cpu_rate") else None
            mem_rate = int(total_mem * limit_rates["mem_rate"]) if limit_rates.get("mem_rate") else None

            if (cpu_rate is not None or mem_rate is not None) and self.is_running:
                if extra_cgroup_ids:
                    # Multi-cgroup app: distribute total budget proportionally so that
                    # the aggregate allowed headroom equals the intended cap, not N × cap.
                    all_ids = [app_id] + extra_cgroup_ids
                    mem_dist = _split_proportionally(mem_rate, all_ids, per_cg_mem_rss)
                    cpu_dist = _split_proportionally(cpu_rate, all_ids, per_cg_cpu)
                    primary_ok = self.controlManager.adjust_resources(
                        app_id, "critical",
                        cpu_quota=cpu_dist.get(app_id, cpu_rate),
                        mem_high=mem_dist.get(app_id, mem_rate),
                    )
                    if primary_ok:
                        resource_limited = True
                        logger.info(f"Successfully limited CPU/Memory for {app_name} ({app_id})")
                    for extra_id in extra_cgroup_ids:
                        ok = self.controlManager.adjust_resources(
                            extra_id, "critical",
                            cpu_quota=cpu_dist.get(extra_id, cpu_rate),
                            mem_high=mem_dist.get(extra_id, mem_rate),
                        )
                        logger.info(
                            f"{'Successfully limited' if ok else 'Failed to limit'} "
                            f"CPU/Memory for extra cgroup {extra_id}"
                        )
                else:
                    auto_limit = self.controlManager.adjust_resources(
                        app_id,
                        "critical",
                        cpu_quota=cpu_rate,
                        mem_high=mem_rate,
                    )
                    if auto_limit:
                        resource_limited = True
                        logger.info(f"Successfully limited CPU/Memory for {app_name}")

        # Disk IO limits
        if is_disk_io_stressed and limit_rates.get("disk_io_rate"):
            io_limits = limit_rates.get("disk_io_rate", {})
            if io_limits and self.is_running:
                limits = {
                    "default": {  # add per-disk entries for fine-grained control
                        "rbps": io_limits['read'] * 1024 ** 2,
                        "wbps": io_limits['write'] * 1024 ** 2,
                        "wiops": io_limits['write_iops'],
                        "riops": io_limits['read_iops']
                    }
                }
                io_limited = self.io_ctl.set_disk_io_throttle(app_id, limits=limits)
                if not io_limited:
                    logger.error(f"Failed to set IO limit for {app_name}")
                for extra_id in extra_cgroup_ids:
                    self.io_ctl.set_disk_io_throttle(extra_id, limits=limits)

        # Record the limit outcome
        if resource_limited or io_limited:
            g_limited_apps[app_id] = (
                app_name,
                limit_rates,
                {'cpu_mem_limited': resource_limited, 'io_limited': io_limited},
                None  # None indicates fully limited
            )
            if extra_cgroup_ids:
                g_extra_cgroup_ids[app_id] = extra_cgroup_ids

            if is_controlled:
                app_utils.update_app_status(app_id, "limited")

            app_utils.callback_manager.send_callback_notification({
                'app_id': app_id,
                'app_name': app_name,
                'status': "limited",
                'purpose': "app"
            }, False)

    def restore_resources(self, app_id, app_name, limit_rates, limit_parts, restore_type):
        """
        Common resource restore logic.
        :param app_id: application ID
        :param app_name: application name
        :param limit_rates: rate-limit configuration
        :param limit_parts: flags indicating which resources were limited
        :param restore_type: restore scope ("partial" or "full")
        :return: (success, restored_parts)
        """
        global g_limited_apps, g_extra_cgroup_ids
        restore_success = True
        # Extra cgroups for multi-process apps (e.g. hs_vlm.service alongside hs_agent.service)
        extra_ids = g_extra_cgroup_ids.get(app_id, [])

        if self.is_running:
            # Restore CPU/memory
            if limit_parts.get('cpu_mem_limited', False):
                if restore_type == "partial":
                    cpu_restore = int(100 * limit_rates["cpu_rate"] * 2) if "cpu_rate" in limit_rates else None
                    mem_restore = int(self.resource_monitor.get_total_memory() * limit_rates[
                        "mem_rate"] * 2) if "mem_rate" in limit_rates else None
                    if not self.controlManager.adjust_resources(
                        app_id, "medium", cpu_quota=cpu_restore, mem_high=mem_restore, is_restore=False
                    ):
                        logger.error(f"Failed to partially restore CPU/Memory for {app_name}")
                        restore_success = False
                    for extra_id in extra_ids:
                        self.controlManager.adjust_resources(
                            extra_id, "medium", cpu_quota=cpu_restore, mem_high=mem_restore, is_restore=False
                        )
                else:  # full restore
                    cpu_mem_restored = self.controlManager.adjust_resources(app_id, "low")
                    if not cpu_mem_restored:
                        logger.error(f"Failed to fully restore CPU/Memory for {app_name}")
                        restore_success = False
                    else:
                        g_limited_apps[app_id] = (app_name, limit_rates, {
                            'cpu_mem_limited': False,
                            'io_limited': limit_parts['io_limited']
                        }, None)
                    for extra_id in extra_ids:
                        self.controlManager.adjust_resources(extra_id, "low")
            # Restore IO limits
            if limit_parts.get('io_limited', False):
                if restore_type == "partial" and "disk_io_rate" in limit_rates:
                    io_limits = limit_rates["disk_io_rate"]
                    limits = {
                        "default": {
                            "rbps": io_limits['read'] * 2 * 1024 ** 2,
                            "wbps": io_limits['write'] * 2 * 1024 ** 2,
                            "wiops": io_limits['write_iops'] * 2,
                            "riops": io_limits['read_iops'] * 2
                        }
                    }
                    if not self.io_ctl.set_disk_io_throttle(app_id, limits=limits):
                        logger.error(f"Failed to partially restore disk IO for {app_name}")
                        restore_success = False
                    for extra_id in extra_ids:
                        self.io_ctl.set_disk_io_throttle(extra_id, limits=limits)
                elif restore_type == "full":
                    if not self.io_ctl.restore_disk_io_throttle(app_id):
                        logger.error(f"Failed to fully restore disk IO for {app_name}")
                        restore_success = False
                    else:
                        g_limited_apps[app_id] = (app_name, limit_rates, {
                            'cpu_mem_limited': limit_parts['cpu_mem_limited'],
                            'io_limited': False
                        }, None)
                    for extra_id in extra_ids:
                        self.io_ctl.restore_disk_io_throttle(extra_id)
            # Remove extra cgroup tracking on full restore
            if restore_type == "full":
                g_extra_cgroup_ids.pop(app_id, None)

        return restore_success

    def _handle_disk_io_stressed(self, top_consumers):
        """
            Disk IO pressure handling strategy.
            Disk IO control differs from CPU/memory control:
            1. Unmanaged apps: when their disk IO causes high pressure, intervene only if a managed app is running and consuming significant IO.
            2. Managed apps: only check the status of critical apps when IO pressure is high.
            3. Critical apps: never throttled for disk IO.
        """
        app_info = top_consumers[0] if top_consumers else None
        if not app_info:
            return False, False, None, None

        # Fetch control state
        app_id = app_info['app'].get('id') if app_info.get('app') else None
        app_name = (app_info.get('process', {}).get('name') or '').lower()

        is_controlled, controlled_data = app_utils.get_app_control_info(app_id, app_name)
        priority = controlled_data.get('priority') if controlled_data else None

        # Case 1: current process is an unmanaged app
        if not is_controlled:
            controlled_apps = app_utils.get_controlled_apps() or []
            # logger.debug(f"Disk IO stressed - checking controlled apps: {controlled_apps}")
            for controlled_app in controlled_apps:
                # Check whether the managed app is running and consuming high IO
                running_pids = app_utils.get_app_processes(controlled_app['app_name'])
                logger.debug(f"Disk IO stressed - controlled app {controlled_app['app_name']} running PIDs: {running_pids}")
                if running_pids:
                    # Check whether these PIDs exceed the disk IO threshold
                    # iotop -b -p <pid> -o -k -n 3 -d 1
                    is_high_io, msg = app_utils.check_pids_disk_io_usage(running_pids, threshold_mb=100)

                    if is_high_io:  # unmanaged app must yield its disk IO
                        return True, False, app_id, self.get_limited_rates("undefined")
                    else:
                        logger.info(f"Disk IO stressed - No controlled app with high IO usage found.")
            return False, False, None, None

        # Case 2: current process is a managed app — only check whether a critical app is running with high IO
        elif priority != 'critical':
            critical_apps = app_utils.get_controlled_apps(priority="Critical") or []
            for critical_app in critical_apps:
                running_pids = app_utils.get_app_processes(critical_app['app_name'])
                if running_pids:
                    is_high_io = app_utils.check_pids_disk_io_usage([running_pids], threshold_mb=100)
                    if is_high_io:
                        return True, True, app_id, self.get_limited_rates(priority or "undefined")

        return False, False, None, None

    def _handle_critical_pressure(self, top_consumers, reach_threshold):
        """Handle resource pressure (processes one app per invocation)."""
        if not top_consumers or not top_consumers[0]:
            return False, False, None, None

        # Initialise instance variables
        self._critical_counter = getattr(self, '_critical_counter', 0)
        self._last_notification_time = getattr(self, '_last_notification_time', 0)

        app_info = top_consumers[0]
        app_id = app_info['app'].get('id') if app_info.get('app') else None
        app_name = (app_info.get('process', {}).get('name') or '').lower()

        is_controlled, controlled_data = app_utils.get_app_control_info(app_id, app_name)
        priority = controlled_data.get('priority') if controlled_data else None

        # System resource usage snapshot
        usage_data = self.resource_monitor.get_resource_usage()
        is_sys_busy = usage_data['cpu']['is_busy'] or usage_data['memory']['is_busy']

        # Case 0: special scenario handling
        if is_sys_busy and not reach_threshold:
            current_time = time.time()
            if current_time - self._last_notification_time >= self.config.cooldown_time:
                app_utils.callback_manager.send_callback_notification({
                    'app_id': "",
                    'app_name': "",
                    'status': "high_usage_by_multiple_instances",
                    'purpose': "notify"
                }, False)
                self._last_notification_time = current_time
            self._critical_counter = 0
            return False, False, None, None

        # Unmanaged app or managed non-critical app -> apply adjustment directly
        if not is_controlled or priority != 'critical':
            self._critical_counter = 0
            return True, is_controlled, app_id, self.get_limited_rates(priority or "undefined")

        # Critical managed app -> skip; increment counter
        self._critical_counter += 1
        if self._critical_counter >= 1:
            current_time = time.time()
            if current_time - self._last_notification_time >= self.config.cooldown_time:
                app_utils.callback_manager.send_callback_notification({
                    'app_id': "",
                    'app_name': "",
                    'status': "manual_app_limit_by_user",
                    'purpose': "notify"
                }, False)
                self._last_notification_time = current_time
            self._critical_counter = 0

        return False, False, None, None

    def restore_all_limited_apps_resources(self):
        """Restore all limited apps resources"""
        global g_limited_apps, g_limited_apps_manual, g_extra_cgroup_ids, g_manual_limit_baseline
        if not g_limited_apps and not g_limited_apps_manual:
            logger.info("No limited apps to restore")
            return

        logger.info(
            f"Restoring resources for {len(g_limited_apps)} limited apps and "
            f"{len(g_limited_apps_manual)} manual limited apps")

        all_limited_apps = {}
        all_limited_apps.update(g_limited_apps)
        all_limited_apps.update(g_limited_apps_manual)

        for app_id, (app_name, _, limit_parts, _) in list(all_limited_apps.items()):
            try:
                app_source = "manual" if app_id in g_limited_apps_manual else "auto"
                logger.info(f"Restoring resources for {app_source} limited app: {app_id}, name: {app_name}")
                restore_success = True
                extra_ids = g_extra_cgroup_ids.pop(app_id, [])

                # Restore CPU/Memory limits
                if limit_parts.get('cpu_mem_limited', False):
                    if not self.controlManager.adjust_resources(app_id, "low"):
                        logger.error(f"Failed to restore CPU/Memory for {app_source} limited app {app_id}")
                        restore_success = False
                    for extra_id in extra_ids:
                        self.controlManager.adjust_resources(extra_id, "low")

                # Restore IO limits
                if limit_parts.get('io_limited', False):
                    if not self.io_ctl.restore_disk_io_throttle(app_id):
                        logger.error(f"Failed to remove IO limits for {app_source} limited app {app_id}")
                        restore_success = False
                    for extra_id in extra_ids:
                        self.io_ctl.restore_disk_io_throttle(extra_id)

                if restore_success:
                    logger.info(f"{app_source.capitalize()} limited app resources restoration completed")
            except Exception as e:
                logger.error(f"Failed to restore resources for app {app_id}: {str(e)}")
            finally:
                # Remove app from tracking regardless of restore success to avoid repeated attempts on failure
                g_limited_apps.pop(app_id, None)
                g_limited_apps_manual.pop(app_id, None)
                g_manual_limit_baseline.pop(app_id, None)

        logger.info("All limited apps resources restoration completed")

    def cancel_relaunch_by_app_id(self, app_id: str) -> bool:
        """Remove queue items for the given app_id and terminate the associated process."""
        def condition(item):
            data, _ = item
            return data.get('app_id') == app_id

        # Remove matching items from the queue
        removed_items = self.app_priority_queue.remove_if(condition)

        # Terminate the associated process
        killed = False
        for item in removed_items:
            data, _ = item
            pid = data.get('pid')
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed = True
                except ProcessLookupError:
                    pass

        return killed

    def _get_limit_rate_bounds(self, priority: str) -> Dict[str, Dict[str, float]]:
        priority = (priority or "undefined").lower()
        cpu_bounds = {
            "high": {"min": 0.10, "max": 0.90},
            "medium": {"min": 0.05, "max": 0.70},
            "low": {"min": 0.01, "max": 0.50},
            "undefined": {"min": 0.01, "max": 0.40},
        }
        mem_bounds = {
            "high": {"min": 0.10, "max": 0.60},
            "medium": {"min": 0.05, "max": 0.40},
            "low": {"min": 0.01, "max": 0.30},
            "undefined": {"min": 0.01, "max": 0.30},
        }
        return {
            "cpu": cpu_bounds.get(priority, cpu_bounds["undefined"]),
            "memory": mem_bounds.get(priority, mem_bounds["undefined"]),
        }

    @staticmethod
    def _clamp_rate(value: Optional[float], low: float, high: float) -> Optional[float]:
        if value is None:
            return None
        return max(low, min(high, float(value)))

    def _get_policy_rate_options(self, resource: str, priority: str, current_rate: Optional[float]) -> list[float]:
        """Return sorted percentage options derived from yaml limit_policy rates."""
        policy = (self.config.limit_policy or {}).get(resource, {}) if hasattr(self.config, 'limit_policy') else {}
        rate_cfg = policy.get("rate", {}) if isinstance(policy, dict) else {}
        values: list[float] = []

        if isinstance(rate_cfg, dict):
            for raw in rate_cfg.values():
                try:
                    v = float(raw)
                    if v > 0:
                        values.append(v)
                except (TypeError, ValueError):
                    continue

            # Make sure current-priority config is always included if present.
            p_val = rate_cfg.get((priority or "undefined").lower())
            try:
                if p_val is not None:
                    pv = float(p_val)
                    if pv > 0:
                        values.append(pv)
            except (TypeError, ValueError):
                pass

        if current_rate is not None:
            values.append(float(current_rate))

        if not values:
            return []

        unique_sorted = sorted({round(v * 100, 1) for v in values if v > 0})
        return unique_sorted

    @staticmethod
    def _is_io_limit_reached(io_read_mb: float, io_write_mb: float, io_read_iops: float, io_write_iops: float) -> bool:
        return (
            (io_read_mb + io_write_mb) >= IO_LIMIT_MBPS_THRESHOLD or
            (io_read_iops + io_write_iops) >= IO_LIMIT_IOPS_THRESHOLD
        )

    def _load_app_limit_overrides(self, app_id: str) -> Optional[Dict[str, Any]]:
        """Load per-app manually saved limit overrides from the DB."""
        try:
            from db.DatabaseModel import AIAppPriority
            record = AIAppPriority.query().filter(AIAppPriority.app_id == app_id).first()
            if record and record.limit_overrides_json:
                return json.loads(record.limit_overrides_json)
        except Exception as e:
            logger.debug(f"Could not load per-app limit overrides for '{app_id}': {e}")
        return None

    def get_resource_limit_profile(self, app_id: str, app_name: str, priority: str = "undefined") -> Dict[str, Any]:
        priority = (priority or "undefined").lower()
        app_overrides = self._load_app_limit_overrides(app_id)
        rates = self.get_limited_rates(priority, limit_overrides=app_overrides)
        bounds = self._get_limit_rate_bounds(priority)

        cpu_rate = rates.get("cpu_rate")
        mem_rate = rates.get("mem_rate")
        # Use the saved per-app disk IO rate values for display regardless of whether the
        # enabled switch is on or off.  get_limited_rates() skips populating disk_io_rate
        # when disk_enabled=False, which would cause the form to fall back to config defaults
        # instead of the previously saved values.
        saved_disk_rate = (
            app_overrides.get("disk_io", {}).get("rate")
            if isinstance(app_overrides, dict) and isinstance(app_overrides.get("disk_io"), dict)
            else None
        )
        io_rate = rates.get("disk_io_rate") or (saved_disk_rate if isinstance(saved_disk_rate, dict) else {})
        cpu_options = self._get_policy_rate_options("cpu", priority, cpu_rate)
        mem_options = self._get_policy_rate_options("memory", priority, mem_rate)

        usage = app_utils.get_app_resource_usage(app_id, app_name) or {}
        io_read_mb = usage.get("io_read_mb", 0)
        io_write_mb = usage.get("io_write_mb", 0)
        io_read_iops = usage.get("io_read_iops", 0)
        io_write_iops = usage.get("io_write_iops", 0)
        is_io_limit = self._is_io_limit_reached(io_read_mb, io_write_mb, io_read_iops, io_write_iops)

        process_names = app_utils._get_app_process_names(app_id=app_id, app_name=app_name) or []
        cgroup_paths = usage.get("cgroup_paths") or ([usage.get("cgroup_path")] if usage.get("cgroup_path") else [])
        cgroup_ids = [os.path.basename(path) for path in cgroup_paths if path]

        # Config-level defaults for disk IO at this priority, used as fallback when no per-app override is active.
        disk_policy = (self.config.limit_policy or {}).get('disk_io', {}) if hasattr(self.config, 'limit_policy') else {}
        disk_rates_cfg = disk_policy.get('rate', {}) if isinstance(disk_policy, dict) else {}
        cfg_disk_rate = (
            disk_rates_cfg.get(priority)
            or disk_rates_cfg.get('undefined')
            or {}
        )

        def _io_item(key: str, v: Any) -> Dict[str, int]:
            cfg_default = cfg_disk_rate.get(key) if isinstance(cfg_disk_rate, dict) else None
            if v is not None:
                value = max(1, int(v))
            elif cfg_default is not None:
                value = max(1, int(cfg_default))
            else:
                value = 1
            return {"value": value, "min": 1, "max": value}

        # disk IO section is enabled if the user has previously saved a per-app override with
        # enabled=True, OR if the app currently exhibits high IO pressure.
        has_app_io_override = bool(
            isinstance(app_overrides, dict)
            and isinstance(app_overrides.get("disk_io"), dict)
            and app_overrides["disk_io"].get("enabled", False)
        )
        disk_io_enabled = has_app_io_override or (bool(io_rate) and is_io_limit)

        return {
            "cpu": {
                "enabled": cpu_rate is not None,
                "value": round((cpu_rate or 0) * 100, 2),
                "min": round(bounds["cpu"]["min"] * 100, 2),
                "max": round(bounds["cpu"]["max"] * 100, 2),
                "options": cpu_options,
            },
            "memory": {
                "enabled": mem_rate is not None,
                "value": round((mem_rate or 0) * 100, 2),
                "min": round(bounds["memory"]["min"] * 100, 2),
                "max": round(bounds["memory"]["max"] * 100, 2),
                "options": mem_options,
            },
            "disk_io": {
                "enabled": disk_io_enabled,
                "is_io_limit": is_io_limit,
                "write": _io_item("write", io_rate.get("write")),
                "read": _io_item("read", io_rate.get("read")),
                "write_iops": _io_item("write_iops", io_rate.get("write_iops")),
                "read_iops": _io_item("read_iops", io_rate.get("read_iops")),
            },
            "process_names": process_names,
            "cgroup_ids": sorted(set(cgroup_ids)),
        }

    def get_limited_rates(
            self,
            priority: str,
            limit_overrides: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Union[float, Dict[str, int], None]]:
        """
        Return all enabled resource limit configurations for the given priority.
        :return:
            {
                "cpu_rate": float or None,
                "mem_rate": float or None,
                "disk_io_rate": {"write": x, "read": y} or None
            }
        """
        priority = priority.lower()
        result = {
            "cpu_rate": None,
            "mem_rate": None,
            "disk_io_rate": None
        }

        # Validate limit_policy configuration
        if not hasattr(self.config, 'limit_policy'):
            return result

        bounds = self._get_limit_rate_bounds(priority)
        overrides = limit_overrides or {}

        limit_policy_cfg = self.config.limit_policy or {}

        # Handle CPU limits
        cpu_cfg = limit_policy_cfg.get('cpu', {})
        cpu_rates = cpu_cfg.get('rate', {})
        cpu_ovr = overrides.get("cpu", {}) if isinstance(overrides.get("cpu", {}), dict) else {}
        cpu_enabled = cpu_ovr.get("enabled", cpu_cfg.get('enabled', False))
        cpu_rate = cpu_ovr.get("rate", cpu_rates.get(priority))
        if cpu_enabled and cpu_rate is not None:
            result['cpu_rate'] = self._clamp_rate(cpu_rate, bounds["cpu"]["min"], bounds["cpu"]["max"])

        # Handle memory limits
        mem_cfg = limit_policy_cfg.get('memory', {})
        mem_rates = mem_cfg.get('rate', {})
        mem_ovr = overrides.get("memory", {}) if isinstance(overrides.get("memory", {}), dict) else {}
        mem_enabled = mem_ovr.get("enabled", mem_cfg.get('enabled', False))
        mem_rate = mem_ovr.get("rate", mem_rates.get(priority))
        if mem_enabled and mem_rate is not None:
            result['mem_rate'] = self._clamp_rate(mem_rate, bounds["memory"]["min"], bounds["memory"]["max"])

        # Handle disk IO limits
        disk_cfg = limit_policy_cfg.get('disk_io', {})
        disk_rates = disk_cfg.get('rate', {})
        default_disk_rate = disk_rates.get(priority)
        disk_ovr = overrides.get("disk_io", {}) if isinstance(overrides.get("disk_io", {}), dict) else {}
        disk_enabled = disk_ovr.get("enabled", disk_cfg.get('enabled', False))
        disk_rate = disk_ovr.get("rate", default_disk_rate)
        if disk_enabled and isinstance(disk_rate, dict):
            def _to_pos_int(name: str, fallback: int) -> int:
                raw = disk_rate.get(name, fallback)
                try:
                    return max(1, int(float(raw)))
                except (TypeError, ValueError):
                    return max(1, int(fallback))

            default_write = default_disk_rate.get("write", 1) if default_disk_rate else 1
            default_read = default_disk_rate.get("read", 1) if default_disk_rate else 1
            default_wiops = default_disk_rate.get("write_iops", 1) if default_disk_rate else 1
            default_riops = default_disk_rate.get("read_iops", 1) if default_disk_rate else 1
            result['disk_io_rate'] = {
                "write": _to_pos_int("write", default_write),
                "read": _to_pos_int("read", default_read),
                "write_iops": _to_pos_int("write_iops", default_wiops),
                "read_iops": _to_pos_int("read_iops", default_riops),
            }

        logger.debug(f"Priority '{priority}' limit rates: {result}")
        return result

    def set_resource_limit(
            self,
            app_id: str,
            app_name: str,
            priority: str = None,
            limit_overrides: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Set resource limits for an application (balanced policy)."""
        global g_limited_apps_manual, g_app_id_mapping, g_extra_cgroup_ids, g_manual_limit_baseline

        # Get limit rates based on priority
        priority = priority or "undefined"
        if isinstance(limit_overrides, dict):
            try:
                from db.DatabaseModel import AIAppPriority
                AIAppPriority.update_record(id=app_id, limit_overrides_json=json.dumps(limit_overrides))
            except Exception as e:
                logger.warning(f"Failed to persist per-app limit overrides for '{app_id}': {e}")
        limit_rates = self.get_limited_rates(priority, limit_overrides=limit_overrides)
        if not limit_rates:
            logger.error(f"No limit rates defined for priority: {priority}")
            return False

        # Get app resource usage data.
        usage = app_utils.get_app_resource_usage(app_id, app_name)
        if usage is None:
            logger.warning(f"No resource usage data for {app_name}, using empty defaults")
            usage = {}

        # For multi-process apps usage.cgroup_paths lists every cgroup; for
        # single-cgroup apps fall back to the single cgroup_path.
        all_cgroup_paths = usage.get("cgroup_paths") or (
            [usage["cgroup_path"]] if usage.get("cgroup_path") else []
        )
        effective_app_ids = [os.path.basename(p) for p in all_cgroup_paths if p]
        if not effective_app_ids:
            logger.warning(f"Could not determine cgroup path for {app_name} (ID: {app_id})")
            return False
        effective_app_id = effective_app_ids[0]   # primary (lexicographically smallest cgroup)
        extra_effective_ids = effective_app_ids[1:]

        # Collect raw (unfiltered) resource values from the current sample.
        raw_cpu_percent = usage.get("cpu_percent", 0)
        mem_current = usage.get("mem_current", 0) + usage.get("mem_swap_current", 0)  # RSS + swap = true working set
        io_read_mb = usage.get("io_read_mb", 0)
        io_write_mb = usage.get("io_write_mb", 0)
        io_read_iops = usage.get("io_read_iops", 0)
        io_write_iops = usage.get("io_write_iops", 0)

        # Peak-latch: if this app was previously manually limited, compare the current
        # sample against the stored peak and take the higher value for each resource.
        # This prevents a second "limit" invocation from computing an even tighter cap
        # because the first limit is still active and artificially suppresses the reading.
        # The baseline stores raw (pre-filter) cpu_percent so comparisons remain meaningful
        # even when the first sample was below the 10% threshold.
        baseline = g_manual_limit_baseline.get(effective_app_id, {})
        if baseline:
            raw_cpu_percent = max(raw_cpu_percent, baseline.get("cpu_percent", 0))
            mem_current = max(mem_current, baseline.get("mem_total", 0))
            io_read_mb = max(io_read_mb, baseline.get("io_read_mb", 0))
            io_write_mb = max(io_write_mb, baseline.get("io_write_mb", 0))
            io_read_iops = max(io_read_iops, baseline.get("io_read_iops", 0))
            io_write_iops = max(io_write_iops, baseline.get("io_write_iops", 0))
            logger.debug(
                f"[peak-latch] {app_name}: CPU {usage.get('cpu_percent', 0):.1f}%→{raw_cpu_percent:.1f}% "
                f"Mem {usage.get('mem_current', 0) + usage.get('mem_swap_current', 0):.1f}→{mem_current:.1f} MB"
            )

        # Apply CPU threshold filter once, after peak-latch is resolved.
        # cpu_percent here is "share of all cores" (Δ / elapsed / num_cpus); a single
        # core fully pegged on a 16-core box reads ~6.3%. A 10% gate would discard
        # 1-core workloads entirely, so use 2% (~1/3 of one core on a 16-core box).
        cpu_usage_percent = raw_cpu_percent if raw_cpu_percent >= 2 else 0

        # Keep automatic IO-pressure gating for legacy/auto paths, but if user passes
        # disk_io overrides from UI, honor user decision directly.
        is_io_limit = self._is_io_limit_reached(io_read_mb, io_write_mb, io_read_iops, io_write_iops)
        force_user_io_limit = bool(
            isinstance(limit_overrides, dict) and isinstance(limit_overrides.get("disk_io"), dict)
        )

        # Set limits based on usage and configured rates.
        # Use max(1, int(...)) so a small but non-zero usage*rate (e.g.
        # 6.3 * 0.05 = 0.315) doesn't floor to 0 and look like "no limit
        # requested" — controller.adjust_resources rejects cpu_quota=0 anyway.
        cpu_quota = (max(1, int(cpu_usage_percent * limit_rates["cpu_rate"]))
                     if (limit_rates.get("cpu_rate") and cpu_usage_percent > 0) else None)
        mem_high = (max(1, int(mem_current * limit_rates["mem_rate"]))
                    if (limit_rates.get("mem_rate") and mem_current > 0) else None)
        io_limits = limit_rates.get("disk_io_rate", {})
        should_apply_io_limit = bool(io_limits) and (force_user_io_limit or is_io_limit)

        # Quantization trace: shows whether int(usage * rate) collapsed a real
        # usage reading to 0 (i.e. usage*rate < 1). Helps diagnose cases where
        # cpu_usage_percent>0 but adjust_resources later rejects cpu_quota=0.
        logger.debug(
            f"[set_resource_limit] {app_name}: cpu_usage_percent={cpu_usage_percent} "
            f"* cpu_rate={limit_rates.get('cpu_rate')} -> cpu_quota={cpu_quota}; "
            f"mem_current={mem_current}MB * mem_rate={limit_rates.get('mem_rate')} -> mem_high={mem_high}; "
            f"is_io_limit={is_io_limit} force_user_io_limit={force_user_io_limit} "
            f"should_apply_io_limit={should_apply_io_limit}"
        )

        # If there is nothing to limit (process undetectable or usage too low), tell
        # the caller so it can surface a friendly message — this is NOT a failure.
        # Returning a {"skipped": reason} dict lets the HTTP layer respond 200 with
        # the reason as retmsg (single notification, dialog auto-closes), instead of
        # the generic "No matching app found or failed to set resource limit" error.
        no_cpu_limit = cpu_quota is None
        no_mem_limit = mem_high is None
        no_io_limit = not should_apply_io_limit
        if no_cpu_limit and no_mem_limit and no_io_limit:
            reason = (
                f"Unable to detect resource usage for {app_name}; skipping limit. Please select another application."
                if not usage
                else f"{app_name} has negligible resource usage (CPU<10%, memory≈0, IO<100 MB/s and <1000 IOPS); no limit needed. Please select another application."
            )
            logger.warning(reason)
            return {"skipped": reason}

        logger.debug(f"Calculated limits - CPU: {cpu_quota if cpu_quota else 'No Limit'}, "
                     f"Memory: {mem_high if mem_high else 'No Limit'}, is_io_limit: {is_io_limit}, "
                     f"force_user_io_limit: {force_user_io_limit}, should_apply_io_limit: {should_apply_io_limit}")

        # 5. Apply resource limits
        resource_limited = False
        io_limited = False

        # Per-cgroup breakdown for proportional distribution (keyed by basename).
        # Only present for multi-process apps from _get_multi_process_app_resource_usage.
        per_cg_mem = usage.get('per_cgroup_mem', {})       # {basename: bytes}
        per_cg_cpu_delta = usage.get('per_cgroup_cpu_delta', {})  # {basename: cpu usec delta}

        # Apply peak-latch to per-cgroup breakdowns so proportional distribution uses historical peak weights.
        if baseline:
            baseline_pcg_mem = baseline.get("per_cgroup_mem", {})
            baseline_pcg_cpu = baseline.get("per_cgroup_cpu_delta", {})
            if baseline_pcg_mem:
                per_cg_mem = {
                    cg: max(per_cg_mem.get(cg, 0), baseline_pcg_mem.get(cg, 0))
                    for cg in set(per_cg_mem) | set(baseline_pcg_mem)
                }
            if baseline_pcg_cpu:
                per_cg_cpu_delta = {
                    cg: max(per_cg_cpu_delta.get(cg, 0), baseline_pcg_cpu.get(cg, 0))
                    for cg in set(per_cg_cpu_delta) | set(baseline_pcg_cpu)
                }

        # CPU/memory limits – distribute proportionally across multi-cgroup apps so the
        # aggregate allowed headroom equals the intended cap, not N × cap.
        if (cpu_quota is not None or mem_high is not None) and self.is_running:
            if extra_effective_ids:
                all_ids = [effective_app_id] + extra_effective_ids
                mem_dist = _split_proportionally(mem_high, all_ids, per_cg_mem)
                cpu_dist = _split_proportionally(cpu_quota, all_ids, per_cg_cpu_delta)
                primary_ok = self.controlManager.adjust_resources(
                    effective_app_id, "critical",
                    cpu_quota=cpu_dist.get(effective_app_id, cpu_quota),
                    mem_high=mem_dist.get(effective_app_id, mem_high),
                )
                if primary_ok:
                    resource_limited = True
                    # The memory limit will affect the data of PSI, causing misjudgment of the system pressure,
                    # and it is necessary to reduce the effect of data on psi
                    self.controlManager.set_limited_app_dominant(True)
                    logger.info(f"Successfully set CPU/Memory limits for {app_name} ({effective_app_id})")
                else:
                    logger.error(f"Failed to set CPU/Memory limits for {app_name} ({effective_app_id})")
                for extra_id in extra_effective_ids:
                    ok = self.controlManager.adjust_resources(
                        extra_id, "critical",
                        cpu_quota=cpu_dist.get(extra_id, cpu_quota),
                        mem_high=mem_dist.get(extra_id, mem_high),
                    )
                    logger.info(
                        f"{'Successfully set' if ok else 'Failed to set'} "
                        f"CPU/Memory limits for extra cgroup {extra_id}"
                    )
            else:
                if self.controlManager.adjust_resources(
                        effective_app_id, "critical",
                        cpu_quota=cpu_quota,
                        mem_high=mem_high
                ):
                    resource_limited = True
                    # The memory limit will affect the data of PSI, causing misjudgment of the system pressure,
                    # and it is necessary to reduce the effect of data on psi
                    self.controlManager.set_limited_app_dominant(True)
                    logger.info(f"Successfully set CPU/Memory limits for {app_name} ({effective_app_id})")
                else:
                    logger.error(f"Failed to set CPU/Memory limits for {app_name} ({effective_app_id})")

        # Disk IO limits – apply to primary and all extra cgroups
        if should_apply_io_limit and io_limits and self.is_running:
            limits = {
                "default": {  # add per-disk entries for fine-grained control
                    "rbps": io_limits['read'] * 1024 ** 2,
                    "wbps": io_limits['write'] * 1024 ** 2,
                    "wiops": io_limits['write_iops'],
                    "riops": io_limits['read_iops']
                }
            }
            io_limited = self.io_ctl.set_disk_io_throttle(effective_app_id, limits=limits)
            if io_limited:
                logger.info(f"Successfully set disk IO limits for {app_name} ({effective_app_id})")
            else:
                logger.error(f"Failed to set disk IO limit for {app_name} ({effective_app_id})")
            for extra_id in extra_effective_ids:
                self.io_ctl.set_disk_io_throttle(extra_id, limits=limits)

        # Manual limits go to g_limited_apps_manual and should NOT be auto-restored.
        # Remove from g_limited_apps if it was previously auto-limited to prevent
        # the monitoring loop from auto-restoring a manually-limited app.
        if effective_app_id in g_limited_apps:
            g_limited_apps.pop(effective_app_id, None)
            logger.info(f"Removed {app_name} from auto-limited apps (now manually limited)")

        # 6. Record the limit state (as long as at least one limit succeeded)
        if resource_limited or io_limited:
            g_limited_apps_manual[effective_app_id] = (app_name, limit_rates, {
                'cpu_mem_limited': resource_limited,
                'io_limited': io_limited
            }, None)  # None indicates fully limited
            g_app_id_mapping[app_id] = effective_app_ids  # store full list for restore
            if extra_effective_ids:
                g_extra_cgroup_ids[effective_app_id] = extra_effective_ids
            app_utils.update_app_status(app_id, "a_limited")
            app_utils.callback_manager.send_callback_notification({
                'app_id': app_id,
                'app_name': app_name,
                'status': "a_limited",
                'purpose': "app"
            }, False)
            # Save/update the peak-latch baseline so future limit invocations use max(current, peak).
            # Intentionally not cleared on restore so that a re-limit after restore still benefits
            # from the historical high-water mark.  raw_cpu_percent already holds max(current, baseline)
            # after the peak-latch block above.
            g_manual_limit_baseline[effective_app_id] = {
                "cpu_percent": raw_cpu_percent,
                "mem_total": mem_current,
                "io_read_mb": io_read_mb,
                "io_write_mb": io_write_mb,
                "io_read_iops": io_read_iops,
                "io_write_iops": io_write_iops,
                "per_cgroup_mem": per_cg_mem,
                "per_cgroup_cpu_delta": per_cg_cpu_delta,
            }
            logger.info(f"Recorded resource limits for {app_name}")
            return True

        logger.warning(f"No resource limits successfully applied for {app_name}")
        return False

    def set_restore_resource(self, app_id: str) -> bool:
        """Restore resource limits for the given app_id."""
        global g_limited_apps_manual, g_app_id_mapping, g_extra_cgroup_ids

        # Get the effective app ID(s) – may be a list for multi-cgroup apps
        raw = g_app_id_mapping.pop(app_id, app_id)
        effective_app_ids = raw if isinstance(raw, list) else [raw]
        effective_app_id = effective_app_ids[0]
        extra_effective_ids = effective_app_ids[1:]
        # Also pick up any extras recorded via g_extra_cgroup_ids (e.g. from auto path)
        extra_effective_ids = extra_effective_ids or g_extra_cgroup_ids.pop(effective_app_id, [])

        app_name, _, limit_parts, _ = g_limited_apps_manual.pop(effective_app_id, (None, None, {}, None))
        restore_success = True
        try:
            logger.info(f"Restoring resources for app: {app_id}, name: {app_name}")

            # Restore CPU/memory – primary and all extra cgroups
            if limit_parts.get('cpu_mem_limited', False):
                if not self.controlManager.adjust_resources(effective_app_id, "low"):
                    logger.error(f"Failed to restore CPU/Memory for {app_id} ({effective_app_id})")
                    restore_success = False
                for extra_id in extra_effective_ids:
                    self.controlManager.adjust_resources(extra_id, "low")

            # Restore IO limits – primary and all extra cgroups
            if limit_parts.get('io_limited', False):
                if not self.io_ctl.restore_disk_io_throttle(effective_app_id):
                    logger.error(f"Failed to remove IO limits for {app_id} ({effective_app_id})")
                    restore_success = False
                for extra_id in extra_effective_ids:
                    self.io_ctl.restore_disk_io_throttle(extra_id)

            if restore_success:
                app_utils.update_app_status(app_id, "running")
                app_utils.callback_manager.send_callback_notification({
                    'app_id': app_id,
                    'app_name': app_name,
                    'status': "running",
                    'purpose': "app"
                }, False)
                logger.info(f"Resources restored for {app_id}")

            return restore_success
        except Exception as e:
            logger.error(f"Failed to restore resources for {app_id}: {str(e)}")
            return False
        finally:
            # PSI data is may not right, so we need to delay the reset of dominant status.
            time.sleep(self.config.regular_update_sys_pressure_time)
            self.controlManager.set_limited_app_dominant(False)

    def _execute_task(self, task: WorkloadTask, pressure_level: str) -> bool:
        """Execute a queued workload task."""
        try:
            if task.pid:
                self.running_tasks[task.pid] = task
                logger.info("Task %s registered (PID: %d)", task.workload.name, task.pid)

                self.controlManager.adjust_resources("", pressure_level)
                return True
            return False
        except Exception as e:
            logger.error("Task registration failed: %s", str(e))
            return False


    def register_workload_group(self, group: WorkloadGroup):
        """Register a workload type."""
        with self._lock:
            self.workload_groups[group.name] = group
            logger.info(f"Registered workload group: {group.name}")


    def add_workload(self, group_name: str, params: Dict = None) -> bool:
        """Add a concrete task to the processing queue."""
        if group_name not in self.workload_groups:
            logger.error(f"Unknown workload group: {group_name}")
            return False

        task = {
            "type": "new_app",
            "group": group_name,
            "params": params or {},
            "task_id": f"wl_{time.time_ns()}"
        }
        self.push_task(task)
        logger.debug(f"add workload to task: {task}")
        return True


    def shutdown(self):
        """
        Stop the service thread, wait for it to finish, and ensure all queued tasks are processed.
        """
        logger.info("Service is stopping.")
        if not self.is_running:
            logger.debug("Service is already stopped; no action needed")
            return
        self.is_running = False

        self.restore_all_limited_apps_resources()
        self.network_controller.clear_network_rules_on_exit()
        if hasattr(self, "monitor_thread"):
            self.monitor_thread.join(timeout=1)
        if hasattr(self, "handle_thread"):
            self.handle_thread.join(timeout=1)
        if hasattr(self, "app_intercept_thread"):
            self.app_intercept_thread.join(timeout=1)
        logger.info("Service stopped; all threads have exited")
