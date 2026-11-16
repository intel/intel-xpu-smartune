# SmarTune SDK and CLI

`smartune` is a command line client for the SmarTune REST API, and the backend for the agent
skills in [`../skills`](../skills) and the MCP server (`smartune-mcp`). Its output is
trimmed for an LLM to read: each command prints one compact JSON document, at most 16 KB by
default. Every command is read-only except `smartune control`, which changes how the balancer
treats an app and only prints a plan unless given `--yes`.

The [API exposure table](../docs/reference/agent-api-exposure.md) lists which endpoints the CLI
uses and why the others are left out; [`../skills/README.md`](../skills/README.md#not-yet-supported)
lists what agents cannot do yet.

## Install

The quickest way is the one-step script, run as the user the agent runs as (not root). It works
with both a deb install and a source checkout started with `start_smartune.sh`:

```bash
skills/install.sh                  # CLI + MCP server + credentials + skills for every agent found
skills/install.sh --no-mcp         # without the MCP server (by default skipped with a warning
                                   # when Python < 3.10 or PyPI is unreachable; --mcp makes it required)
skills/install.sh --no-control     # without the smartune-control skill (installed by default)
skills/install.sh --uninstall      # remove all of it again
```

It installs the `smartune` and `smartune-mcp` commands into `~/.local/bin` and copies the
instance's `key/api_token` and `key/b_server.crt` to `~/.config/smartune/` (this step uses `sudo`
for `api_token`, which only root can read). It installs `agent/` into
`~/.local/share/smartune/agent`, and the skills into OpenClaw and/or Claude Code. For OpenClaw it
also registers the MCP server, read-only, under `mcp.servers` in `openclaw.json` when OpenClaw has
the `openclaw mcp` command; for Claude Code it prints the `claude mcp add` command. At the end it
runs `smartune doctor`. Run `skills/install.sh --help` for every option.

With MCP (the default) the SDK goes into a private venv under `~/.local/share/smartune/venv`.
With `--no-mcp`, or when MCP was skipped, and if the system Python already has `requests` (the
only dependency), the CLI is installed by copying the package and writing a small launcher: no
pip and no network.

To install by hand instead:

```bash
pipx install ./sdk                  # or './sdk[mcp]' for the MCP server (Python >= 3.10)
smartune doctor
```

To run from a checkout without installing it, use `python3 -m smartune_sdk` from this directory.

## Configure

