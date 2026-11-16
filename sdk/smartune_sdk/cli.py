# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# `smartune` command line: queries over the SmarTune REST API, with output
# trimmed for agents (and humans) to read, plus the `smartune control` balancer
# actions (control.py), which only print a plan unless given --yes. Every command
# prints one JSON document on stdout -- the result, or {"error", "hint"} on
# failure -- and exits with a code from client.EXIT_*.

import argparse
import json
import sys
import time

from . import __version__
from . import control
from .client import EXIT_USAGE, SmartuneClient, SmartuneError
from .doctor import run_doctor
from .shape import (DEFAULT_MAX_BYTES, compact, downsample, dumps, fit, iso,
                    localize_times, numeric_stats, parse_duration, pick, window)

STATUS_SECTIONS = ("cpu", "memory", "pressure", "gpu", "npu", "disk", "network")
DEFAULT_STATUS_SECTIONS = ("cpu", "memory", "pressure", "gpu", "npu", "disk")
HISTORY_SECTIONS = ("cpu", "memory", "pressure", "gpu", "npu")
STATIC_SECTIONS = ("bios", "os", "driver", "cpu", "memory", "network", "disk", "gpu", "npu")
# Log sources registered by diagnostics/sources/__init__.py. The server silently
# skips a name it does not know, so the CLI rejects one instead.
LOG_SOURCES = ("smartune", "benchmark", "journal")
BENCH_BACKENDS = ("genai", "ovms", "app")  # benchmark/service/results.py BACKENDS
HISTORY_CADENCE_S = 5  # monitor/metrics/history.py persists one dynamic row per 5 s


# -- metric extraction (shared by live status and history rows) ---------------

def _gpu_devices(gpu):
    usage = (gpu or {}).get("gpu_usage") or {}
    return ((usage.get("parsed") or {}).get("devices")) or []


def _gpu_util(device):
    """Busiest engine, which is how the dashboard defines GPU utilisation."""
    if device.get("utilization") is not None:
        return device["utilization"]
    values = [v for v in (device.get("engine_util") or {}).values() if isinstance(v, (int, float))]
    return max(values) if values else None


def _npu_metrics(npu):
    """NPU numbers from either a live npu_smi block (JSON in `raw`) or a history row."""
    smi = (npu or {}).get("npu_smi") or {}
    if not smi.get("available"):
        return {"available": False, "error": smi.get("error")}
    parsed = smi.get("parsed")
    if not isinstance(parsed, dict) and isinstance(smi.get("raw"), str):
        try:
            parsed = json.loads(smi["raw"])
        except ValueError:
            parsed = None
    parsed = parsed if isinstance(parsed, dict) else {}
    out = {"available": True}
    util = smi.get("utilization_percent")
    if util is None:
        util = parsed.get("utilization_percent", parsed.get("utilization"))
    out["utilization_percent"] = util
    out.update(pick(parsed, "frequency_mhz", "power_w", "temperature_c"))
    if isinstance(parsed.get("memory_bytes"), (int, float)):
        out["memory_mb"] = round(parsed["memory_bytes"] / 1048576, 1)
    elif smi.get("memory_mb") is not None:
        out["memory_mb"] = smi["memory_mb"]
    return out


QUIET_NOTE = ("A benchmark run has paused SmarTune's collection (quiet mode). {what} "
               "Check `smartune bench status` for the run.")


def _quiet(data, what):
    """The server's quiet-mode marker, kept so an agent cannot mistake it for live data."""
    quiet = (data or {}).get("quiet_mode") or {}
    if not quiet.get("active"):
        return {}
    out = {"quiet_mode": True, "note": QUIET_NOTE.format(what=what)}
    if quiet.get("cached_at"):
        out["cached_at"] = quiet["cached_at"]
    return out


