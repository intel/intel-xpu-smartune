# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0


import csv, re, statistics
from collections import OrderedDict
from functools import partial

# Config dimensions. The fixed ones, plus one per benchmark parameter: the
# aggregator writes every parameter a case ran with as a `param_<name>` column
# (from its case.json), so a sweep over batch size, shape, streams or anything
# else becomes a dimension here without this file naming it.
# The dimensions that actually reach the pivot table / HTML are decided by the data: dimensions whose column is missing from the CSV, or whose entire column is empty, are automatically dropped,
# and a parameter that has the same value in every row is reported as a fixed setting rather than offered as an axis.
BASE_DIMS = ["model_name", "model_source", "platform", "device", "precision", "mode"]
PARAM_PREFIX = "param_"
BATCH = PARAM_PREFIX + "batch_size"
# Kept for callers that import it; the live list is per-CSV (candidate_dims).
DIMS = BASE_DIMS + [BATCH]
# Columns that describe a case but are neither a dimension nor a measurement.
# `batch_size` is the pre-case.json "bs1" tag, folded into param_batch_size.
NON_METRIC = {"case_name", "case_dir", "variant_id", "swept_keys", "case_status",
              "overridden", "tool", "task", "batch_size",
              "series", "round", "warmup", "sampled"}


def candidate_dims(columns):
    dims = [d for d in BASE_DIMS if d in columns]
    params = [c for c in columns if c.startswith(PARAM_PREFIX)]
    if "batch_size" in columns and BATCH not in params:
        params.insert(0, BATCH)
    return dims + params


def param_label(dim):
    return dim[len(PARAM_PREFIX):] if dim.startswith(PARAM_PREFIX) else dim


def _sort_key(value):
    """Numbers numerically (2 before 16), everything else as text, after them."""
    try:
        return (0, float(value), "")
    except (TypeError, ValueError):
        return (1, 0.0, str(value))

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


def _parse(v):
    try:
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


def cfg_label(rec, varying=()):
    parts = [rec.get(d) for d in ("model_name", "device", "precision")]
    parts += ["%s=%s" % (param_label(d), rec.get(d)) for d in varying
              if d.startswith(PARAM_PREFIX) and rec.get(d) not in (None, "")]
    return "/".join(str(p) for p in parts if p not in (None, ""))


# ---------------- Load + fold + CV ----------------
def load_records(csv_path, primary_col):
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit("CSV is empty")
    cols = list(rows[0].keys())
    dims = candidate_dims(cols)
    metrics = [c for c in cols if c not in dims and c not in NON_METRIC]

    # Rows written before case.json existed carry their batch size as "bs4".
    for r in rows:
        legacy = (r.get("batch_size") or "").strip()
        if not (r.get(BATCH) or "").strip() and legacy.lower().startswith("bs"):
            r[BATCH] = legacy[2:]

    # A multi-round run's warm-up round is recorded but not reported.
    rows_in = len(rows)
    rows = [r for r in rows if (r.get("warmup") or "").lower() != "true"]

    # Repeated measurements of the same config are folded to their median. The
    # key is every dimension INCLUDING every parameter, so two cases that differ
    # in any setting -- batch size, shape, streams -- stay two records; only true
    # repetitions (another round, another job, same everything) fold together.
    #
    # Which repetitions feed which median depends on whether the hardware
    # sampler ran in full for them (the `sampled` column; a single-round run is
    # always sampled):
    #   KPIs      from the unsampled ones when there are any -- the sampler's own
    #             load is not in them -- else from all
    #   the rest  (hardware medians, the pre-run snapshot) from the sampled ones
    #             when there are any -- the unsampled ones only have light rows
    #             -- else from all
    buckets = OrderedDict()
    for r in rows:
        key = tuple(r.get(d, "") or "" for d in dims)
        sampled = (r.get("sampled") or "true").lower() != "false"
        b = buckets.setdefault(key, {"on": {m: [] for m in metrics}, "off": {m: [] for m in metrics}})
        side = b["on"] if sampled else b["off"]
        for m in metrics:
            side[m].append(_parse(r.get(m, "")))

    def pick(b, m):
        on = [x for x in b["on"][m] if x is not None]
        off = [x for x in b["off"][m] if x is not None]
        if m.startswith("kpi"):
            return off or on
        return on or off

    records = []
    for key, b in buckets.items():
        rec = dict(zip(dims, key))
        for m in metrics:
            rec[m] = _median(pick(b, m))
        # repeat count & primary-throughput CV, over the same repetitions the
        # primary median was taken from (the un-folded raw values)
        pv = pick(b, primary_col) if primary_col in metrics else []
        rec["_n_runs"] = len(pv)
        rec["d_n_runs"] = len(pv) or None
        if len(pv) >= 2 and statistics.mean(pv) != 0:
            rec["d_cv_throughput_pct"] = statistics.stdev(pv) / statistics.mean(pv) * 100.0
        else:
            rec["d_cv_throughput_pct"] = None
        # What full sampling cost: the sampled rounds' primary KPI against the
        # unsampled ones', when the config has both.
        if primary_col in metrics:
            on = _median([x for x in b["on"][primary_col] if x is not None])
            off = _median([x for x in b["off"][primary_col] if x is not None])
            rec["d_sampling_overhead_pct"] = (
                (on - off) / off * 100.0 if on is not None and off not in (None, 0) else None)
        records.append(rec)
    return records, metrics, rows_in, dims


