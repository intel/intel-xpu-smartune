#!/usr/bin/env bash
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0


BENCHMARK_PY="${DIR_GENAI}/tools/llm_bench/benchmark.py"
DEVICE="CPU"
NUM_ITERS=2
RESULTS_ROOT="${DIR_BENCHMARKS}/genai"
# One run name per batch, set by benchmark/service/runner.py so every model/device
# wrapper in a "Run" shares a directory; falls back to TEST for a standalone run.
RUN_NAME="${BENCH_RUN_NAME:-TEST}"
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
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ ! -f "${BENCHMARK_PY}" ]]; then
    echo "benchmark.py not found at ${BENCHMARK_PY}" >&2
    exit 1
fi

# Run "$@" for one case, capturing everything it prints into <case_dir>/detail.log.
#
# detail.log is the only input the aggregation step has: it carries both the
# backend's KPI lines and the [begin, end] window used to slice the run's
# metrics.csv (see scripts/analysis/export_windowed_metric_medians.py). Upstream
# produced it from metrics/metrics_collect.sh, which also launched five sudo
# background samplers and tore them down with a pattern-matched `kill -9`.
# SmarTune samples in-process instead (benchmark/service/sampler.py), so what is left here is
# the tee and the window marker -- no sudo, no background processes.
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

    # llm_bench prints its own PIPELINE TIME line, which brackets just the
    # measured iterations. Only fall back to wall-clock -- which also covers
    # model load and compile, and so understates power and utilisation -- when
    # the command printed none.
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
        printf 'model\tquant\tstatus\ttask\tdevice\tmodel_dir\tlog_file\n' > "${SUMMARY_FILE}"
fi
}

run_case() {
    local safe_name="$1"
    local quant="$2"
    local task="$3"
    local model_dir="$4"
    shift 4

    # Identity of this case, shared with the plan manifest and the override files
    # (bench_advanced_common.sh). DEVICE comes from the enclosing device loop
    # gen_wrapper.py emits, so it is set by the time run_case is called.
    local case_key="${safe_name}__${quant}__${DEVICE}"

    # The command this case would run. Built first so the plan can print it and the
    # override can replace it, both before anything is written to disk.
    local -a cmd=(
        "${PYTHON_BIN}" "${BENCHMARK_PY}"
        --device "${DEVICE}"
        -n "${NUM_ITERS}"
        -bs "${BATCH_SIZE}"
        -t "${task}"
        -m "${model_dir}"
    )
    cmd+=("$@")

    # Advanced run: the operator edited this case in the plan review. Replace the
    # command wholesale with what they submitted, run through `bash -c` so any
    # shell they typed is honoured. --device and -m are theirs to change too; the
    # bookkeeping below keys off DEVICE and model_dir, not the command text, so a
    # changed command never desyncs the results directory.
    if _bench_has_override "${case_key}"; then
        cmd=(bash -c "$(_bench_override_body "${case_key}")")
    fi

    # Plan phase: record the command and return before update_dir. Nothing under
    # the results tree is created -- no RUN_DIR, summary.tsv, case_dir or
    # detail.log -- so a plan leaves the disk exactly as it found it.
    if [ -n "${BENCH_PRINT_ONLY:-}" ]; then
        _bench_emit_manifest "${case_key}" "${safe_name}" "${quant}" "${task}" \
            "${DEVICE}" "${model_dir}" "${cmd[@]}"
        return 0
    fi

    update_dir
    local case_dir="${RUN_DIR}/${safe_name}_${quant}"
    mkdir -p "${case_dir}"

    local log_file="${case_dir}/benchmark.log"
    # summary.tsv points at detail.log (the actual run output + KPI/timing lines
    # written by run_with_metrics); benchmark.log holds only the [INFO] header.
    local detail_log="${case_dir}/detail.log"

    {
        echo "[INFO] Model: ${safe_name}"
        echo "[INFO] Quantized Model: ${quant}"
        echo "[INFO] Task: ${task}"
        echo "[INFO] Device: ${DEVICE}"
        echo "[INFO] Model Directory: ${model_dir}"
        echo "[INFO] Command: ${cmd[*]}"
    } | tee "${log_file}"

    source ${PYENV_VENV_DIR}/bin/activate
    cd ${DIR_GENAI}/tools/llm_bench
    echo "==========Current directory: $(pwd)"
    if run_with_metrics "${case_dir}" "${cmd[@]}"; then
        echo "===============run success"
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
            "${safe_name}" "${quant}" "ok" "${task}" "${DEVICE}" "${model_dir}" "${detail_log}" >> "${SUMMARY_FILE}"
    else
        echo "===============run failed"
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
            "${safe_name}" "${quant}" "failed" "${task}" "${DEVICE}" "${model_dir}" "${detail_log}" >> "${SUMMARY_FILE}"
    fi
}