def summarize_dynamic(data):
    out = {"collected_at": data.get("collected_at")}
    out.update(_quiet(data, "These figures are the last cached values, not live ones; "
                            "sections that were never cached are missing."))
    if "cpu" in data:
        out["cpu"] = pick(data["cpu"] or {}, "usage_total", "iowait_total", "p_core_usage",
                          "e_core_usage", "lpe_core_usage", "temperature_c", "pkg_power_w")
    if "memory" in data:
        out["memory"] = pick(data["memory"] or {}, "usage_percent", "total_gb", "available_gb",
                             "swap_used_gb", "swap_total_gb", "swap_usage_percent")
    if "pressure" in data:
        out["pressure"] = pick(data["pressure"] or {}, "level", "score", "cpu", "memory", "io",
                               "is_disk_io_stressed", "network_pressure_level",
                               "network_pressure_pct", "network_worst_nic")
    if "disk" in data:
        disk = data["disk"] or {}
        out["disk"] = pick(disk, "level", "score", "is_stressed", "stressed_disks",
                           "busy_level", "busy_pct", "pressure_pct")
        out["disk"]["disks"] = {
            name: pick(d or {}, "read_mbps", "write_mbps", "pressure", "disk_type", "is_busy")
            for name, d in (disk.get("disk_io") or {}).items()
        }
    if "gpu" in data:
        gpu = data["gpu"] or {}
        out["gpu"] = {
            "devices": [
                {
                    **pick(dev, "pci_dev", "dev_type", "drv_name"),
                    "utilization": _gpu_util(dev),
                    "engine_util": dev.get("engine_util") or {},
                    "power_w": dev.get("power_w") or {},
                    "freqs": [pick(f, "name", "act_mhz", "max_mhz", "throttled")
                              for f in dev.get("freqs") or []],
                }
                for dev in _gpu_devices(gpu)
            ],
            "vram": gpu.get("vram") or {},
        }
    if "npu" in data:
        out["npu"] = _npu_metrics(data["npu"])
    if "network" in data:
        net = data["network"] or {}
        out["network"] = net.get("per_nic", net)
    return out


def _row_metrics(row):
    """Flat scalar metrics for one history row."""
    data = row.get("data") or {}
    cpu = data.get("cpu") or {}
    mem = data.get("memory") or {}
    pressure = data.get("pressure") or {}
    point = {
        "cpu": cpu.get("usage_total"),
        "mem": mem.get("usage_percent"),
        "swap_gb": mem.get("swap_used_gb"),
        "pressure": pressure.get("score"),
        "level": pressure.get("level"),
    }
    if "gpu" in data:
        utils = [_gpu_util(d) for d in _gpu_devices(data["gpu"])]
        utils = [u for u in utils if u is not None]
        point["gpu"] = max(utils) if utils else None
    if "npu" in data:
        point["npu"] = _npu_metrics(data["npu"]).get("utilization_percent")
    return {k: v for k, v in point.items() if v is not None}


# -- command handlers ----------------------------------------------------------
# Each takes (client, args) and returns a JSON-serialisable result.

def cmd_caps(client, args):
    return client.capabilities()


def cmd_doctor(client, args):
    return run_doctor(client)


def cmd_status(client, args):
    sections = _sections(args.sections, STATUS_SECTIONS) if args.sections else DEFAULT_STATUS_SECTIONS
    data = client.dynamic_info(sections)
    return data if args.raw else summarize_dynamic(data)


def cmd_info(client, args):
    if args.section and args.section not in STATIC_SECTIONS:
        raise SmartuneError(f"unknown section '{args.section}'",
                            hint="Valid: " + ", ".join(STATIC_SECTIONS), exit_code=EXIT_USAGE)
    return client.static_info(args.section)


def cmd_top(client, args):
    paused = "Per-app figures are not collected meanwhile, so an empty list does not mean the system is idle."
    if args.io:
        data = client.app_disk_io_stats(args.n) or {}
        return {"apps": [
            pick(a, "pid", "name", "app_name", "io_read_rate", "io_write_rate",
                 "io_read_iops", "io_write_iops", "io_per_disk", "score")
            for a in data.get("apps") or []
        ], **_quiet(data, paused), "units": "rates in MB/s, iops in block-layer requests/s"}

    # The server ranks by its combined score; re-ranking by one resource needs a
    # wider pool to sort from.
    fetch = args.n if args.by == "score" else max(args.n, 50)
    data = client.app_resource_stats(fetch) or {}
    rows = []
    for a in data.get("apps") or []:
        row = pick(a, "app_id", "app_name", "pid", "process_name", "memory_mb",
                   "gpu_util", "gpu_mem_mb", "npu_mem_mb", "io_read_rate", "io_write_rate", "score")
        if isinstance(a.get("cpu_usage"), (int, float)):
            row["cpu_pct"] = round(a["cpu_usage"] * 100, 1)  # server sends a 0-1 fraction
        rows.append(row)
    sort_key = {"cpu": "cpu_pct", "mem": "memory_mb", "gpu": "gpu_util", "npu": "npu_mem_mb",
                "score": "score"}[args.by]
    rows.sort(key=lambda r: r.get(sort_key) or 0, reverse=True)
    return {"by": args.by, "apps": rows[: args.n], **_quiet(data, paused),
            "units": "cpu_pct of total CPU capacity, gpu_util % of busiest engine, "
                     "gpu_mem_mb / npu_mem_mb device memory held in MB, io in MB/s"}


