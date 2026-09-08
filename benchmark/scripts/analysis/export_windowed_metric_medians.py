#!/usr/bin/env python3
"""Turn each benchmark case's detail.log into one row of KPIs plus the hardware
medians measured while that case was running.

Three steps per case:

  1. Scrape the backend's KPIs out of detail.log. Each backend prints a different
     shape (llm_bench's `[Average] P[n] ...`, benchmark_app's `Throughput: ...
     FPS`), so there is one parser each.
  2. Find the measurement window -- preferably the `PIPELINE TIME: ... begin:/end:`
     markers, otherwise the per-iteration `start:/end:` timestamps with the warm-up
     iteration dropped. This is what keeps model load and graph compilation out of
     the power and utilisation figures.
  3. Slice the run's metrics.csv to that window and take a median per column.
     Median, not mean, so one scheduling hiccup does not move the number.

The output feeds pivot_report.py / analyze.py, which pivot on the dimension
columns (model_name / device / precision / batch_size) that strip_precision_suffix
recovers from the case directory name.

SmarTune fork: upstream read five per-case CSVs written by metrics/metrics_collect.sh
(PTAT, its own gpu_monitor, intel-npu-smi, a memory monitor, and IGT's xe-perf).
Sampling now happens in-process (benchmark/service/sampler.py) and lands in ONE run-level CSV
passed as --metrics-csv, so the five blocks collapsed into one. The PTAT columns
(PL1/PL2, per-core IPC, TJMAX) and the xe-perf EU counters went with them: both
tools need an out-of-tree install -- an Intel-internal artifactory download and an
IGT build from an innersource clone -- so neither is shippable. Re-syncing this
file from upstream is a manual merge, not a copy.
"""

import argparse
import csv
import statistics
from datetime import datetime
from pathlib import Path
import re


DETAIL_TIME_PATTERN = re.compile(
    r"^\[ INFO \] \[(?P<iteration>[^\]]+)\]\[P(?P<prompt>\d+)\] start: "
    r"(?P<start>[^,]+), end: (?P<end>.+)$"
)

PIPELINE_TIME_PATTERN = re.compile(
    r"PIPELINE TIME:.*?begin:\s*(?P<begin>-?\d+(?:\.\d+)?),\s*end:\s*(?P<end>-?\d+(?:\.\d+)?)"
)

# Trailing config tags on a case_name — precision, device, and batch-size
# markers (e.g. "CellViT-256-x40_cpu_fp16_bs1" -> "CellViT-256-x40").
# Stripping them yields the bare model_name shared across configurations.
CONFIG_SUFFIX_PATTERN = re.compile(
    r'[_\-](?:'
    # precision
    r'fp32|fp16|fp64|int8|int4|uint8|uint4|float32|float16|float64|'
    r'bf16|bfloat16|f32|f16|i8|i4|'
    # device
    r'igpu|dgpu|gpu|cpu|npu|'
    # framework / format marker
    r'ov|'
    # batch size
    r'(?:bs|batch)[_\-]?\d+'
    r')$',
    re.IGNORECASE,
)
# Backward-compatible alias.
PRECISION_SUFFIX_PATTERN = CONFIG_SUFFIX_PATTERN


def strip_precision_suffix(case_name: str) -> tuple[str, str | None]:
    """Strip trailing config tags and infer the model source in one pass.

    Tags may appear stacked in any order (e.g. "model_cpu_fp16_bs1"); they are
    stripped repeatedly until none remain, yielding the bare model_name. While
    walking, an 'ov' tag marks the model as an OpenVINO IR model ('ov').

    Returns (model_name, model_source); model_source defaults to 'optimum'.
    """
    model_source = 'optimum'
    prev = None
    while prev != case_name:
        prev = case_name
        match = CONFIG_SUFFIX_PATTERN.search(case_name)
        if not match:
            break
        token = match.group(0).lstrip('_-').lower()
        if token == 'ov':
            model_source = 'ov'
        case_name = case_name[:match.start()]
    return case_name, model_source

