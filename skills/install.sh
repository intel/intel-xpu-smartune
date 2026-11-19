#!/usr/bin/env bash
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# One-step setup of the SmarTune agent integration for the current user:
#
#   1. the `smartune` CLI and the MCP server `smartune-mcp` in ~/.local/bin (MCP is skipped
#      with --mcp no, or with a warning when Python < 3.10 or PyPI cannot be reached)
#   2. a copy of the running instance's API token and TLS certificate in
#      ~/.config/smartune/, where the CLI finds them without any variable set
#   3. the shared agent rules and references (agent/) in ~/.local/share/smartune/
#   4. the skills, for OpenClaw and/or Claude Code (all four; --control no leaves out
#      smartune-control)
#   5. a final `smartune doctor` run
#
# Works with both a deb install (/opt/intel/smartune) and the source code run
# with start_smartune.sh: only where the instance keeps key/ differs. Run it as
# the user the agent runs as, not as root; it calls sudo only to copy the two
# root-only key files. Running it again updates everything in place.
#
#   skills/install.sh                     detect the instance and the agents
#   skills/install.sh --target openclaw   only OpenClaw
#   skills/install.sh --instance source   use the key/ of this source code directory
#   skills/install.sh --mcp no            CLI and skills only, no MCP server
#   skills/install.sh --control no        read-only skills only (no smartune-control)
#   skills/install.sh --remote user@host  use SmarTune on another machine (run it there first)
#   skills/install.sh --uninstall         remove what this script installed
#
# In the deb package this script is /usr/bin/smartune-agent-install.

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
SDK_SRC="$REPO/sdk"
AGENT_SRC="$REPO/agent"
READ_SKILLS=(smartune-status smartune-diagnose smartune-bench)
CONTROL_SKILL=smartune-control                            # writes: opt out with --control no
ALL_SKILLS=("${READ_SKILLS[@]}" "$CONTROL_SKILL")
DEB_DIR="/opt/intel/smartune"
AGENT_WHEELS="$REPO/wheels/agent"                         # bundled by packaging/deb/build_deb.sh
if [[ "$REPO" == "$DEB_DIR" ]]; then
    SELF_CMD="smartune-agent-install"                     # how to name this script in messages
else
    SELF_CMD="skills/install.sh"
fi

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/smartune"   # same rule as sdk client.config_dir()
DATA_DIR="$HOME/.local/share/smartune"                    # an agent/ candidate in sdk mcp_server.py
BIN_DIR="$HOME/.local/bin"
MARKER="# smartune-agent-install"                         # tags the launchers written here

TARGETS="auto"
SMARTUNE_DIR=""
WITH_MCP=1
MCP_REQUIRED=0                                            # --mcp: fail instead of skipping MCP
WITH_CONTROL=1
SKIP_CREDENTIALS=0
UNINSTALL=0
REMOTE=""
REMOTE_PORT=9001
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
OPENCLAW_WORKSPACE=""
CLAUDE_HOME="$HOME/.claude"

tildify() { printf '%s' "${1/#$HOME/\~}"; }  # shorten paths under $HOME for display

# -- what a plain run would do here (shown by --help and in the plan) -------------

default_instance() {  # where the key files come from with --instance auto (the default)
    if [[ -d "$DEB_DIR" ]]; then echo "$DEB_DIR"; else echo "$REPO"; fi
}

instance_label() {  # instance_label DIR
    case "$1" in
        "$DEB_DIR") echo "deb install" ;;
        "$REPO")    echo "source code" ;;
        *)          echo "instance at $1" ;;
    esac
}

python_version() { python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "none"; }

bundled_wheels_fit() {  # the deb's wheels/agent/ was built for this CPython (native deps)
    [[ -f "$AGENT_WHEELS/.python_version" ]] || return 1
    [[ "$(cat "$AGENT_WHEELS/.python_version")" == "$(python_version)" ]]
}

mcp_source() {  # how the MCP server would be installed here
    if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        echo "skipped: needs Python >= 3.10 (this is $(python_version))"
    elif bundled_wheels_fit; then
        echo "from the bundled wheels (no network)"
    else
        echo "from PyPI (needs network; set HTTPS_PROXY behind a proxy)"
    fi
}

