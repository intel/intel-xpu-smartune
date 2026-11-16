# Agent API exposure

[← Back to contents](../README.md)

This page lists every SmarTune REST endpoint and says whether agents can reach it through the
`smartune` CLI, the skills and the MCP server, or whether it stays for the dashboard and the
service itself. `sdk/tests/test_parity.py` fails when the server gains a route that has no row
here, so every new endpoint must be classified in this file.

Categories:

| Category | Meaning |
|---|---|
| **Read-only** | Allowed by the `READ_ROUTES` allow-list in `sdk/smartune_sdk/client.py` |
| **Control** | Changes system state. Listed in `CONTROL_ROUTES` in `client.py` and reachable only through `smartune control ...`: without `--yes` the command only prints a plan, and it runs only after the user agrees (`agent/policy/consent.md`). These checks run on the client, so install the control skill only in trusted environments until the server has scoped tokens (`skills/install.sh --no-control` leaves it out). Over MCP these actions are the write tools of `smartune-mcp --allow-control`, and the user confirms each change in an elicitation form |
| **Control (not exposed)** | Changes system state and is not offered yet. If it is ever exposed, it must go through the same plan-then-confirm flow |
| **Internal** | Used only by the dashboard or the service, of no value to an agent, or has side effects. Not exposed |
| **Forbidden** | Destructive. Agents never get it |

## Service and authentication (`/auth`, `/smartune`)

| Endpoint | Category | Notes |
|---|---|---|
| `GET /smartune/capabilities` | Read-only | `smartune caps`. Tells whether balancer, benchmark and diagnostics are available |
| `POST /auth/login` | Internal | Browser login handshake; the CLI sends the `X-Auth-Token` header directly |
| `GET /smartune/shutdown` | Internal | The dashboard checks whether it can stop the service from this machine |
| `POST /smartune/shutdown` | Forbidden | Stops the whole service (lifts every limit, aborts a running benchmark). The server only checks that the request comes from this machine, which a local agent satisfies; enabled only in packaged deployments |

## Monitoring (`/monitor`)

| Endpoint | Category | Notes |
|---|---|---|
| `GET /monitor/dynamic_info[/<section>]` | Read-only | `smartune status`. **The CLI always passes `sections`**: without it the server starts a full background collection and writes a history record |
| `GET /monitor/static_info[/<section>]` | Read-only | `smartune info`. Never passes `force_refresh` |
| `GET /monitor/app_resource_stats` | Read-only | `smartune top` |
| `GET /monitor/app_disk_io_stats` | Read-only | `smartune top --io` |
| `GET /monitor/processes` | Read-only | `smartune ps`. Filtering, sorting and truncation happen on the client |
| `GET /monitor/process_detail` | Read-only | `smartune proc` |
| `GET /monitor/history` | Read-only | `smartune history`. The client computes the statistics and downsamples |
| `GET /monitor/history/retention` | Internal | Used by the settings page; `diag retention` covers the agent's needs |
| `POST /monitor/history/retention` | Internal | Changes configuration and deletes history data at once |
| `GET /monitor/config-hints` | Internal | Field descriptions and defaults for the settings page |
| `GET/POST /monitor/config/*` (weights_top, passive_control, monitored_sections, network_control, system_pressure, ...) | Internal | Tuning parameters. An agent changing them carries far more risk than benefit, and reading them only matters to whoever tunes the system |

## Applications (`/app`, needs balancer)