BENCHMARK_APP_PATTERNS = {
    'kpi_count_iterations': re.compile(r"\[ INFO \] Count:\s+(\d+)\s+iterations"),
    'kpi_duration_ms': re.compile(r"\[ INFO \] Duration:\s+([\d.]+)\s+ms"),
    'kpi_latency_median_ms': re.compile(r"\[ INFO \]\s+Median:\s+([\d.]+)\s+ms"),
    'kpi_latency_average_ms': re.compile(r"\[ INFO \]\s+Average:\s+([\d.]+)\s+ms"),
    'kpi_latency_min_ms': re.compile(r"\[ INFO \]\s+Min:\s+([\d.]+)\s+ms"),
    'kpi_latency_max_ms': re.compile(r"\[ INFO \]\s+Max:\s+([\d.]+)\s+ms"),
    'kpi_throughput_fps': re.compile(r"\[ INFO \] Throughput:\s+([\d.]+)\s+FPS"),
}

# GenAI Average line pattern - captures all metrics in one line.
# MULTILINE matters: without it `$` only matches at the end of the file, so the
# [Average] line had to be the very last line of detail.log. run_with_metrics
# appends a PIPELINE TIME line after it when the backend printed none, which
# would otherwise silently turn every genai case into "no KPIs found".
GENAI_AVERAGE_PATTERN = re.compile(
    r"\[ INFO \] \[Average\] P\[\d+\](?P<metrics>.+)$",
    re.MULTILINE,
)

# Columns of the run-level metrics CSV (benchmark/service/sampler.py writes them; keep the two
# in step) mapped to the report column each one becomes. The `_median` suffix is
# what it says: every value here is the median over the case's measurement window.
#
# cpu_package_power_w is the package RAPL domain -- cores plus the integrated GPU
# -- so it is the right figure for a perf/watt column on any device. gpu_power_w
# is the graphics domain alone.
METRICS_COLUMNS = {
    # CPU
    'cpu_usage_pct': 'cpu_usage_percent_median',
    'cpu_p_core_usage_pct': 'cpu_p_core_usage_percent_median',
    'cpu_e_core_usage_pct': 'cpu_e_core_usage_percent_median',
    'cpu_p_core_freq_mhz': 'cpu_p_core_frequency_mhz_median',
    'cpu_e_core_freq_mhz': 'cpu_e_core_frequency_mhz_median',
    'cpu_package_power_w': 'cpu_package_power_w_median',
    'cpu_package_temp_c': 'cpu_package_temp_c_median',
    'cpu_package_tjmax_c': 'cpu_package_tjmax_c_median',
    # Memory
    'memory_used_gb': 'memory_used_gb_median',
    'memory_available_gb': 'memory_available_gb_median',
    'memory_used_pct': 'memory_used_percent_median',
    'memory_bandwidth_gb_s': 'memory_bandwidth_gb_s_median',
    'memory_bandwidth_pct': 'memory_bandwidth_percent_median',
    # GPU
    'gpu_power_w': 'gpu_power_w_median',
    'gpu_freq_mhz': 'gpu_frequency_mhz_median',
    'gpu_render_busy_pct': 'gpu_render_busy_percent_median',
    'gpu_compute_busy_pct': 'gpu_compute_busy_percent_median',
    'gpu_video_busy_pct': 'gpu_video_busy_percent_median',
    # NPU
    'npu_utilization_pct': 'npu_utilization_percent_median',
    'npu_power_w': 'npu_power_w_median',
    'npu_frequency_mhz': 'npu_frequency_mhz_median',
    'npu_bandwidth_mib_s': 'npu_bandwidth_mib_s_median',
    'npu_temperature_c': 'npu_temperature_c_median',
}

# Epoch seconds, matching the begin/end markers in detail.log.
METRICS_TIMESTAMP_COLUMN = 'timestamp_s'


def safe_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == '' or text.lower() in {'na', 'nan', 'invalid', 'none'}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_benchmark_app_kpis(detail_log: Path) -> dict[str, str]:
    content = detail_log.read_text(encoding='utf-8')
    kpis = {}
    for key, pattern in BENCHMARK_APP_PATTERNS.items():
        match = pattern.search(content)
        if match:
            kpis[key] = match.group(1)
    return kpis


def parse_benchmark_app_duration_s(detail_log: Path) -> float:
    content = detail_log.read_text(encoding='utf-8')
    matches = list(BENCHMARK_APP_PATTERNS['kpi_duration_ms'].finditer(content))
    if not matches:
        raise ValueError(f'No benchmark_app duration found in {detail_log}')
    return float(matches[-1].group(1)) / 1000.0


