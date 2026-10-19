#!/bin/bash
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Build the SmarTune .deb inside a clean, reproducible Ubuntu 26.04 container.
#
# Use this instead of build_deb.sh when your host is NOT Ubuntu 26.04, or when
# you want a reproducible build that doesn't depend on your host's installed
# packages. It builds the Ubuntu 26.04 builder image (Dockerfile.ubuntu2604),
# runs the build inside it, and writes the resulting .deb to ./build/.
#
# All arguments are forwarded verbatim to build_deb.sh (the image ENTRYPOINT),
# so the same options apply: [VERSION] and --full/--all (full package) etc.
# HTTP(S)/no_proxy env vars are detected and passed through automatically.
#
# Requires: Docker.

set -euo pipefail

usage() {
    cat <<EOF
Build the SmarTune .deb in a reproducible Ubuntu 26.04 Docker container.

Usage: $(basename "$0") [VERSION] [OPTIONS]

    (no option)   Build the monitor-only package (smartune-monitor).
    --full        Build the full package (smartune: balancer + monitor).
    VERSION       Override the package version (e.g. 1.5.1).
    -h, --help    Show this help and exit.

Examples:
    $(basename "$0")              # monitor-only .deb -> ./build/
    $(basename "$0") --full       # full .deb         -> ./build/

Output: ./build/smartune[-monitor]_<ver>_amd64.deb
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$HERE" rev-parse --show-toplevel)"
IMAGE="smartune-deb-builder:ubuntu-26.04"

BUILD_PROXY_ARGS=()
RUN_PROXY_ARGS=()
for proxy_name in http_proxy https_proxy all_proxy no_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY; do
    proxy_value="${!proxy_name:-}"
    [ -n "$proxy_value" ] || continue
    proxy_value="${proxy_value//127.0.0.1/host.docker.internal}"
    proxy_value="${proxy_value//localhost/host.docker.internal}"
    BUILD_PROXY_ARGS+=(--build-arg "$proxy_name=$proxy_value")
    RUN_PROXY_ARGS+=(-e "$proxy_name=$proxy_value")
done

echo "==> Building Ubuntu 26.04 builder image"
docker build \
    --add-host host.docker.internal:host-gateway \
    "${BUILD_PROXY_ARGS[@]}" \
    -t "$IMAGE" \
    -f "$HERE/Dockerfile.ubuntu2604" \
    "$REPO_ROOT"

mkdir -p "$REPO_ROOT/build"

echo "==> Building deb in Ubuntu 26.04"
docker run --rm \
    --user "$(id -u):$(id -g)" \
    --add-host host.docker.internal:host-gateway \
    "${RUN_PROXY_ARGS[@]}" \
    -e HOME=/tmp \
    -e npm_config_cache=/tmp/npm-cache \
    -v "$REPO_ROOT:/workspace:ro" \
    -v "$REPO_ROOT/build:/workspace/build" \
    --tmpfs "/workspace/dashboard/node_modules:exec,uid=$(id -u),gid=$(id -g)" \
    --tmpfs "/workspace/dashboard/dist:uid=$(id -u),gid=$(id -g)" \
    "$IMAGE" "$@"