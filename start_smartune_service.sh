#!/bin/bash

# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Systemd entry point: called by smartune.service, already running as root.
# No sudo usage or trap installation needed; lifecycle is managed by systemd.

set -e
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"

KEY_DIR="$SCRIPT_DIR/key"
CERT_FILE="$KEY_DIR/b_server.crt"
KEY_FILE="$KEY_DIR/b_server.key"

mkdir -p "$KEY_DIR" "$SCRIPT_DIR/logs"
if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
    echo "Certificate and key already exist. Skipping generation."
else
    echo "Generating certificate..."
    openssl req -x509 -newkey rsa:4096 \
        -keyout "$KEY_FILE" -out "$CERT_FILE" \
        -days 365 -nodes -subj "/CN=localhost" \
        -addext "subjectAltName=IP:127.0.0.1"
    chmod 644 "$CERT_FILE"
    chmod 600 "$KEY_FILE"
    echo "Certificate generated successfully"
fi

# Build/refresh the .smartune-venv virtual environment (no-op when up to date).
# On failure, say so clearly; systemd then retries per Restart=on-failure.
if ! bash "$SCRIPT_DIR/setup_venv.sh"; then
    echo "[smartune] ERROR: Python environment setup failed; service not started." >&2
    exit 1
fi

PYTHON_BIN="$SCRIPT_DIR/.smartune-venv/bin/python"
exec "$PYTHON_BIN" "$SCRIPT_DIR/smartune.py"