def parse_notebook_kpis(detail_log: Path) -> dict[str, str]:
    """
    Parse notebook inference KPIs from PIPELINE TIME logs in detail.log.

    The notebook infer.py scripts output:
        PIPELINE TIME: Time taken to iteration X: Y.YYYYYYYYY sec, begin: AAAA.AAAA, end: BBBB.BBBB

    This function extracts:
    - iteration_count: number of timed iterations (excluding warmup)
    - iteration_avg_sec: average iteration duration
    - iteration_min_sec: minimum iteration duration
    - iteration_max_sec: maximum iteration duration
    - throughput_fps: 1 / avg_duration
    """
    content = detail_log.read_text(encoding='utf-8')
    kpis = {}

    # Pattern to match iteration PIPELINE TIME lines (excluding warmup and model loading)
    iteration_pattern = re.compile(
        r'PIPELINE TIME: Time taken to iteration.*: ([\d.]+) sec'
    )

    # Extract all iteration durations
    iteration_durations = []
    for match in iteration_pattern.finditer(content):
        duration = float(match.group(1))
        iteration_durations.append(duration)

    if not iteration_durations:
        return kpis

    # Calculate statistics
    kpis['kpi_iteration_count'] = str(len(iteration_durations))
    avg_duration = sum(iteration_durations) / len(iteration_durations)
    kpis['kpi_iteration_avg_sec'] = f'{avg_duration:.9f}'
    kpis['kpi_iteration_min_sec'] = f'{min(iteration_durations):.9f}'
    kpis['kpi_iteration_max_sec'] = f'{max(iteration_durations):.9f}'
    kpis['kpi_throughput_fps'] = f'{1.0 / avg_duration:.6f}'

    return kpis


def parse_genai_kpis(detail_log: Path) -> dict[str, str]:
    """
    Parse GenAI benchmark KPIs from the [Average] line in detail.log.

    Handles different task types with metric name normalization:
    - Text generation: 1st token latency, 2nd token latency, 2nd tokens throughput
    - Embedding: 1st iteration latency, 2nd iteration latency, 2nd iteration throughput

    All variants are mapped to unified KPI names:
    - first_latency_ms (from "1st token/iteration latency")
    - second_latency_ms (from "2nd token/iteration latency")
    - throughput_tokens_s (from "2nd tokens/iteration throughput")
    - input_token_size (from "Input token size")
    """
    content = detail_log.read_text(encoding='utf-8')
    kpis = {}

    # Find the [Average] line. A run with several prompts prints one per prompt
    # index; the last is the one that summarises the whole case.
    matches = list(GENAI_AVERAGE_PATTERN.finditer(content))
    if not matches:
        return kpis
    match = matches[-1]

    metrics_text = match.group('metrics')

    # Parse individual metrics with flexible patterns
    # 1st token/iteration latency: X.XX ms
    first_latency_match = re.search(
        r'1st (?:token|iteration) latency:\s*([\d.]+)\s*ms',
        metrics_text
    )
    if first_latency_match:
        kpis['kpi_first_latency_ms'] = first_latency_match.group(1)

    # 2nd token/iteration latency: X.XX ms/token
    second_latency_match = re.search(
        r'2nd (?:token|iteration) latency:\s*([\d.]+|NA)\s*(?:ms/token)?',
        metrics_text
    )
    if second_latency_match:
        value = second_latency_match.group(1)
        if value != 'NA':
            kpis['kpi_second_latency_ms'] = value

    # 2nd tokens/iteration throughput: X.XX tokens/s
    throughput_match = re.search(
        r'2nd (?:tokens|iteration) throughput:\s*([\d.]+|NA)\s*(?:tokens/s)?',
        metrics_text
    )
    if throughput_match:
        value = throughput_match.group(1)
        if value != 'NA':
            kpis['kpi_throughput_tokens_s'] = value

    # Input token size: XXX (optional)
    input_size_match = re.search(
        r'Input token size:\s*(\d+)',
        metrics_text
    )
    if input_size_match:
        kpis['kpi_input_token_size'] = input_size_match.group(1)

    return kpis


