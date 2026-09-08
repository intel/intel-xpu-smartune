#!/bin/bash
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Build the Python virtualenv the monitor runs in, installing the pinned deps
# OFFLINE from the wheels bundled in the .deb (see build_deb.sh). No PyPI access
# is needed or attempted. Idempotent: a stamp records the requirements hash so a
# reconfigure with unchanged pins is a no-op, and an upgrade with changed pins
# rebuilds from the new bundled wheels.
#
# Called ONLY from the deb postinst — venv creation is an install-time step, not
# a launch-time one. If it fails, postinst fails and apt reports the install as
# failed (fail-fast), rather than deferring a fragile pip run to first launch.

set -e

INSTALL_DIR="/opt/intel/smartune"
VENV="$INSTALL_DIR/venv"
REQ="$INSTALL_DIR/requirements.txt"
WHEELS="$INSTALL_DIR/wheels"
STAMP="$VENV/.deps_ok"

# venv mode, recorded by build_deb.sh alongside the wheels:
#   isolated  monitor-only deb — a clean venv (default if the marker is absent).
#   system    full deb — created with --system-site-packages so the apt-installed
#             bcc/eBPF binding (python3-bpfcc) is importable; the balancer needs it.
VENV_MODE="$(cat "$WHEELS/.venv_mode" 2>/dev/null || echo isolated)"

# Stamp keyed on both the requirements pins AND the venv mode: a mode switch
# (e.g. reinstalling the full deb over an isolated monitor venv, or vice-versa)
# must force a rebuild, else the reused venv would lack system site-packages and
# the balancer's `from bcc import BPF` would fail at launch.
req_hash="$(sha256sum "$REQ" 2>/dev/null | cut -d' ' -f1):$VENV_MODE"

if [ -x "$VENV/bin/python" ] && [ -f "$STAMP" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$req_hash" ]; then
    exit 0
fi

if [ ! -d "$WHEELS" ]; then
    echo "[smartune] ERROR: bundled wheels missing at $WHEELS; the package is incomplete." >&2
    exit 1
fi

echo "[smartune] Building Python environment from bundled wheels..."

# --system-site-packages only for the full (balancer) deb, so its venv can import
# the apt-installed bcc binding. An existing venv created in the other mode would
# have the wrong site-packages visibility, so drop it and recreate cleanly rather
# than reusing it — cheap, and the deps are reinstalled from the bundled wheels.
[ "$VENV_MODE" = system ] && SSP=(--system-site-packages) || SSP=()
if [ -x "$VENV/bin/python" ]; then
    cfg="$VENV/pyvenv.cfg"
    want="false"; [ "$VENV_MODE" = system ] && want="true"
    have="$(sed -n 's/^include-system-site-packages *= *//p' "$cfg" 2>/dev/null | tr 'A-Z' 'a-z' | tr -d ' ')"
    [ "$have" = "$want" ] || rm -rf "$VENV"
fi
if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "${SSP[@]}" "$VENV"
fi

# The bundled native wheels are built for one CPython minor (see build_deb.sh).
# If this host's Python differs, the --no-index install below would fail with a
# cryptic "no matching distribution"; catch it here with an actionable message.
host_py="$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
bundle_py="$(cat "$WHEELS/.python_version" 2>/dev/null || true)"
if [ -n "$bundle_py" ] && [ "$host_py" != "$bundle_py" ]; then
    echo "[smartune] ERROR: this package bundles wheels for CPython $bundle_py but this system has $host_py." >&2
    echo "[smartune]        Install the build for this OS release (one .deb is built per release)." >&2
    exit 1
fi

# --no-index + --find-links: install strictly from the bundled wheels, never
# reaching the network. Fails loudly if a required wheel is absent.
"$VENV/bin/python" -m pip install --no-index --find-links="$WHEELS" -r "$REQ"

echo "$req_hash" > "$STAMP"
echo "[smartune] Python environment ready."
