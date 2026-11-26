# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Assemble correlated diagnostic evidence for an app, job, or time window."""

import time
from datetime import datetime

from utils.logger import get_logger

from diagnostics.system_info import read_boot_id

logger = get_logger(__name__)

_DEFAULT_WINDOW_SECONDS = 15 * 60


def _iso_to_epoch(ts_iso):
    if not ts_iso:
        return None
    try:
        return int(datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError, AttributeError):
        return None


_TERMINAL_JOB_STATUSES = ("done", "failed", "cancelled")
# Window bound for a job with no terminal event (an orphan if the service died under it).
_MAX_OPEN_JOB_SECONDS = 24 * 3600


def _job_bounds(rows, timestamps, now):
    """(start, end) epoch of a benchmark job from its recorded started_at / finished_at, else its
    event times; a running job ends now."""
    starts, ends, finished = list(timestamps), [], False
    for row in rows:
        attributes = row.get("attributes") or {}
        if not isinstance(attributes, dict):
            continue
        if isinstance(attributes.get("started_at"), (int, float)):
            starts.append(attributes["started_at"])
        if isinstance(attributes.get("finished_at"), (int, float)):
            ends.append(attributes["finished_at"])
        finished = finished or attributes.get("status") in _TERMINAL_JOB_STATUSES
    start = int(min(starts)) if starts else now
    if finished:
        return start, int(max(ends + timestamps, default=start)) + 60
    return start, min(now, start + _MAX_OPEN_JOB_SECONDS)


def _event_times(rows):
    return [timestamp for timestamp in (_iso_to_epoch(row.get("ts_utc")) for row in rows) if timestamp]


def job_window(job_id):
    """(start, end) epoch of a benchmark job from its events, with a minute of lead-in; None if unknown."""
    from diagnostics import event_store

    rows = event_store.query_events(limit=500, job_id=job_id)
    if not rows:
        return None
    start, end = _job_bounds(rows, _event_times(rows), int(time.time()))
    return start - 60, end


def _resolve_window(scope_kind, scope_value, start_time, end_time):
    """Resolve explicit or event-derived evidence bounds for a context scope."""
    from diagnostics import event_store

    now = int(time.time())
    if start_time is None or end_time is None:
        job = job_window(scope_value) if scope_kind == "job_id" else None
        if job is not None:
            start_time = start_time or job[0]
            end_time = end_time or job[1]
            return int(start_time), int(end_time)
        filters = {scope_kind: scope_value} if scope_kind in ("job_id", "app_id") else {}
        rows = event_store.query_events(limit=500, **filters)
        timestamps = _event_times(rows)
        if timestamps:
            start_time = start_time or (min(timestamps) - 60)
            end_time = end_time or (max(timestamps) + 60)
        else:
            end_time = end_time or now
            start_time = start_time or (end_time - _DEFAULT_WINDOW_SECONDS)
    return int(start_time), int(end_time)


def _host_level_events(event_store, start_time, end_time):
    """Warning+ events in the window with no app_id / job_id (OOM kills, GPU hangs, crashes),
    which a scoped query would miss."""
    result = event_store.query_window_events(
        start_time=start_time, end_time=end_time, limit=1,
        keep_severities=event_store.IMPORTANT_SEVERITIES)
    return [event for event in result["events"]
            if event.get("severity") in event_store.IMPORTANT_SEVERITIES
            and not event.get("app_id") and not event.get("job_id")]


# Slack around the window: STOPPED and STARTED land just outside a run's samples.
_LIFECYCLE_SLACK_SECONDS = 60


def _service_lifecycle(event_store, start_time, end_time):
    """Service start / stop / crash events around the window, oldest first.

    Queried on their own: being info-level and host-wide, scoped or bounded queries leave them out."""
    from diagnostics import service_lifecycle

    rows = event_store.query_events(
        event_types=[service_lifecycle.STARTED, service_lifecycle.STOPPED, service_lifecycle.CRASHED],
        start_time=int(start_time) - _LIFECYCLE_SLACK_SECONDS,
        end_time=int(end_time) + _LIFECYCLE_SLACK_SECONDS, limit=200)
    return sorted(rows, key=lambda e: _iso_to_epoch(e.get("ts_utc")) or 0)


