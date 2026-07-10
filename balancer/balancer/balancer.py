# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import json
import os, signal, subprocess, time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Union

from collections import OrderedDict
from controller.app_intercept import AppIntercept

from utils.logger import logger
from utils import app_utils
from config.config import b_config
import threading
from multiprocessing import JoinableQueue
import queue
import heapq
from controller.network import NetworkController
from controller.io import IOController


IO_LIMIT_MBPS_THRESHOLD = 100
IO_LIMIT_IOPS_THRESHOLD = 1000


@dataclass
class LimitedApp:
    """All runtime state for one app currently under a resource limit.

    A single ``LimitRegistry.apps`` entry keyed by the primary effective
    app id (the lexicographically-first cgroup basename).  ``source``
    distinguishes pressure-driven ("auto") from REST/UI ("manual") limits
    so the pressure loop never restores or replaces a manual limit.

    Fields:
      * public_app_id — the public app id (DB primary key) used for
            status updates and SSE callbacks.  For auto limits it may
            equal the cgroup key.
      * limit_rates   — the rate config used when the limit was applied.
      * limit_parts   — {'cpu_mem_limited': bool, 'io_limited': bool}.
      * state         — ``None`` for fully limited, ``"partially_restored"``
            after a partial restore (auto only).
      * cgroups       — [primary, *extras]; multi-cgroup apps fan restores
            out across every entry.
      * pids          — snapshot of the app's PIDs at limit time, used by
            the reaper to detect that the app has closed (see
            DynamicBalancer._is_app_closed).
    """
    public_app_id: str
    app_name: str
    source: str                              # "auto" | "manual"
    limit_rates: dict
    limit_parts: dict
    state: Optional[str] = None
    cgroups: list = field(default_factory=list)
    pids: set = field(default_factory=set)


class LimitRegistry:
    """Runtime registry of every app currently under a resource limit.

    Fields:
      * apps — OrderedDict[primary_effective_app_id, LimitedApp].  The
            single source of truth for both auto- and manual-limited apps
            (see ``LimitedApp.source``).  Insertion order is preserved so
            the auto restore path can pop the oldest limit first.
      * manual_limit_baseline — {effective_app_id: peak usage snapshot}
            that persists across restore→limit cycles to keep the
            manually-applied cap from tightening when a second sample
            (taken under an active limit) reports a lower value than the
            original peak.  Kept separate from ``apps`` on purpose: its
            lifetime outlives an individual LimitedApp entry (a manual
            restore removes the entry but intentionally keeps the peak).
      * is_limited_app_dominant — True when the current top process is
            one we already limited; the pressure loop reads this so it
            doesn't count its own throttled traffic as fresh pressure.
      * lock — guards every mutation of ``apps`` so the reaper thread and
            the REST manual limit/restore calls never race.
    """

    def __init__(self):
        self.apps: "OrderedDict[str, LimitedApp]" = OrderedDict()
        self.manual_limit_baseline: Dict[str, dict] = {}
        self.is_limited_app_dominant: bool = False
        self.lock = threading.RLock()

    # --- Query helpers (preserve the ordering semantics the callers rely on) ---
    def first_auto(self) -> "Optional[tuple[str, LimitedApp]]":
        """Return the oldest auto-limited (key, LimitedApp), or None.

        Mirrors the previous ``next(iter(auto_limited_apps.items()))``
        FIFO-head behaviour, now filtered by source over the unified dict.
        """
        for key, app in self.apps.items():
            if app.source == "auto":
                return key, app
        return None

    def pop_last_auto(self) -> "Optional[tuple[str, LimitedApp]]":
        """Pop and return the most-recently-inserted auto-limited entry.

        Mirrors the previous ``auto_limited_apps.popitem()`` (LIFO tail)
        used by the combined-policy full-restore path.
        """
        for key in reversed(self.apps):
            if self.apps[key].source == "auto":
                return key, self.apps.pop(key)
        return None

    def by_public_id(self, public_app_id: str, source: Optional[str] = None) -> "Optional[tuple[str, LimitedApp]]":
        """Find the (key, LimitedApp) whose public_app_id matches, or None.

        When *source* is given, only entries of that source ("auto"/"manual")
        are considered.  The manual restore path passes ``source="manual"`` so
        a user-initiated restore can never pull an auto-limited app out of the
        pressure-driven staged-recovery flow.
        """
        for key, app in self.apps.items():
            if app.public_app_id == public_app_id and (source is None or app.source == source):
                return key, app
        return None


@dataclass
class _MonitorLoopState:
    """Per-loop runtime state for ``DynamicBalancer._run_monitor_resource_loop``.

    Shared across the policy-specific tick methods so they can read and
    mutate the same loop variables.
    """
    default_idle_check_interval: float
    idle_check_interval: float
    last_check_time: float = 0.0
    last_reap_time: float = 0.0
    last_network_sample_time: float = 0.0
    network_sample_interval: float = 5.0          # network sampling interval (seconds)
    top_consume_apps: list = None
    reach_threshold: bool = False                 # some apps may have negligible resource usage; skip limiting them
    restore_pending: bool = False                 # True when there are apps waiting to be restored
    pressure_start_time: Optional[float] = None   # timestamp when pressure entered medium/low
    current_pressure: Optional[str] = None        # current pressure level; used to detect stability
    disk_io_not_stressed_start_time: Optional[float] = None  # timestamp when disk IO pressure was relieved
    sustained_critical_iters: int = 0
    prev_pressure: Optional[str] = None
    current_time: float = 0.0

    # Stability thresholds, kept on the state object so the tick methods
    # can reach them directly.
    STABLE_PERIOD: int = 1800                     # 30-minute stability period (seconds)
    STABLE_DISK_IO_PERIOD: int = 300              # 5-minute disk IO stability period (seconds)

    def __post_init__(self):
        if self.top_consume_apps is None:
            self.top_consume_apps = []

    def reset(self) -> None:
        """Clear transient state when the loop bails out of a tick."""
        self.top_consume_apps = []
        self.idle_check_interval = self.default_idle_check_interval
        self.pressure_start_time = None


