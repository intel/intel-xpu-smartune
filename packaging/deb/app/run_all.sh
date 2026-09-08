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
if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
    echo "[smartune] ERROR: venv missing at $INSTALL_DIR/venv — reinstall the package." >&2
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
        -addext "subjectAltName=IP:127.0.0.1"
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

# 3. UI-lease auto-shutdown: on demand launch means nothing stops this root
# service when the user closes the dashboard. With this flag the service tracks
# open-UI leases (heartbeats from each tab) and exits cleanly once the last one
# is gone; exit code 0 means systemd's Restart=on-failure won't revive it. The
# grace windows have sensible defaults (see utils/ui_lease.py) and can be tuned
# via SMARTUNE_UI_*_GRACE env vars if needed.
export SMARTUNE_UI_LEASE=1

# 4. Launch the combined balancer + monitor (port 9001, serves the dashboard UI
# and its /api). smartune.py -a handles the balancer/ chdir + sys.path setup.
exec "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/smartune.py" -a
