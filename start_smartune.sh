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

mkdir -p "$KEY_DIR"
if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
    echo "Certificate and key already exist. Skipping generation."
else
    echo "Generating certificate..."
    openssl req -x509 -newkey rsa:4096 \
        -keyout "$KEY_FILE" -out "$CERT_FILE" \
        -days 365 -nodes -subj "/CN=localhost" \
        -addext "subjectAltName=IP:127.0.0.1"

    if [ $? -eq 0 ]; then
        echo "Certificate generated successfully"
        chmod 644 "$CERT_FILE"
        chmod 600 "$KEY_FILE"
    else
        echo "Certificate generation failed" >&2
        exit 1
    fi
fi

PYTHON_BIN="$(command -v python3)"

cleanup() {
    echo "Clean up..."
    sudo pkill -f "smartune.py" 2>/dev/null
    wait
    stty sane 2>/dev/null || true
    echo "Service stopped."
}
trap cleanup INT TERM EXIT

if [ "$MODE" = "monitor" ]; then
    echo "Starting monitor only..."
    sudo -E "$PYTHON_BIN" "$SCRIPT_DIR/smartune.py" -m
else
    echo "Starting balancer + monitor..."
    sudo -E "$PYTHON_BIN" "$SCRIPT_DIR/smartune.py" -a
fi
