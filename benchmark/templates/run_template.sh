#!/usr/bin/env bash
# run_template.sh - parameterized copy of run.sh.
# benchmark/service/runner.py substitutes two placeholder tokens: the models JSON block
# (inside the heredoc below) and the pipeline stage (the opt= line below).
#set -euo pipefail

# 0) 载入全局变量
# BENCH_SRC_ROOT lets the caller render this template into a script that lives
# OUTSIDE the benchmark source tree (benchmark/service/runner.py writes to the runtime dir so
# the vendored tree stays read-only). Falls back to the script's own directory,
# which is the upstream behaviour.
SCRIPT_DIR="${BENCH_SRC_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
source "$SCRIPT_DIR/configs/global_vars.sh"

opt="__OPT__"  # build + benchmark 阶段选择

# 0.5) Environment on demand.
#
# Only the huggingface base venv is built up front (setup): it is all that
# listing models and the build/download stage (`hf download`) need. OpenVINO is
# built one version at a time, when a benchmark asks for it -- so ensure the base
# venv exists, and for a benchmark ensure + switch to the chosen OV column. The
# switch is per-process (this script only): the service-side `venv` symlink stays
# on the base venv, so model listing keeps working while a benchmark runs.
bash "$SCRIPT_DIR/uv_build_envs.sh" bootstrap || exit 1
if [ "$opt" == "benchmark" ] || [ "$opt" == "all" ]; then
    if [ -z "${BENCH_OV_VERSION:-}" ]; then
        echo "ERROR: BENCH_OV_VERSION is not set for a benchmark run" >&2
        exit 1
    fi
    bash "$SCRIPT_DIR/uv_build_envs.sh" ensure "$BENCH_OV_VERSION" || exit 1
    OV_SUB="$PYENV_MULTI_DIR/ov_pool/ov_${BENCH_OV_VERSION}"
    export PYENV_VENV_DIR="$OV_SUB/venv"
    # python_env_selector.py reads PYENV_VERSION_DIR to pick a transformers venv.
    export PYENV_VERSION_DIR="$OV_SUB/python_version"
    source "$PYENV_VENV_DIR/bin/activate"
    echo "=== active OpenVINO for this run: ${BENCH_OV_VERSION} ($PYENV_VENV_DIR)"
fi

# HF_TOKEN is supplied by the caller's environment (benchmark/service/env.py). Never hardcode
# a credential here -- this file is rendered into a world-readable run script.
export HF_TOKEN="${HF_TOKEN:-}"


echo "==== environment variables ===="
# The token itself is never printed: this log is world-readable under the runtime
# tree AND is streamed to the browser over SSE (benchmark/service/events.py), so
# echoing it here would undo the care benchmark/service/env.py takes to keep the
# credential out of every file on disk. Whether one is set is all anyone debugging
# a 401 actually needs.
# Spelled out rather than as ${HF_TOKEN:+set}${HF_TOKEN:-unset}: the second
# expansion is ":-", which substitutes only when the variable is EMPTY -- so a
# token that was set printed itself, verbatim, right here.
if [ -n "${HF_TOKEN:-}" ]; then
  echo "=== HF_TOKEN is set"
else
  echo "=== HF_TOKEN is unset"
fi
echo "=== HF_ENDPOINT is set to: ${HF_ENDPOINT}"
echo "=== http_proxy is set to: ${http_proxy}"
echo "=== https_proxy is set to: ${https_proxy}"
echo "=== no_proxy is set to: ${no_proxy}"

# 1) 由页面生成的模型 JSON -> 写入 models_input.json
mkdir -p "$DIR_LOGS"

cat > "$JSON_MODELS_INPUT" <<'EOF'
__MODELS_INPUT_JSON__
EOF

if [ "$opt" == "build" ] || [ "$opt" == "all" ]; then
# =============== Build (Pass 1 only, optimum-cli) ===============
python "$SCRIPT_ROUTE" \
  --models-json "$JSON_MODELS_INPUT" \
  --skip-pass3 \
  --skip-pass2 \
  --output "$DIR_LOGS/convert_routing.json"

  python "$SCRIPT_GEN_WRAPPER" \
  --routes-json "$DIR_LOGS/convert_routing.json" \
  --strategy download \
  --stage build \
  --output-metadata "$DIR_LOGS/pass1_scripts.json"

python "$SCRIPT_EXECUTE_WRAPPER" \
  --scripts-metadata "$DIR_LOGS/pass1_scripts.json" \
  --timeout "$TIMEOUT_CONVERT" \
  --output "$DIR_LOGS/pass1_results.json"
fi

if [ "$opt" == "benchmark" ] || [ "$opt" == "all" ]; then
# =============== BENCHMARK (GenAI llm_bench) ===============
python "$SCRIPT_ROUTE" \
  --models-json "$JSON_MODELS_INPUT" \
  --models-dir "$DIR_IR" \
  --output "$DIR_LOGS/benchmark_routing.json"

python "$SCRIPT_GEN_WRAPPER" \
  --routes-json "$DIR_LOGS/benchmark_routing.json" \
  --strategy all \
  --stage benchmark \
  --output-metadata "$DIR_LOGS/benchmark_genai_scripts.json"

python "$SCRIPT_EXECUTE_WRAPPER" \
  --scripts-metadata "$DIR_LOGS/benchmark_genai_scripts.json" \
  --timeout "$TIMEOUT_BENCHMARK" \
  --output "$DIR_LOGS/benchmark_genai_results.json"
