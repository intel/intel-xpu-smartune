# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Read the benchmark pipeline's result artifacts for the dashboard.
#
# Each backend template (benchmark_{genai,ovms,app}_common.sh) writes one
# summary.tsv per run directory:
#
#   <benchmarks>/<backend>/<RUN_NAME>_<DEVICE>/summary.tsv
#   <benchmarks>/<backend>/<RUN_NAME>_<DEVICE>/<model>_<quant>/benchmark.log
#
# summary.tsv says whether each case ran; it carries no numbers. Those come from
# windowed_metric_medians.csv, which the pipeline's aggregation step writes by
# scraping every case's detail.log for KPIs and taking medians over the
# measurement window of the run-level sampling CSV (sampler.py). The two are
# joined here on the case directory.
#
# That CSV sits one level ABOVE the run directories, at
#
#   <benchmarks>/<backend>/windowed_metric_medians.csv
#   <benchmarks>/<backend>/pivot_report.html
#
# because run_template.sh invokes the aggregator once per backend, over the whole
# backend tree -- so one file covers TEST_CPU, TEST_GPU and TEST_NPU together.
# Both locations are searched anyway: the aggregator can also be pointed at a
# single run directory by hand, and a tree produced that way has it there.
#
# summary.tsv's columns differ per backend (genai has `device`, ovms has `port`),
# so the header row is honoured rather than assumed.
#
# Nothing here is deduplicated. A run directory is named after the run that
# produced it (runner.py exports BENCH_RUN_NAME; the wrappers default RUN_NAME to
# it), so benchmarking the same model, precision and device twice leaves two case
# directories with two measurements, and both are returned. Deciding which of
# them is "the" result is a question about the numbers, not about the files, and
# it is answered where the numbers are read -- in the dashboard, which groups the
# repetitions of one test together and marks the best.

import csv
import importlib.util
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from utils.logger import logger

from benchmark.service import env

# Backends in the order the UI should show them.
BACKENDS = ("genai", "ovms", "app")

# The pivot report, built from the medians CSV below.
_REPORT_NAMES = ("pivot_report.html", "report.html")

# Written by benchmark/scripts/analysis/export_windowed_metric_medians.py.
_MEDIANS_NAME = "windowed_metric_medians.csv"

# The pivot report's machine-readable half, written next to the HTML. Carries the
# same cases as the medians CSV plus the derived comparisons (speedup vs CPU,
# performance per watt, ...) already computed across the whole backend -- which is
# the one thing this service could not recompute per run directory, since a
# speedup is only defined against the other devices in the same sweep.
_PIVOT_JSON_NAME = "pivot_report.json"

# Columns of the medians CSV that only describe the case, not its measurements.
# summary.tsv already identifies the case, so they are dropped on merge.
_MEDIANS_DIMENSION_COLUMNS = frozenset({
    "case_name", "case_dir", "model_name", "model_source",
    "platform", "device", "batch_size", "precision", "mode",
})


# metric metadata
#
# What a metric key means, so the dashboard does not have to guess. It used to
# carry a hardcoded label table and no notion of direction at all, which is fine
# for a plain table and not enough for a comparison: highlighting the best cell
# in a row, or colouring a bar by how good it is, needs to know whether more is
# better -- and no naming convention says whether "cpu_usage_percent" wants to be
# high or low. So the answer is stated here, once, next to the code that reads
# the files.
#
# `higher_is_better` is deliberately three-valued. None means the metric has no
# direction (frequency, input length, utilisation): showing it is useful,
# ranking by it is not, and a UI that assumed a direction would confidently mark
# a "best" that means nothing.

# Display order of the groups, and the label each gets.
_METRIC_GROUPS = (
    ("kpi", "KPI"),
    ("derived", "Comparison"),
    ("cpu", "CPU"),
    ("memory", "Memory"),
    ("gpu", "GPU"),
    ("npu", "NPU"),
)

