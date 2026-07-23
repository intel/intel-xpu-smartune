# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0


import json
import os
import threading
import time
from typing import Any, Callable, Dict, Optional
from flask import Blueprint, request

import psutil

from db.DatabaseModel import MonitorSnapshot
from monitor import ResourceMonitor, PSIMonitor, PressureAnalyzer
from monitor.system_info import (
    collect_static_info,
    collect_dynamic_info,
    STATIC_INFO_SECTIONS,
    DYNAMIC_INFO_SECTIONS,
)
from monitor.app_discovery import is_noise_process
from utils.app_utils import get_cgroup_path_by_pid
from utils.http_utils import RetCode, construct_response
from utils.logger import logger
from utils.self_ident import is_own_process

monitor_bp = Blueprint('monitor', __name__, url_prefix='/monitor')

_resource_monitor = None
_system_pressure_monitor = None

# ---------------------------------------------------------------------------
# Background auto-refresh cache for /dynamic_info
# ---------------------------------------------------------------------------
# A daemon thread pre-collects dynamic_info every _DYNAMIC_INFO_REFRESH_INTERVAL_SEC
# seconds.  The REST endpoint simply returns the cached value, making each poll
# response near-instant regardless of how frequently the UI calls it.
# This is the same pattern used by SystemPressureMonitor._start_auto_refresh.
_DYNAMIC_INFO_REFRESH_INTERVAL_SEC: float = 2.0   # background collection interval
_DYNAMIC_INFO_CACHE: Dict[str, Any] = {"data": None, "ts": 0.0}
_DYNAMIC_INFO_CACHE_LOCK = threading.Lock()
_dynamic_info_refresh_started = False
_dynamic_info_refresh_start_lock = threading.Lock()
# Stop signal + handle for the background collector so it can be shut down
# cleanly at service stop (rather than only dying with the process).
_dynamic_info_stop_event = threading.Event()
_dynamic_info_collector_thread = None

# Section-scoped on-demand cache for /dynamic_info?sections=... and
# /dynamic_info/<section>.  Unlike the full snapshot above, these requests are
# meant for integrators who only want one hardware block: they collect ONLY the
# requested sections and never start the full-collection background thread, so
# polling /dynamic_info/gpu never queries CPU/NPU/disk/etc.  A short per-section
# TTL coalesces rapid polls so each request doesn't re-fork xpu-smi/npu-smi.
_DYNAMIC_SECTION_TTL_SEC: float = 1.0
_DYNAMIC_SECTION_CACHE: Dict[str, Dict[str, Any]] = {}  # section -> {"data":..., "ts":...}
_DYNAMIC_SECTION_CACHE_LOCK = threading.Lock()
# Sections whose collection needs the ResourceMonitor / SystemPressureMonitor
# singletons; everything else (cpu/memory/network/gpu/npu) needs neither, so a
# GPU-only request never constructs those monitors.
_SECTIONS_NEED_RM = frozenset({"disk"})
_SECTIONS_NEED_SPM = frozenset({"disk", "pressure"})

# ---------------------------------------------------------------------------
# Background auto-refresh cache for /app_resource_stats and /app_disk_io_stats
# ---------------------------------------------------------------------------
# Both endpoints internally invoke ResourceMonitor._get_top_processes, which
# performs several blocking psutil/time.sleep sampling rounds (CPU+IO+GPU) and
# costs multiple seconds per call.  Without caching, every dashboard client
# would trigger its own collection cycle, multiplying CPU/IO load N-fold and
# making the server feel sluggish as soon as more than one dashboard is open.
# A single daemon thread refreshes both datasets every
# _APP_STATS_REFRESH_INTERVAL_SEC seconds; all clients read from the shared
# cache so the cost is independent of the number of connected dashboards.
_APP_STATS_REFRESH_INTERVAL_SEC: float = 2.0
# If no client has requested app stats within this many seconds, the refresh
# thread parks itself (cheap blocking wait) until the next request wakes it up.
# This avoids burning CPU on the expensive _get_top_processes pipeline when
# nobody is looking at the App Resources tab.  Set just slightly above the
# client poll interval (5 s) so one missed poll triggers parking but a
# steady-state client never trips it.
_APP_STATS_IDLE_TIMEOUT_SEC: float = 5.5
_APP_STATS_CACHE_N: int = 10  # collect up to this many entries; clients receive a slice
_APP_STATS_CACHE: Dict[str, Any] = {
    "resource": None,
    "disk_io": None,
    "ts": 0.0,
    "last_request_ts": 0.0,
}
_APP_STATS_CACHE_LOCK = threading.Lock()
_app_stats_request_event = threading.Event()  # set by request handler to wake the refresher
_app_stats_refresh_started = False
_app_stats_refresh_start_lock = threading.Lock()


def _get_monitored_sections() -> list:
    """Resolve the configured set of sections to continuously monitor.

    ``monitored_sections`` unset (None) → monitor every section (the default,
    backward compatible).  An explicit list is validated against
    ``DYNAMIC_INFO_SECTIONS`` (unknown names dropped) and returned in canonical
    order.  An explicit empty list means "pure on-demand" — no background
    collector runs.
    """
    from config.config import b_config
    raw = b_config.monitored_sections
    if raw is None:
        return list(DYNAMIC_INFO_SECTIONS)
    raw_set = set(raw)
    return [name for name in DYNAMIC_INFO_SECTIONS if name in raw_set]


def _dynamic_info_collector_loop() -> None:
    """Continuously collect the configured ``monitored_sections`` into the
    shared cache and persist them to history, until signalled to stop.

    The configured set is re-read every cycle, so it only ever collects the
    hardware the operator asked to monitor — a deployment configured for GPU
    only never queries CPU/NPU/disk/etc. in the background.
    """
    while not _dynamic_info_stop_event.is_set():
        loop_start = time.time()
        try:
            monitored = _get_monitored_sections()
            if monitored:
                # sections=None collects the full snapshot (and keeps the exact
                # historical full-snapshot shape); a subset collects just those.
                all_set = set(monitored) == set(DYNAMIC_INFO_SECTIONS)
                sections_arg = None if all_set else monitored
                need_rm = any(s in _SECTIONS_NEED_RM for s in monitored)
                need_spm = any(s in _SECTIONS_NEED_SPM for s in monitored)
                data = collect_dynamic_info(
                    resource_monitor=_get_resource_monitor() if need_rm else None,
                    system_pressure_monitor=_get_system_pressure_monitor() if need_spm else None,
                    sections=sections_arg,
                    persist=True,
                )
                with _DYNAMIC_INFO_CACHE_LOCK:
                    _DYNAMIC_INFO_CACHE["data"] = data
                    _DYNAMIC_INFO_CACHE["ts"] = time.time()
        except Exception as exc:
            logger.debug("dynamic_info collector error: %s", exc)
        elapsed = time.time() - loop_start
        # Wait on the stop event (not time.sleep) so shutdown is prompt; still
        # sleep at least 0.1 s to avoid a tight loop on a fast/exception path.
        _dynamic_info_stop_event.wait(max(0.1, _DYNAMIC_INFO_REFRESH_INTERVAL_SEC - elapsed))


def _start_dynamic_info_auto_refresh() -> None:
    """Start the config-driven background collector (idempotent).

    Collects the configured ``monitored_sections`` every
    ``_DYNAMIC_INFO_REFRESH_INTERVAL_SEC`` seconds so API requests return
    immediately from the warm cache.  A no-op when ``monitored_sections`` is
    empty (pure on-demand mode).  Started at service startup; also called
    lazily by the full endpoint as a dev/test self-start fallback.
    """
    global _dynamic_info_refresh_started, _dynamic_info_collector_thread
    with _dynamic_info_refresh_start_lock:
        if _dynamic_info_refresh_started or not _get_monitored_sections():
            return
        _dynamic_info_refresh_started = True
        _dynamic_info_stop_event.clear()
        _dynamic_info_collector_thread = threading.Thread(
            target=_dynamic_info_collector_loop, daemon=True, name="dynamic-info-collector")
        _dynamic_info_collector_thread.start()


def stop_dynamic_info_collector() -> None:
    """Signal the background collector to stop and wait briefly for it to exit.

    Safe to call when the collector was never started (no-op).
    """
    global _dynamic_info_refresh_started
    with _dynamic_info_refresh_start_lock:
        if not _dynamic_info_refresh_started:
            return
        _dynamic_info_refresh_started = False
    _dynamic_info_stop_event.set()
    if _dynamic_info_collector_thread is not None:
        _dynamic_info_collector_thread.join(timeout=2)


