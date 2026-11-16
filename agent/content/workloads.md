<!-- Copyright (c) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Workload signatures

How common workloads show up in `smartune` output, so you can tell what an app is doing from its
figures and spot a workload behaving unlike itself. Field meanings are in `metrics.md`; what to do
about a problem is in `methodology.md`.

These are **qualitative signatures**, which SmarTune's code does not check. SmarTune ships no
per-workload thresholds, so this file gives no numbers. A workload that does not match its usual
pattern is a question to ask, not a fault to report.

> Status: first version. Typical figures per platform (for example the normal TPOT of an 8B model
> on the iGPU, or the bandwidth of an SMB copy) are still to be added by the domain owners.

| Workload | Where it shows | Usually also | Telltale of trouble |
|---|---|---|---|
| **LLM / AI inference on the GPU** | `gpu.devices[].engine_util.ccs` (compute) busy; large `gpu_mem_mb` in `top --by gpu` | Bursty utilisation between requests; modest CPU | GT `throttled` with a power reason; `gpu_mem_mb` near the device's memory, or on an iGPU, system memory falling, since the iGPU shares it |
| **AI inference on the NPU** | `npu.utilization_percent` and `npu_mem_mb` in `top --by npu` | Low CPU and GPU for the same app | NPU `available: false` with an `error` (telemetry, not the model, is failing) |
| **AI inference on the CPU** | High `cpu_pct` for one app across P-cores (`p_core_usage`) | Memory bandwidth bound; large `memory_mb` | Rising CPU PSI with other apps waiting; package temperature near TjMax |
| **Video transcoding / decoding** | `engine_util.vcs` (decode/encode) and `vecs` (enhancement) busy, not `ccs` | Steady rather than bursty; moderate CPU | Frame drops reported by the app while `vcs` is saturated |
| **3D / rendering / display** | `engine_util.rcs` busy | Tied to an interactive app | `rcs` contention when an inference app shares the GPU |
| **Backup, large copy, indexing** | `top --io` (`io_read_rate` / `io_write_rate`); `disk.disks.<name>` MB/s | I/O PSI rises; disk channel `busy_pct` | Disk channel reaching `critical`, which can lead SmarTune to cap the app's I/O (`CONTROL_DISK_IO_LIMIT_APPLIED`) |
| **File serving (SMB / NFS)** | Network channel and disk I/O together | Many small requests; low CPU | `network_pressure_level` rising from drops and queueing on `network_worst_nic`, not from bandwidth alone |
| **Build / compile** | Every core busy; many short-lived processes in `ps` | Memory spikes per compiler job; disk reads | Memory pressure from too many parallel jobs; OOM kills of compiler processes |
| **Memory leak** | One app's `memory_mb` grows steadily across `history` | System `available_gb` falls in step | The `memory_growth_risk` finding; eventually an OOM kill or a memory limit on that app |
| **Benchmark run (SmarTune's own)** | `bench status` shows `current`; output carries `quiet_mode: true` | Monitoring and automatic control paused | Another heavy app running at the same time: the measurement is not representative |

## Reading them together

- **The busiest GPU engine identifies the work**: `ccs` means compute or AI, `vcs` / `vecs` media,
  `rcs` 3D, `bcs` copies. Utilisation alone says how busy the GPU is, not what it is doing.
- **On an integrated GPU, GPU memory is system memory.** Inference that grows its GPU memory also
  lowers `available_gb` and can raise memory pressure.
- **Utilisation is not pressure.** A workload can keep a resource at 100 % without anything
  waiting (PSI low, `level` low). Contention starts when other work is delayed.
- **Per-app NPU utilisation is not measured.** Attribute NPU load by `npu_mem_mb`, and only when a
  single app holds NPU memory.
