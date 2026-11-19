#!/usr/bin/env bash
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# Advanced-run helpers, sourced by the benchmark commons (genai, app).
#
# They wrap the final benchmark command run_case builds with two envelope
# features, each gated by an environment variable that benchmark/service/runner.py
# sets. Neither is ever set on an ordinary run, which then behaves exactly as it
# did before this file existed.
#
#   BENCH_PRINT_ONLY    -- plan phase. run_case appends the command it WOULD run
#                          to BENCH_CMD_MANIFEST and returns before it touches the
#                          results tree, so a plan measures nothing and leaves no
#                          RUN_DIR / summary.tsv / detail.log behind. This is what
#                          the "Advanced run" review reads to show the commands.
#
#   BENCH_CMD_OVERRIDES -- advanced run. A per-case file
#                          "${BENCH_CMD_OVERRIDES}/<case_key>.cmd" holds a command
#                          the operator edited in that review; when present it
#                          REPLACES the built command wholesale (run via `bash -c`,
#                          in the same cwd/venv run_case would have used). The
#                          command runs as the same unprivileged pipeline user as
#                          every other benchmark subprocess -- this is not an
#                          escalation, only a substitution the operator authored.
#
#   BENCH_CMD_OVERRIDES/<case_key>.skip
#                       -- the operator unticked this case in the review: run_case
#                          returns without running or recording it.
#
# case_key ties the two sides together and is spelled the same in both commons and
# in runner.py: "<safe_name>__<quant>__<DEVICE>", plus "__<variant_id>" when
# gen_wrapper.py expanded the model's parameter sweep (it always does now; a
# hand-run wrapper without BENCH_VARIANT keeps the short key). runner.py names
# the .cmd/.skip files by it and reads it back out of the manifest.
#
# Per-variant inputs, set by gen_wrapper.py as prefix assignments on each
# run_case line:
#
#   BENCH_VARIANT          the variant id -- also the case's subdirectory name
#   BENCH_VARIANT_META     JSON describing the variant (params, swept, errors)
#   BENCH_VARIANT_INVALID  set when validation failed; the case is recorded as
#                          "invalid" and skipped unless its command was overridden

# case_meta.py does the JSON; located through the pipeline's own variable, with a
# path relative to this file for a templates tree used outside the pipeline.
_BENCH_CASE_META_PY="${DIR_SCRIPTS_ROOT:-$(dirname "${BASH_SOURCE[0]}")/../scripts}/params/case_meta.py"

_bench_py() {
    command -v python3 || command -v python || echo python
}

# The case_key for "<safe_name> <quant> <device>", with the variant appended.
_bench_case_key() {
    local key="$1__$2__$3"
    [ -n "${BENCH_VARIANT:-}" ] && key+="__${BENCH_VARIANT}"
    printf '%s' "$key"
}

# Path of this case's override file, or non-zero when overrides are not in play.
_bench_override_file() {
    [ -n "${BENCH_CMD_OVERRIDES:-}" ] || return 1
    printf '%s/%s.cmd' "${BENCH_CMD_OVERRIDES}" "$1"
}

_bench_has_override() {
    local f
    f=$(_bench_override_file "$1") || return 1
    [ -f "$f" ]
}

_bench_override_body() {
    local f
    f=$(_bench_override_file "$1") || return 1
    cat "$f"
}

# The operator unticked this case in the plan review.
_bench_is_skipped() {
    [ -n "${BENCH_CMD_OVERRIDES:-}" ] && [ -f "${BENCH_CMD_OVERRIDES}/$1.skip" ]
}

# Shell-quote every argument and join with spaces, so the manifest carries a
# command that round-trips: pasted into a shell it reproduces this exact argv, and
# the operator edits from there.
_bench_quote_join() {
    local out="" tok
    for tok in "$@"; do
        out+="${out:+ }$(printf '%q' "$tok")"
    done
    printf '%s' "$out"
}

# Append one JSON record to the plan manifest: the case, its command, and the
# variant's parameters and validation result. No-op when no manifest was asked
# for.
_bench_emit_manifest() {
    [ -n "${BENCH_CMD_MANIFEST:-}" ] || return 0
    local case_key="$1" model="$2" quant="$3" task="$4" device="$5" model_dir="$6"
    shift 6
    "$(_bench_py)" "${_BENCH_CASE_META_PY}" manifest --out "${BENCH_CMD_MANIFEST}" \
        --meta "${BENCH_VARIANT_META:-}" --case-key "$case_key" --model "$model" \
        --quant "$quant" --task "$task" --device "$device" --model-dir "$model_dir" \
        --command "$(_bench_quote_join "$@")"
}

# Write <case_dir>/case.json: the variant's configuration plus this case's
# runtime facts. Called before the run (status "running", so a killed run still
# leaves a description) and again after it.
#   $1 case_dir  $2 status  $3 case_key  $4 model  $5 quant  $6 task  $7 tool
#   $8 model_dir  $9 overridden(0/1)  ${10} launch_ts  ${11} end_ts  rest: command
_bench_write_case_json() {
    local case_dir="$1" status="$2" case_key="$3" model="$4" quant="$5" task="$6" \
          tool="$7" model_dir="$8" overridden="$9" launch_ts="${10}" end_ts="${11}"
    shift 11
    "$(_bench_py)" "${_BENCH_CASE_META_PY}" case --out "${case_dir}/case.json" \
        --meta "${BENCH_VARIANT_META:-}" --case-key "$case_key" --model "$model" \
        --quant "$quant" --task "$task" --tool "$tool" --device "${DEVICE}" \
        --model-dir "$model_dir" --status "$status" --overridden "$overridden" \
        --launch-ts "$launch_ts" --end-ts "$end_ts" \
        --command "$(_bench_quote_join "$@")" \
        || echo "[WARN] could not write ${case_dir}/case.json" >&2
}

# The variant's validation errors, one per line (empty when it has none).
_bench_variant_errors() {
    [ -n "${BENCH_VARIANT_META:-}" ] || return 0
    "$(_bench_py)" "${_BENCH_CASE_META_PY}" errors --meta "${BENCH_VARIANT_META}"
}