def split_dims(records, dims, primary_col):
    """(varying, fixed): dimensions that take more than one value, and the
    parameters that take exactly one -- a setting of the whole sweep, shown once
    instead of as a one-column axis. Decided over the records that measured
    something, so a case that never ran cannot add a value nobody can compare."""
    measured = [r for r in records if r.get(primary_col) is not None] or records
    varying, fixed = [], {}
    for d in dims:
        values = {r.get(d) for r in measured if r.get(d) not in (None, "")}
        if len(values) > 1:
            varying.append(d)
        elif len(values) == 1 and d.startswith(PARAM_PREFIX):
            fixed[param_label(d)] = next(iter(values))
        elif len(values) == 1:
            varying.append(d)   # a base dim with one value still labels the rows
    return varying, fixed


# ---------------- Derived metrics ----------------
# Always computed. Per-parameter comparisons are added by add_derived for every
# parameter the sweep varied (see _param_derived_names).
DERIVED = [
    ("d_cv_throughput_pct", "Primary throughput CV", "%"),
    ("d_speedup_vs_fp32",   "Speedup vs fp32",       "x"),
    ("d_rel_best_prec",     "Rel. best (precision)", "x"),
    ("d_speedup_vs_cpu",    "Speedup vs CPU",        "x"),
    ("d_rel_best_device",   "Rel. best (device)",    "x"),
    ("d_perf_per_watt",     "Efficiency",            "fps/W"),
    ("d_temp_headroom_c",   "Temp headroom",         "℃"),
    ("d_model_mem_gb",      "Memory added by the run", "GB"),
    ("d_n_runs",            "Repetitions measured",  ""),
    ("d_sampling_overhead_pct", "Sampling overhead", "%"),
]


def _param_derived_names(dim):
    """(rel-best, throughput gain, latency cost) metric names for one parameter.

    Batch size keeps the names the dashboard has always shown for it."""
    if dim == BATCH:
        return "d_rel_best_batch", "d_bs_tp_gain_pct", "d_bs_lat_cost_pct"
    name = re.sub(r"[^A-Za-z0-9]+", "_", param_label(dim)).strip("_")
    return "d_rel_best_%s" % name, "d_%s_tp_gain_pct" % name, "d_%s_lat_cost_pct" % name


def _is_numeric_dim(records, dim):
    values = [r.get(dim) for r in records if r.get(dim) not in (None, "")]
    return bool(values) and all(_sort_key(v)[0] == 0 for v in values)


