#!/bin/bash

# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Create/refresh the SmarTune Python virtual environment (.smartune-venv) used by
# start_smartune.sh and start_smartune_service.sh (run from a source checkout).
#
# The venv is created with --system-site-packages so system-provided packages
# (bcc, OpenVINO bindings, ...) stay importable. Packages from requirements.txt
# are installed INTO the venv only and shadow the system copies there.

set -e

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
VENV_DIR="$SCRIPT_DIR/.smartune-venv"
REQ_FILE="$SCRIPT_DIR/requirements.txt"
STAMP="$VENV_DIR/.requirements.sha256"
CHECK_IMPORTS="import flask, peewee, psutil, yaml, requests, urllib3"

fail() {
    echo "[smartune] ERROR: $*" >&2
    exit 1
}

[ -f "$REQ_FILE" ] || fail "requirements.txt not found at $REQ_FILE."
command -v python3 >/dev/null 2>&1 || fail "python3 not found. Install it with: sudo apt install -y python3"
if [ -e "$VENV_DIR" ] && [ ! -w "$VENV_DIR" ]; then
    fail "$VENV_DIR is not writable by $(id -un) (probably created as root by an older version). Remove it with: sudo rm -rf $VENV_DIR"
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
    python3 -c "import venv, ensurepip" >/dev/null 2>&1 \
        || fail "python3-venv is not installed. Install it with: sudo apt install -y python3-venv"
    echo "[smartune] Creating virtual environment at $VENV_DIR ..."
    # Drop a half-created directory from an earlier failed attempt.
    rm -rf "$VENV_DIR"
    python3 -m venv --system-site-packages "$VENV_DIR" \
        || { rm -rf "$VENV_DIR"; fail "failed to create the virtual environment at $VENV_DIR."; }
fi

REQ_HASH="$(sha256sum "$REQ_FILE" | cut -d' ' -f1)"
if [ "$(cat "$STAMP" 2>/dev/null)" != "$REQ_HASH" ]; then
    echo "[smartune] Installing Python dependencies into $VENV_DIR ..."
    if ! "$VENV_DIR/bin/python" -m pip install -r "$REQ_FILE"; then
        rm -f "$STAMP"
        fail "pip install failed. Check network/proxy access to PyPI and any version conflict reported above, then retry. (Delete $VENV_DIR to rebuild from scratch.)"
    fi
    echo "$REQ_HASH" > "$STAMP"
fi

# Final sanity check, so a broken environment is reported here instead of as a
# bare ImportError from the service.
if ! CHECK_ERR="$("$VENV_DIR/bin/python" -c "$CHECK_IMPORTS" 2>&1)"; then
    echo "$CHECK_ERR" >&2
    rm -f "$STAMP"
    fail "the virtual environment is installed but required modules cannot be imported. Delete $VENV_DIR and retry."
fi
