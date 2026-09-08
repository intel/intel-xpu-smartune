#!/bin/bash
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Build the smartune-monitor .deb.
#
#   packaging/deb/build_deb.sh [VERSION] [OPTIONS]
#
# The dashboard is rebuilt (npm run build) by default so a plain run always
# packages the current frontend. Pass --skip-ui to reuse an existing
# dashboard/dist and skip the (slow) npm build when the frontend is unchanged.
#
# Packages a *monitor-only* subset of the repo (no balancer/eBPF) plus the built
# dashboard, wired so the Flask monitor serves both the UI and its /api at
# https://localhost:9001. The app installs to /opt/intel/smartune; the desktop
# icon starts it on demand and opens the browser.
#
# Requirements on the build host: dpkg-deb, git, python3-pip, and (only if the
# dashboard needs (re)building) Node.js 20.19+ / npm. Runtime Python deps are
# downloaded as wheels at build time and bundled into the .deb, so the target
# installs them offline into a venv at install time (no network on the target).

set -euo pipefail

VERSION="1.5.0"
ARCH="amd64"
SKIP_UI=0
FULL=0

usage() {
        cat <<EOF
Usage: $(basename "$0") [VERSION] [OPTIONS]

Build a Debian package for Intel XPU SmarTune.

Arguments:
    VERSION       Package version (default: $VERSION)

Options:
    --skip-ui     Reuse dashboard/dist instead of running npm ci and npm run build.
                                If dashboard/dist/index.html is missing, the dashboard is built.
    --full        Build the full smartune package with the balancer and monitor.
    --all         Alias for --full.
    -h, --help    Show this help message and exit.

Examples:
    $(basename "$0")
    $(basename "$0") 1.5.1
    $(basename "$0") 1.5.1 --skip-ui
    $(basename "$0") 1.5.1 --full

The default build produces smartune-monitor. The full build produces smartune.
The .deb file is written to the repository's build/ directory.
EOF
}

for arg in "$@"; do
    case "$arg" in
                -h|--help)     usage; exit 0 ;;
        --skip-ui)     SKIP_UI=1 ;;
        --full|--all)  FULL=1 ;;
        -*)            echo "Unknown option: $arg" >&2; exit 1 ;;
        *)             VERSION="$arg" ;;
    esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$HERE" rev-parse --show-toplevel)"

# Two mutually-exclusive package variants sharing the install dir and service
# name (see --full). The flag switches only: package name, staged source paths,
# control template, install-time run script, and the venv site-packages mode.
#   monitor-only (default): smartune-monitor, isolated venv, smartune.py -m.
#   full (--full/--all):    smartune, --system-site-packages venv (for the
#                           apt-installed bcc), balancer + monitor, smartune.py -a.
if [ "$FULL" -eq 1 ]; then
    PKG_NAME="smartune"
    CONTROL_TMPL="control.full"
    RUN_SCRIPT="run_all.sh"
    VENV_MODE="system"
    # Monitor subset PLUS the balancer and the benchmark toolchain. benchmark/ is
    # only ~800 KB of scripts once its generated runtime/ is excluded (see
    # STAGE_EXCLUDES); the monitor-only variant ships without it and hides the tab.
    SRC_PATHS=(smartune.py smartune_api.py features.py requirements.txt monitor utils db config balancer benchmark)
    echo "==> Building FULL package (balancer + monitor): $PKG_NAME"
else
    PKG_NAME="smartune-monitor"
    CONTROL_TMPL="control"
    RUN_SCRIPT="run_monitor.sh"
    VENV_MODE="isolated"
    SRC_PATHS=(smartune.py smartune_api.py features.py requirements.txt monitor utils db config)
    echo "==> Building monitor-only package: $PKG_NAME"
fi
STAGE="$REPO_ROOT/build/${PKG_NAME}_${VERSION}_${ARCH}"
APP_DIR="$STAGE/opt/intel/smartune"
DEB_OUT="$REPO_ROOT/build/${PKG_NAME}_${VERSION}_${ARCH}.deb"

echo "==> Cleaning staging area"
rm -rf "$STAGE" "$DEB_OUT"
mkdir -p "$APP_DIR" \
         "$STAGE/DEBIAN" \
         "$STAGE/lib/systemd/system" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/scalable/apps" \
         "$STAGE/usr/share/polkit-1/actions"

# --- 1. Built dashboard (dashboard/dist) ------------------------------------
# Rebuilt by default; --skip-ui reuses an existing dist. A --skip-ui with no
# prebuilt dist can't be honoured (nothing to package), so we build anyway.
DIST="$REPO_ROOT/dashboard/dist"
if [ "$SKIP_UI" -eq 1 ] && [ -f "$DIST/index.html" ]; then
    echo "==> Skipping dashboard build (--skip-ui); reusing existing dist"
else
    [ "$SKIP_UI" -eq 1 ] && echo "==> --skip-ui ignored: no prebuilt dist to reuse"
    echo "==> Building dashboard (npm run build)"
    ( cd "$REPO_ROOT/dashboard" && npm ci && npm run build )
fi
[ -f "$DIST/index.html" ] || { echo "ERROR: dashboard build did not produce $DIST/index.html." >&2; exit 1; }

