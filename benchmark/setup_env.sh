#!/usr/bin/env bash
# Setup environment for model_agentic framework
# This script initializes the runtime environment using global_vars.sh
#
# The Python environments are built by uv_build_envs.sh under
# ${DIR_ENV_ROOT}/.gen/multi_env. Setup only builds the huggingface-only base
# venv (all that listing/downloading models needs); the OpenVINO x transformers
# columns are built one version at a time, on demand, when a benchmark asks for
# one (run_template.sh). This script clones the openvino.genai checkout the OV
# build reads its requirements from, kicks off the base build, and stages the
# sample assets / version report around it.

set -euo pipefail

# Detect script directory and source global variables
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR_AGENTIC_ROOT="$(cd "${SCRIPT_DIR}" && pwd)"

# Source global variables
GLOBAL_VARS="${DIR_AGENTIC_ROOT}/configs/global_vars.sh"
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
# `bootstrap`). OpenVINO columns are NOT built here: each is built on demand at
# benchmark time for the version chosen on the Models tab, hardlink-deduped
# under ${PYENV_MULTI_DIR} and sharing uv's cache with this base venv.
echo "Building base huggingface environment with uv..."
bash "${DIR_AGENTIC_ROOT}/uv_build_envs.sh" build
echo ""

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
