<!-- Copyright (c) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# SmarTune metrics reference

What the fields in `smartune` output mean. The scoring model behind the pressure figures is
described in full in `docs/design/pressure-algorithm.md`.

## System pressure (`smartune status` → `pressure`)

| Field | Range | Meaning |
|---|---|---|
| `level` | `low` / `medium` / `high` / `critical` | SmarTune's overall verdict, and what drives automatic throttling. As it rises, lower-priority apps are limited; as it eases, they are restored in stages |
| `score` | 0 – 1 | Smoothed score behind `level`. Default bands: `low` < 0.6 ≤ `medium` < 0.8 ≤ `high` < 1.0 = `critical` (configurable in `config.yaml` → `thresholds`). A score of 0.5 is still `low` |
| `cpu`, `memory`, `io` | 0 – 1 | Raw PSI from `/proc/pressure/*` (`some`, rolling average): the fraction of time at least one task was stalled waiting for that resource. 0.25 means stalls 25 % of the time |
| `is_disk_io_stressed` | bool | Whether any disk is currently stressed (see the disk channel below) |
| `network_pressure_level`, `network_pressure_pct`, `network_worst_nic` | level, % | The separate network channel (see below) |

How to read them:

- **PSI measures contention, not utilisation.** CPU at 100 % with nobody waiting is benign and
  keeps PSI low; a high PSI means work is actually being delayed.
- **Memory dominates the system score.** The default weights are `cpu 1 : memory 7 : io 2`, and
  memory scarcity (low `MemAvailable`) can push the score to `critical` on its own, even with
  little CPU or I/O stall.
- The score discounts stall caused by apps SmarTune has already limited, so it can sit in
  `medium` after a limit even though raw PSI is still elevated. That is by design: it stops
  SmarTune from limiting the same app again and again.

## Disk channel (`status` → `disk`)

A separate channel with its own level. It does not feed the system score above.

| Field | Meaning |
|---|---|
| `level`, `score` | Disk-IO pressure level and score. `critical` is what authorises an `io.max` cap on an app |
| `pressure_pct` | Disk pressure gated by I/O PSI, in % |
| `busy_pct`, `busy_level` | Share of disks that are busy. A busy disk is not necessarily under pressure |
| `disks.<name>` | Per disk: `read_mbps`, `write_mbps`, `pressure` (0 – 1), `disk_type` (nvme / ssd / hdd), `is_busy` |

## Network channel

Also separate. It is driven by drops and queueing on each NIC, not by bandwidth alone:
a busy but clean link is not pressure. Network shaping is off by default.

## CPU and memory (`status` → `cpu`, `memory`)

| Field | Meaning |
|---|---|
| `cpu.usage_total` | CPU utilisation across all logical CPUs, % |
| `cpu.iowait_total` | Share of CPU time idle while waiting for I/O, % |
| `cpu.p_core_usage`, `e_core_usage`, `lpe_core_usage` | Utilisation per core type on hybrid CPUs, % |
| `cpu.pkg_power_w`, `temperature_c` | Package power (RAPL) in W, package temperature in °C |
| `memory.usage_percent`, `available_gb`, `total_gb` | Memory in use (%), available and total (GB) |
| `memory.swap_used_gb`, `swap_usage_percent` | Swap in use. Swap that keeps growing alongside high memory PSI points to memory pressure |

## GPU (`status` → `gpu`)

| Field | Meaning |
|---|---|
| `devices[].utilization` | Utilisation of the **busiest engine**, %. This is how the dashboard defines GPU utilisation |
| `devices[].engine_util` | Per engine, %: `rcs` render/3D, `ccs` compute (typical for AI inference), `vcs` video decode/encode, `vecs` video enhancement, `bcs` copy |
| `devices[].freqs[]` | Per GT: `act_mhz` (actual), `max_mhz`, and `throttled` when the frequency is being held down |
| `devices[].power_w` | GPU power in W, where hwmon reports it |
| `vram` | Device memory used and total, on discrete GPUs |

## NPU (`status` → `npu`)

`available: false` means no NPU, or its telemetry is not readable (an `error` field says why).
Otherwise: `utilization_percent`, `frequency_mhz`, `memory_mb`, and `power_w` / `temperature_c`
where the platform reports them.

## Per-app figures (`smartune top`)

| Field | Meaning |
|---|---|
| `cpu_pct` | Share of **total** CPU capacity across all cores, %. 100 % means every core is busy |
| `memory_mb` | Resident memory, MB |
| `gpu_util` | GPU utilisation attributed to the app, % of the busiest engine |
| `gpu_mem_mb` | GPU memory attributed to the app, MB |
| `npu_mem_mb` | NPU memory held by the app, MB. Per-app NPU utilization is not measured |
| `io_read_rate`, `io_write_rate` | Disk I/O, MB/s |
| `score` | SmarTune's combined ranking score. Use it only to rank apps against each other, not as a percentage |

While a benchmark runs, SmarTune pauses collection (quiet mode). `status` and `top` then carry
`"quiet_mode": true` and a `note`: `status` shows the last cached values (`cached_at` says when they
were taken) and `top` returns no apps. Neither means the system is idle.

`smartune apps` shows `cpu_affinity` only for an app pinned to a core type (P / E / LPE) or to
specific cores.

## History (`smartune history`)

`stats` gives `min` / `avg` / `max` / `last` for each metric over the window; `series` is an evenly
downsampled time series that always includes the latest sample; `pressure_levels` counts samples
at each level. History is recorded every 5 seconds, and how far back it reaches is shown by
`smartune diag retention`.