# --- 2. Clean source tree ---------------------------------------------------
echo "==> Staging application tree"
# Copy only the modules the selected variant imports (config/db/utils/monitor +
# smartune*.py/features.py, plus balancer/ for --full), straight from the working tree so
# local changes are picked up and node_modules / dev assets are never touched.
# Runtime/dev artifacts are dropped at COPY time rather than deleted afterwards.
# That matters for benchmark/runtime specifically: it holds the benchmark venv,
# downloaded models and IR, and is routinely tens of GB on a machine that has run
# a benchmark — staging it only to delete it would be slow and could fill the
# build host's disk. tar is Essential on Debian, so this adds no build dependency.
STAGE_EXCLUDES=(
    --exclude=__pycache__
    --exclude='*.pyc'
    --exclude=node_modules
    # Generated benchmark state. Recreated on first use at the env root, which
    # defaults to <install dir>/benchmark/runtime — see config.yaml's
    # benchmark.env_root to move it onto a data volume.
    --exclude='benchmark/runtime'
    # Dev-only: eBPF experiments and unit tests.
    --exclude='balancer/test'
    --exclude='config/runtime_state.json'
)
for p in "${SRC_PATHS[@]}"; do
    [ -e "$REPO_ROOT/$p" ] || { echo "ERROR: missing $p" >&2; exit 1; }
done
# pipefail (set at the top) makes a failure in either half abort the build.
tar -C "$REPO_ROOT" "${STAGE_EXCLUDES[@]}" -cf - "${SRC_PATHS[@]}" | tar -C "$APP_DIR" -xf -

# The built UI (copied fresh so an uncommitted rebuild is picked up).
mkdir -p "$APP_DIR/dashboard/dist"
cp -a "$DIST/." "$APP_DIR/dashboard/dist/"

# Vendored Python wheels: download every runtime dep (incl. transitive) as a
# wheel so the target installs them offline, with no PyPI access at install time.
#
# Some deps ship native, CPython-version-specific wheels (markupsafe, pyyaml,
# charset-normalizer, ...), so the bundle is tied to one CPython minor. We build
# it for THIS host's Python, which is the standard "build on the distro you ship
# to" model: build on Ubuntu 24.04 -> a 3.12 bundle for 24.04; build on 22.04
# later -> a 3.10 bundle for 22.04. One .deb per distro release, each self-
# contained. Nothing to change here when adding a release — just build there.
#
# --only-binary=:all: guarantees no sdist sneaks in (which would need a compiler
# on the target); if a pin has no matching wheel the build fails loudly here,
# which is exactly where a missing dependency should surface.
PY_TAG="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "==> Downloading Python wheels for offline install (CPython $PY_TAG)"
WHEELS_DIR="$APP_DIR/wheels"
mkdir -p "$WHEELS_DIR"
python3 -m pip download \
    --only-binary=:all: --prefer-binary \
    -r "$APP_DIR/requirements.txt" \
    -d "$WHEELS_DIR"
# Record which CPython the bundle targets so mismatches are diagnosable.
echo "$PY_TAG" > "$APP_DIR/wheels/.python_version"
# Record the venv mode for ensure_venv.sh: the full package needs a
# --system-site-packages venv so the apt-installed bcc binding is importable.
echo "$VENV_MODE" > "$APP_DIR/wheels/.venv_mode"

# Runtime launchers (kept under packaging/, installed alongside the app). Only
# the run script the service references is installed ($RUN_SCRIPT); the venv is
# built at install time by ensure_venv.sh.
install -m 0755 "$HERE/app/ensure_venv.sh"      "$APP_DIR/ensure_venv.sh"
install -m 0755 "$HERE/app/$RUN_SCRIPT"         "$APP_DIR/$RUN_SCRIPT"
install -m 0755 "$HERE/app/start_gui.sh"        "$APP_DIR/start_gui.sh"
# Privileged helper elevated by start_gui.sh via pkexec. Its polkit action
# (installed below) is matched by this exact path, so it must not move.
install -m 0755 "$HERE/app/smartune-authhelper" "$APP_DIR/smartune-authhelper"

# --- 3. systemd unit, desktop entry, icon -----------------------------------
# The unit filename stays smartune-monitor.service in both variants (authhelper /
# start_gui.sh / prerm reference it by that fixed name); only its ExecStart
# differs, rendered from the template to point at the variant's run script.
sed -e "s#@RUN_SCRIPT@#/opt/intel/smartune/$RUN_SCRIPT#" \
    "$HERE/templates/smartune.service.in" > "$STAGE/lib/systemd/system/smartune-monitor.service"
chmod 0644 "$STAGE/lib/systemd/system/smartune-monitor.service"
install -m 0644 "$HERE/smartune-monitor.desktop" "$STAGE/usr/share/applications/smartune-monitor.desktop"
install -m 0644 "$HERE/smartune-monitor.svg" \
                "$STAGE/usr/share/icons/hicolor/scalable/apps/smartune-monitor.svg"

# polkit action: gives the pkexec prompt for smartune-authhelper a readable
# message instead of dumping the raw command line into the password dialog.
install -m 0644 "$HERE/com.intel.smartune.policy" \
                "$STAGE/usr/share/polkit-1/actions/com.intel.smartune.policy"

# --- 4. DEBIAN control + maintainer scripts ---------------------------------
sed -e "s/@VERSION@/$VERSION/" -e "s/@ARCH@/$ARCH/" \
    "$HERE/templates/$CONTROL_TMPL" > "$STAGE/DEBIAN/control"
for s in postinst prerm postrm; do
    install -m 0755 "$HERE/templates/$s" "$STAGE/DEBIAN/$s"
done

# --- 5. Build ----------------------------------------------------------------
echo "==> Building $DEB_OUT"
dpkg-deb --root-owner-group --build "$STAGE" "$DEB_OUT"

# The staging tree was only an intermediate; the .deb now contains everything.
# Remove it so build/ holds just the package (a fresh build re-creates it anyway).
rm -rf "$STAGE"

echo ""
echo "Done: $DEB_OUT"
echo "  Install:  sudo apt install $DEB_OUT      # resolves Depends"
echo "        or  sudo dpkg -i $DEB_OUT && sudo apt-get -f install"
echo "  Launch:   application menu -> 'Intel XPU SmarTune Monitor' (or the desktop icon)"