def parse_detail_window(detail_log: Path) -> tuple[float, float]:
    pipeline_start = None
    pipeline_end = None
    warmup_start = None
    last_end = None
    earliest_start = None

    for raw_line in detail_log.read_text(encoding='utf-8').splitlines():
        stripped_line = raw_line.strip()

        pipeline_match = PIPELINE_TIME_PATTERN.search(stripped_line)
        if pipeline_match:
            begin_ts = float(pipeline_match.group('begin'))
            end_ts = float(pipeline_match.group('end'))
            if pipeline_start is None or begin_ts < pipeline_start:
                pipeline_start = begin_ts
            if pipeline_end is None or end_ts > pipeline_end:
                pipeline_end = end_ts
            continue

        match = DETAIL_TIME_PATTERN.match(stripped_line)
        if not match:
            continue
        iteration = match.group('iteration')
        start_ts = datetime.fromisoformat(match.group('start')).timestamp()
        end_ts = datetime.fromisoformat(match.group('end')).timestamp()
        if earliest_start is None or start_ts < earliest_start:
            earliest_start = start_ts
        if last_end is None or end_ts > last_end:
            last_end = end_ts
        if iteration == 'warm-up' and warmup_start is None:
            warmup_start = start_ts

    # Per-iteration timestamps win over PIPELINE TIME. llm_bench prints the
    # former; the latter is the wall-clock fallback run_with_metrics appends,
    # which also spans model load and compile -- minutes of near-idle that would
    # drag every median down. PIPELINE TIME is the only window a backend that
    # prints no iteration lines (benchmark_app, the notebook wrappers) has.
    if warmup_start is None:
        warmup_start = earliest_start
    if warmup_start is not None and last_end is not None:
        return warmup_start, last_end

    if pipeline_start is not None and pipeline_end is not None:
        return pipeline_start, pipeline_end

    raise ValueError(f'No iteration timestamps found in {detail_log}')


def median_or_blank(values: list[float]) -> str:
    if not values:
        return ''
    return f'{statistics.median(values):.6f}'


def collect_csv_medians(
    csv_path: Path | None,
    start_ts: float,
    end_ts: float,
) -> tuple[dict[str, str], int]:
    """Median of every METRICS_COLUMNS entry over [start_ts, end_ts].

    A column with no sample in the window comes back as '' rather than being
    omitted, so every row of the report has the same shape whether or not the
    hardware it names was present.
    """
    medians = {target: '' for target in METRICS_COLUMNS.values()}
    if csv_path is None or not csv_path.is_file():
        return medians, 0

    buckets: dict[str, list[float]] = {target: [] for target in METRICS_COLUMNS.values()}
    row_count = 0

    with csv_path.open(encoding='utf-8', newline='') as handle:
        for row in csv.DictReader(handle):
            ts = safe_float(row.get(METRICS_TIMESTAMP_COLUMN))
            if ts is None or ts < start_ts or ts > end_ts:
                continue
            row_count += 1
            for source_name, target_name in METRICS_COLUMNS.items():
                value = safe_float(row.get(source_name))
                if value is not None:
                    buckets[target_name].append(value)

    medians.update({key: median_or_blank(values) for key, values in buckets.items()})
    return medians, row_count


def catogary(case_dir: Path) -> dict[str, str]:
    """
    Categorize the case directory based on its path components.

    Extracts keywords from the full case directory path and categorizes them into:
    - platform: PTL, NVL, ARL, WCL (case-insensitive)
    - device: cpu, gpu, npu (case-insensitive)
    - batch_size: bs1, bs8, batch1, batch8, etc.
    - precision: fp32, fp16, int8, int4, float32, float16, etc.

    Returns a dictionary with extracted category keys that can be merged
    into the summary dictionary.
    """
    # Get path components for analysis (case-insensitive)
    path_parts = [part.lower() for part in case_dir.parts]

    result = {}

    # Extract platform (PTL, NVL, ARL, WCL)
    platform_pattern = re.compile(r'(?:^|[^a-z0-9])(ptl|nvl|arl|wcl)(?=$|[^a-z0-9])', re.IGNORECASE)
    for part in path_parts:
        match = platform_pattern.search(part)
        if match:
            result['platform'] = match.group(1).upper()
            break

    # Extract device type (gpu, cpu, npu).
    # If several conflicting device keywords appear in the path, pick the one
    # that appears earliest: earliest path component first, then leftmost
    # position within that component.
    device_keyword_map = {
        'igpu': 'gpu',
        'dgpu': 'gpu',
        'gpu': 'gpu',
        'cpu': 'cpu',
        'npu': 'npu',
    }
    device_pattern = re.compile(r'(igpu|dgpu|gpu|cpu|npu)', re.IGNORECASE)
    for part in path_parts:
        matches = list(device_pattern.finditer(part))
        if matches:
            earliest = min(matches, key=lambda m: m.start())
            result['device'] = device_keyword_map[earliest.group(1).lower()]
            break

    # Extract batch size (bs1, bs8, batch1, batch8, etc.)
    batch_pattern = re.compile(r'(?:bs|batch)[_\-]?(\d+)', re.IGNORECASE)
    for part in path_parts:
        match = batch_pattern.search(part)
        if match:
            batch_num = match.group(1)
            result['batch_size'] = f'bs{batch_num}'
            break

    # Extract precision (fp32, fp16, int8, int4, float32, float16, etc.)
    # Use lookahead/lookbehind to match precision keywords with flexible boundaries
    precision_pattern = re.compile(
        r'(?:^|[_\-/\s])(fp32|fp16|fp64|int8|int4|uint8|uint4|float32|float16|float64|'
        r'bf16|bfloat16|f32|f16|i8|i4)(?:[_\-/\s]|$)',
        re.IGNORECASE
    )
    for part in path_parts:
        match = precision_pattern.search(part)
        if match:
            precision = match.group(1).lower()
            # Normalize precision names
            precision_map = {
                'float32': 'fp32',
                'float16': 'fp16',
                'float64': 'fp64',
                'bfloat16': 'bf16',
                'f32': 'fp32',
                'f16': 'fp16',
                'i8': 'int8',
                'i4': 'int4',
            }
            result['precision'] = precision_map.get(precision, precision)
            break

    # Keep the original case_dir and case_name for backward compatibility
    parent_dir_name = case_dir.parent.name if case_dir.parent != case_dir else ''
    result['case_name'] = case_dir.name
    # model_name = case_name minus the precision tag (shared across precisions);
    # model_source = provenance inferred from the trailing tag in the same pass:
    #   '_ov' -> 'ov' (OpenVINO IR), otherwise 'optimum'
    result['model_name'], result['model_source'] = strip_precision_suffix(case_dir.name)
    # Full case directory path so downstream tooling can link back to detail.log
    result['case_dir'] = str(case_dir)

    return result