usage() {
    cat <<EOF
Usage: $SELF_CMD [OPTIONS]

Set up the SmarTune CLI, credentials and agent skills for the current user.

Options:
  --target LIST          openclaw, claude, both (openclaw,claude) or none.
                         Default: every agent found (~/.openclaw, ~/.claude).
  --instance auto|deb|source|DIR
                         The SmarTune instance whose key/ (token and certificate) is copied.
                         Default: auto.
                         auto:   deb when $DEB_DIR exists, otherwise source.
                         deb:    the deb install, $DEB_DIR/key.
                         source: this source code directory, for start_smartune.sh:
                                 $(tildify "$REPO")/key.
                         DIR:    DIR/key, for an instance installed elsewhere (for example
                                 the source code directory when this script comes from the deb).
  --mcp auto|yes|no      The MCP server (smartune-mcp). Default: auto.
                         auto: install it; when it cannot be (it needs Python >= 3.10 and
                               python3-venv), warn and go on without it.
                         yes:  install it, and stop with an error when that fails.
                         no:   do not install it; removes one installed earlier.
                         When installed, it is also registered with OpenClaw (read-only
                         tools) if its 'openclaw mcp' command is available.
  --control yes|no       The smartune-control skill. Default: yes (not shipped in the
                         monitor-only deb). It lets the agent register apps and change
                         limits, priorities, freeze state and core binding, each change
                         applied only after the user agrees to a plan. The token it uses
                         can call every endpoint, so install it only for an agent you trust.
                         no: leave it out; removes one installed earlier.
  --skip-credentials     Do not copy the token and certificate.
                         Default: copy them to ~/.config/smartune/.
  --remote USER@HOST     SmarTune runs on another machine: copy the token and certificate
                         from HOST (where this script must have run first, as USER) with
                         scp, and use https://HOST:PORT. The server must listen on the
                         network (SMARTUNE_LISTEN in /etc/default/intel-smartune).
                         Default: off (SmarTune on this machine, https://127.0.0.1:9001).
  --port PORT            SmarTune port for --remote. Default: 9001.
  --openclaw-workspace D OpenClaw agent workspace. Default: $OPENCLAW_HOME/workspace.
  --uninstall            Remove everything this script installed.
  -h, --help             Show this help.

EOF
}

say()  { printf '==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
# copy_tree SRC DST: cp -r, with the copy writable for this user
copy_tree() { cp -r "$1" "$2" && chmod -R u+w "$2"; }
shq()  { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }  # single-quote for sh

ensure_dir() {  # ensure_dir DIR...: create each DIR; stop unless this user can write to it
    local dir top
    for dir in "$@"; do
        mkdir -p "$dir" 2>/dev/null || true
        [[ -d "$dir" && -w "$dir" ]] && continue
        top="$dir"
        while [[ ! -e "$top" ]]; do top="$(dirname "$top")"; done
        die "$(id -un) cannot write to $dir ($top is owned by $(stat -c %U "$top")). Fix the owner, then rerun: sudo chown -R $(id -un): $(shq "$top")"
    done
}

set_instance() {  # set_instance auto|deb|source|DIR -> SMARTUNE_DIR ("" = auto)
    case "$1" in
        auto)   SMARTUNE_DIR="" ;;
        deb)    SMARTUNE_DIR="$DEB_DIR" ;;
        source)
            [[ "$REPO" != "$DEB_DIR" ]] || die "--instance source: this script comes from the deb package, not the source code; give the source code directory instead: --instance /path/to/source"
            SMARTUNE_DIR="$REPO" ;;
        *)
            [[ -d "$1" ]] || die "--instance takes auto, deb, source or an existing directory (got '$1')"
            SMARTUNE_DIR="$(cd "$1" && pwd)" ;;
    esac
}

set_mcp() {  # set_mcp auto|yes|no
    case "$1" in
        auto) WITH_MCP=1; MCP_REQUIRED=0 ;;
        yes)  WITH_MCP=1; MCP_REQUIRED=1 ;;
        no)   WITH_MCP=0; MCP_REQUIRED=0 ;;
        *)    die "--mcp takes auto, yes or no (got '$1')" ;;
    esac
}

