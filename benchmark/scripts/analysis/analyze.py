#!/usr/bin/env python3
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Benchmark data analysis engine (standalone script, can also be imported by pivot_report.py).

Responsibilities: read raw CSV -> compute CV from repeated rows -> fold to median -> compute derived metrics d_* -> scan thresholds to generate "conclusions".
Each conclusion carries several "panel specs" (rows/cols/metric/how/filters/highlight) for one-click visualization in the frontend.

KPIs are driven by a configurable PROFILE; switching benchmark type (benchmark_app / genai / custom) only requires switching the profile.

Usage (standalone):
  python analyze.py [input_csv] [profile_name]
  Default: windowed_metric_medians.csv, profile auto-detected
  Output: prints conclusion summary to terminal + writes conclusions.json
"""
import csv, json, sys, os, statistics
from collections import OrderedDict

# Candidate config dimensions (DIMS): extend here as needed, e.g. platform / model_source.
# The dimensions that actually reach the pivot table / HTML are decided by the data: dimensions whose column is missing from the CSV, or whose entire column is empty, are automatically dropped.
DIMS = ["model_name", "model_source", "platform", "device", "batch_size", "precision", "mode"]
EXCLUDE = set(DIMS + ["case_name", "case_dir"])

# ---------------- KPI Profile (core generalization point) ----------------
PROFILES = {
    "benchmark_app": {
        "name": "benchmark_app",
        "kpis": [
            {"col": "kpi_throughput_fps",    "label": "Throughput",  "unit": "fps", "goal": "max"},
            {"col": "kpi_latency_median_ms", "label": "Latency P50", "unit": "ms",  "goal": "min"},
        ],
        "primary_throughput": "kpi_throughput_fps",
        "power_col": "cpu_package_power_w_median",
    },
    # For extension: GenAI-type benchmark (this CSV has no such columns, example only)
    "genai": {
        "name": "genai",
        "kpis": [
            {"col": "kpi_throughput_tokens_s", "label": "Throughput",  "unit": "t/s", "goal": "max"},
            {"col": "kpi_first_latency_ms", "label": "Latency",       "unit": "ms",   "goal": "min"}
        ],
        "primary_throughput": "kpi_throughput_tokens_s",
        "power_col": "cpu_package_power_w_median",
    },
}


def detect_profile(columns):
    cols = set(columns)
    if "kpi_throughput_tokens_s" in cols or {"ttft_ms", "tpot_ms"} & cols:
        return PROFILES["genai"]
    return PROFILES["benchmark_app"]


def resolve_profile(profile, columns):
    if profile is None:
        return detect_profile(columns)
    if isinstance(profile, str):
        return PROFILES[profile]
    return profile


# ---------------- Basic utilities ----------------
def _median(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def _geomean(vals):
    v = [x for x in vals if x is not None and x > 0]
    if not v:
        return None
    p = 1.0
    for x in v:
        p *= x
    return p ** (1.0 / len(v))


def _parse(v):
    try:
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


def cfg_label(rec):
    parts = [rec.get(d) for d in ("model_name", "device", "precision", "batch_size")]
    return "/".join(str(p) for p in parts if p not in (None, ""))


# ---------------- Load + fold + CV ----------------
def load_records(csv_path, primary_col):
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit("CSV is empty")
    cols = list(rows[0].keys())
    dims = [d for d in DIMS if d in cols]          # keep only candidate dimensions present in the CSV
    metrics = [c for c in cols if c not in EXCLUDE]

    buckets = OrderedDict()
    for r in rows:
        key = tuple(r.get(d, "") for d in dims)
        b = buckets.setdefault(key, {m: [] for m in metrics})
        for m in metrics:
            b[m].append(_parse(r.get(m, "")))

    records = []
    for key, b in buckets.items():
        rec = dict(zip(dims, key))
        for m in metrics:
            rec[m] = _median(b[m])
        # repeat count & primary-throughput CV (using the un-folded raw values)
        pv = [x for x in b.get(primary_col, []) if x is not None] if primary_col in b else []
        rec["_n_runs"] = len(pv)
        if len(pv) >= 2 and statistics.mean(pv) != 0:
            rec["d_cv_throughput_pct"] = statistics.stdev(pv) / statistics.mean(pv) * 100.0
        else:
            rec["d_cv_throughput_pct"] = None
        records.append(rec)
    return records, metrics, len(rows), dims


# ---------------- Derived metrics ----------------
DERIVED = [
    ("d_cv_throughput_pct", "Primary throughput CV", "%"),
    ("d_speedup_vs_fp32",   "Speedup vs fp32",       "x"),
    ("d_rel_best_prec",     "Rel. best (precision)", "x"),
    ("d_speedup_vs_cpu",    "Speedup vs CPU",        "x"),
    ("d_rel_best_device",   "Rel. best (device)",    "x"),
    ("d_bs_tp_gain_pct",    "bs8 throughput gain",   "%"),
    ("d_bs_lat_cost_pct",   "bs8 latency cost",      "%"),
    ("d_rel_best_batch",    "Rel. best (batch)",     "x"),
    ("d_perf_per_watt",     "Efficiency",            "fps/W"),
    ("d_temp_headroom_c",   "Temp headroom",         "℃"),
]


def add_derived(records, profile):
    tp = profile["primary_throughput"]
    power = profile["power_col"]
    lat = next((k["col"] for k in profile["kpis"] if k["goal"] == "min"), None)
    idx = {(r.get("model_name"), r.get("device"), r.get("batch_size"), r.get("precision")): r for r in records}

    def g(m, d, b, p):
        return idx.get((m, d, b, p))

    for r in records:
        for name, _, _ in DERIVED:
            r.setdefault(name, None)
        m, d, b, p = r.get("model_name"), r.get("device"), r.get("batch_size"), r.get("precision")
        v = r.get(tp)
        # perf/watt: whole-package power as the unified denominator
        pw = r.get(power)
        if v is not None and pw not in (None, 0):
            r["d_perf_per_watt"] = v / pw
        # temperature headroom
        tj, tc = r.get("cpu_package_tjmax_c_median"), r.get("cpu_package_temp_c_median")
        if tj is not None and tc is not None:
            r["d_temp_headroom_c"] = tj - tc
        # precision: vs fp32 (strict; null if baseline missing) + relative to group best (robust to missing)
        base = g(m, d, b, "fp32")
        if v is not None and base is not None and base.get(tp) not in (None, 0):
            r["d_speedup_vs_fp32"] = v / base[tp]
        best = _max_none([g(m, d, b, pp) and g(m, d, b, pp).get(tp) for pp in _vals(records, "precision")])
        if v is not None and best not in (None, 0):
            r["d_rel_best_prec"] = v / best
        # device: vs cpu (strict baseline) + relative to group best (robust to missing, normalized 0-1)
        cpu = g(m, "cpu", b, p)
        if v is not None and cpu is not None and cpu.get(tp) not in (None, 0):
            r["d_speedup_vs_cpu"] = v / cpu[tp]
        bestd = _max_none([g(m, dd, b, p) and g(m, dd, b, p).get(tp) for dd in _vals(records, "device")])
        if v is not None and bestd not in (None, 0):
            r["d_rel_best_device"] = v / bestd
        # batch: relative to group best (normalized 0-1)
        bestb = _max_none([g(m, d, bb, p) and g(m, d, bb, p).get(tp) for bb in _vals(records, "batch_size")])
        if v is not None and bestb not in (None, 0):
            r["d_rel_best_batch"] = v / bestb

    # batch size: bs8 vs bs1 (gain/cost attached to the bs8 row)
    for r in records:
        if r.get("batch_size") != "bs8":
            continue
        r1 = g(r.get("model_name"), r.get("device"), "bs1", r.get("precision"))
        if not r1:
            continue
        v8, v1 = r.get(tp), r1.get(tp)
        if v8 is not None and v1 not in (None, 0):
            r["d_bs_tp_gain_pct"] = (v8 - v1) / v1 * 100.0
        if lat:
            l8, l1 = r.get(lat), r1.get(lat)
            if l8 is not None and l1 not in (None, 0):
                r["d_bs_lat_cost_pct"] = (l8 - l1) / l1 * 100.0


def _vals(records, dim):
    seen = []
    for r in records:
        if r.get(dim) not in seen:
            seen.append(r.get(dim))
    return seen


def _max_none(xs):
    xs = [x for x in xs if x is not None]
    return max(xs) if xs else None


# ---------------- Panel spec helpers ----------------
def panel(rows, cols, metric, how="median", filters=None, title=None, highlight=None):
    remain_field=set(DIMS) - set(rows) - set(cols) - set(filters.keys() if filters else [])
    p = {"rows": rows + list(remain_field), "cols": cols, "metric": metric, "how": how, "filters": filters or {}}
    if title:
        p["title"] = title
    if highlight:
        p["highlight"] = highlight
    return p


def _fmt(v, unit=""):
    if v is None:
        return "–"
    a = abs(v)
    s = "%.0f" % v if a >= 1000 else "%.1f" % v if a >= 100 else "%.2f" % v if a >= 1 else "%.3f" % v
    if unit == "%":
        return s + "%"
    if unit == "x":
        return s + "×"
    if unit:
        return s + " " + unit
    return s


# ---------------- Conclusion generation L0-L6 ----------------
def build_conclusions(records, metrics, profile, n_raw):
    tp = profile["primary_throughput"]
    C = []

    def add(cid, cat, sev, title, detail, panels):
        C.append({"id": cid, "category": cat, "severity": sev,
                  "title": title, "detail": detail, "panels": panels})
    '''
    # ---- L0 coverage & credibility ----
    failed = [r for r in records if r.get(tp) is None]
    total = len(records)
    if failed:
        by_dev = _count(failed, "device")
        by_prec = _count(failed, "precision")
        add("L0-coverage", "L0 Coverage", "warn" if failed else "good",
            "%d/%d configs did not complete (no primary KPI)" % (len(failed), total),
            "by device: %s ; by precision: %s" % (by_dev, by_prec),
            [panel(["model_name", "precision"], ["device", "batch_size"], tp, "count",
                   title="Coverage matrix (has value=1, empty=not completed)")])
    else:
        add("L0-coverage", "L0 Coverage", "good", "All %d configs completed" % total, "", [])

    shaky = [r for r in records if (r.get("d_cv_throughput_pct") or 0) > 5]
    if shaky:
        worst = max(shaky, key=lambda r: r["d_cv_throughput_pct"])
        add("L0-stability", "L0 Coverage", "warn",
            "%d configs show repeated-test jitter >5%% (use data with caution)" % len(shaky),
            "most jittery: %s CV=%s" % (cfg_label(worst), _fmt(worst["d_cv_throughput_pct"], "%")),
            [panel(["model_name", "precision"], ["device", "batch_size"],
                   "d_cv_throughput_pct", "max", title="Repeated-test CV% (lower is more stable)", highlight="max")])
    else:
        add("L0-stability", "L0 Coverage", "good", "All configs have repeated-test CV <=5%, data is stable", "", [])

    # ---- L1 primary KPI ranking (profile-driven, one per KPI + efficiency) ----
    kpi_specs = [k for k in profile["kpis"] if k["col"] in metrics] + \
                [{"col": "d_perf_per_watt", "label": "Efficiency", "unit": "fps/W", "goal": "max"}]
    for k in kpi_specs:
        col, goal, unit = k["col"], k["goal"], k["unit"]
        cand = [r for r in records if r.get(col) is not None]
        if not cand:
            continue
        best = (max if goal == "max" else min)(cand, key=lambda r: r[col])
        add("L1-best-" + col, "L1 Ranking", "good",
            "「%s」 best: %s = %s" % (k["label"], cfg_label(best), _fmt(best[col], unit)),
            "goal=%s (%s)" % (goal, "higher is better" if goal == "max" else "lower is better"),
            [panel(["model_name", "precision"], ["device", "batch_size"], col, "median",
                   title="%s overview (highlight %s)" % (k["label"], "best"),
                   highlight="max" if goal == "max" else "min")])

    # best primary-throughput config per model (one conclusion, multiple panels)
    models = _vals(records, "model_name")
    lines, pans = [], []
    for mdl in models:
        cand = [r for r in records if r.get("model_name") == mdl and r.get(tp) is not None]
        if not cand:
            continue
        b = max(cand, key=lambda r: r[tp])
        lines.append("%s->%s/%s/%s(%s)" % (mdl, b["device"], b["precision"], b["batch_size"], _fmt(b[tp])))
        pans.append(panel(["device", "precision"], ["batch_size"], tp, "median",
                          filters={"model_name": mdl}, title="%s best throughput config" % mdl, highlight="max"))
    if pans:
        add("L1-best-per-model", "L1 Ranking", "info",
            "Best throughput config per model", " ; ".join(lines), pans)

    # device ranking (by how many (model,precision,batch) combos each device wins on primary throughput)
    wins = _device_wins(records, tp)
    if wins:
        rank = " > ".join("%s(%d wins)" % (d, n) for d, n in wins)
        add("L1-device-rank", "L1 Ranking", "info",
            "Device throughput ranking: %s" % rank, "counts how many combos each device tops in throughput",
            [panel(["model_name", "precision"], ["device"], tp, "median",
                   title="Device x model/precision throughput", highlight="max")])
    '''
    # ---- L2 precision/quantization ----
    add("L2-quant-overall", "L2 Precision", "info", "Quantization precision/speedup overview", "",
        [panel(["model_name", "device"], ["precision"], tp, "median", title="Throughput x precision"),
         panel(["model_name", "device"], ["precision"], "d_rel_best_prec", "median", title="Rel. best precision (0-1)")] )
    '''
    for prec in [p for p in _vals(records, "precision") if p != "fp32"]:
        pairs = [r["d_speedup_vs_fp32"] for r in records
                 if r.get("precision") == prec and r.get("d_speedup_vs_fp32") is not None]
        denom = sum(1 for r in records if r.get("precision") == prec and r.get(tp) is not None)
        gm = _geomean(pairs)
        if gm is None:
            continue
        sev = "good" if gm > 1.05 else "warn" if gm < 0.95 else "info"
        add("L2-quant-" + prec, "L2 Precision", "info",
            "%s geometric mean vs fp32 %s (based on %d/%d groups with both ends present)" % (prec, _fmt(gm, "x"), len(pairs), denom),
            "groups missing the fp32 baseline are dropped, no cross-contamination",
            [panel(["model_name", "device"], ["precision"], tp, "median",
                   title="Throughput x precision"),
             panel(["model_name", "device"], ["precision"], "d_speedup_vs_fp32", "median",
                   title="Speedup vs fp32 x", highlight="max"),
             panel(["model_name", "device"], ["precision"], "d_rel_best_prec", "median",
                   title="Rel. best precision (0-1)", highlight="max")])
    
    # quantization gain sorted by device
    dev_gain = []
    for dev in _vals(records, "device"):
        pr = [r["d_speedup_vs_fp32"] for r in records
              if r.get("device") == dev and r.get("precision") == "int8" and r.get("d_speedup_vs_fp32") is not None]
        gm = _geomean(pr)
        if gm is not None:
            dev_gain.append((dev, gm))
    if len(dev_gain) >= 2:
        dev_gain.sort(key=lambda x: -x[1])
        txt = " > ".join("%s %s" % (d, _fmt(g, "x")) for d, g in dev_gain)
        add("L2-quant-by-device", "L2 Precision", "info",
            "int8 quantization gain by device: %s" % txt, "geometric mean int8 speedup vs fp32 per device",
            [panel(["device"], ["model_name"], "d_speedup_vs_fp32", "median",
                   filters={"precision": "int8"}, title="int8 speedup x device", highlight="max")])
    '''
    # ---- L3 device comparison (vs cpu) ----
    add("L3-dev-overall", "L3 Device", "info", "Device comparison overview", "",
        [panel(["model_name", "precision"], ["device"], tp, "median", title="Throughput x device"),
         panel(["model_name", "precision"], ["device"], "d_rel_best_device", "median",
               title="Rel. best device (0-1)", highlight="max")])
    '''
    for dev in [d for d in _vals(records, "device") if d != "cpu"]:
        sp = [r["d_speedup_vs_cpu"] for r in records
              if r.get("device") == dev and r.get("precision") == "int8" and r.get("d_speedup_vs_cpu") is not None]
        gm = _geomean(sp)
        if gm is None:
            continue
        add("L3-dev-" + dev, "L3 Device", "good" if gm > 1 else "info",
            "%s averages %s vs CPU (int8, based on %d groups)" % (dev.upper(), _fmt(gm, "x"), len(sp)),
            "compared within the same (model,precision,batch)",
            [panel(["model_name", "precision"], ["device"], tp, "median", title="Throughput x device"),
             panel(["model_name", "precision"], ["device"], "d_speedup_vs_cpu", "median",
                   filters={"precision": "int8"}, title="Speedup vs CPU x", highlight="max"),
             panel(["model_name", "precision"], ["device"], "d_rel_best_device", "median",
                   title="Rel. best device (0-1)", highlight="max")])
    '''
    # ---- L4 batch size ----
    add("L4-batch-overall", "L4 Batch size", "info", "Batch size comparison overview", "",
        [panel(["model_name", "device"], ["batch_size"], tp, "median", title="Throughput x batch size"),
         panel(["model_name", "device"], ["batch_size"], "d_rel_best_batch", "median",
               title="Rel. best batch (0-1)", highlight="max")])
    '''
    tg = _geomean([1 + (r["d_bs_tp_gain_pct"] / 100.0) for r in records if r.get("d_bs_tp_gain_pct") is not None])
    lc = [r["d_bs_lat_cost_pct"] for r in records if r.get("d_bs_lat_cost_pct") is not None]
    if tg is not None:
        gain_pct = (tg - 1) * 100
        lc_med = _median(lc)
        lat = next((k["col"] for k in profile["kpis"] if k["goal"] == "min"), None)
        pans = [panel(["model_name", "device"], ["batch_size"], tp, "median",
                      title="Throughput x batch size", highlight="max"),
                panel(["model_name", "device"], ["batch_size"], "d_rel_best_batch", "median",
                      title="Rel. best batch (0-1)", highlight="max")]
        if lat:
            pans.append(panel(["model_name", "device"], ["batch_size"], lat, "median",
                              title="Latency x batch size", highlight="min"))
        add("L4-batch", "L4 Batch size", "info",
            "bs8 vs bs1: throughput %s%s, latency %s" % ("+" if gain_pct >= 0 else "", _fmt(gain_pct, "%"),
                                              ("+" + _fmt(lc_med, "%")) if (lc_med or 0) >= 0 else _fmt(lc_med, "%")),
            "throughput-latency tradeoff; geometric mean throughput gain / median latency cost",
            pans)
    '''
    '''    # ---- L5 efficiency ----
    eff = [r for r in records if r.get("d_perf_per_watt") is not None]
    if eff:
        be = max(eff, key=lambda r: r["d_perf_per_watt"])
        dev_eff = []
        for dev in _vals(records, "device"):
            gm = _geomean([r["d_perf_per_watt"] for r in eff if r.get("device") == dev])
            if gm:
                dev_eff.append((dev, gm))
        dev_eff.sort(key=lambda x: -x[1])
        lead = ""
        if len(dev_eff) >= 2 and dev_eff[-1][1] > 0:
            lead = "; %s leads %s in efficiency by %s" % (dev_eff[0][0].upper(), dev_eff[-1][0].upper(),
                                        _fmt(dev_eff[0][1] / dev_eff[-1][1], "x"))
        add("L5-efficiency", "L5 Efficiency", "good",
            "Most power-efficient: %s = %s" % (cfg_label(be), _fmt(be["d_perf_per_watt"], "fps/W")) + lead,
            "efficiency = primary throughput / whole-package power (W)",
            [panel(["model_name", "device"], ["precision"], "d_perf_per_watt", "median",
                   title="Efficiency fps/W (highlight best)", highlight="max")])
    '''
    # ---- L6 hardware bottleneck signals (threshold-triggered) ----
    _signal(records, C, add, "d_temp_headroom_c", lambda r: r.get("d_temp_headroom_c") is not None and r["d_temp_headroom_c"] < 5,
            "L6 Bottleneck", "warn", "Hitting the thermal wall", "temp headroom <5℃, likely throttling",
            panel(["model_name", "precision"], ["device", "batch_size"], "d_temp_headroom_c", "min",
                  title="Temp headroom ℃ (lower is more dangerous)", highlight="min"))
    _signal(records, C, add, "gpu_busy",
            lambda r: r.get("device") == "gpu" and (r.get("gpu_compute_busy_percent_median") or 0) < 95,
            "L6 Bottleneck", "info", "GPU compute engine not fully loaded (<95%)", "GPU compute may be underutilized",
            panel(["model_name", "precision"], ["precision"], "gpu_compute_busy_percent_median",
                  "median", filters={"device": "gpu"}, title="GPU compute engine busy%", highlight="min"))
    _signal(records, C, add, "npu_util",
            lambda r: r.get("device") == "npu" and (r.get("npu_utilization_percent_median") is not None) and r["npu_utilization_percent_median"] < 95,
            "L6 Bottleneck", "info", "NPU utilization is low (<95%)", "NPU compute may be underutilized",
            panel(["model_name", "precision"], ["precision"], "npu_utilization_percent_median",
                  "median", filters={"device": "npu"}, title="NPU utilization%", highlight="min"))
    _signal(records, C, add, "mem_bw",
            lambda r: (r.get("memory_bandwidth_percent_median") or 0) > 80,
            "L6 Bottleneck", "warn", "Memory bandwidth usage >80%", "likely memory-access bottleneck",
            panel(["model_name", "device"], ["precision", "batch_size"], "memory_bandwidth_percent_median",
                  "median", title="Memory bandwidth usage%", highlight="max"))

    return C


def _signal(records, C, add, cid, pred, cat, sev, title, detail, pan):
    hit = [r for r in records if pred(r)]
    if not hit:
        return
    examples = ", ".join(cfg_label(r) for r in hit[:4]) + (" …" if len(hit) > 4 else "")
    add("L6-" + cid, cat, sev, "%s (%d configs)" % (title, len(hit)),
        detail + " | e.g.: " + examples, [pan])


def _count(recs, dim):
    c = {}
    for r in recs:
        c[r.get(dim)] = c.get(r.get(dim), 0) + 1
    return ", ".join("%s×%d" % (k, v) for k, v in sorted(c.items(), key=lambda x: -x[1]))


def _device_wins(records, tp):
    combos = {}
    for r in records:
        if r.get(tp) is None:
            continue
        key = (r.get("model_name"), r.get("precision"), r.get("batch_size"))
        combos.setdefault(key, []).append((r.get("device"), r[tp]))
    wins = {}
    for key, lst in combos.items():
        d, _ = max(lst, key=lambda x: x[1])
        wins[d] = wins.get(d, 0) + 1
    return sorted(wins.items(), key=lambda x: -x[1])


# ---------------- Public main entry ----------------
def enrich(csv_path, profile=None):
    # read the header once to decide profile and primary
    with open(csv_path, newline="") as f:
        header = next(csv.reader(f))
    prof = resolve_profile(profile, header)
    primary = prof["primary_throughput"]

    records, base_metrics, n_raw, dims = load_records(csv_path, primary)
    add_derived(records, prof)

    derived_names = [n for n, _, _ in DERIVED]
    metrics = base_metrics + derived_names

    # grouping (including "derived analysis")
    label_map = {"kpi": "KPI Performance", "cpu": "CPU Power/Freq/Thermal", "gpu": "GPU Monitoring",
                 "npu": "NPU", "memory": "Memory", "d": "Derived Analysis"}
    groups = {}
    for m in metrics:
        groups.setdefault(label_map.get(m.split("_")[0], m.split("_")[0]), []).append(m)

    # unit table (for frontend fmt)
    units = {n: u for n, _, u in DERIVED}
    for k in prof["kpis"]:
        units[k["col"]] = k["unit"]

    # drop all-empty dimensions: only dimensions with at least one non-empty value reach the pivot / HTML
    effective_dims = [d for d in dims if any(r.get(d) not in (None, "") for r in records)]
    dim_values = {d: sorted({r[d] for r in records if r.get(d) not in (None, "")})
                  for d in effective_dims}
    conclusions = build_conclusions(records, metrics, prof, n_raw)

    # strip internal fields when outputting records
    clean = []
    for r in records:
        clean.append({k: v for k, v in r.items() if not k.startswith("_")})

    return {
        "data": clean, "metrics": metrics, "groups": groups, "dim_values": dim_values,
        "dims": effective_dims,
        "units": units, "conclusions": conclusions, "profile": prof, "n_raw": n_raw,
    }


SEV_MARK = {"good": "✓", "info": "·", "warn": "⚠", "bad": "✗"}


def main():
    inp = sys.argv[1] if len(sys.argv) > 1 else "windowed_metric_medians.csv"
    prof = sys.argv[2] if len(sys.argv) > 2 else None
    if not os.path.exists(inp):
        raise SystemExit("Input file not found: " + inp)
    res = enrich(inp, prof)
    print("profile = %s | %d raw rows -> %d config | %d metrics (incl. %d derived) | %d conclusions" % (
        res["profile"]["name"], res["n_raw"], len(res["data"]),
        len(res["metrics"]), len(DERIVED), len(res["conclusions"])))
    print("-" * 70)
    cat = None
    for c in res["conclusions"]:
        if c["category"] != cat:
            cat = c["category"]
            print("\n[%s]" % cat)
        print("  %s %s" % (SEV_MARK.get(c["severity"], "·"), c["title"]))
        if c["detail"]:
            print("      %s" % c["detail"])
        if c["panels"]:
            print("      panels x%d" % len(c["panels"]))
    out = os.path.join(os.path.dirname(os.path.abspath(inp)), "conclusions.json")
    with open(out, "w") as f:
        json.dump({"profile": res["profile"], "conclusions": res["conclusions"]}, f, ensure_ascii=False, indent=2)
    print("\nWritten:", out)


if __name__ == "__main__":
    main()