def summarize_case_app(case_dir: Path, detail_log_name: str, metrics_csv: Path | None) -> dict[str, str]:
    detail_log = case_dir / detail_log_name
    if not detail_log.is_file():
        raise FileNotFoundError(f'{detail_log_name} not found in {case_dir}')

    kpis = parse_benchmark_app_kpis(detail_log)
    if not kpis:
        raise ValueError(f'No benchmark_app KPIs found in {detail_log}')
    # case_dir shows the parent directory name (second to last path component)
    summary = catogary(case_dir)
    summary.update(kpis)

    # benchmark_app prints no window of its own, so run_with_metrics appends a
    # wall-clock PIPELINE TIME that also covers the model load. Duration: is
    # benchmark_app's own count of the measured phase, so walking back from the
    # end of the run trims the load off the front.
    _, window_end = parse_detail_window(detail_log)
    window_start = window_end - parse_benchmark_app_duration_s(detail_log)

    medians, _ = collect_csv_medians(metrics_csv, window_start, window_end)
    summary.update(medians)
    return summary


def summarize_case(
    case_dir: Path,
    detail_log_name: str,
    metrics_csv: Path | None,
    mode: str = 'genai',
) -> dict[str, str]:
    detail_log = case_dir / detail_log_name
    if not detail_log.is_file():
        raise FileNotFoundError(f'{detail_log_name} not found in {case_dir}')

    window_start, window_end = parse_detail_window(detail_log)
    # case_dir shows the parent directory name (second to last path component)
    summary=catogary(case_dir)

    # Extract KPIs based on mode
    if mode == 'genai':
        kpis = parse_genai_kpis(detail_log)
    elif mode == 'notebook':
        kpis = parse_notebook_kpis(detail_log)
    else:
        kpis = {}
    if not kpis:
        raise ValueError(f'No {mode} KPIs found in {detail_log}')
    summary.update(kpis)

    medians, _ = collect_csv_medians(metrics_csv, window_start, window_end)
    summary.update(medians)
    return summary


def discover_case_dirs(input_path: Path, detail_log_name: str) -> list[tuple[Path, str]]:
    """
    Discover case directories containing detail logs.

    Returns:
        List of tuples: (case_dir, actual_detail_log_filename)
    """
    # Check if input is a file matching the pattern
    if input_path.is_file() and input_path.name.endswith(detail_log_name):
        return [(input_path.parent, input_path.name)]

    # Check if input directory has a matching file directly
    matching_files = list(input_path.glob(f'*{detail_log_name}'))
    if matching_files:
        return [(input_path, f.name) for f in matching_files]

    # Recursively search for matching files
    matching_paths = sorted(input_path.rglob(f'*{detail_log_name}'))
    if not matching_paths:
        raise FileNotFoundError(f'No *{detail_log_name} found under {input_path}')

    # Return unique (case_dir, filename) tuples
    result = []
    seen = set()
    for path in matching_paths:
        case_dir = path.parent
        filename = path.name
        key = (case_dir, filename)
        if key not in seen:
            result.append(key)
            seen.add(key)
    return result