set_control() {  # set_control yes|no
    case "$1" in
        yes) WITH_CONTROL=1 ;;
        no)  WITH_CONTROL=0 ;;
        *)   die "--control takes yes or no (got '$1')" ;;
    esac
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)             TARGETS="${2:?--target needs a value}"; shift 2 ;;
        --instance)           [[ $# -ge 2 ]] || die "--instance needs a value: auto, deb, source or a directory"
                              set_instance "$2"; shift 2 ;;
        --mcp)                [[ $# -ge 2 ]] || die "--mcp needs a value: auto, yes or no"
                              set_mcp "$2"; shift 2 ;;
        --control)            [[ $# -ge 2 ]] || die "--control needs a value: yes or no"
                              set_control "$2"; shift 2 ;;
        --skip-credentials)   SKIP_CREDENTIALS=1; shift ;;
        --remote)             REMOTE="${2:?--remote needs USER@HOST}"; shift 2 ;;
        --port)               REMOTE_PORT="${2:?--port needs a value}"; shift 2 ;;
        --openclaw-workspace) OPENCLAW_WORKSPACE="${2:?--openclaw-workspace needs a value}"; shift 2 ;;
        --uninstall)          UNINSTALL=1; shift ;;
        -h|--help)            usage; exit 0 ;;
        *)                    usage >&2; die "unknown option: $1" ;;
    esac
done
OPENCLAW_WORKSPACE="${OPENCLAW_WORKSPACE:-$OPENCLAW_HOME/workspace}"
# The monitor-only deb (smartune-monitor) has no balancer and ships without smartune-control.
CONTROL_SHIPPED=1
[[ -d "$HERE/$CONTROL_SKILL" ]] || CONTROL_SHIPPED=0
[[ $CONTROL_SHIPPED -eq 1 ]] || WITH_CONTROL=0  # the plan below says so
SKILLS=("${READ_SKILLS[@]}")
[[ $WITH_CONTROL -eq 1 ]] && SKILLS+=("$CONTROL_SKILL")

[[ $EUID -ne 0 ]] || die "run this as the user the agent runs as, not as root (it uses sudo itself where needed)"
command -v python3 >/dev/null || die "python3 is required"
PYTHON="$(command -v python3)"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 8))' || die "Python >= 3.8 is required"

# -- targets ---------------------------------------------------------------------

want_openclaw=0
want_claude=0
case "$TARGETS" in
    auto)
        { [[ -d "$OPENCLAW_HOME" ]] || command -v openclaw >/dev/null; } && want_openclaw=1
        { [[ -d "$CLAUDE_HOME" ]] || command -v claude >/dev/null; } && want_claude=1
        ;;
    none) ;;
    *)
        IFS=',' read -r -a list <<< "${TARGETS/both/openclaw,claude}"
        for t in "${list[@]}"; do
            case "$t" in
                openclaw) want_openclaw=1 ;;
                claude)   want_claude=1 ;;
                *)        die "unknown target '$t' (use openclaw, claude, both or none)" ;;
            esac
        done
        ;;
esac
[[ -n "$SMARTUNE_DIR" ]] || SMARTUNE_DIR="$(default_instance)"

# -- plan: say up front what this run does and where it puts things ----------------

