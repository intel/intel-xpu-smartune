#!/bin/bash
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# systemd ExecStart for the FULL smartune package (balancer + monitor). Runs as
# root — the balancer needs it for eBPF (bcc), cgroup writes, cpupower, tc, msr,
# etc., and the monitor for its full telemetry. Ensures the self-signed TLS
# certificate exists, then launches the combined service (smartune.py -a).
#
# The venv is built at install time by postinst (ensure_venv.sh) with
# --system-site-packages so the apt-installed bcc binding is importable; this
# script only runs it. If the venv is missing the install is broken, so we fail
# fast with a clear message instead of silently trying to rebuild at launch.

set -e

INSTALL_DIR="/opt/intel/smartune"
# Stay in INSTALL_DIR. smartune.py:run_all() chdirs into balancer/ itself so the
# CWD-relative controller/bpf_event.c resolves; everything else (key/, database,
# config.yaml, api_token) is resolved via __file__ and is CWD-independent.
cd "$INSTALL_DIR"

mkdir -p "$INSTALL_DIR/logs"

# 1. The venv must already exist (built by postinst). Don't self-heal here.
if [ ! -x "$INSTALL_DIR/smartune-venv/bin/python" ]; then
    echo "[smartune] ERROR: venv missing at $INSTALL_DIR/smartune-venv — reinstall the package." >&2
    exit 1
fi

# 2. Self-signed cert the Flask server needs (the service refuses to start
# without it). Same key/ dir and filenames balance_service.py resolves to.
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

# 2b. API access token — provisioned HERE, before the heavy service import, so
# the authhelper (which pkexec-reads this file immediately after `systemctl
# start`) never races the server's own startup provisioning. smartune.py -a
# imports the balancer (bcc/eBPF), which can take longer than the authhelper's
# wait window; writing the token up front means the desktop icon hands the
# browser a valid token on every launch. An existing token is reused so it stays
# stable across restarts (an open dashboard tab keeps working); a new one is
# generated only when the file is absent or empty. The service reads this same
# file, which the service reads when it provisions its in-memory token hash.
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
export BALANCER_HOST="${SMARTUNE_LISTEN:-0.0.0.0}"
echo "[smartune] Dashboard will listen on $BALANCER_HOST:${BALANCER_PORT:-9001} (SMARTUNE_LISTEN in /etc/default/intel-smartune)."

# 3b. Refuse to start if the port is already taken (typically a SmarTune started
# from a source tree). Exit 3 is listed in the unit's RestartPreventExitStatus, so
# systemd gives up at once instead of restart-looping on "Address already in use".
PORT="${BALANCER_PORT:-9001}"
if command -v ss >/dev/null 2>&1 && [ -n "$(ss -Hltn "sport = :$PORT" 2>/dev/null)" ]; then
    echo "[smartune] ERROR: port $PORT is already in use by another program:" >&2
    ss -Hltnp "sport = :$PORT" >&2 || true
    echo "[smartune] Stop that program, then launch SmarTune again." >&2
    exit 3
fi

# 4. Launch the combined balancer + monitor (port 9001, serves the dashboard UI
# and its /api). smartune.py -a handles the balancer/ chdir + sys.path setup.
exec "$INSTALL_DIR/smartune-venv/bin/python" "$INSTALL_DIR/smartune.py" -a
