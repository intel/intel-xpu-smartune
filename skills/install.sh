#!/usr/bin/env bash
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# One-step setup of the SmarTune agent integration for the current user:
#
#   1. the `smartune` CLI and the MCP server `smartune-mcp` in ~/.local/bin (MCP is skipped
#      with --no-mcp, or with a warning when Python < 3.10 or PyPI cannot be reached)
#   2. a copy of the running instance's API token and TLS certificate in
#      ~/.config/smartune/, where the CLI finds them without any variable set
#   3. the shared agent rules and references (agent/) in ~/.local/share/smartune/
#   4. the skills, for OpenClaw and/or Claude Code (all four; --no-control leaves out
#      smartune-control)
#   5. a final `smartune doctor` run
#
# Works with both a deb install (/opt/intel/smartune) and a source checkout run
# with start_smartune.sh: only where the instance keeps key/ differs. Run it as
# the user the agent runs as, not as root; it calls sudo only to copy the two
# root-only key files. Running it again updates everything in place.
#
#   skills/install.sh                     detect the instance and the agents
#   skills/install.sh --target openclaw   only OpenClaw
#   skills/install.sh --source            use the key/ of this checkout
#   skills/install.sh --no-mcp            CLI and skills only, no MCP server
#   skills/install.sh --no-control        read-only skills only (no smartune-control)
#   skills/install.sh --remote user@host  use SmarTune on another machine (run it there first)
#   skills/install.sh --uninstall         remove what this script installed

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
SDK_SRC="$REPO/sdk"
AGENT_SRC="$REPO/agent"
READ_SKILLS=(smartune-status smartune-diagnose smartune-bench)
CONTROL_SKILL=smartune-control                            # writes: opt out with --no-control
ALL_SKILLS=("${READ_SKILLS[@]}" "$CONTROL_SKILL")
DEB_DIR="/opt/intel/smartune"

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

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Set up the SmarTune CLI, credentials and agent skills for the current user.

Options:
  --target LIST          openclaw, claude, both (openclaw,claude) or none.
                         Default: every agent found (~/.openclaw, ~/.claude).
  --deb                  Use the deb install's key/ ($DEB_DIR). Default when it exists.
  --source               Use the key/ of this checkout ($REPO), for start_smartune.sh.
  --smartune-dir DIR     Use DIR/key/ (an instance installed elsewhere).
  --no-mcp               Do not install the MCP server (smartune-mcp), which is installed by
                         default and registered with OpenClaw (read-only tools) when its
                         'openclaw mcp' command is available. It needs Python >= 3.10,
                         python3-venv and network access to PyPI; without them the script
                         warns and continues without MCP. Rerunning with --no-mcp removes it.
  --mcp                  Require the MCP server: fail instead of skipping it.
  --no-control           Leave out the smartune-control skill, which is installed by default
                         and lets the agent register apps and change limits, priorities,
                         freeze state and core binding -- each change shown as a plan and
                         applied only after the user agrees. Rerunning with --no-control
                         removes it again. Without this option, the printed Claude Code MCP
                         command also enables the write tools, which ask the user in a
                         form (smartune-mcp --allow-control). The token it uses can call
                         every endpoint (no read-only token yet), so install control only
                         for an agent you trust on this machine.
  --control              Install smartune-control (the default; kept for older scripts).
  --skip-credentials     Do not copy the token and certificate.
  --remote USER@HOST     SmarTune runs on another machine: copy the token and certificate
                         from HOST (where this script must have run first, as USER) with
                         scp, and use https://HOST:PORT. The certificate is pinned, so the
                         address need not appear in it. The server must listen on the
                         network (deb default; SMARTUNE_LISTEN in /etc/default/intel-smartune).
  --port PORT            SmarTune port for --remote (default 9001).
  --openclaw-workspace D OpenClaw agent workspace (default: $OPENCLAW_HOME/workspace).
  --uninstall            Remove everything this script installed.
  -h, --help             Show this help.
EOF
}

say()  { printf '==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
shq()  { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }  # single-quote for sh

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)             TARGETS="${2:?--target needs a value}"; shift 2 ;;
        --deb)                SMARTUNE_DIR="$DEB_DIR"; shift ;;
        --source)             SMARTUNE_DIR="$REPO"; shift ;;
        --smartune-dir)       SMARTUNE_DIR="${2:?--smartune-dir needs a value}"; shift 2 ;;
        --mcp)                WITH_MCP=1; MCP_REQUIRED=1; shift ;;
        --no-mcp)             WITH_MCP=0; MCP_REQUIRED=0; shift ;;
        --control)            WITH_CONTROL=1; shift ;;
        --no-control)         WITH_CONTROL=0; shift ;;
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

[[ -d "$SDK_SRC/smartune_sdk" ]] || die "cannot find the SDK at $SDK_SRC (run this script from a SmarTune checkout)"
mkdir -p "$BIN_DIR" "$DATA_DIR"

skip_mcp() {  # skip_mcp REASON: fatal with --mcp, otherwise continue without MCP
    [[ $MCP_REQUIRED -eq 0 ]] || die "--mcp: $1"
    warn "$1; continuing without the MCP server (the CLI and skills work without it). Fix it and rerun to add MCP, or pass --no-mcp to silence this."
    WITH_MCP=0
}

