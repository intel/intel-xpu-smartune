#!/usr/bin/env bash
# Build multi-version benchmark environments with uv (co-resolve, hardlink-deduped).
#
# Each environment is a COMPLETE venv = base packages + one OpenVINO + one
# transformers, resolved together. uv's global cache hardlinks identical wheels
# across venvs, so disk cost ~= one copy despite N*M environments.
#
# OpenVINO columns are built ON DEMAND (one per user-chosen version), not as a
# pre-resolved matrix. Up front only the huggingface-only base venv is built,
# because listing/downloading models needs the `hf` CLI, not OpenVINO.
#
# Layout ($PYENV_MULTI_DIR = $DIR_ENV_ROOT/.gen/multi_env):
#   uv_cache/                         UV cache (same FS as ov_pool -> hardlinks work)
#   uv_bin/uv                         pinned uv binary
#   uv_base_requirements.txt          shared base reqs (genai + project OV pins)
#   ov_pool/base/venv/                huggingface-only venv  (the `venv` default)
#   ov_pool/ov_<OV>/venv/             base + OV + DEFAULT_TF   (selector's default venv)
#   ov_pool/ov_<OV>/python_version/transformers_<TF>/   base + OV + TF (selector pool)
#   venv            -> ov_pool/base/venv                    (= $PYENV_VENV_DIR)
#
# Commands:
#   ./uv_build_envs.sh bootstrap      build the base huggingface venv (= `build`)
#   ./uv_build_envs.sh ensure <OV>    build one OV column on demand if missing
#   ./uv_build_envs.sh use <OV>       relink the active venv at an OV column
#
# A benchmark run points $PYENV_VENV_DIR / $PYENV_VERSION_DIR at its OV column
# for its own process only (run_template.sh), so the service-side `venv` symlink
# stays on the base venv and model listing keeps working during a run.
#
# This replaces multi_version.sh: uv venvs are complete, so the old base_venv.pth
# and hand-written optimum-cli wrappers are no longer needed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Capture our own args, then clear positional params: global_vars.sh reads $1
# (DIR_ENV_ROOT=${1:-...}, NETWORK_PROFILE=${1:-...}) from the caller's args.
CMD="${1:-build}"
ARG="${2:-}"
set --
source "${SCRIPT_DIR}/configs/global_vars.sh" >/dev/null 2>&1

# ---- Version matrix (editable) ----
# OV_VERSIONS is intentionally EMPTY: OpenVINO versions are no longer pre-built as
# a matrix. The version to build now comes from the user's per-run choice on the
# Models tab (BENCH_OV_VERSION), and `ensure <OV>` builds exactly that column on
# demand -- see run_template.sh. Only huggingface (the base venv) is built up
# front, because listing/downloading models needs the `hf` CLI, not OpenVINO.
OV_VERSIONS=()
TF_VERSIONS=("4.57.0" "5.0.0" "5.2.0" "5.5.0")
DEFAULT_TF="5.5.0"
# Fallback used only by `use <OV>` when no version is given; the pool no longer
# has a canonical "default" now that columns are built one at a time on demand.
DEFAULT_OV="${OV_VERSIONS[0]:-}"

# Base (huggingface-only) venv. Cheap to build, no OpenVINO, no genai checkout:
# just the download toolchain (hf CLI + modelscope) that search_models.py and the
# `opt=build` download stage need. The active `venv` symlink points here until a
# benchmark relinks to a specific OV column for its own process only.
DOWNLOAD_REQS="${SCRIPT_DIR}/requirements/download.requirements.txt"

# ---- Paths ----
# Everything the build owns lives under one dir (see global_vars.sh) so the
# runtime root stays tidy instead of scattering uv_cache/ov_pool/uv_bin at top.
MULTI_ENV="${PYENV_MULTI_DIR:-${DIR_ENV_ROOT}/.gen/multi_env}"
export UV_CACHE_DIR="${MULTI_ENV}/uv_cache"   # MUST share FS with OV_POOL for hardlink dedup
export UV_LINK_MODE="hardlink"
OV_POOL="${MULTI_ENV}/ov_pool"
BASE_VENV="${OV_POOL}/base/venv"          # huggingface-only venv (the `venv` link default)
UV_BIN="${MULTI_ENV}/uv_bin/uv"
BASE_REQS="${MULTI_ENV}/uv_base_requirements.txt"
GENAI_REQS="${DIR_GENAI}/tools/llm_bench/requirements.txt"
OV_REQS="${SCRIPT_DIR}/requirements/openvino.requirements.txt"   # project OV pins folded into base

