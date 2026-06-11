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
from monitor.system_info import collect_static_info, collect_dynamic_info
from utils.http_utils import RetCode, construct_response
from utils.logger import logger

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


def _start_dynamic_info_auto_refresh() -> None:
    """Start the background thread that pre-caches dynamic_info.

    Idempotent: calling more than once has no effect.  The thread collects
    fresh metrics every ``_DYNAMIC_INFO_REFRESH_INTERVAL_SEC`` seconds and
    stores the result in ``_DYNAMIC_INFO_CACHE`` so that API requests return
    immediately without blocking on expensive metric collection.
    """
    global _dynamic_info_refresh_started
    with _dynamic_info_refresh_start_lock:
        if _dynamic_info_refresh_started:
            return
        _dynamic_info_refresh_started = True

    def refresh_loop() -> None:
        while True:
            loop_start = time.time()
            try:
                monitor = _get_resource_monitor()
                spm = _get_system_pressure_monitor()
                data = collect_dynamic_info(
                    resource_monitor=monitor,
                    system_pressure_monitor=spm,
                )
                with _DYNAMIC_INFO_CACHE_LOCK:
                    _DYNAMIC_INFO_CACHE["data"] = data
                    _DYNAMIC_INFO_CACHE["ts"] = time.time()
            except Exception as exc:
                logger.debug("dynamic_info auto-refresh error: %s", exc)
            elapsed = time.time() - loop_start
            # Always sleep at least 0.1 s to avoid a tight loop if collection
            # finishes faster than expected (e.g. an exception path).
            time.sleep(max(0.1, _DYNAMIC_INFO_REFRESH_INTERVAL_SEC - elapsed))

    t = threading.Thread(target=refresh_loop, daemon=True, name="dynamic-info-refresh")
    t.start()


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
    "weights_top": "weights_top_updated_at",
    "retention":   "retention_updated_at",
}


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




