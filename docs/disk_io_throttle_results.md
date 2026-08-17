<!-- Copyright (c) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Disk I/O under pressure — measured effect

What the disk-IO channel actually buys you, measured end to end: three workloads fight
over one NVMe, the disk channel reaches `critical`, and SmarTune caps the throttleable
ones so the **Critical-priority app keeps the device**.

[`docs/pressure_algorithm.md`](pressure_algorithm.md) explains how the score and the level
are computed. This document starts where that one stops — at `disk_level = critical` — and
shows what the caps do to real throughput.

Two full runs of [`balancer/test/testing_io.sh`](../balancer/test/testing_io.sh) on the
same machine are used throughout:

| | raw log | difference |
|---|---|---|
| **Run A** | [`testing_io_resoult.txt`](../balancer/test/testing_io_resoult.txt) | `fio_lo2` **unregistered** → capped at the `undefined` rates |
| **Run B** | [`testing_io_resoult_sec.txt`](../balancer/test/testing_io_resoult_sec.txt) | `fio_lo2` **registered at Low** → capped at the `Low` rates |

Everything else — hardware, profile, durations — is identical, so the pair isolates one
variable: which priority row the rates come from.

---

## 1. The setup

Three fio workloads run **concurrently**, each in its own transient systemd scope, so
SmarTune discovers and throttles them through the real product path — no hand-written
cgroup paths:

| scope | binary | registered as | expected treatment |
|---|---|---|---|
| `hi-io.scope` | `fio_hi` | **Critical** | never throttled |
| `lo-io.scope` | `fio_lo` | **Low** | capped at the `low` rates |
| `lo2-io.scope` | `fio_lo2` | Run A: not registered<br>Run B: **Low** | Run A: `undefined` rates (stricter)<br>Run B: `low` rates |

- Profile `device` (the default): `libaio`, `direct=1`, `bs=4k`, `jobs=16`, `iodepth=128`.
  Deep async queues saturate the *device*; the stall that pushes the gate to `critical`
  comes from the block layer's tag pool, not from writeback.
- Two phases of 600 s, **write then read**, because `io.max` caps the two directions
  independently (`wbps`/`wiops` vs `rbps`/`riops`) — only running both shows the read half
  is wired up at all. Each phase relaunches the scopes, so it starts unthrottled and earns
  its own cap.
- The sampler reads each scope's `io.stat` every 5 s and labels it `free` or `CAP …`,
  where the label is parsed back out of the scope's real `io.max`.
- "before" / "after" in the tables below are the windows on either side of the first
  throttle. `hi-io` is never capped itself, so it is split at the *run's* first throttle —
  its before/after rows cover the same wall-clock windows as the others.

---

## 2. The chain being exercised

```
disk USE model + io PSI  ──►  disk_level  ──►  candidate selection  ──►  io.max
   (pressure_algorithm.md)      critical        (priority rules)        (per disk)
```