def _start_app_stats_auto_refresh() -> None:
    """Start the background thread that pre-caches app resource and disk I/O stats.

    Idempotent: calling more than once has no effect.  The thread collects
    fresh per-app metrics every ``_APP_STATS_REFRESH_INTERVAL_SEC`` seconds
    and stores the result in ``_APP_STATS_CACHE`` so that API requests return
    immediately, regardless of how many dashboard clients are connected.
    """
    global _app_stats_refresh_started
    with _app_stats_refresh_start_lock:
        if _app_stats_refresh_started:
            return
        _app_stats_refresh_started = True

    def refresh_loop() -> None:
        while True:
            # Park the refresh loop if nobody has requested app stats within the
            # idle window — avoids running the expensive _get_top_processes pipeline
            # when no dashboard is on the App Resources tab.
            with _APP_STATS_CACHE_LOCK:
                last_req = _APP_STATS_CACHE.get("last_request_ts", 0.0)
            if time.time() - last_req > _APP_STATS_IDLE_TIMEOUT_SEC:
                # Drop stale cache so the next request gets fresh data instead
                # of whatever was last computed minutes/hours ago.
                with _APP_STATS_CACHE_LOCK:
                    _APP_STATS_CACHE["resource"] = None
                    _APP_STATS_CACHE["disk_io"] = None
                # logger.debug("[poll-debug] app_stats refresher PARK (idle)")
                # Block until a request handler wakes us up.  No timeout: we
                # only resume work when someone actually wants the data.
                _app_stats_request_event.wait()
                _app_stats_request_event.clear()
                # logger.debug("[poll-debug] app_stats refresher WAKE")
                continue

            loop_start = time.time()
            # logger.debug("[poll-debug] app_stats refresh START")
            try:
                monitor = _get_resource_monitor()
                resource = monitor.get_app_resource_stats(n=_APP_STATS_CACHE_N)
                disk_io = monitor.get_app_disk_io_stats(n=_APP_STATS_CACHE_N)
                with _APP_STATS_CACHE_LOCK:
                    _APP_STATS_CACHE["resource"] = resource
                    _APP_STATS_CACHE["disk_io"] = disk_io
                    _APP_STATS_CACHE["ts"] = time.time()
            except Exception as exc:
                logger.debug("app_stats auto-refresh error: %s", exc)
            elapsed = time.time() - loop_start
            # logger.debug(f"[poll-debug] app_stats refresh END   (took {elapsed:.2f}s)")
            time.sleep(max(0.1, _APP_STATS_REFRESH_INTERVAL_SEC - elapsed))

    t = threading.Thread(target=refresh_loop, daemon=True, name="app-stats-refresh")
    t.start()


def _get_resource_monitor() -> ResourceMonitor:
    """Return the shared ResourceMonitor instance, creating it if needed."""
    global _resource_monitor
    if _resource_monitor is None:
        _resource_monitor = ResourceMonitor()
    return _resource_monitor


def _get_system_pressure_monitor():
    """Return the shared SystemPressureMonitor instance, creating it if needed."""
    global _system_pressure_monitor
    if _system_pressure_monitor is None:
        from config.config import b_config
        _system_pressure_monitor = SystemPressureMonitor(b_config)
    return _system_pressure_monitor


def register_system_pressure_monitor(spm) -> None:
    """Register an externally-created SystemPressureMonitor instance as the shared singleton.

    Call this once during application startup (after the balancer's ControlManager is
    initialised) so that the monitor API endpoints and collect_dynamic_info always return
    the same pressure data as the balancer's own decision logic.
    """
    global _system_pressure_monitor
    _system_pressure_monitor = spm


# ---------------------------------------------------------------------------
# Snapshot retention settings and background cleanup
# ---------------------------------------------------------------------------
# MonitorSnapshot rows are written every few seconds; without periodic cleanup
# the database grows without bound.  A background thread runs an hourly sweep
# and deletes rows older than _SNAPSHOT_RETENTION_DAYS days.
#
# The retention period is user-configurable via the History tab and persisted
# in a small JSON file alongside the database so the setting survives restarts.

_SNAPSHOT_RETENTION_DEFAULT_DAYS: int = 3
_SNAPSHOT_RETENTION_MIN_DAYS: int = 1
_SNAPSHOT_RETENTION_MAX_DAYS: int = 7
_SNAPSHOT_CLEANUP_INTERVAL_SEC: float = 300.0  # run cleanup every 5 minutes

# Path of the runtime-state file — stored next to config.yaml so all
# locally-tunable state lives under config/.  Holds dashboard-driven values
# (snapshot retention) plus optimistic-concurrency timestamps.  Listed in
# balancer/.gitignore since it is per-deployment runtime state, not source.
_SETTINGS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "config", "runtime_state.json"
)
_SETTINGS_LOCK = threading.Lock()

# In-memory copy; populated by _load_retention_settings() on first use.
_retention_days: Optional[int] = None
_cleanup_started = False
_cleanup_start_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Optimistic-concurrency timestamps for shared (global) configuration
# ---------------------------------------------------------------------------
# Both /config/weights_top and /history/retention are global state: any client
# can change them and every other client is affected.  To prevent silent
# last-write-wins overwrites, each settable section carries an `updated_at`
# unix timestamp.  GET returns it, POST must echo it back as
# `expected_updated_at`; if the value on disk has moved on (someone else
# saved meanwhile) the server returns RetCode.CONFLICT with the current
# state so the UI can prompt the user to reload.
_CONFIG_TS_KEYS = {
    "weights_top":              "weights_top_updated_at",
    "retention":                "retention_updated_at",
    "passive_resource_control": "passive_resource_control_updated_at",
    "monitored_sections":       "monitored_sections_updated_at",
    "system_pressure":          "system_pressure_updated_at",
    "disk_pressure":            "disk_pressure_updated_at",
    "limit_policy":             "limit_policy_updated_at",
}

_CONFIG_CONFLICT_MSG = "Configuration was modified by another client; please reload."


def _check_config_conflict(section: str, expected_raw: Any, build_current):
    """Shared optimistic-concurrency gate for config POST handlers.

    Returns a ready-to-send conflict/error Response, or ``None`` when the write
    may proceed.  ``build_current`` is a zero-arg callable returning the payload
    describing the current server state (embedded under ``current`` on conflict).
    """
    expected_ts = _coerce_expected_ts(expected_raw)
    if expected_ts == -1:
        return construct_response(
            data={"success": False},
            retcode=RetCode.ARGUMENT_ERROR,
            retmsg="expected_updated_at must be an integer",
        )
    current_ts = _get_config_updated_at(section)
    conflict = (expected_ts is None and current_ts != 0) or (
        expected_ts is not None and expected_ts != current_ts
    )
    if conflict:
        logger.info("%s conflict: expected=%s current=%d", section, expected_ts, current_ts)
        return construct_response(
            data={"success": False, "current": build_current()},
            retcode=RetCode.CONFLICT,
            retmsg=_CONFIG_CONFLICT_MSG,
        )
    return None


_MONITORED_SECTIONS_SNAPSHOT_KEY = "monitored_sections_snapshot"


def _get_monitored_sections_updated_at() -> int:
    """Return a change-driven updated_at for monitored_sections.

    The timestamp advances only when the *effective* monitored sections
    actually change (a config edit that alters the resolved list, or a future
    API update) — never on unrelated config.yaml writes such as adding an app
    or tweaking weights, which is what a raw file mtime would (wrongly) react
    to.  The last-seen section list and its timestamp are persisted in
    monitor_settings.json, so the value is stable across repeated reads and
    monotonic across service restarts.
    """
    key = _CONFIG_TS_KEYS["monitored_sections"]
    current = _get_monitored_sections()
    with _SETTINGS_LOCK:
        settings = _read_settings_file()
        stored_snapshot = settings.get(_MONITORED_SECTIONS_SNAPSHOT_KEY)
        try:
            stored_ts = int(settings.get(key, 0))
        except (TypeError, ValueError):
            stored_ts = 0
        # Unchanged and already stamped once → return the persisted value.
        if stored_snapshot == current and stored_ts > 0:
            return stored_ts
        # First observation, or the resolved sections changed → bump.  Keep it
        # strictly greater than the stored value so the change is always
        # detectable even within the same wall-clock second.
        new_ts = max(stored_ts + 1, int(time.time()))
        try:
            _write_settings_file({key: new_ts, _MONITORED_SECTIONS_SNAPSHOT_KEY: current})
        except Exception as exc:
            # Persistence unavailable (e.g. read-only config dir).  Never fail
            # the caller over a bookkeeping write — return the last persisted
            # value so the timestamp stays stable instead of advancing on every
            # call (which would make clients refetch endlessly).
            logger.debug("monitored_sections updated_at persist failed: %s", exc)
            return stored_ts
        return new_ts


def _read_settings_file() -> Dict[str, Any]:
    """Load the raw monitor_settings.json contents (or {} on any failure)."""
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_settings_file(updates: Dict[str, Any]) -> None:
    """Merge ``updates`` into monitor_settings.json atomically."""
    existing = _read_settings_file()
    existing.update(updates)
    tmp = _SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(existing, fh)
    os.replace(tmp, _SETTINGS_FILE)