# Only where the derived form below would be wrong or clumsy.
#
# Lowercase except for acronyms, because these names are read in a table header
# beside their unit ("TTFT (ms)", "memory used (GB)") rather than as sentences,
# and a column of Title Case competes with the numbers under it for emphasis.
# TTFT and TPOT are the names the two latencies are universally known by; the
# descriptive "First token" / "Next token" moved into _METRIC_NOTES, which is
# what the header tooltip shows.
_METRIC_LABELS = {
    "kpi_first_latency_ms": "TTFT",
    "kpi_second_latency_ms": "TPOT",
    "kpi_throughput_tokens_s": "throughput",
    "kpi_input_token_size": "input length",
    "cpu_usage_percent_median": "CPU usage",
    "cpu_p_core_usage_percent_median": "P-core usage",
    "cpu_e_core_usage_percent_median": "E-core usage",
    "cpu_p_core_frequency_mhz_median": "P-core clock",
    "cpu_e_core_frequency_mhz_median": "E-core clock",
    "cpu_package_power_w_median": "CPU package power",
    "cpu_package_temp_c_median": "CPU package temp",
    "cpu_package_tjmax_c_median": "CPU Tjmax",
    # The absolute and the proportional reading of the same thing both appear in
    # one table, so each says which it is rather than both reading "memory used".
    "memory_used_gb_median": "memory used",
    "memory_available_gb_median": "memory free",
    "memory_used_percent_median": "memory used, share",
    "memory_bandwidth_gb_s_median": "memory bandwidth",
    "memory_bandwidth_percent_median": "memory bandwidth, share",
    "gpu_power_w_median": "GPU power",
    "gpu_frequency_mhz_median": "GPU clock",
    "gpu_render_busy_percent_median": "GPU render busy",
    "gpu_compute_busy_percent_median": "GPU compute busy",
    "gpu_video_busy_percent_median": "GPU video busy",
    "npu_utilization_percent_median": "NPU utilisation",
    "npu_power_w_median": "NPU power",
    "npu_frequency_mhz_median": "NPU clock",
    "npu_bandwidth_mib_s_median": "NPU bandwidth",
    "npu_temperature_c_median": "NPU temp",
    "d_cv_throughput_pct": "throughput variance",
    "d_speedup_vs_fp32": "speedup vs fp32",
    "d_speedup_vs_cpu": "speedup vs CPU",
    "d_rel_best_prec": "vs best precision",
    "d_rel_best_device": "vs best device",
    "d_rel_best_batch": "vs best batch",
    "d_bs_tp_gain_pct": "batching throughput gain",
    "d_bs_lat_cost_pct": "batching latency cost",
    "d_perf_per_watt": "performance per watt",
    "d_temp_headroom_c": "temperature headroom",
}

# Suffix -> unit. Longest match wins, so "_gb_s_median" beats "_gb_median".
_METRIC_UNITS = (
    ("_tokens_s", "tok/s"),
    ("_gb_s_median", "GB/s"),
    ("_mib_s_median", "MiB/s"),
    ("_mhz_median", "MHz"),
    ("_percent_median", "%"),
    ("_gb_median", "GB"),
    ("_w_median", "W"),
    ("_c_median", "°C"),
    ("_pct", "%"),
    ("_ms", "ms"),
    ("_c", "°C"),
)

# Metrics whose direction is known. Everything else is None -- see above.
_HIGHER_IS_BETTER = {
    "kpi_throughput_tokens_s": True,
    "kpi_first_latency_ms": False,
    "kpi_second_latency_ms": False,
    "cpu_package_power_w_median": False,
    "cpu_package_temp_c_median": False,
    "gpu_power_w_median": False,
    "npu_power_w_median": False,
    "npu_temperature_c_median": False,
    "memory_used_gb_median": False,
    "memory_used_percent_median": False,
    "d_speedup_vs_fp32": True,
    "d_speedup_vs_cpu": True,
    "d_rel_best_prec": True,
    "d_rel_best_device": True,
    "d_rel_best_batch": True,
    "d_perf_per_watt": True,
    "d_temp_headroom_c": True,
    "d_bs_tp_gain_pct": True,
    "d_bs_lat_cost_pct": False,
    "d_cv_throughput_pct": False,
}

# Units the suffix rules cannot see, because they are not in the name. The pivot
# report calls perf-per-watt "fps/W" from its generic profile; for an LLM sweep
# the numerator is the throughput KPI, which is tokens per second.
_METRIC_UNIT_OVERRIDES = {
    "d_speedup_vs_fp32": "×",
    "d_speedup_vs_cpu": "×",
    "d_rel_best_prec": "×",
    "d_rel_best_device": "×",
    "d_rel_best_batch": "×",
    "d_perf_per_watt": "tok/s/W",
    "kpi_input_token_size": "tokens",
}

# What a number actually means, where the name does not say and the answer
# changes how it should be read.
#
# The derived metrics are the ones that need this most. Every one of them is a
# ratio, and a ratio is only interpretable if you know its denominator: a column
# of 1.00× reads as "no difference" when it can equally mean "this row IS the
# baseline" or "this sweep had nothing to compare against". Both happen here --
# a CPU row's speedup-vs-CPU is 1.00× by definition, and vs-best-batch is 1.00×
# for every row of a sweep that used a single batch size. So the definition
# travels with the metric and the UI shows it on hover.
_METRIC_NOTES = {
    "kpi_first_latency_ms": (
        "Time to first token: from submitting the prompt to the first token out. "
        "Paid once per request, and it scales with the prompt."
    ),
    "kpi_second_latency_ms": (
        "Time per output token, after the first. Paid for every token; "
        "throughput is its reciprocal."
    ),
    "kpi_throughput_tokens_s": "Generated tokens per second, over the whole generation.",
    "kpi_input_token_size": "Length of the prompt the case was run with.",
    "d_speedup_vs_cpu": (
        "Throughput relative to the CPU running the same model at the same "
        "precision. CPU rows are 1.00× by definition -- they are the baseline."
    ),
    "d_rel_best_device": (
        "Throughput relative to the fastest device that produced a measurement "
        "for this model and precision. The fastest one is 1.00×."
    ),
    "d_rel_best_prec": (
        "Throughput relative to the fastest precision that produced a "
        "measurement on this device. A device where only one precision ran is "
        "1.00× because it has nothing to be compared with -- not because it won."
    ),
    "d_rel_best_batch": (
        "Throughput relative to the best batch size for this model, precision "
        "and device. 1.00× throughout when the sweep used a single batch size."
    ),
    "d_speedup_vs_fp32": (
        "Throughput relative to the same model at fp32. Absent when the sweep "
        "had no fp32 baseline -- fp16 does not substitute for one."
    ),
    "d_perf_per_watt": (
        "Throughput divided by CPU package power, for every device: that is the "
        "power column the pipeline's report profile names. An accelerator's own "
        "rail is NOT in the denominator, so a GPU or NPU figure counts only what "
        "the CPU package drew while it ran."
    ),
    "d_temp_headroom_c": "Tjmax minus the median CPU package temperature.",
    "d_cv_throughput_pct": (
        "Spread of throughput across the run's iterations, as a coefficient of "
        "variation. Needs more than one iteration to exist."
    ),
    "d_bs_tp_gain_pct": "Throughput gained by batching, against the smallest batch size measured.",
    "d_bs_lat_cost_pct": "Latency added by batching, against the smallest batch size measured.",
    "cpu_package_power_w_median": "Median over the measurement window, not a peak.",
    "cpu_package_tjmax_c_median": "The junction temperature limit this CPU throttles at.",
    "memory_used_percent_median": "Share of total system memory in use.",
    "memory_bandwidth_percent_median": "Share of the memory controller's theoretical peak bandwidth.",
    "npu_power_w_median": "Absent on drivers that do not expose an NPU power rail.",
    "npu_temperature_c_median": "Absent on drivers that do not expose an NPU temperature sensor.",
}