| Endpoint | Category | Notes |
|---|---|---|
| `POST /app/get_controlled_app` | Read-only | `smartune apps` / `limits`. With no controlled apps the server returns retcode 404, which the CLI treats as an empty list |
| `POST /app/get_pending_app` | Read-only | `smartune pending`. Also treats 404 as an empty list |
| `POST /app/get_priority_data` | Read-only | `smartune app` |
| `POST /app/limit_snapshot` | Read-only | `smartune app`, `diag why` |
| `POST /app/auto_limited_apps` | Read-only | `smartune limits` |
| `POST /app/resource_limit_profile` | Read-only | `smartune control profile`, and the defaults and allowed ranges in a `control limit` plan (out-of-range values are rejected on the client, because the server silently clamps them) |
| `POST /app/auto_limit_exclusions` | Read-only | `smartune control exclusions`, and the plan of `control exclusion-remove` |
| `POST /app/resource_limit` | Control | `smartune control limit`. The plan fixes every value and the `target_cgroups`; an auto-limited app is blocked (run `control lock` first) |
| `POST /app/resource_restore` | Control | `smartune control restore` for an app in `MANUAL_LIMITED` (also thaws it) |
| `POST /app/auto_limit_restore` | Control | `smartune control restore` for an app in `AUTO_LIMITED`. Adds the app to the exclusion list, which the plan states; under high or critical pressure the plan suggests `lock` instead |
| `POST /app/lock_to_manual` | Control | `smartune control lock` |
| `POST /app/freeze` | Control | `smartune control freeze` / `thaw`, only for manually limited apps |
| `POST /app/set_priority`, `/app/set_network_priority` | Control | `smartune control priority` / `net-priority` |
| `POST /app/auto_limit_exclusion_remove` | Control | `smartune control exclusion-remove` |
| `POST /app/cancel_relaunch` | Control | `smartune control cancel-launch` |
| `POST /app/cpu_affinity` | Control | `smartune control affinity`: pin to P / E / LPE cores or a core list, or restore. `class=status` is a query but shares the endpoint with writes, so it stays out of the read-only allow-list; the pinning state is in the `cpu_affinity` field of `get_controlled_app` (`smartune apps`) |
| `POST /app/adopt_auto_limit` | Internal | Take Control: registers an unregistered instance as a controlled app and takes it over. Part of the registration wizard and needs a human to judge the process identity |
| `POST /app/set_oom_score` | Control (not exposed) | Undecided: a lower OOM score makes the process harder for the OOM killer to kill |
| `POST /app/new_controlled_app` | Control | `smartune control register`. The plan first uses the two discovery endpoints to show the matched processes and the match rule that will be saved. Too-broad rules (interpreters, shells, container runtimes), SmarTune itself and the agent, critical system processes, and overlaps with existing apps are blocked. The remark is tagged `[smartune-control]` |
| `POST /app/discover_search`, `/app/discover_extract` | Read-only | Only scan `/proc`; used by the `control register` plan |
| `POST /app/set_to_control`, `/app/merge_controlled_app_processes`, `/app/set_controlled_app_processes` | Internal | Change the process identity of a registered app; needs human judgement |
| `GET/POST /app/get_apps` | Internal | Wizard list; writes to the database when given `store` |
| `POST /app/check_running_apps` | Internal | Despite the name, it registers running apps with the service |
| `POST /app/interrupted_limit_status` | Internal | After a service restart, the UI uses it to tell whether an interrupted limit can be resumed |
| `GET /app/events` | Internal | The dashboard's SSE stream. Could become MCP notifications later |
| `POST /app/kill_process`, `/app/suspend_process` | Forbidden | Kill or suspend any process |
| `POST /app/purge_controlled_app` | Control (restricted) | Only as the undo of `register`: `smartune control unregister` sends it only for apps whose remark starts with `[smartune-control]`; other apps can still only be removed in the dashboard. The server first lifts a manual limit and the CPU pinning; an auto-limited app is refused |

## Device frequency (`/device/freq`, needs balancer)

These act on a whole device (a cpufreq policy or a GPU gt), not on one app: every app on it is
affected. The server looks unit ids up only in the list it discovers, clamps a range to each
unit's hardware range, records the range seen before its first change, and restores every unit
when the service stops. Nothing is saved across restarts.

