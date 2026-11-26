# SmarTune agent skills

Four skills let an AI agent (Claude Code, OpenClaw, ...) read and act on a SmarTune machine. All of
them go through the `smartune` CLI ([`../sdk`](../sdk)); the same commands are also served by the
MCP server `smartune-mcp`.

| Skill | What it covers | Changes the machine? | Example questions |
|---|---|---|---|
| [`smartune-status`](smartune-status/SKILL.md) | Live and historical resource use, top consumers, platform info, controlled apps | No | "Who is using the GPU?", "Is the system under pressure?" |
| [`smartune-diagnose`](smartune-diagnose/SKILL.md) | Alerts, events, limit lifecycles, logs, digests, root-cause evidence | No | "Why was ollama limited last night?", "Any alerts today?" |
| [`smartune-bench`](smartune-bench/SKILL.md) | Benchmark run status and results (TTFT, TPOT, throughput, perf/W) | No | "Is the benchmark done?", "GPU or NPU for Qwen3 8B?" |
| [`smartune-control`](smartune-control/SKILL.md) | Register, limit, restore, freeze, prioritize and pin apps; device CPU / GPU frequency | Yes, after the user confirms a plan | "Limit ollama to 30% CPU", "Put X on the E cores" |

Related documents:

- Deployment (install, agent setup, remote use, update, troubleshooting):
  [`../docs/user-guide/agent-integration.md`](../docs/user-guide/agent-integration.md)
- CLI and MCP server reference: [`../sdk/README.md`](../sdk/README.md)
- REST endpoints agents can reach: [`../docs/reference/agent-api-exposure.md`](../docs/reference/agent-api-exposure.md)

## Install

Start SmarTune once first: the installer copies the API token and TLS certificate it creates.
Run the installer as the user the agent runs as, **not** with `sudo`; it calls `sudo` itself once,
to copy the root-only token. It installs into that user's home (`~/.local/bin`,
`~/.config/smartune`, `~/.claude/skills`, `~/.openclaw/workspace/skills`), then runs
`smartune doctor`. Running it again updates everything in place.

### Deb package

```bash
smartune-agent-install
```

- The package ships the skills, the agent rules, the SDK and its wheels under
  `/opt/intel/smartune/`. The CLI and MCP server are installed from those wheels, with no network
  needed.
- The key files come from `/opt/intel/smartune/key/`.
- The monitor-only package (`smartune-monitor`) has no balancer and ships without
  `smartune-control`.
- After upgrading the package, run `smartune-agent-install` again. Before removing the package,
  run `smartune-agent-install --uninstall`: the skills live in each user's home and stay there.

### Source code

```bash
skills/install.sh --instance source
```

- The CLI and MCP server are installed from `sdk/` with pip (needs PyPI; set `HTTPS_PROXY` behind
  a proxy).
- `--instance source` takes the key files from the source code directory's `key/` (created by
  `start_smartune.sh`). The default, `auto`, picks the deb when `/opt/intel/smartune` exists, so
  pass `source` when both exist and the running service is started from the source code; otherwise the deb's key
  files are copied and `smartune doctor` fails with `certificate not trusted`.
- Uninstall with `skills/install.sh --uninstall`.

### Common options

