#!/bin/bash
# Global Variables for Model Agentic Framework
# Source this file in all scripts: source $(dirname $0)/../configs/global_vars.sh
#
# Naming convention (prefix = what the value points at):
#   DIR_*      directory paths
#   PYENV_*    Python virtual-environment tree
#   SCRIPT_*   executable script files (.py/.sh)
#   JSON_*     json file paths
#   TIMEOUT_*  timeout scalars (seconds)
# Library/contract vars keep their upstream names: HF_*, MODELSCOPE_CACHE,
# NETWORK_PROFILE, SMARTUNE_BENCH_ENV_ROOT.

# ============ Runtime Environment ============
# SmarTune points SMARTUNE_BENCH_ENV_ROOT at benchmark/runtime (see benchmark/service/env.py)
# so the multi-GB venv/models/IR tree does not land in /tmp, which is subject to
# tmpfiles cleanup. Unset falls back to the upstream default.
export DIR_ENV_ROOT=${SMARTUNE_BENCH_ENV_ROOT:-/tmp/skill_env}

# ============ Python Env ============
# Multi-version Python env tree, built by uv_build_envs.sh: the base venv, the
# OpenVINO x transformers pool, and uv's own cache/bin all live under one dir so
# the runtime root stays tidy (mirrors how the rest of .gen is organised).
export PYENV_MULTI_DIR=${DIR_ENV_ROOT}/.gen/multi_env
export PYENV_VENV_DIR=${PYENV_MULTI_DIR}/venv
export PYENV_VERSION_DIR=${PYENV_MULTI_DIR}/python_version

# Auto-activate if exists
if [ -f "${PYENV_VENV_DIR}/bin/activate" ]; then
    source "${PYENV_VENV_DIR}/bin/activate"
fi

# ============ Directories (runtime) ============
export DIR_MODELS=${DIR_ENV_ROOT}/models
export DIR_IR=${DIR_ENV_ROOT}/models
export DIR_QUANTIZED=${DIR_ENV_ROOT}/quantized
export DIR_NOTEBOOKS=${DIR_ENV_ROOT}/notebooks
export DIR_GENAI=${DIR_ENV_ROOT}/.gen/genai
export DIR_SCRIPTS=${DIR_ENV_ROOT}/scripts
export DIR_LOGS=${DIR_ENV_ROOT}/.gen/logs
export DIR_BENCHMARKS=${DIR_ENV_ROOT}/benchmarks
# Per-run artifacts written by the service, not by this pipeline: the run script,
# its log, and the hardware sampling CSV (benchmark/service/env.py's paths()["runs"]).
export DIR_RUNS=${DIR_ENV_ROOT}/.gen/runs
# Generated script subdirectory
export DIR_SCRIPTS_NOTEBOOK=${DIR_SCRIPTS}/notebook

# ============ Directories (source code) ============
# Auto-detect DIR_AGENTIC_ROOT based on this script's location
# global_vars.sh is in configs/, so DIR_AGENTIC_ROOT is the parent directory
export DIR_AGENTIC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export DIR_SCRIPTS_ROOT=${DIR_AGENTIC_ROOT}/scripts
export DIR_TEMPLATES_ROOT=${DIR_AGENTIC_ROOT}/templates

# ============ Scripts ============
# Routing script (unified router for convert/quantize/benchmark)
export SCRIPT_ROUTE=${DIR_SCRIPTS_ROOT}/routing/route.py
# Generator script (unified wrapper generator for all stages)
export SCRIPT_GEN_WRAPPER=${DIR_SCRIPTS_ROOT}/generators/gen_wrapper.py
# Executor script
export SCRIPT_EXECUTE_WRAPPER=${DIR_SCRIPTS_ROOT}/executors/execute_wrapper.py
# Report generator
export SCRIPT_GENERATE_REPORT=${DIR_SCRIPTS_ROOT}/analysis/pivot_report.py
# KPI scrape + windowed medians over the run-level CSV that benchmark/service/sampler.py
# writes. The path it reads comes in as BENCH_METRICS_CSV (see benchmark/service/env.py).
export SCRIPT_METRICS_MEDIAN=${DIR_SCRIPTS_ROOT}/analysis/export_windowed_metric_medians.py
# Folds the per-run medians written by the script above back into one
# backend-wide table for the pivot report, without recomputing older runs
# against a sampling CSV that does not cover them.
export SCRIPT_MERGE_MEDIANS=${DIR_SCRIPTS_ROOT}/analysis/merge_windowed_medians.py

# ============ JSON ============
export JSON_MODELS_INPUT=${DIR_LOGS}/models_input.json

# ============ Tool Configuration ============
export HF_HOME=${DIR_ENV_ROOT}/.gen/huggingface_cache
export MODELSCOPE_CACHE=${DIR_ENV_ROOT}/.gen/modelscope_cache

# ============ Network Configuration ============
# Usage: export NETWORK_PROFILE before sourcing this file, or pass it as $1:
#   NETWORK_PROFILE=corp_proxy source global_vars.sh
#
# Default is EMPTY -- no proxy is imposed. A proxy that is right for one site is
# wrong (and a silent connectivity failure) for every other, so the corporate
# endpoints now live in config.yaml (benchmark.http_proxy / https_proxy), not here.
#
# Note $1 is only consulted when it looks like a profile name: this file is
# `source`d, so a bare $1 would otherwise pick up the *caller's* first positional
# argument and silently select a profile nobody asked for.
NETWORK_PROFILE=${NETWORK_PROFILE:-}
if [ -z "${NETWORK_PROFILE}" ]; then
    case "${1:-}" in
        scheme1|scheme2|none) NETWORK_PROFILE="$1" ;;
    esac
fi

case "$NETWORK_PROFILE" in
    scheme1|scheme2)
        # Proxy endpoints come from the environment (benchmark/service/env.py exports them
        # from config.yaml). The profile only selects the HF endpoint + the
        # download tuning that goes with it.
        export HF_ENDPOINT=${HF_ENDPOINT:-https://huggingface.co}
        # Xet-backed storage stalls at 0 bytes behind some proxies; force classic
        # HTTP LFS (resumable).
        export HF_HUB_DISABLE_XET=1
        export HF_HUB_ENABLE_HF_TRANSFER=0
        export HF_HUB_DOWNLOAD_TIMEOUT=30
        ;;
    *)
        # No profile: leave http_proxy/https_proxy exactly as the caller set them
        # (usually unset) and use the official endpoint.
        export HF_ENDPOINT=${HF_ENDPOINT:-https://huggingface.co}
        ;;
esac

# ============ Timeouts (seconds) ============
export TIMEOUT_CONVERT=3600
export TIMEOUT_BENCHMARK=7200