install_venv() {  # install_venv SPEC: the SDK in a private venv; returns pip's status
    say "Installing the CLI$([[ $WITH_MCP -eq 1 ]] && echo ' and the MCP server') into a private venv ($DATA_DIR/venv)"
    "$PYTHON" -m venv --clear "$DATA_DIR/venv" >/dev/null 2>&1 \
        || die "could not create a venv; install it with: sudo apt install python3-venv"
    note "pip install $1 (needs PyPI; set HTTPS_PROXY behind a proxy)"
    # Show what pip fetches, so a blocked network reads as slow progress, not a hang.
    set +e
    "$DATA_DIR/venv/bin/pip" install --progress-bar off --disable-pip-version-check \
        --timeout 20 --retries 2 "$1" 2>&1 \
        | grep -E --line-buffered '^(Successfully|ERROR|WARNING: Retrying)' | sed -u 's/^/    /'
    local status=${PIPESTATUS[0]}
    set -e
    [[ $status -eq 0 ]] || return "$status"
    rm -rf "$DATA_DIR/lib"
    local names=(smartune)
    [[ $WITH_MCP -eq 1 ]] && names+=(smartune-mcp)  # the entry point exists even without the MCP SDK
    for name in "${names[@]}"; do
        claim_launcher "$name"
        ln -s "$DATA_DIR/venv/bin/$name" "$BIN_DIR/$name"
        note "$BIN_DIR/$name -> $DATA_DIR/venv/bin/$name"
    done
}

if [[ $WITH_MCP -eq 1 ]] && ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    skip_mcp "the MCP server needs Python >= 3.10 (MCP SDK 2.x); this is $("$PYTHON" -V 2>&1)"
fi
if [[ $WITH_MCP -eq 1 ]] && ! install_venv "$SDK_SRC[mcp]"; then
    skip_mcp "pip could not install the MCP SDK (it needs network access to PyPI; check HTTPS_PROXY)"
fi
if [[ $WITH_MCP -eq 1 ]]; then
    :  # installed above
elif ! "$PYTHON" -I -c 'import requests' 2>/dev/null; then
    # The system Python has no requests: a private venv with just the SDK.
    install_venv "$SDK_SRC" || die "pip install failed (it needs network access to PyPI; check HTTPS_PROXY)"
else
    # The system Python already has requests, the only dependency: copy the
    # package and write a launcher. No pip, no network.
    say "Installing the CLI ($DATA_DIR/lib, using $PYTHON)"
    rm -rf "$DATA_DIR/lib" "$DATA_DIR/venv"
    mkdir -p "$DATA_DIR/lib"
    cp -r "$SDK_SRC/smartune_sdk" "$DATA_DIR/lib/"
    find "$DATA_DIR/lib" -name __pycache__ -type d -prune -exec rm -rf {} +
    claim_launcher smartune
    # -I: ignore PYTHON* variables, user site-packages and the current directory,
    # so nothing in the agent's working directory can shadow the SDK's imports.
    cat > "$BIN_DIR/smartune" <<EOF
#!/bin/sh
$MARKER: launcher written by skills/install.sh; remove with skills/install.sh --uninstall
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
    note "They come from ~/.config/smartune/ on $remote_host, put there by skills/install.sh on that machine."
    mkdir -p "$CONFIG_DIR"
    chmod 700 "$CONFIG_DIR"
    tmp_keys="$(mktemp -d)"
    if ! scp -q "$REMOTE:.config/smartune/api_token" "$REMOTE:.config/smartune/b_server.crt" "$tmp_keys/"; then
        rm -rf "$tmp_keys"
        die "could not copy them from $REMOTE. Run skills/install.sh on $remote_host first (as that user), and check that \`ssh $REMOTE\` works"
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
    if [[ -z "$SMARTUNE_DIR" ]]; then
        if [[ -d "$DEB_DIR" ]]; then SMARTUNE_DIR="$DEB_DIR"; else SMARTUNE_DIR="$REPO"; fi
    fi
    KEY_DIR="$SMARTUNE_DIR/key"
    case "$SMARTUNE_DIR" in
        "$DEB_DIR") kind="deb install" ;;
        "$REPO")    kind="source checkout" ;;
        *)          kind="instance at $SMARTUNE_DIR" ;;
    esac
    say "Copying the API token and certificate from $KEY_DIR ($kind)"

    exists() { [[ -e "$1" ]] || sudo test -e "$1"; }
    if ! exists "$KEY_DIR/api_token" || ! exists "$KEY_DIR/b_server.crt"; then
        die "$KEY_DIR/api_token or b_server.crt is missing. Start SmarTune once first (it creates them), or pick the right instance with --deb / --source / --smartune-dir"
    fi
    mkdir -p "$CONFIG_DIR"
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
cp -r "$AGENT_SRC" "$DATA_DIR/agent"

# -- 4. skills -------------------------------------------------------------------------

install_skills_into() {  # install_skills_into DIR: returns 0 when it removed the control skill
    mkdir -p "$1"
    for s in "${SKILLS[@]}"; do
        rm -rf "${1:?}/$s"
        cp -r "$HERE/$s" "$1/$s"
    done
    note "copied ${SKILLS[*]} to $1"
    if [[ $WITH_CONTROL -eq 0 && -d "$1/$CONTROL_SKILL" ]]; then
        rm -rf "${1:?}/$CONTROL_SKILL"
        note "removed $1/$CONTROL_SKILL (rerun without --no-control to install it again)"
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
