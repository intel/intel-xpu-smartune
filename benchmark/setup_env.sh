#!/usr/bin/env bash
# Setup environment for model_agentic framework
# This script initializes the runtime environment using global_vars.sh
#
# The Python environments are built by uv_build_envs.sh under
# ${DIR_ENV_ROOT}/.gen/multi_env. This script clones the openvino.genai checkout
# the OpenVINO build reads its requirements from, builds the huggingface-only
# base venv (all that listing/downloading models needs), and -- when
# BENCH_SETUP_OV names a version -- the complete OpenVINO x transformers column
# a benchmark runs inside. It then stages the sample assets / version report.
#
# BENCH_SETUP_OV comes from the dashboard (benchmark/service/env.py start_setup)
# and is what makes installing the environment install all of it, rather than
# leaving the column to the first benchmark that asks for one.

set -euo pipefail

# Detect script directory and source global variables
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR_AGENTIC_ROOT="$(cd "${SCRIPT_DIR}" && pwd)"

# Source global variables
GLOBAL_VARS="${DIR_AGENTIC_ROOT}/env/global_vars.sh"
if [[ ! -f "${GLOBAL_VARS}" ]]; then
    echo "Error: global_vars.sh not found at: ${GLOBAL_VARS}" >&2
    exit 1
fi

echo "Loading global variables from: ${GLOBAL_VARS}"
source "${GLOBAL_VARS}"

# Verify essential variables are loaded
if [[ -z "${DIR_ENV_ROOT:-}" ]]; then
    echo "Error: DIR_ENV_ROOT not set after sourcing global_vars.sh" >&2
    exit 1
fi

echo "Using DIR_ENV_ROOT: ${DIR_ENV_ROOT}"

OPENVINO_GENAI_REPO="https://github.com/openvinotoolkit/openvino.genai.git"

# Version report paths
VERSION_REPORT="${DIR_LOGS}/setup_versions.txt"
VERSION_REPORT_SCRIPT="${DIR_AGENTIC_ROOT}/report_tool_versions.py"

# Helper function: sync git repository
sync_repo() {
	local repo_url="$1"
	local repo_dir="$2"

	if [[ -d "${repo_dir}/.git" ]]; then
		echo "Updating existing repository: ${repo_dir}"
		git -C "${repo_dir}" pull --ff-only
		echo "✓ Repository updated"
	elif [[ -e "${repo_dir}" ]]; then
		echo "Error: Path exists but is not a git repo: ${repo_dir}" >&2
		exit 1
	else
		echo "Cloning repository: ${repo_url}"
		git clone --depth 1 -- "${repo_url}" "${repo_dir}"
		echo "✓ Repository cloned"
	fi
}

echo ""
echo "=================================================="
echo "Setting up model_agentic environment"
echo "=================================================="
echo ""

# Clone OpenVINO GenAI repository FIRST: uv_build_envs.sh derives the shared base
# requirements from its tools/llm_bench/requirements.txt, so the checkout must be
# present before the build runs.
echo "Setting up OpenVINO GenAI repository..."
sync_repo "${OPENVINO_GENAI_REPO}" "${DIR_GENAI}"
echo ""

# Build the huggingface-only base venv with uv (uv_build_envs.sh's `build` ==
# `bootstrap`). Seconds, and enough to list and download models.
echo "Building base huggingface environment with uv..."
bash "${DIR_AGENTIC_ROOT}/uv_build_envs.sh" build
echo ""

# The OpenVINO column for the requested version: OpenVINO + openvino-tokenizers
# + openvino-genai + every transformers python_env_selector.py can pick, all
# hardlink-deduped, so a second version only costs the wheels that differ.
#
# After the clone, necessarily: uv_build_envs.sh derives the shared base
# requirements from ${DIR_GENAI}/tools/llm_bench/requirements.txt.
if [[ -n "${BENCH_SETUP_OV:-}" ]]; then
    echo "Building the OpenVINO ${BENCH_SETUP_OV} environment (the long part)..."
    bash "${DIR_AGENTIC_ROOT}/uv_build_envs.sh" ensure "${BENCH_SETUP_OV}"
    echo ""
else
    echo "No OpenVINO version requested (BENCH_SETUP_OV unset); base environment only."
    echo ""
fi

# Stage sample assets used by the llm_bench harness.
cp "${DIR_AGENTIC_ROOT}/test_audio.wav" "${DIR_GENAI}/tools/llm_bench/"
cp "${DIR_AGENTIC_ROOT}/synthetic_448x448.jpg" "${DIR_GENAI}/tools/llm_bench/"

# Generate version report if script exists
if [[ -f "${VERSION_REPORT_SCRIPT}" ]]; then
    echo "Generating version report..."
    python3 "${VERSION_REPORT_SCRIPT}" \
        --venv "${PYENV_VENV_DIR}" \
        --notebooks-dir "${DIR_NOTEBOOKS}" \
        --output "${VERSION_REPORT}" 2>/dev/null || echo "⚠ Version report script not available"
    echo "✓ Version report generated: ${VERSION_REPORT}"
else
    echo "⚠ Version report script not found, skipping"
fi
echo ""

echo "=================================================="
echo "✓ Environment setup complete!"
echo "=================================================="
echo ""
echo "Environment paths:"
echo "  DIR_ENV_ROOT: ${DIR_ENV_ROOT}"
echo "  Virtual environment: ${PYENV_VENV_DIR}"
echo ""