_RECLAIM_EVENT_TYPES = ("RESOURCE_MEMORY_RECLAIM_APPLIED", "RESOURCE_MEMORY_RECLAIM_FAILED",
                        "RESOURCE_MEMORY_RECLAIM_EXHAUSTED")


def _is_control_action(event):
    """Whether SmarTune acted on an app or device: control changes and memory reclaim.
    A refused request changed nothing."""
    if event.get("event_type") in _RECLAIM_EVENT_TYPES:
        return True
    return event.get("category") == "platform.control" and event.get("impact") != "refused"


def _control_actions(event_store, start_time, end_time, app_id=None):
    scope = {"app_id": app_id} if app_id else {}
    rows = _merge_events(
        event_store.query_events(category="platform.control", start_time=start_time,
                                 end_time=end_time, limit=200, **scope),
        event_store.query_events(event_types=_RECLAIM_EVENT_TYPES, start_time=start_time,
                                 end_time=end_time, limit=200, **scope))
    return [event for event in rows if _is_control_action(event)]


def _merge_events(*groups):
    merged = {}
    for group in groups:
        for event in group:
            merged.setdefault(event.get("event_id"), event)
    # ts_utc may mix offsets, so order by the parsed instant, not the string.
    return sorted(merged.values(), key=lambda e: _iso_to_epoch(e.get("ts_utc")) or 0,
                  reverse=True)


def assemble_context(*, scope_kind, scope_value, start_time=None, end_time=None, full_series=False):
    """Return aligned evidence for a job, app, or explicit time window.
    ``full_series`` returns raw monitor snapshots instead of the compact series."""
    from diagnostics import event_store, log_query, metrics

    start_time, end_time = _resolve_window(scope_kind, scope_value, start_time, end_time)
    scope = {"kind": scope_kind, "value": scope_value}
    scope_filter = {scope_kind: scope_value} if scope_kind in ("job_id", "app_id") else {}
    window = event_store.query_window_events(
        start_time=start_time, end_time=end_time, limit=500, **scope_filter)
    events = window["events"]
    if scope_filter:
        events = _merge_events(events, _host_level_events(event_store, start_time, end_time))
    control_actions = _control_actions(
        event_store, start_time, end_time, scope_value if scope_kind == "app_id" else None)
    bench_events = event_store.query_events(
        category="workload.benchmark", start_time=start_time, end_time=end_time, limit=200)
    concurrent_jobs = sorted({event["job_id"] for event in bench_events if event.get("job_id")})
    metrics_window = metrics.read_aligned(
        job_id=(scope_value if scope_kind == "job_id" else None),
        start_time=start_time, end_time=end_time)
    log_sources = ["smartune"]
    if scope_kind == "job_id":
        log_sources.append("benchmark")
    logs = log_query.query(
        sources=log_sources, start_time=start_time, end_time=end_time,
        job_id=(scope_value if scope_kind == "job_id" else None), limit=200)
    # Kernel warnings and above (OOM reports, GPU resets, driver faults) for the same window.
    kernel_logs = log_query.query(
        sources=["journal"], start_time=start_time, end_time=end_time,
        level="warning", kernel_only=True, limit=200)
    logs = _merge_log_results(logs, kernel_logs)
    assembled = {
        "scope": scope,
        "window": {"from": start_time, "to": end_time},
        "boot_id": read_boot_id(),
        "events": events,
        "events_truncated": window["truncated"],
        "control_actions": control_actions,
        "service_lifecycle": _service_lifecycle(event_store, start_time, end_time),
        "concurrent_jobs": concurrent_jobs,
        # Alert occurrences that overlap the evidence window (active or since
        # resolved), not whatever happens to be active now.
        "alerts": event_store.query_alerts(
            active_only=False, start_time=start_time, end_time=end_time, limit=100),
        "metrics": metrics_window,
        "logs": logs,
    }
    from diagnostics import insights

    # Findings are judged on the full series, before it is compacted for the response.
    assembled["findings"] = insights.evaluate_rules(assembled)
    if not full_series:
        metrics_window["monitor"] = compact_monitor(metrics_window.get("monitor") or {})
    return assembled