| Endpoint | Category | Notes |
|---|---|---|
| `GET /device/freq/status` | Read-only | `smartune control freq-status`, and the current ranges in a `freq-limit` / `freq-restore` plan |
| `POST /device/freq/status` | Internal | Same query as the GET; the client uses the GET form only |
| `POST /device/freq/set` | Control | `smartune control freq-limit`. The plan expands groups (`cpu`, `gpu`, `p`, `e`, `lpe`, a GPU card) into explicit unit ids, pins both min and max, shows the clamped range per unit, and warns when a CPU maximum falls below half of a unit's top clock. A partial failure is reported with a hint not to repeat it |
| `POST /device/freq/restore` | Control | `smartune control freq-restore`. Always sends an explicit unit list: the server restores *every* limited unit when the list is empty, which would include units limited after the plan |

## Diagnostics (`/diag`, needs diagnostics)

| Endpoint | Category | Notes |
|---|---|---|
| `GET /diag/alerts` | Read-only | `smartune diag alerts` |
| `GET /diag/events` | Read-only | `smartune diag events` |
| `GET /diag/control-lifecycles` | Read-only | `smartune diag lifecycles`, `diag why` |
| `GET /diag/logs` | Read-only | `smartune diag logs`. The server redacts the lines |
| `GET /diag/digest` | Read-only | `smartune diag digest`. Also the agent-side source for AI log analysis and daily reports |
| `GET /diag/context` | Read-only | `smartune diag context`, `diag why` |
| `GET /diag/boots`, `/diag/retention`, `/diag/event-catalog` | Read-only | Supporting queries |
| `GET /diag/monitor-metrics` | Read-only | In the allow-list; the CLI has no command for it yet and uses `smartune history` instead |
| `POST /diag/alerts/<key>/acknowledge`, `/silence` | Control (not exposed) | Silencing an alert can hide a problem; needs user confirmation if ever exposed |
| `POST /diag/control-lifecycles/clear-without-runtime-state` | Internal | Cleans up orphaned records; an operator task |

## Benchmark (`/bench`, needs benchmark)

| Endpoint | Category | Notes |
|---|---|---|
| `GET /bench/run`, `/bench/run/<id>` | Read-only | `smartune bench status`, `bench run` |
| `GET /bench/preflight`, `/bench/quiet_mode` | Read-only | `bench status` |
| `GET /bench/results`, `/bench/results/matrix` | Read-only | `bench results` |
| `GET /bench/models` | Read-only | `bench models` |
| `GET /bench/env` | Internal | **A GET, but it installs the venv when missing**, so it stays out of the read-only allow-list |
| `POST /bench/run`, `/bench/run/<id>/cancel` | Control (not exposed) | A run enters quiet mode, which pauses automatic control |
| `POST /bench/run/plan`, `GET /bench/run/<id>/plan`, `POST /bench/run/advanced` | Internal | Advanced mode; needs a human to edit the command |
| `POST /bench/env/setup`, `/bench/env/ov`, `/bench/models/refresh` | Internal | Environment installation and switching; slow |
| `GET /bench/network`, `POST /bench/network`, `POST /bench/network/test` | Internal | Proxy and Hugging Face token settings for model and package downloads (settings page). `POST` changes configuration and restarts pending downloads; `/network/test` makes outbound probes |
| `POST /bench/quiet_mode` | Internal | Leaving quiet mode makes the run's results incomparable |
| `GET /bench/models/memory`, `/bench/models/disk`, `/bench/env/setup/log`, `/bench/run/metrics/latest`, `/bench/results/log`, `/bench/results/timeline` | Internal | UI details; add to the allow-list when needed |
| `GET /bench/events` | Internal | SSE |
| `DELETE /bench/results`, `/bench/results/job`, `/bench/models/local` | Forbidden | Delete benchmark results or model files |

## Known documentation issues

- In `docs/reference/api.md`, the `GET /monitor/dynamic_info` example shows `gpu.gpu_usage.devices`; the code returns `gpu.gpu_usage.parsed.devices` (see `monitor/metrics/gpu_perf.py`). In the live endpoint, NPU `npu_smi` is `{available, raw, error}`, with the metrics in the JSON string in `raw`.
