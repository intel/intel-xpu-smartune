#!/usr/bin/env bash
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0


set -uo pipefail

DEVICE="CPU"
RESULTS_ROOT="${DIR_BENCHMARKS}/app"
# One run name per batch, set by benchmark/service/runner.py so every model/device
# wrapper in a "Run" shares a directory; falls back to TEST for a standalone run.
RUN_NAME="${BENCH_RUN_NAME:-TEST}"

# Advanced-run helpers (plan manifest + per-case command override). Sourced by
# path relative to this file so it resolves wherever the templates tree lives.
. "$(dirname "${BASH_SOURCE[0]}")/bench_advanced_common.sh"

usage() {
    cat <<'EOF'
Usage: xxx_wrapper.sh [options]

Options:
  --device DEVICE         OpenVINO device passed to benchmark_app (default: CPU)
  --run-name NAME         Name for this run; defaults to "TEST"

Batch size, shape and every other benchmark_app parameter are rendered per case
by gen_wrapper.py from the model's configuration and passed to run_case.
  -h, --help              Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --device)
            DEVICE="$2"
            shift 2
            ;;
        --run-name)
            RUN_NAME="$2"
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
    # Process launch and exit, for case.json (see benchmark_genai_common.sh).
    CASE_LAUNCH_TS="${begin}"
    CASE_END_TS="${end}"

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
    # One directory per round of a multi-round run (run_template.sh exports
    # BENCH_ROUND), so every round's cases sit side by side under the same tree
    # and none overwrites another's.
    RUN_DIR="${RESULTS_ROOT}/${RUN_NAME}${BENCH_ROUND:+_r${BENCH_ROUND}}_${DEVICE}"
    mkdir -p "${RUN_DIR}"
    SUMMARY_FILE="${RUN_DIR}/summary.tsv"

    if [[ -f "${SUMMARY_FILE}" ]]; then
        echo "Summary file already exists at ${SUMMARY_FILE}" >&2
    else
        printf 'model\tquant\tstatus\tdevice\tmodel_dir\tlog_file\tvariant\n' > "${SUMMARY_FILE}"
    fi
}

run_case() {
    local model_dir="$1"
    local quant=$(basename "${model_dir}")
    local model_name=$(basename "$(dirname "${model_dir}")")
    local safe_name="${model_name}_${quant}"
    shift 1

    # Identity of this case, shared with the plan manifest and the override files
    # (bench_advanced_common.sh). DEVICE comes from the enclosing device loop,
    # BENCH_VARIANT from the prefix assignments on this line.
    local case_key
    case_key=$(_bench_case_key "${safe_name}" "${quant}" "${DEVICE}")

    model_file=$(ls "${model_dir}"/*.xml | head -n 1)

    # The command this case would run. Built first so the plan can print it and the
    # override can replace it, both before anything is written to disk. Everything
    # past the model directory is the variant's rendered parameters (batch size,
    # shape, hint, ...) followed by the model's free-form extra arguments.
    local -a cmd=(
        "benchmark_app"
        -d "${DEVICE}"
        -m "${model_file}"
    )
    cmd+=("$@")

    # Advanced run: replace the command with the operator's edited one. Bookkeeping
    # below keys off DEVICE and model_dir, not the command text.
    local overridden=0
    if _bench_has_override "${case_key}"; then
        cmd=(bash -c "$(_bench_override_body "${case_key}")")
        overridden=1
    fi

    # Plan phase: record the command and return before touching the results tree.
    if [ -n "${BENCH_PRINT_ONLY:-}" ]; then
        _bench_emit_manifest "${case_key}" "${safe_name}" "${quant}" "benchmark_app" \
            "${DEVICE}" "${model_dir}" "${cmd[@]}"
        return 0
    fi

    # Unticked in the review: not run, not recorded.
    if _bench_is_skipped "${case_key}"; then
        echo "===============skipped by operator: ${case_key}"
        return 0
    fi

    update_dir
    # One directory per model/precision, one subdirectory per variant; see
    # benchmark_genai_common.sh.
    local case_dir="${RUN_DIR}/${safe_name}"
    [ -n "${BENCH_VARIANT:-}" ] && case_dir+="/${BENCH_VARIANT}"
    mkdir -p "${case_dir}"

    local log_file="${case_dir}/benchmark.log"
    # summary.tsv points at detail.log (the actual run output + KPI/timing lines
    # written by run_with_metrics); benchmark.log holds only the [INFO] header.
    local detail_log="${case_dir}/detail.log"

    # Contradictory configuration: recorded, not run, unless overridden.
    if [ -n "${BENCH_VARIANT_INVALID:-}" ] && [ "${overridden}" = "0" ]; then
        {
            echo "[ERROR] invalid configuration, case not run:"
            _bench_variant_errors | sed 's/^/[ERROR]   /'
        } | tee "${detail_log}"
        _bench_write_case_json "${case_dir}" "invalid" "${case_key}" "${safe_name}" "${quant}" \
            "" "benchmark_app" "${model_dir}" "${overridden}" "" "" "${cmd[@]}"
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
            "${safe_name}" "${quant}" "invalid" "${DEVICE}" "${model_dir}" "${detail_log}" \
            "${BENCH_VARIANT:-}" >> "${SUMMARY_FILE}"
        return 0
    fi

    {
        echo "[INFO] Model: ${safe_name}"
        echo "[INFO] Quantized Model: ${quant}"
        echo "[INFO] Device: ${DEVICE}"
        echo "[INFO] Variant: ${BENCH_VARIANT:-}"
        echo "[INFO] Model Directory: ${model_dir}"
        echo "[INFO] Command: ${cmd[*]}"
    } | tee "${log_file}"

    _bench_write_case_json "${case_dir}" "running" "${case_key}" "${safe_name}" "${quant}" \
        "" "benchmark_app" "${model_dir}" "${overridden}" "$(date +%s.%N)" "" "${cmd[@]}"

    source ${PYENV_VENV_DIR}/bin/activate
    cd ${DIR_GENAI}/tools/llm_bench
    echo "==========Current directory: $(pwd)"
    local status
    CASE_LAUNCH_TS="" CASE_END_TS=""
    if run_with_metrics "${case_dir}" "${cmd[@]}"; then
        echo "===============run success"
        status="ok"
    else
        echo "===============run failed"
        status="failed"
    fi
    _bench_write_case_json "${case_dir}" "${status}" "${case_key}" "${safe_name}" "${quant}" \
        "" "benchmark_app" "${model_dir}" "${overridden}" "${CASE_LAUNCH_TS}" "${CASE_END_TS}" "${cmd[@]}"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "${safe_name}" "${quant}" "${status}" "${DEVICE}" "${model_dir}" "${detail_log}" \
        "${BENCH_VARIANT:-}" >> "${SUMMARY_FILE}"
}