if [[ $UNINSTALL -eq 0 ]]; then
    agents=()
    [[ $want_claude -eq 1 ]] && agents+=("Claude Code -> $(tildify "$CLAUDE_HOME")/skills/")
    [[ $want_openclaw -eq 1 ]] && agents+=("OpenClaw -> $(tildify "$OPENCLAW_WORKSPACE")/skills/")
    [[ ${#agents[@]} -gt 0 ]] || agents=("none (no skills installed)")
    if [[ "$TARGETS" == auto ]]; then agents_how="found on this machine; --target to choose"; else agents_how="--target $TARGETS"; fi
    if [[ -n "$REMOTE" ]]; then
        keys="from $REMOTE with scp, server https://${REMOTE##*@}:$REMOTE_PORT"
    elif [[ $SKIP_CREDENTIALS -eq 1 ]]; then
        keys="keep the ones in $(tildify "$CONFIG_DIR")/ (--skip-credentials)"
    else
        keys="$SMARTUNE_DIR/key ($(instance_label "$SMARTUNE_DIR")) -> $(tildify "$CONFIG_DIR")/"
    fi
    if [[ $WITH_MCP -eq 1 ]]; then mcp_plan="$(tildify "$BIN_DIR")/smartune-mcp, $(mcp_source)"; else mcp_plan="not installed (--mcp no)"; fi
    if [[ $WITH_CONTROL -eq 1 ]]; then
        control_plan="included (--control no to leave it out)"
    elif [[ $CONTROL_SHIPPED -eq 0 ]]; then
        control_plan="not in this package (monitor only, no balancer)"
    else
        control_plan="left out (--control no)"
    fi
    say "Plan for $(id -un) (all options and defaults: $SELF_CMD --help)"
    for a in "${agents[@]}"; do note "agent        $a"; done
    note "             ($agents_how)"
    note "skills       ${SKILLS[*]}"
    note "control      smartune-control $control_plan"
    note "key files    $keys"
    note "CLI          $(tildify "$BIN_DIR")/smartune"
    note "MCP server   $mcp_plan"
    note "agent rules  $(tildify "$DATA_DIR")/agent/"
fi

# -- helpers -----------------------------------------------------------------------

is_ours() {  # a launcher written by this script: our marker, or a link into DATA_DIR
    local path="$1"
    [[ -L "$path" && "$(readlink "$path")" == "$DATA_DIR"/* ]] && return 0
    [[ -f "$path" ]] && grep -qF "$MARKER" "$path" 2>/dev/null
}

claim_launcher() {  # refuse to overwrite a `smartune` someone else installed
    local path="$BIN_DIR/$1"
    if [[ -e "$path" || -L "$path" ]] && ! is_ours "$path"; then
        die "$path exists and was not installed by this script; remove it first (for pipx: pipx uninstall smartune-sdk)"
    fi
    rm -f "$path"
}

# Edit OpenClaw files with Python: JSON has to be merged, not overwritten.
openclaw_config() {  # openclaw_config enable|disable SKILL...
    local action="$1"; shift
    "$PYTHON" - "$action" "$OPENCLAW_HOME/openclaw.json" "$@" <<'PY'
import json, os, shutil, sys
action, path, skills = sys.argv[1], sys.argv[2], sys.argv[3:]
snippet = json.dumps({"skills": {"entries": {s: {"enabled": True} for s in skills}}}, indent=2)
if not os.path.isfile(path):
    if action == "enable":
        print(f"    {path} does not exist yet (run `openclaw onboard` first). Then merge:\n{snippet}")
    sys.exit(0)
try:
    with open(path, encoding="utf-8") as fh:
        config = json.load(fh)
except ValueError:
    print(f"    {path} is not plain JSON, so it was left alone. Merge this into it:\n{snippet}")
    sys.exit(0)
entries = config.setdefault("skills", {}).setdefault("entries", {})
if action == "enable":
    for s in skills:
        entry = entries.get(s) if isinstance(entries.get(s), dict) else {}
        entry["enabled"] = True
        entries[s] = entry
else:
    for s in skills:
        entries.pop(s, None)
backup = path + ".bak-smartune"
if not os.path.exists(backup):
    shutil.copy2(path, backup)
tmp = path + ".tmp-smartune"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(config, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
os.replace(tmp, path)
print(f"    {'enabled' if action == 'enable' else 'removed'} {', '.join(skills)} in {path} (backup: {backup})")
PY
}

openclaw_env() {  # openclaw_env set|unset: the SMARTUNE_* lines of ~/.openclaw/.env
    "$PYTHON" - "$1" "$OPENCLAW_HOME/.env" "$CONFIG_DIR" <<'PY'
import os, sys
action, path, config_dir = sys.argv[1:]
values = {
    "SMARTUNE_TOKEN_FILE": os.path.join(config_dir, "api_token"),
    "SMARTUNE_CACERT": os.path.join(config_dir, "b_server.crt"),
    "SMARTUNE_AGENT_ID": "openclaw",
}
lines = []
if os.path.isfile(path):
    with open(path, encoding="utf-8") as fh:
        lines = [l for l in fh.read().splitlines() if l.split("=", 1)[0].strip() not in values]
if action == "set":
    lines += [f"{k}={v}" for k, v in values.items()]
elif not os.path.isfile(path):
    sys.exit(0)
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + ("\n" if lines else ""))
os.chmod(path, 0o600)
print(f"    {'wrote' if action == 'set' else 'removed'} {', '.join(values)} in {path}")
PY
}

openclaw_supports_mcp() {  # whether this OpenClaw reads mcp.servers (it has `openclaw mcp`)
    command -v openclaw >/dev/null && openclaw mcp --help >/dev/null 2>&1
}

# OpenClaw's outbound MCP servers live under mcp.servers in openclaw.json. The
# entry is read-only (no --allow-control): whether OpenClaw shows MCP forms to
# the user is untested, and smartune-control falls back to the CLI, which asks
# in the conversation. Only an entry whose command is our launcher is removed.
openclaw_mcp() {  # openclaw_mcp add|remove
    "$PYTHON" - "$1" "$OPENCLAW_HOME/openclaw.json" "$BIN_DIR/smartune-mcp" <<'PY'
import json, os, shutil, sys
action, path, command = sys.argv[1:]
# A stdio server is just command/args (OpenClaw docs/cli/mcp/transports.md): `transport`
# is only for remote servers ("sse" / "streamable-http") and "stdio" fails validation.
entry = {"command": command, "args": []}
snippet = json.dumps({"mcp": {"servers": {"smartune": entry}}}, indent=2)
if not os.path.isfile(path):
    if action == "add":
        print(f"    {path} does not exist yet (run `openclaw onboard` first). Then merge:\n{snippet}")
    sys.exit(0)
try:
    with open(path, encoding="utf-8") as fh:
        config = json.load(fh)
except ValueError:
    if action == "add":
        print(f"    {path} is not plain JSON, so it was left alone. Merge this into it:\n{snippet}")
    sys.exit(0)
mcp = config.get("mcp") if isinstance(config.get("mcp"), dict) else {}
servers = mcp.get("servers") if isinstance(mcp.get("servers"), dict) else {}
current = servers.get("smartune")
if action == "add":
    if isinstance(current, dict) and current.get("command") not in (None, command):
        print(f"    {path} already has an mcp.servers.smartune entry of its own; left it alone")
        sys.exit(0)
    servers["smartune"] = entry
    mcp["servers"] = servers
    config["mcp"] = mcp
else:
    if not (isinstance(current, dict) and current.get("command") == command):
        sys.exit(0)
    del servers["smartune"]
backup = path + ".bak-smartune"
if not os.path.exists(backup):
    shutil.copy2(path, backup)
tmp = path + ".tmp-smartune"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(config, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
os.replace(tmp, path)
print(f"    {'registered' if action == 'add' else 'removed'} the smartune MCP server "
      f"(mcp.servers.smartune, read-only) in {path} (backup: {backup})")
PY
}

# -- uninstall ---------------------------------------------------------------------

if [[ $UNINSTALL -eq 1 ]]; then
    say "Removing the SmarTune agent integration for $(id -un)"
    for name in smartune smartune-mcp; do
        if is_ours "$BIN_DIR/$name"; then rm -f "$BIN_DIR/$name"; note "removed $BIN_DIR/$name"; fi
    done
    if [[ -d "$DATA_DIR" ]]; then rm -rf "$DATA_DIR"; note "removed $DATA_DIR"; fi
    for f in api_token b_server.crt url; do
        if [[ -e "$CONFIG_DIR/$f" ]]; then rm -f "$CONFIG_DIR/$f"; note "removed $CONFIG_DIR/$f"; fi
    done
    rmdir "$CONFIG_DIR" 2>/dev/null || true
    for s in "${ALL_SKILLS[@]}"; do
        for dir in "$OPENCLAW_WORKSPACE/skills/$s" "$CLAUDE_HOME/skills/$s"; do
            if [[ -d "$dir" ]]; then rm -rf "$dir"; note "removed $dir"; fi
        done
    done
    if [[ -d "$OPENCLAW_HOME" ]]; then
        openclaw_config disable "${ALL_SKILLS[@]}"
        openclaw_mcp remove
        openclaw_env unset
        note "Restart OpenClaw to apply: openclaw gateway restart"
    fi
    say "Done. The SmarTune service itself was not touched."
    exit 0
fi

# -- 1. CLI ------------------------------------------------------------------------

[[ -d "$SDK_SRC/smartune_sdk" ]] || die "cannot find the SDK at $SDK_SRC (run this script from the SmarTune source code, or use smartune-agent-install from the deb package)"
ensure_dir "$BIN_DIR" "$DATA_DIR"

skip_mcp() {  # skip_mcp REASON: fatal with --mcp, otherwise continue without MCP
    [[ $MCP_REQUIRED -eq 0 ]] || die "--mcp yes: $1"
    warn "$1; continuing without the MCP server (the CLI and skills work without it). Fix it and rerun to add MCP, or pass --mcp no to silence this."
    WITH_MCP=0
}

pip_install() {  # pip_install ARGS...: into the private venv, quiet; returns pip's status
    # Show what pip fetches, so a blocked network reads as slow progress, not a hang.
    set +e
    "$DATA_DIR/venv/bin/pip" install --progress-bar off --disable-pip-version-check \
        --timeout 20 --retries 2 "$@" 2>&1 \
        | grep -E --line-buffered '^(Successfully|ERROR|WARNING: Retrying)' | sed -u 's/^/    /'
    local status=${PIPESTATUS[0]}
    set -e
    return "$status"
}

install_venv() {  # install_venv EXTRAS ("" or "[mcp]"): the SDK in a private venv; returns pip's status
    say "Installing the CLI$([[ $WITH_MCP -eq 1 ]] && echo ' and the MCP server') into a private venv ($DATA_DIR/venv)"
    "$PYTHON" -m venv --clear "$DATA_DIR/venv" >/dev/null 2>&1 \
        || die "could not create a venv; install it with: sudo apt install python3-venv"
    local status=1
    if bundled_wheels_fit; then
        note "pip install smartune-sdk$1 from the bundled wheels ($AGENT_WHEELS, no network)"
        pip_install --no-index --find-links "$AGENT_WHEELS" "smartune-sdk$1" && status=0
        [[ $status -eq 0 ]] || note "the bundled wheels did not install; trying PyPI instead"
    elif [[ -d "$AGENT_WHEELS" ]]; then
        note "the bundled wheels ($AGENT_WHEELS) are for another Python version; using PyPI instead"
    fi
    if [[ $status -ne 0 ]]; then
        # pip writes build/ into the source tree: build from a copy.
        local src
        src="$(mktemp -d)"
        copy_tree "$SDK_SRC/." "$src/"
        rm -rf "$src/build" "$src"/*.egg-info
        note "pip install $SDK_SRC$1 (needs PyPI; set HTTPS_PROXY behind a proxy)"
        pip_install "$src$1" && status=0
        rm -rf "$src"
    fi
    [[ $status -eq 0 ]] || return "$status"
    rm -rf "$DATA_DIR/lib"
    local names=(smartune)
    [[ $WITH_MCP -eq 1 ]] && names+=(smartune-mcp)  # the entry point exists even without the MCP SDK
    for name in "${names[@]}"; do
        claim_launcher "$name"
        ln -s "$DATA_DIR/venv/bin/$name" "$BIN_DIR/$name" || die "could not create $BIN_DIR/$name"
        note "$BIN_DIR/$name -> $DATA_DIR/venv/bin/$name"
    done
}

if [[ $WITH_MCP -eq 1 ]] && ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    skip_mcp "the MCP server needs Python >= 3.10 (MCP SDK 2.x); this is $("$PYTHON" -V 2>&1)"
fi
if [[ $WITH_MCP -eq 1 ]] && ! install_venv "[mcp]"; then
    skip_mcp "pip could not install the MCP SDK (it needs network access to PyPI; check HTTPS_PROXY)"
fi
if [[ $WITH_MCP -eq 1 ]]; then
    :  # installed above
elif ! "$PYTHON" -I -c 'import requests' 2>/dev/null; then
    # The system Python has no requests: a private venv with just the SDK.
    install_venv "" || die "pip install failed (it needs network access to PyPI; check HTTPS_PROXY)"
else
    # The system Python already has requests, the only dependency: copy the
    # package and write a launcher. No pip, no network.
    say "Installing the CLI ($DATA_DIR/lib, using $PYTHON)"
    rm -rf "$DATA_DIR/lib" "$DATA_DIR/venv"
    mkdir -p "$DATA_DIR/lib"
    copy_tree "$SDK_SRC/smartune_sdk" "$DATA_DIR/lib/smartune_sdk"
    find "$DATA_DIR/lib" -name __pycache__ -type d -prune -exec rm -rf {} +
    claim_launcher smartune
    # -I: ignore PYTHON* variables, user site-packages and the current directory,
    # so nothing in the agent's working directory can shadow the SDK's imports.
    cat > "$BIN_DIR/smartune" <<EOF
#!/bin/sh
$MARKER: launcher written by $SELF_CMD; remove with $SELF_CMD --uninstall
SMARTUNE_SDK_LIB=$(shq "$DATA_DIR/lib") exec $(shq "$PYTHON") -I -c 'import os, sys; sys.path.insert(0, os.environ.pop("SMARTUNE_SDK_LIB")); from smartune_sdk.cli import main; sys.exit(main())' "\$@"
EOF
    chmod 755 "$BIN_DIR/smartune"
    note "$BIN_DIR/smartune"
fi
if [[ $WITH_MCP -eq 0 ]] && is_ours "$BIN_DIR/smartune-mcp"; then
    rm -f "$BIN_DIR/smartune-mcp"
    note "removed $BIN_DIR/smartune-mcp"
fi
note "version: $("$BIN_DIR/smartune" --version)"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not on PATH. Add it (e.g. in ~/.profile) so agents can run \`smartune\`; for OpenClaw also make sure the gateway's PATH includes it." ;;
esac

# -- 2. credentials ------------------------------------------------------------------

if [[ -n "$REMOTE" ]]; then
    remote_host="${REMOTE##*@}"
    [[ -n "$remote_host" && "$REMOTE_PORT" =~ ^[0-9]+$ ]] || die "--remote needs USER@HOST and --port a number"
    say "Copying the API token and certificate from $REMOTE (scp)"
    note "They come from ~/.config/smartune/ on $remote_host, put there by skills/install.sh (smartune-agent-install with the deb) on that machine."
    ensure_dir "$CONFIG_DIR"
    chmod 700 "$CONFIG_DIR"
    tmp_keys="$(mktemp -d)"
    if ! scp -q "$REMOTE:.config/smartune/api_token" "$REMOTE:.config/smartune/b_server.crt" "$tmp_keys/"; then
        rm -rf "$tmp_keys"
        die "could not copy them from $REMOTE. Run skills/install.sh (smartune-agent-install with the deb) on $remote_host first (as that user), and check that \`ssh $REMOTE\` works"
    fi
    install -m 600 "$tmp_keys/api_token" "$CONFIG_DIR/api_token"
    install -m 644 "$tmp_keys/b_server.crt" "$CONFIG_DIR/b_server.crt"
    rm -rf "$tmp_keys"
    printf 'https://%s:%s\n' "$remote_host" "$REMOTE_PORT" > "$CONFIG_DIR/url"
    chmod 600 "$CONFIG_DIR/url"
    note "$CONFIG_DIR/api_token, $CONFIG_DIR/b_server.crt, $CONFIG_DIR/url (https://$remote_host:$REMOTE_PORT)"
    note "This token can call every SmarTune endpoint and now lives on this machine too; the CLI only sends read requests."
elif [[ $SKIP_CREDENTIALS -eq 0 ]]; then
    rm -f "$CONFIG_DIR/url"  # local instance: the default https://127.0.0.1:9001
    KEY_DIR="$SMARTUNE_DIR/key"
    say "Copying the API token and certificate from $KEY_DIR ($(instance_label "$SMARTUNE_DIR"))"

    exists() { [[ -e "$1" ]] || sudo test -e "$1"; }
    if ! exists "$KEY_DIR/api_token" || ! exists "$KEY_DIR/b_server.crt"; then
        die "$KEY_DIR/api_token or b_server.crt is missing. Start SmarTune once first (it creates them), or pick the right instance with --instance deb|source|DIR"
    fi
    ensure_dir "$CONFIG_DIR"
    chmod 700 "$CONFIG_DIR"
    copy_key() {  # copy_key NAME MODE: plain copy when readable, else through sudo
        local src="$KEY_DIR/$1" dst="$CONFIG_DIR/$1"
        if [[ -r "$src" ]]; then
            install -m "$2" "$src" "$dst"
        else
            sudo install -m "$2" -o "$(id -un)" -g "$(id -gn)" "$src" "$dst"
        fi
    }
    copy_key api_token 600
    copy_key b_server.crt 644
    note "$CONFIG_DIR/api_token, $CONFIG_DIR/b_server.crt"
    note "This token can call every SmarTune endpoint; the CLI only sends read requests."
fi

# -- 3. agent rules and references ---------------------------------------------------

say "Installing agent rules and references ($DATA_DIR/agent)"
rm -rf "$DATA_DIR/agent"
copy_tree "$AGENT_SRC" "$DATA_DIR/agent"

# -- 4. skills -------------------------------------------------------------------------

# Each SKILL.md names the shared rules and references as `agent/policy/...` and
# `agent/content/...`. Clients resolve such paths against the skill's own directory, so every
# installed skill gets its own copy of agent/. The repository keeps the single source; these
# copies are regenerated on every install, and --uninstall removes them with the skill.
install_skills_into() {  # install_skills_into DIR: returns 0 when it removed the control skill
    ensure_dir "$1"
    for s in "${SKILLS[@]}"; do
        rm -rf "${1:?}/$s"
        copy_tree "$HERE/$s" "$1/$s"
        copy_tree "$AGENT_SRC" "$1/$s/agent"
    done
    note "copied ${SKILLS[*]} to $1 (each with its own agent/)"
    if [[ $WITH_CONTROL -eq 0 && -d "$1/$CONTROL_SKILL" ]]; then
        rm -rf "${1:?}/$CONTROL_SKILL"
        if [[ $CONTROL_SHIPPED -eq 1 ]]; then
            note "removed $1/$CONTROL_SKILL (rerun with --control yes to install it again)"
        else
            note "removed $1/$CONTROL_SKILL (this package has no balancer to control)"
        fi
        return 0
    fi
    return 1
}

if [[ $want_openclaw -eq 1 ]]; then
    say "Installing the skills for OpenClaw"
    if install_skills_into "$OPENCLAW_WORKSPACE/skills"; then
        openclaw_config disable "$CONTROL_SKILL"
    fi
    openclaw_config enable "${SKILLS[@]}"
    openclaw_env set
    if [[ $WITH_MCP -eq 0 ]]; then
        openclaw_mcp remove
    elif openclaw_supports_mcp; then
        openclaw_mcp add
        openclaw_mcp_added=1
    else
        note "MCP not registered with OpenClaw: this OpenClaw has no \`openclaw mcp\` command (older"
        note "version, or not on PATH). The skills use the CLI and work without it. To add it later,"
        note "merge into openclaw.json: {\"mcp\": {\"servers\": {\"smartune\": {\"command\": \"$BIN_DIR/smartune-mcp\", \"args\": []}}}}"
    fi
fi
if [[ $want_claude -eq 1 ]]; then
    say "Installing the skills for Claude Code"
    install_skills_into "$CLAUDE_HOME/skills" || true
fi
if [[ $WITH_CONTROL -eq 1 && ( $want_openclaw -eq 1 || $want_claude -eq 1 ) ]]; then
    warn "smartune-control is installed: the agent can change limits, priorities, freeze state and core binding after the user confirms each plan. The token it uses can call every SmarTune endpoint; the CLI only sends the control actions."
fi
if [[ $want_openclaw -eq 0 && $want_claude -eq 0 ]]; then
    say "No agent selected or found: skipped the skills (rerun with --target openclaw or --target claude)"
fi

# -- 5. verify ---------------------------------------------------------------------------

say "Checking the connection (smartune doctor)"
doctor_ok=1
if ! out="$("$BIN_DIR/smartune" doctor 2>&1)"; then doctor_ok=0; fi
"$PYTHON" - "$out" <<'PY' || true
import json, sys
try:
    report = json.loads(sys.argv[1])
except ValueError:
    sys.exit("    could not read the doctor report:\n" + sys.argv[1])
for check in report.get("checks", []):
    print("    {:<5} {:<10} {}".format(check["status"], check["name"], check["detail"]))
    if check.get("hint") and check["status"] != "ok":
        print("          -> " + check["hint"])
PY

echo
say "Installed for $(id -un)"
note "CLI          $(tildify "$BIN_DIR")/smartune"
if [[ $WITH_MCP -eq 1 ]]; then
    note "MCP server   $(tildify "$BIN_DIR")/smartune-mcp"
else
    note "MCP server   not installed"
fi
if [[ -n "$REMOTE" || $SKIP_CREDENTIALS -eq 0 ]]; then
    note "key files    $(tildify "$CONFIG_DIR")/ (api_token, b_server.crt)"
fi
note "agent rules  $(tildify "$DATA_DIR")/agent/"
[[ $want_claude -eq 1 ]] && note "Claude Code  $(tildify "$CLAUDE_HOME")/skills/: ${SKILLS[*]}"
[[ $want_openclaw -eq 1 ]] && note "OpenClaw     $(tildify "$OPENCLAW_WORKSPACE")/skills/: ${SKILLS[*]}"
note "uninstall    $SELF_CMD --uninstall"
echo
if [[ $doctor_ok -eq 1 ]]; then
    say "Done."
else
    warn "Installed, but SmarTune is not usable yet; fix the first failed check above and run: smartune doctor"
fi
if [[ $want_openclaw -eq 1 ]]; then
    note "OpenClaw: restart the gateway to load the skills$([[ ${openclaw_mcp_added:-0} -eq 1 ]] && echo ' and MCP server'): openclaw gateway restart"
fi
if [[ $want_claude -eq 1 ]]; then
    note "Claude Code: the skills are available in new sessions."
    if [[ $WITH_MCP -eq 1 ]]; then
        mcp_args=""
        [[ $WITH_CONTROL -eq 1 ]] && mcp_args=" --allow-control"
        # The server name goes first: --env takes several values and would swallow it.
        note "MCP for Claude Code: claude mcp add smartune --scope user --transport stdio --env SMARTUNE_AGENT_ID=claude-code -- $BIN_DIR/smartune-mcp$mcp_args"
        if [[ -n "$mcp_args" ]]; then
            note "(in the VS Code extension leave out --allow-control: it declines MCP forms without showing them)"
        fi
    fi
fi
