#!/usr/bin/env bash
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
# case_key ties the two sides together and is spelled the same in both commons and
# in runner.py: "<safe_name>__<quant>__<DEVICE>". runner.py names the .cmd files by
# it and reads it back out of the manifest.

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

# Append one JSON record to the plan manifest. Every field goes in as an argv
# element (never interpolated into the program text), so no path or prompt can
# break the JSON or the shell -- python does the escaping. No-op when no manifest
# was asked for.
_bench_emit_manifest() {
    [ -n "${BENCH_CMD_MANIFEST:-}" ] || return 0
    local py
    py=$(command -v python3 || command -v python || echo python)
    local case_key="$1" model="$2" quant="$3" task="$4" device="$5" model_dir="$6"
    shift 6
    local cmd_str
    cmd_str=$(_bench_quote_join "$@")
    "$py" - "$BENCH_CMD_MANIFEST" "$case_key" "$model" "$quant" "$task" \
        "$device" "$model_dir" "$cmd_str" <<'PY'
import json, sys
manifest, case_key, model, quant, task, device, model_dir, command = sys.argv[1:9]
rec = {
    "case_key": case_key,
    "model": model,
    "quant": quant,
    "task": task,
    "device": device,
    "model_dir": model_dir,
    "command": command,
}
with open(manifest, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec) + "\n")
PY
}
