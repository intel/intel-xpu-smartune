#!/bin/bash
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# systemd ExecStart for intel-smartune.service (monitor-only package). Runs as root (needed for the
# full telemetry: msr modprobe, cgroup/fdinfo reads, cpupower, ...). Ensures the
# self-signed TLS certificate exists, then launches the monitor.
#
# The venv is built at install time by postinst (ensure_venv.sh), NOT here — this
# script only runs it. If the venv is missing the install is broken, so we fail
# fast with a clear message instead of silently trying to rebuild at launch.

set -e

INSTALL_DIR="/opt/intel/smartune"
cd "$INSTALL_DIR"

mkdir -p "$INSTALL_DIR/logs"

# 1. The venv must already exist (built by postinst). Don't self-heal here.
if [ ! -x "$INSTALL_DIR/smartune-venv/bin/python" ]; then
    echo "[smartune] ERROR: venv missing at $INSTALL_DIR/smartune-venv — reinstall the package." >&2
    exit 1
fi

# 2. Self-signed cert the Flask server needs (monitor refuses to start without it).
KEY_DIR="$INSTALL_DIR/key"
CERT_FILE="$KEY_DIR/b_server.crt"
KEY_FILE="$KEY_DIR/b_server.key"
mkdir -p "$KEY_DIR"
if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    echo "[smartune] Generating self-signed certificate..."
    openssl req -x509 -newkey rsa:4096 \
        -keyout "$KEY_FILE" -out "$CERT_FILE" \
        -days 365 -nodes -subj "/CN=localhost" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
    chmod 644 "$CERT_FILE"
    chmod 600 "$KEY_FILE"
fi

# 2b. API access token — provisioned HERE, before the service import, so the
# authhelper (which pkexec-reads this file immediately after `systemctl start`)
# never races the server's own startup provisioning. The monitor imports fast,
# but doing this identically to the full package (run_all.sh) removes any
# dependency on startup timing and keeps both variants byte-for-byte consistent.
# An existing token is reused so it stays stable across restarts (an open
# dashboard tab keeps working); a new one is generated only when the file is
# absent or empty. The service reads this same file when it provisions its
# in-memory token hash.
TOKEN_FILE="$KEY_DIR/api_token"
if [ ! -s "$TOKEN_FILE" ]; then
    ( umask 077; openssl rand -hex 32 > "$TOKEN_FILE" )
fi
chmod 600 "$TOKEN_FILE"

# 3. Started on demand from the desktop icon, the service runs until the user
# stops it: this flag adds a "Stop service" action to the dashboard (see
# utils/ui_shutdown.py). `systemctl stop intel-smartune` works as well.
export SMARTUNE_UI_SHUTDOWN=1

# 3a. Listen address, from /etc/default/intel-smartune (the unit's
# EnvironmentFile): every interface unless the admin set 127.0.0.1, so other
# machines can open the dashboard by this host's IP. The server itself
# defaults to 127.0.0.1 when run from a source tree.
export MONITOR_HOST="${SMARTUNE_LISTEN:-0.0.0.0}"
echo "[smartune] Dashboard will listen on $MONITOR_HOST:${MONITOR_PORT:-9001} (SMARTUNE_LISTEN in /etc/default/intel-smartune)."

# 3b. Refuse to start if the port is already taken (typically a SmarTune started
# from a source tree). Exit 3 is listed in the unit's RestartPreventExitStatus, so
# systemd gives up at once instead of restart-looping on "Address already in use".
PORT="${MONITOR_PORT:-9001}"
if command -v ss >/dev/null 2>&1 && [ -n "$(ss -Hltn "sport = :$PORT" 2>/dev/null)" ]; then
    echo "[smartune] ERROR: port $PORT is already in use by another program:" >&2
    ss -Hltnp "sport = :$PORT" >&2 || true
    echo "[smartune] Stop that program, then launch SmarTune again." >&2
    exit 3
fi

# 4. Launch the monitor (port 9001, serves both the dashboard UI and its /api).
exec "$INSTALL_DIR/smartune-venv/bin/python" "$INSTALL_DIR/smartune.py" -m
