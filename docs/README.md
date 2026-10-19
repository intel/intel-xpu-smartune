# SmarTune documentation

Pick the section that matches what you're trying to do.

| I want to… | Go to |
| --- | --- |
| Install and run SmarTune | [Install](#install) |
| Learn to use the dashboard | [User guide](#user-guide) |
| Call the REST API | [Reference](#reference) |
| Understand how and why it behaves the way it does | [Design & internals](#design--internals) |

## Install

[Install guide](install.md) — run from source (dependencies, backend, dashboard dev server, systemd), the Debian package (build, install, launch, uninstall), and building native dependencies on other platforms.

## User guide

Using the dashboard, one tab at a time. Start with *Getting started*.

1. [Getting started](user-guide/getting-started.md) — requirements, the three ways to start, log in, and a tour of the dashboard
2. [Home](user-guide/home.md) — the live platform overview and Pressure Overview (system, disk I/O, network)
3. [Applications](user-guide/applications.md) — resource consumers, processes, and adding an app to be controlled
4. [Control center](user-guide/control-center.md) — priority, resource limits, keep-alive, the launch queue, and restoring limits
5. [Models (Benchmark)](user-guide/models.md) — download and benchmark AI models and compare results
6. [System diagnostics](user-guide/diagnostics.md) — the event timeline, alerts, logs, and advice
7. [History](user-guide/history.md) — long-term pressure and utilization trends
8. [Settings](user-guide/settings.md) — pressure thresholds, control policies, and diagnostic rules
9. [About](user-guide/about.md) — hardware and software inventory
10. [Common workflows](user-guide/workflows.md) — a few end-to-end tasks
11. [Troubleshooting & FAQ](user-guide/troubleshooting.md)

## Reference

| Document | What it covers |
| --- | --- |
| [Backend API](reference/api.md) | Core REST endpoints (`/auth`, `/smartune`, `/app`, `/monitor`) |
| [Benchmark API](../benchmark/API_benchmark.md) | `/bench` endpoints (optional feature) |
| [Diagnostics API](../diagnostics/API_diagnostics.md) | `/diag` endpoints (optional feature) |

## Design & internals

| Document | What it covers |
| --- | --- |
| [Architecture & repository layout](design/architecture.md) | Components, how they fit together, and the source tree |
| [Balancer guide](design/balancer.md) | Every scenario the balancer acts on under Auto and Manual control, what it adjusts, and all tunable parameters |
| [Pressure algorithm](design/pressure-algorithm.md) | How the system and disk I/O pressure scores and levels are computed |
| [Disk I/O under pressure](design/disk-io-throttle-results.md) | Measured effect of `io.max` caps on real throughput |
| [Balancer README](../balancer/README.md) | Resource governance & app priority management |
| [Dashboard README](../dashboard/README.md) | Front-end development |
