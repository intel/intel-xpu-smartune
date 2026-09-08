#!/usr/bin/env python3
"""
基准数据分析引擎（独立脚本，也可被 pivot_report.py import）。

职责：读原始 CSV → 用重复行算 CV → 折叠 median → 算派生指标 d_* → 扫描阈值生成"结论"。
每条结论自带若干"面板 spec"(rows/cols/metric/how/filters/highlight)，供前端一键可视化。

KPI 由可配置的 PROFILE 驱动，换 benchmark 类型(benchmark_app / genai / 自定义)只需换 profile。

用法（独立运行）:
  python analyze.py [输入csv] [profile名]
  默认: windowed_metric_medians.csv, profile 自动探测
  产物: 终端打印结论摘要 + 写出 conclusions.json
"""
import csv, json, sys, os, statistics
from collections import OrderedDict

# 候选配置维度（DIMS）：按需在此扩展，如 platform / model_source 等额外配置。
# 真正落到透视表 / HTML 的维度由数据决定：CSV 中缺失该列、或整列均为空值的维度会被自动剔除。
DIMS = ["model_name", "model_source", "platform", "device", "batch_size", "precision", "mode"]
EXCLUDE = set(DIMS + ["case_name", "case_dir"])

# ---------------- KPI Profile（核心泛化点） ----------------
PROFILES = {
    "benchmark_app": {
        "name": "benchmark_app",
        "kpis": [
            {"col": "kpi_throughput_fps",    "label": "吞吐",    "unit": "fps", "goal": "max"},
            {"col": "kpi_latency_median_ms", "label": "时延P50", "unit": "ms",  "goal": "min"},
        ],
        "primary_throughput": "kpi_throughput_fps",
        "power_col": "cpu_package_power_w_median",
    },
    # 供扩展：GenAI 类 benchmark（本 CSV 无这些列，仅示例）
    "genai": {
        "name": "genai",
        "kpis": [
            {"col": "kpi_throughput_tokens_s", "label": "吞吐",            "unit": "t/s", "goal": "max"},
            {"col": "kpi_first_latency_ms", "label": "时延",            "unit": "ms",   "goal": "min"}
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


# ---------------- 基础工具 ----------------
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


# ---------------- 加载 + 折叠 + CV ----------------
def load_records(csv_path, primary_col):
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit("CSV 为空")
    cols = list(rows[0].keys())
    dims = [d for d in DIMS if d in cols]          # 仅保留 CSV 中存在的候选维度
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
        # 重复次数 & 主吞吐 CV（用未折叠的原始值）
        pv = [x for x in b.get(primary_col, []) if x is not None] if primary_col in b else []
        rec["_n_runs"] = len(pv)
        if len(pv) >= 2 and statistics.mean(pv) != 0:
            rec["d_cv_throughput_pct"] = statistics.stdev(pv) / statistics.mean(pv) * 100.0
        else:
            rec["d_cv_throughput_pct"] = None
        records.append(rec)
    return records, metrics, len(rows), dims


# ---------------- 派生指标 ----------------
DERIVED = [
    ("d_cv_throughput_pct", "主吞吐CV",       "%"),
    ("d_speedup_vs_fp32",   "相对fp32加速",   "x"),
    ("d_rel_best_prec",     "相对最优(精度)", "x"),
    ("d_speedup_vs_cpu",    "相对CPU加速",    "x"),
    ("d_rel_best_device",   "相对最优(设备)", "x"),
    ("d_bs_tp_gain_pct",    "bs8吞吐增益",    "%"),
    ("d_bs_lat_cost_pct",   "bs8时延代价",    "%"),
    ("d_rel_best_batch",    "相对最优(批)",   "x"),
    ("d_perf_per_watt",     "能效",           "fps/W"),
    ("d_temp_headroom_c",   "温度余量",       "℃"),
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
        # perf/watt：整机封装功耗作统一分母
        pw = r.get(power)
        if v is not None and pw not in (None, 0):
            r["d_perf_per_watt"] = v / pw
        # 温度余量
        tj, tc = r.get("cpu_package_tjmax_c_median"), r.get("cpu_package_temp_c_median")
        if tj is not None and tc is not None:
            r["d_temp_headroom_c"] = tj - tc
        # 精度：vs fp32（严格；基线缺则 null） + 相对本组最优（抗缺失）
        base = g(m, d, b, "fp32")
        if v is not None and base is not None and base.get(tp) not in (None, 0):
            r["d_speedup_vs_fp32"] = v / base[tp]
        best = _max_none([g(m, d, b, pp) and g(m, d, b, pp).get(tp) for pp in _vals(records, "precision")])
        if v is not None and best not in (None, 0):
            r["d_rel_best_prec"] = v / best
        # 设备：vs cpu（严格基线） + 相对本组最优（抗缺失，归一化 0-1）
        cpu = g(m, "cpu", b, p)
        if v is not None and cpu is not None and cpu.get(tp) not in (None, 0):
            r["d_speedup_vs_cpu"] = v / cpu[tp]
        bestd = _max_none([g(m, dd, b, p) and g(m, dd, b, p).get(tp) for dd in _vals(records, "device")])
        if v is not None and bestd not in (None, 0):
            r["d_rel_best_device"] = v / bestd
        # 批：相对本组最优（归一化 0-1）
        bestb = _max_none([g(m, d, bb, p) and g(m, d, bb, p).get(tp) for bb in _vals(records, "batch_size")])
        if v is not None and bestb not in (None, 0):
            r["d_rel_best_batch"] = v / bestb

    # 批大小：bs8 相对 bs1（增益/代价挂在 bs8 行）
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


# ---------------- 面板 spec 辅助 ----------------
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


# ---------------- 结论生成 L0–L6 ----------------
def build_conclusions(records, metrics, profile, n_raw):
    tp = profile["primary_throughput"]
    C = []

    def add(cid, cat, sev, title, detail, panels):
        C.append({"id": cid, "category": cat, "severity": sev,
                  "title": title, "detail": detail, "panels": panels})
    '''
    # ---- L0 覆盖 & 可信度 ----
    failed = [r for r in records if r.get(tp) is None]
    total = len(records)
    if failed:
        by_dev = _count(failed, "device")
        by_prec = _count(failed, "precision")
        add("L0-coverage", "L0 覆盖", "warn" if failed else "good",
            "%d/%d 个 config 未跑通（无主KPI）" % (len(failed), total),
            "按设备: %s ; 按精度: %s" % (by_dev, by_prec),
            [panel(["model_name", "precision"], ["device", "batch_size"], tp, "count",
                   title="覆盖矩阵（有值=1，空=未跑通）")])
    else:
        add("L0-coverage", "L0 覆盖", "good", "全部 %d 个 config 均跑通" % total, "", [])

    shaky = [r for r in records if (r.get("d_cv_throughput_pct") or 0) > 5]
    if shaky:
        worst = max(shaky, key=lambda r: r["d_cv_throughput_pct"])
        add("L0-stability", "L0 覆盖", "warn",
            "%d 个 config 重复测试抖动 >5%%（数据慎用）" % len(shaky),
            "最抖: %s CV=%s" % (cfg_label(worst), _fmt(worst["d_cv_throughput_pct"], "%")),
            [panel(["model_name", "precision"], ["device", "batch_size"],
                   "d_cv_throughput_pct", "max", title="重复测试 CV%（越低越稳）", highlight="max")])
    else:
        add("L0-stability", "L0 覆盖", "good", "所有 config 重复测试 CV ≤5%，数据稳定", "", [])

    # ---- L1 主 KPI 排名（profile 驱动，每 KPI 一条 + 能效） ----
    kpi_specs = [k for k in profile["kpis"] if k["col"] in metrics] + \
                [{"col": "d_perf_per_watt", "label": "能效", "unit": "fps/W", "goal": "max"}]
    for k in kpi_specs:
        col, goal, unit = k["col"], k["goal"], k["unit"]
        cand = [r for r in records if r.get(col) is not None]
        if not cand:
            continue
        best = (max if goal == "max" else min)(cand, key=lambda r: r[col])
        add("L1-best-" + col, "L1 排名", "good",
            "「%s」最优: %s = %s" % (k["label"], cfg_label(best), _fmt(best[col], unit)),
            "goal=%s（%s）" % (goal, "越大越好" if goal == "max" else "越小越好"),
            [panel(["model_name", "precision"], ["device", "batch_size"], col, "median",
                   title="%s 全景（高亮%s）" % (k["label"], "最优"),
                   highlight="max" if goal == "max" else "min")])

    # 各模型最佳主-吞吐配置（一条，多面板）
    models = _vals(records, "model_name")
    lines, pans = [], []
    for mdl in models:
        cand = [r for r in records if r.get("model_name") == mdl and r.get(tp) is not None]
        if not cand:
            continue
        b = max(cand, key=lambda r: r[tp])
        lines.append("%s→%s/%s/%s(%s)" % (mdl, b["device"], b["precision"], b["batch_size"], _fmt(b[tp])))
        pans.append(panel(["device", "precision"], ["batch_size"], tp, "median",
                          filters={"model_name": mdl}, title="%s 最佳吞吐配置" % mdl, highlight="max"))
    if pans:
        add("L1-best-per-model", "L1 排名", "info",
            "各模型最佳吞吐配置", " ; ".join(lines), pans)

    # 设备排名（按主吞吐在多少 (模型,精度,批) 组合上夺冠）
    wins = _device_wins(records, tp)
    if wins:
        rank = " > ".join("%s(%d胜)" % (d, n) for d, n in wins)
        add("L1-device-rank", "L1 排名", "info",
            "设备吞吐排名: %s" % rank, "统计各设备在多少组合上吞吐第一",
            [panel(["model_name", "precision"], ["device"], tp, "median",
                   title="设备 × 模型/精度 吞吐", highlight="max")])
    '''
    # ---- L2 精度/量化 ----
    add("L2-quant-overall", "L2 精度", "info", "量化精度/加速概览", "",
        [panel(["model_name", "device"], ["precision"], tp, "median", title="吞吐 × 精度"),
         panel(["model_name", "device"], ["precision"], "d_rel_best_prec", "median", title="相对最优精度 (0-1)")] )
    '''
    for prec in [p for p in _vals(records, "precision") if p != "fp32"]:
        pairs = [r["d_speedup_vs_fp32"] for r in records
                 if r.get("precision") == prec and r.get("d_speedup_vs_fp32") is not None]
        denom = sum(1 for r in records if r.get("precision") == prec and r.get(tp) is not None)
        gm = _geomean(pairs)
        if gm is None:
            continue
        sev = "good" if gm > 1.05 else "warn" if gm < 0.95 else "info"
        add("L2-quant-" + prec, "L2 精度", "info",
            "%s 相对 fp32 几何平均 %s（基于 %d/%d 组两端齐全）" % (prec, _fmt(gm, "x"), len(pairs), denom),
            "缺 fp32 基线的组已剔除，不外溢污染",
            [panel(["model_name", "device"], ["precision"], tp, "median",
                   title="吞吐 × 精度"),
             panel(["model_name", "device"], ["precision"], "d_speedup_vs_fp32", "median",
                   title="相对fp32加速 ×", highlight="max"),
             panel(["model_name", "device"], ["precision"], "d_rel_best_prec", "median",
                   title="相对最优精度 (0-1)", highlight="max")])
    
    # 量化收益按设备排序
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
        add("L2-quant-by-device", "L2 精度", "info",
            "int8 量化收益按设备: %s" % txt, "各设备 int8 相对 fp32 的几何平均加速",
            [panel(["device"], ["model_name"], "d_speedup_vs_fp32", "median",
                   filters={"precision": "int8"}, title="int8 加速 × 设备", highlight="max")])
    '''
    # ---- L3 设备对比（vs cpu） ----
    add("L3-dev-overall", "L3 设备", "info", "设备对比概览", "",
        [panel(["model_name", "precision"], ["device"], tp, "median", title="吞吐 × 设备"),
         panel(["model_name", "precision"], ["device"], "d_rel_best_device", "median",
               title="相对最优设备 (0-1)", highlight="max")])
    '''
    for dev in [d for d in _vals(records, "device") if d != "cpu"]:
        sp = [r["d_speedup_vs_cpu"] for r in records
              if r.get("device") == dev and r.get("precision") == "int8" and r.get("d_speedup_vs_cpu") is not None]
        gm = _geomean(sp)
        if gm is None:
            continue
        add("L3-dev-" + dev, "L3 设备", "good" if gm > 1 else "info",
            "%s 相对 CPU 平均 %s（int8, 基于 %d 组）" % (dev.upper(), _fmt(gm, "x"), len(sp)),
            "同 (模型,精度,批) 内比较",
            [panel(["model_name", "precision"], ["device"], tp, "median", title="吞吐 × 设备"),
             panel(["model_name", "precision"], ["device"], "d_speedup_vs_cpu", "median",
                   filters={"precision": "int8"}, title="相对CPU加速 ×", highlight="max"),
             panel(["model_name", "precision"], ["device"], "d_rel_best_device", "median",
                   title="相对最优设备 (0-1)", highlight="max")])
    '''
    # ---- L4 批大小 ----
    add("L4-batch-overall", "L4 批大小", "info", "批大小对比概览", "",
        [panel(["model_name", "device"], ["batch_size"], tp, "median", title="吞吐 × 批大小"),
         panel(["model_name", "device"], ["batch_size"], "d_rel_best_batch", "median",
               title="相对最优批 (0-1)", highlight="max")])
    '''
    tg = _geomean([1 + (r["d_bs_tp_gain_pct"] / 100.0) for r in records if r.get("d_bs_tp_gain_pct") is not None])
    lc = [r["d_bs_lat_cost_pct"] for r in records if r.get("d_bs_lat_cost_pct") is not None]
    if tg is not None:
        gain_pct = (tg - 1) * 100
        lc_med = _median(lc)
        lat = next((k["col"] for k in profile["kpis"] if k["goal"] == "min"), None)
        pans = [panel(["model_name", "device"], ["batch_size"], tp, "median",
                      title="吞吐 × 批大小", highlight="max"),
                panel(["model_name", "device"], ["batch_size"], "d_rel_best_batch", "median",
                      title="相对最优批 (0-1)", highlight="max")]
        if lat:
            pans.append(panel(["model_name", "device"], ["batch_size"], lat, "median",
                              title="时延 × 批大小", highlight="min"))
        add("L4-batch", "L4 批大小", "info",
            "bs8 相对 bs1: 吞吐 %s%s，时延 %s" % ("+" if gain_pct >= 0 else "", _fmt(gain_pct, "%"),
                                              ("+" + _fmt(lc_med, "%")) if (lc_med or 0) >= 0 else _fmt(lc_med, "%")),
            "吞吐-时延权衡；几何平均吞吐增益 / 中位时延代价",
            pans)
    '''
    '''    # ---- L5 能效 ----
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
            lead = "；%s 能效领先 %s %s" % (dev_eff[0][0].upper(), dev_eff[-1][0].upper(),
                                        _fmt(dev_eff[0][1] / dev_eff[-1][1], "x"))
        add("L5-efficiency", "L5 能效", "good",
            "最省电: %s = %s" % (cfg_label(be), _fmt(be["d_perf_per_watt"], "fps/W")) + lead,
            "能效 = 主吞吐 / 整机封装功耗(W)",
            [panel(["model_name", "device"], ["precision"], "d_perf_per_watt", "median",
                   title="能效 fps/W（高亮最优）", highlight="max")])
    '''
    # ---- L6 硬件瓶颈信号（阈值触发） ----
    _signal(records, C, add, "d_temp_headroom_c", lambda r: r.get("d_temp_headroom_c") is not None and r["d_temp_headroom_c"] < 5,
            "L6 瓶颈", "warn", "触及温度墙", "温度余量<5℃，疑降频",
            panel(["model_name", "precision"], ["device", "batch_size"], "d_temp_headroom_c", "min",
                  title="温度余量 ℃（越低越危险）", highlight="min"))
    _signal(records, C, add, "gpu_busy",
            lambda r: r.get("device") == "gpu" and (r.get("gpu_compute_busy_percent_median") or 0) < 95,
            "L6 瓶颈", "info", "GPU 存在计算引擎未满载(<95%)", "GPU 计算可能未充分利用",
            panel(["model_name", "precision"], ["precision"], "gpu_compute_busy_percent_median",
                  "median", filters={"device": "gpu"}, title="GPU 计算引擎占用%", highlight="min"))
    _signal(records, C, add, "npu_util",
            lambda r: r.get("device") == "npu" and (r.get("npu_utilization_percent_median") is not None) and r["npu_utilization_percent_median"] < 95,
            "L6 瓶颈", "info", "NPU 存在利用率偏低(<95%)", "NPU 计算可能未充分利用",
            panel(["model_name", "precision"], ["precision"], "npu_utilization_percent_median",
                  "median", filters={"device": "npu"}, title="NPU 利用率%", highlight="min"))
    _signal(records, C, add, "mem_bw",
            lambda r: (r.get("memory_bandwidth_percent_median") or 0) > 80,
            "L6 瓶颈", "warn", "内存带宽占用>80%", "疑访存瓶颈",
            panel(["model_name", "device"], ["precision", "batch_size"], "memory_bandwidth_percent_median",
                  "median", title="内存带宽占用%", highlight="max"))

    return C


def _signal(records, C, add, cid, pred, cat, sev, title, detail, pan):
    hit = [r for r in records if pred(r)]
    if not hit:
        return
    examples = ", ".join(cfg_label(r) for r in hit[:4]) + (" …" if len(hit) > 4 else "")
    add("L6-" + cid, cat, sev, "%s（%d 个 config）" % (title, len(hit)),
        detail + " | 例: " + examples, [pan])


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


# ---------------- 对外主入口 ----------------
def enrich(csv_path, profile=None):
    # 先读一遍列名以定 profile 与 primary
    with open(csv_path, newline="") as f:
        header = next(csv.reader(f))
    prof = resolve_profile(profile, header)
    primary = prof["primary_throughput"]

    records, base_metrics, n_raw, dims = load_records(csv_path, primary)
    add_derived(records, prof)

    derived_names = [n for n, _, _ in DERIVED]
    metrics = base_metrics + derived_names

    # 分组（含"派生分析"）
    label_map = {"kpi": "KPI 性能", "cpu": "CPU 功耗/频率/热", "gpu": "GPU 监控",
                 "npu": "NPU", "memory": "内存", "d": "派生分析"}
    groups = {}
    for m in metrics:
        groups.setdefault(label_map.get(m.split("_")[0], m.split("_")[0]), []).append(m)

    # 单位表（供前端 fmt）
    units = {n: u for n, _, u in DERIVED}
    for k in prof["kpis"]:
        units[k["col"]] = k["unit"]

    # 剔除整列空值的维度：只有存在非空取值的维度才最终落到透视 / HTML
    effective_dims = [d for d in dims if any(r.get(d) not in (None, "") for r in records)]
    dim_values = {d: sorted({r[d] for r in records if r.get(d) not in (None, "")})
                  for d in effective_dims}
    conclusions = build_conclusions(records, metrics, prof, n_raw)

    # 输出 records 时去掉内部字段
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
        raise SystemExit("找不到输入文件: " + inp)
    res = enrich(inp, prof)
    print("profile = %s | %d 原始行 -> %d config | %d 指标(含%d派生) | %d 条结论" % (
        res["profile"]["name"], res["n_raw"], len(res["data"]),
        len(res["metrics"]), len(DERIVED), len(res["conclusions"])))
    print("-" * 70)
    cat = None
    for c in res["conclusions"]:
        if c["category"] != cat:
            cat = c["category"]
            print("\n【%s】" % cat)
        print("  %s %s" % (SEV_MARK.get(c["severity"], "·"), c["title"]))
        if c["detail"]:
            print("      %s" % c["detail"])
        if c["panels"]:
            print("      面板×%d" % len(c["panels"]))
    out = os.path.join(os.path.dirname(os.path.abspath(inp)), "conclusions.json")
    with open(out, "w") as f:
        json.dump({"profile": res["profile"], "conclusions": res["conclusions"]}, f, ensure_ascii=False, indent=2)
    print("\n已写出:", out)


if __name__ == "__main__":
    main()
