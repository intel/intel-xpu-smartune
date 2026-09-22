#!/usr/bin/env bash

set -uo pipefail

BENCHMARK_PY="${DIR_GENAI}/tools/llm_bench/benchmark.py"
DEVICE="CPU"
NUM_ITERS=2
RESULTS_ROOT="${DIR_BENCHMARKS}/app"
# One run name per batch, set by benchmark/service/runner.py so every model/device
# wrapper in a "Run" shares a directory; falls back to TEST for a standalone run.
RUN_NAME="${BENCH_RUN_NAME:-TEST}"
#RUN_NAME="TEST"
PYTHON_BIN="python"
BATCH_SIZE=1

# Advanced-run helpers (plan manifest + per-case command override). Sourced by
# path relative to this file so it resolves wherever the templates tree lives.
. "$(dirname "${BASH_SOURCE[0]}")/bench_advanced_common.sh"

usage() {
    cat <<'EOF'
Usage: xxx_wrapper.sh [options]

Options:
  --device DEVICE         OpenVINO device passed to benchmark.py (default: CPU)
  --num-iters N           Number of benchmark iterations (default: 2)
  --run-name NAME         Name for this run; defaults to "TEST"
  -h, --help              Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --device)
            DEVICE="$2"
            shift 2
            ;;
        --num-iters)
            NUM_ITERS="$2"
            shift 2
            ;;
        --run-name)
            RUN_NAME="$2"
            shift 2
            ;;
        --batch-size)
            BATCH_SIZE="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            break
            ;;
    esac
done

if [[ ! -f "${BENCHMARK_PY}" ]]; then
    echo "benchmark.py not found at ${BENCHMARK_PY}" >&2
    exit 1
fi

# Capture the case output into <case_dir>/detail.log and mark the measurement
# window. See benchmark_genai_common.sh for why this is inline rather than a call
# out to metrics/metrics_collect.sh.
run_with_metrics() {
    local case_dir="$1"
    shift
    mkdir -p "${case_dir}"
    local detail_log="${case_dir}/detail.log"

    local begin end status
    begin=$(date +%s.%N)
    echo "$@" > "${detail_log}"
    "$@" 2>&1 >> "${detail_log}"
    status=${PIPESTATUS[0]}
    end=$(date +%s.%N)

    # benchmark_app prints no window of its own, so this wall-clock fallback is
    # the normal path here; it includes the model load, which the medians for
    # this backend therefore average over.
    if ! grep -q 'PIPELINE TIME' "${detail_log}" 2>/dev/null; then
        printf 'PIPELINE TIME: Time taken to run case: %s sec, begin: %s, end: %s\n' \
            "$(awk -v b="${begin}" -v e="${end}" 'BEGIN{printf "%.9f", e-b}')" \
            "${begin}" "${end}" >> "${detail_log}"
    fi
    return ${status}
}

update_dir() {
    RUN_DIR="${RESULTS_ROOT}/${RUN_NAME}_${DEVICE}"
    mkdir -p "${RUN_DIR}"
    SUMMARY_FILE="${RUN_DIR}/summary.tsv"

    if [[ -f "${SUMMARY_FILE}" ]]; then
        echo "Summary file already exists at ${SUMMARY_FILE}" >&2
    else
        printf 'model\tquant\tstatus\tdevice\tmodel_dir\tlog_file\n' > "${SUMMARY_FILE}"
    fi
}

run_case() {
    local model_dir="$1"
    local quant=$(basename "${model_dir}")
    local model_name=$(basename "$(dirname "${model_dir}")")
    local safe_name="${model_name}_${quant}"
    shift 1

    # Identity of this case, shared with the plan manifest and the override files
    # (bench_advanced_common.sh). DEVICE comes from the enclosing device loop.
    local case_key="${safe_name}__${quant}__${DEVICE}"

    model_file=$(ls "${model_dir}"/*.xml | head -n 1)

    # The command this case would run. Built first so the plan can print it and the
    # override can replace it, both before anything is written to disk.
    local -a cmd=(
        "benchmark_app"
        -d "${DEVICE}"
        -m "${model_file}"
    )
    cmd+=("$@")

    # Advanced run: replace the command with the operator's edited one. Bookkeeping
    # below keys off DEVICE and model_dir, not the command text.
    if _bench_has_override "${case_key}"; then
        cmd=(bash -c "$(_bench_override_body "${case_key}")")
    fi

    # Plan phase: record the command and return before touching the results tree.
    if [ -n "${BENCH_PRINT_ONLY:-}" ]; then
        _bench_emit_manifest "${case_key}" "${safe_name}" "${quant}" "benchmark_app" \
            "${DEVICE}" "${model_dir}" "${cmd[@]}"
        return 0
    fi

    update_dir
    local case_dir="${RUN_DIR}/${safe_name}"
    mkdir -p "${case_dir}"

    local log_file="${case_dir}/benchmark.log"
    # summary.tsv points at detail.log (the actual run output + KPI/timing lines
    # written by run_with_metrics); benchmark.log holds only the [INFO] header.
    local detail_log="${case_dir}/detail.log"

    {
        echo "[INFO] Model: ${safe_name}"
        echo "[INFO] Quantized Model: ${quant}"
        echo "[INFO] Device: ${DEVICE}"
        echo "[INFO] Model Directory: ${model_dir}"
        echo "[INFO] Command: ${cmd[*]}"
    } | tee "${log_file}"

    source ${PYENV_VENV_DIR}/bin/activate
    cd ${DIR_GENAI}/tools/llm_bench
    echo "==========Current directory: $(pwd)"
    if run_with_metrics "${case_dir}" "${cmd[@]}"; then
        echo "===============run success"
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
            "${safe_name}" "${quant}" "ok" "${DEVICE}" "${model_dir}" "${detail_log}" >> "${SUMMARY_FILE}"
    else
        echo "===============run failed"
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
            "${safe_name}" "${quant}" "failed" "${DEVICE}" "${model_dir}" "${detail_log}" >> "${SUMMARY_FILE}"
    fi
}