@monitor_bp.route('/app_resource_stats', methods=['GET'])
def get_app_resource_stats():
    """
    Return per-application CPU/memory resource usage for the App Resources dashboard tab.

    Unlike /top_consumers (which returns only the top-1 process and applies system-pressure
    threshold filtering), this endpoint returns the top N applications by combined CPU/memory
    score without any threshold gate, making it suitable for general resource display.

    Query params:
        n (int, optional): Number of top apps to return. Default: 10.

    Response data:
        {
            "apps": [
                {
                    "app_id": <str>,
                    "app_name": <str>,
                    "pid": <int>,
                    "process_name": <str>,
                    "cmdline": <str>,
                    "cpu_usage": <float>,      # fraction of total CPU capacity (0-1)
                    "memory_mb": <float>,      # resident memory in MB
                    "io_read_rate": <float>,   # disk read rate in MB/s
                    "io_write_rate": <float>,  # disk write rate in MB/s
                    "score": <float>,
                    "gpu_util": <float>,       # peak GPU engine utilisation % (0-100); 0 when GPU not in use
                    "gpu_mem_mb": <float>      # GPU memory used in MB (drm-memory-* from /proc fdinfo)
                },
                ...
            ]
        }
    """
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
    """
    Return per-application disk I/O usage for the App Resources dashboard tab.

    Unlike /top_disk_io_consumers (which returns only the top-1 process), this endpoint
    returns the top N applications by disk I/O score, suitable for general display.

    Query params:
        n (int, optional): Number of top apps to return. Default: 10.

    Response data:
        {
            "apps": [
                {
                    "pid": <int>,
                    "name": <str>,          # dominant process name (highest IO contributor in cgroup)
                    "app_name": <str>,      # human-readable app/cgroup name
                    "cmdline": <str>,
                    "io_read_rate": <float>,    # read throughput in MB/s
                    "io_write_rate": <float>,   # write throughput in MB/s
                    "io_read_iops": <float>,    # read operations per second
                    "io_write_iops": <float>,   # write operations per second
                    "score": <float>
                },
                ...
            ]
        }
    """
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
    """
    Return a list of all running processes sorted by CPU usage, similar to the top command.

    Response data:
        {
            "count": <int>,
            "processes": [
                {
                    "pid": <int>,
                    "name": <str>,
                    "username": <str>,
                    "cpu_percent": <float>,    # CPU usage percent
                    "memory_percent": <float>, # memory usage percent
                    "mem_rss_kb": <float>,     # resident set size in KB
                    "status": <str>,           # process status (running/sleeping/...)
                    "cmdline": <str>           # full command line
                },
                ...
            ]
        }
    """
    try:
        procs = []
        attrs = ['pid', 'name', 'username', 'cpu_percent', 'memory_percent',
                 'status', 'cmdline', 'memory_info']
        for p in psutil.process_iter(attrs):
            try:
                info = p.info
                mem_rss_kb = round(info['memory_info'].rss / 1024, 0) if info.get('memory_info') else 0
                cmdline_parts = info.get('cmdline') or []
                cmdline = ' '.join(cmdline_parts) if cmdline_parts else (info.get('name') or '')
                procs.append({
                    'pid': info['pid'],
                    'name': info.get('name') or '',
                    'username': info.get('username') or '',
                    'cpu_percent': round(info.get('cpu_percent') or 0, 1),
                    'memory_percent': round(info.get('memory_percent') or 0, 2),
                    'mem_rss_kb': mem_rss_kb,
                    'status': info.get('status') or '',
                    'cmdline': cmdline,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
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


@monitor_bp.route('/static_info', methods=['GET'])
def get_static_info():
    """
    Return static system configuration info.

    Response data:
        {
            "bios": { ... },
            "os": { ... },
            "driver": { ... },
            "cpu": { ... },
            "memory": { ... },
            "io": { ... },
            "gpu": { ... },
            "npu": { ... },
            "collected_at": <str>
        }
    """
    try:
        force_raw = (request.args.get('force_refresh') or '').strip().lower()
        force_refresh = force_raw in {'1', 'true', 'yes', 'y', 'on'}
        data = collect_static_info(force_refresh=force_refresh)
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


@monitor_bp.route('/dynamic_info', methods=['GET'])
def get_dynamic_info():
    """
    Return dynamic system metrics snapshot.

    The background auto-refresh thread keeps the cache up to date so this
    endpoint responds immediately without blocking on metric collection.  On
    the very first request (before the cache is populated) it falls back to
    collecting synchronously.

    Response data:
        {
            "cpu": { ... },
            "memory": { ... },
            "pressure": { ... },
            "network": { ... },
            "disk": { ... },
            "gpu": { ... },
            "npu": { ... },
            "collected_at": <str>
        }
    """
    _start_dynamic_info_auto_refresh()

    with _DYNAMIC_INFO_CACHE_LOCK:
        data = _DYNAMIC_INFO_CACHE.get("data")

    if data is None:
        # Cache not yet populated — collect synchronously on first call.
        try:
            monitor = _get_resource_monitor()
            spm = _get_system_pressure_monitor()
            data = collect_dynamic_info(resource_monitor=monitor, system_pressure_monitor=spm)
            with _DYNAMIC_INFO_CACHE_LOCK:
                _DYNAMIC_INFO_CACHE["data"] = data
                _DYNAMIC_INFO_CACHE["ts"] = time.time()
        except Exception as e:
            logger.error(f"get_dynamic_info failed: {str(e)}")
            return construct_response(
                data={},
                retcode=RetCode.EXCEPTION_ERROR,
                retmsg=str(e)
            )

    return construct_response(
        data=data,
        retmsg="Successfully retrieved dynamic system info"
    )


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
    """Return the current MonitorSnapshot retention period and allowed options.

    Response data:
        {
            "retention_days": <int>,        // current setting (1-7)
            "default_days": <int>,          // built-in default (3)
            "min_days": <int>,              // minimum allowed (1)
            "max_days": <int>,              // maximum allowed (7)
            "updated_at": <int>             // unix seconds; 0 if never written via API
        }
    """
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
    """Update the MonitorSnapshot retention period and optionally trigger an immediate cleanup.

    Request body:
        {
            "retention_days": <int>,                // required, 1-7
            "expected_updated_at": <int> (optional) // unix ts from prior GET
        }

    Optimistic concurrency: see /config/weights_top — same scheme, mismatch
    is reported with ``RetCode.CONFLICT`` and a ``current`` payload.

    Response (success):
        {
            "retention_days": <int>,
            "deleted": <int>,         // rows deleted by the immediate cleanup sweep
            "updated_at": <int>
        }
    Response (409 conflict):
        {
            "current": {
                "retention_days": <int>,
                "updated_at": <int>,
                ...
            }
        }
    """
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

    def set_limited_app_dominant(self, is_dominant: bool):
        """Set whether the rate-limited app is currently dominant."""
        if self._is_limited_app_dominant != is_dominant:
            self._is_limited_app_dominant = is_dominant

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
            score = self.analyzer.calculate_pressure_score(
                psi_data,
                usage_data,
                self._is_limited_app_dominant
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
        still used by display/point-in-time paths (UI, appIntercept) that must NOT
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


@monitor_bp.route('/config/weights_top', methods=['GET'])
def get_weights_top():
    """Get current weights_top configuration.

    Response:
        {
            "cpu": int,
            "memory": int,
            "io": int,
            "gpu": int,
            "updated_at": int        // unix seconds; 0 if never written via API
        }
    """
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
    """Update weights_top configuration.

    Request body:
        {
            "cpu": int (optional),
            "memory": int (optional),
            "gpu": int (optional),
            "expected_updated_at": int (optional)   // unix ts from prior GET
        }

    Optimistic concurrency: when ``expected_updated_at`` is provided, it must
    match the server's current updated_at; otherwise the request is rejected
    with ``RetCode.CONFLICT`` and the response payload includes the latest
    state so the client can prompt the user to reload.  Omitting the field
    is allowed only when the server side has never been written through this
    API (i.e. updated_at == 0), so first-time saves still work.

    Note: I/O weight is not configurable via this API as Disk I/O ranking
    uses pure throughput (MB/s) without weight adjustment.

    Response (success):
        {
            "success": bool,
            "updated_weights": dict,
            "updated_at": int
        }
    Response (409 conflict):
        {
            "success": false,
            "current": dict,        // latest weights including updated_at
        }
    """
    try:
        from config.config import b_config

        data = request.get_json()
        if not isinstance(data, dict):
            return construct_response(
                data={"success": False},
                retcode=RetCode.PARAM_ERROR,
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
                            retcode=RetCode.PARAM_ERROR,
                            retmsg=f"Weight for {key} must be non-negative"
                        )
                except (TypeError, ValueError):
                    return construct_response(
                        data={"success": False},
                        retcode=RetCode.PARAM_ERROR,
                        retmsg=f"Invalid value for {key}, must be an integer"
                    )

        if not updates:
            return construct_response(
                data={"success": False},
                retcode=RetCode.PARAM_ERROR,
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
