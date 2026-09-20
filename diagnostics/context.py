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
        return int(datetime.fromisoformat(ts_iso).timestamp())
    except (ValueError, TypeError):
        return None


def _resolve_window(scope_kind, scope_value, start_time, end_time):
    """Resolve explicit or event-derived evidence bounds for a context scope."""
    from diagnostics import event_store

    now = int(time.time())
    if start_time is None or end_time is None:
        filters = {scope_kind: scope_value} if scope_kind in ("job_id", "app_id") else {}
        rows = event_store.query_events(limit=500, **filters)
        timestamps = [timestamp for timestamp in (_iso_to_epoch(row.get("ts_utc")) for row in rows) if timestamp]
        if timestamps:
            start_time = start_time or (min(timestamps) - 60)
            end_time = end_time or (max(timestamps) + 60)
        else:
            end_time = end_time or now
            start_time = start_time or (end_time - _DEFAULT_WINDOW_SECONDS)
    return int(start_time), int(end_time)


def assemble_context(*, scope_kind, scope_value, start_time=None, end_time=None):
    """Return aligned evidence for a job, app, or explicit time window."""
    from diagnostics import event_store, log_query, metrics

    start_time, end_time = _resolve_window(scope_kind, scope_value, start_time, end_time)
    scope = {"kind": scope_kind, "value": scope_value}
    scope_filter = {scope_kind: scope_value} if scope_kind in ("job_id", "app_id") else {}
    events = event_store.query_events(
        start_time=start_time, end_time=end_time, limit=500, **scope_filter)
    control_actions = event_store.query_events(
        category="platform.control", start_time=start_time, end_time=end_time, limit=200,
        **({"app_id": scope_value} if scope_kind == "app_id" else {}))
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
    assembled = {
        "scope": scope,
        "window": {"from": start_time, "to": end_time},
        "boot_id": read_boot_id(),
        "events": events,
        "control_actions": control_actions,
        "concurrent_jobs": concurrent_jobs,
        "alerts": event_store.query_alerts(active_only=True, limit=100),
        "metrics": metrics_window,
        "logs": logs,
    }
    from diagnostics import insights

    assembled["findings"] = insights.evaluate_rules(assembled)
    return assembled


def assemble_findings(*, start_time, end_time):
    """Evaluate window findings with only the evidence required by insight rules."""
    from diagnostics import event_store, insights, metrics

    events = event_store.query_events(
        start_time=int(start_time), end_time=int(end_time), limit=500)
    assembled = {
        "window": {"from": int(start_time), "to": int(end_time)},
        "events": events,
        "control_actions": [
            event for event in events if event.get("category") == "platform.control"
        ],
        "metrics": {
            "monitor": metrics.read_monitor(int(start_time), int(end_time)),
        },
    }
    return insights.evaluate_rules(assembled)