def _metric_group(key: str) -> str:
    if key.startswith("d_"):
        return "derived"
    prefix = key.split("_", 1)[0]
    return prefix if prefix in {g for g, _ in _METRIC_GROUPS} else "other"


def _metric_unit(key: str) -> Optional[str]:
    override = _METRIC_UNIT_OVERRIDES.get(key)
    if override:
        return override
    for suffix, unit in _METRIC_UNITS:
        if key.endswith(suffix):
            return unit
    return None


def _metric_label(key: str) -> str:
    """A readable name for a metric key, known or not.

    The fallback matters: a KPI added by an upstream llm_bench should show up
    with a plain name rather than be dropped for not being in the table above.
    """
    label = _METRIC_LABELS.get(key)
    if label:
        return label
    stem = re.sub(r"_median$", "", key)
    stem = re.sub(r"^(kpis?|d)_", "", stem)
    return stem.replace("_", " ").strip() or key


def metric_meta(key: str) -> dict:
    group = _metric_group(key)
    return {
        "key": key,
        "label": _metric_label(key),
        "unit": _metric_unit(key),
        "higher_is_better": _HIGHER_IS_BETTER.get(key),
        "group": group,
        "group_label": dict(_METRIC_GROUPS).get(group, "Other"),
        "description": _METRIC_NOTES.get(key),
    }


def _sorted_metric_meta(keys) -> List[dict]:
    """Metric descriptors, grouped in report order rather than discovery order."""
    order = {name: index for index, (name, _) in enumerate(_METRIC_GROUPS)}
    return sorted(
        (metric_meta(key) for key in keys),
        key=lambda m: (order.get(m["group"], len(order)), m["label"]),
    )