| Variable | Default | Meaning |
|---|---|---|
| `SMARTUNE_URL` | `~/.config/smartune/url` if it exists (written by `skills/install.sh --remote`), else `https://127.0.0.1:9001` | Server address |
| `SMARTUNE_TOKEN` | — | API token (the server's `key/api_token`) |
| `SMARTUNE_TOKEN_FILE` | `~/.config/smartune/api_token` if it exists | Alternative to `SMARTUNE_TOKEN`: a file containing the token |
| `SMARTUNE_CACERT` | `~/.config/smartune/b_server.crt` if it exists, else the system CA store | Server certificate: `key/b_server.crt` of the running instance |
| `SMARTUNE_PIN_CERT` | pin when `SMARTUNE_CACERT` is one self-signed certificate | `0` keeps normal CA and hostname verification instead (see [Certificate pinning](#use-from-another-machine)) |
| `SMARTUNE_TIMEOUT` | `20` | Request timeout in seconds |
| `SMARTUNE_AGENT_ID` | `smartune-cli` | Sent as `X-Agent-Id`, so the caller can be identified |

`~/.config/smartune` means `$XDG_CONFIG_HOME/smartune` when that is set. With the two files in
place, which `skills/install.sh` does, no variable is needed.

The token and certificate are created by the server under its `key/` directory, which only root
can read: `/opt/intel/smartune/key/` for the deb, `<checkout>/key/` for a source run. To copy them
by hand:

```bash
mkdir -p ~/.config/smartune
sudo install -m 600 -o "$USER" /opt/intel/smartune/key/api_token ~/.config/smartune/api_token
install -m 644 /opt/intel/smartune/key/b_server.crt ~/.config/smartune/b_server.crt   # world-readable
```

Copy them again if the server regenerates them; `smartune doctor` then reports an untrusted
certificate or a rejected token.

> [!WARNING]
> The CLI only sends read requests, plus the `smartune control` actions after `--yes`, but this is
> enforced on the client side. The server currently has a single token, and that token also allows
> every write endpoint. Treat it as an admin credential until the server has scoped tokens.

## Commands

```text
smartune doctor                            check config, server, TLS, token, version, features, telemetry
smartune caps                              features this instance serves
smartune status [--sections gpu,npu]       live CPU / memory / pressure / GPU / NPU / disk
smartune top [--by cpu|mem|gpu|npu] [--io] top resource consumers
smartune ps [--grep NAME] [--sort mem]     processes
smartune proc PID                          one process
smartune history [--since 6h]              trend: min / avg / max plus a downsampled series
smartune info [SECTION]                    platform, drivers, firmware
smartune apps | app APP_ID | limits | pending      controlled apps and their limit state
smartune diag alerts | events | lifecycles | logs | digest | context | why APP_ID | boots | retention | catalog
smartune bench status | run RUN_ID [--log] | wait [RUN_ID] [--timeout S] | results [--latest --model --device --job] | models
smartune control profile APP_ID | exclusions                       limit ranges, auto-limit exclusions (read-only)
smartune control limit APP_ID [--cpu PCT|--no-cpu] [--mem PCT|--no-mem] [--disk-read MB --disk-write MB
                 --disk-read-iops N --disk-write-iops N|--no-disk] [--cgroup ID]... [--yes]
smartune control restore | lock | freeze | thaw | cancel-launch APP_ID [--yes]
smartune control priority APP_ID low|medium|high|critical [--yes]
smartune control net-priority APP_ID low|high|critical|system [--yes]
smartune control affinity APP_ID p|e|lpe|custom --cpus LIST|restore [--yes]
smartune control exclusion-remove KEY [--yes]
smartune control register NAME [--priority P] [--pid PID]... [--keyword TEXT]... [--id ID] [--yes]
smartune control unregister APP_ID [--yes]                         only apps `register` added
smartune control freq-status [--kind cpu|gpu]                      CPU / GPU frequency ranges (read-only)
smartune control freq-limit UNIT... [--min-mhz N] [--max-mhz N] [--yes]   whole device, every app on it
smartune control freq-restore [UNIT...] [--yes]                    default: every unit SmarTune limits
```

`smartune doctor` runs each check in order and reports `ok`, `warn`, `fail` or `skip`, with one hint
for each problem. A failed check skips the checks that depend on it. It recognises a missing or
untrusted certificate, a missing or rejected token, and a service that is not running. The exit
code is the one of the first failed check (3 = connection or TLS, 4 = token), or 0. The TCP and TLS
checks use a plain socket handshake, so `doctor` never sends the unauthenticated request that would
log an `AUTH_ACCESS_DENIED` event on the server.

`smartune bench wait` polls a run (by default the run in progress) until it is `done`, `failed` or
`cancelled`, or until `--timeout` passes (default 300 s). In that case it returns
`"finished": false`. For a run that did not succeed, it attaches the end of the log.

Starting and cancelling benchmark runs are write operations and are not offered yet.

`smartune control` changes go in two steps. Without `--yes` the command changes nothing and prints a
plan built from the server's live state: `current` and `target` values, `effects`, `blockers` or
`no_change` when it cannot or need not be applied, and how to undo it (`undo_with`, `undo_note`).
Values are checked against the bounds the server reports for the app's priority
(`/app/resource_limit_profile`). The plan's `apply_with` is the same change as one fully explicit
command with `--yes` and `--expect <control_status>`: it re-checks the plan, refuses with exit
code 7 (before any write is sent) if the app changed state in between, applies it, and returns
`before`, `after` and the server's `message`. Only the endpoints in `CONTROL_ROUTES`
(`client.py`) are reachable this way; agents follow
[`../agent/policy/consent.md`](../agent/policy/consent.md) and never add `--yes` on their own.

`freq-limit` and `freq-restore` act on a whole device rather than an app. A unit is one cpufreq
policy (`cpu:policy0`) or one GPU gt (`gpu:card0/gt0`). `freq-limit` also takes the groups `cpu`,
`gpu`, `p`, `e`, `lpe` or a GPU card (`card0`). The plan expands them into unit ids, keeps each
unit's current value for a side left out (units that then need different pairs get one request
each), and shows the range each unit gets after the server clamps it to that unit's hardware range. `freq-restore` without units plans every unit
SmarTune limits, and its `apply_with` lists them explicitly. SmarTune restores every unit when the
service stops; nothing is saved.

Global options: `--pretty` indents the JSON, and `--max-bytes N` changes the size cap. Where a
command supports `--raw`, it returns the API payload without summarizing it.

If a result is cut to fit the size cap, the output includes a `_truncated` field listing the
shortened lists and their original lengths.

Timestamps are printed in the server's local time with its UTC offset, for example
`2026-10-29 23:14:05 +0800`. The API mixes epoch seconds, UTC ISO strings and naive local strings;
the CLI rewrites them all into this one form (`--raw` output is left as the API sent it). What each
field means is described in
[`../agent/content/metrics.md`](../agent/content/metrics.md).

Exit codes: `0` ok, `2` usage error, `3` connection or TLS error, `4` authentication error,
`5` API error, `6` feature not served by this instance, `7` a `smartune control` change refused
before it was sent (blockers, or the app's state changed since the plan). On failure, stdout contains
`{"error": ..., "hint": ...}`.

## Use from OpenClaw

Setting up OpenClaw (skills, `.env`, `openclaw.json` entries, the MCP server entry) is covered in
the deployment guide:
[Finish the setup in the agent](../docs/user-guide/agent-integration.md#4-finish-the-setup-in-the-agent),
and [by hand](../docs/user-guide/agent-integration.md#5-install-skills-from-another-directory-or-by-hand)
without `skills/install.sh`.

## Use from an MCP client

`smartune-mcp` serves every read-only CLI command as an MCP tool: 30 tools, each marked
`readOnlyHint`. Started with `--allow-control`, it also serves the 14 `smartune control` changes
as write tools (`limit_app`, `restore_app`, `register_app`, `limit_device_frequency`, ...),
marked `destructiveHint`. A write
tool takes no `--yes` from the model: it plans the change, asks the user to approve it through an
MCP elicitation form (for `limit_app`, prefilled with the app's defaults and allowed ranges, which
the user can edit), and only on "accept" runs the plan's `apply_with`. Clients without form support
get a refusal. The plan and the answer are injected by the SDK's resolvers, so the flow works on the
handshake protocol (mid-call `elicitation/create`) and on 2026-07-28 (`InputRequiredResult` round
trips, with the answers sealed in `request_state`). Enable it only for clients that show the form to
a person. Each tool runs the CLI command through the same code path, so a tool and its
command return the same JSON and hit the same allow-list and size cap. The server instructions are
put together from [`../agent/policy`](../agent/policy). The files in
[`../agent/content`](../agent/content) are served as resources: `smartune://metrics`,
`smartune://methodology` and `smartune://workloads`. If `agent/` is not found (checked in this
order: `SMARTUNE_AGENT_DIR`, the repository checkout, `~/.local/share/smartune/agent` (where
`skills/install.sh` puts it), `/usr/share/smartune/agent`, `/opt/intel/smartune/agent`), a short built-in summary of the rules is used.

Claude Code:

```bash
claude mcp add smartune --scope user --transport stdio \
  --env SMARTUNE_TOKEN_FILE=$HOME/.config/smartune/api_token \
  --env SMARTUNE_CACERT=$HOME/.config/smartune/b_server.crt \
  --env SMARTUNE_AGENT_ID=claude-code \
  -- smartune-mcp                      # add --allow-control for the write tools
```

The server name must come before the `--env` options: `--env` takes several values and would
otherwise take the name as one of them.

Cursor / VS Code (`mcp.json`):

```json
{
  "servers": {
    "smartune": {
      "command": "smartune-mcp",
      "env": {
        "SMARTUNE_TOKEN_FILE": "/home/<user>/.config/smartune/api_token",
        "SMARTUNE_CACERT": "/home/<user>/.config/smartune/b_server.crt",
        "SMARTUNE_AGENT_ID": "vscode"
      }
    }
  }
}
```

The default transport is stdio. `smartune-mcp --http [--port 9011]` serves streamable HTTP so several
clients can share one server. It listens on 127.0.0.1 only, because anything that can reach the
port can query the machine with the configured token.

## Use from another machine

The agent (OpenClaw, Claude Code, an MCP client) can run on a different machine from SmarTune.
The setup steps (`skills/install.sh --remote user@smartune-host`) are in the deployment guide:
[Agent on another machine](../docs/user-guide/agent-integration.md#6-agent-on-another-machine).
What the client does on its side:

**Certificate pinning.** The server's self-signed certificate names only `localhost` and
`127.0.0.1`, so a normal hostname check fails for any other address. When `SMARTUNE_CACERT` (or
`~/.config/smartune/b_server.crt`) is exactly one self-signed certificate, the client pins it: the
server must present that very certificate (SHA-256 fingerprint), and only the hostname comparison
is skipped. This is what a browser's "trust this certificate" exception does, and it is stricter
than CA verification: even a certificate signed with the server's key is refused if it is not this
one. A CA bundle keeps normal CA and hostname verification, and so does `SMARTUNE_PIN_CERT=0`.
Certificate verification is never turned off.

> [!WARNING]
> Remote access puts the server's single token, which also allows every write endpoint, on the
> agent machine and sends it over the network (encrypted). Use it only between trusted machines
> until the server has a read-only token. As an alternative that keeps the token on the SmarTune
> machine, run the MCP server over SSH:
> `claude mcp add smartune --transport stdio -- ssh -T user@smartune-host /home/user/.local/bin/smartune-mcp`.

## Tests

```bash
cd sdk && python3 -m pytest -q tests
```

The tests are offline. A fake HTTP session stands in for the server.

`tests/test_mcp.py` checks that every CLI command has an MCP tool (or is listed in
`EXCLUDED_COMMANDS`) and that every tool builds a command line the CLI accepts. Its server test
runs only where `mcp` 2.x is installed.

`tests/test_parity.py` also checks the SDK against the rest of the repository: every
`READ_ROUTES` entry must match a route the server defines, the allow-list may reach no write
endpoint, and every `smartune ...` command quoted in `skills/` or `agent/` must parse. These
checks read the server source without importing it, and are skipped outside a repository checkout.