fi

# =============== 数据整理 ===============
# BENCH_METRICS_CSV is the run-level sampling CSV (benchmark/service/env.py exports it while
# benchmark/service/sampler.py writes it). It is absent for a build-only run, or when the
# sampler could not start; the aggregation still runs and just emits KPIs.
_metrics_args=()
if [ -n "${BENCH_METRICS_CSV:-}" ]; then
  _metrics_args=(--metrics-csv "$BENCH_METRICS_CSV")
fi

# Every backend a benchmark wrapper can write into, matching the RESULTS_ROOT of
# templates/benchmark_{genai,app}_common.sh and benchmark/service/results.py's
# BACKENDS. `app` used to be missing here, so benchmark_app cases -- which the
# router picks for any single-xml/bin IR -- produced a summary.tsv the dashboard
# could show but never the medians CSV it joins the numbers from.
#
# A mode with no cases leaves its results directory empty, and the aggregator
# exits non-zero on "no detail.log found". That is not a run failure.
for _bench_mode in genai app; do
  _backend_dir="$DIR_BENCHMARKS/$_bench_mode"
  [ -d "$_backend_dir" ] || continue

  # This run's directories: the wrappers name them "<BENCH_RUN_NAME>_<DEVICE>".
  _run_dirs=()
  if [ -n "${BENCH_RUN_NAME:-}" ]; then
    for _run_dir in "$_backend_dir/${BENCH_RUN_NAME}"_*; do
      [ -d "$_run_dir" ] && _run_dirs+=("$_run_dir")
    done
  fi

  if [ ${#_run_dirs[@]} -eq 0 ]; then
    # No run name (this template invoked by hand), or this backend saw no cases
    # this time. Fall back to the whole tree, which is the upstream behaviour and
    # the only thing that can work when the runs are not separable.
    python "$SCRIPT_METRICS_MEDIAN" "$_backend_dir/" \
      --mode "$_bench_mode" "${_metrics_args[@]}" \
      || echo "[WARN] metrics aggregation for $_bench_mode skipped (no usable detail.log)."
    continue
  fi

  # Existing rows first, so this run's numbers win for the cases it measured and
  # every other case keeps what it already had.
  _merge_inputs=()
  [ -f "$_backend_dir/windowed_metric_medians.csv" ] \
    && _merge_inputs+=("$_backend_dir/windowed_metric_medians.csv")

  # Runs measured before the split above: their hardware columns were blanked by
  # a later run's whole-tree pass, and the backend-wide file is all they have.
  # The samples that DO cover them are still on disk -- benchmark/service/runner.py
  # keeps one CSV per run under DIR_RUNS, named after the run id that the
  # directory itself carries ("<timestamp>_<run_id>_<DEVICE>") -- so the numbers
  # are recomputed once, into the run's own file, and never disturbed again.
  for _run_dir in "$_backend_dir"/*/; do
    _run_dir="${_run_dir%/}"
    [ -f "$_run_dir/windowed_metric_medians.csv" ] && continue
    case "$(basename "$_run_dir")" in "${BENCH_RUN_NAME}"_*) continue ;; esac
    _run_id=$(basename "$_run_dir" \
      | sed -n 's/^[0-9]\{8\}_[0-9]\{6\}_\([0-9a-f]\{1,\}\)_[A-Za-z]\{1,\}$/\1/p')
    [ -n "$_run_id" ] || continue
    _past_csv="$DIR_RUNS/run_${_run_id}_metrics.csv"
    [ -f "$_past_csv" ] || continue
    echo "[INFO] restoring hardware medians for $(basename "$_run_dir") from $_past_csv"
    if python "$SCRIPT_METRICS_MEDIAN" "$_run_dir/" \
         --mode "$_bench_mode" --metrics-csv "$_past_csv"; then
      _merge_inputs+=("$_run_dir/windowed_metric_medians.csv")
    else
      echo "[WARN] could not re-aggregate $(basename "$_run_dir"); leaving it as it is."
    fi
  done

  for _run_dir in "${_run_dirs[@]}"; do
    if python "$SCRIPT_METRICS_MEDIAN" "$_run_dir/" \
         --mode "$_bench_mode" "${_metrics_args[@]}"; then
      _merge_inputs+=("$_run_dir/windowed_metric_medians.csv")
    else
      echo "[WARN] metrics aggregation for $(basename "$_run_dir") skipped (no usable detail.log)."
    fi
  done

  if [ ${#_merge_inputs[@]} -gt 0 ]; then
    python "$SCRIPT_MERGE_MEDIANS" "${_merge_inputs[@]}" \
      --output "$_backend_dir/windowed_metric_medians.csv" \
      || echo "[WARN] merging metrics for $_bench_mode failed; the per-run files are still readable."
  fi
done

# The pivot report is built from windowed_metric_medians.csv, which the step
# above produces. Without it there is nothing to pivot, so a failure here must
# not fail the run -- summary.tsv (written per case) is what the dashboard reads
# for results either way.
for _bench_mode in genai app; do
  if [ -d "$DIR_BENCHMARKS/$_bench_mode" ]; then
    python "$SCRIPT_GENERATE_REPORT" "$DIR_BENCHMARKS/$_bench_mode/" \
      || echo "[WARN] pivot report for $_bench_mode skipped (no metrics data)."
  fi
done