ensure_uv() {
    if [[ -x "${UV_BIN}" ]]; then return; fi
    if command -v uv >/dev/null 2>&1; then UV_BIN="$(command -v uv)"; return; fi
    echo ">> Installing uv ..."
    mkdir -p "${MULTI_ENV}/uv_bin"
    curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR="${MULTI_ENV}/uv_bin" INSTALLER_NO_MODIFY_PATH=1 sh
}

# Build the shared base requirements once: genai llm_bench reqs PLUS the project's
# openvino.requirements.txt (git optimum-intel, safetensors/diffusers pins,
# modelscope, ...), each with the axes we pin per-combo (openvino trio +
# transformers) and the index URLs we handle ourselves (nightly OV index; torch
# cpu via --torch-backend) stripped out. Folding openvino.requirements.txt in here
# -- rather than a separate pip pass -- means every combo venv, including the
# python_version pool, matches the base that setup_env.sh used to build.
STRIP_AXES=(
    -e 'storage\.openvinotoolkit\.org'
    -e 'download\.pytorch\.org'
    -e '^[[:space:]]*openvino([-_](tokenizers|genai))?([[:space:]<>=!~]|$)'
    -e '^[[:space:]]*transformers\b'
)
gen_base_reqs() {
    if [[ ! -f "${GENAI_REQS}" ]]; then
        echo "ERROR: genai requirements not found: ${GENAI_REQS}" >&2
        echo "       run setup_env.sh first to clone openvino.genai." >&2
        exit 1
    fi
    grep -viE "${STRIP_AXES[@]}" "${GENAI_REQS}" > "${BASE_REQS}"
    if [[ -f "${OV_REQS}" ]]; then
        grep -viE "${STRIP_AXES[@]}" "${OV_REQS}" >> "${BASE_REQS}"
        echo ">> folded project OV requirements from ${OV_REQS}"
    fi
    echo ">> base requirements written: ${BASE_REQS}"
}

# install_combo <OV> <TF> <target_venv_dir>
install_combo() {
    local ov="$1" tf="$2" dir="$3"
    local trio_ver="${ov}.0"   # openvino-tokenizers / openvino-genai use <OV>.0
    echo ""
    echo "==== OV ${ov} + transformers ${tf}  ->  ${dir} ===="
    "${UV_BIN}" venv "${dir}" --python 3.12 || return 1
    "${UV_BIN}" pip install --python "${dir}/bin/python" --torch-backend cpu \
        -r "${BASE_REQS}" \
        "openvino==${ov}" \
        "openvino-tokenizers==${trio_ver}" \
        "openvino-genai==${trio_ver}" \
        "transformers[sentencepiece]==${tf}"
}

# Point $DIR_ENV_ROOT/<name> at <target>. If a pre-existing *real* dir/file is
# there (e.g. the pre-uv base venv), move it aside once to <name>.pre_uv.bak so
# the migration is reversible rather than destructive.
link_active() {
    local target="$1" link="$2"
    if [[ -e "${link}" && ! -L "${link}" ]]; then
        local bak="${link}.pre_uv.bak"
        if [[ -e "${bak}" ]]; then
            rm -rf "${link}"                # backup already exists; drop the stale copy
        else
            echo "   moving pre-uv ${link} -> ${bak}"
            mv "${link}" "${bak}"
        fi
    fi
    ln -sfnT "${target}" "${link}"
}

activate_ov() {
    local ov="$1"
    local sub="${OV_POOL}/ov_${ov}"
    if [[ ! -d "${sub}/venv" ]]; then
        echo "ERROR: OV ${ov} not built (missing ${sub}/venv)" >&2
        exit 1
    fi
    # The framework reads these two paths (global_vars.sh / service/env.py /
    # python_env_selector.py); point them at the active OV's built venvs.
    local venv_link="${PYENV_VENV_DIR:-${MULTI_ENV}/venv}"
    local pv_link="${PYENV_VERSION_DIR:-${MULTI_ENV}/python_version}"
    mkdir -p "$(dirname "${venv_link}")" "$(dirname "${pv_link}")"
    link_active "${sub}/venv"           "${venv_link}"
    link_active "${sub}/python_version" "${pv_link}"
    echo ">> active OV = ${ov}"
    echo "   ${venv_link} -> $(readlink -f "${venv_link}")"
    echo "   ${pv_link} -> $(readlink -f "${pv_link}")"
}