def add_derived(records, profile, dims, varying):
    """Cross-record comparisons. Returns the (name, label, unit) list computed.

    A comparison along one dimension is between records that agree on EVERY
    other dimension -- including every parameter -- so a speedup vs CPU compares
    the same batch size and shape on both devices, never a mix.
    """
    tp = profile["primary_throughput"]
    power = profile["power_col"]
    lat = next((k["col"] for k in profile["kpis"] if k["goal"] == "min"), None)
    pos = {d: i for i, d in enumerate(dims)}

    def key(r):
        return tuple(r.get(d) or "" for d in dims)

    idx = {key(r): r for r in records}

    def peer(r, dim, value):
        if dim not in pos:
            return None
        k = list(key(r))
        k[pos[dim]] = value
        return idx.get(tuple(k))

    groups = {}
    for d in dims:
        for r in records:
            k = list(key(r))
            k[pos[d]] = None
            groups.setdefault((d, tuple(k)), []).append(r)

    def group_of(r, dim):
        k = list(key(r))
        k[pos[dim]] = None
        return groups.get((dim, tuple(k)), [r])

    def rel_best(r, dim):
        v = r.get(tp)
        best = _max_none([x.get(tp) for x in group_of(r, dim)])
        return v / best if v is not None and best not in (None, 0) else None

    derived = list(DERIVED)
    param_dims = [d for d in varying if d.startswith(PARAM_PREFIX)]
    for d in param_dims:
        rel, gain, cost = _param_derived_names(d)
        label = param_label(d)
        derived.append((rel, "Rel. best (%s)" % label, "x"))
        if _is_numeric_dim(records, d):
            derived.append((gain, "%s throughput gain" % label, "%"))
            derived.append((cost, "%s latency cost" % label, "%"))

    for r in records:
        for name, _, _ in derived:
            r.setdefault(name, None)
        v = r.get(tp)
        # perf/watt: whole-package power as the unified denominator
        pw = r.get(power)
        if v is not None and pw not in (None, 0):
            r["d_perf_per_watt"] = v / pw
        # temperature headroom
        tj, tc = r.get("cpu_package_tjmax_c_median"), r.get("cpu_package_temp_c_median")
        if tj is not None and tc is not None:
            r["d_temp_headroom_c"] = tj - tc
        # memory the case added over what the machine held before it launched
        used, pre = r.get("memory_used_gb_median"), r.get("pre_memory_used_gb")
        if used is not None and pre is not None:
            r["d_model_mem_gb"] = used - pre
        # precision: vs fp32 (strict; null if baseline missing) + relative to group best (robust to missing)
        if "precision" in pos:
            base = peer(r, "precision", "fp32")
            if v is not None and base is not None and base.get(tp) not in (None, 0):
                r["d_speedup_vs_fp32"] = v / base[tp]
            r["d_rel_best_prec"] = rel_best(r, "precision")
        # device: vs cpu (strict baseline) + relative to group best (robust to missing, normalized 0-1)
        if "device" in pos:
            cpu = peer(r, "device", "cpu")
            if v is not None and cpu is not None and cpu.get(tp) not in (None, 0):
                r["d_speedup_vs_cpu"] = v / cpu[tp]
            r["d_rel_best_device"] = rel_best(r, "device")
        # every swept parameter: relative to the best value, and for numeric ones
        # the gain/cost against the smallest value measured
        for d in param_dims:
            rel, gain, cost = _param_derived_names(d)
            r[rel] = rel_best(r, d)
            if not _is_numeric_dim(records, d):
                continue
            group = [x for x in group_of(r, d) if x.get(d) not in (None, "")]
            if not group:
                continue
            base = min(group, key=lambda x: _sort_key(x.get(d)))
            bv = base.get(tp)
            if v is not None and bv not in (None, 0):
                r[gain] = (v - bv) / bv * 100.0
            if lat:
                l, bl = r.get(lat), base.get(lat)
                if l is not None and bl not in (None, 0):
                    r[cost] = (l - bl) / bl * 100.0
    return derived


def _max_none(xs):
    xs = [x for x in xs if x is not None]
    return max(xs) if xs else None


