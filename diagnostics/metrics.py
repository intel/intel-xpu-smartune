# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Unified metric read façade. Symmetric to LogSource but for time series. Two
# providers, both keyed on wall-clock so /diag/context can align them:
#   * monitor  -- system time series from MonitorSnapshot (CPU/mem/GPU/pressure)
#   * benchmark -- a run's 2Hz sampling CSV (time series) + windowed medians (KPI)
#
# The alignment is the whole point of answering "why is this job slow": the
# benchmark sampler uses epoch seconds and MonitorSnapshot uses collected_at on
# the same wall-clock, so a throughput dip lines up with the same-instant system
# state (CPU throttled / GPU contended / pressure critical). New metric sources
# implement the same read(scope, from, to) shape and plug in with zero changes.

import json
from pathlib import Path

from utils.logger import get_logger

logger = get_logger(__name__)


def read_monitor(start_time, end_time, limit=2000):
    """System metric series from MonitorSnapshot within [start_time, end_time]
    (epoch seconds). Reuses the existing indexed range query."""
    try:
        from db.DatabaseModel import MonitorSnapshot
    except Exception as exc:
        logger.warning("metrics monitor provider unavailable: %s", exc)
        return {"series": [], "count": 0}

    rows = MonitorSnapshot.query_recent(
        snapshot_type="dynamic",
        start_time=int(start_time) if start_time is not None else None,
        end_time=int(end_time) if end_time is not None else None,
        limit=limit,
    )
    series = []
    for r in rows:
        try:
            payload = json.loads(r.data_json) if r.data_json else {}
        except (ValueError, TypeError):
            payload = {}
        series.append({
            "collected_at": r.collected_at,
            "ts_epoch": int(r.create_time or 0),
            "data": payload,
        })
    # Oldest-first for charting.
    series.sort(key=lambda s: s["ts_epoch"])
    return {"series": series, "count": len(series)}


def read_benchmark(job_id=None, start_time=None, end_time=None):
    """A benchmark run's KPI medians + (when locatable) sampling time series.

    Best-effort and guarded: benchmark may be absent. Reuses benchmark's own
    results/artifact helpers rather than re-parsing CSV layouts."""
    result = {"kpi": None, "series": [], "run": None}
    try:
        from benchmark.service import results  # noqa: F401
    except Exception:
        return result

    # Lifecycle events persist meta.run_name, which is the unique artifact prefix
    # generated for a job. Never substitute the newest run: wrong evidence is
    # worse than no evidence in a diagnostic context.
    try:
        run_dir = _run_dir_for_job(results, job_id)
        if run_dir is not None:
            result["run"] = run_dir.name
            medians = results._read_medians(run_dir)  # {case: {metric: value}}
            if medians:
                result["kpi"] = medians
            result["series"] = _read_sampling_series(run_dir, start_time, end_time)
    except Exception as exc:
        logger.debug("metrics benchmark provider partial: %s", exc)
    return result


def run_name_for_job(job_id):
    """Return the persisted benchmark artifact prefix for ``job_id``, if known."""
    if not job_id:
        return None
    try:
        from diagnostics import event_store
        events = event_store.query_events(job_id=job_id, category="workload.benchmark", limit=50)
    except Exception as exc:
        logger.debug("benchmark job lookup failed: %s", exc)
        return None
    for event in events:
        meta = (event.get("attributes") or {}).get("meta") or {}
        run_name = meta.get("run_name")
        if isinstance(run_name, str) and run_name:
            return run_name
    return None


def _run_dir_for_job(results, job_id):
    """Locate the exact result directory for a persisted benchmark job."""
    run_name = run_name_for_job(job_id)
    if not run_name:
        return None
    try:
        from benchmark.service import env
        runs_root = env.paths().get("runs")
    except Exception:
        return None
    if not runs_root or not runs_root.is_dir():
        return None
    candidates = []
    for backend_dir in runs_root.iterdir():
        if not backend_dir.is_dir():
            continue
        for run_dir in backend_dir.iterdir():
            if run_dir.is_dir() and (run_dir.name == run_name or run_dir.name.startswith(f"{run_name}_")):
                candidates.append(run_dir)
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _read_sampling_series(run_dir, start_time, end_time):
    """Parse a run's 2Hz sampling CSV (column ``timestamp_s`` = epoch seconds),
    filtered to the window. Returns a list of per-sample dicts."""
    import csv

    sampling = None
    for name in ("sampling.csv", "sampler.csv", "resource_samples.csv"):
        cand = run_dir / name
        if cand.exists():
            sampling = cand
            break
    if sampling is None:
        return []
    out = []
    try:
        with open(sampling, newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                ts = row.get("timestamp_s") or row.get("timestamp") or row.get("ts")
                try:
                    ts_epoch = float(ts)
                except (TypeError, ValueError):
                    continue
                if start_time is not None and ts_epoch < start_time:
                    continue
                if end_time is not None and ts_epoch > end_time:
                    continue
                out.append({"ts_epoch": ts_epoch, **row})
    except OSError as exc:
        logger.debug("benchmark sampling read failed: %s", exc)
    return out


def read_aligned(job_id=None, start_time=None, end_time=None):
    """Both providers for a window, ready to overlay on a shared wall-clock x-axis.
    ``/diag/context`` calls this so the Agent gets job perf curve + same-window
    system resource / throttle / concurrency in one shot."""
    return {
        "monitor": read_monitor(start_time, end_time),
        "benchmark": read_benchmark(job_id=job_id, start_time=start_time, end_time=end_time),
        "window": {"from": start_time, "to": end_time},
    }
