<!-- Copyright (c) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# SmarTune diagnostic methodology

What to look at first for a symptom, which signal confirms or rules out a cause, and which
`smartune` command to run next. This is guidance for your own judgement, **not a procedure to run
in full**: pick the rows that match the question, and stop as soon as the evidence answers it.

The thresholds below are **reference values** taken from SmarTune's own configuration
(`config/config.yaml`) and deterministic rules (`diagnostics/insights.py`,
`diagnostics/gpu_throttle.py`). A value near a threshold is a signal worth checking, not proof of a
cause. Where SmarTune defines no threshold, this file says so instead of inventing one.
Field meanings are in `metrics.md`.

> Status: first version, built from the rules in the code. Workload-specific thresholds (for
> example, the normal memory PSI for an inference server) are still to be added by the domain
> owners.

## Start here

1. **Scope**: the app (`smartune apps` gives the `app_id`), the benchmark run (`smartune bench status`
   gives the run id) or the time window.
2. **Look for findings SmarTune has already computed**: `smartune diag alerts`, then
   `smartune diag context --since 1h --findings-only` (or `--app <app_id>` / `--job <run_id>`).
   Each finding carries evidence, a confidence (0–1) and validation steps. Quote them; a confidence
   below about 0.6 means the signal is present but the evidence is thin.
3. **Find the matching row below** and run only the next commands it lists.

## Signals, reference values and next steps

| Signal | Reference value (source) | Next |
|---|---|---|
| System pressure `level` | `medium` from score 0.6, `high` from 0.8, `critical` at 1.0 (`thresholds`) | `smartune history --since 1h --sections cpu,memory,pressure` to see how long it lasted; `smartune top` for who is driving it |
| Pressure is high and memory dominates | Memory carries weight 7 of 10 in the score (`weights`), so memory alone can reach `critical` | `smartune top --by mem`; then `smartune diag why <app_id>` for the top app |
| Available memory low | Warning at ≤ 1.0 GB, error at ≤ 0.5 GB (`oom_risk`) | `smartune top --by mem`; `smartune diag events --type RESOURCE_MEMORY_OOM_KILL --since 6h` |
| Available memory falling | ≥ 0.05 GB/min sustained over ≥ 5 min. Projected exhaustion ≤ 30 min is a warning, ≤ 10 min an error (`memory_growth_risk`) | `smartune history --since 1h --sections memory`; `smartune top --by mem` to find the grower |
| OOM kill | Any `RESOURCE_MEMORY_OOM_KILL` event, from the kernel or systemd-oomd (`attributes.killer`) (`oom_risk`) | `smartune diag events --type RESOURCE_MEMORY_OOM_KILL --since 24h`; `smartune top --by mem` |
| Kernel or hardware fault | Any `PLATFORM_KERNEL_PANIC` / `_OOPS` / `_LOCKUP` / `_HUNG_TASK` / `_WARNING`, `PLATFORM_HOST_UNCLEAN_SHUTDOWN`, `DEVICE_HARDWARE_ERROR_*`, `DEVICE_STORAGE_IO_ERROR` or `DEVICE_GPU_HANG` event (`kernel_health_warning`; severity follows the worst type) | `smartune diag events --since 24h --severity warning` (warning and above: `PLATFORM_KERNEL_WARNING` is a warning). The event's `attributes.excerpt` holds the whole kernel report: read the call trace for the faulting module and what the task waited on; `smartune diag logs --since 1h --source journal --level error` for the rest |
| Swap growing while memory pressure is high | No numeric threshold in SmarTune; the trend is the signal | `smartune history --since 2h --sections memory,pressure` |
| CPU package hot | At or above the sensor's TjMax (85 °C when the sensor reports none); error at 5 °C over it (`thermal_throttling_detected`) | `smartune status --sections cpu`; correlate with `smartune diag events --type CONTROL_CPU_LIMIT_APPLIED` |
| GPU frequency held down | `throttled` on a GT, confirmed after 3 consecutive samples; reasons PL1, PL2, PL4, thermal, PROCHOT, RATL, VR TDC (`gpu_throttle`) | `smartune status --sections gpu`; `smartune diag events --type DEVICE_GPU_THROTTLED --since 6h` |
| GPU hang | Any `DEVICE_GPU_HANG` event | `smartune diag logs --since 1h --source journal --keyword gpu`; `smartune info driver` |
| Disk I/O pressure | Disk channel `critical` (score 1.0, `disk_thresholds`) is what authorises an `io.max` cap | `smartune top --io`; `smartune diag events --type CONTROL_DISK_IO_LIMIT_APPLIED` |
| Network pressure | `medium` 0.5, `high` 0.7, `critical` 0.9 (`network_thresholds`); driven by drops and queueing, not bandwidth | `smartune status --sections network` |
| A limit keeps being applied and recovered | ≥ 2 automatic re-limits of one app and resource, each within 600 s of the limit being lifted (`control_oscillation`). Manual limits do not count | `smartune diag lifecycles --app <app_id>`; `smartune history --since 6h --sections pressure` |
| A service crashed or restarts in a loop | `PLATFORM_SERVICE_CRASHED`, `PLATFORM_SYSTEMD_RESTART_LOOP` events | `smartune diag events --since 24h --keyword <service>`; `smartune diag logs --since 1h --source journal --all-journal --keyword <service>` |
| A process crashed | `PLATFORM_PROCESS_CRASHED` events, grouped by program and faulting object; error from 3 crashes of one pair (`process_crashes`) | `smartune diag events --type PLATFORM_PROCESS_CRASHED --since 24h`; `smartune diag logs --since 1h --source journal --all-journal --keyword <process>` |
| A new or bursting error message | `LOG_ERROR_SIGNATURE_NEW` (never seen on this host; an event, not an alert), `LOG_ERROR_SIGNATURE_BURST` (far above its own history; alerts) | `smartune diag events --type LOG_ERROR_SIGNATURE_NEW --since 24h`; `smartune diag logs --since 1h --keyword <text>` |
| Telemetry gap | Monitor samples further apart than the gap threshold (at least 60 s) while SmarTune was running (`incomplete_diagnostics_window`); gaps from SmarTune stops or restarts are not reported | Treat conclusions across the gap as incomplete. `smartune diag retention`, then `smartune doctor` |
| No events where some are expected | A log source unreadable, the event writer dropping events, or events failing to store (`storage.failed`) | `smartune diag sources` before concluding that nothing happened |
| GPU or NPU telemetry unavailable | The adapter reports an error, as opposed to the device being absent (`telemetry_source_unavailable`) | `smartune doctor` (telemetry check); `smartune info gpu` / `smartune info npu` |
| PSI (`pressure.cpu`, `memory`, `io`) elevated | SmarTune defines no standalone PSI threshold. PSI feeds the score above, so read it together with `level` | `smartune history --since 1h --sections pressure` |

