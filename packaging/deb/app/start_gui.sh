#!/bin/bash
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Desktop launcher — the .desktop entry's Exec. Runs as the logged-in *user*
# (NOT root), so it can open the browser in that user's graphical session. The
# only privileged step is starting the systemd service, done via pkexec, which
# pops the graphical polkit password prompt.

URL="https://localhost:9001"
UNIT="smartune-monitor.service"
# Fixed-path privileged helper. Elevated via pkexec; a polkit action registered
# against this exact path (com.intel.smartune.policy) gives the auth prompt a
# readable message instead of dumping the raw command line into the dialog.
AUTH_HELPER="/opt/intel/smartune/smartune-authhelper"

notify() { command -v notify-send >/dev/null 2>&1 && notify-send "Intel XPU SmarTune" "$1" || echo "[smartune] $1"; }
# The root page is protected, so an unauthenticated request normally returns
# 401 even when the service is healthy. Only curl's 000 response means that
# the server could not be reached.
is_up() { [ "$(curl -k -sS --connect-timeout 2 -o /dev/null -w '%{http_code}' "$URL" 2>/dev/null)" != "000" ]; }

# A SINGLE privileged step (one graphical polkit prompt) on every launch: run the
# helper, which starts the service if it is not already running and prints the
# root-only api_token on stdout.
#
# We fetch the token *even when the service is already up* because the token is
# the only thing that auto-logs the dashboard in. The api_token file is mode
# 0600 owned by root, so an unprivileged user cannot read it without elevating.
# Doing this unconditionally means clicking the icon always lands straight in the
# app — regardless of whether a previous launch left the service running, and
# regardless of whether this browser happens to have a token cached from before.
if is_up; then
    notify "Connecting to the monitor service..."
else
    notify "Starting the monitor service..."
fi
TOKEN="$(pkexec "$AUTH_HELPER")" || { notify "Failed to start the monitor service (authentication cancelled?)."; exit 1; }
TOKEN="${TOKEN//[$'\n\r ']/}"

# Wait for the server to accept connections. When the service was already up this
# returns immediately; on a cold start the venv is already built at install time,
# so this is just process start + cert check + Flask bind — 20s is ample headroom.
for _ in $(seq 1 20); do
    is_up && break
    sleep 1
done

if ! is_up; then
    notify "The monitor did not come up in time. Check: journalctl -u $UNIT"
fi

# Open the dashboard in the user's default browser. Pass the token in the URL
# *hash* (never sent to the server / access log) so the dashboard auto-logs-in;
# the dashboard consumes it, overwrites any stale token in localStorage, and
# strips it from the address bar.
#
# The ?b=<epoch> cache-buster makes the URL a *distinct document* on every
# launch. Without it, when a https://localhost:9001 tab is already open, browsers
# tend to just focus that tab and IGNORE the new #token= fragment — so the app
# keeps using the previous (now stale) localStorage token and every request 401s.
# A changing query forces an actual navigation, so the fresh token always applies.
# Only the timestamp lands in the query/access log; the token stays in the hash.
if [ -n "$TOKEN" ]; then
    xdg-open "$URL/?b=$(date +%s)#token=$TOKEN" >/dev/null 2>&1 || true
else
    xdg-open "$URL" >/dev/null 2>&1 || true
fi