| Option | Effect |
|---|---|
| `--target openclaw\|claude\|both\|none` | Which agents get the skills. Default: every agent found |
| `--instance auto\|deb\|source\|DIR` | Which SmarTune instance's `key/` to copy. Default: `auto` (`deb` when `/opt/intel/smartune` exists, else `source`) |
| `--control yes\|no` | Install `smartune-control` or leave it out. Default: `yes` |
| `--mcp auto\|yes\|no` | The MCP server. Default: `auto` (installed; skipped with a warning when Python < 3.10). `yes` stops with an error instead; `no` leaves it out |
| `--remote user@host` | SmarTune runs on another machine ([details](../docs/user-guide/agent-integration.md#6-agent-on-another-machine)) |
| `--help` | Every option with its default |

What the installer changes and how to finish the setup in each agent:
[deployment guide](../docs/user-guide/agent-integration.md#3-install).

## Commands by skill

### `smartune-status`

| What | Command |
|---|---|
| Live overview: CPU, memory, pressure (PSI), GPU, NPU, disk, network | `smartune status` · `smartune status --sections gpu,npu` |
| Top resource consumers (by cpu, mem, gpu, npu, or disk IO) | `smartune top --by gpu` · `smartune top --io` |
| Find processes, details of one PID | `smartune ps --grep <name>` · `smartune proc <pid>` |
| Trend over a window (min, avg, max, downsampled) | `smartune history --since 6h` |
| Platform, driver, firmware and BIOS info | `smartune info` · `smartune info driver` |
| Controlled, limited and queued apps | `smartune apps` · `smartune app <app_id>` · `smartune limits` · `smartune pending` |
| Features this instance serves | `smartune caps` |
| Connection self-check | `smartune doctor` |

### `smartune-diagnose`

| What | Command |
|---|---|
| Active alerts (`--all` includes resolved ones) | `smartune diag alerts` |
| Why an app was limited, and when it recovered | `smartune diag why <app_id>` |
| Event ledger (filter by severity, app, type, keyword) | `smartune diag events` |
| Limit lifecycles: when applied, when recovered | `smartune diag lifecycles` |
| Redacted logs (sources: smartune, benchmark, journal) | `smartune diag logs` |
| Digest for a day, week, boot session or the last N hours | `smartune diag digest` · `smartune diag boots` |
| Root-cause evidence (by app, benchmark run or window) | `smartune diag context` |
| Event types, and how far back the data reaches | `smartune diag catalog` · `smartune diag retention` |
| Whether diagnostics is complete, and which events raise alerts | `smartune diag sources` · `smartune diag policies` |

Method: scope the question, take the overview, drill down, then conclude. Which signal to check
first for each symptom, the reference thresholds and the next command are in
[`../agent/content/methodology.md`](../agent/content/methodology.md).

### `smartune-bench`

| What | Command |
|---|---|
| Whether a run is in progress, and whether a new run could start (preflight) | `smartune bench status` |
| Wait for a run to finish (no sleeping or guessing; attaches the log tail on failure) | `smartune bench wait` · `smartune bench wait <run_id> --timeout 600` |
| Status and log tail of one run | `smartune bench run <run_id> --log` |
| Results: TTFT, TPOT, throughput, speedup vs CPU, performance per watt | `smartune bench results --latest` · `smartune bench results --model qwen3-8b --device gpu` |
| Models that can be benchmarked | `smartune bench models --search qwen` |

Starting and cancelling runs is done in the dashboard; the agent waits for a run with `bench wait`.

### `smartune-control`

Every change is shown to the user as a plan first and applied only after they agree.

| What | Command | Balancer endpoint |
|---|---|---|
| Put a running app under control | `smartune control register <name>` · `--priority medium` · `--pid <pid>` when several match | Plan: `/app/discover_search`, `/app/discover_extract` (read-only); apply: `/app/new_controlled_app` |
| Remove an app that `register` added | `smartune control unregister <app_id>` | `/app/purge_controlled_app` |
| Allowed limit ranges and running instances (read-only) | `smartune control profile <app_id>` | `/app/resource_limit_profile` |
| Limit CPU / memory (% of the app's current use) and disk IO (MB/s, IOPS) | `smartune control limit <app_id> --cpu 30 --mem 20` | `/app/resource_limit` |
| Lift a manual or automatic limit | `smartune control restore <app_id>` | `resource_restore` (manual) or `auto_limit_restore` (automatic; also adds the app to the exclusion list) |
| Keep an automatic limit but make it manual (no release, no rebound window) | `smartune control lock <app_id>` | `/app/lock_to_manual` |
| Freeze / thaw (manually limited apps only) | `smartune control freeze <app_id>` · `smartune control thaw <app_id>` | `/app/freeze` |
| Priority, network priority | `smartune control priority <app_id> high` · `smartune control net-priority <app_id> high` | `/app/set_priority`, `/app/set_network_priority` |
| Pin to P / E / LPE cores or a core list, or unpin | `smartune control affinity <app_id> e` · `smartune control affinity <app_id> restore` | `/app/cpu_affinity` |
| Auto-limit exclusion list: view, remove | `smartune control exclusions` · `smartune control exclusion-remove <app_id>` | `/app/auto_limit_exclusions`, `/app/auto_limit_exclusion_remove` |
| Cancel a queued launch | `smartune control cancel-launch <app_id>` | `/app/cancel_relaunch` |
| CPU / GPU frequency ranges per unit (read-only) | `smartune control freq-status` | `/device/freq/status` |
| Limit min / max frequency of the whole CPU or GPU, by unit id or group (`cpu`, `gpu`, `p`, `e`, `lpe`, `card0`) | `smartune control freq-limit e --max-mhz 2000` | `/device/freq/set` |
| Put frequency ranges back to what they were before SmarTune changed them | `smartune control freq-restore` · `smartune control freq-restore gpu` | `/device/freq/restore` |
