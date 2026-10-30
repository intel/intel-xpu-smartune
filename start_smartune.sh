#!/bin/bash

# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Top-level entry point for the SmarTune product.
#
#   -a  (default)  start balancer + monitor together (single process, port 9001)
#   -m             start the monitor only (standalone, port 9001)

set -e

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
cd "$SCRIPT_DIR"

usage() {
    cat <<EOF
Usage: $(basename "$0") [-a | -m | -h]

  -a   Start balancer + monitor together (single process, port 9001). Default.
  -m   Start the monitor only (standalone telemetry, port 9001).
  -h   Show this help and exit.
EOF
}

MODE="all"
while getopts "amh" opt; do
    case "$opt" in
        a) MODE="all" ;;
        m) MODE="monitor" ;;
        h) usage; exit 0 ;;
        *) usage >&2; exit 1 ;;
    esac
done

KEY_DIR="$SCRIPT_DIR/key"
CERT_FILE="$KEY_DIR/b_server.crt"
KEY_FILE="$KEY_DIR/b_server.key"

# The directory must not be writable by a non-root user: otherwise a key can
# be replaced after its ownership and mode have been checked.
sudo install -d -o root -g root -m 755 "$KEY_DIR"
if [ -L "$CERT_FILE" ] || [ -L "$KEY_FILE" ]; then
    echo "Refusing symlinked TLS certificate or private key." >&2
    exit 1
fi
if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
    echo "Certificate and key already exist. Skipping generation."
else
    echo "Generating certificate..."
    sudo openssl req -x509 -newkey rsa:4096 \
        -keyout "$KEY_FILE" -out "$CERT_FILE" \
        -days 365 -nodes -subj "/CN=localhost" \
        -addext "subjectAltName=IP:127.0.0.1"

    if [ $? -ne 0 ]; then
        echo "Certificate generation failed" >&2
        exit 1
    fi
    echo "Certificate generated successfully"
fi

# Ensure existing and newly generated TLS files have the expected ownership
# and permissions before starting the service.
sudo chown root:root "$CERT_FILE" "$KEY_FILE"
sudo chmod 644 "$CERT_FILE"
sudo chmod 600 "$KEY_FILE"

# OS packages needed to build the venv; only this step needs sudo.
if ! python3 -c "import venv, ensurepip" >/dev/null 2>&1; then
    echo "[smartune] Installing python3-venv..."
    if ! sudo apt-get install -y python3-venv; then
        echo "SmarTune was NOT started: could not install python3-venv. Run manually: sudo apt install -y python3-venv" >&2
        exit 1
    fi
fi

# Python environment: setup_venv.sh creates .smartune-venv and installs
# requirements.txt on first run (and when requirements.txt changes). It runs
# as the current user, so the venv stays owned by the checkout owner.
VENV_DIR="$SCRIPT_DIR/.smartune-venv"
if ! bash "$SCRIPT_DIR/setup_venv.sh"; then
    echo "SmarTune was NOT started: Python environment setup failed (see errors above)." >&2
    exit 1
fi

PYTHON_BIN="$VENV_DIR/bin/python"

# Refuse to start a second instance. If SmarTune is already running, leave the
# existing service untouched and exit *before* installing the cleanup trap, so
# this aborted start never signals the process that is already serving.
if pgrep -f "$SCRIPT_DIR/smartune.py" >/dev/null 2>&1; then
    echo "SmarTune is already running; refusing to start another instance." >&2
    echo "Stop the running service first (it keeps serving on port 9001)." >&2
    exit 1
fi

CHILD_PID=""
cleanup() {
    echo "Clean up..."
    if [ -n "$CHILD_PID" ]; then
        # Only terminate the service this script started (and its children),
        # never other SmarTune instances.
        sudo pkill -P "$CHILD_PID" 2>/dev/null || true
        sudo kill "$CHILD_PID" 2>/dev/null || true
    fi
    wait 2>/dev/null
    stty sane 2>/dev/null || true
    echo "Service stopped."
}
trap cleanup INT TERM EXIT

if [ "$MODE" = "monitor" ]; then
    echo "Starting monitor only..."
    sudo -E "$PYTHON_BIN" "$SCRIPT_DIR/smartune.py" -m &
else
    echo "Starting balancer + monitor..."
    sudo -E "$PYTHON_BIN" "$SCRIPT_DIR/smartune.py" -a &
fi
CHILD_PID=$!
wait "$CHILD_PID"
