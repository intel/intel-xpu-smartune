# Agent integration: deployment guide

[← Back to contents](../README.md)

This page sets up SmarTune for an AI agent (OpenClaw or Claude Code) on a machine that already
runs SmarTune, so the agent can answer questions such as "who is using the GPU?" or "why was
ollama limited?" and, if you allow it, change limits after the user confirms each change.

Everything is done by one script, [`skills/install.sh`](../../skills/install.sh). For what the
skills can do once installed, see [`skills/README.md`](../../skills/README.md); for the CLI and
MCP server in detail, see [`sdk/README.md`](../../sdk/README.md).

> [!NOTE]
> The deb package does not include the skills or the CLI yet, and there is no
> `smartune agent install-skills` command. Use `skills/install.sh` from a SmarTune checkout as
> described here. For other features agents do not have yet, see
> [Not yet supported](../../skills/README.md#not-yet-supported).

## 1. What gets installed, and where

The script works for the user who runs it. Nothing is installed system-wide and the SmarTune
service itself is not touched.

| What | Copied from (repository) | Installed to |
|---|---|---|
| `smartune` CLI and MCP server `smartune-mcp` (left out with `--no-mcp`) | `sdk/` | `~/.local/bin/` (launchers), `~/.local/share/smartune/venv/` (or `.../lib/` without MCP) |
| API token | `<instance>/key/api_token` (root-only) | `~/.config/smartune/api_token` (mode 600) |
| TLS certificate | `<instance>/key/b_server.crt` | `~/.config/smartune/b_server.crt` (mode 644) |
| Shared agent rules and references | `agent/` | `~/.local/share/smartune/agent/` |
| Read-only skills: `smartune-status`, `smartune-diagnose`, `smartune-bench` | `skills/<name>/` | OpenClaw: `~/.openclaw/workspace/skills/<name>/`<br>Claude Code: `~/.claude/skills/<name>/` |
| `smartune-control` skill (left out with `--no-control`) | `skills/smartune-control/` | same as above |
| OpenClaw settings | — | `~/.openclaw/openclaw.json` (`skills.entries`, and `mcp.servers.smartune` when OpenClaw has the `openclaw mcp` command; backup `openclaw.json.bak-smartune`) and `~/.openclaw/.env` (`SMARTUNE_*` lines) |

`~/.config` is `$XDG_CONFIG_HOME` when that is set, and `~/.openclaw` is `$OPENCLAW_HOME`.

### Where the token and certificate come from

The SmarTune service creates `api_token`, `b_server.crt` and `b_server.key` in its `key/`
directory the first time it starts. Which `key/` the script reads depends on how SmarTune runs:

| SmarTune runs as | `key/` directory | Script option |
|---|---|---|
| Debian package | `/opt/intel/smartune/key/` | `--deb` (default when `/opt/intel/smartune` exists) |
| Source checkout (`start_smartune.sh`) | `<checkout>/key/` | `--source` (default when there is no deb install) |
| Anywhere else | `<DIR>/key/` | `--smartune-dir DIR` |
| Another machine | that machine's `~/.config/smartune/` | `--remote user@host` (see [section 6](#6-agent-on-another-machine)) |

Only root can read `api_token`, so copying it is the one step that calls `sudo`
(`b_server.crt` is world-readable). The private key
`b_server.key` is never copied. If the service later regenerates the files (for example after
the certificate expires), run the script again.

> [!WARNING]
> SmarTune has a single API token, and it can call every endpoint, including the write ones.
> The CLI only sends read requests (plus `smartune control` changes after the user confirms), but
> that limit is enforced on the client. Treat the copied token as an admin credential and install
> the integration only for agents you trust on this machine.

## 2. Before you start

- **SmarTune is running.** Deb: start it from the application menu or with
  `sudo systemctl start intel-smartune.service` (it does not start on boot by default). Source:
  `bash start_smartune.sh`. The dashboard does not need to be open.
- **A checkout of this repository on the machine.** The skills, `agent/` and the SDK are not in
  the deb package yet, so the script must run from a checkout. A deb install plus a checkout of
  the same version is fine.
- **Python 3.8 or later.** The MCP server, installed by default, also needs Python 3.10 or later,
  `python3-venv` and access to PyPI (set `HTTPS_PROXY` behind a proxy). Without them the script
  warns, skips MCP and installs everything else; the skills work without MCP.
- **The agent is installed for the same user.** OpenClaw: run `openclaw onboard` once so that
  `~/.openclaw/openclaw.json` exists. Claude Code: `~/.claude` exists.
- **Run as the user the agent runs as, not as root.** The script refuses to run as root and asks
  for your `sudo` password only to copy the key files.

## 3. Install

From the repository root (the script finds its own files, so any working directory works):

```bash
skills/install.sh --target openclaw            # OpenClaw only
skills/install.sh --target claude              # Claude Code only
skills/install.sh                              # every agent found (~/.openclaw, ~/.claude)
```

Add options as needed:

| Option | Effect |
|---|---|
| `--target openclaw\|claude\|both\|none` | Which agents get the skills. Default: every agent found |
| `--deb` / `--source` / `--smartune-dir DIR` | Which SmarTune instance to take the key files from ([section 1](#where-the-token-and-certificate-come-from)) |
| `--no-control` | Leave out `smartune-control`. It is installed by default and lets the agent register apps, limit, restore, freeze, set priority and core binding, each only after the user confirms a plan. Rerunning **with** `--no-control` removes it |
| `--no-mcp` | Do not install the MCP server. By default `smartune-mcp` is installed, registered with OpenClaw (read-only, see [section 4](#openclaw)) and, for Claude Code, the command that registers it is printed. Rerunning **with** `--no-mcp` removes it |
| `--mcp` | Require the MCP server: stop with an error instead of skipping it when Python or PyPI is missing |
| `--openclaw-workspace DIR` | OpenClaw workspace, when it is not `~/.openclaw/workspace` |
| `--skip-credentials` | Keep the key files already in `~/.config/smartune/` |
| `--remote USER@HOST [--port N]` | SmarTune runs on another machine ([section 6](#6-agent-on-another-machine)) |
| `--uninstall` | Remove everything the script installed ([section 7](#7-update-and-uninstall)) |

A typical OpenClaw install, with all four skills:

```bash
skills/install.sh --target openclaw
```

For an agent that should only read, add `--no-control`.

Run it as your normal user, **without** `sudo`. This one command does every step; there is no
separate step for the key files:

| Step | What the script does | Needs `sudo`? |
|---|---|---|
| 1. CLI and MCP | Installs `smartune` and `smartune-mcp` into `~/.local/bin` (MCP skipped with a warning if Python < 3.10 or PyPI is unreachable) | No |
| 2. Key files | Copies `api_token` and `b_server.crt` from the instance's `key/` directory to `~/.config/smartune/` | **Yes, for `api_token` only**: the script calls `sudo` itself and asks for your password once. `b_server.crt` is world-readable and is copied without it |
| 3. Agent rules | Copies `agent/` to `~/.local/share/smartune/agent/` | No |
| 4. Skills | Copies the skills into the agent's skill directory and, for OpenClaw, updates `openclaw.json` (skills and MCP server) and `.env` | No |
| 5. Check | Runs `smartune doctor` | No |

You therefore need `sudo` rights, but you do not prefix the command with `sudo`; the script
refuses to run as root. If the key files are already in place (or you copy them yourself, see
[section 5](#5-install-skills-from-another-directory-or-by-hand)), add `--skip-credentials` and
the script does not call `sudo` at all.

The script prints each step and ends with a `smartune doctor` report:

```text
==> Installing the CLI and the MCP server into a private venv (~/.local/share/smartune/venv)
    pip install .../sdk[mcp] (needs PyPI; set HTTPS_PROXY behind a proxy)
    Successfully installed ...
    ~/.local/bin/smartune -> ~/.local/share/smartune/venv/bin/smartune
    ~/.local/bin/smartune-mcp -> ~/.local/share/smartune/venv/bin/smartune-mcp
==> Copying the API token and certificate from /opt/intel/smartune/key (deb install)
==> Installing agent rules and references (~/.local/share/smartune/agent)
==> Installing the skills for OpenClaw
    copied smartune-status smartune-diagnose smartune-bench smartune-control to ~/.openclaw/workspace/skills
    enabled smartune-status, ... in ~/.openclaw/openclaw.json (backup: ~/.openclaw/openclaw.json.bak-smartune)
    wrote SMARTUNE_TOKEN_FILE, SMARTUNE_CACERT, SMARTUNE_AGENT_ID in ~/.openclaw/.env
    registered the smartune MCP server (mcp.servers.smartune, read-only) in ~/.openclaw/openclaw.json (...)
==> Checking the connection (smartune doctor)
    ok    config     ...
    ok    connect    127.0.0.1:9001 accepts connections
    ...
==> Done.
```

If a check fails, fix it first ([section 8](#8-troubleshooting)) and run `smartune doctor` again.

## 4. Finish the setup in the agent

### OpenClaw

1. Restart the gateway so it loads the skills, the MCP server and the new `.env`:

   ```bash
   openclaw gateway restart
   ```

2. Make sure `~/.local/bin` is on the gateway's `PATH`; the script warns when it is not on yours.
3. Ask a question, for example "Who is using the GPU right now?" or "Any alerts today?".

If `openclaw.json` is not plain JSON (comments, JSON5), the script leaves it alone and prints the
snippet to merge by hand:

```json
{
  "skills": {
    "entries": {
      "smartune-status": { "enabled": true },
      "smartune-diagnose": { "enabled": true },
      "smartune-bench": { "enabled": true }
    }
  }
}
```

**MCP server.** When OpenClaw has the `openclaw mcp` command (versions with native MCP
support), the script adds this entry to `openclaw.json`; with an older OpenClaw it prints it
instead, and the skills keep working through the CLI:

```json
{ "mcp": { "servers": { "smartune": {
  "command": "/home/<user>/.local/bin/smartune-mcp", "args": [] } } } }
```

The entry serves only the read-only tools (no `--allow-control`): whether OpenClaw shows MCP
confirmation forms to the user has not been tested yet. Changes still work: the
`smartune-control` skill then uses the CLI and asks for confirmation in the conversation. The
MCP server finds the token and certificate in `~/.config/smartune/` by itself. An existing
`mcp.servers.smartune` entry that points elsewhere is left alone.

With a weaker local model that does not pick the skills up on its own, add this line to the
Tools section of `~/.openclaw/workspace/AGENTS.md`: "For questions about this machine's resource
usage, load, alerts, why an app was limited or model benchmark results, you must use the
smartune-status / smartune-diagnose / smartune-bench skills."

### Claude Code

The skills are picked up by new sessions. The script does not change Claude Code's MCP settings;
it prints the command that registers the MCP server (unless `--no-mcp`), for example:

```bash
claude mcp add smartune --scope user --transport stdio --env SMARTUNE_AGENT_ID=claude-code -- ~/.local/bin/smartune-mcp
```

It adds `--allow-control` unless you passed `--no-control`. That turns on the MCP write tools,
which ask the user to approve each change in a form shown by the client.

> [!IMPORTANT]
> The Claude Code **VS Code extension** (verified with 2.1.325) runs Claude Code in print mode,
> where MCP forms are not shown and are answered `decline` automatically, within about 0.2 s, without
> the user seeing them. Every MCP write tool then reports "the user did not approve it". In the
> VS Code extension, register `smartune-mcp` **without** `--allow-control`: the
> `smartune-control` skill then uses the CLI instead, showing the plan in the conversation and
> applying it after the user agrees. Use `--allow-control` only in clients that really show the
> form, such as Claude Code in a terminal.

## 5. Install skills from another directory, or by hand

The script always copies the skills that sit next to it (`skills/` of the same checkout) and
`agent/` from the same checkout. To deploy skills from a different directory:

- **A different checkout or a modified copy:** run the `install.sh` inside that copy. It installs
  that copy's `skills/`, `agent/` and `sdk/`.
- **A different destination:** use `--openclaw-workspace DIR` for OpenClaw, or set
  `OPENCLAW_HOME`. Claude Code always uses `~/.claude/skills/`.
- **By hand** (for example to put the skills in a project's `.claude/skills/`): install the CLI
  and key files with the script first, using `--target none`, then copy the skill directories:

  ```bash
  skills/install.sh --target none                 # CLI, key files, agent/, doctor; no skills
  mkdir -p <dest>/skills
  cp -r skills/smartune-status skills/smartune-diagnose skills/smartune-bench <dest>/skills/
  cp -r skills/smartune-control <dest>/skills/    # only if the agent may make changes
  ```

  Each `SKILL.md` carries a summary of the shared rules, so the skill directories alone are
  enough; the full rules in `~/.local/share/smartune/agent/` are used when present. For OpenClaw,
  also enable the entries in `openclaw.json` and add the `.env` lines yourself:

  ```bash
  SMARTUNE_TOKEN_FILE=/home/<user>/.config/smartune/api_token
  SMARTUNE_CACERT=/home/<user>/.config/smartune/b_server.crt
  SMARTUNE_AGENT_ID=openclaw
  ```

**Copying the key files by hand.** You only need this when you do not let the script do it
(for example with `--skip-credentials`, or without the script at all); `skills/install.sh`
otherwise does exactly this in its step 2:

```bash
mkdir -p ~/.config/smartune && chmod 700 ~/.config/smartune
sudo install -m 600 -o "$USER" -g "$(id -gn)" /opt/intel/smartune/key/api_token ~/.config/smartune/api_token
install -m 644 /opt/intel/smartune/key/b_server.crt ~/.config/smartune/b_server.crt
```

Only `api_token` needs `sudo`; the certificate is world-readable. For a source run, replace
`/opt/intel/smartune` with the checkout directory.

## 6. Agent on another machine

1. On the SmarTune machine, make sure the service listens on the network. That is the deb default
   (`SMARTUNE_LISTEN=0.0.0.0` in `/etc/default/intel-smartune`); for a source run, start it with
   `BALANCER_HOST=0.0.0.0`. Then run `skills/install.sh` there once, as the user you will connect
   as.
2. On the agent machine, run:

   ```bash
   skills/install.sh --remote user@smartune-host            # --port N if not 9001
   ```

   It copies the token and certificate from that user's `~/.config/smartune/` with `scp`, saves
   `https://smartune-host:9001` to `~/.config/smartune/url`, installs the CLI and skills and runs
   `smartune doctor`.

The server's certificate names only `localhost` and `127.0.0.1`. The CLI pins that exact
certificate (by SHA-256 fingerprint) and skips only the hostname comparison, so any address
works and verification stays on. Remote access puts the admin token on the agent machine; use it
only between trusted machines. To keep the token on the SmarTune machine, run the MCP server over
SSH instead:

```bash
claude mcp add smartune --transport stdio -- ssh -T user@smartune-host /home/user/.local/bin/smartune-mcp
```

## 7. Update and uninstall

- **Update** (new checkout, new skills, regenerated key files): run the same command again. It
  replaces everything in place.
- **Remove only the control skill:** rerun with `--no-control`.
- **Remove only the MCP server:** rerun with `--no-mcp` (also removes the OpenClaw entry).
- **Uninstall:**

  ```bash
  skills/install.sh --uninstall
  openclaw gateway restart        # OpenClaw only
  ```

  This removes the launchers, `~/.local/share/smartune/`, the key files and URL in
  `~/.config/smartune/`, the four skills from OpenClaw and Claude Code, and for OpenClaw the
  `skills.entries`, the `mcp.servers.smartune` entry and the `SMARTUNE_*` lines it added. It does
  not unregister an MCP server you added with
  `claude mcp add` (`claude mcp remove smartune`), and does not touch the SmarTune service.

## 8. Troubleshooting

Run `smartune doctor` first. It checks configuration, connection, TLS, token, version, features
and telemetry in order, and prints a hint for the first problem.

| Symptom | Cause and fix |
|---|---|
| `key/api_token or b_server.crt is missing` | SmarTune has not started yet with this `key/` directory. Start it once, or choose the right instance with `--deb`, `--source` or `--smartune-dir` |
| `doctor`: `connect` fails (exit code 3) | The service is not running, or listens on another address or port. Start it; for remote use check `SMARTUNE_LISTEN` / `BALANCER_HOST` and the firewall |
| `doctor`: `connect` and `tls` pass but `auth` says "Cannot connect" | An HTTP(S) proxy in the environment (`HTTPS_PROXY`, `ALL_PROXY`) without `127.0.0.1` in `NO_PROXY`. SDKs before this fix sent local requests through the proxy; update with `skills/install.sh`, or add `127.0.0.1,localhost` to `NO_PROXY` and `no_proxy`. For a remote SmarTune, add its host to `NO_PROXY` |
| `doctor`: `tls` fails | The certificate was regenerated after you copied it. Run the script again |
| `doctor`: `auth` fails (exit code 4) | The token was regenerated, or the wrong instance's token was copied. Run the script again with the right `--deb` / `--source` |
| `~/.local/bin is not on PATH` | Add it in `~/.profile`, and to the OpenClaw gateway's environment |
| `... exists and was not installed by this script` | Another `smartune` is already in `~/.local/bin` (for example from pipx). Remove it (`pipx uninstall smartune-sdk`) and run again |
| `WARNING: pip could not install the MCP SDK ... continuing without the MCP server` | No access to PyPI. Everything else is installed; the skills work through the CLI. Set `HTTPS_PROXY` and rerun to add MCP, or pass `--no-mcp` to stop the warning |
| `WARNING: the MCP server needs Python >= 3.10` | The system Python is older. Same as above: MCP is skipped, the rest works |
| `pip install failed` | No access to PyPI and the system Python has no `requests`, so even the CLI cannot be installed. Set `HTTPS_PROXY`, or `sudo apt install python3-requests` and rerun |
| OpenClaw does not list the smartune MCP tools | The script printed "MCP not registered with OpenClaw" (no `openclaw mcp` command): update OpenClaw or merge the printed entry by hand, then restart the gateway |
| OpenClaw does not use the skills | Restart the gateway; check `skills.entries` in `openclaw.json`; for a weak model add the `AGENTS.md` line from [section 4](#openclaw) |
| MCP write tools always return `"user_response": "decline"` | The client answered the form without showing it (see the note in [section 4](#claude-code)). Register `smartune-mcp` without `--allow-control` in that client |