# Points and fields the context response keeps of the monitor series; the raw one runs to megabytes.
_COMPACT_POINTS = 240
_PRESSURE_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_COMPACT_FIELDS = {
    "cpu": ("usage_total", "temperature_c", "package_tjmax_c"),
    "memory": ("usage_percent", "available_gb", "swap_usage_percent"),
    "pressure": ("score", "level", "cpu", "memory", "io", "network_pressure_pct"),
    "disk": ("pressure_pct",),
}
# Fields where the low end is the one that matters within a bucket.
_KEEP_MIN = {("memory", "available_gb")}


def _merge_value(section, field, current, value):
    if value is None:
        return current
    if current is None:
        return value
    if field == "level":
        return value if _PRESSURE_RANK.get(value, -1) > _PRESSURE_RANK.get(current, -1) else current
    if not isinstance(value, (int, float)) or not isinstance(current, (int, float)):
        return current
    return min(current, value) if (section, field) in _KEEP_MIN else max(current, value)


def compact_monitor(monitor):
    """Downsample to at most _COMPACT_POINTS samples of _COMPACT_FIELDS, keeping each bucket's worst
    value (highest load and pressure, lowest available memory) so short spikes survive."""
    series = monitor.get("series") or []
    if not series:
        return {**monitor, "series": [], "count": 0, "detail": "summary", "sampled_from": 0}
    start, end = series[0].get("ts_epoch") or 0, series[-1].get("ts_epoch") or 0
    width = max(1.0, (end - start + 1) / _COMPACT_POINTS)
    buckets = {}
    for sample in series:
        ts = sample.get("ts_epoch") or 0
        index = min(_COMPACT_POINTS - 1, int((ts - start) / width))
        bucket = buckets.setdefault(index, {"ts_epoch": ts, "data": {}})
        bucket["ts_epoch"] = max(bucket["ts_epoch"], ts)
        data = sample.get("data") or {}
        for section, fields in _COMPACT_FIELDS.items():
            source = data.get(section)
            if not isinstance(source, dict):
                continue
            target = bucket["data"].setdefault(section, {})
            for field in fields:
                target[field] = _merge_value(section, field, target.get(field), source.get(field))
    compact = [buckets[index] for index in sorted(buckets)]
    return {"series": compact, "count": len(compact), "detail": "summary",
            "sampled_from": len(series), "step_seconds": max(int(width), monitor.get("step_seconds") or 0)}


def _merge_log_results(primary, extra):
    """Fold a second log_query result into the first, newest first."""
    records = sorted((primary.get("records") or []) + (extra.get("records") or []),
                     key=lambda r: r.get("ts_epoch") or 0, reverse=True)
    sources = list(dict.fromkeys((primary.get("sources") or []) + (extra.get("sources") or [])))
    failed = list(dict.fromkeys((primary.get("failed_sources") or []) + (extra.get("failed_sources") or [])))
    return {**primary, "records": records, "sources": sources, "failed_sources": failed,
            "count": len(records),
            "truncated": bool(primary.get("truncated") or extra.get("truncated"))}


def assemble_findings(*, start_time, end_time):
    """Evaluate window findings with only the evidence required by insight rules."""
    from diagnostics import event_store, insights, metrics

    events = event_store.query_window_events(
        start_time=int(start_time), end_time=int(end_time), limit=500)["events"]
    assembled = {
        "window": {"from": int(start_time), "to": int(end_time)},
        "events": events,
        "control_actions": [event for event in events if _is_control_action(event)],
        "service_lifecycle": _service_lifecycle(event_store, start_time, end_time),
        "metrics": {
            "monitor": metrics.read_monitor(int(start_time), int(end_time)),
        },
    }
    return insights.evaluate_rules(assembled)