def cmd_ps(client, args):
    data = client.processes() or {}
    procs = data.get("processes") or []
    if args.grep:
        needle = args.grep.lower()
        procs = [p for p in procs
                 if needle in (p.get("name") or "").lower() or needle in (p.get("cmdline") or "").lower()]
    key = "cpu_percent" if args.sort == "cpu" else "memory_percent"
    procs.sort(key=lambda p: p.get(key) or 0, reverse=True)
    rows = []
    for p in procs[: args.n]:
        row = pick(p, "pid", "name", "username", "cpu_percent", "memory_percent", "status")
        if isinstance(p.get("mem_rss_kb"), (int, float)):
            row["rss_mb"] = round(p["mem_rss_kb"] / 1024, 1)
        cmdline = p.get("cmdline") or ""
        row["cmdline"] = cmdline if len(cmdline) <= 160 else cmdline[:160] + "..."
        rows.append(row)
    return {"total_processes": data.get("count", len(data.get("processes") or [])),
            "matched": len(procs), "processes": rows}


def cmd_proc(client, args):
    return client.process_detail(args.pid)


def cmd_history(client, args):
    seconds = parse_duration(args.since)
    sections = _sections(args.sections, HISTORY_SECTIONS) if args.sections else ("cpu", "memory", "pressure", "gpu")
    limit = min(20000, seconds // HISTORY_CADENCE_S + 10)
    data = client.history(seconds, sections, limit) or {}
    rows = sorted(data.get("items") or [], key=lambda r: r.get("collected_at") or 0)
    if args.raw:
        return data
    points = [{"t": iso(r.get("collected_at")), **_row_metrics(r)} for r in rows]
    metrics = sorted({k for p in points for k in p if k not in ("t", "level")})
    levels = {}
    for p in points:
        if p.get("level"):
            levels[p["level"]] = levels.get(p["level"], 0) + 1
    return {
        "window": {"from": iso(data.get("start_time")), "to": iso(data.get("end_time"))},
        "samples": len(points),
        "stats": {m: numeric_stats([p.get(m) for p in points]) for m in metrics},
        "pressure_levels": levels,
        "series": downsample(points, args.points),
        "units": "cpu/mem/gpu/npu in %, pressure score 0-1 (default bands: medium >= 0.6, high >= 0.8, critical = 1.0), swap in GB",
    }


def _app_row(app):
    row = pick(app, "app_id", "app_name", "priority", "network_priority", "status",
               "app_summary_status", "control_status", "frozen", "oom_score",
               "process_names", "remark")
    if app.get("cpu_affinity"):  # only when pinned to a core type or core list
        row["cpu_affinity"] = app["cpu_affinity"]
    if app.get("control_status") not in (None, "NORMAL"):
        row["effective"] = app.get("effective")
        row["auto_detail"] = app.get("auto_detail")
    return row


def cmd_apps(client, args):
    apps = client.controlled_apps()
    return {"count": len(apps), "apps": apps if args.raw else [_app_row(a) for a in apps]}


def cmd_app(client, args):
    return {
        "priority": client.priority_data(app_id=args.app_id),
        "limit": client.limit_snapshot(args.app_id),
    }


def cmd_limits(client, args):
    auto = client.auto_limited_apps() or {}
    manual = [_app_row(a) for a in client.controlled_apps()
              if a.get("control_status") == "MANUAL_LIMITED"]
    return {
        "sys_pressure_level": auto.get("sys_pressure_level"),
        "disk_pressure_level": auto.get("disk_pressure_level"),
        "auto_limited": auto.get("apps") or [],
        "manual_limited": manual,
    }


def cmd_pending(client, args):
    return {"pending": client.pending_apps()}


def cmd_diag_alerts(client, args):
    data = client.diag("alerts", active="false" if args.all else None, limit=args.n) or {}
    return {"alerts": data.get("alerts") or [], "count": data.get("count")}


def cmd_diag_events(client, args):
    start, end = window(args.since)
    data = client.diag("events", severity=args.severity, category=args.category,
                       app_id=args.app, event_type=args.type, job_id=args.job,
                       keyword=args.keyword, limit=args.n, **{"from": start, "to": end}) or {}
    events = data.get("events") or []
    return {"window": {"from": iso(start), "to": iso(end)}, "count": len(events), "events": events}


def cmd_diag_lifecycles(client, args):
    start, end = window(args.since)
    data = client.diag("control-lifecycles", app_id=args.app, limit=args.n,
                       **{"from": start, "to": end}) or {}
    rows = data.get("lifecycles") or []
    return {"window": {"from": iso(start), "to": iso(end)}, "count": len(rows), "lifecycles": rows}


def cmd_diag_logs(client, args):
    start, end = window(args.since)
    data = client.diag("logs", source=args.source, level=args.level, keyword=args.keyword,
                       job_id=args.job, limit=args.n, **{"from": start, "to": end}) or {}
    lines = data.get("lines") or []
    return {"window": {"from": iso(start), "to": iso(end)}, "sources": data.get("sources"),
            "truncated_by_server": data.get("truncated"), "lines": lines}


def cmd_diag_digest(client, args):
    if args.since:
        start, end = window(args.since)
        return client.diag("digest", **{"from": start, "to": end})
    if args.period == "boot":
        if not args.boot:
            raise SmartuneError("--period boot needs --boot <boot_id>",
                                hint="List boot ids with `smartune diag boots`.", exit_code=EXIT_USAGE)
        return client.diag("digest", period="boot", boot_id=args.boot)
    return client.diag("digest", period=args.period, date=args.date or time.strftime("%Y-%m-%d"))


def cmd_diag_context(client, args):
    if args.job:
        return client.diag("context", job_id=args.job)
    if args.app:
        return client.diag("context", app_id=args.app)
    start, end = window(args.since)
    return client.diag("context", findings_only="true" if args.findings_only else None,
                       **{"from": start, "to": end})


def cmd_diag_why(client, args):
    """Everything that explains one app's control history, in one call."""
    start, end = window(args.since)
    out = {"app_id": args.app_id, "window": {"from": iso(start), "to": iso(end)}}

    def part(name, fn):
        try:
            out[name] = fn()
        except SmartuneError as exc:
            out[name] = exc.to_dict()

    part("current_limit", lambda: client.limit_snapshot(args.app_id))
    part("lifecycles", lambda: (client.diag("control-lifecycles", app_id=args.app_id, limit=20,
                                            **{"from": start, "to": end}) or {}).get("lifecycles") or [])
    part("events", lambda: (client.diag("events", app_id=args.app_id, limit=30,
                                        **{"from": start, "to": end}) or {}).get("events") or [])

    def context():
        ctx = client.diag("context", app_id=args.app_id) or {}
        return pick(ctx, "window", "findings", "control_actions")
    part("context", context)
    return out


def cmd_diag_simple(name, **fixed):
    def handler(client, args):
        params = dict(fixed)
        if getattr(args, "n", None):
            params["limit"] = args.n
        return client.diag(name, **params)
    return handler


def cmd_bench_status(client, args):
    runs = client.bench("run") or {}
    return {
        "current": runs.get("current"),
        "recent": (runs.get("recent") or [])[: args.n],
        "preflight": client.bench("preflight"),
    }


BENCH_TERMINAL = ("done", "failed", "cancelled")  # benchmark/service/jobs.py STATUS_*
BENCH_FAILED_LOG_CHARS = 3000  # log tail `bench wait` attaches to a run that did not succeed


def _bench_job(client, run_id, log_chars=0):
    """One run's status, plus the last `log_chars` of its log when asked.

    The server returns the log forwards from ?offset= as {chunk, offset, size},
    at most 256 KB at a time; the tail needs the size first, so a long log takes
    a second request starting at size - log_chars.
    """
    if not log_chars:
        return client.bench(f"run/{run_id}") or {}
    job = client.bench(f"run/{run_id}", offset=0) or {}
    size = job.get("size") or 0
    chunk = job.get("chunk") or ""
    if size > len(chunk.encode("utf-8")) and size > log_chars:
        job = client.bench(f"run/{run_id}", offset=size - log_chars) or job
        chunk = job.get("chunk") or ""
    for key in ("chunk", "offset", "size"):
        job.pop(key, None)
    if len(chunk) > log_chars:
        chunk = chunk[-log_chars:]
    omitted = max(0, size - len(chunk.encode("utf-8")))
    job["log_tail"] = (f"...({omitted} earlier bytes omitted)\n" if omitted else "") + chunk
    return job


def cmd_bench_run(client, args):
    return _bench_job(client, args.run_id, args.log_chars if args.log else 0)


def cmd_bench_wait(client, args):
    """Poll one run until it finishes or --timeout passes, so an agent waits on
    a single command instead of sleeping and guessing."""
    run_id = args.run_id
    if not run_id:
        current = (client.bench("run") or {}).get("current")
        if not current:
            return {"current": None, "note": "No benchmark run is in progress."}
        run_id = current.get("id")
    start = time.monotonic()
    while True:
        job = _bench_job(client, run_id)
        waited = round(time.monotonic() - start)
        if job.get("status") in BENCH_TERMINAL:
            if job["status"] != "done":
                job = _bench_job(client, run_id, BENCH_FAILED_LOG_CHARS)
            return {"finished": True, "waited_s": waited, "run": job}
        remaining = args.timeout - (time.monotonic() - start)
        if remaining <= 0:
            return {"finished": False, "waited_s": waited, "run": job,
                    "hint": f"Still {job.get('status') or 'running'}; run "
                            f"`smartune bench wait {run_id}` again to keep waiting."}
        time.sleep(min(args.interval, remaining))


# Row fields kept by default; paths and directory names only help the dashboard.
BENCH_ROW_KEYS = ("job", "job_started_at", "backend", "model", "device", "precision", "mode",
                  "batch_size", "ov", "status", "task", "failure_reason", "metrics")


# Metric groups kept by default: the measured KPIs (TTFT, TPOT, throughput) and
# the derived comparisons (speedup vs CPU, performance per watt). The server
# assigns each metric its group (benchmark/service/results.py, metric_meta).
BENCH_KPI_GROUPS = ("kpi", "derived")


def cmd_bench_results(client, args):
    data = client.bench("results/matrix", backend=args.backend) or {}
    if args.raw:
        return data
    rows = data.get("rows") or []
    jobs = (data.get("dimensions") or {}).get("jobs") or []  # newest first
    if args.latest and jobs:
        rows = [r for r in rows if r.get("job") == jobs[0]]
    if args.job:
        rows = [r for r in rows if r.get("job") == args.job]
    if args.model:
        needle = args.model.lower()
        rows = [r for r in rows if needle in (r.get("model") or "").lower()]
    if args.device:
        rows = [r for r in rows if (r.get("device") or "") == args.device.lower()]
    matched = len(rows)

    kpi_keys = {m.get("key") for m in data.get("metrics") or [] if m.get("group") in BENCH_KPI_GROUPS}
    keep = (lambda k: True) if args.metrics == "all" else kpi_keys.__contains__
    out_rows = []
    for row in rows[: args.n]:
        out = pick(row, *BENCH_ROW_KEYS)
        out["metrics"] = {k: v for k, v in (row.get("metrics") or {}).items() if keep(k)}
        out_rows.append({k: v for k, v in out.items() if v not in (None, "")})
    used = {k for r in out_rows for k in r.get("metrics") or {}}
    return {
        "matched": matched,
        "rows": out_rows,
        "metrics": [pick(m, "key", "label", "unit", "higher_is_better", "description")
                    for m in data.get("metrics") or [] if m.get("key") in used],
        "primary_metric": data.get("primary_metric"),
        "jobs": jobs[:10],
        "dimensions": pick(data.get("dimensions") or {}, "models", "devices", "precisions",
                           "backends", "ovs"),
    }


def cmd_bench_models(client, args):
    return client.bench("models", search=args.search, limit=args.n)


# -- argument parsing ----------------------------------------------------------

def _sections(text, allowed):
    names = tuple(s.strip() for s in text.split(",") if s.strip())
    bad = [s for s in names if s not in allowed]
    if bad or not names:
        raise SmartuneError(f"unknown section(s): {', '.join(bad) or text}",
                            hint="Valid: " + ", ".join(allowed), exit_code=EXIT_USAGE)
    return names


def _positive_int(text):
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got '{text}'")
    return value


def _log_sources(text):
    names = [s.strip() for s in text.split(",") if s.strip()]
    bad = [s for s in names if s not in LOG_SOURCES]
    if bad or not names:
        raise argparse.ArgumentTypeError(
            f"unknown log source(s): {', '.join(bad) or text} (valid: {', '.join(LOG_SOURCES)})")
    return ",".join(names)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="smartune",
        description="SmarTune queries for agents and operators; `smartune control` changes how an "
                    "app is controlled, and only prints a plan unless given --yes. Connection: SMARTUNE_URL, SMARTUNE_TOKEN / SMARTUNE_TOKEN_FILE, SMARTUNE_CACERT.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--pretty", action="store_true", help="indent the JSON output")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                        help=f"cap on output size; long lists are cut to fit (default {DEFAULT_MAX_BYTES})")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    def add(parent, name, handler, help_text, raw=False):
        p = parent.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(handler=handler)
        if raw:
            p.add_argument("--raw", action="store_true", help="return the API payload without summarising")
        return p

    add(sub, "caps", cmd_caps, "Which features this SmarTune instance serves")
    add(sub, "doctor", cmd_doctor,
        "Check the connection step by step: config, server reachable, TLS, token, version, features, telemetry")

    p = add(sub, "status", cmd_status, "Live system snapshot: CPU, memory, pressure, GPU, NPU, disk", raw=True)
    p.add_argument("--sections", help="comma list from: " + ",".join(STATUS_SECTIONS))

    p = add(sub, "info", cmd_info, "Static platform info: model, drivers, firmware, hardware")
    p.add_argument("section", nargs="?", help="one of: " + ", ".join(STATIC_SECTIONS))

    p = add(sub, "top", cmd_top, "Applications using the most resources")
    p.add_argument("-n", type=int, default=10, help="rows (default 10)")
    p.add_argument("--by", choices=("score", "cpu", "mem", "gpu", "npu"), default="score",
                   help="npu ranks by NPU memory held; per-app NPU utilization is not measured")
    p.add_argument("--io", action="store_true", help="rank by disk I/O instead")

    p = add(sub, "ps", cmd_ps, "Running processes, like top")
    p.add_argument("-n", type=int, default=20, help="rows (default 20)")
    p.add_argument("--sort", choices=("cpu", "mem"), default="cpu")
    p.add_argument("--grep", help="keep processes whose name or command line contains this text")

    p = add(sub, "proc", cmd_proc, "Details of one process")
    p.add_argument("pid", type=int)

    p = add(sub, "history", cmd_history, "Resource trend over a time window", raw=True)
    p.add_argument("--since", default="1h", help="window length, e.g. 30m, 6h, 2d (default 1h)")
    p.add_argument("--sections", help="comma list from: " + ",".join(HISTORY_SECTIONS))
    p.add_argument("--points", type=int, default=30, help="series points to return (default 30)")

    p = add(sub, "apps", cmd_apps, "Applications under SmarTune control, with priority and limit state", raw=True)
    p = add(sub, "app", cmd_app, "Priority and live limit state of one controlled app")
    p.add_argument("app_id")
    add(sub, "limits", cmd_limits, "Every app currently limited, automatically or manually")
    add(sub, "pending", cmd_pending, "Apps queued at launch, waiting for resources")

    diag = sub.add_parser("diag", help="Diagnostics: alerts, events, control lifecycles, logs, digests")
    dsub = diag.add_subparsers(dest="diag_command", metavar="DIAG_COMMAND")
    dsub.required = True

    p = add(dsub, "alerts", cmd_diag_alerts, "Active alerts (or all with --all)")
    p.add_argument("--all", action="store_true", help="include resolved alerts")
    p.add_argument("-n", type=int, default=50)

    p = add(dsub, "events", cmd_diag_events, "Operational events in a time window")
    p.add_argument("--since", default="24h")
    p.add_argument("--severity")
    p.add_argument("--category")
    p.add_argument("--app", help="app_id")
    p.add_argument("--type", help="event_type, see `smartune diag catalog`")
    p.add_argument("--job", help="benchmark job id")
    p.add_argument("--keyword")
    p.add_argument("-n", type=int, default=50)

    p = add(dsub, "lifecycles", cmd_diag_lifecycles, "Resource-limit lifecycles: what was limited, why, and when it recovered")
    p.add_argument("--since", default="24h")
    p.add_argument("--app", help="app_id")
    p.add_argument("-n", type=int, default=50)

    p = add(dsub, "logs", cmd_diag_logs, "Redacted log lines across SmarTune, journald and other sources")
    p.add_argument("--since", default="1h")
    p.add_argument("--source", type=_log_sources,
                   help="comma list from: " + ",".join(LOG_SOURCES) + " (default all)")
    p.add_argument("--level", help="minimum level, e.g. warning, error")
    p.add_argument("--keyword")
    p.add_argument("--job", help="benchmark job id")
    p.add_argument("-n", type=int, default=100)

    p = add(dsub, "digest", cmd_diag_digest, "Summary of a day, week, boot session or recent window")
    p.add_argument("--period", choices=("day", "week", "boot"), default="day")
    p.add_argument("--date", help="YYYY-MM-DD for day/week (default today)")
    p.add_argument("--boot", help="boot_id for --period boot")
    p.add_argument("--since", help="digest the last window instead, e.g. 6h")

    p = add(dsub, "context", cmd_diag_context, "Evidence bundle for root-cause analysis (app, benchmark job or window)")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--app", help="app_id")
    scope.add_argument("--job", help="benchmark job id")
    p.add_argument("--since", default="1h", help="window when neither --app nor --job is given")
    p.add_argument("--findings-only", action="store_true")

    p = add(dsub, "why", cmd_diag_why, "Why an app is (or was) limited: current limit, lifecycles, events, findings")
    p.add_argument("app_id")
    p.add_argument("--since", default="24h")

    p = add(dsub, "boots", cmd_diag_simple("boots"), "Recent boot sessions")
    p.add_argument("-n", type=int, default=15)
    add(dsub, "retention", cmd_diag_simple("retention"), "How far back each evidence source reaches")
    add(dsub, "catalog", cmd_diag_simple("event-catalog"), "Built-in diagnostic event types")

    bench = sub.add_parser("bench", help="Model benchmark: run status and results (read-only)")
    bsub = bench.add_subparsers(dest="bench_command", metavar="BENCH_COMMAND")
    bsub.required = True

    p = add(bsub, "status", cmd_bench_status, "Running job, recent runs and whether a run could start now")
    p.add_argument("-n", type=int, default=5, help="recent runs (default 5)")
    p = add(bsub, "run", cmd_bench_run, "Status of one run")
    p.add_argument("run_id")
    p.add_argument("--log", action="store_true", help="include the tail of the run log")
    p.add_argument("--log-chars", type=_positive_int, default=4000)
    p = add(bsub, "wait", cmd_bench_wait, "Wait for a run to finish (default: the run in progress)")
    p.add_argument("run_id", nargs="?")
    p.add_argument("--timeout", type=_positive_int, default=300,
                   help="seconds to wait before returning with finished=false (default 300)")
    p.add_argument("--interval", type=_positive_int, default=5, help="poll interval in seconds (default 5)")
    p = add(bsub, "results", cmd_bench_results, "Benchmark results across runs: KPIs per model, device and precision",
            raw=True)
    p.add_argument("--backend", choices=BENCH_BACKENDS)
    p.add_argument("--latest", action="store_true", help="only the newest job")
    p.add_argument("--job", help="only this job (see `jobs` in the output)")
    p.add_argument("--model", help="keep models whose name contains this text")
    p.add_argument("--device", help="cpu, gpu or npu")
    p.add_argument("--metrics", choices=("kpi", "all"), default="kpi",
                   help="kpi: KPIs and derived comparisons only (default); all: every sampled metric")
    p.add_argument("-n", type=int, default=30, help="rows (default 30)")
    p = add(bsub, "models", cmd_bench_models, "Benchmarkable models")
    p.add_argument("--search")
    p.add_argument("-n", type=int, default=20)

    control.add_parser(sub, add)

    return parser


def execute(args, client=None):
    """Run parsed arguments: (exit code, the JSON-ready document to print).

    Shared by main() and the MCP server, so both shape output the same way.
    """
    try:
        client = client or SmartuneClient()
        result = args.handler(client, args)
        # doctor returns a full report even when a check fails, with its exit code.
        exit_code = getattr(result, "exit_code", 0)
    except SmartuneError as exc:
        return exc.exit_code, exc.to_dict()
    except ValueError as exc:  # bad --since and the like
        return EXIT_USAGE, {"error": str(exc)}

    if getattr(args, "raw", False):
        return exit_code, fit(result, args.max_bytes)  # untrimmed, but still size-capped
    return exit_code, fit(compact(localize_times(result)), args.max_bytes)


def main(argv=None, client=None):
    args = build_parser().parse_args(argv)
    exit_code, output = execute(args, client)
    print(dumps(output, pretty=args.pretty))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
