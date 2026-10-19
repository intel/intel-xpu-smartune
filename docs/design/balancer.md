<!-- Copyright (c) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Balancer Guide — Auto and Manual Control

The balancer does one thing: when resources run short, it decides who gives way.

That decision has two possible sources:

| | Auto | Manual |
|---|---|---|
| Who starts it | The pressure monitor loop, unattended | The user, via the dashboard or the REST API |
| When it acts | Only once pressure reaches `critical` | The moment the user clicks |
| Who gets limited | It picks for itself — the current top consumer | The app the user names |
| By how much | Looked up in `limit_policy` by priority | Adjusted by the user in the dialog; same defaults |
| Does it give back on its own | Yes, in stages | No — Restore has to be clicked |
| Master switch | `passive_resource_control.enabled` | None (always available) |

The two are mutually exclusive: an app only ever carries one of them. Auto skips any cgroup that already carries a manual limit, and limiting an app by hand also pulls it out of the auto candidate pool (an exclusion).

> How the pressure score itself is computed is covered in [pressure-algorithm.md](pressure-algorithm.md). This document only covers what happens once it has been computed.

---

## Contents

- [Part I — Auto Control: Scenarios Handled Automatically](#part-i--auto-control-scenarios-handled-automatically)
- [Part II — Manual Control: What the User Can Do](#part-ii--manual-control-what-the-user-can-do)
- [Part III — Parameter Reference](#part-iii--parameter-reference)
- [Part IV — I Want X, What Do I Change](#part-iv--i-want-x-what-do-i-change)
- [Appendix — Where the Code Lives](#appendix--where-the-code-lives)

---

# Part I — Auto Control: Scenarios Handled Automatically

Every automatic action runs in the same loop: it wakes about once a second and makes a decision every `monitor_idle_check_interval` (10s by default). The one exception is the rising edge into critical, which decides immediately rather than waiting out the 10s.

```
every 1s:   sample memory (feeds the swap decision)
            ↓
every 10s, or on a rising edge into critical:
            read the pressure levels (system / disk)
            → L0 swap growth / release
            → limit or restore (the 8 scenarios below)
            → network channel (judged independently)
every 2s:   reaper — clear leftover limits from apps that have exited
```

### About memory: three layers, L0 / L1 / L2

Memory is the resource most likely to take the machine down, so it gets three separate layers of handling; scenarios 1–3 below are one layer each. These three labels are used consistently in this document and in the code's logs:

| Label | What it is | Which scenario | Acts on |
|---|---|---|---|
| L0 | Swap growth — making room for memory to be written out to | Scenario 3 | The whole machine |
| L1 | Writing the `memory.high` soft limit | Scenario 1 | The one app picked for limiting |
| L2 | Actively calling `memory.reclaim` to push pages out | Scenario 2 | Idle apps that are already limited |

They stack from the bottom up: L0 is the fuel (with swap full, neither layer above it has anywhere to put anonymous pages), L1 is the workhorse (the kernel stops the app on its allocation path and reclaims in place, regardless of how busy it is), and L2 is the supplement (going out to collect what idle apps are still holding).

### 1. System pressure critical → limit CPU and memory (L1)

| | |
|---|---|
| Trigger | System pressure level = `critical` (memory carries the heaviest weight; see the pressure document) |
| Who gets picked | The number-one top consumer. Apps at `critical` priority are skipped outright — a notification is sent, no limit is applied |
| What it does | Writes `CPUQuota` and `memory.high`. When an app spans several cgroups, the quota is split across them in proportion to actual usage |
| By how much | CPU: `limit_policy.cpu.rate[priority]` of whole-machine capacity (rate=0.7 → 70% of the machine)<br>Memory: `memory.high = min(total RAM × rate, app working set × rate)` |
| Side effects | The CPU governor switches to `performance`. If the app still carries an auto `io.max`, that is removed first — otherwise the reclaim in the next step would be writing to swap through the app's own I/O limit |
| Will not happen to | Cgroups that already carry a manual limit; apps the user has excluded; apps that already carry an auto CPU/memory limit |

Taking `min(machine share, working-set share)` for memory is deliberate: capping an app that uses 2% of RAM "to 30% of the machine" is no cap at all.

<sub>Code: `balancer.py#_handle_critical_pressure` → `balancer.py#_apply_resource_limits`; the memory formula is in `balancer.py#_auto_mem_high_mb`</sub>

### 2. System pressure critical → reclaim memory from idle apps (L2)

| | |
|---|---|
| Trigger | Immediately after scenario 1, in the same tick (the target has just been pressed down to its `memory.high`, which is when reclaim is cheapest) |
| Goal | Bring machine-wide `used_eff` below `reclaim.target_mem_pct` (80% by default). Not to push every app below its own `memory.high` |
| Who gets picked | Every app carrying a CPU/memory limit, manual ones included, weighted so the most recently limited give up the most. `critical` priority is skipped |
| How much per round | 1%–5% of current usage (`gain_min` / `gain_max`); where it lands in that band is set by memory PSI — free memory is the gate, PSI is the throttle |
| Skip conditions | CPU busy (`idle_cpu_threshold`, read from the app's own cgroup `cpu.stat`, in units of cores), page cache share too high (`max_file_cache_fraction`), an `io.max` present, cooldown not elapsed (`cooldown_sec`, 30s), or the shortfall not being in anonymous pages |
| How it runs | In chunks (`chunk_mb`, 200MB, roughly 1ms/MB) handed to a background thread; it never blocks the main loop |

This layer only touches idle apps, and its skip conditions are far stricter than L1's — pushing pages out of a running app just gets them faulted straight back in, and the stall time is paid for nothing. A limited app giving up memory on its own is scenario 1's L1 (`memory.high`) doing the work; that path is indifferent to how busy the app is and is unaffected by the skip conditions here.

<sub>Code: `balancer.py#_tick_memory_reclaim`, `control_manager.py#should_reclaim`</sub>

### 3. Memory / swap watermarks → swap growth and release (L0)

L2 cannot work without swap (`memory.reclaim` returns EAGAIN), so L0 does not wait for pressure to reach critical; it has watermarks of its own.

| | |
|---|---|
| Growth trigger | `memory > grow_mem_watermark` (80%) and `swap free < grow_swap_watermark` (50%); or `swap free < grow_swap_critical` (10%) on its own |
| What it reads | The worst reading within the `pressure_window_sec` (30s) window, not the instantaneous value |
| Step size | `MemTotal × step_ratio` (25%), with the total capped at `MemTotal × max_swap_ratio` (2.0), leaving `disk_reserve_pct` / `disk_reserve_min_gb` free on disk |
| Cadence | Normal rhythm most of the time; tightened while critical |
| Release | Once pressure recedes, one file is given back per round. Unaffected by turning `passive_resource_control` off — what has been allocated still has to be returned |
| Only touches its own | Path `/var/lib/smartune/swap`, filesystem restricted by `allow_fs` (ext4/xfs) |

<sub>Code: `balancer.py#_tick_swap_headroom`, `balancer.py#_tick_swap_release`</sub>

### 4. Disk I/O pressure → `io.max` throttling

Two-stage, which is the biggest difference from the system channel:

| Disk level | Action |
|---|---|
| `high` | Identify only, do not throttle — pre-fetch the top I/O consumers and hold them ready for the next tick |
| `critical` | Actually write `io.max` |

| | |
|---|---|
| Who gets picked | Top disk consumers, walked down the list one by one (unlike the system channel, which only ever takes the first). `critical` priority is skipped; anything already limited is skipped |
| Floor | The app's I/O on that disk has to exceed `candidate_floor[medium]` (MB/s or IOPS, either one) — throttling a light user will not save the device |
| By how much | The read/write MB/s and read/write IOPS from `limit_policy.disk_io.rate[priority]`, multiplied by `media_scale[medium]` |
| Which disks | Only the disks that are actually busy. When the busy-disk information cannot be read it degrades to limiting every disk, not to limiting none |

The `rate` table is calibrated against NVMe, and `media_scale` is what converts it down to slower media (HDD 0.25, USB 0.15, and so on). Every entry in the table is configured with `iops = MB/s × 300`; changing only one of the pair makes the effective ceiling for small I/O a different number than intended.

<sub>Code: `balancer.py#_handle_disk_io_stressed`, `balancer.py#_qualifies_for_throttle`</sub>

### 5. App launched during critical → suspend and queue

| | |
|---|---|
| Trigger | eBPF catches the execve while the system is in critical |
| What it does | `SIGSTOP` to suspend, state set to `pending`, pushed into a max-priority queue |
| Exception | Apps at `critical` priority are not held; they are let through immediately and given OOM protection |
| Release | Once pressure leaves critical and the disks are not stressed, one app is released by priority with `SIGCONT` per tick |
| Safety net | Under quiet mode (a benchmark is being measured) every other automatic action stops, but releases continue — otherwise an app the user launched would stay suspended indefinitely |
| Manual override | `/app/cancel_relaunch` cancels a queued launch |

<sub>Code: `app_intercept.py#print_event`, `app_intercept.py#handle_monitored_app`, `balancer.py#_drain_pending_app_queue`</sub>

### 6. Apps at `critical` priority → OOM protection

`oom_score_adj` is lowered automatically at launch, reducing the chance of being picked by the OOM killer. This applies only to controlled apps at `critical` priority, and the original value is written back on restore.

<sub>Code: `app_utils.py#adjust_oom_priority`</sub>

### 7. CPU frequency governor

It follows the limiting actions, not the pressure level:

| Action | Governor |
|---|---|
| A limit is applied (auto or manual) | `performance` |
| Partial restore | `performance` |
| Full restore | `powersave` |

<sub>Code: `control_manager.py#adjust_resources`, `governor.py#set_performance`</sub>

### 8. Staged restore

The two timers are independent — the system channel going stable does not release disk limits, and vice versa.

| Condition | Action |
|---|---|
| Pressure stable at `medium` for `STABLE_PERIOD` (30 min) | System channel, half back: CPUQuota and memory.high relaxed to twice the original limit |
| Pressure stable at `low` for 30 min | System channel, all back: CPUQuota and memory.high cleared |
| Disks out of stress for `STABLE_DISK_IO_PERIOD` (5 min) | Disk channel, half back: `io.max` relaxed to twice |
| Disks out of stress for 10 min | Disk channel, all back: `io.max` removed |

After a half-restore the app's state becomes `partially_restored` and it stays in the Auto Control list; it only leaves once both channels have been fully returned.

There is also the reaper: every `limit_reap_interval` (2s) it sweeps the list and clears leftover limits the instant an app has exited, without waiting for any timer.

<sub>Code: `balancer.py#_tick_separated_restore`, `balancer.py#_restore_channel_for`, `balancer.py#_reap_closed_apps`</sub>

### 9. Network (an independent channel)

Network control is entirely separate from everything above: its own switches, its own pressure thresholds, its own cooldowns.

| | |
|---|---|
| Switches | `enable_network_control` (create the classes), `enable_network_pressure_shaping` (adjust by pressure), `enable_app_pps_police` (small-packet flood policing); all off by default |
| Trigger | Inbound and outbound are judged separately, and only pressure = `critical` leads to shaping |
| What it does | Squeezes the `ceil` of the tc htb classes in order: low first, then high. The critical class is never shaped |
| Recovery | Starts as soon as pressure drops out of critical, in the reverse order: high first, then low, with actual usage deciding between a full and a half restore |
| Rate limiting | 10s cooldown on shaping, 30s on recovery; 5s sampling interval |
| Classification | Controlled apps land in a class by their network priority; unregistered apps all go to low; system ports (22/53/80/443/123) get a dedicated class |

<sub>Code: `network.py#handle_network_pressure`, `network.py#_apply_bandwidth_limit`, `network.py#_recover_network_pressure`</sub>

### The two ways to pause Auto

| Switch | Effect |
|---|---|
| `passive_resource_control.enabled: false` | Automatic monitoring and limiting stop. Existing auto limits are not released — they are handed over wholesale as manual limits. Manual limiting and network control are unaffected |
| Quiet mode (a benchmark is running) | The whole decision block is skipped without touching any config switch. Only queue releases and the reaper keep running |

What the handover does when the switch goes off: no cgroup is written, the limits stay in the kernel exactly as they were, and only ownership flips from auto to manual (releasing everything at once would let the suppressed load surge back). Only apps already registered in the controlled-app list are handed over; unregistered instances stay on auto and are released gradually by the staged restore once pressure eases — the handover does not add apps to the controlled list on the user's behalf.

> A handed-over manual limit is process-lifetime just like any other manual limit: it is lost when the service restarts.

<sub>Code: `balancer.py#lock_to_manual`, `balancer.py#lock_all_auto_to_manual`, `balancer.py#_run_monitor_resource_loop`</sub>

---

# Part II — Manual Control: What the User Can Do

Everything here comes from the dashboard's Balance page, or from calling the REST API directly (full definitions in [API_ENDPOINTS.md](../reference/api.md)).

### Limit and restore

| Action | API | Notes |
|---|---|---|
| Limit by hand | `/app/resource_limit` | Four groups of values, see the table below. `target_cgroups` limits only the named running instances |
| Query the adjustable range | `/app/resource_limit_profile` | Returns current values, bounds and dropdown options; used by the UI dialog |
| Restore | `/app/resource_restore` | Only returns manual limits; auto limits are left alone |
| Query the snapshot | `/app/limit_snapshot` | The values actually written out right now |

What the manual limit dialog lets you set:

| Item | Unit | Default comes from | Adjustable range | Notes |
|---|---|---|---|---|
| CPU | % | `limit_policy.cpu.rate[priority]` | high 10–90, medium 5–70, low 1–50, undefined 1–40 | The percentage is relative to the app's own current usage (`current usage × rate`), not to the whole machine. The bands are per priority and hardcoded; neither the UI nor the yaml can change them |
| Memory | % | `limit_policy.memory.rate[priority]` | high 10–60, medium 5–40, low 1–30, undefined 1–30 | Also relative to the app's own current usage (RSS + swap). Same band rules as above |
| Disk read/write | MB/s | `limit_policy.disk_io.rate[priority]` | Lower bound 1 | An absolute value, independent of the app's current usage. Entering a value above the current one relaxes an existing limit; to remove it entirely, use Restore |
| Disk read/write IOPS | ops/s | Same as above | Lower bound 1 | Same as above |

The manual path derives "current usage" from a peak latch (the highest reading observed this round), so a second sample cannot ratchet the limit tighter and tighter.

<sub>Code: `balancer.py#get_resource_limit_profile`, `balancer.py#_get_limit_rate_bounds`, `balancer.py#_install_resource_limit`</sub>

### Taking over from Auto

This is the only path between auto and manual, and it is designed to leave no release window:

| UI button | API | Applies to | What it does |
|---|---|---|---|
| Lock to manual | `/app/lock_to_manual` | Apps already in the controlled list | Leaves the cgroup limits untouched and transfers ownership to the user. The app moves from Auto Control to Manual Control, and the Limit / Restore / Edit buttons unlock |
| Take Control | `/app/adopt_auto_limit` | Unregistered instances | Registration and takeover in one step, triggered by the user clicking this button (not done automatically): the app is written into the controlled-app list first, then the existing limit is adopted. The end state is the same as the row above |
| Restore | `/app/auto_limit_restore` | Any auto limit | Releases the limit immediately and adds the app to the exclusions, so the next critical tick will not pick it again |
| Query exclusions | `/app/auto_limit_exclusions` | — | Exclusions live in memory only and are cleared when the service restarts |
| Clear an exclusion | `/app/auto_limit_exclusion_remove` | — | Puts the app back in the auto candidate pool |

Two things worth being explicit about:

- Registration is always something the user does. The system never writes an unregistered instance into the controlled list by itself. An auto limit on an unregistered app that the user leaves alone simply stays on auto until the staged restore lets it go.
- Why there is a takeover at all rather than just releasing: releasing outright opens a window under high pressure for the suppressed load to rush back in. The takeover releases nothing — the limit stays in the kernel the whole time, only the owner changes.

<sub>Code: `balancer.py#lock_to_manual`, `balancer.py#adopt_auto_limit`, `balancer.py#restore_auto_limited`</sub>

### Priority and processes

| Action | API | Notes |
|---|---|---|
| Set priority | `/app/set_priority` | `critical`/`high`/`medium`/`low`; also adjusts the OOM score to match |
| Set network priority | `/app/set_network_priority` | Independent of the resource priority; decides the tc class |
| Set OOM score | `/app/set_oom_score` | Sets `oom_score_adj` by hand |
| Kill a process | `/app/kill_process` | |
| Suspend a process | `/app/suspend_process` | |
| Cancel a queued launch | `/app/cancel_relaunch` | Takes the app back out of the pending queue |

### What a priority actually buys you

| Priority | Auto limiting | Launch interception | OOM protection | Default CPU/memory quota |
|---|---|---|---|---|
| `critical` | Exempt | Exempt | Yes | Not configured |
| `high` | Will be limited | Will be held | No | 70% / 30% |
| `medium` | Will be limited | Will be held | No | 50% / 20% |
| `low` | Will be limited | Will be held | No | 40% / 10% |
| `undefined` (unregistered) | Will be limited | Not held (it is not recognised) | No | 30% / 10% |

---

# Part III — Parameter Reference

The "Where" column has three values:

- 🖥 UI — changeable in the Settings dialog, effective immediately
- 📄 yaml — only by editing `config/config.yaml`
- 🔒 code — a hardcoded constant; changing it means changing code

### A. Pressure detection (decides when the machine counts as stressed)

| Parameter | Default | Effect of raising it | Where |
|---|---|---|---|
| `thresholds.{low,medium,high,critical}` | 0.4/0.6/0.8/1.0 | Critical is entered later → limiting is more conservative | 🖥 |
| `weights.{cpu,memory,io}` | 1/7/2 | That dimension counts for more in the total score | 🖥 |
| `mem_gate_steepness` | 8 | A steeper memory pressure curve, narrowing the low→critical transition (above 16 it may skip `medium` entirely, which breaks the staged restore) | 🖥 |
| `memory_busy_threshold` / `cpu_busy_threshold` | 80 / 90 | A looser definition of "busy" | 🖥 |
| `disk_thresholds.{...}` | 0.4/0.6/0.8/1.0 | Disks are judged stressed later | 🖥 |
| `disk_pressure_model.sub_weights` | latency .55 / queue .35 / utilisation .10 | The corresponding sub-signal counts for more | 🖥 |
| `disk_pressure_model.sigmoid_k` | 8.0 | A sharper transition around the half point | 🖥 |
| `disk_pressure_model.max_p_weight` | 0.8 | The busiest disk gets more of a say (below 0.8 the limit may never trigger at all on a multi-disk machine) | 🖥 |
| `disk_pressure_model.{util_half_pct, await_half_ms, queue_half}` | per medium | The same hardware state reads as lower pressure | 📄 |
| `disk_psi_weights.{some,full}` | 0.5 / 3.0 | I/O PSI contributes more to how severe the stall is judged to be | 📄 |
| `network_thresholds.{...}` | 0.3/0.5/0.7/0.9 | The network is shaped later | 🖥 |
| `weights_top.{cpu,memory,gpu}` | 2/7/5 | Changes how top consumers are ranked | 🖥 |

### B. Limit strength (decides how hard the limit bites)

| Parameter | Default | Effect of raising it | Where |
|---|---|---|---|
| `limit_policy.cpu.enabled` | true | Turning it off means CPU is never limited | 🖥 |
| `limit_policy.cpu.rate.{high,medium,low,undefined}` | .7/.5/.4/.3 | More CPU left to the app → a looser limit | 🖥 |
| `limit_policy.memory.enabled` / `.rate` | true / .3/.2/.1/.1 | Same, for `memory.high` | 🖥 |
| `limit_policy.disk_io.enabled` / `.rate` | true / see yaml | Higher read/write MB/s and IOPS ceilings → a looser limit | 🖥 |
| `limit_policy.disk_io.media_scale.{nvme,sata_ssd,mmc,hdd,usb}` | 1.0/.6/.3/.25/.15 | Looser limits on that medium (1.0 is the ceiling; above that it is no longer scaling down but relaxing) | 🖥 |
| `limit_policy.disk_io.candidate_floor[medium].{mb_s,iops}` | see yaml | A higher floor → only heavier I/O users get limited | 🖥 |
| `limit_policy.policy` | `separated` | The only implemented mode; kept as an extension point | 🖥 |

### C. Memory reclaim and swap

| Parameter | Default | Effect of raising it | Where |
|---|---|---|---|
| `memory.reclaim.enabled` | true | — | 📄 |
| `reclaim.target_mem_pct` | 80 | A looser reclaim target, so it stops sooner | 📄 |
| `reclaim.psi_full_scale` | 0.10 | More PSI needed before the throttle opens fully → slower reclaim | 📄 |
| `reclaim.gain_min` / `gain_max` | 0.01 / 0.05 | A larger share reclaimed per round → faster convergence, easier overshoot | 📄 |
| `reclaim.swappiness` | 180 | More willing to swap out anonymous pages (0 = page cache only, which is useless here) | 📄 |
| `reclaim.chunk_mb` | 200 | Larger chunks, so each call blocks for longer | 📄 |
| `reclaim.idle_cpu_threshold` | 0.05 | Busier apps get reclaimed too (the pages come straight back, which is usually a bad trade) | 📄 |
| `reclaim.max_file_cache_fraction` | 0.5 | More tolerant of cache-heavy apps | 📄 |
| `reclaim.cooldown_sec` | 30 | A longer gap between two reclaim rounds on the same app | 📄 |
| `swap_growth.enabled` | true | Turning it off makes L2 stop working before long | 📄 |
| `swap_growth.grow_mem_watermark` / `grow_swap_watermark` | .80 / .50 | Growth happens later | 📄 |
| `swap_growth.grow_swap_critical` | 0.10 | The swap-alone emergency growth triggers later | 📄 |
| `swap_growth.pressure_window_sec` | 30 | The worst value over a longer window → more sensitive but slower to let go | 📄 |
| `swap_growth.step_ratio` / `max_swap_ratio` | .25 / 2.0 | Bigger steps / a higher total cap | 📄 |
| `swap_growth.disk_reserve_pct` / `_min_gb` | 10 / 20 | More headroom left on disk → less room to grow into | 📄 |
| `swap_growth.zswap` / `_compressor` / `_pool_percent` | false / zstd / 20 | In-memory compressed pool | 📄 |
| `swap_growth.path` / `allow_fs` | `/var/lib/smartune/swap` / ext4,xfs | — | 📄 |

### D. Cadence (decides how fast it reacts and how long recovery takes)

| Parameter | Default | Effect of raising it | Where |
|---|---|---|---|
| `regular_update_sys_pressure_time` | 5s | Pressure data refreshes more slowly | 🖥 |
| `monitor_idle_check_interval` | 10s | Sparser decisions (clamped to [2, 30] and never below the item above) | 📄 |
| `limit_reap_interval` | 2s | Leftover limits from exited apps are cleared more slowly | 📄 |
| `cooldown_time` | 15s | A longer gap between repeated notifications | 📄 |
| `STABLE_PERIOD` | 1800s | The system channel recovers more slowly | 🔒 `balancer.py#_MonitorLoopState` |
| `STABLE_DISK_IO_PERIOD` | 300s | The disk channel recovers more slowly (full restore = 2×) | 🔒 `balancer.py#_MonitorLoopState` |
| `network_sample_interval` | 5s | Sparser network sampling | 🔒 `balancer.py#_MonitorLoopState` |
| Network `limit_cooldown` / `recover_cooldown` | 10s / 30s | Less jitter in network shaping and recovery, but slower to react | 🔒 `network.py#NetworkController` |

### E. Master switches and scope

| Parameter | Default | Notes | Where |
|---|---|---|---|
| `passive_resource_control.enabled` | true | The auto master switch. Off = existing auto limits are handed over to manual wholesale, not released | 🖥 |
| `monitored_sections` | all on | Which dimensions are collected in the background; `[]` = purely on demand | 🖥 |
| `controlled_apps` | empty | The controlled-app list (name / id / cmdline / bpf_name / process_names) | 🖥 wizard |
| `blacklist` | system processes | Process-name prefixes that are never candidates | 📄 |
| `enable_network_control` | false | Creates the tc classes | 🖥 |
| `enable_network_pressure_shaping` | false | Adjusts bandwidth by pressure | 🖥 |
| `enable_app_pps_police` | false | Small-packet flood policing | 🖥 |
| `network_pps_policy.{app_pps_high,pps_cap_rate,pps_cap_burst}` | 50000/10000/2000 | Flood detection and rate limiting | 🖥 |
| `config_network_bw[class].{min,max}` | see yaml | Per-class rate / ceil, as a share of the link | 🖥 |
| `network_system_ports` | 22,53,80,443,123 | Ports that get the system class | 🖥 |
| `network_interfaces` | `[]` | Interfaces under control; empty = all of them | 🖥 |

---

# Part IV — I Want X, What Do I Change

### "The limit arrives too late, the machine has already stalled"

- Lower `thresholds.critical` (1.0 → 0.9, say), or raise `weights.memory`
- Lower `monitor_idle_check_interval` (2s minimum) — though note the rising edge into critical never waits for that timer anyway
- For memory specifically, also check `mem_gate_steepness`: too small and the curve is too flat for the score to climb

### "The limit is too aggressive, it is hurting the wrong app"

- Raise `limit_policy.{cpu,memory}.rate` (leave more to the app)
- Move that app to `critical` priority — it becomes exempt from auto limiting and launch interception, and gains OOM protection
- Or hit Restore on it once in the UI, which puts it in the exclusions (though those are lost on restart)
- To stop automatic intervention entirely: `passive_resource_control.enabled: false`

### "Recovery is too slow — pressure is long gone and the limit is still there"

- `STABLE_PERIOD` (30 min) is a code constant; changing it means changing code
- As a one-off, Restore in the UI releases it immediately

### "My HDD / USB stick has been throttled into uselessness"

- Adjust `limit_policy.disk_io.media_scale[medium]` (1.0 is the ceiling)
- Or raise `candidate_floor[medium]` so light I/O never becomes a candidate in the first place

### "io.max has never once triggered on my multi-disk machine"

- Check `disk_pressure_model.max_p_weight`: with N disks the aggregate score is capped at `1 - (1-1/N)(1-max_p_weight)`, so reaching `disk_thresholds.high` requires `max_p_weight ≥ 1 - 0.2N/(N-1)`. 0.8 is the safe lower bound for any number of disks

### "I set a CPU limit by hand and nothing happened"

- First make sure the app's CPU usage was not read as zero: the manual path has a `cpu_usage_percent >= 2` floor, and anything below 2% (as a share of the whole machine) gets no CPU limit written at all
- Then check the log for `Invalid cpu_quota ... must be 1-100`, which means the value was out of range and was dropped. Note that 1–100 here is a share of whole-machine capacity, not a single-core percentage
- To verify a limit really landed, read cgroupfs rather than `systemctl show` — the latter keeps reporting whatever systemd set last, and drift caused by writing cgroupfs directly is invisible there

### "The balancer is interfering with my benchmark numbers"

- Quiet mode handles this automatically: the whole decision block is skipped and no config switch is touched. Nothing to do by hand

### "Network shaping does nothing"

- All three switches are off by default; at minimum `enable_network_control` and `enable_network_pressure_shaping` have to be on
- The network is only shaped at pressure = `critical`, and the `critical` class is never shaped

---

## Appendix — Where the Code Lives

| Topic | File |
|---|---|
| Main decision loop, auto limit/restore, manual limit API implementation | [balancer/balancer/balancer.py](../../balancer/balancer/balancer.py) |
| REST / SSE endpoints | [balancer/balance_service.py](../../balancer/balance_service.py) |
| cgroup writes (CPU/memory/IO) | [balancer/controller/controller.py](../../balancer/controller/controller.py) |
| Governor, swap, reclaim scheduling | [balancer/controller/control_manager.py](../../balancer/controller/control_manager.py) |
| eBPF launch interception | [balancer/controller/app_intercept.py](../../balancer/controller/app_intercept.py) |
| Network tc / iptables | [balancer/controller/network.py](../../balancer/controller/network.py) |
| Pressure score computation | [monitor/pressure.py](../../monitor/pressure.py), [monitor/disk_pressure.py](../../monitor/disk_pressure.py) |
| Config read/write endpoints | [monitor/monitor_api.py](../../monitor/monitor_api.py) |
| Defaults | [config/config.yaml](../../config/config.yaml) |