1. **`high` arms, `critical` fires.** At `high` the top disk-IO consumers are resolved and
   logged but nothing is written; only `critical` applies a cap
   ([`balancer.py:904`](../balancer/balancer/balancer.py#L904)).
2. **One candidate per critical tick**, walked in descending-IO order
   ([`_handle_disk_io_stressed`](../balancer/balancer/balancer.py#L1657)). A candidate is
   skipped when it is a **Critical** app, when it is already under an auto io-limit (so the
   next tick moves on to the next heavy user), or when it is doing too little I/O on that
   disk for a cap to relieve anything ([`_qualifies_for_throttle`](../balancer/balancer/balancer.py#L539)).
3. **Rates come from the app's priority row**; an unmanaged app counts as `undefined`, the
   strictest tier. The row is scaled by the disk's media class and written as `io.max` on
   the stressed disks only ([`_scaled_io_limits`](../balancer/balancer/balancer.py#L577)).

The two rows that matter here, from `limit_policy.disk_io.rate` in
[`config/config.yaml`](../config/config.yaml) (`media_scale.nvme = 1.0`, so these land
unscaled):

| priority | write MB/s | read MB/s | write IOPS | read IOPS |
|---|---|---|---|---|
| `low` | 20 | 30 | 6000 | 9000 |
| `undefined` | 10 | 20 | 1000 | 8000 |

---

## 3. Run A — the unmanaged app at `undefined`

Per-scope throughput, before vs after the throttle:

| scope | priority | phase | before | after | cap written |
|---|---|---|---|---|---|
| `hi-io` | critical | write | 91.0 MB/s | **154.8 MB/s** | — |
| `lo-io` | low | write | 91.2 MB/s | 19.9 MB/s | `w20 r30` |
| `lo2-io` | undefined | write | 77.6 MB/s | 10.0 MB/s | `w10 r20` |
| `hi-io` | critical | read | 155.2 MB/s | **417.5 MB/s** | — |
| `lo-io` | low | read | 157.4 MB/s | 30.0 MB/s | `w20 r30` |
| `lo2-io` | undefined | read | 151.1 MB/s | 20.0 MB/s | `w10 r20` |

Share of the three scopes' combined throughput:

| phase | window | hi-io (Critical) | lo-io (Low) | lo2-io (undefined) | TOTAL |
|---|---|---|---|---|---|
| write | before | 33.2% | 33.3% | 33.4% | 273.7 MB/s |
| write | after | **83.6%** | 10.8% | 5.6% | 185.2 MB/s |
| read | before | 33.6% | 33.6% | 32.8% | 461.5 MB/s |
| read | after | **88.6%** | 7.2% | 4.2% | 471.3 MB/s |

## 4. Run B — the same app registered at Low

| scope | priority | phase | before | after | cap written |
|---|---|---|---|---|---|
| `hi-io` | critical | write | 187.4 MB/s | **284.4 MB/s** | — |
| `lo-io` | low | write | 176.0 MB/s | 18.2 MB/s | `w20 r30` |
| `lo2-io` | **low** | write | 184.3 MB/s | 18.2 MB/s | `w20 r30` |
| `hi-io` | critical | read | 152.5 MB/s | **409.3 MB/s** | — |
| `lo-io` | low | read | 160.1 MB/s | 30.0 MB/s | `w20 r30` |
| `lo2-io` | **low** | read | 105.2 MB/s | 30.0 MB/s | `w20 r30` |

| phase | window | hi-io (Critical) | lo-io (Low) | lo2-io (Low) | TOTAL |
|---|---|---|---|---|---|
| write | before | 34.0% | 32.0% | 34.0% | 550.5 MB/s |
| write | after | **87.8%** | 5.6% | 6.6% | 323.9 MB/s |
| read | before | 37.4% | 36.9% | 25.8% | 408.1 MB/s |
| read | after | **86.8%** | 6.9% | 6.4% | 471.7 MB/s |

> The summary table inside `testing_io_resoult2.txt` still prints `undefined*` in the
> priority column for `lo2-io` — that label is hardcoded by the script for the unregistered
> case. The cap it actually wrote, `w20 r30`, is the **Low** row: in Run B `lo2-io` was a
> registered Low app.

---

## 5. What the numbers show

**The caps land exactly.** Measured `after` throughput equals the written cap within
sampling noise — `19.9 / 20`, `10.0 / 10`, `30.0 / 30`, `20.0 / 20` MB/s. With 4 KiB blocks
the *bandwidth* half of `io.max` is the binding one (30 MB/s ÷ 4 KiB = 7 680 IOPS, which is
exactly what the read rows report), which is what the `iops = MB/s × 300` calibration in
`config.yaml` is designed to give.

**Priority is the only input that decides the number.** Run A vs Run B differ in nothing
but whether `fio_lo2` was a registered Low app; the cap moves from `w10 r20` to `w20 r30`
and its write share from 5.6% to 6.6% accordingly. An unmanaged heavy writer is throttled
*harder* than a managed Low one — deliberately, since nothing is known about it.

**The Critical app is protected in both absolute and relative terms.** It is never capped
(`cap` column is `-` in every row), its share of the device goes from ~1/3 to **84–89%**,
and its absolute throughput *rises*: write +70% (Run A) / +52% (Run B), read +169% / +168%.
The relative share is the primary evidence; the absolute number is the useful one.

**Order and timing.** The first cap appears 12–78 s into a phase — the disk channel has to
smooth into `critical` first (§4 of the algorithm doc), and the fast end of that range is
Run B's read phase, which inherited an already-elevated level from the write phase that
preceded it. The second app is capped
2–3 samples later, i.e. one candidate per critical tick in descending-IO order. In the
write phases `lo-io` was the heavier user and went first; in the read phases `lo2-io` did.

**Read TOTAL is flat, write TOTAL drops.** Read throughput barely moves across the split
(461 → 471, 408 → 472 MB/s): the caps redistribute the device rather than waste it. On the
write side TOTAL falls (274 → 185, 551 → 324 MB/s) — that is the SSD itself slowing down
under sustained random 4 K writes (SLC cache exhaustion, GC), not the caps destroying
throughput, and it is exactly why `hi-io` still *gaining* absolute MB/s across the same
window is the meaningful result.

---

## 6. Reproducing

```bash
sudo bash balancer/test/testing_io.sh   # both runs above; PROFILE=device, 600 s write + 600 s read
```

Register `fio_hi` as **Critical** and `fio_lo` as **Low** in the dashboard's *Add App*
wizard during the write phase; leave `fio_lo2` alone to reproduce Run A, or register it at
Low to reproduce Run B. The summary table prints on exit, covering both phases.

While it runs, the service log carries the decision trail:

| tag | what it tells you |
|---|---|
| `[disk-level]` | the level, and the USE + PSI numbers behind it (algorithm doc §15) |
| `[disk-io]` | the candidate list, why each candidate was skipped, and the cap applied |

`python3 balancer/test/psi_probe.py 600 1 --scope hi-io.scope,lo-io.scope,lo2-io.scope`
watches the per-scope PSI alongside.

---

## 7. Scope of these results

- **One NVMe, one media class.** `media_scale` and the per-media `candidate_floor` rows are
  not exercised here; on slower media both the cap and the floor move.
- **The `undefined` write-IOPS cap did not bind in Run A.** `lo2-io` settled at 10.0 MB/s /
  2 559 write IOPS while `undefined.write_iops` is 1000. The bandwidth half held exactly, so
  the cap as a whole worked, but the IOPS half is worth re-checking before it is relied on.
- **PSI figures in the log tail are post-throttle.** The `pressure diagnostics` block prints
  at exit, when the caps have already relieved the device, so it reads lower than the values
  that triggered the throttle.
- **These are the passive, priority-derived defaults.** Per-app active limits set through the
  API take a different path and are not covered by this run.
