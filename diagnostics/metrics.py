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


def _resource_utilization_values(data):
    values = {}

    def add(label, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        values[label] = max(0, min(100, value))

    cpu = data.get("cpu") or {}
    memory = data.get("memory") or {}
    add("CPU", cpu.get("usage_total") if isinstance(cpu, dict) else None)
    add("Memory", memory.get("usage_percent") if isinstance(memory, dict) else None)

    gpu = data.get("gpu") or {}
    gpu_usage = gpu.get("gpu_usage") if isinstance(gpu, dict) else {}
    parsed = gpu_usage.get("parsed") if isinstance(gpu_usage, dict) else {}
    devices = parsed.get("devices") if isinstance(parsed, dict) else []
    if isinstance(devices, list):
        for index, device in enumerate(devices):
            if not isinstance(device, dict):
                continue
            device_type = str(device.get("dev_type") or "").lower()
            if "integrated" in device_type or "igpu" in device_type:
                label = "iGPU"
            elif "discrete" in device_type or "dgpu" in device_type:
                label = "dGPU"
            else:
                label = f"GPU{index}"
            add(label, device.get("utilization"))

    npu = data.get("npu") or {}
    npu_smi = npu.get("npu_smi") if isinstance(npu, dict) else {}
    disk = data.get("disk") or {}
    network = data.get("network") or {}
    npu_utilization = None
    if isinstance(npu_smi, dict):
        npu_utilization = npu_smi.get("utilization_percent")
        parsed_npu = npu_smi.get("parsed") or {}
        if npu_utilization is None and isinstance(parsed_npu, dict):
            npu_utilization = parsed_npu.get("utilization_percent", parsed_npu.get("utilization"))
    add("NPU", npu_utilization)
    add("Disk", disk.get("utilization") if isinstance(disk, dict) else None)
    add("Network", network.get("utilization_percent") if isinstance(network, dict) else None)
    return values


def read_resource_utilization(start_time, end_time, limit=2000, max_points=120):
    """Return resource averages, peaks, and a compact trend for diagnostics."""
    try:
        from db.DatabaseModel import MonitorSnapshot
    except Exception as exc:
        logger.warning("metrics monitor provider unavailable: %s", exc)
        return {"resources": [], "trend": [], "count": 0}

    rows = MonitorSnapshot.query_recent(
        snapshot_type="dynamic",
        start_time=int(start_time) if start_time is not None else None,
        end_time=int(end_time) if end_time is not None else None,
        limit=limit,
    )
    values = {}
    buckets = {}
    range_start = int(start_time or 0)
    bucket_width = max(1, (int(end_time or range_start) - range_start) / max(1, max_points))

    for row in rows:
        try:
            data = json.loads(row.data_json) if row.data_json else {}
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            continue
        sample_values = _resource_utilization_values(data)
        timestamp = int(row.create_time or range_start)
        bucket_index = max(0, min(max_points - 1, int((timestamp - range_start) / bucket_width)))
        bucket = buckets.setdefault(bucket_index, {"ts_epoch": int(range_start + bucket_index * bucket_width), "values": {}})
        for label, value in sample_values.items():
            total, count, peak = values.get(label, (0, 0, value))
            values[label] = (total + value, count + 1, max(peak, value))
            bucket_total, bucket_count = bucket["values"].get(label, (0, 0))
            bucket["values"][label] = (bucket_total + value, bucket_count + 1)

    resources = [
        {"label": label, "value": total / count, "peak": peak, "count": count}
        for label, (total, count, peak) in values.items()
        if count
    ]
    trend = [
        {
            "ts_epoch": bucket["ts_epoch"],
            "values": {
                label: total / count
                for label, (total, count) in bucket["values"].items()
                if count
            },
        }
        for _, bucket in sorted(buckets.items())
    ]
    return {"resources": resources, "trend": trend, "count": len(rows)}


def read_benchmark(job_id=None, start_time=None, end_time=None):
    """A benchmark run's KPI summary + (when locatable) sampling time series.

    Prefers the live artifact tree, and falls back to the compact summary frozen
    onto the job's completion event when that tree is gone -- a job deleted from
    the Results tab keeps its numbers but not its sampling timeline or raw logs.
    ``source`` says which was used ("disk" / "persisted" / None); ``summary`` is
    the per-case KPI object both paths share so a client renders one shape.

    Best-effort and guarded: benchmark may be absent. Reuses benchmark's own
    results helpers rather than re-parsing CSV layouts."""
    result = {"kpi": None, "series": [], "run": None, "source": None, "summary": None}
    try:
        from benchmark.service import results  # noqa: F401
    except Exception:
        results = None

    # Lifecycle events persist meta.run_name, which is the unique artifact prefix
    # generated for a job. Never substitute the newest run: wrong evidence is
    # worse than no evidence in a diagnostic context.
    if results is not None:
        try:
            run_dir = _run_dir_for_job(results, job_id)
            if run_dir is not None:
                result["run"] = run_dir.name
                medians = results._read_medians(run_dir)  # {case: {metric: value}}
                if medians:
                    result["kpi"] = medians
                result["series"] = _read_sampling_series(run_dir, start_time, end_time)
                # The same compact summary the persisted path returns, built from
                # the live tree so both look identical to the dashboard.
                result["summary"] = results.result_snapshot(run_name_for_job(job_id))
                if result["summary"] or result["kpi"]:
                    result["source"] = "disk"
        except Exception as exc:
            logger.debug("metrics benchmark provider partial: %s", exc)

    if result["summary"] is None:
        persisted = _persisted_result(job_id)
        if persisted is not None:
            result["summary"] = persisted
            result["run"] = result["run"] or persisted.get("run_name")
            result["source"] = "persisted"
    return result


def _persisted_result(job_id):
    """The KPI summary frozen on a benchmark job's completion event, if any.

    This is what lets a deleted job's results still answer "how did it do": the
    hook writes ``results.result_snapshot`` into the terminal event's attributes,
    and it is read back here once the on-disk run directory no longer exists.
    """
    if not job_id:
        return None
    try:
        from diagnostics import event_store
        events = event_store.query_events(
            job_id=job_id, category="workload.benchmark", limit=50)
    except Exception as exc:
        logger.debug("persisted benchmark result lookup failed: %s", exc)
        return None
    for event in events:
        result = (event.get("attributes") or {}).get("result")
        if isinstance(result, dict) and result.get("cases"):
            return result
    return None


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


def environment_snapshot(at_epoch):
    """Thermal/power/frequency/pressure snapshot nearest to completion time.

    Captures CPU temp, GPU power/freq, disk pressure, network utilization from
    the most recent MonitorSnapshot in [at_epoch-30, at_epoch], condensed into
    a flat small dict. Returns None if no sample found (not an error)."""
    if at_epoch is None:
        return None
    try:
        # Fetch the most recent "dynamic" snapshot within 30s before completion.
        monitor_data = read_monitor(start_time=at_epoch - 30, end_time=at_epoch, limit=1)
        if not monitor_data.get("series"):
            return None
        latest = monitor_data["series"][-1]  # read_monitor returns oldest-first
        data = latest.get("data") or {}
        # Extract key telemetry fields in a compact structure.
        snapshot = {}
        cpu = data.get("cpu") or {}
        if cpu.get("temperature_c") is not None:
            snapshot["cpu_temperature_c"] = cpu["temperature_c"]
        gpu = data.get("gpu") or {}
        gpu_usage = gpu.get("gpu_usage") or {}
        gpu_parsed = gpu_usage.get("parsed") or {}
        devices = gpu_parsed.get("devices") or []
        if devices:
            dev = devices[0]  # Primary GPU only, for brevity
            if dev.get("power_w"):
                snapshot["gpu_power_w"] = dev["power_w"]
            freqs = dev.get("freqs") or []
            if freqs:
                snapshot["gpu_cur_mhz"] = freqs[0].get("cur_mhz")
        disk = data.get("disk") or {}
        if disk.get("pressure_pct") is not None:
            snapshot["disk_pressure_pct"] = disk["pressure_pct"]
        network = data.get("network") or {}
        if network.get("utilization_percent") is not None:
            snapshot["network_util_pct"] = network["utilization_percent"]
        return snapshot or None
    except Exception as exc:
        logger.debug("environment_snapshot failed: %s", exc)
        return None


def read_aligned(job_id=None, start_time=None, end_time=None):
    """Both providers for a window, ready to overlay on a shared wall-clock x-axis.
    ``/diag/context`` calls this so the Agent gets job perf curve + same-window
    system resource / throttle / concurrency in one shot."""
    return {
        "monitor": read_monitor(start_time, end_time),
        "benchmark": read_benchmark(job_id=job_id, start_time=start_time, end_time=end_time),
        "window": {"from": start_time, "to": end_time},
    }