def _get_config_updated_at(section: str) -> int:
    """Return the persisted updated_at (unix seconds) for a config section.

    Returns 0 if the section has never been saved through this API — clients
    sending expected_updated_at=0 (or None) for a never-written section will
    therefore be accepted on first write.
    """
    key = _CONFIG_TS_KEYS.get(section)
    if not key:
        return 0
    with _SETTINGS_LOCK:
        raw = _read_settings_file().get(key, 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _bump_config_updated_at(section: str) -> int:
    """Persist a fresh updated_at for ``section`` and return the new value."""
    key = _CONFIG_TS_KEYS.get(section)
    if not key:
        return 0
    new_ts = int(time.time())
    with _SETTINGS_LOCK:
        _write_settings_file({key: new_ts})
    return new_ts


def _coerce_expected_ts(value: Any) -> Optional[int]:
    """Best-effort cast of ``expected_updated_at`` from the request body.

    Returns ``None`` when the caller omitted the field (treated as "first
    write — accept unconditionally" only when the server side is also 0).
    Returns ``-1`` when the field was provided but malformed; the caller
    surfaces this as ARGUMENT_ERROR rather than CONFLICT.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _load_retention_settings() -> int:
    """Load retention days from the settings file.  Returns the loaded value (or default)."""
    global _retention_days
    with _SETTINGS_LOCK:
        if _retention_days is not None:
            return _retention_days
        try:
            with open(_SETTINGS_FILE, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            days = int(cfg.get("snapshot_retention_days", _SNAPSHOT_RETENTION_DEFAULT_DAYS))
            days = max(_SNAPSHOT_RETENTION_MIN_DAYS, min(days, _SNAPSHOT_RETENTION_MAX_DAYS))
        except Exception:
            days = _SNAPSHOT_RETENTION_DEFAULT_DAYS
        _retention_days = days
        return days


def _save_retention_settings(days: int) -> int:
    """Persist retention days to the settings file and update the in-memory value.

    Returns the new ``updated_at`` unix timestamp written for this section so
    callers can echo it back to the client.  A fresh timestamp is written even
    if the value did not change, because the act of "save" itself is a write
    that other clients should reload past.
    """
    global _retention_days
    days = max(_SNAPSHOT_RETENTION_MIN_DAYS, min(int(days), _SNAPSHOT_RETENTION_MAX_DAYS))
    new_ts = int(time.time())
    ts_key = _CONFIG_TS_KEYS["retention"]
    with _SETTINGS_LOCK:
        _retention_days = days
        try:
            existing = _read_settings_file()
            existing["snapshot_retention_days"] = days
            existing[ts_key] = new_ts
            tmp = _SETTINGS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(existing, fh)
            os.replace(tmp, _SETTINGS_FILE)
        except Exception as exc:
            logger.warning("Failed to save monitor settings: %s", exc)
            return 0
    return new_ts


def _run_snapshot_cleanup() -> None:
    """Delete MonitorSnapshot rows older than the configured retention period."""
    days = _load_retention_settings()
    try:
        deleted = MonitorSnapshot.delete_older_than(days)
        if deleted:
            logger.info("Snapshot cleanup: deleted %d rows older than %d day(s)", deleted, days)
        else:
            logger.debug("Snapshot cleanup: no rows to delete (retention = %d day(s))", days)
    except Exception as exc:
        logger.warning("Snapshot cleanup failed: %s", exc)


def _start_snapshot_cleanup_task() -> None:
    """Start the background thread that periodically deletes old snapshots.

    Idempotent — calling more than once has no effect.
    """
    global _cleanup_started
    with _cleanup_start_lock:
        if _cleanup_started:
            return
        _cleanup_started = True

    def cleanup_loop() -> None:
        # Run once at startup (with a short delay to let the server settle),
        # then every _SNAPSHOT_CLEANUP_INTERVAL_SEC seconds.
        time.sleep(30)
        while True:
            _run_snapshot_cleanup()
            time.sleep(_SNAPSHOT_CLEANUP_INTERVAL_SEC)

    t = threading.Thread(target=cleanup_loop, daemon=True, name="snapshot-cleanup")
    t.start()


# Numeric ordering for pressure levels used by the peak-latch logic.
# Higher numbers represent higher pressure.  "unknown" ranks below every
# real level so that it never masks a valid reading.
_LEVEL_ORDER: Dict[str, int] = {
    "unknown":  -1,
    "low":       0,
    "medium":    1,
    "high":      2,
    "critical":  3,
}


class SystemPressureMonitor:
    """ Manages overall system pressure state based on PSI and resource usage,
    with auto-refresh and disk I/O stress tracking."""
    def __init__(self, config):
        self.config = config
        self.psi = PSIMonitor()
        self.res = ResourceMonitor()
        self.analyzer = PressureAnalyzer(config)

        self._current_level = None
        self.is_current_disk_io_stressed = False
        self.score = 0.0
        self._disk_io_stress: dict = {}
        self._last_update_time = 0
        _MIN_PRESSURE_UPDATE = 1.0   # seconds
        _MAX_PRESSURE_UPDATE = 60.0  # seconds
        self._CACHE_TTL = max(_MIN_PRESSURE_UPDATE, min(_MAX_PRESSURE_UPDATE, config.regular_update_sys_pressure_time))
        self._is_limited_app_dominant = False
        # Cgroup paths (relative to the cgroup mount) of the auto-limited apps that are
        # currently among the top consumers, used to discount their self-inflicted PSI.
        # Empty when no auto-limited app is dominant.
        self._dominant_cgroups = []
        self._update_lock = threading.Lock()

        # Peak-latch fields: track the highest pressure seen since the balancer
        # last called consume_peak_pressure_level().  They only rise (never fall)
        # during each refresh cycle so that transient spikes cannot be silently
        # skipped by the balancer's idle_check_interval gate.
        self._peak_level = None
        self._peak_disk_io_stressed = False

        # Listeners notified when the system transitions into or out of the
        # "critical" pressure level.  Each entry is a callable(is_critical: bool).
        self._critical_state_listeners: list[Callable[[bool], None]] = []

        self._start_auto_refresh()

    def register_critical_state_listener(self, callback) -> None:
        """Register a callback invoked when system pressure enters or leaves the
        "critical" level.

        The callback receives a single bool: ``True`` when entering critical,
        ``False`` when leaving.  Callbacks are fired from the auto-refresh
        thread, so they must be thread-safe and non-blocking.
        """
        self._critical_state_listeners.append(callback)

    def set_limited_app_dominant(self, is_dominant: bool, dominant_cgroups=None):
        """Set whether any rate-limited app is currently dominant, and (when so) the cgroup
        paths used to discount their self-inflicted PSI. Accepts a single path or a list."""
        self._is_limited_app_dominant = is_dominant
        if not is_dominant or not dominant_cgroups:
            self._dominant_cgroups = []
        elif isinstance(dominant_cgroups, str):
            self._dominant_cgroups = [dominant_cgroups]
        else:
            self._dominant_cgroups = list(dominant_cgroups)

    def _start_auto_refresh(self):
        """Start the background thread that periodically refreshes system pressure state."""
        def refresh_loop():
            while True:
                time.sleep(self._CACHE_TTL * 0.9)
                self._safe_update()

        threading.Thread(target=refresh_loop, daemon=True).start()

    def _safe_update(self):
        """Thread-safe pressure level update."""
        if self._update_lock.acquire(blocking=False):
            try:
                new_level, score, disk_io_stressed, disk_io_stress = self._update_pressure_level()
                old_level = self._current_level
                self._current_level = new_level
                self.score = score
                self.is_current_disk_io_stressed = disk_io_stressed
                self._disk_io_stress = disk_io_stress
                # Peak latch: only raise the peak, never lower it.  The balancer
                # resets the peak via consume_peak_pressure_level().
                if _LEVEL_ORDER.get(new_level, -1) > _LEVEL_ORDER.get(self._peak_level, -1):
                    self._peak_level = new_level
                if disk_io_stressed:
                    self._peak_disk_io_stressed = True
            finally:
                self._update_lock.release()

            # Notify listeners outside the lock to avoid re-entrant deadlock.
            # We compare the old and new levels after releasing the lock; the
            # transition flags are local, so they are safe to use here.
            was_critical = (old_level == "critical")
            is_critical = (new_level == "critical")
            if was_critical != is_critical:
                for cb in self._critical_state_listeners:
                    try:
                        cb(is_critical)
                    except Exception as exc:
                        logger.error("Critical state listener raised an error: %s", exc)

    def _update_pressure_level(self) -> tuple[str, float, bool, dict]:
        """Recompute the current pressure level using internal state."""
        try:
            psi_data = self.psi.get_current_pressure()
            usage_data = self.res.get_resource_usage()
            disk_io = self.res.is_disk_io_stressed()
            self_fraction = None
            if self._is_limited_app_dominant and self._dominant_cgroups:
                self_fraction = self.psi.get_self_inflicted_fraction(self._dominant_cgroups)
            score = self.analyzer.calculate_pressure_score(
                psi_data,
                usage_data,
                self._is_limited_app_dominant,
                self_fraction
            )
            logger.debug(f"disk_io={disk_io}")
            level = self.analyzer.get_pressure_level(score, self.config.thresholds)
            self._last_update_time = time.time()
            return level, score, disk_io.get("is_stressed", False), disk_io
        except Exception as e:
            logger.error("Failed to update pressure level: %s", str(e))
            return "unknown", 0.0, False, {}


    def get_current_pressure_level(self) -> tuple:
        """Return the current pressure level as (level, score, is_disk_io_stressed)."""
        logger.debug("Current PSI level: %s (pressure: %.2f), disk io stressed: %s", self._current_level, self.score,
                     self.is_current_disk_io_stressed)
        return self._current_level, self.score, self.is_current_disk_io_stressed

    def consume_peak_pressure_level(self) -> tuple:
        """Return the highest pressure level seen since the last call, then reset the peak.

        Returns (peak_level, score, peak_disk_io_stressed).

        The balancer calls this instead of get_current_pressure_level() so that
        transient spikes (e.g. a brief "critical" window that resolves before the
        idle_check_interval gate opens) are never silently dropped.  The peak is
        reset to the current instantaneous level after each call, so the next call
        starts fresh.  This decouples correctness from the relationship between
        idle_check_interval, regular_update_sys_pressure_time, and the UI poll
        interval — no dynamic coupling between those three clocks is required.

        Note: get_current_pressure_level() is intentionally kept separate and is
        still used by display/point-in-time paths (UI, app_intercept) that must NOT
        consume or reset the peak.
        """
        with self._update_lock:
            peak_level = self._peak_level if self._peak_level is not None else self._current_level
            peak_disk_io = self._peak_disk_io_stressed
            # Reset peak to current instantaneous values ready for the next window.
            self._peak_level = self._current_level
            self._peak_disk_io_stressed = self.is_current_disk_io_stressed
        logger.debug(
            "consume_peak: peak_level=%s, peak_disk_io=%s (current=%s)",
            peak_level, peak_disk_io, self._current_level,
        )
        return peak_level, self.score, peak_disk_io

    def get_disk_io_stress(self) -> dict:
        """Return the cached disk IO stress details from the most recent update.

        The dict format matches ResourceMonitor.is_disk_io_stressed:
        {
            "is_stressed": bool,
            "stressed_disks": list[str],
            "iowait": float,
            "details": {disk: {utilization, read_kb_per_sec, write_kb_per_sec, read_iops, write_iops, is_busy}}
        }
        """
        return self._disk_io_stress

    def update_network_pressure_level(self, network_data):
        """Update the network pressure level independently.

        Returns: (tx_level, rx_level, tx_value, rx_value)
        """
        try:
            tx_level = self.analyzer.get_pressure_level(network_data['tx'], self.config.network_thresholds)
            rx_level = self.analyzer.get_pressure_level(network_data['rx'], self.config.network_thresholds)
            return tx_level, rx_level, network_data['tx'], network_data['rx']
        except Exception as e:
            logger.error("Failed to update network pressure level: %s", str(e))
            return "unknown", "unknown", 0.0, 0.0


@monitor_bp.route('/app_resource_stats', methods=['GET'])
def get_app_resource_stats():
    """Return per-application CPU/memory/GPU resource usage (top N by score)."""
    try:
        # logger.debug(f"[poll-debug] app_resource_stats START client={request.remote_addr}")
        _start_app_stats_auto_refresh()
        n = int(request.args.get('n', 10))

        with _APP_STATS_CACHE_LOCK:
            _APP_STATS_CACHE["last_request_ts"] = time.time()
            apps = _APP_STATS_CACHE.get("resource")
        # Wake the refresher in case it parked itself during an idle window.
        _app_stats_request_event.set()

        if apps is None:
            # Cache not yet populated (cold start, or refresher just woke from
            # an idle park) — collect synchronously so the client gets data now.
            apps = _get_resource_monitor().get_app_resource_stats(n=max(n, _APP_STATS_CACHE_N))
            with _APP_STATS_CACHE_LOCK:
                if _APP_STATS_CACHE.get("resource") is None:
                    _APP_STATS_CACHE["resource"] = apps
                    _APP_STATS_CACHE["ts"] = time.time()

        # logger.debug(f"[poll-debug] app_resource_stats END   client={request.remote_addr}")
        return construct_response(
            data={'apps': apps[:n]},
            retmsg="Successfully retrieved app resource stats"
        )
    except Exception as e:
        logger.error(f"get_app_resource_stats failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/app_disk_io_stats', methods=['GET'])
def get_app_disk_io_stats():
    """Return per-application disk I/O usage (top N by score)."""
    try:
        _start_app_stats_auto_refresh()
        n = int(request.args.get('n', 10))

        with _APP_STATS_CACHE_LOCK:
            _APP_STATS_CACHE["last_request_ts"] = time.time()
            apps = _APP_STATS_CACHE.get("disk_io")
        _app_stats_request_event.set()

        if apps is None:
            apps = _get_resource_monitor().get_app_disk_io_stats(n=max(n, _APP_STATS_CACHE_N))
            with _APP_STATS_CACHE_LOCK:
                if _APP_STATS_CACHE.get("disk_io") is None:
                    _APP_STATS_CACHE["disk_io"] = apps
                    _APP_STATS_CACHE["ts"] = time.time()

        return construct_response(
            data={'apps': apps[:n]},
            retmsg="Successfully retrieved app disk I/O stats"
        )
    except Exception as e:
        logger.error(f"get_app_disk_io_stats failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/processes', methods=['GET'])
def get_processes():
    """Return all running processes sorted by CPU usage."""
    try:
        procs = []
        # Normalise per-process CPU% to the whole machine (0-100%) so it shares
        # the summary bar's scale instead of psutil's per-core value (>100%).
        cpu_count = psutil.cpu_count() or 1
        attrs = ['pid', 'name', 'username', 'cpu_percent', 'memory_percent',
                 'status', 'cmdline', 'memory_info', 'create_time', 'uids']
        for p in psutil.process_iter(attrs):
            try:
                info = p.info
                mem = info.get('memory_info')
                mem_rss_kb = round(mem.rss / 1024, 0) if mem else 0
                mem_shared_kb = round(getattr(mem, 'shared', 0) / 1024, 0) if mem else 0
                cmdline_parts = info.get('cmdline') or []
                cmdline = ' '.join(cmdline_parts) if cmdline_parts else (info.get('name') or '')
                uids = info.get('uids')
                name = info.get('name') or ''
                # SmartTune's own processes must never be managed/killed via the UI;
                # shells and blacklisted daemons are never a meaningful "app" to
                # balance, so both are surfaced to the front-end as flags.
                is_self = is_own_process(info['pid'], cmdline)
                balancer_candidate = not is_self and not is_noise_process(name)
                procs.append({
                    'pid': info['pid'],
                    'name': name,
                    'username': info.get('username') or '',
                    'uid': uids.real if uids else None,
                    'cpu_percent': round((info.get('cpu_percent') or 0) / cpu_count, 1),
                    'memory_percent': round(info.get('memory_percent') or 0, 2),
                    'mem_rss_kb': mem_rss_kb,
                    'mem_shared_kb': mem_shared_kb,
                    'status': info.get('status') or '',
                    'create_time': info.get('create_time'),
                    'cgroup': get_cgroup_path_by_pid(info['pid']) or '',
                    'cmdline': cmdline,
                    'is_self': is_self,
                    'balancer_candidate': balancer_candidate,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        # Optional fdinfo-sampled per-PID GPU stats (adds a ~0.3s sampling window).
        if request.args.get('gpu') in ('1', 'true', 'yes'):
            gpu_stats = _get_resource_monitor().get_per_pid_gpu_stats()
            for p in procs:
                devs = gpu_stats.get(p['pid'])
                if devs:
                    p['gpu_devices'] = devs

        # Optional per-PID disk IO rates (delta since the previous call, no sleep).
        if request.args.get('io') in ('1', 'true', 'yes'):
            io_rates = _get_resource_monitor().get_per_pid_io_rates()
            for p in procs:
                r = io_rates.get(p['pid'])
                if r:
                    p['io_read_rate'] = round(r['io_read_rate'], 1)
                    p['io_write_rate'] = round(r['io_write_rate'], 1)

        procs.sort(key=lambda x: x['cpu_percent'], reverse=True)
        return construct_response(
            data={'count': len(procs), 'processes': procs},
            retmsg="Successfully retrieved process list"
        )
    except Exception as e:
        logger.error(f"get_processes failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/process_detail', methods=['GET'])
def get_process_detail():
    """Return detailed read-only info for a single PID (Properties dialog)."""
    try:
        pid = int(request.args.get('pid', 0))
    except (TypeError, ValueError):
        return construct_response(data={}, retcode=RetCode.ARGUMENT_ERROR, retmsg="Invalid pid")

    def _safe(fn, default=None):
        try:
            return fn()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            return default

    try:
        p = psutil.Process(pid)
        with p.oneshot():
            cmdline_parts = _safe(p.cmdline, []) or []
            detail = {
                'pid': pid,
                'name': _safe(p.name, '') or '',
                'exe': _safe(p.exe, '') or '',
                'cwd': _safe(p.cwd, '') or '',
                'username': _safe(p.username, '') or '',
                'status': _safe(p.status, '') or '',
                'ppid': _safe(p.ppid),
                'num_threads': _safe(p.num_threads),
                'num_fds': _safe(p.num_fds),
                'nice': _safe(p.nice),
                'create_time': _safe(p.create_time),
                'cmdline': ' '.join(cmdline_parts),
            }
        return construct_response(data=detail, retmsg="Successfully retrieved process detail")
    except psutil.NoSuchProcess:
        return construct_response(data={}, retcode=RetCode.NOT_EXISTING, retmsg=f"Process {pid} not found")
    except Exception as e:
        logger.error(f"get_process_detail failed: {str(e)}")
        return construct_response(data={}, retcode=RetCode.EXCEPTION_ERROR, retmsg=str(e))


def _static_force_refresh() -> bool:
    force_raw = (request.args.get('force_refresh') or '').strip().lower()
    return force_raw in {'1', 'true', 'yes', 'y', 'on'}


@monitor_bp.route('/static_info', methods=['GET'])
def get_static_info():
    """Return static system configuration info (hardware, OS, drivers).

    Returns the full config by default.  An optional comma-separated
    ``sections`` query parameter restricts the payload to specific groups, e.g.
    ``/static_info?sections=gpu,cpu``.  Static info is always served from the
    in-memory cache, so filtering is a pure response-view projection — it never
    triggers a partial (re)collection.  Valid names are in
    ``STATIC_INFO_SECTIONS``.
    """
    try:
        data = collect_static_info(force_refresh=_static_force_refresh())

        sections, err = _parse_section_param(request.args.get('sections'), STATIC_INFO_SECTIONS)
        if err:
            return construct_response(data={}, retcode=RetCode.ARGUMENT_ERROR, retmsg=err)
        if sections is not None:
            data = _project_sections(data, sections)

        return construct_response(
            data=data,
            retmsg="Successfully retrieved static system info"
        )
    except Exception as e:
        logger.error(f"get_static_info failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/static_info/<section>', methods=['GET'])
def get_static_info_section(section):
    """Return a single section of the static config, e.g. ``/static_info/gpu``.

    ``/static_info/all`` returns the full config (equivalent to
    ``/static_info``).
    """
    try:
        data = collect_static_info(force_refresh=_static_force_refresh())

        section = (section or '').strip().lower()
        if section == 'all':
            return construct_response(
                data=data,
                retmsg="Successfully retrieved static system info"
            )
        if section not in STATIC_INFO_SECTIONS:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg=(f"Unknown section '{section}'. "
                        f"Valid sections: {', '.join(STATIC_INFO_SECTIONS)}, all")
            )
        return construct_response(
            data=_project_sections(data, [section]),
            retmsg="Successfully retrieved static system info"
        )
    except Exception as e:
        logger.error(f"get_static_info_section failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


# Sections valid for /history projection span both snapshot types, since a
# history query may return static and/or dynamic rows.  Union preserves order
# and drops duplicates (cpu/memory/disk/gpu/npu appear in both).
HISTORY_SECTIONS = tuple(dict.fromkeys(STATIC_INFO_SECTIONS + DYNAMIC_INFO_SECTIONS))


def _parse_section_param(raw, valid_sections):
    """Parse a comma-separated ``sections`` query value.

    Returns ``(sections, error)``:
      * ``sections`` is ``None`` when ``raw`` is empty (meaning "all sections"),
        otherwise the requested names filtered to ``valid_sections`` order with
        duplicates removed.
      * ``error`` is a human-readable message when any requested name is
        invalid, else ``None``.
    """
    raw = (raw or '').strip()
    if not raw:
        return None, None
    requested = [s.strip().lower() for s in raw.split(',') if s.strip()]
    invalid = [s for s in requested if s not in valid_sections]
    if invalid:
        return None, (f"Invalid section(s): {', '.join(invalid)}. "
                      f"Valid sections: {', '.join(valid_sections)}")
    requested_set = set(requested)
    return [name for name in valid_sections if name in requested_set], None


def _project_sections(data, sections):
    """Return a copy of ``data`` keeping only ``collected_at`` + ``sections``.

    Non-dict payloads (e.g. a raw string that failed to parse) pass through
    unchanged.  Section names absent from ``data`` are silently skipped so the
    same projection works across static and dynamic snapshots.
    """
    if not isinstance(data, dict):
        return data
    result = {}
    if 'collected_at' in data:
        result['collected_at'] = data['collected_at']
    for name in sections:
        if name in data:
            result[name] = data[name]
    return result


def _respond_dynamic_info_full():
    """Serve the full snapshot (all sections).

    When everything is monitored (the default), the background collector's cache
    already holds the whole snapshot, so we return it verbatim (fast path) and
    collect once synchronously on a cold cache.  When only a subset is monitored,
    we assemble all sections — monitored ones from the cache, the rest on demand
    — so /dynamic_info always returns all sections regardless of config.
    """
    if not set(_get_monitored_sections()).issuperset(DYNAMIC_INFO_SECTIONS):
        return _respond_dynamic_sections(list(DYNAMIC_INFO_SECTIONS))

    with _DYNAMIC_INFO_CACHE_LOCK:
        data = _DYNAMIC_INFO_CACHE.get("data")

    if data is None:
        # Cold cache (first call before the collector has produced a snapshot).
        try:
            monitor = _get_resource_monitor()
            spm = _get_system_pressure_monitor()
            data = collect_dynamic_info(resource_monitor=monitor, system_pressure_monitor=spm)
            with _DYNAMIC_INFO_CACHE_LOCK:
                _DYNAMIC_INFO_CACHE["data"] = data
                _DYNAMIC_INFO_CACHE["ts"] = time.time()
        except Exception as e:
            logger.error(f"get_dynamic_info failed: {str(e)}")
            return construct_response(data={}, retcode=RetCode.EXCEPTION_ERROR, retmsg=str(e))

    payload = dict(data)
    payload["monitored_sections_updated_at"] = _get_monitored_sections_updated_at()
    return construct_response(data=payload, retmsg="Successfully retrieved dynamic system info")


def _respond_dynamic_sections(sections):
    """Serve a section-filtered dynamic_info response.

    For each requested section: if it is one of the configured
    ``monitored_sections``, serve it from the warm background cache (continuous,
    no per-request collection).  Otherwise collect it on demand — the modular
    path that never queries hardware the caller didn't ask for.  A short
    per-section TTL cache coalesces rapid on-demand polls.
    """
    monitored = set(_get_monitored_sections())
    result = {
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "monitored_sections_updated_at": _get_monitored_sections_updated_at(),
    }

    # Monitored sections: slice from the background-refreshed full cache.
    with _DYNAMIC_INFO_CACHE_LOCK:
        cached = _DYNAMIC_INFO_CACHE.get("data")
    on_demand = []
    for name in sections:
        if name in monitored and cached is not None and name in cached:
            result[name] = cached[name]
        else:
            # Not monitored, or monitored but the cache isn't warm yet (startup).
            on_demand.append(name)

    if not on_demand:
        return construct_response(data=result, retmsg="Successfully retrieved dynamic system info")

    # On-demand sections: reuse the per-section TTL cache to coalesce polls.
    now = time.time()
    stale = []
    with _DYNAMIC_SECTION_CACHE_LOCK:
        for name in on_demand:
            entry = _DYNAMIC_SECTION_CACHE.get(name)
            if entry is not None and (now - entry["ts"]) < _DYNAMIC_SECTION_TTL_SEC:
                result[name] = entry["data"]
            else:
                stale.append(name)

    if stale:
        try:
            # Only construct the heavyweight monitors when a requested section
            # actually needs them — a gpu/npu/cpu request touches neither.
            need_rm = any(s in _SECTIONS_NEED_RM for s in stale)
            need_spm = any(s in _SECTIONS_NEED_SPM for s in stale)
            monitor = _get_resource_monitor() if need_rm else None
            spm = _get_system_pressure_monitor() if need_spm else None
            fresh = collect_dynamic_info(
                resource_monitor=monitor,
                system_pressure_monitor=spm,
                sections=stale,
            )
        except Exception as e:
            logger.error(f"get_dynamic_info (sections) failed: {str(e)}")
            return construct_response(data={}, retcode=RetCode.EXCEPTION_ERROR, retmsg=str(e))

        ts = time.time()
        with _DYNAMIC_SECTION_CACHE_LOCK:
            for name in stale:
                if name in fresh:
                    _DYNAMIC_SECTION_CACHE[name] = {"data": fresh[name], "ts": ts}
                    result[name] = fresh[name]

    return construct_response(data=result, retmsg="Successfully retrieved dynamic system info")


@monitor_bp.route('/dynamic_info', methods=['GET'])
def get_dynamic_info():
    """Return a dynamic system metrics snapshot (background-cached).

    Returns the full snapshot by default.  An optional comma-separated
    ``sections`` query parameter restricts the payload to specific hardware
    groups, e.g. ``/dynamic_info?sections=cpu,gpu`` — this collects only those
    sections on demand and does not start the full-collection background
    thread.  Valid section names are listed in ``DYNAMIC_INFO_SECTIONS``.
    """
    sections, err = _parse_section_param(request.args.get('sections'), DYNAMIC_INFO_SECTIONS)
    if err:
        return construct_response(data={}, retcode=RetCode.ARGUMENT_ERROR, retmsg=err)

    if sections is None:
        _start_dynamic_info_auto_refresh()
        return _respond_dynamic_info_full()
    return _respond_dynamic_sections(sections)


@monitor_bp.route('/dynamic_info/<section>', methods=['GET'])
def get_dynamic_info_section(section):
    """Return a single hardware section of the dynamic snapshot.

    Convenience sub-resource for the common single-section case, e.g.
    ``/dynamic_info/cpu`` — collects only that section on demand.
    ``/dynamic_info/all`` returns the full snapshot (equivalent to
    ``/dynamic_info``, served from the background cache).
    """
    section = (section or '').strip().lower()
    if section == 'all':
        _start_dynamic_info_auto_refresh()
        return _respond_dynamic_info_full()
    if section not in DYNAMIC_INFO_SECTIONS:
        return construct_response(
            data={},
            retcode=RetCode.ARGUMENT_ERROR,
            retmsg=(f"Unknown section '{section}'. "
                    f"Valid sections: {', '.join(DYNAMIC_INFO_SECTIONS)}, all")
        )
    return _respond_dynamic_sections([section])


@monitor_bp.route('/history', methods=['GET'])
def get_history():
    _start_snapshot_cleanup_task()
    try:
        snapshot_type = (request.args.get('snapshot_type') or '').strip().lower()
        if snapshot_type in ('', 'all'):
            snapshot_type = None
        elif snapshot_type not in ('static', 'dynamic'):
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="snapshot_type must be one of: static, dynamic, all"
            )

        # Optional field projection: restrict each returned snapshot's `data`
        # payload to the requested sections.  This is where filtering pays off
        # most — a history query can return thousands of rows, each carrying a
        # full snapshot, so projecting to e.g. `cpu` alone can shrink the
        # response dramatically.  It only trims serialization/transfer; the
        # rows are still read from the DB in full.
        sections, err = _parse_section_param(request.args.get('sections'), HISTORY_SECTIONS)
        if err:
            return construct_response(data={}, retcode=RetCode.ARGUMENT_ERROR, retmsg=err)

        limit_raw = request.args.get('limit', '100')
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="limit must be an integer"
            )

        limit = max(1, min(limit, 20000))

        start_raw = (request.args.get('start_time') or '').strip()
        end_raw = (request.args.get('end_time') or '').strip()
        # range_seconds: client picks a preset window length but lets the
        # server anchor the window to its own clock.  This avoids "no data"
        # when a client's wall clock is skewed from the server (e.g. NTP
        # not synced) — the snapshots are written using server time, so
        # querying with a client-derived end_time can land in an empty
        # interval.  start_time/end_time still take precedence for custom
        # ranges where the user picked specific timestamps.
        range_seconds_raw = (request.args.get('range_seconds') or '').strip()

        start_time = None
        end_time = None

        if start_raw:
            try:
                start_time = int(start_raw)
            except (TypeError, ValueError):
                return construct_response(
                    data={},
                    retcode=RetCode.ARGUMENT_ERROR,
                    retmsg="start_time must be a unix timestamp (seconds)"
                )

        if end_raw:
            try:
                end_time = int(end_raw)
            except (TypeError, ValueError):
                return construct_response(
                    data={},
                    retcode=RetCode.ARGUMENT_ERROR,
                    retmsg="end_time must be a unix timestamp (seconds)"
                )

        if start_time is None and end_time is None and range_seconds_raw:
            try:
                range_seconds = int(range_seconds_raw)
            except (TypeError, ValueError):
                return construct_response(
                    data={},
                    retcode=RetCode.ARGUMENT_ERROR,
                    retmsg="range_seconds must be an integer"
                )
            if range_seconds <= 0:
                return construct_response(
                    data={},
                    retcode=RetCode.ARGUMENT_ERROR,
                    retmsg="range_seconds must be positive"
                )
            server_now = int(time.time())
            end_time = server_now
            start_time = server_now - range_seconds

        if start_time is not None and end_time is not None and start_time > end_time:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="start_time must be less than or equal to end_time"
            )

        rows = MonitorSnapshot.query_recent(
            snapshot_type=snapshot_type,
            limit=limit,
            start_time=start_time,
            end_time=end_time,
        )

        items = []
        for row in rows:
            payload = None
            if row.data_json:
                try:
                    payload = json.loads(row.data_json)
                except Exception:
                    payload = row.data_json

            if sections is not None:
                payload = _project_sections(payload, sections)

            items.append({
                'id': row.id,
                'snapshot_type': row.snapshot_type,
                'source': row.source,
                'collected_at': row.collected_at,
                'create_time': row.create_time,
                'update_time': row.update_time,
                'create_date': str(row.create_date) if row.create_date else None,
                'update_date': str(row.update_date) if row.update_date else None,
                'data': payload,
            })

        return construct_response(
            data={
                'snapshot_type': snapshot_type or 'all',
                'sections': sections,  # null = all fields returned
                'limit': limit,
                'start_time': start_time,
                'end_time': end_time,
                # server_time lets the client detect clock skew and warn the
                # user; it's the authoritative reference for "now" used to
                # resolve range_seconds above.
                'server_time': int(time.time()),
                'count': len(items),
                'items': items,
            },
            retmsg="Successfully retrieved monitor history"
        )
    except Exception as e:
        logger.error(f"get_history failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/history/retention', methods=['GET'])
def get_history_retention():
    """Return the current MonitorSnapshot retention period and allowed options."""
    _start_snapshot_cleanup_task()
    return construct_response(
        data={
            'retention_days': _load_retention_settings(),
            'default_days': _SNAPSHOT_RETENTION_DEFAULT_DAYS,
            'min_days': _SNAPSHOT_RETENTION_MIN_DAYS,
            'max_days': _SNAPSHOT_RETENTION_MAX_DAYS,
            'updated_at': _get_config_updated_at("retention"),
        },
        retmsg="Successfully retrieved retention settings"
    )


@monitor_bp.route('/history/retention', methods=['POST'])
def set_history_retention():
    """Update the MonitorSnapshot retention period with optimistic concurrency."""
    try:
        body = request.get_json(silent=True) or {}
        days_raw = body.get('retention_days')
        if days_raw is None:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="retention_days is required"
            )
        try:
            days = int(days_raw)
        except (TypeError, ValueError):
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="retention_days must be an integer"
            )
        if not (_SNAPSHOT_RETENTION_MIN_DAYS <= days <= _SNAPSHOT_RETENTION_MAX_DAYS):
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg=f"retention_days must be between {_SNAPSHOT_RETENTION_MIN_DAYS} and {_SNAPSHOT_RETENTION_MAX_DAYS}"
            )

        client_addr = request.remote_addr
        expected_ts = _coerce_expected_ts(body.get("expected_updated_at"))
        if expected_ts == -1:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="expected_updated_at must be an integer"
            )
        current_ts = _get_config_updated_at("retention")

        def _conflict_payload() -> Dict[str, Any]:
            return {
                "current": {
                    "retention_days": _load_retention_settings(),
                    "default_days": _SNAPSHOT_RETENTION_DEFAULT_DAYS,
                    "min_days": _SNAPSHOT_RETENTION_MIN_DAYS,
                    "max_days": _SNAPSHOT_RETENTION_MAX_DAYS,
                    "updated_at": current_ts,
                }
            }

        if expected_ts is None:
            if current_ts != 0:
                logger.info(
                    "retention conflict (no expected_updated_at) from %s; current_ts=%d",
                    client_addr, current_ts,
                )
                return construct_response(
                    data=_conflict_payload(),
                    retcode=RetCode.CONFLICT,
                    retmsg="Retention was modified by another client; please reload."
                )
        elif expected_ts != current_ts:
            logger.info(
                "retention conflict from %s: expected=%d current=%d",
                client_addr, expected_ts, current_ts,
            )
            return construct_response(
                data=_conflict_payload(),
                retcode=RetCode.CONFLICT,
                retmsg="Retention was modified by another client; please reload."
            )

        new_ts = _save_retention_settings(days)
        _start_snapshot_cleanup_task()

        # Run an immediate cleanup sweep so the new policy takes effect right away.
        deleted = MonitorSnapshot.delete_older_than(days)
        logger.info(
            "retention accepted from %s: days=%d updated_at=%d deleted=%d",
            client_addr, days, new_ts, deleted,
        )

        return construct_response(
            data={'retention_days': days, 'deleted': deleted, 'updated_at': new_ts},
            retmsg=f"Retention set to {days} day(s)"
        )
    except Exception as e:
        logger.error(f"set_history_retention failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/config/weights_top', methods=['GET'])
def get_weights_top():
    """Get current weights_top configuration."""
    try:
        from config.config import b_config
        weights = dict(b_config.weights_top or {})
        weights["updated_at"] = _get_config_updated_at("weights_top")
        return construct_response(
            data=weights,
            retmsg="Successfully retrieved weights_top configuration"
        )
    except Exception as e:
        logger.error(f"get_weights_top failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/config/weights_top', methods=['POST'])
def update_weights_top():
    """Update weights_top configuration with optimistic concurrency."""
    try:
        from config.config import b_config

        data = request.get_json()
        if not isinstance(data, dict):
            return construct_response(
                data={"success": False},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="Request body must be a JSON object"
            )

        # Validate input
        valid_keys = ['cpu', 'memory', 'gpu']
        updates = {}
        for key in valid_keys:
            if key in data:
                try:
                    updates[key] = int(data[key])
                    if updates[key] < 0:
                        return construct_response(
                            data={"success": False},
                            retcode=RetCode.ARGUMENT_ERROR,
                            retmsg=f"Weight for {key} must be non-negative"
                        )
                except (TypeError, ValueError):
                    return construct_response(
                        data={"success": False},
                        retcode=RetCode.ARGUMENT_ERROR,
                        retmsg=f"Invalid value for {key}, must be an integer"
                    )

        if not updates:
            return construct_response(
                data={"success": False},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="No valid weight updates provided"
            )

        client_addr = request.remote_addr
        expected_ts = _coerce_expected_ts(data.get("expected_updated_at"))
        if expected_ts == -1:
            return construct_response(
                data={"success": False},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="expected_updated_at must be an integer"
            )
        current_ts = _get_config_updated_at("weights_top")
        # None ⇒ caller did not send the field; only acceptable when server
        # side has also never been written (cold-start path).
        if expected_ts is None:
            if current_ts != 0:
                logger.info(
                    "weights_top conflict (no expected_updated_at) from %s; current_ts=%d",
                    client_addr, current_ts,
                )
                current = dict(b_config.weights_top or {})
                current["updated_at"] = current_ts
                return construct_response(
                    data={"success": False, "current": current},
                    retcode=RetCode.CONFLICT,
                    retmsg="Configuration was modified by another client; please reload."
                )
        elif expected_ts != current_ts:
            logger.info(
                "weights_top conflict from %s: expected=%d current=%d",
                client_addr, expected_ts, current_ts,
            )
            current = dict(b_config.weights_top or {})
            current["updated_at"] = current_ts
            return construct_response(
                data={"success": False, "current": current},
                retcode=RetCode.CONFLICT,
                retmsg="Configuration was modified by another client; please reload."
            )

        # Update the configuration.  update_config_section returns False both
        # for failures and for "no values changed" — treat the latter as a
        # successful no-op so that Save without edits doesn't surface as an
        # error.  We still bump updated_at so other clients reload past this
        # write.
        logger.info("Updating weights_top from %s: %s (expected_ts=%s)",
                    client_addr, updates, expected_ts)
        b_config.update_config_section('weights_top', updates)

        new_ts = _bump_config_updated_at("weights_top")
        updated = dict(b_config.weights_top or {})
        updated["updated_at"] = new_ts
        logger.info(
            "weights_top accepted from %s: %s -> updated_at=%d",
            client_addr, b_config.weights_top, new_ts,
        )
        return construct_response(
            data={
                "success": True,
                "updated_weights": updated,
                "updated_at": new_ts,
            },
            retmsg="Successfully updated weights_top configuration"
        )

    except Exception as e:
        logger.error(f"update_weights_top failed: {str(e)}")
        return construct_response(
            data={"success": False},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/config/passive_control', methods=['GET'])
def get_passive_control():
    """Get the current passive resource-control switch state."""
    try:
        from config.config import b_config
        prc = dict(b_config.passive_resource_control or {})
        return construct_response(
            data={
                "enabled": bool(prc.get("enabled", True)),
                "updated_at": _get_config_updated_at("passive_resource_control"),
            },
            retmsg="Successfully retrieved passive_resource_control configuration"
        )
    except Exception as e:
        logger.error(f"get_passive_control failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/config/monitored_sections', methods=['GET'])
def get_monitored_sections_config():
    """Get the effective dynamic-info monitored sections.

    Returns both:
      - sections: effective canonical section order currently used by monitor_api
      - configured_sections: raw b_config.monitored_sections (None means "all")
      - all_sections: full supported dynamic sections list
    """
    try:
        from config.config import b_config
        raw = b_config.monitored_sections
        configured_sections = list(raw) if isinstance(raw, list) else None
        return construct_response(
            data={
                "sections": _get_monitored_sections(),
                "configured_sections": configured_sections,
                "all_sections": list(DYNAMIC_INFO_SECTIONS),
                "updated_at": _get_monitored_sections_updated_at(),
            },
            retmsg="Successfully retrieved monitored_sections configuration"
        )
    except Exception as e:
        logger.error(f"get_monitored_sections_config failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/config/monitored_sections', methods=['POST'])
def update_monitored_sections_config():
    """Update the dynamic-info monitored sections with optimistic concurrency.

    Body: ``{ "sections": [...], "expected_updated_at": <int> }``.  An empty
    list is accepted and means "pure on-demand" (no background collector).
    Unknown section names are rejected.  On success the effective set changes
    and the change-driven updated_at advances.
    """
    try:
        from config.config import b_config

        data = request.get_json()
        if not isinstance(data, dict):
            return construct_response(
                data={"success": False},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="Request body must be a JSON object"
            )

        raw_sections = data.get("sections")
        if not isinstance(raw_sections, list):
            return construct_response(
                data={"success": False},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="sections must be a list"
            )

        valid = list(DYNAMIC_INFO_SECTIONS)
        seen = set()
        for item in raw_sections:
            name = str(item).strip().lower()
            if name not in valid:
                return construct_response(
                    data={"success": False},
                    retcode=RetCode.ARGUMENT_ERROR,
                    retmsg=f"Unknown section '{item}'. Valid sections: {', '.join(valid)}"
                )
            seen.add(name)
        # Persist in canonical order regardless of the order the client sent.
        normalized = [name for name in valid if name in seen]

        client_addr = request.remote_addr
        expected_ts = _coerce_expected_ts(data.get("expected_updated_at"))
        if expected_ts == -1:
            return construct_response(
                data={"success": False},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="expected_updated_at must be an integer"
            )
        current_ts = _get_monitored_sections_updated_at()

        def _conflict_payload() -> Dict[str, Any]:
            raw = b_config.monitored_sections
            return {
                "success": False,
                "current": {
                    "sections": _get_monitored_sections(),
                    "configured_sections": list(raw) if isinstance(raw, list) else None,
                    "all_sections": valid,
                    "updated_at": current_ts,
                },
            }

        if expected_ts is None:
            if current_ts != 0:
                logger.info(
                    "monitored_sections conflict (no expected_updated_at) from %s; current_ts=%d",
                    client_addr, current_ts,
                )
                return construct_response(
                    data=_conflict_payload(),
                    retcode=RetCode.CONFLICT,
                    retmsg="Configuration was modified by another client; please reload."
                )
        elif expected_ts != current_ts:
            logger.info(
                "monitored_sections conflict from %s: expected=%d current=%d",
                client_addr, expected_ts, current_ts,
            )
            return construct_response(
                data=_conflict_payload(),
                retcode=RetCode.CONFLICT,
                retmsg="Configuration was modified by another client; please reload."
            )

        logger.info("Updating monitored_sections from %s: %s (expected_ts=%s)",
                    client_addr, normalized, expected_ts)
        if not b_config.set_monitored_sections(normalized):
            return construct_response(
                data={"success": False},
                retcode=RetCode.OPERATING_ERROR,
                retmsg="Failed to persist monitored_sections (unsupported config layout?)"
            )

        # (Re)start the background collector if there is now something to
        # monitor; idempotent and a no-op when the new set is empty.
        _start_dynamic_info_auto_refresh()

        new_ts = _get_monitored_sections_updated_at()
        raw = b_config.monitored_sections
        logger.info(
            "monitored_sections accepted from %s: %s -> updated_at=%d",
            client_addr, normalized, new_ts,
        )
        return construct_response(
            data={
                "success": True,
                "sections": _get_monitored_sections(),
                "configured_sections": list(raw) if isinstance(raw, list) else None,
                "all_sections": valid,
                "updated_at": new_ts,
            },
            retmsg="Successfully updated monitored_sections configuration"
        )

    except Exception as e:
        logger.error(f"update_monitored_sections_config failed: {str(e)}")
        return construct_response(
            data={"success": False},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/config/passive_control', methods=['POST'])
def update_passive_control():
    """Toggle the passive resource-control switch with optimistic concurrency."""
    try:
        from config.config import b_config

        data = request.get_json()
        if not isinstance(data, dict):
            return construct_response(
                data={"success": False},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="Request body must be a JSON object"
            )

        if "enabled" not in data:
            return construct_response(
                data={"success": False},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="enabled is required"
            )
        # Accept native bools and the common string forms used by some clients.
        raw = data["enabled"]
        if isinstance(raw, bool):
            enabled = raw
        elif isinstance(raw, str):
            enabled = raw.strip().lower() in {"1", "true", "yes", "y", "on"}
        else:
            try:
                enabled = bool(int(raw))
            except (TypeError, ValueError):
                return construct_response(
                    data={"success": False},
                    retcode=RetCode.ARGUMENT_ERROR,
                    retmsg="enabled must be a boolean"
                )

        client_addr = request.remote_addr
        expected_ts = _coerce_expected_ts(data.get("expected_updated_at"))
        if expected_ts == -1:
            return construct_response(
                data={"success": False},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="expected_updated_at must be an integer"
            )
        current_ts = _get_config_updated_at("passive_resource_control")

        def _conflict_payload() -> Dict[str, Any]:
            prc = dict(b_config.passive_resource_control or {})
            return {
                "success": False,
                "current": {
                    "enabled": bool(prc.get("enabled", True)),
                    "updated_at": current_ts,
                },
            }

        if expected_ts is None:
            if current_ts != 0:
                logger.info(
                    "passive_control conflict (no expected_updated_at) from %s; current_ts=%d",
                    client_addr, current_ts,
                )
                return construct_response(
                    data=_conflict_payload(),
                    retcode=RetCode.CONFLICT,
                    retmsg="Configuration was modified by another client; please reload."
                )
        elif expected_ts != current_ts:
            logger.info(
                "passive_control conflict from %s: expected=%d current=%d",
                client_addr, expected_ts, current_ts,
            )
            return construct_response(
                data=_conflict_payload(),
                retcode=RetCode.CONFLICT,
                retmsg="Configuration was modified by another client; please reload."
            )

        logger.info("Updating passive_resource_control from %s: enabled=%s (expected_ts=%s)",
                    client_addr, enabled, expected_ts)
        b_config.update_config_section('passive_resource_control', {'enabled': enabled})

        new_ts = _bump_config_updated_at("passive_resource_control")
        logger.info(
            "passive_control accepted from %s: enabled=%s -> updated_at=%d",
            client_addr, enabled, new_ts,
        )
        return construct_response(
            data={
                "success": True,
                "enabled": enabled,
                "updated_at": new_ts,
            },
            retmsg="Successfully updated passive_resource_control configuration"
        )

    except Exception as e:
        logger.error(f"update_passive_control failed: {str(e)}")
        return construct_response(
            data={"success": False},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )

# ---------------------------------------------------------------------------
# Generic auto-control config get/set.
#
# thresholds / weights / pressure_detection / collection / limit_policy are all
# "read a config group, validate, persist" operations, so they share ONE
# parametrized endpoint instead of five near-duplicate ones.  Each section
# contributes a small spec (get / validate / write); the route handles the
# common optimistic-concurrency check and response envelope.  The pre-existing
# static routes (weights_top, passive_control, monitored_sections, retention)
# keep their dedicated handlers and take priority over this dynamic route.
# ---------------------------------------------------------------------------
_LIMIT_POLICY_MODES = ("combined", "separated")
_LIMIT_PRIORITIES = ("high", "medium", "low", "undefined")
_DISK_RATE_FIELDS = ("write", "read", "write_iops", "read_iops")
_THRESHOLD_KEYS = ("low", "medium", "high", "critical")
_PSI_WEIGHT_KEYS = ("cpu", "memory", "io")


def _cfg():
    """Fetch the live global config (local import matches the rest of this module)."""
    from config.config import b_config
    return b_config


# --- system_pressure: update interval + level cut-offs + weights + factor -----
def _get_system_pressure():
    cfg = _cfg()
    return {
        "regular_update_sys_pressure_time": getattr(cfg, "regular_update_sys_pressure_time", 5),
        "thresholds": dict(cfg.thresholds or {}),
        "weights": dict(cfg.weights or {}),
        "dominant_app_reduce_factor": getattr(cfg, "dominant_app_reduce_factor", None),
    }


def _validate_system_pressure(body):
    updates = {}

    if body.get("regular_update_sys_pressure_time") is not None:
        interval = float(body["regular_update_sys_pressure_time"])
        if not (1 <= interval <= 3600):
            raise ValueError("regular_update_sys_pressure_time must be within [1, 3600] seconds")
        updates["regular_update_sys_pressure_time"] = interval

    th_body = body.get("thresholds")
    if isinstance(th_body, dict):
        th = {}
        for key in _THRESHOLD_KEYS:
            if key in th_body and th_body[key] is not None:
                val = float(th_body[key])
                if not (0 < val <= 1):
                    raise ValueError(f"threshold {key} must be within (0, 1]")
                th[key] = val
        if th:
            merged = {**(_cfg().thresholds or {}), **th}
            ordered = [merged[k] for k in _THRESHOLD_KEYS if merged.get(k) is not None]
            if ordered != sorted(ordered):
                raise ValueError("thresholds must be ordered low <= medium <= high <= critical")
            updates["thresholds"] = th

    w_body = body.get("weights")
    if isinstance(w_body, dict):
        w = {}
        for key in _PSI_WEIGHT_KEYS:
            if key in w_body and w_body[key] is not None:
                val = int(w_body[key])
                if val < 0:
                    raise ValueError(f"{key} weight must be non-negative")
                w[key] = val
        if w:
            updates["weights"] = w

    if body.get("dominant_app_reduce_factor") is not None:
        val = float(body["dominant_app_reduce_factor"])
        if not (1 <= val <= 100):
            raise ValueError("dominant_app_reduce_factor must be within [1, 100]")
        updates["dominant_app_reduce_factor"] = val

    if not updates:
        raise ValueError("No valid system_pressure updates provided")
    return updates


def _write_system_pressure(updates):
    cfg = _cfg()
    if "thresholds" in updates:
        cfg.update_config_section("thresholds", updates["thresholds"])
    if "weights" in updates:
        cfg.update_config_section("weights", updates["weights"])
    scalars = {k: updates[k] for k in ("dominant_app_reduce_factor", "regular_update_sys_pressure_time") if k in updates}
    if scalars:
        cfg.update_top_level_scalars(scalars)
    return True


# --- disk_pressure: disk utilisation threshold -------------------------------
# (iowait / throughput thresholds stay config-only; see is_disk_io_stressed)
def _get_disk_pressure():
    return {"disk_utilization_threshold": getattr(_cfg(), "disk_utilization_threshold", None)}


def _validate_disk_pressure(body):
    key = "disk_utilization_threshold"
    if key not in body or body[key] is None:
        raise ValueError(f"{key} is required")
    val = float(body[key])
    if not (0 <= val <= 100):
        raise ValueError(f"{key} must be within [0, 100]")
    return {key: val}


# --- limit_policy: full nested policy + per-resource enable/rate --------------
def _limit_policy_snapshot():
    """Full limit_policy view (policy + per-resource enabled/rate)."""
    lp = _cfg().limit_policy or {}

    def _res(name):
        cfg = lp.get(name) or {}
        return {"enabled": bool(cfg.get("enabled", True)), "rate": dict(cfg.get("rate") or {})}

    return {
        "policy": lp.get("policy", "combined"),
        "cpu": _res("cpu"),
        "memory": _res("memory"),
        "disk_io": _res("disk_io"),
    }


def _validate_limit_policy(body):
    """Validate a (partial) nested limit_policy update from the UI."""
    updates = {}

    if "policy" in body:
        policy = str(body["policy"]).strip().lower()
        if policy not in _LIMIT_POLICY_MODES:
            raise ValueError(f"policy must be one of {', '.join(_LIMIT_POLICY_MODES)}")
        updates["policy"] = policy

    for res in ("cpu", "memory"):
        res_body = body.get(res)
        if not isinstance(res_body, dict):
            continue
        res_upd = {}
        if "enabled" in res_body:
            res_upd["enabled"] = bool(res_body["enabled"])
        if isinstance(res_body.get("rate"), dict):
            rate = {}
            for pri in _LIMIT_PRIORITIES:
                if res_body["rate"].get(pri) is not None:
                    val = float(res_body["rate"][pri])
                    if not (0 < val <= 1):
                        raise ValueError(f"{res} {pri} rate must be within (0, 1]")
                    rate[pri] = val
            if rate:
                res_upd["rate"] = rate
        if res_upd:
            updates[res] = res_upd

    disk_body = body.get("disk_io")
    if isinstance(disk_body, dict):
        disk_upd = {}
        if "enabled" in disk_body:
            disk_upd["enabled"] = bool(disk_body["enabled"])
        if isinstance(disk_body.get("rate"), dict):
            rate = {}
            for pri in _LIMIT_PRIORITIES:
                fields_body = disk_body["rate"].get(pri)
                if not isinstance(fields_body, dict):
                    continue
                fields = {}
                for key in _DISK_RATE_FIELDS:
                    if fields_body.get(key) is not None:
                        ival = int(float(fields_body[key]))
                        if ival < 1:
                            raise ValueError(f"disk_io {pri} {key} must be >= 1")
                        fields[key] = ival
                if fields:
                    rate[pri] = fields
            if rate:
                disk_upd["rate"] = rate
        if disk_upd:
            updates["disk_io"] = disk_upd

    if not updates:
        raise ValueError("No valid limit_policy updates provided")
    return updates


# section -> {get: ()->dict, validate: (body)->updates, write: (updates)->bool}
_CONFIG_SPECS = {
    "system_pressure": {
        "get": _get_system_pressure,
        "validate": _validate_system_pressure,
        "write": _write_system_pressure,
    },
    "disk_pressure": {
        "get": _get_disk_pressure,
        "validate": _validate_disk_pressure,
        "write": lambda u: _cfg().update_top_level_scalars(u),
    },
    "limit_policy": {
        "get": _limit_policy_snapshot,
        "validate": _validate_limit_policy,
        "write": lambda u: _cfg().set_limit_policy(u),
    },
}


@monitor_bp.route('/config/<section>', methods=['GET'])
def get_config_section(section):
    """Get one auto-control config group (see _CONFIG_SPECS)."""
    try:
        spec = _CONFIG_SPECS.get(section)
        if spec is None:
            return construct_response(
                data={}, retcode=RetCode.ARGUMENT_ERROR,
                retmsg=f"Unknown config section '{section}'. Valid: {', '.join(_CONFIG_SPECS)}")
        data = dict(spec["get"]())
        data["updated_at"] = _get_config_updated_at(section)
        return construct_response(data=data, retmsg=f"Successfully retrieved {section} configuration")
    except Exception as e:
        logger.error(f"get_config_section({section}) failed: {str(e)}")
        return construct_response(data={}, retcode=RetCode.EXCEPTION_ERROR, retmsg=str(e))


@monitor_bp.route('/config/<section>', methods=['POST'])
def update_config_generic(section):
    """Update one auto-control config group with optimistic concurrency."""
    try:
        spec = _CONFIG_SPECS.get(section)
        if spec is None:
            return construct_response(
                data={"success": False}, retcode=RetCode.ARGUMENT_ERROR,
                retmsg=f"Unknown config section '{section}'. Valid: {', '.join(_CONFIG_SPECS)}")

        body = request.get_json()
        if not isinstance(body, dict):
            return construct_response(data={"success": False}, retcode=RetCode.ARGUMENT_ERROR,
                                      retmsg="Request body must be a JSON object")
        try:
            updates = spec["validate"](body)
        except (ValueError, TypeError) as ve:
            return construct_response(data={"success": False}, retcode=RetCode.ARGUMENT_ERROR, retmsg=str(ve))

        def _current():
            cur = dict(spec["get"]())
            cur["updated_at"] = _get_config_updated_at(section)
            return cur

        conflict = _check_config_conflict(section, body.get("expected_updated_at"), _current)
        if conflict:
            return conflict

        logger.info("Updating config '%s' from %s: %s", section, request.remote_addr, updates)
        spec["write"](updates)
        new_ts = _bump_config_updated_at(section)
        data = dict(spec["get"]())
        data["updated_at"] = new_ts
        data["success"] = True
        return construct_response(data=data, retmsg=f"Successfully updated {section} configuration")
    except Exception as e:
        logger.error(f"update_config_generic({section}) failed: {str(e)}")
        return construct_response(data={"success": False}, retcode=RetCode.EXCEPTION_ERROR, retmsg=str(e))