def _read_summary(summary_path: Path) -> List[dict]:
    rows: List[dict] = []
    try:
        with open(summary_path, newline="", encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                # Skip blank trailing lines and any row the writer left half-formed.
                if not row or not (row.get("model") or "").strip():
                    continue
                rows.append({k: (v or "").strip() for k, v in row.items() if k})
    except OSError as exc:
        logger.warning(f"Unreadable benchmark summary {summary_path}: {exc}")
    return rows


def _artifact(run_dir: Path, name: str) -> Optional[Path]:
    """Locate a per-run artifact, in the run directory or its backend directory.

    See the header: the aggregation step is normally run over a whole backend, so
    its output covers every run directory under it and sits alongside them.
    """
    for candidate in (run_dir / name, run_dir.parent / name):
        if candidate.is_file():
            return candidate
    return None


def _medians_rows(run_dir: Path) -> List[dict]:
    """The medians CSV as written, dimensions and measurements together.

    Three readers below take different slices of the same rows, so the file is
    parsed once per caller rather than each of them opening it.
    """
    path = _artifact(run_dir, _MEDIANS_NAME)
    if path is None:
        return []
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            return [row for row in csv.DictReader(fh) if row]
    except OSError as exc:
        logger.warning(f"Unreadable benchmark medians {path}: {exc}")
        return []


def _index_by_case(pairs: List[Tuple[dict, dict]]) -> Dict[str, dict]:
    """Key each value by its case directory and, where unambiguous, its case name.

    The aggregator writes an absolute case_dir, so that is the exact key. The
    bare directory name is a fallback for a tree that has been moved since it was
    produced -- but a backend-wide CSV holds the same case name once per device
    ("Qwen_Qwen3-0_6B_int4_ov" under TEST_CPU, TEST_GPU and TEST_NPU alike), and
    a fallback that resolved those to whichever came first would quietly report
    one device's numbers under another. So a name that occurs more than once
    gets no fallback key: showing nothing is recoverable, showing the wrong
    device's throughput is not.
    """
    by_case: Dict[str, dict] = {}
    by_name: Dict[str, dict] = {}
    ambiguous: Set[str] = set()
    for row, value in pairs:
        case_dir = (row.get("case_dir") or "").strip()
        if case_dir:
            by_case.setdefault(case_dir, value)
        name = (row.get("case_name") or "").strip()
        if name:
            if name in by_name:
                ambiguous.add(name)
            by_name.setdefault(name, value)
    for name, value in by_name.items():
        if name not in ambiguous:
            by_case.setdefault(name, value)
    return by_case


def _read_medians(run_dir: Path) -> Dict[str, Dict[str, float]]:
    """Measurements per case.

    Values are floats -- a blank means the collector was unavailable for that
    case, and is dropped rather than shown as 0.
    """
    pairs: List[Tuple[dict, dict]] = []
    for row in _medians_rows(run_dir):
        metrics: Dict[str, float] = {}
        for key, raw in row.items():
            if not key or key in _MEDIANS_DIMENSION_COLUMNS:
                continue
            try:
                metrics[key] = float(raw)
            except (TypeError, ValueError):
                continue
        if metrics:
            pairs.append((row, metrics))
    return _index_by_case(pairs)


def _read_dimensions(run_dir: Path) -> Dict[str, Dict[str, str]]:
    """How the aggregator classified each case: device, precision, mode, ...

    summary.tsv names a case its own way -- device "NPU" against the CSV's "npu",
    quant "int4_ov" against precision "int4" -- and those spellings are what a
    comparison view has to group by. Taking them from the aggregator instead of
    re-deriving them keeps the dashboard's axes identical to the report's.
    """
    keys = _MEDIANS_DIMENSION_COLUMNS - {"case_name", "case_dir"}
    return _index_by_case([
        (row, {key: (row.get(key) or "").strip() for key in keys if row.get(key)})
        for row in _medians_rows(run_dir)
    ])


def _pivot_payload(run_dir: Path) -> dict:
    """pivot_report.json for this run's backend, or an empty mapping."""
    path = _artifact(run_dir, _PIVOT_JSON_NAME)
    if path is None:
        return {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning(f"Unreadable benchmark pivot report {path}: {exc}")
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_derived(run_dir: Path) -> Dict[str, Dict[str, float]]:
    """Cross-case comparisons per case: speedup vs CPU, perf per watt, ...

    These are the one thing this service cannot recompute from a single case: a
    speedup is only defined against the other devices in the same sweep. The
    pivot report already computed them over the whole backend, so they are read
    back rather than re-derived.

    Its rows carry no case directory, only the grouping the report declares in
    `dims` -- model, device, precision, mode -- so they are matched to the
    medians rows, which do carry one, on exactly that grouping.

    That grouping does NOT include the run, and since runner.py started giving
    every run its own directory (benchmark/templates/benchmark_*_common.sh take
    RUN_NAME from BENCH_RUN_NAME), the same test measured twice is two medians
    rows under one pivot row. The report's figure is the group's -- it was
    computed from the medians across every repetition -- so it is attached to
    each of them rather than dropped: a repeated test would otherwise lose its
    whole Comparison column, which is the opposite of what repeating it was for.
    Two repetitions therefore show the same speedup, which is correct; what
    differs between them is what was measured, not what the sweep concluded.
    """
    payload = _pivot_payload(run_dir)
    rows = payload.get("data")
    dims = [d for d in (payload.get("dims") or []) if isinstance(d, str)]
    if not isinstance(rows, list) or not dims:
        return {}

    by_dims: Dict[Tuple[str, ...], Dict[str, float]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        derived = {
            name: float(value)
            for name, value in row.items()
            if name.startswith("d_") and isinstance(value, (int, float))
            and not isinstance(value, bool)
        }
        if derived:
            by_dims[tuple(str(row.get(dim) or "").strip() for dim in dims)] = derived

    pairs: List[Tuple[dict, dict]] = []
    for row in _medians_rows(run_dir):
        derived = by_dims.get(tuple((row.get(dim) or "").strip() for dim in dims))
        if derived:
            pairs.append((row, derived))
    return _index_by_case(pairs)


def _primary_metric(run_dir: Path) -> Optional[str]:
    """The metric the pipeline's report profile treats as the headline one.

    Which measurement decides "the best run of this test" is a property of the
    workload, not of the dashboard: a text-generation profile says throughput, a
    classification one would say something else. The report already declares it,
    so the UI is told rather than left to hardcode a guess.
    """
    profile = _pivot_payload(run_dir).get("profile")
    if not isinstance(profile, dict):
        return None
    primary = profile.get("primary_throughput")
    return primary if isinstance(primary, str) and primary else None


# A traceback's last exception line, and the "Exception from <file>:<line>:"
# frames OpenVINO stacks above the message that actually says what went wrong.
_EXCEPTION_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception)):\s*(.*)$")
_EXCEPTION_FRAME_RE = re.compile(r"^Exception from \S+:\d+:\s*$")
_LOG_LEVEL_RE = re.compile(r"^\[\s*[A-Z]+\s*\]")
_FAILURE_REASON_MAX = 400


def _failure_reason(case_dir: Path, max_bytes: int = 64 * 1024) -> Optional[str]:
    """One line saying why a case failed, pulled from its detail.log.

    summary.tsv records only "failed", so the dashboard could say a case did not
    run but never why -- and "the NPU compiler rejects this quantisation recipe"
    and "the device fell off the bus" call for completely different responses
    from whoever is reading. The whole log stays one click away; this is the
    part worth putting in a table cell.
    """
    log = case_dir / "detail.log"
    try:
        size = log.stat().st_size
        with open(log, encoding="utf-8", errors="replace") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            lines = fh.read().splitlines()
    except OSError:
        return None

    for index in range(len(lines) - 1, -1, -1):
        match = _EXCEPTION_RE.match(lines[index].strip())
        if not match:
            continue
        parts = [match.group(1) + ":", match.group(2)]
        # The message often continues on the following lines, through a chain of
        # nested plugin frames; keep going until the log's own output resumes.
        for line in lines[index + 1:]:
            stripped = line.strip()
            if not stripped or _LOG_LEVEL_RE.match(stripped):
                break
            if _EXCEPTION_FRAME_RE.match(stripped):
                continue
            parts.append(stripped)
        reason = " ".join(part for part in parts if part)
        break
    else:
        # No traceback: the runner may have logged the failure itself.
        errors = [l.strip() for l in lines if l.strip().startswith(("[ERROR]", "[ ERROR ]"))]
        if not errors:
            return None
        reason = errors[-1]

    reason = re.sub(r"\s+", " ", reason).strip()
    if len(reason) > _FAILURE_REASON_MAX:
        reason = reason[:_FAILURE_REASON_MAX - 1].rstrip() + "…"
    return reason or None


def _case_keys(row: dict) -> List[str]:
    """Join keys for one summary row, most specific first.

    summary.tsv records the per-case log file, whose parent is exactly the case
    directory the aggregator recorded.
    """
    log_file = (row.get("log_file") or "").strip()
    if not log_file:
        return []
    case_dir = Path(log_file).parent
    return [str(case_dir), case_dir.name]


def _find_report(run_dir: Path) -> Optional[str]:
    for name in _REPORT_NAMES:
        candidate = _artifact(run_dir, name)
        if candidate is not None:
            return str(candidate)
    return None


# Jobs
#
# A run directory is one device's half of one job: runner.py picks a single
# BENCH_RUN_NAME per request and the wrappers append the device to it, so
# benchmarking two precisions on CPU and NPU leaves 20260902_181245_ab12cd_CPU
# and 20260902_181245_ab12cd_NPU. The dashboard groups by what the user actually
# started -- the job -- so the name is put back together here rather than each
# reader learning the convention.

_JOB_STARTED_RE = re.compile(r"^(\d{8}_\d{6})")


def _job_name(run_dir_name: str, devices: Iterable[str]) -> str:
    """The run directory name with its device suffix removed.

    Longest suffix first, so a device whose name contains another ("GPU" inside
    a hypothetical "DGPU") cannot strip the wrong number of characters. A
    directory that ends in no known device -- a tree assembled by hand, or the
    legacy "TEST" one -- is its own job, which is the honest answer.
    """
    candidates = {str(d).strip() for d in devices if str(d).strip()}
    candidates.update(("CPU", "GPU", "NPU"))
    for device in sorted(candidates, key=len, reverse=True):
        suffix = f"_{device}"
        if run_dir_name.lower().endswith(suffix.lower()):
            return run_dir_name[: -len(suffix)] or run_dir_name
    return run_dir_name


def _job_started_at(job: str) -> Optional[float]:
    """When the job started, from the timestamp runner.py named it with.

    None for a name that carries no timestamp; the UI falls back to the run's
    mtime, which is when it finished writing rather than when it began -- close
    enough to sort by, and the only thing such a tree has.
    """
    match = _JOB_STARTED_RE.match(job)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").timestamp()
    except ValueError:
        return None


def collect(backend: Optional[str] = None) -> dict:
    """All result rows currently on disk, grouped by run directory."""
    root = env.paths()["benchmarks"]
    backends = (backend,) if backend else BACKENDS

    runs = []
    measured_runs = 0
    primary_metric: Optional[str] = None
    for name in backends:
        backend_dir = root / name
        if not backend_dir.is_dir():
            continue
        for run_dir in sorted(backend_dir.iterdir()):
            summary = run_dir / "summary.tsv"
            if not summary.is_file():
                continue
            rows = _read_summary(summary)
            medians = _read_medians(run_dir)
            derived = _read_derived(run_dir)
            dimensions = _read_dimensions(run_dir)

            # Union of the metric columns actually present, in first-seen order,
            # so the UI can build its table without knowing the schema.
            metric_columns: List[str] = []
            seen = set()
            for row in rows:
                for key in _case_keys(row):
                    metrics = medians.get(key)
                    if metrics is None:
                        continue
                    # Measured and derived are one namespace to the UI: both are
                    # numbers about this case, and which file they came from is
                    # an implementation detail of the pipeline.
                    row["metrics"] = {**metrics, **derived.get(key, {})}
                    for column in row["metrics"]:
                        if column not in seen:
                            seen.add(column)
                            metric_columns.append(column)
                    break
                for key in _case_keys(row):
                    if key in dimensions:
                        row["dimensions"] = dimensions[key]
                        break
                if row.get("status", "").startswith("failed"):
                    log_file = (row.get("log_file") or "").strip()
                    if log_file:
                        reason = _failure_reason(Path(log_file).parent)
                        if reason:
                            row["failure_reason"] = reason

            if metric_columns:
                measured_runs += 1
            primary_metric = primary_metric or _primary_metric(run_dir)
            try:
                mtime = summary.stat().st_mtime
            except OSError:
                mtime = 0.0
            job = _job_name(
                run_dir.name,
                (r.get("dimensions", {}).get("device") or r.get("device") or "" for r in rows),
            )
            runs.append({
                "backend": name,
                "run": run_dir.name,
                # Which invocation produced this directory. Several run
                # directories -- one per device -- share one job.
                "job": job,
                "job_started_at": _job_started_at(job),
                "dir": str(run_dir),
                "updated_at": mtime,
                "report": _find_report(run_dir),
                "rows": rows,
                "metric_columns": metric_columns,
                "ok": sum(1 for r in rows if r.get("status") == "ok"),
                "failed": sum(1 for r in rows if r.get("status", "").startswith("failed")),
            })

    runs.sort(key=lambda r: r["updated_at"], reverse=True)
    return {
        "runs": runs,
        "count": sum(len(r["rows"]) for r in runs),
        # What each metric key means, for every key any run produced. The UI used
        # to carry its own table of these and could only guess at a KPI it had
        # not been taught; the pipeline's schema is not fixed, so the answer
        # travels with the data instead.
        "metrics": _sorted_metric_meta({c for r in runs for c in r["metric_columns"]}),
        # Which metric decides "the best run of this test" -- see _primary_metric.
        # None when no pivot report has been produced yet; the UI then falls back
        # to the most recent repetition, which needs no metric at all.
        "primary_metric": primary_metric,
        "benchmarks_dir": str(root),
        # False means every run on disk predates metrics collection, or its
        # aggregation step never ran -- the UI says so rather than leaving the
        # user to wonder why the columns are empty.
        "metrics_available": measured_runs > 0,
    }


def _precision_of(quant: str) -> str:
    """Weight format from a summary row's quant, when the aggregator did not say.

    "int4_ov" and "int4_cw_ov" are the same precision exported two ways; the
    trailing source token is what distinguishes the repository, not the number
    of bits the comparison groups by.
    """
    parts = [p for p in quant.split("_") if p]
    return parts[0] if parts else quant


def matrix(backend: Optional[str] = None) -> dict:
    """Every case on disk as one flat table, for comparing across runs.

    `collect` groups by run directory, which is the right shape for "what did
    this run do" and the wrong one for "which device is fastest": a run
    directory holds exactly one device, so every comparison the user asked for
    crosses several of them. Same rows, one level flatter, plus the axes and the
    metric descriptors the UI needs to plot them.

    Built on top of `collect` rather than beside it so the table and the charts
    cannot disagree about what ran.
    """
    data = collect(backend)

    rows: List[dict] = []
    metric_keys: List[str] = []
    seen_metrics = set()
    for run in data["runs"]:
        for source in run["rows"]:
            dims = source.get("dimensions") or {}
            quant = source.get("quant") or ""
            log_file = (source.get("log_file") or "").strip()
            metrics = source.get("metrics") or {}
            for key in metrics:
                if key not in seen_metrics:
                    seen_metrics.add(key)
                    metric_keys.append(key)
            rows.append({
                "backend": run["backend"],
                "run": run["run"],
                # The invocation this case belongs to, and when it started. One
                # job spans every device it swept, which is what the Results tab
                # folds by; `run` remains the directory, i.e. one device of it.
                "job": run["job"],
                "job_started_at": run["job_started_at"],
                # When the run that produced this case last wrote its summary.
                # The run directory name is timestamped by runner.py and would
                # usually sort the same way, but it is a name, not a clock: a
                # tree assembled by hand has whatever names its author chose,
                # and "which repetition is the newest" has to hold there too.
                "updated_at": run["updated_at"],
                "model": source.get("model") or "",
                # Lowercase throughout, matching the aggregator: summary.tsv
                # spells the same device "NPU" and the medians CSV "npu", and a
                # chart that grouped by the raw string would draw two of them.
                "device": (dims.get("device") or source.get("device") or "").lower(),
                "precision": dims.get("precision") or _precision_of(quant),
                "quant": quant,
                "mode": dims.get("mode") or run["backend"],
                "batch_size": dims.get("batch_size") or "",
                "status": source.get("status") or "",
                "task": source.get("task") or "",
                "case_dir": str(Path(log_file).parent) if log_file else "",
                "log_file": log_file,
                "model_dir": source.get("model_dir") or "",
                "metrics": metrics,
                # Present only on failures, and the reason a failed case is
                # returned at all: dropping it would leave a chart showing CPU
                # and GPU with no hint that NPU was ever attempted.
                "failure_reason": source.get("failure_reason"),
            })

    def axis(field: str) -> List[str]:
        return sorted({r[field] for r in rows if r[field]})

    # Jobs are the one axis that is not sorted alphabetically: they are moments,
    # and the newest is the one a reader wants at the top. `data["runs"]` is
    # already ordered newest-first, so first-seen order is that order.
    jobs: List[str] = []
    for run in data["runs"]:
        if run["job"] and run["job"] not in jobs:
            jobs.append(run["job"])

    return {
        "rows": rows,
        "dimensions": {
            "models": axis("model"),
            "devices": axis("device"),
            "precisions": axis("precision"),
            "backends": axis("backend"),
            "runs": axis("run"),
            "jobs": jobs,
        },
        "metrics": _sorted_metric_meta(metric_keys),
        "primary_metric": data["primary_metric"],
        "benchmarks_dir": data["benchmarks_dir"],
        "metrics_available": data["metrics_available"],
    }


def read_case_log(log_path: str, max_bytes: int = 256 * 1024) -> Optional[str]:
    """Tail a per-case benchmark.log referenced by a summary row.

    The path comes from summary.tsv, which we wrote ourselves, but it arrives back
    through the API from the browser -- so it is confined to the benchmarks tree
    before being opened.
    """
    root = env.paths()["benchmarks"].resolve()
    try:
        resolved = Path(log_path).resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        logger.warning(f"Rejected benchmark log path outside {root}: {log_path}")
        return None
    # summary.tsv points at benchmark.log (just the [INFO] header + command). The
    # actual run output and KPI lines are in detail.log next to it, which is what a
    # user clicking a result row wants to read -- prefer it when present. It sits in
    # the same (already path-checked) case directory, so no re-validation is needed.
    if resolved.name == "benchmark.log":
        detail = resolved.with_name("detail.log")
        if detail.is_file():
            resolved = detail
    if not resolved.is_file():
        return None
    try:
        size = resolved.stat().st_size
        with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            return fh.read()
    except OSError as exc:
        logger.warning(f"Unreadable benchmark case log {resolved}: {exc}")
        return None


# Per-case sample timeline
#
# Every number elsewhere in this file is one median per case, taken over that
# case's measurement window. The samples those medians came from are still on
# disk -- sampler.py writes one run-level CSV at 2 Hz -- and a median cannot
# distinguish a case that ramped from 15 W to 40 W from one that sat at 28 W
# throughout. So the window is sliced back out of that CSV and returned as it
# was sampled.
#
# Nothing about the window is re-derived here. The pipeline's aggregation script
# is loaded and asked, so a curve and the median printed beside it cannot come
# from two different opinions about when the case was running.

_AGGREGATOR_PATH = env.SRC_ROOT / "scripts" / "analysis" / "export_windowed_metric_medians.py"

# How many points one curve is worth. A case runs for tens of seconds at 2 Hz,
# so this is only reached by a pathological run; striding down to it keeps one
# response from carrying an hour of samples that would land on a 700px chart.
_TIMELINE_MAX_POINTS = 1500

# A run directory as runner.py names it: "<timestamp>_<run_id>_<DEVICE>". The
# run id is what ties it back to the sampling CSV -- the same derivation
# run_template.sh does with sed when it re-aggregates an earlier run.
_RUN_ID_RE = re.compile(r"^\d{8}_\d{6}_([0-9a-f]+)_[A-Za-z]+$")

_aggregator = None


def _aggregation_module():
    """The pipeline's aggregation script, imported by path.

    It lives in benchmark/scripts/analysis/, which is not a package -- it is run
    as a script, under the benchmark venv's interpreter -- so it cannot simply be
    imported by name. It pulls in nothing outside the standard library, so
    loading it into this process is safe, and it is the only definition of a
    case's measurement window and of which CSV column becomes which metric.
    """
    global _aggregator
    if _aggregator is None:
        spec = importlib.util.spec_from_file_location(
            "bench_windowed_metric_medians", _AGGREGATOR_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {_AGGREGATOR_PATH}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _aggregator = module
    return _aggregator


def _run_metrics_csv(run_dir: Path) -> Optional[Path]:
    """The sampling CSV covering a run directory, if it is still on disk."""
    match = _RUN_ID_RE.match(run_dir.name)
    if not match:
        return None
    candidate = env.paths()["runs"] / f"run_{match.group(1)}_metrics.csv"
    return candidate if candidate.is_file() else None


def timeline(case_dir: str) -> Optional[dict]:
    """Hardware samples over one case's measurement window.

    None whenever the curve cannot be produced honestly: a path outside the
    benchmarks tree, a case with no detail.log to take a window from, a run
    whose sampling CSV has been cleaned up (or that predates in-process
    sampling), or a window that contains no samples at all. The caller reports
    that as "nothing recorded" rather than drawing an empty chart.

    Series are keyed by the *median* metric name -- cpu_usage_pct arrives as
    cpu_usage_percent_median -- so the browser can label an axis from the metric
    descriptors it already holds instead of learning the CSV's own schema.
    """
    root = env.paths()["benchmarks"].resolve()
    try:
        resolved = Path(case_dir).resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        logger.warning(f"Rejected benchmark case path outside {root}: {case_dir}")
        return None
    detail = resolved / "detail.log"
    if not detail.is_file():
        return None
    csv_path = _run_metrics_csv(resolved.parent)
    if csv_path is None:
        return None

    try:
        aggregator = _aggregation_module()
        start, end = aggregator.parse_detail_window(detail)
    except (ImportError, OSError, ValueError) as exc:
        logger.warning(f"No measurement window for {resolved}: {exc}")
        return None

    columns = dict(aggregator.METRICS_COLUMNS)
    times: List[float] = []
    series: Dict[str, List[Optional[float]]] = {target: [] for target in columns.values()}
    try:
        with open(csv_path, newline="", encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh):
                stamp = aggregator.safe_float(row.get(aggregator.METRICS_TIMESTAMP_COLUMN))
                if stamp is None or stamp < start or stamp > end:
                    continue
                # Seconds into the window, not epoch: the chart's x-axis is "how
                # far into the run", and a reader has no use for the wall clock.
                times.append(round(stamp - start, 3))
                for source, target in columns.items():
                    series[target].append(aggregator.safe_float(row.get(source)))
    except OSError as exc:
        logger.warning(f"Unreadable benchmark metrics CSV {csv_path}: {exc}")
        return None

    if not times:
        return None

    stride = -(-len(times) // _TIMELINE_MAX_POINTS)
    if stride > 1:
        times = times[::stride]
        series = {key: values[::stride] for key, values in series.items()}

    # A column nothing filled is an absent sensor, not a flat line at zero, so it
    # is dropped: the selector above the chart should offer only curves that
    # exist.
    series = {key: values for key, values in series.items()
              if any(value is not None for value in values)}

    return {
        "case_dir": str(resolved),
        "start": start,
        "end": end,
        "duration_s": round(end - start, 3),
        "count": len(times),
        "t": times,
        "series": series,
    }


# Deletion
#
# Nothing else in this service removes results: a run that is repeated adds a
# measurement, and which of them is "the" one is decided when they are read.
# This exists for the one case where that is not what the user wants -- they are
# re-running a configuration precisely because the previous attempt is not worth
# keeping (a misconfigured host, a thermal outlier) and would otherwise go on
# competing to be the best run of that test forever.


def _rewrite_summary(summary: Path, removed: Set[str]) -> int:
    """Drop the rows pointing into `removed` from a summary.tsv.

    Returns how many rows are left. The file is the only record that a case
    existed -- the medians CSV is joined *from* it -- so a case directory that
    was deleted without this would keep showing up as a row with no numbers.
    """
    try:
        with open(summary, newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            fieldnames = reader.fieldnames or []
            kept = [
                row for row in reader
                if str(Path((row.get("log_file") or "").strip()).parent) not in removed
            ]
    except OSError as exc:
        logger.warning(f"Unreadable benchmark summary {summary}: {exc}")
        return -1

    if not fieldnames:
        return len(kept)
    try:
        with open(summary, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter="\t")
            writer.writeheader()
            for row in kept:
                writer.writerow({key: row.get(key, "") for key in fieldnames})
    except OSError as exc:
        logger.warning(f"Could not rewrite benchmark summary {summary}: {exc}")
        return -1
    return len(kept)


def delete_cases(case_dirs: Sequence[str]) -> dict:
    """Remove benchmark cases and the summary rows that name them.

    Paths arrive from the browser, so each is resolved and confined to the
    benchmarks tree before anything is unlinked -- the same guard read_case_log
    applies, for a considerably less forgiving operation. Anything outside is
    skipped and reported rather than aborting the batch: a request for ten
    cases, one of which no longer exists, should still remove the nine.

    A run directory whose last case goes is removed with it. What is left
    otherwise is a summary.tsv with a header and nothing under it, which every
    reader here would report as a run that did nothing.
    """
    root = env.paths()["benchmarks"].resolve()
    removed: Dict[Path, Set[str]] = {}
    skipped: List[str] = []

    for raw in case_dirs or []:
        try:
            case_dir = Path(str(raw)).resolve()
            case_dir.relative_to(root)
        except (OSError, ValueError):
            logger.warning(f"Rejected benchmark case path outside {root}: {raw}")
            skipped.append(str(raw))
            continue
        if case_dir == root or case_dir.parent == root:
            # A backend directory, or the tree itself. Only a case directory --
            # two levels down, inside a run directory -- is deletable here.
            logger.warning(f"Refused to delete a non-case benchmark path: {case_dir}")
            skipped.append(str(raw))
            continue
        if case_dir.is_dir():
            try:
                shutil.rmtree(case_dir)
            except OSError as exc:
                logger.warning(f"Could not remove benchmark case {case_dir}: {exc}")
                skipped.append(str(raw))
                continue
        # Recorded even when the directory was already gone: the summary row
        # naming it is exactly what has to go too.
        removed.setdefault(case_dir.parent, set()).add(str(case_dir))

    runs_removed = 0
    for run_dir, cases in removed.items():
        summary = run_dir / "summary.tsv"
        if not summary.is_file():
            continue
        if _rewrite_summary(summary, cases) == 0:
            try:
                shutil.rmtree(run_dir)
                runs_removed += 1
            except OSError as exc:
                logger.warning(f"Could not remove empty run directory {run_dir}: {exc}")

    count = sum(len(cases) for cases in removed.values())
    logger.info(f"Deleted {count} benchmark case(s), {runs_removed} run directory(ies)")
    return {"removed": count, "runs_removed": runs_removed, "skipped": skipped}