## By symptom

### "Why was app X limited?"

`smartune diag why <app_id>` returns the current limit, lifecycles, events and findings. If a
lifecycle has `control_mode: auto`, show the pressure around its `applied_at` with
`smartune history --since 24h --sections cpu,memory,pressure`. If it is `manual`, an operator set
it: report that rather than looking for a pressure cause.

### "The machine is slow"

`smartune status`, then `smartune top`. If `level` is `low` and no app stands out, the slowdown is
probably not resource contention SmarTune can see. Say so, and check `smartune diag alerts` for
hardware or kernel events before concluding.

### "The GPU is slower than expected"

`smartune status --sections gpu`: which engine is busy (`ccs` for compute, `vcs` / `vecs` for media)
and whether a GT is `throttled`. A throttle with a power reason (PL1 / PL2 / PL4) points at the
power budget; `thermal` / `PROCHOT` points at cooling. `smartune top --by gpu` shows whether
another app shares the GPU.

### "The benchmark was slow or failed"

`smartune bench wait <run_id>` (or `smartune bench run <run_id> --log`) for the status and the end
of the log, then `smartune diag context --job <run_id>`. A run measured while another heavy app was
active, or while the GPU was throttled, is not comparable with a clean run. Say so rather than
reporting its numbers as the device's performance.

### "What happened last night / since the reboot"

`smartune diag digest --date <YYYY-MM-DD>`, or `smartune diag boots` followed by
`smartune diag digest --period boot --boot <boot_id>`. Drill into a specific event only if the user
asks or the digest is ambiguous.

## What to recommend

Recommend only what the evidence supports, and say what it rests on. Typical forms:

- Memory exhaustion: reduce the top app's footprint or concurrency, or raise its priority so
  SmarTune limits other apps first. Priority changes are made in the dashboard.
- Thermal / power throttling: check cooling or the platform power profile; numbers measured while
  throttled are not representative.
- Oscillating control: the pressure thresholds or recovery hysteresis may need tuning in
  `config.yaml`. This is an operator decision; report it, and do not tune anything yourself.
- Missing telemetry: fix the source (driver, kernel support) before trusting diagnoses that need it.