def write_summary_csv(rows: list[dict[str, str]], output_path: Path) -> None:
    preferred = [
        'case_name', 'model_name', 'model_source', 'platform', 'device', 'batch_size', 'precision']
    fieldnames = []
    seen = set()
    for name in preferred:
        if name not in seen:
            fieldnames.append(name)
            seen.add(name)
    for row in rows:
        for key in row.keys():
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)

    # Group all kpi_* columns immediately after the preferred block, preserving
    # their discovery order. Otherwise kpi_ fields first introduced by a later
    # case (e.g. a text_generation run appearing after embedding runs, which
    # exposes ttft/decode kpis the earlier rows lacked) get appended after the
    # monitoring columns and drift to the far right of the table.
    def _column_group(name: str) -> int:
        if name in preferred:
            return 0
        if name.startswith('kpi_'):
            return 1
        if name.startswith('kpis_'):
            return 2
        return 3
    fieldnames.sort(key=_column_group)  # stable sort keeps within-group order

    # Always keep case_dir as the very last column.
    if 'case_dir' in fieldnames:
        fieldnames.remove('case_dir')
        fieldnames.append('case_dir')

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def add_mode_field (rows: list[dict[str, str]], mode: str) -> None:
    """Add a 'mode' field to each row in the list of dictionaries."""
    for row in rows:
        row['mode'] = mode

def main() -> int:
    parser = argparse.ArgumentParser(
        description='Use detail.log warm-up start and final iteration end to window monitoring CSVs and export median metrics.'
    )
    parser.add_argument('input_path', help='Case directory containing detail.log, or a results root containing many case directories')
    parser.add_argument('-o', '--output', help='Output CSV path. Defaults to <input_path>/windowed_metric_medians.csv')
    parser.add_argument('--detail-log-name', default='detail.log', help='Detail log filename to use when scanning case directories. Defaults to detail.log')
    parser.add_argument('--mode', choices=['app', 'genai', 'notebook'], default='genai',
                        help='Benchmark mode: app (benchmark_app), genai (GenAI pipeline), notebook (notebook-based). Defaults to genai')
    parser.add_argument('--metrics-csv',
                        help='Run-level hardware sampling CSV written by benchmark/service/sampler.py. Every case is '
                             'windowed out of this one file. Omit it to export KPIs only.')
    args = parser.parse_args()

    input_path = Path(args.input_path)
    if not input_path.exists():
        raise FileNotFoundError(f'Input path not found: {input_path}')

    metrics_csv = Path(args.metrics_csv) if args.metrics_csv else None
    if metrics_csv is not None and not metrics_csv.is_file():
        # Not fatal: the KPIs still come out, the medians columns just stay blank.
        print(f'Warning: metrics CSV not found at {metrics_csv}; exporting KPIs only')
        metrics_csv = None

    case_info = discover_case_dirs(input_path, args.detail_log_name)
    rows = []
    skipped_cases = []
    for case_dir, actual_log_name in case_info:
        try:
            if args.mode == 'app':
                rows.append(summarize_case_app(case_dir, actual_log_name, metrics_csv))
            else:
                rows.append(summarize_case(case_dir, actual_log_name, metrics_csv, mode=args.mode))
        except (ValueError, FileNotFoundError) as exc:
            skipped_cases.append((case_dir, str(exc)))
            # Still emit a minimal row (category info + case_dir, but no KPIs) so
            # downstream reports can flag the case as an Error and link to detail.log.
            error_row = catogary(case_dir)
            error_row['case_dir'] = str(case_dir)
            rows.append(error_row)

    add_mode_field(rows, args.mode)
    output_path = Path(args.output) if args.output else input_path / 'windowed_metric_medians.csv'
    write_summary_csv(rows, output_path)
    print(f'Wrote {len(rows)} rows to {output_path}')
    if skipped_cases:
        print(f'Skipped {len(skipped_cases)} case(s) without usable detail.log timestamps:')
        for case_dir, reason in skipped_cases:
            print(f'  - {case_dir}: {reason}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())