class TopConsumerPrefetcher:
    """Background-warmed cache of the top resource-consuming apps.

    ``resource_monitor.get_top_resource_consumers()`` is a multi-second
    CPU+IO+GPU sampling pipeline; running it inline at the moment pressure
    hits ``critical`` would delay throttling by that same duration. This
    class warms the answer asynchronously so the eventual critical-path
    lookup returns immediately.

    Pure cache, no autonomous behavior:
      * Never schedules its own work — every fetch is triggered by an
        explicit ``start(reason)`` call from the caller.
      * Never inspects ``passive_resource_control`` — the caller is
        responsible for skipping ``start()`` when auto-limit is off, so
        that the multi-second sampling never runs without a consumer.

    Allowed triggers from the pressure loop (each gated by the caller):
      1. Rising-edge into ``high``       — ``reason="entering_high"``
      2. Sustained-critical recheck      — ``reason="sustained_critical_recheck"``
      3. Critical-state listener entry   — ``reason="critical_listener"``

    Debounce (NOT a validity TTL): ``CACHE_TTL`` only suppresses repeat
    ``start()`` calls fired within seconds of one another (e.g. rising-edge
    followed by listener). ``resolve_for_critical()`` will happily return
    cached data of any age — refresh is event-driven, not time-driven.

    Cold-start fallback: ``resolve_for_critical()`` first returns cached
    data; if empty, waits up to ``CRITICAL_WAIT`` for an in-flight
    prefetch; only then falls back to a synchronous fetch (paying the
    full sampling cost), so the first critical event after boot never
    proceeds without top-consumer info.

    Thread-safety: ``_lock`` guards the cache dict; ``_inflight`` is a
    one-shot gate that prevents fetch storms when multiple triggers race.
    """

    CACHE_TTL = 5.0                       # rising-edge / listener debounce window (s)
    CRITICAL_WAIT = 0.35                  # max wait for an in-flight prefetch on cold-start (s)
    SUSTAINED_CRITICAL_REFRESH_ITERS = 5  # iters of sustained critical before background recheck

    def __init__(self, fetch_top_consumers):
        """
        :param fetch_top_consumers: callable returning
            ``(apps, reach_threshold)``; usually
            ``resource_monitor.get_top_resource_consumers``.
        """
        self._fetch = fetch_top_consumers
        self._cache = {"apps": [], "reach_threshold": False, "fetched_at": 0.0}
        self._lock = threading.Lock()
        self._inflight = threading.Event()

    def start(self, reason: str) -> None:
        """Kick off a background prefetch. No-op when one is already in
        flight or when the cache was refreshed within ``CACHE_TTL``
        seconds (back-to-back trigger debounce).

        Caller is responsible for ensuring auto-limit is enabled before
        invoking this — the cache has no other consumer.
        """
        if self._inflight.is_set():
            logger.debug(f"Top-consumer prefetch skipped ({reason}): fetch already in flight")
            return
        with self._lock:
            age = time.time() - self._cache["fetched_at"]
            if self._cache["apps"] and age < self.CACHE_TTL:
                logger.debug(f"Top-consumer prefetch skipped ({reason}): cache fresh, age={age:.2f}s")
                return
        self._inflight.set()
        t0 = time.time()
        logger.debug(f"Top-consumer prefetch started ({reason})")

        def _worker():
            try:
                apps, threshold = self._fetch()
                with self._lock:
                    self._cache["apps"] = list(apps or [])
                    self._cache["reach_threshold"] = bool(threshold)
                    self._cache["fetched_at"] = time.time()
                logger.debug(
                    f"Top-consumer prefetch completed ({reason}): apps={len(apps)}, "
                    f"reach_threshold={threshold}, took={time.time() - t0:.2f}s"
                )
            except Exception as e:
                logger.warning(f"Top-consumer prefetch failed ({reason}): {e}")
            finally:
                self._inflight.clear()

        threading.Thread(target=_worker, daemon=True).start()

    def resolve_for_critical(self):
        """Return ``(apps, reach_threshold)`` for the critical-path lookup.

        Order of operations:
          1. Return cached data immediately when present (any age).
          2. Otherwise wait up to ``CRITICAL_WAIT`` for an in-flight
             prefetch and return its result.
          3. As a last resort, run a synchronous fetch — pays the full
             multi-second sampling cost; only reached on cold-start
             before any trigger has fired.
        """
        with self._lock:
            apps = list(self._cache["apps"])
            threshold = bool(self._cache["reach_threshold"])
            age = time.time() - self._cache["fetched_at"]
        if apps:
            logger.debug(f"Critical resolve: using cached top (age={age:.2f}s, apps={len(apps)})")
            return apps, threshold

        if self._inflight.is_set():
            logger.debug(f"Critical resolve: waiting up to {self.CRITICAL_WAIT}s for in-flight prefetch")
            self._inflight.wait(self.CRITICAL_WAIT)
            with self._lock:
                apps = list(self._cache["apps"])
                threshold = bool(self._cache["reach_threshold"])
            if apps:
                logger.debug(f"Critical resolve: got cache after wait (apps={len(apps)})")
                return apps, threshold

        logger.debug("Critical resolve: cache empty, falling back to synchronous fetch")
        return self._fetch()


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
        self.bpf_monitor = AppIntercept("controller/bpf_event.c")
        self.config = b_config
        self.control_manager = self.bpf_monitor.control_manager
        self.resource_monitor = self.control_manager.res
        self.io_ctl = IOController()

        self.known_pids = set()

        self.is_running = False
        self.app_detect_queue = JoinableQueue(1000000)
        self.app_priority_queue = MaxPriorityQueue()
        # Runtime state for every app currently under a resource limit.
        # See LimitRegistry / LimitedApp for the full field list.
        self.all_limits = LimitRegistry()

        # Background-warmed top-consumer cache. Pure cache; callers must
        # gate ``start()`` on passive_resource_control being enabled —
        # the fetch is a multi-second CPU+IO+GPU sampling pipeline.
        self.top_prefetcher = TopConsumerPrefetcher(
            self.resource_monitor.get_top_resource_consumers
        )

        self.network_controller = NetworkController()

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
        """Main pressure-driven decision loop.

        Each iteration:
          1. Samples peak pressure (and disk-IO stress, in separated policy).
          2. Warms the top-consumer cache on rising edges / sustained
             critical, gated by ``passive_resource_control``.
          3. Dispatches to the policy-specific tick method.
          4. Runs the network tick.

        Decision logic lives in ``_tick_separated_policy`` /
        ``_tick_combined_policy``; this method only orchestrates.
        """
        logger.info("Monitor resource service started")
        state = self._make_monitor_loop_state()
        policy = self.config.limit_policy['policy']

        self.control_manager.register_critical_state_listener(self._on_critical_state_changed)

        while self.is_running:
            try:
                state.current_time = time.time()

                _prc = self.config.passive_resource_control or {}
                passive_enabled = bool(_prc.get('enabled', True))

                if not self.app_priority_queue.empty() or (state.current_time - state.last_check_time) >= state.idle_check_interval:
                    # Use consume_peak_pressure_level() instead of get_current_pressure_level()
                    # so that transient "critical" spikes that occurred while the
                    # idle_check_interval gate was closed are never silently dropped.
                    if policy == "separated":
                        pressure, _, is_disk_io_stressed = self.control_manager.consume_peak_pressure_level()
                    else:  # policy == "combined"
                        pressure, *_ = self.control_manager.consume_peak_pressure_level()
                        is_disk_io_stressed = False

                    state.last_check_time = state.current_time
                    # Top-consumer prefetch / recheck only exist to warm the cache for the
                    # auto-limit path.  When passive control is off we are not going to
                    # apply any auto-limit, so skip the multi-second sampling pipeline.
                    self._maybe_trigger_prefetch(state, pressure, passive_enabled)

                    if policy == "separated":
                        self._tick_separated_policy(state, pressure, is_disk_io_stressed, passive_enabled)
                    elif policy == "combined":
                        self._tick_combined_policy(state, pressure, passive_enabled)
                    state.prev_pressure = pressure
                self._run_network_tick(state)

                # Reaper: restore limits for apps that have since closed. Runs
                # on its own short cadence (limit_reap_interval), independent of
                # the idle_check_interval gate above, so a closed app's stale
                # limit is lifted within a couple of seconds.
                reap_interval = float(getattr(self.config, "limit_reap_interval", 2))
                if state.current_time - state.last_reap_time >= reap_interval:
                    state.last_reap_time = state.current_time
                    self._reap_closed_apps()

                time.sleep(1)
            except Exception as e:
                logger.error(f"Error in monitor loop: {str(e)}", exc_info=True)
                state.reset()
                time.sleep(1)

        logger.info("Monitor resource service stopped")

    def _make_monitor_loop_state(self) -> "_MonitorLoopState":
        """Build the per-loop state object, clamping idle_check_interval
        to the [min, max] window and emitting a warning when the config
        value gets clamped."""
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
        return _MonitorLoopState(
            default_idle_check_interval=default_idle_check_interval,
            idle_check_interval=default_idle_check_interval,
        )

    def _on_critical_state_changed(self, is_critical: bool) -> None:
        """Critical-state listener — backstops the rising-edge prefetch
        when pressure jumps directly into critical from below the ``high``
        band. Gates on ``passive_resource_control`` so the multi-second
        top-consumer sampling is only paid when auto-limit will use it."""
        if not is_critical:
            return
        prc = self.config.passive_resource_control or {}
        if not bool(prc.get('enabled', True)):
            logger.debug("Critical-state listener fired but passive control disabled: skipping prefetch")
            return
        logger.debug("Critical-state listener fired: triggering top-consumer prefetch")
        self.top_prefetcher.start("critical_listener")

    def _maybe_trigger_prefetch(self, state: "_MonitorLoopState", pressure: str, passive_enabled: bool) -> None:
        """Edge-trigger and sustained-critical recheck for the
        top-consumer prefetch. No-op when passive_resource_control is
        disabled (the multi-second sampling has no consumer in that case).
        """
        if not passive_enabled:
            state.sustained_critical_iters = 0
            return

        # Edge trigger: prefetch whenever pressure enters the high band from
        # any other state (low/medium below, critical above). This is the
        # core mechanism — by the time we reach critical the cache is warm.
        # Sustained high stays cached. The critical-state listener is a
        # backstop for non-high→critical direct jumps.
        if pressure == "high" and state.prev_pressure != "high":
            logger.debug(
                f"Pressure edge {state.prev_pressure}→high: triggering top-consumer prefetch"
            )
            self.top_prefetcher.start("entering_high")

        # Sustained-critical recheck: if critical persists for N iters, the
        # original top1 has had ample time to settle under its limit. Refresh
        # top in background to catch a new dominant app that may have taken
        # over. Counter resets whenever pressure drops out of critical.
        if pressure == "critical":
            state.sustained_critical_iters += 1
            if state.sustained_critical_iters >= TopConsumerPrefetcher.SUSTAINED_CRITICAL_REFRESH_ITERS:
                logger.debug(
                    f"Sustained critical for {state.sustained_critical_iters} iters: "
                    f"triggering background top-consumer recheck"
                )
                self.top_prefetcher.start("sustained_critical_recheck")
                state.sustained_critical_iters = 0
        else:
            state.sustained_critical_iters = 0

    def _drain_pending_app_queue(self, state: "_MonitorLoopState") -> None:
        """Pop one pending app off ``app_priority_queue`` and resume it:
        emit SIGCONT, flip DB status to "running", broadcast the SSE
        callback, then reset loop state.
        """
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
        state.reset()

    def _update_dominant_flag_from_top(self, state: "_MonitorLoopState") -> None:
        """Walk the prefetched top-consumer list and decide whether any
        already-limited (and not yet partially-restored) app is the
        current dominant resource consumer. Sets
        ``self.all_limits.is_limited_app_dominant`` and pushes the flag down
        into the control manager so PSI baselines compensate correctly.
        """
        for app_info in state.top_consume_apps:
            # Match by cgroup membership, not just the top-consumer id: a
            # controlled multi-cgroup app is keyed in the registry by its
            # resolved primary cgroup basename, which need not equal this
            # sample's ``app.id`` or its surfaced cgroup.
            current_app_id = (app_info.get('app') or {}).get('id')
            top_cgroups = set()
            if app_info.get('cgroup'):
                top_cgroups.add(os.path.basename(app_info['cgroup']))
            for extra in app_info.get('extra_cgroups', []) or []:
                top_cgroups.add(os.path.basename(extra))

            entry = self.all_limits.apps.get(current_app_id)
            if entry is None:
                for cand_key, cand in self.all_limits.apps.items():
                    if cand.source == "auto" and (
                        cand_key in top_cgroups or top_cgroups & set(cand.cgroups)
                    ):
                        entry = cand
                        break

            if entry is not None and entry.source == "auto":
                self.all_limits.is_limited_app_dominant = (entry.state != "partially_restored")
                break
            else:
                self.all_limits.is_limited_app_dominant = False

        logger.debug(f"Balance- was the process limited before? {self.all_limits.is_limited_app_dominant}")
        self.control_manager.set_limited_app_dominant(self.all_limits.is_limited_app_dominant)

    def _tick_separated_policy(
        self,
        state: "_MonitorLoopState",
        pressure: str,
        is_disk_io_stressed: bool,
        passive_enabled: bool,
    ) -> None:
        """One iteration of the separated-policy state machine.

        Three mutually-exclusive cases:
          * critical pressure or disk-IO stress — apply or refresh limits
          * pending app launches with no critical pressure — drain queue
          * medium/low pressure with limited apps — staged restore
        """
        if passive_enabled and (pressure == "critical" or is_disk_io_stressed):
            state.restore_pending = False

            if not is_disk_io_stressed:
                state.pressure_start_time = None
                if not state.top_consume_apps:
                    state.top_consume_apps, state.reach_threshold = self.top_prefetcher.resolve_for_critical()
            else:
                state.disk_io_not_stressed_start_time = None
                state.top_consume_apps = self.resource_monitor.get_top_disk_io_consumers()
                state.reach_threshold = True  # IO pressure always counts as threshold-crossing
            if state.top_consume_apps:
                self._update_dominant_flag_from_top(state)

                if not is_disk_io_stressed:
                    should_adjust, is_controlled, app_id, limit_rates = self._handle_critical_pressure(
                        state.top_consume_apps, state.reach_threshold)
                else:
                    should_adjust, is_controlled, app_id, limit_rates = self._handle_disk_io_stressed(
                        state.top_consume_apps)

                if not self.all_limits.is_limited_app_dominant and state.reach_threshold and should_adjust and app_id:
                    self._apply_resource_limits(
                        state.top_consume_apps[0],
                        app_id,
                        limit_rates,
                        is_controlled,
                        is_disk_io_stressed=is_disk_io_stressed
                    )

                state.top_consume_apps.pop(0)
            else:
                state.reset()

        elif not self.app_priority_queue.empty() and pressure != "critical" and not is_disk_io_stressed:
            self._drain_pending_app_queue(state)
        else:
            self._tick_separated_restore(state, pressure, is_disk_io_stressed)

    def _tick_separated_restore(
        self,
        state: "_MonitorLoopState",
        pressure: str,
        is_disk_io_stressed: bool,
    ) -> None:
        """Staged restore arm of the separated-policy tick.

        Tracks two independent stability timers (pressure and disk-IO) and
        runs partial / full restore on the head of ``auto_limited_apps``
        once the relevant timer crosses ``STABLE_PERIOD`` /
        ``STABLE_DISK_IO_PERIOD``.
        """
        if not (self.all_limits.first_auto() is not None and not state.restore_pending):
            return

        should_check_pressure = (pressure in ("medium", "low") and
                                 any(app.limit_parts.get('cpu_mem_limited', False) for app in
                                     self.all_limits.apps.values() if app.source == "auto"))
        should_check_io = (not is_disk_io_stressed and
                           any(app.limit_parts.get('io_limited', False) for app in
                               self.all_limits.apps.values() if app.source == "auto"))
        if not (should_check_pressure or should_check_io):
            state.reset()
            return

        logger.info(f"pressure_start_time: {state.pressure_start_time}, "
                    f"current_pressure: {state.current_pressure}, pressure: {pressure}")
        if should_check_pressure:
            if (state.pressure_start_time is None) or (state.current_pressure != pressure):
                state.pressure_start_time = state.current_time
                state.current_pressure = pressure
                logger.info(
                    f"Pressure level changed to {pressure}. "
                    f"Will restore resources after {state.STABLE_PERIOD} sec if it remains stable.")

        if should_check_io:
            if state.disk_io_not_stressed_start_time is None:
                state.disk_io_not_stressed_start_time = state.current_time
                logger.info(
                    f"Disk IO stress resolved. Will consider for restoration after {state.STABLE_DISK_IO_PERIOD} sec if it remains stable.")

        pressure_stable = (should_check_pressure and
                           (state.current_time - state.pressure_start_time >= state.STABLE_PERIOD))
        io_stable = (should_check_io and
                     (state.current_time - state.disk_io_not_stressed_start_time >= state.STABLE_DISK_IO_PERIOD))
        io_double_stable = (should_check_io and
                     (state.current_time - state.disk_io_not_stressed_start_time >= state.STABLE_DISK_IO_PERIOD * 2))

        logger.info(f"pressure_stable: {pressure_stable}, io_stable: {io_stable}, io_double_stable: {io_double_stable}")

        if pressure_stable and pressure == "medium":
            state.restore_pending = True
            app_id, entry = self.all_limits.first_auto()
            app_name, limit_rates, limit_parts = entry.app_name, entry.limit_rates, entry.limit_parts
            if entry.state != "partially_restored":
                logger.info(
                    f"Pressure remained at 'medium' for {state.STABLE_PERIOD} sec. "
                    f"Partially restoring app {app_id}.")
                if self.restore_resources(app_id, app_name, limit_rates, limit_parts, "partial"):
                    entry.state = "partially_restored"
                else:
                    logger.warning(f"Partial restore failed for {app_name}")
                self.all_limits.apps.move_to_end(app_id)
        elif io_stable and not io_double_stable:
            state.restore_pending = True
            app_id, entry = self.all_limits.first_auto()
            app_name, limit_rates, limit_parts = entry.app_name, entry.limit_rates, entry.limit_parts
            if entry.state != "partially_restored":
                logger.info(f"Disk IO stress resolved. Partially restoring app {app_id}.")
                if self.restore_resources(app_id, app_name, limit_rates, limit_parts, "partial"):
                    entry.state = "partially_restored"
                else:
                    logger.warning(f"Partial restore failed for {app_name}")
                self.all_limits.apps.move_to_end(app_id)
        elif (pressure_stable and pressure == "low") or io_double_stable:
            state.restore_pending = True
            app_id, entry = self.all_limits.first_auto()
            app_name, limit_rates, limit_parts = entry.app_name, entry.limit_rates, entry.limit_parts

            success = self.restore_resources(app_id, app_name, limit_rates, limit_parts,
                                             "full")
            if success:
                updated_limits = entry.limit_parts
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
                    self.all_limits.apps.pop(app_id, None)
                    logger.info(f"Fully restored app {app_id}, removed from limited apps")

                    if io_double_stable:
                        state.disk_io_not_stressed_start_time = None
                        logger.debug("Reset IO stress timer after full restoration")
                else:
                    self.all_limits.apps.move_to_end(app_id)
                    logger.info(f"Partial restore for app {app_id}, moved to end of queue")
            else:
                logger.error(f"Failed to restore resources for app {app_id}")
                self.all_limits.apps.move_to_end(app_id)
        state.restore_pending = False

    def _tick_combined_policy(
        self,
        state: "_MonitorLoopState",
        pressure: str,
        passive_enabled: bool,
    ) -> None:
        """One iteration of the combined-policy state machine.

        Combined policy treats CPU/memory and disk-IO as a single pressure
        signal, so there's no parallel disk-IO branch and no double-stable
        timer. Three mutually-exclusive cases:
          * critical pressure         — apply or refresh limits (CPU/Mem + IO together)
          * pending app launches      — drain queue when pressure isn't critical
          * medium/low pressure       — staged restore on a single timer
        """
        if passive_enabled and pressure == "critical":
            state.pressure_start_time = None
            state.restore_pending = False
            if not state.top_consume_apps:
                state.top_consume_apps, state.reach_threshold = self.top_prefetcher.resolve_for_critical()

            if state.top_consume_apps:
                self._update_dominant_flag_from_top(state)
                should_adjust, is_controlled, app_id, limit_rates = self._handle_critical_pressure(
                    state.top_consume_apps, state.reach_threshold)

                if not self.all_limits.is_limited_app_dominant and state.reach_threshold and should_adjust and app_id:
                    self._apply_combined_critical_limits(
                        state.top_consume_apps[0], app_id, limit_rates, is_controlled
                    )

                state.top_consume_apps.pop(0)
            else:
                state.reset()
        elif not self.app_priority_queue.empty() and pressure != "critical":
            self._drain_pending_app_queue(state)
        else:
            self._tick_combined_restore(state, pressure)

    def _apply_combined_critical_limits(
        self,
        target: dict,
        app_id: str,
        limit_rates: dict,
        is_controlled: bool,
    ) -> None:
        """Apply combined-policy CPU/Memory + disk-IO limits to the
        dominant top consumer.
        """
        app_name = target.get('process', {}).get('name') or ''
        total_mem = self.resource_monitor.get_total_memory()
        logger.info(f"Adjusting resources for app: {app_id}")
        extra_cgroup_ids = target.get('extra_cgroups', [])
        per_cg_mem_rss = target.get('per_cgroup_mem_rss', {})
        per_cg_cpu = target.get('per_cgroup_cpu', {})

        resource_limited = False
        io_limited = False

        cpu_rate = int(100 * limit_rates["cpu_rate"]) if limit_rates.get("cpu_rate") else None
        mem_rate = int(total_mem * limit_rates["mem_rate"]) if limit_rates.get(
            "mem_rate") else None

        if (cpu_rate is not None or mem_rate is not None) and self.is_running:
            if extra_cgroup_ids:
                all_ids = [app_id] + extra_cgroup_ids
                mem_dist = _split_proportionally(mem_rate, all_ids, per_cg_mem_rss)
                cpu_dist = _split_proportionally(cpu_rate, all_ids, per_cg_cpu)
                auto_limit = self.control_manager.adjust_resources(
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
                    ok = self.control_manager.adjust_resources(
                        extra_id, "critical",
                        cpu_quota=cpu_dist.get(extra_id, cpu_rate),
                        mem_high=mem_dist.get(extra_id, mem_rate),
                    )
                    logger.info(
                        f"{'Successfully limited' if ok else 'Failed to limit'} "
                        f"CPU/Memory for extra cgroup {extra_id}"
                    )
            else:
                auto_limit = self.control_manager.adjust_resources(
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

        io_limits = limit_rates.get("disk_io_rate", {})
        if io_limits and self.is_running:
            limits = {
                "default": {
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

        if resource_limited or io_limited:
            self.all_limits.apps[app_id] = LimitedApp(
                public_app_id=app_id,
                app_name=app_name,
                source="auto",
                limit_rates=limit_rates,
                limit_parts={'cpu_mem_limited': resource_limited, 'io_limited': io_limited},
                state=None,
                cgroups=[app_id] + list(extra_cgroup_ids),
                pids=set(target.get('pids') or []),
            )

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

    def _tick_combined_restore(self, state: "_MonitorLoopState", pressure: str) -> None:
        """Staged restore arm of the combined-policy tick.

        Single ``STABLE_PERIOD`` timer drives both partial (at medium) and
        full (at low) restore on the head of ``auto_limited_apps``.
        """
        if not (self.all_limits.first_auto() is not None and not state.restore_pending):
            return
        if pressure not in ("medium", "low"):
            state.reset()
            return

        if (state.pressure_start_time is None) or (state.current_pressure != pressure):
            state.pressure_start_time = state.current_time
            state.current_pressure = pressure
            logger.info(
                f"Pressure level changed to {pressure}. "
                f"Will restore resources after {state.STABLE_PERIOD} sec if it remains stable."
            )
            return

        if state.current_time - state.pressure_start_time < state.STABLE_PERIOD:
            return

        state.restore_pending = True

        if pressure == "medium":
            app_id, entry = self.all_limits.first_auto()
            app_name, limit_rates, limit_parts = entry.app_name, entry.limit_rates, entry.limit_parts
            if entry.state != "partially_restored":
                total_mem = self.resource_monitor.get_total_memory()
                logger.info(
                    f"Pressure remained at 'medium' for {state.STABLE_PERIOD} sec. "
                    f"Partially restoring app {app_id} (twice the rate of limited resources)."
                )
                extra_ids = entry.cgroups[1:]
                restore_success = True

                if limit_parts.get('cpu_mem_limited', False):
                    cpu_restore = int(100 * limit_rates[
                        "cpu_rate"] * 2) if "cpu_rate" in limit_rates else None
                    mem_restore = int(total_mem * limit_rates[
                        "mem_rate"] * 2) if "mem_rate" in limit_rates else None

                    if (cpu_restore is not None or mem_restore is not None) and self.is_running:
                        cpu_mem_restored = self.control_manager.adjust_resources(
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
                            self.control_manager.adjust_resources(
                                extra_id, "medium",
                                cpu_quota=cpu_restore,
                                mem_high=mem_restore,
                                is_restore=False,
                            )

                if (limit_parts.get('io_limited', False) and "disk_io_rate" in limit_rates) and self.is_running:
                    io_restored = True
                    io_limits = limit_rates["disk_io_rate"]

                    limits = {
                        "default": {
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

                if restore_success:
                    entry.state = "partially_restored"
                else:
                    logger.warning(f"Partial restore failed for {app_name}")

                self.all_limits.apps.move_to_end(app_id)
        else:  # pressure == "low"
            app_id, entry = self.all_limits.pop_last_auto()
            app_name, limit_parts = entry.app_name, entry.limit_parts
            logger.info(
                f"Pressure remained at 'low' for {state.STABLE_PERIOD} sec. "
                f"Fully restoring app {app_id} (100% resources)."
            )

            restore_success = True
            extra_ids = entry.cgroups[1:]

            if limit_parts.get('cpu_mem_limited', False) and self.is_running:
                if not self.control_manager.adjust_resources(app_id, "low"):
                    logger.error(f"Failed to fully restore CPU/Memory for {app_name}")
                    restore_success = False
                for extra_id in extra_ids:
                    self.control_manager.adjust_resources(extra_id, "low")

            if limit_parts.get('io_limited', False) and self.is_running:
                io_restored = True

                if not self.io_ctl.restore_disk_io_throttle(app_id):
                    logger.error(f"Failed to remove IO limits for {app_name}")
                    io_restored = False
                for extra_id in extra_ids:
                    self.io_ctl.restore_disk_io_throttle(extra_id)

                if not io_restored:
                    restore_success = False

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

        state.restore_pending = False
        state.reset()  # reset timer and current pressure state

    def _run_network_tick(self, state: "_MonitorLoopState") -> None:
        """Network sampling + handling. Runs every iteration (regardless
        of ``idle_check_interval``) so traffic pressure stays current.
        """
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
        if state.current_time - state.last_network_sample_time >= state.network_sample_interval:
            state.last_network_sample_time = state.current_time
            network_data = self.network_controller.network.get_current_pressure()
            tx_pressure, rx_pressure, *_ = self.control_manager.update_network_pressure_level(network_data)
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

    def _run_handle_loop(self):
        logger.info("Resource handle service is wait for processing")
        while self.is_running:
            try:
                coming_app = self.bpf_monitor.app_pending_queue.get(block=True, timeout=5)
                logger.info(f"_run_handle_loop: Processing app {coming_app}")

                priority = app_utils.get_app_priority(app_name=coming_app["app_name"])
                logger.info(f"_run_handle_loop: App {coming_app['app_name']} priority is {priority}")

                priority_num = app_utils.get_priority_value(priority)
                logger.debug(f"_run_handle_loop: priority value is {priority_num}")
                self.app_priority_queue.put((coming_app, priority_num))
                logger.info(f"_run_handle_loop: Resource insufficient, {coming_app} app added to pending queue")

            except Exception:
                time.sleep(2)
        logger.debug("Exiting _run_handle_loop")

    def _run_app_intercept_loop(self):
        logger.info("Resource app intercept service is wait for processing")

        self.bpf_monitor.bpf["events"].open_perf_buffer(self.bpf_monitor.print_event)

        monitor_apps = app_utils.get_controlled_apps()

        if monitor_apps:
            monitored_names = [app["app_name"] for app in monitor_apps if app.get("app_name") and app["app_name"].strip()]
            self.bpf_monitor.add_to_monitorlist(monitored_names)
            logger.info(f"Monitoring execve() for: {', '.join(monitored_names)}")

            logger.debug(f"monitor_apps: {monitor_apps}")
            for app in monitor_apps:
                app_utils.adjust_oom_priority(app["app_id"], app["app_name"], app["priority"], app.get("cmdline", ""))
        else:
            logger.warning("No controlled apps to monitor")

        while self.is_running:
            try:
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
        app_name = target_app.get('process', {}).get('name') or ''
        total_mem = self.resource_monitor.get_total_memory()
        logger.info(f"Adjusting resources for app: {app_id}")

        extra_cgroup_ids = target_app.get('extra_cgroups', [])
        per_cg_mem_rss = target_app.get('per_cgroup_mem_rss', {})
        per_cg_cpu = target_app.get('per_cgroup_cpu', {})

        resource_limited = False
        io_limited = False

        if not is_disk_io_stressed:
            cpu_rate = int(100 * limit_rates["cpu_rate"]) if limit_rates.get("cpu_rate") else None
            mem_rate = int(total_mem * limit_rates["mem_rate"]) if limit_rates.get("mem_rate") else None

            if (cpu_rate is not None or mem_rate is not None) and self.is_running:
                if extra_cgroup_ids:
                    all_ids = [app_id] + extra_cgroup_ids
                    mem_dist = _split_proportionally(mem_rate, all_ids, per_cg_mem_rss)
                    cpu_dist = _split_proportionally(cpu_rate, all_ids, per_cg_cpu)
                    primary_ok = self.control_manager.adjust_resources(
                        app_id, "critical",
                        cpu_quota=cpu_dist.get(app_id, cpu_rate),
                        mem_high=mem_dist.get(app_id, mem_rate),
                    )
                    if primary_ok:
                        resource_limited = True
                        logger.info(f"Successfully limited CPU/Memory for {app_name} ({app_id})")
                    for extra_id in extra_cgroup_ids:
                        ok = self.control_manager.adjust_resources(
                            extra_id, "critical",
                            cpu_quota=cpu_dist.get(extra_id, cpu_rate),
                            mem_high=mem_dist.get(extra_id, mem_rate),
                        )
                        logger.info(
                            f"{'Successfully limited' if ok else 'Failed to limit'} "
                            f"CPU/Memory for extra cgroup {extra_id}"
                        )
                else:
                    auto_limit = self.control_manager.adjust_resources(
                        app_id,
                        "critical",
                        cpu_quota=cpu_rate,
                        mem_high=mem_rate,
                    )
                    if auto_limit:
                        resource_limited = True
                        logger.info(f"Successfully limited CPU/Memory for {app_name}")

        if is_disk_io_stressed and limit_rates.get("disk_io_rate"):
            io_limits = limit_rates.get("disk_io_rate", {})
            if io_limits and self.is_running:
                limits = {
                    "default": {
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

        if resource_limited or io_limited:
            self.all_limits.apps[app_id] = LimitedApp(
                public_app_id=app_id,
                app_name=app_name,
                source="auto",
                limit_rates=limit_rates,
                limit_parts={'cpu_mem_limited': resource_limited, 'io_limited': io_limited},
                state=None,  # None indicates fully limited
                cgroups=[app_id] + list(extra_cgroup_ids),
                pids=set(target_app.get('pids') or []),
            )

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
        restore_success = True
        entry = self.all_limits.apps.get(app_id)
        extra_ids = entry.cgroups[1:] if entry else []

        if self.is_running:
            if limit_parts.get('cpu_mem_limited', False):
                if restore_type == "partial":
                    cpu_restore = int(100 * limit_rates["cpu_rate"] * 2) if "cpu_rate" in limit_rates else None
                    mem_restore = int(self.resource_monitor.get_total_memory() * limit_rates[
                        "mem_rate"] * 2) if "mem_rate" in limit_rates else None
                    if not self.control_manager.adjust_resources(
                        app_id, "medium", cpu_quota=cpu_restore, mem_high=mem_restore, is_restore=False
                    ):
                        logger.error(f"Failed to partially restore CPU/Memory for {app_name}")
                        restore_success = False
                    for extra_id in extra_ids:
                        self.control_manager.adjust_resources(
                            extra_id, "medium", cpu_quota=cpu_restore, mem_high=mem_restore, is_restore=False
                        )
                else:  # full restore
                    cpu_mem_restored = self.control_manager.adjust_resources(app_id, "low")
                    if not cpu_mem_restored:
                        logger.error(f"Failed to fully restore CPU/Memory for {app_name}")
                        restore_success = False
                    elif entry is not None:
                        entry.limit_parts = {
                            'cpu_mem_limited': False,
                            'io_limited': limit_parts['io_limited'],
                        }
                    for extra_id in extra_ids:
                        self.control_manager.adjust_resources(extra_id, "low")
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
                    elif entry is not None:
                        entry.limit_parts = {
                            'cpu_mem_limited': limit_parts['cpu_mem_limited'],
                            'io_limited': False,
                        }
                    for extra_id in extra_ids:
                        self.io_ctl.restore_disk_io_throttle(extra_id)

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

        app_id = app_info['app'].get('id') if app_info.get('app') else None
        app_name = (app_info.get('process', {}).get('name') or '').lower()

        is_controlled, controlled_data = app_utils.get_app_control_info(app_id, app_name)
        priority = controlled_data.get('priority') if controlled_data else None

        if not is_controlled:
            controlled_apps = app_utils.get_controlled_apps() or []
            for controlled_app in controlled_apps:
                running_pids = app_utils.get_app_processes(controlled_app['app_name'])
                logger.debug(f"Disk IO stressed - controlled app {controlled_app['app_name']} running PIDs: {running_pids}")
                if running_pids:
                    is_high_io, msg = app_utils.check_pids_disk_io_usage(running_pids, threshold_mb=100)

                    if is_high_io:
                        return True, False, app_id, self.get_limited_rates("undefined")
                    else:
                        logger.info(f"Disk IO stressed - No controlled app with high IO usage found.")
            return False, False, None, None

        elif priority != 'critical':
            critical_apps = app_utils.get_controlled_apps(priority="Critical") or []
            for critical_app in critical_apps:
                running_pids = app_utils.get_app_processes(critical_app['app_name'])
                if running_pids:
                    is_high_io = app_utils.check_pids_disk_io_usage([running_pids], threshold_mb=100)
                    if is_high_io:
                        return True, True, app_id, self.get_limited_rates(priority or "undefined")

        return False, False, None, None

    def _resolve_controlled_target(self, app_info: dict, controlled_data: dict) -> Optional[str]:
        """Resolve a controlled app's real cgroups and rewrite ``app_info`` so
        the auto-limit apply path fans out across all of them.

        The top-consumer sample keys an app by a single cgroup (or, for
        ``process_names`` apps, by a public app id that is not a systemd unit
        name), which makes multi-cgroup controlled apps either under-limited or
        not limited at all.  This resolves the controlled app's full cgroup set
        via :func:`app_utils.get_app_resource_usage` (the same source the manual
        path uses) and mutates ``app_info`` in place:

          * ``extra_cgroups``       -> the non-primary cgroup basenames
          * ``pids``                -> the app's live PIDs (for close-detection)
          * ``per_cgroup_mem_rss`` / ``per_cgroup_cpu`` -> basename-keyed weights
            used to split the limit proportionally.

        Returns the primary (lexicographically-first) cgroup basename to use as
        the limit key, or ``None`` if the cgroups could not be resolved (in
        which case the caller keeps the original top-consumer id).
        """
        public_id = controlled_data.get('app_id')
        name = controlled_data.get('app_name') or ''
        usage = app_utils.get_app_resource_usage(public_id, name) or {}
        cgroup_paths = usage.get('cgroup_paths') or (
            [usage['cgroup_path']] if usage.get('cgroup_path') else []
        )
        effective_ids = [os.path.basename(p) for p in cgroup_paths if p]
        if not effective_ids:
            logger.warning(
                f"Could not resolve cgroups for controlled app '{name}' "
                f"({public_id}); limiting the top-consumer cgroup only")
            return None

        primary = min(effective_ids)
        extras = [e for e in effective_ids if e != primary]

        app_info['extra_cgroups'] = extras
        if usage.get('pids'):
            app_info['pids'] = usage['pids']
        if usage.get('per_cgroup_mem'):
            app_info['per_cgroup_mem_rss'] = usage['per_cgroup_mem']
        if usage.get('per_cgroup_cpu_delta'):
            app_info['per_cgroup_cpu'] = usage['per_cgroup_cpu_delta']

        logger.info(
            f"Controlled app '{name}' resolved to cgroups {effective_ids}; "
            f"primary={primary}, extras={extras}")
        return primary

    def _handle_critical_pressure(self, top_consumers, reach_threshold):
        """Handle resource pressure (processes one app per invocation)."""
        if not top_consumers or not top_consumers[0]:
            return False, False, None, None

        self._critical_counter = getattr(self, '_critical_counter', 0)
        self._last_notification_time = getattr(self, '_last_notification_time', 0)

        app_info = top_consumers[0]
        app_id = app_info['app'].get('id') if app_info.get('app') else None
        app_name = (app_info.get('process', {}).get('name') or '').lower()

        is_controlled, controlled_data = app_utils.get_app_control_info(app_id, app_name)
        priority = controlled_data.get('priority') if controlled_data else None

        usage_data = self.resource_monitor.get_resource_usage()
        is_sys_busy = usage_data['cpu']['is_busy'] or usage_data['memory']['is_busy']

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

        if not is_controlled or priority != 'critical':
            self._critical_counter = 0
            # A controlled app may span several cgroups while the top-consumer
            # sample only surfaces one of them (and, for process_names apps,
            # reports a public app id that is not a valid systemd unit). Once
            # we know it is controlled, resolve the app's full cgroup set and
            # rewrite the target so the limit fans out to every cgroup — the
            # same treatment the manual limit path already applies.
            if is_controlled and controlled_data:
                resolved_id = self._resolve_controlled_target(app_info, controlled_data)
                if resolved_id:
                    app_id = resolved_id
            return True, is_controlled, app_id, self.get_limited_rates(priority or "undefined")

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

    def _restore_entry(self, entry: "LimitedApp", notify: bool) -> bool:
        """Fully restore one already-removed limited app's cgroups.

        Shared restore path for the shutdown sweep
        (:meth:`restore_all_limited_apps_resources`, ``notify=False``) and
        the reaper (:meth:`_reap_closed_apps`, ``notify=True``).  The caller
        must have already popped ``entry`` from ``self.all_limits.apps`` (and its
        ``manual_limit_baseline``) under the lock — this method only touches
        cgroups, never the registry, so it is safe to run outside the lock.

        When ``notify`` is True and the restore succeeds, emits the
        app-status "running" callback plus a "notify" callback so the UI can
        tell the user the app closed and its limit was lifted.
        """
        cgroups = entry.cgroups or [entry.public_app_id]
        key = cgroups[0]
        app_name, source = entry.app_name, entry.source
        restore_success = True
        logger.info(f"Restoring resources for {source} limited app: {key}, name: {app_name}")
        try:
            gone = 0
            for idx, cg in enumerate(cgroups):
                is_primary = (idx == 0)
                # A closed app's cgroup is often already removed; there is
                # nothing to restore, so skip it quietly instead of retrying
                # systemctl and logging errors.
                if not self._cgroup_exists(cg):
                    gone += 1
                    logger.debug(f"Cgroup {cg} already gone; nothing to restore")
                    continue
                if entry.limit_parts.get('cpu_mem_limited', False):
                    if not self.control_manager.adjust_resources(cg, "low") and is_primary:
                        logger.error(f"Failed to restore CPU/Memory for {source} limited app {cg}")
                        restore_success = False
                if entry.limit_parts.get('io_limited', False):
                    if not self.io_ctl.restore_disk_io_throttle(cg) and is_primary:
                        logger.error(f"Failed to remove IO limits for {source} limited app {cg}")
                        restore_success = False

            if gone == len(cgroups):
                logger.info(f"All cgroups for {source} limited app {key} already gone; limit already cleared")
            elif restore_success:
                logger.info(f"{source.capitalize()} limited app resources restoration completed")
        except Exception as e:
            logger.error(f"Failed to restore resources for app {key}: {str(e)}")
            restore_success = False

        if notify and restore_success:
            app_utils.update_app_status(entry.public_app_id, "running")
            app_utils.callback_manager.send_callback_notification({
                'app_id': entry.public_app_id,
                'app_name': app_name,
                'status': "running",
                'purpose': "app"
            }, False)
            # Tell the UI the app closed and we lifted its (now-stale) limit.
            app_utils.callback_manager.send_callback_notification({
                'app_id': entry.public_app_id,
                'app_name': app_name,
                'status': "app_closed_limit_restored",
                'purpose': "notify"
            }, False)

        return restore_success

    def restore_all_limited_apps_resources(self):
        """Restore all limited apps resources (called on shutdown)."""
        with self.all_limits.lock:
            if not self.all_limits.apps:
                logger.info("No limited apps to restore")
                return

            auto_n = sum(1 for a in self.all_limits.apps.values() if a.source == "auto")
            manual_n = sum(1 for a in self.all_limits.apps.values() if a.source == "manual")
            logger.info(
                f"Restoring resources for {auto_n} limited apps and "
                f"{manual_n} manual limited apps")

            for key in list(self.all_limits.apps):
                entry = self.all_limits.apps.pop(key, None)
                self.all_limits.manual_limit_baseline.pop(key, None)
                if entry is not None:
                    self._restore_entry(entry, notify=False)

        logger.info("All limited apps resources restoration completed")

    def _cgroup_exists(self, cgroup_id: str) -> bool:
        """Return True if a cgroup directory named *cgroup_id* still exists.

        Used before restoring a closed app: if the scope/service is already
        gone its limit died with it, so restoring is a no-op we skip to avoid
        noisy systemctl retries. On any lookup error assume it exists so the
        restore still proceeds (old behaviour).
        """
        mount = getattr(self.config, "cgroup_mount", "/sys/fs/cgroup")
        try:
            result = subprocess.run(
                ["find", mount, "-name", cgroup_id, "-type", "d"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5,
            )
            return bool(result.stdout.strip())
        except Exception:
            return True

    # Signals whose default action terminates the process.
    _FATAL_PENDING_SIGNALS = frozenset({2, 9, 15})  # SIGINT, SIGKILL, SIGTERM

    def _pid_gone_or_dying(self, pid: int) -> bool:
        """True if *pid* is dead, a zombie, or stuck in 'D' with a pending fatal
        signal. The last case is a task that was asked to die but cannot receive
        the signal because our own throttle is pinning it in uninterruptible
        sleep; counting it as gone lets the reaper restore and unblock it. A
        healthy busy app never carries a pending kill signal.
        """
        try:
            with open(f"/proc/{pid}/status") as f:
                content = f.read()
        except (FileNotFoundError, ProcessLookupError):
            return True
        except Exception:
            return False  # can't tell — treat as alive, don't restore

        fields = {}
        for line in content.splitlines():
            if line.startswith(("State:", "SigPnd:", "ShdPnd:")):
                key, _, value = line.partition(":")
                fields[key] = value.strip()

        state = (fields.get("State", "") or " ")[0]
        if state == 'Z':
            return True
        if state == 'D':
            pending = 0
            for key in ("ShdPnd", "SigPnd"):
                try:
                    pending |= int(fields.get(key, "0").split()[0], 16)
                except (ValueError, IndexError):
                    pass
            fatal_mask = 0
            for sig in self._FATAL_PENDING_SIGNALS:
                fatal_mask |= 1 << (sig - 1)
            if pending & fatal_mask:
                logger.info(f"PID {pid} stuck in 'D' with a pending fatal signal; treating as gone")
                return True
        return False

    def _is_app_closed(self, entry: "LimitedApp") -> bool:
        """Decide whether a limited app has closed, so its limit can be lifted.

        Detection is PID-based, not cgroup-emptiness-based, since an app may
        share its cgroup with other processes that keep it non-empty.

          * All snapshot PIDs gone or dying-but-pinned -> the app closed.
          * Multi-cgroup app where any limited cgroup no longer has a live
            snapshot PID -> the app is broken; lift the limit.

        Callers must have already filtered out entries with no PID snapshot.
        """
        alive = [pid for pid in entry.pids if not self._pid_gone_or_dying(pid)]
        if not alive:
            return True

        if len(entry.cgroups) > 1:
            live_cgroups = set()
            for pid in alive:
                cg = app_utils.get_cgroup_path_by_pid(pid)
                if cg:
                    live_cgroups.add(os.path.basename(cg))
            if set(entry.cgroups) - live_cgroups:
                return True

        return False

    def _reap_closed_apps(self) -> None:
        """One reaper pass: restore limits for any app that has closed.

        Runs in the monitor thread (so it never races the auto limit/restore
        logic) on a short, fixed cadence independent of
        ``monitor_idle_check_interval``.  Closed entries are popped under the
        lock — so a concurrent manual restore cannot double-act — and the
        actual cgroup restore + notifications happen outside the lock to keep
        the hold time short.
        """
        closed: list = []
        with self.all_limits.lock:
            for key in list(self.all_limits.apps):
                entry = self.all_limits.apps.get(key)
                if entry is None:
                    continue
                if not entry.pids:
                    logger.warning(
                        f"Reaper: no PID snapshot for limited app {key} "
                        f"({entry.app_name}); skipping close-check")
                    continue
                if self._is_app_closed(entry):
                    self.all_limits.apps.pop(key, None)
                    self.all_limits.manual_limit_baseline.pop(key, None)
                    closed.append(entry)

        for entry in closed:
            logger.info(
                f"Reaper: app '{entry.app_name}' ({entry.public_app_id}) closed; "
                f"restoring its {entry.source} limit")
            self._restore_entry(entry, notify=True)

    def cancel_relaunch_by_app_id(self, app_id: str) -> bool:
        """Remove queue items for the given app_id and terminate the associated process."""
        def condition(item):
            data, _ = item
            return data.get('app_id') == app_id

        removed_items = self.app_priority_queue.remove_if(condition)
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

        if not hasattr(self.config, 'limit_policy'):
            return result

        bounds = self._get_limit_rate_bounds(priority)
        overrides = limit_overrides or {}

        limit_policy_cfg = self.config.limit_policy or {}

        cpu_cfg = limit_policy_cfg.get('cpu', {})
        cpu_rates = cpu_cfg.get('rate', {})
        cpu_ovr = overrides.get("cpu", {}) if isinstance(overrides.get("cpu", {}), dict) else {}
        cpu_enabled = cpu_ovr.get("enabled", cpu_cfg.get('enabled', False))
        cpu_rate = cpu_ovr.get("rate", cpu_rates.get(priority))
        if cpu_enabled and cpu_rate is not None:
            result['cpu_rate'] = self._clamp_rate(cpu_rate, bounds["cpu"]["min"], bounds["cpu"]["max"])

        mem_cfg = limit_policy_cfg.get('memory', {})
        mem_rates = mem_cfg.get('rate', {})
        mem_ovr = overrides.get("memory", {}) if isinstance(overrides.get("memory", {}), dict) else {}
        mem_enabled = mem_ovr.get("enabled", mem_cfg.get('enabled', False))
        mem_rate = mem_ovr.get("rate", mem_rates.get(priority))
        if mem_enabled and mem_rate is not None:
            result['mem_rate'] = self._clamp_rate(mem_rate, bounds["memory"]["min"], bounds["memory"]["max"])

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

        usage = app_utils.get_app_resource_usage(app_id, app_name)
        if usage is None:
            logger.warning(f"No resource usage data for {app_name}, using empty defaults")
            usage = {}

        all_cgroup_paths = usage.get("cgroup_paths") or (
            [usage["cgroup_path"]] if usage.get("cgroup_path") else []
        )
        effective_app_ids = [os.path.basename(p) for p in all_cgroup_paths if p]
        if not effective_app_ids:
            logger.warning(f"Could not determine cgroup path for {app_name} (ID: {app_id})")
            return False
        effective_app_id = effective_app_ids[0]   # primary (lexicographically smallest cgroup)
        extra_effective_ids = effective_app_ids[1:]

        raw_cpu_percent = usage.get("cpu_percent", 0)
        mem_current = usage.get("mem_current", 0) + usage.get("mem_swap_current", 0)  # RSS + swap = true working set
        io_read_mb = usage.get("io_read_mb", 0)
        io_write_mb = usage.get("io_write_mb", 0)
        io_read_iops = usage.get("io_read_iops", 0)
        io_write_iops = usage.get("io_write_iops", 0)

        baseline = self.all_limits.manual_limit_baseline.get(effective_app_id, {})
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

        cpu_usage_percent = raw_cpu_percent if raw_cpu_percent >= 2 else 0

        is_io_limit = self._is_io_limit_reached(io_read_mb, io_write_mb, io_read_iops, io_write_iops)
        force_user_io_limit = bool(
            isinstance(limit_overrides, dict) and isinstance(limit_overrides.get("disk_io"), dict)
        )

        cpu_quota = (max(1, int(cpu_usage_percent * limit_rates["cpu_rate"]))
                     if (limit_rates.get("cpu_rate") and cpu_usage_percent > 0) else None)
        mem_high = (max(1, int(mem_current * limit_rates["mem_rate"]))
                    if (limit_rates.get("mem_rate") and mem_current > 0) else None)
        io_limits = limit_rates.get("disk_io_rate", {})
        should_apply_io_limit = bool(io_limits) and (force_user_io_limit or is_io_limit)

        logger.debug(
            f"[set_resource_limit] {app_name}: cpu_usage_percent={cpu_usage_percent} "
            f"* cpu_rate={limit_rates.get('cpu_rate')} -> cpu_quota={cpu_quota}; "
            f"mem_current={mem_current}MB * mem_rate={limit_rates.get('mem_rate')} -> mem_high={mem_high}; "
            f"is_io_limit={is_io_limit} force_user_io_limit={force_user_io_limit} "
            f"should_apply_io_limit={should_apply_io_limit}"
        )

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

        resource_limited = False
        io_limited = False

        per_cg_mem = usage.get('per_cgroup_mem', {})
        per_cg_cpu_delta = usage.get('per_cgroup_cpu_delta', {})

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

        if (cpu_quota is not None or mem_high is not None) and self.is_running:
            if extra_effective_ids:
                all_ids = [effective_app_id] + extra_effective_ids
                mem_dist = _split_proportionally(mem_high, all_ids, per_cg_mem)
                cpu_dist = _split_proportionally(cpu_quota, all_ids, per_cg_cpu_delta)
                primary_ok = self.control_manager.adjust_resources(
                    effective_app_id, "critical",
                    cpu_quota=cpu_dist.get(effective_app_id, cpu_quota),
                    mem_high=mem_dist.get(effective_app_id, mem_high),
                )
                if primary_ok:
                    resource_limited = True
                    self.control_manager.set_limited_app_dominant(True)
                    logger.info(f"Successfully set CPU/Memory limits for {app_name} ({effective_app_id})")
                else:
                    logger.error(f"Failed to set CPU/Memory limits for {app_name} ({effective_app_id})")
                for extra_id in extra_effective_ids:
                    ok = self.control_manager.adjust_resources(
                        extra_id, "critical",
                        cpu_quota=cpu_dist.get(extra_id, cpu_quota),
                        mem_high=mem_dist.get(extra_id, mem_high),
                    )
                    logger.info(
                        f"{'Successfully set' if ok else 'Failed to set'} "
                        f"CPU/Memory limits for extra cgroup {extra_id}"
                    )
            else:
                if self.control_manager.adjust_resources(
                        effective_app_id, "critical",
                        cpu_quota=cpu_quota,
                        mem_high=mem_high
                ):
                    resource_limited = True
                    self.control_manager.set_limited_app_dominant(True)
                    logger.info(f"Successfully set CPU/Memory limits for {app_name} ({effective_app_id})")
                else:
                    logger.error(f"Failed to set CPU/Memory limits for {app_name} ({effective_app_id})")

        if should_apply_io_limit and io_limits and self.is_running:
            limits = {
                "default": {
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

        with self.all_limits.lock:
            existing = self.all_limits.apps.get(effective_app_id)
            if existing is not None and existing.source == "auto":
                self.all_limits.apps.pop(effective_app_id, None)
                logger.info(f"Removed {app_name} from auto-limited apps (now manually limited)")

            if resource_limited or io_limited:
                self.all_limits.apps[effective_app_id] = LimitedApp(
                    public_app_id=app_id,
                    app_name=app_name,
                    source="manual",
                    limit_rates=limit_rates,
                    limit_parts={'cpu_mem_limited': resource_limited, 'io_limited': io_limited},
                    state=None,
                    cgroups=[effective_app_id] + list(extra_effective_ids),
                    pids=set(usage.get('pids') or []),
                )
                app_utils.update_app_status(app_id, "a_limited")
                app_utils.callback_manager.send_callback_notification({
                    'app_id': app_id,
                    'app_name': app_name,
                    'status': "a_limited",
                    'purpose': "app"
                }, False)
                self.all_limits.manual_limit_baseline[effective_app_id] = {
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
        """Restore resource limits for the given app_id (manual/UI path).

        Behaviour unchanged from the pre-registry-refactor version; the
        only additions are (1) locating the entry via the unified registry
        and (2) popping it under ``self.all_limits.lock`` so the reaper thread
        cannot restore the same app concurrently.  ``manual_limit_baseline``
        is intentionally left in place (peak latch survives a manual
        restore, as before).
        """
        with self.all_limits.lock:
            # Manual restore only ever targets manual limits — auto limits are
            # owned by the pressure loop's staged recovery (and the reaper on
            # close), never by an explicit user restore.
            found = self.all_limits.by_public_id(app_id, source="manual")
            if found is not None:
                effective_app_id, entry = found
                effective_app_ids = list(entry.cgroups) or [effective_app_id]
                app_name = entry.app_name
                limit_parts = entry.limit_parts
                self.all_limits.apps.pop(effective_app_id, None)
            else:
                # Fallback: treat app_id itself as the effective cgroup id,
                # matching the previous default when no mapping existed.
                effective_app_ids = [app_id]
                app_name, limit_parts = None, {}

        effective_app_id = effective_app_ids[0]
        extra_effective_ids = effective_app_ids[1:]
        restore_success = True
        try:
            logger.info(f"Restoring resources for app: {app_id}, name: {app_name}")

            if limit_parts.get('cpu_mem_limited', False):
                if not self.control_manager.adjust_resources(effective_app_id, "low"):
                    logger.error(f"Failed to restore CPU/Memory for {app_id} ({effective_app_id})")
                    restore_success = False
                for extra_id in extra_effective_ids:
                    self.control_manager.adjust_resources(extra_id, "low")

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
            time.sleep(self.config.regular_update_sys_pressure_time)
            self.control_manager.set_limited_app_dominant(False)

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
