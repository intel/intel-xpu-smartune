# Home

[← Back to contents](../README.md)

**Purpose:** answer *"how is the platform doing right now, and is anything under
pressure?"* at a glance. Home is the live overview that refreshes continuously.

<!-- Screenshot: the full Home tab, top to bottom if it fits, otherwise the top
     half showing the Pressure Overview panel and the resource cards. -->
![Home overview](images/home-overview.png)

## Pressure overview

Next to the utilization trend, the **Pressure Overview** panel lists up to three
pressure bars. Each shows a score and a level — **low / medium / high /
critical** — with color, and names the busy disks or NICs when there are any.
These are the signal that drives automatic control.

- **System Pressure** — combines CPU and I/O stall (from Linux PSI) with memory
  scarcity into one composite score.
- **Disk I/O Pressure** — an **independent** channel: a saturated disk raises
  this without inflating the system score, so disk throttling can kick in on its
  own merits.
- **Network Pressure** — a moving-average view of network load (shown when
  network monitoring/control is active).

Use the **Configure pressure thresholds** control on this panel to jump to the
relevant [Settings](settings.md) section if the levels feel too sensitive or too
lax.

> Want to understand exactly how the scores and levels are computed? See the
> [Pressure algorithm deep dive](../design/pressure-algorithm.md).

## Resource details

Below the pressure panel, **Resource Details** cards show live utilization for each
subsystem your server is monitoring:

- **CPU** — overall and per-core style utilization, plus package power.
- **Memory** — used / available, and (where available) memory bandwidth.
- **GPU** — per-card (iGPU/dGPU) frequency, power, per-engine utilization and
  VRAM.
- **NPU** — utilization, power, temperature, frequency, and memory.
- **Disk** and **Network** — throughput and activity.

Use **Configure monitored resources** to choose which of these cards are shown —
handy on a platform without a dGPU or NPU, for example.

## Top resource consumers

The **Top Resource Consumers** panel lists the processes/apps currently using the
most resources. It's the quickest way to see "what is making the machine busy."
For the full controlled-app and process views, open
[Applications](applications.md).

## Workloads & diagnostics

Two summary panels tie Home to the rest of the dashboard:

- **System Diagnostics** — a compact summary of recent events/alerts. Selecting
  it opens the full [System diagnostics](diagnostics.md) tab.
- **Resource Controls** — a summary of what limits SmarTune currently has in
  place. Selecting it opens the [Control center](control-center.md).

## Quick links

Small controls on Home jump you straight to related views:

- **Open history** → [History](history.md)
- **Open system information** → [About](about.md)
- **Configure pressure thresholds / monitored resources** → [Settings](settings.md)

---

Next: **[Applications](applications.md)** — see and manage the apps and processes
behind those numbers.