# Reject anything that is not a bare X.Y.Z: the value is interpolated into paths
# and pip specifiers, and a user-supplied version reaches here from the request.
validate_ov() {
    local ov="$1"
    if [[ ! "${ov}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "ERROR: invalid OpenVINO version '${ov}' (expected X.Y.Z)" >&2
        exit 2
    fi
}

# bootstrap: build the huggingface-only base venv and point the active `venv`
# symlink at it. This is all that listing models and downloading weights need, so
# it is what setup runs up front -- no OpenVINO, no genai checkout, seconds not
# hours. Idempotent: re-running only reinstalls if the venv is missing/broken.
bootstrap() {
    ensure_uv
    if [[ -x "${BASE_VENV}/bin/python" ]] && \
       "${BASE_VENV}/bin/python" -c "import huggingface_hub" >/dev/null 2>&1; then
        echo ">> base huggingface venv already present: ${BASE_VENV}"
    else
        echo "==== base huggingface venv  ->  ${BASE_VENV} ===="
        mkdir -p "$(dirname "${BASE_VENV}")"
        "${UV_BIN}" venv "${BASE_VENV}" --python 3.12 || return 1
        # jinja2 is needed by the build stage (gen_wrapper.py renders the run
        # scripts from templates); it runs in this base venv, so install it here
        # alongside the download toolchain rather than only in the OV columns.
        if [[ -f "${DOWNLOAD_REQS}" ]]; then
            "${UV_BIN}" pip install --python "${BASE_VENV}/bin/python" -r "${DOWNLOAD_REQS}" || return 1
        else
            "${UV_BIN}" pip install --python "${BASE_VENV}/bin/python" huggingface_hub modelscope jinja2 || return 1
        fi
    fi
    # The service (env.py / global_vars.sh) reads $PYENV_VENV_DIR for the `hf`
    # CLI; keep it pointing at the base venv. A benchmark relinks $PYENV_VENV_DIR
    # to its OV column inside its own process only (run_template.sh), never here.
    local venv_link="${PYENV_VENV_DIR:-${MULTI_ENV}/venv}"
    mkdir -p "$(dirname "${venv_link}")"
    link_active "${BASE_VENV}" "${venv_link}"
    echo ">> base venv -> $(readlink -f "${venv_link}")"
}

# install_ov_column <OV>: build the full transformers pool for one OpenVINO
# version -- ov_<OV>/venv (DEFAULT_TF) plus ov_<OV>/python_version/transformers_<tf>
# for the rest, so python_env_selector.py can pick a transformers per model.
install_ov_column() {
    local ov="$1"
    ensure_uv
    gen_base_reqs
    local pool="${OV_POOL}/ov_${ov}/python_version"
    mkdir -p "${pool}"
    local failures=()
    for tf in "${TF_VERSIONS[@]}"; do
        local dir
        if [[ "${tf}" == "${DEFAULT_TF}" ]]; then
            dir="${OV_POOL}/ov_${ov}/venv"
        else
            dir="${pool}/transformers_${tf}"
        fi
        if [[ -x "${dir}/bin/python" ]] && \
           "${dir}/bin/python" -c "import openvino,transformers" >/dev/null 2>&1; then
            echo "==== OV ${ov} + tf ${tf}: exists, skip ===="
            continue
        fi
        if ! install_combo "${ov}" "${tf}" "${dir}"; then
            echo "!! FAILED: OV ${ov} + tf ${tf}" >&2
            failures+=("ov${ov}+tf${tf}")
        fi
    done
    if [[ ${#failures[@]} -gt 0 ]]; then
        echo ">> OV ${ov}: completed with ${#failures[@]} failed combo(s): ${failures[*]}" >&2
        return 1
    fi
    echo ">> OV ${ov}: column built."
}

# ensure <OV>: build the column on demand if it is not already usable. Called at
# the start of every benchmark run (run_template.sh); a hit is a single import.
ensure_ov() {
    local ov="$1"
    validate_ov "${ov}"
    local venv="${OV_POOL}/ov_${ov}/venv"
    if [[ -x "${venv}/bin/python" ]] && \
       "${venv}/bin/python" -c "import openvino,transformers" >/dev/null 2>&1; then
        echo ">> OV ${ov}: already built, skipping."
        return 0
    fi
    echo ">> OV ${ov}: not built yet, building column on demand ..."
    install_ov_column "${ov}"
}

case "${CMD}" in
    build)     bootstrap ;;
    bootstrap) bootstrap ;;
    ensure)    ensure_ov "${ARG:?usage: $0 ensure <OV_VERSION>}" ;;
    use)       validate_ov "${ARG:?usage: $0 use <OV_VERSION>}"; activate_ov "${ARG}" ;;
    *)         echo "usage: $0 [build | bootstrap | ensure <OV_VERSION> | use <OV_VERSION>]" >&2; exit 2 ;;
esac