# ---------------- Panel spec helpers ----------------
def panel(rows, cols, metric, how="median", filters=None, title=None, highlight=None, dims=None):
    """One pivot panel. Every dimension not on an axis or filtered is appended
    to the rows, so the table never silently takes a median ACROSS configs (two
    batch sizes in one cell). Callers pass the CSV's live dims."""
    used = set(rows) | set(cols) | set(filters.keys() if filters else [])
    remain_field = [d for d in (dims or DIMS) if d not in used]
    p = {"rows": rows + remain_field, "cols": cols, "metric": metric, "how": how, "filters": filters or {}}
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
def build_conclusions(records, metrics, profile, n_raw, dims=None):
    tp = profile["primary_throughput"]
    dims = dims or DIMS
    panel = partial(globals()["panel"], dims=dims)
    C = []

    def add(cid, cat, sev, title, detail, panels):
        C.append({"id": cid, "category": cat, "severity": sev,
                  "title": title, "detail": detail, "panels": panels})
    # ---- L2 precision/quantization ----
    add("L2-quant-overall", "L2 Precision", "info", "Quantization precision/speedup overview", "",
        [panel(["model_name", "device"], ["precision"], tp, "median", title="Throughput x precision"),
         panel(["model_name", "device"], ["precision"], "d_rel_best_prec", "median", title="Rel. best precision (0-1)")] )
    # ---- L3 device comparison (vs cpu) ----
    add("L3-dev-overall", "L3 Device", "info", "Device comparison overview", "",
        [panel(["model_name", "precision"], ["device"], tp, "median", title="Throughput x device"),
         panel(["model_name", "precision"], ["device"], "d_rel_best_device", "median",
               title="Rel. best device (0-1)", highlight="max")])
    # ---- L4 parameter sweeps (one per parameter the sweep varied) ----
    lat = next((k["col"] for k in profile["kpis"] if k["goal"] == "min"), None)
    for d in [d for d in dims if d.startswith(PARAM_PREFIX)]:
        rel, _, _ = _param_derived_names(d)
        label = param_label(d)
        pans = [panel(["model_name", "device"], [d], tp, "median",
                      title="Throughput x %s" % label, highlight="max"),
                panel(["model_name", "device"], [d], rel, "median",
                      title="Rel. best %s (0-1)" % label, highlight="max")]
        if lat:
            pans.append(panel(["model_name", "device"], [d], lat, "median",
                              title="Latency x %s" % label, highlight="min"))
        measured = [r for r in records if r.get(tp) is not None]
        values = sorted({r.get(d) for r in measured if r.get(d) not in (None, "")}, key=_sort_key)
        detail = ""
        if measured:
            b = max(measured, key=lambda r: r[tp])
            detail = "best: %s = %s" % (cfg_label(b, dims), _fmt(b[tp]))
        add("L4-sweep-" + label, "L4 Parameter sweep", "info",
            "%s sweep: %s" % (label, ", ".join(str(v) for v in values)), detail, pans)

    # ---- L6 hardware bottleneck signals (threshold-triggered) ----
    _signal(records, C, add, "d_temp_headroom_c", lambda r: r.get("d_temp_headroom_c") is not None and r["d_temp_headroom_c"] < 5,
            "L6 Bottleneck", "warn", "Hitting the thermal wall", "temp headroom <5℃, likely throttling",
            panel(["model_name", "precision"], ["device", BATCH], "d_temp_headroom_c", "min",
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
            panel(["model_name", "device"], ["precision", BATCH], "memory_bandwidth_percent_median",
                  "median", title="Memory bandwidth usage%", highlight="max"))

    return C


def _signal(records, C, add, cid, pred, cat, sev, title, detail, pan):
    hit = [r for r in records if pred(r)]
    if not hit:
        return
    varying = pan.get("rows", []) + pan.get("cols", [])
    examples = ", ".join(cfg_label(r, varying) for r in hit[:4]) + (" …" if len(hit) > 4 else "")
    add("L6-" + cid, cat, sev, "%s (%d configs)" % (title, len(hit)),
        detail + " | e.g.: " + examples, [pan])


# ---------------- Public main entry ----------------
def enrich(csv_path, profile=None):
    # read the header once to decide profile and primary
    with open(csv_path, newline="") as f:
        header = next(csv.reader(f))
    prof = resolve_profile(profile, header)
    primary = prof["primary_throughput"]

    records, base_metrics, n_raw, dims = load_records(csv_path, primary)
    varying, fixed = split_dims(records, dims, primary)
    derived = add_derived(records, prof, dims, varying)

    derived_names = [n for n, _, _ in derived]
    metrics = base_metrics + derived_names

    # grouping (including "derived analysis")
    label_map = {"kpi": "KPI Performance", "cpu": "CPU Power/Freq/Thermal", "gpu": "GPU Monitoring",
                 "npu": "NPU", "memory": "Memory", "swap": "Memory", "pre": "Before the run",
                 "d": "Derived Analysis"}
    groups = {}
    for m in metrics:
        groups.setdefault(label_map.get(m.split("_")[0], m.split("_")[0]), []).append(m)

    # unit table (for frontend fmt)
    units = {n: u for n, _, u in derived}
    for k in prof["kpis"]:
        units[k["col"]] = k["unit"]

    # Only dimensions that vary reach the pivot / HTML; a parameter with one
    # value everywhere is reported in fixed_params instead.
    effective_dims = [d for d in varying if any(r.get(d) not in (None, "") for r in records)]
    dim_values = {d: sorted({r[d] for r in records if r.get(d) not in (None, "")}, key=_sort_key)
                  for d in effective_dims}
    conclusions = build_conclusions(records, metrics, prof, n_raw, effective_dims)

    # strip internal fields when outputting records
    clean = []
    for r in records:
        clean.append({k: v for k, v in r.items() if not k.startswith("_")})

    return {
        "data": clean, "metrics": metrics, "groups": groups, "dim_values": dim_values,
        "dims": effective_dims, "fixed_params": fixed,
        "units": units, "conclusions": conclusions, "profile": prof, "n_raw": n_raw,
    }
