# Diagnostics API Guide

REST reference for the `/diag/*` endpoints served by [`diag_api.py`](diag_api.py).

These are read-mostly endpoints over the event ledger, the derived alerts, the unified
log-query façade and the context assembler. They are mounted only when the diagnostics
feature is present in the deployment — check `GET /smartune/capabilities` (`diagnostics: 1`)
before offering the tab.

Base URL, TLS, authentication and the unified `{retcode, retmsg, data}` envelope are the
same as the rest of the product; see the [Backend API Guide](../docs/reference/api.md) for
those and for the shared `RetCode` table. Auth is applied app-wide by `smartune_api`'s
`before_app_request` gate, so none of these routes carries a per-route token check.

---

## Overview

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/diag/event-catalog` | GET | Built-in diagnostic event metadata for Settings |
| `/diag/events` | GET | Filtered operational-event query |
| `/diag/alert-policies` | GET | Which event types create alerts and how each closes / escalates |
| `/diag/alerts` | GET | Derived alerts, active-only by default |
| `/diag/alerts/<dedup_key>/acknowledge` | POST | Acknowledge one active alert (closes one-off faults) |
| `/diag/alerts/<dedup_key>/close` | POST | Close one active alert by hand, whatever its policy |
| `/diag/alerts/<dedup_key>/silence` | POST | Silence one active alert for 30 or 120 minutes |
| `/diag/control-lifecycles` | GET | Resource-limit lifecycles reconstructed from events |
| `/diag/control-lifecycles/clear-without-runtime-state` | POST | Close an orphaned lifecycle |
| `/diag/sources` | GET | Log source health, event writer backlog and failed event writes |
| `/diag/retention` | GET | Retention coverage per evidence source |
| `/diag/logs` | GET | Federated, redacted log query across sources |
| `/diag/boots` | GET | Recent boot sessions for the log selector |
| `/diag/context` | GET | On-demand context assembly for root-cause analysis |
| `/diag/monitor-metrics` | GET | Monitor samples over a time range |
| `/diag/digest` | GET | Day / week / boot / range diagnostics digest |

Common conventions:

- `from` / `to` are **epoch seconds**.
- `limit` / `offset` are integers; a non-numeric value silently falls back to the default
  rather than erroring.
- Boolean query parameters accept `1` / `true` / `yes` (and their negations) case-insensitively.

---

## Events

### GET /diag/event-catalog

**Purpose:** List every built-in diagnostic event type with what it means: Settings renders
the per-event enable switches from it, and a client (the MCP tools, an agent) learns which
attributes each type carries instead of guessing them from samples.

**Request:** No parameters.

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "events": [
      {
        "event_type": "CONTROL_APP_FROZEN_LONG",
        "label": "App frozen too long",
        "description": "An app has stayed frozen longer than the reminder threshold.",
        "domain": "platform",
        "configurable": true,
        "default_enabled": true,
        "lifecycle_action": null
      }
    ]
  }
}
```

**Notes:**
- `configurable: false` marks an event that is always recorded (alerts or read models depend
  on it); the others can be turned off in Settings.
- `lifecycle_action` is what the event does to the resource-limit lifecycle of its
  `protection_id`: `applied` / `recovered` / `failed` for one resource, or a marker on the
  whole lifecycle (`requires_verification`, `cleared_by_reboot`,
  `cleared_without_runtime_state`). `null` for every other event type. The lifecycle is
  rebuilt from this declaration, never from the event name.

---

### GET /diag/events

**Purpose:** Filtered query over the operational-event ledger. Every filter is optional and
they are combined with AND.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | event_id | No | string | A single event by id |
| Query | severity | No | string | Exactly this severity: `info` / `warning` / `error` / `critical` |
| Query | min_severity | No | string | This severity and above, e.g. `warning` also returns `error` and `critical`. Any other value is an argument error. With `keep_important`, `limit` bounds the whole result and `truncated` says more matched |
| Query | category | No | string | Event category |
| Query | source | No | string | Emitting subsystem |
| Query | event_type | No | string | Catalogue event type |
| Query | app_id | No | string | Scope to one app |
| Query | job_id | No | string | Scope to one benchmark job |
| Query | impact | No | string | Impact classification |
| Query | resource_type | No | string | `cpu` / `memory` / `disk_io` / … |
| Query | protection_id | No | string | Scope to one control lifecycle |
| Query | episode_id | No | string | Scope to one pressure episode |
| Query | boot_id | No | string | Scope to one boot session (32 hex digits, from `/diag/boots`) |
| Query | keyword | No | string | Free-text match |
| Query | alertable | No | bool | `true` keeps only event types that have an alert policy (default `false`) |
| Query | keep_important | No | bool | `true` returns every `warning`/`error`/`critical` event in range and applies `limit` only to lower severities, so a burst of info events cannot push an older fault out of a window view. The response then carries `truncated` (lower-severity events were left out). Takes `offset` only together with `severity` or `min_severity`; otherwise a non-zero `offset` is an argument error (default `false`) |
| Query | from | No | int | Window start (epoch seconds), matched against when the event **occurred** |
| Query | to | No | int | Window end (epoch seconds), matched against when the event **occurred** |
| Query | limit | No | int | Max rows (default `200`) |
| Query | offset | No | int | Rows to skip (default `0`) |

> `from` / `to` and the newest-first order use the event's occurrence time (`ts_utc`), not the
> time it was written. Events a scanner recovers after the fact (journal OOM kills, log errors
> found on the next 30 s scan or after a service restart) therefore appear where they happened.

**Request Example:**
```
GET /diag/events?severity=warning&app_id=com.example.app&from=1755760000&to=1755770000&limit=50
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "events": [
      {
        "event_id": "3f9a1c0d2b7e",
        "ts_utc": "2026-11-18T09:20:01.412+00:00",
        "severity": "info",
        "severity_origin": "producer",
        "category": "platform.control",
        "event_type": "CONTROL_CPU_LIMIT_APPLIED",
        "summary": "Cpu limit applied for ollama",
        "source": "balancer",
        "service": null,
        "app_id": "com.example.app",
        "job_id": null,
        "impact": "degraded",
        "resource_type": "cpu",
        "protection_id": "9c1e...",
        "episode_id": null,
        "lifecycle_action": "applied",
        "attributes": {"app_name": "ollama", "control_mode": "auto", "boot_id": "..."},
        "acknowledged_at": null,
        "boot_id": "...",
        "config_revision_id": "..."
      }
    ],
    "count": 1
  }
}
```

**Notes:**
- `count` is the number of rows in this page, not the total matching the filter.
- `severity` is the producer's judgement of this one observation. An alert raised from the
  event has its policy's severity instead (see `GET /diag/alert-policies`), possibly escalated,
  and advice its rule's: an OOM kill is an `error` event and a `critical` alert.
- `lifecycle_action` is set only on an event of a limit lifecycle (it has a `protection_id`);
  see `GET /diag/event-catalog`.

---

## Alerts

### GET /diag/alerts

**Purpose:** List derived diagnostics alerts. Defaults to the currently active ones.

Only the event types in `ALERT_POLICIES` (`diagnostics/alerts.py`), and custom rules whose
alert is on, create alerts (`GET /diag/alert-policies` lists them). Repeated events with the
same event type and scope are merged into one alert, identified by
`dedup_key` = `<EVENT_TYPE>::<scope>`, where scope is the first of `protection_id`,
`job_id`, `app_id`, `service`, `attributes.subject`, `source` that is set.

When an alert should notify -- a new occurrence, a detection past the 300 s cooldown, or a
severity escalation (pushed at once, inside the cooldown too) -- and is neither acknowledged nor
silenced, the balancer service pushes it on its `/app/events` stream as
`{"purpose": "notify", "status": "diag_alert", "alert": {dedup_key, event_type, label,
severity, summary, scope, fire_count, last_event_id, last_fired_at}, ...}`.

Each alert row is one **occurrence** (active → resolved). Active occurrences come from
`AlertState`. Resolved ones come from `AlertEpisode`, which keeps one row per occurrence,
so the same `dedup_key` can appear several times in history.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | active | No | bool | `false` / `0` / `no` also returns resolved occurrences (default `true`) |
| Query | notifyable | No | bool | `true` restricts to active alerts that are neither acknowledged nor silenced (default `false`) |
| Query | from | No | int | Window start (epoch seconds) |
| Query | to | No | int | Window end (epoch seconds) |
| Query | limit | No | int | Max rows (default `100`) |

**Request Example:**
```
GET /diag/alerts?active=false&from=1792998000&to=1793001600&limit=100
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "alerts": [
      {
        "episode_id": "3f6b0c8e9a2d5b1e7c4f0a9d8e6b2c1a",
        "dedup_key": "CONTROL_MEMORY_LIMIT_FAILED::auto:com.example.app:1792998123",
        "event_type": "CONTROL_MEMORY_LIMIT_FAILED",
        "summary": "Failed to apply memory limit for com.example.app",
        "scope": "auto:com.example.app:1792998123",
        "severity": "error",
        "status": "resolved",
        "first_fired_at": "2026-10-29T09:12:00+00:00",
        "last_fired_at": "2026-10-29T09:14:30+00:00",
        "fire_count": 3,
        "last_event_id": "a1b2c3d4e5f6...",
        "acknowledged_at": null,
        "silenced_until": null,
        "silence_reason": null,
        "resolved_at": "2026-10-29T09:20:05+00:00",
        "resolved_event_id": "f6e5d4c3b2a1...",
        "resolved_reason": "recovered",
        "severity_origin": "producer",
        "baseline_severity": null,
        "escalation_rule_id": null,
        "escalation_rule_version": null,
        "escalation_reason": null,
        "escalation_evidence": null,
        "escalated_at": null
      }
    ],
    "count": 1
  }
}
```

**Notes:**
- `status` is `active` or `resolved`. `episode_id` is set only for resolved occurrences
  (`null` for active ones); use it as the row key for history.
- With `from` / `to`, only occurrences whose span `[first_fired_at, resolved_at or now]`
  overlaps the window are returned, and `limit` applies after that filter.
- Results are ordered by `last_fired_at`, newest first.
- `fire_count` is the number of detections in this occurrence; it starts again from `1`
  when a resolved alert fires again.
- `acknowledged_at` belongs to the current occurrence: a new occurrence or a severity
  escalation clears it. `silenced_until` holds until it passes, even when the alert closes
  and fires again in between, so a flapping alert stays quiet for the window asked for. A
  new occurrence also starts at the producer severity, without the previous escalation.
- `resolved_reason` says what closed a resolved occurrence, recorded when it closed:
  `acknowledged`, `recovered` (a recovery event), `expired` (its lease ran out),
  `operator_closed` (the close endpoint below) or `cleared_by_reboot` (a failed resource
  limit whose boot is gone). `null` while active.
- All timestamps are UTC (`+00:00`).
- `dedup_key` can contain `/` (a `kworker/…` task, a `gpu:<pci>/<gt>` subject).
  URL-encode it in the path; the routes accept it either way.
- `count` is the number of rows returned.

---

### GET /diag/alert-occurrences

**Purpose:** Each detection behind one alert occurrence: the events of its type and scope
within its span, newest first. `fire_count` says how many there were; this says when.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | event_type | Yes | string | The alert's `event_type` |
| Query | scope | Yes | string | The alert's `scope` |
| Query | from | No | int | Start of its span (epoch seconds): its `first_fired_at` |
| Query | to | No | int | End of its span (epoch seconds): its `resolved_at`, or now while active |
| Query | limit | No | int | At most this many events (default 200, max 500) |

**Response:** `{"events": [<event>, ...], "count": N, "truncated": false}`. Events have the
`GET /diag/events` shape. `truncated` is set when more detections matched than were returned.
Events older than the retention period are gone, so an old alert can list fewer detections
than its `fire_count`.

---

### GET /diag/alert-policies

**Purpose:** Which event types create alerts, and how each alert closes and escalates: the
one definition the dashboard renders (it keeps no copy of its own).

**Request:** No parameters.

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "policies": [
      {
        "event_type": "CONTROL_CPU_LIMIT_FAILED",
        "label": "CPU limit failed",
        "source": "builtin",
        "severity": "error",
        "close_mode": "ack",
        "ttl_seconds": null,
        "resolved_by": [],
        "escalation": null,
        "description": "Applying or restoring a CPU limit failed. A one-off fault: check the app's cgroup, then acknowledge it to close it. A reboot also closes it.",
        "closes_when": "Closes when acknowledged",
        "closed_because": "Acknowledged"
      }
    ],
    "cooldown_seconds": 300
  }
}
```

**Notes:**
- Built-in policies come first, in `ALERT_POLICIES` order, then the custom rules whose alert is
  on (`source: "custom_rule"`, `event_type: "CUSTOM_RULE::<id>"`, `label` = the rule name).
- `close_mode`: `ack` (acknowledging closes it), `quiet` (closes `ttl_seconds` after the last
  detection; a detection within another `ttl_seconds` reopens the same occurrence) or `recovery` (an event of a `resolved_by` type with the same scope closes it).
- `escalation`, when set: `{escalate_to, threshold, window_seconds, attribute,
  attribute_threshold}` -- `threshold` detections within `window_seconds` of the first, or one
  event whose `attributes[attribute]` reaches `attribute_threshold`, raise the severity.
- `severity` is the alert's, not the event's: see the notes of `GET /diag/events`.

---

### POST /diag/alerts/&lt;dedup_key&gt;/acknowledge

**Purpose:** Acknowledge one active alert.

For one-off faults that have no recovery event (`PLATFORM_KERNEL_PANIC`,
`RESOURCE_MEMORY_OOM_KILL`, `DEVICE_GPU_HANG`, `CONTROL_*_LIMIT_FAILED`, i.e. policies with
`close_on_ack`),
acknowledging also **closes** the alert: it is resolved with `resolved_event_id = null` and
archived as a resolved occurrence. For every other type the alert stays active and is only
marked as handled until its recovery condition is met.

**Request:** `dedup_key` in the path. Optional body:

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | last_event_id | No | string | The alert's `last_event_id` as the client shows it |

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {"acknowledged": true, "closed": false}
}
```

**Notes:**
- `closed` is `true` when the acknowledgement also closed the alert.
- Returns `RetCode.ARGUMENT_ERROR` with `Active alert not found` when the key matches no
  active alert — including when it matches an alert that has already resolved.
- With `last_event_id`, returns `RetCode.CONFLICT` (409) and changes nothing when the
  alert was detected again since (its `last_event_id` moved on); `data` carries the current
  `fire_count` and `last_event_id`. Acknowledging would otherwise also close faults the
  operator never saw. The dashboard always sends it; reload and act again.

---

### POST /diag/alerts/&lt;dedup_key&gt;/close

**Purpose:** Close one active alert by hand, whatever closes it normally. For an alert
whose recovery can no longer be recorded: the condition ended while the service was down,
or the device or protection it is about is gone.

**Request:** `dedup_key` in the path; optional body `last_event_id`, as for acknowledge
(a stale one returns `RetCode.CONFLICT`).

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {"closed": true}
}
```

**Notes:**
- The occurrence is archived with `resolved_reason = "operator_closed"` and is marked
  acknowledged. If the fault is detected again, a new occurrence opens.
- Returns `RetCode.ARGUMENT_ERROR` with `Active alert not found` when the key matches no
  active alert.

---

### POST /diag/alerts/&lt;dedup_key&gt;/silence

**Purpose:** Suppress one active alert for a fixed period.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Path | dedup_key | Yes | string | Alert deduplication key |
| Body | minutes | Yes | int | **Must be exactly `30` or `120`** |

**Request Example:**
```json
{
  "minutes": 30
}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {"silenced_until": "2026-10-13T10:12:00+00:00"}
}
```

**Notes:**
- Any other value for `minutes` (including a valid-looking `60`) returns
  `RetCode.ARGUMENT_ERROR` — the UI offers exactly two durations and the server enforces
  that rather than accepting an arbitrary window.
- The reason is recorded as `operator_silenced`.

---

## Control lifecycles

### GET /diag/control-lifecycles

**Purpose:** List resource-limit lifecycles reconstructed from the event ledger — one entry
per `APPLIED … RECOVERED` span, so an operator can see what was limited, why, and whether it
ever came back.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | from | No | int | Window start (epoch seconds) |
| Query | to | No | int | Window end (epoch seconds) |
| Query | app_id | No | string | Scope to one app |
| Query | protection_id | No | string | Scope to one lifecycle |
| Query | limit | No | int | Max rows (default `200`) |
| Query | offset | No | int | Rows to skip (default `0`) |

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "lifecycles": [
      {
        "protection_id": "auto:com.example.app:1755764400.12",
        "app_id": "com.example.app",
        "app_name": "example",
        "control_mode": "auto",
        "resources": ["cpu", "memory"],
        "applied_at": 1755764400,
        "recovered_at": 1755766200,
        "state": "recovered"
      }
    ],
    "count": 1
  }
}
```

---

### POST /diag/control-lifecycles/clear-without-runtime-state

**Purpose:** Close a lifecycle that was left open by an interrupted run (service restart
mid-limit) once the kernel has been checked and holds nothing recoverable.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | protection_id | Yes | string | Lifecycle to close |

**Request Example:**
```json
{
  "protection_id": "auto:com.example.app:1755764400.12"
}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {"cleared": true}
}
```

**Notes:**
- Applies to a lifecycle that is `active` or `requires_verification` (the status the
  startup reconcile gives a lifecycle a restart interrupted) and kept no cgroup target.
- Returns `RetCode.ARGUMENT_ERROR` with `Control lifecycle still has recoverable state`
  when the limit is in fact still live. Probe first with
  `POST /app/interrupted_limit_status` and restore it through the normal path instead of
  clearing the record.
- A cleared lifecycle counts as resolved, so retention can prune it once it ages out.

---

## Evidence sources

### GET /diag/retention

**Purpose:** Report how far back each diagnostics evidence source actually reaches, so the
UI can grey out a time range nothing can answer for.

**Request:** No parameters.

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "events": {"oldest": 1755000000, "retention_days": 30},
    "monitor": {"oldest": 1755600000, "retention_days": 7},
    "logs": {"oldest": 1755700000, "source": "journald"}
  }
}
```

---

### GET /diag/logs

**Purpose:** Unified, read-only, federated log query across every configured source. Values
are redacted before they are returned.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | source | No | string | Source name; repeatable or a comma-separated list. Omitted = every source |
| Query | from | No | int | Window start (epoch seconds) |
| Query | to | No | int | Window end (epoch seconds) |
| Query | level | No | string | Minimum log level |
| Query | job_id | No | string | Scope to one benchmark job. The `benchmark` source then returns the last lines of the run's log; `from`/`to` do not apply to them, since each line carries only the log file's modification time. Without `from` / `to`, the missing bounds are taken from the job's events (its run, plus a minute before), so `journal` lines cover the job's time range |
| Query | boot_id | No | string | Scope to one boot session (32 hex digits, from `/diag/boots`) |
| Query | keyword | No | string | Free-text match |
| Query | kernel_only | No | bool | journald scope: `true` = kernel ring buffer only (OOM, GPU reset, driver faults); `false` adds userspace units such as systemd restart loops. Other sources ignore it (default `true`) |
| Query | limit | No | int | Max records (default `500`, max `5000`). A merged kernel report counts as one record. When several sources have more than their share, each keeps at least an equal share of the limit |

**Kernel reports.** The kernel prints one journal entry per line, so a hung task,
WARN/BUG/Oops with its Call Trace, an OOM dump or a segfault with its `Code:` line
arrives as many entries. `/diag/logs` returns each such report as **one** record:

- `message` holds every line of the report, joined with `\n`.
- `level` is the most severe level of any line.
- `ts_epoch` and `fields.cursor` are those of the first line.
- `fields.block` gives the report kind: `hung_task`, `warning`, `bug`, `oops`, `gpf`, `panic`, `oom`, `segfault`, `trap`, `soft_lockup`, `hard_lockup`, `rcu_stall`, `mce`, or `stack` (a driver line followed by a stack dump).
- `fields.block_title` is the headline line.
- `fields.line_count` is the number of lines in the report.
- `fields.block_truncated` is set when the report hit the 400-line cap.

Lines are grouped only when they are kernel lines from the same boot, printed at most
0.5 s apart.

In the kernel view (`kernel_only=true`), `level` and `keyword` are applied after the
lines are merged. A `level=error` search therefore returns whole hung-task reports,
including their INFO-priority trace lines, and a keyword that appears only in a trace
frame finds the report it belongs to. With `kernel_only=false` both filters are passed
to journalctl, so kernel reports may come back partial.

Every kernel record also carries `fields.monotonic_s` (the dmesg timestamp). Kernel
lines prefixed with a PCI device (`xe 0000:00:02.0: ...`) also carry
`fields.kernel_driver` and `fields.kernel_pci`.

**Request Example:**
```
GET /diag/logs?source=smartune,journal&level=error&from=1755760000&to=1755770000
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "records": [
      {
        "ts_epoch": 1755764400.12, "ts_iso": "2025-08-21T08:20:00.120000+00:00",
        "level": "ERROR", "source": "journal", "logger": "kernel",
        "message": "INFO: task python3:4211 blocked for more than 122 seconds.\n      Tainted: G ...\nCall Trace:\n <TASK>\n ...",
        "service": null, "app_id": null, "job_id": null,
        "fields": {"transport": "kernel", "monotonic_s": 5806683.977043, "block": "hung_task",
                   "block_title": "INFO: task python3:4211 blocked for more than 122 seconds.",
                   "line_count": 39, "block_truncated": false, "boot_id": "...", "cursor": "..."}
      }
    ],
    "sources": ["smartune", "journal"],
    "failed_sources": [],
    "available_sources": ["smartune", "journal", "benchmark"],
    "truncated": false,
    "count": 1
  }
}
```

- `truncated` is `true` when any source had more matching records than were returned, or
  the journal read its whole raw-line budget (older entries in the window were not seen).
  Narrow the window or the filters to see the rest.
- `failed_sources` lists sources that could not be read (for example journalctl timed out
  or failed). No records from such a source means nothing about the logs it holds.

**Journal event types.** The journal detector matches each kernel report once, using
the printk formats of the kernel subsystems that emit it. It parses only what decides the
event, its severity and its subject; the report itself travels in `attributes.excerpt` for
deeper analysis (the call trace, taint flags, the faulting task).

| Event type | From | Alert |
|---|---|---|
| `PLATFORM_KERNEL_HUNG_TASK` | `INFO: task X:PID blocked for more than N seconds` | error, closed by ack |
| `PLATFORM_KERNEL_OOPS` | `BUG: unable to handle ...`, `kernel BUG at`, `Oops:`, kernel general protection fault. Subject = faulting function and module | critical, closed by ack |
| `PLATFORM_KERNEL_LOCKUP` | soft/hard lockup, RCU stall (including expedited), workqueue lockup | critical, closed by ack |
| `PLATFORM_KERNEL_WARNING` | `WARNING: ... at file.c:N`, plus survivable `BUG:` reports such as "sleeping function called from invalid context" | none |
| `PLATFORM_PROCESS_CRASHED` | user-space `segfault at`, `traps:`, a `systemd-coredump` record (which carries the unit and cgroup), or systemd's `<unit>: Main process exited, code=dumped` (or `code=killed` by SEGV / ABRT / BUS / FPE / ILL / TRAP / SYS) | none |
| `DEVICE_HARDWARE_ERROR_UNCORRECTED` / `_CORRECTED` | machine check (`mce: [Hardware Error]`; with several banks in one report, the worst MCi_STATUS decides), PCIe AER, EDAC CE/UE | uncorrected only: critical, closed by ack |
| `DEVICE_STORAGE_IO_ERROR` | block-layer `... error, dev X, sector N`, `Buffer I/O error`, EXT4/XFS/BTRFS errors, read-only remount, NVMe timeout or reset, ATA exceptions | error, closed by ack |
| `DEVICE_CPU_THERMAL_THROTTLED` | `CPUn: Core/Package temperature above threshold, cpu clock throttled` | none |
| `RESOURCE_MEMORY_OOM_KILL` | the kernel's `Out of memory: Killed process`, or systemd-oomd's `Killed <cgroup> due to memory pressure / memory used`. `attributes.killer` = `kernel` / `systemd-oomd` | critical, closed by ack |
| `PLATFORM_HOST_UNCLEAN_SHUTDOWN` | systemd-journald finding a journal file left open (`... uncleanly shut down, renaming and replacing`): the host stopped without a clean shutdown before this boot. One event per boot | error, closed by ack |

Repeats of the same type on the same `attributes.subject` within the same boot are
rate-limited on event time:

- Hung tasks, Oops, lockups, uncorrected errors and thermal: 1 event per 10 minutes.
- Kernel WARNs: 1 event per hour.
- Corrected errors and storage I/O errors: 3 events per 10 minutes.
- Crashes and GPU faults: 5 events per 10 minutes.

Each recorded event carries `attributes.occurrences` and `attributes.suppressed_since_last`.
A crash seen by more than one source (the kernel line, systemd-coredump, systemd) is recorded
once per source.

`PLATFORM_SYSTEMD_RESTART_LOOP` is recorded at most once per minute per unit while the
loop lasts. It carries `restart_count` and `restarts_since_last_event`. When the systemd
restart counter goes down, a new loop has started and is recorded immediately. The loop's
alert escalates to critical when `restart_count` reaches 10.

**Error signatures.** Error-level lines from SmarTune's own log (ERROR / CRITICAL) and
from the journal (PRIORITY 0..3, any unit) that no built-in or custom rule recognised are
grouped by template. The template is the first line, plus the exception line for a
traceback, with UUIDs, hex values, PCI and MAC addresses, ids of 7+ hex characters, IPv4 /
IPv6 addresses, paths, quoted values and numbers replaced by `<ID>`, `<HEX>`, `<PCI>`,
`<MAC>`, `<IP>`, `<PATH>`, `'<S>'` and `<N>`. A number is replaced whole or not at all; one
glued to a name stays part of the name (`sqlite3`, `nvme0n1`, `c17`) unless it has 5+
digits (`pid12345`). Per template fingerprint (sha1 of source, origin and template, 16 hex
digits) the ErrorSignature table keeps `first_seen`, `last_seen` and `total_count` for 30
days.

| Event type | When | Alert |
|---|---|---|
| `LOG_ERROR_SIGNATURE_NEW` | First occurrence of a fingerprint on this host. At most 10 per hour; the next one carries `new_signatures_suppressed`, and a template held back by the cap is reported when it recurs. None while learning: the first hour from the first scan of a fresh install, kept across restarts, plus 10 minutes of the first boot after it (errors that occur at every boot) | none: evidence for analysis |
| `LOG_ERROR_SIGNATURE_BURST` | At least 10 occurrences in 300 s (counted in 10 s buckets) and at least 5× the expected count, where expected = earlier occurrences / max(age, 1 h) × 300 s. At most one event per 300 s per fingerprint and per component; `other_templates_bursting` counts the others of the same component | error, lease of 600 s |

Both carry `attributes.fingerprint`, `template`, `sample`, `origin` (the logger, the journal
identifier, `systemd:<unit>` for systemd's messages about a unit, or `kernel` /
`kernel:<driver>`), `log_source`, `excerpt`, `total_count` and
`first_seen_epoch`. Bursts also carry `count_in_window`, `expected_in_window` and
`window_seconds`. `attributes.subject` is `<log_source>:<origin>`, so the burst alert is per
component and a cascade of different messages from one failure is a single alert.
`LOG_ERROR` / `LOG_EXCEPTION` events carry the `fingerprint` of their line.

**Custom rule alerts.** A rule in `diagnostic_rules` raises an alert for its
`CUSTOM_RULE::<id>` events when `alert_mode` is set:

- `off`: the default; no alert.
- `until_ack`: closed by acknowledging it.
- `until_quiet`: a lease that each match renews, closed after `alert_quiet_minutes` without a match (1..1440, default 10).

The alert severity is the rule's `severity`, which must then be `warning` or higher. The
policy is read from the current configuration, so `GET /diag/events?alertable=true` and
alert expiry follow configuration changes without a restart. Each alert in the alert
responses carries `close_mode`: `ack`, `quiet`, `recovery` or `null`.

Journal events detected from a line inside a kernel report (`RESOURCE_MEMORY_OOM_KILL`,
`DEVICE_GPU_HANG`, `PLATFORM_KERNEL_PANIC`, custom journal rules) carry the whole report
in `attributes.excerpt` / `attributes.raw_message`, up to 4000 characters. Longer
reports keep their head and tail. Such events also carry `attributes.kernel_block`,
`attributes.block_lines` and `attributes.block_cursor`.

---

### GET /diag/boots

**Purpose:** List recent boot sessions, newest first, for the Logs boot-session selector.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | limit | No | int | Max sessions (default `15`) |

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "boots": [
      {
        "boot_id": "ddd13f42d69b45df847e0f43688a536a",
        "index": 0,
        "first_ts": 1755700000,
        "last_ts": 1755770000,
        "running": true,
        "source": "journald"
      }
    ]
  }
}
```

**Notes:**
- Returns an empty list when no boot-aware source (journald) is present. That is a normal
  state, not an error.
- `boot_id` is the value events carry in `boot_id`, so it can be passed to `/diag/events`,
  `/diag/logs` and `/diag/digest?period=boot`.

---

## Analysis

### GET /diag/context

**Purpose:** On-demand context assembly for root-cause / "why was this slow" analysis.
Returns structured, redacted evidence even when the window contains zero errors — metrics
and control actions still explain a slow-but-not-broken run.

**Request:** Exactly one scope must be given: `job_id`, or `app_id`, or a bare `from`/`to`
window.

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | job_id | No* | string | Scope to one benchmark job |
| Query | app_id | No* | string | Scope to one app |
| Query | from | No* | int | Window start (epoch seconds) |
| Query | to | No* | int | Window end (epoch seconds) |
| Query | findings_only | No | bool | `true` returns just the findings list; **requires `from` and `to`** |
| Query | detail | No | string | `full` returns the raw monitor snapshots in `metrics.monitor.series` |

\* `job_id` wins over `app_id`, which wins over the bare window. With none of them, `from`
and `to` are both required.

**Request Example:**
```
GET /diag/context?job_id=bench-20261013-0912
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "scope": {"kind": "job_id", "value": "bench-20261013-0912"},
    "window": {"from": 1755764400, "to": 1755766200},
    "findings": [],
    "events": [],
    "control_actions": [],
    "service_lifecycle": [],
    "metrics": {
      "monitor": {"series": [{"ts_epoch": 1755764405, "data": {
        "cpu": {"usage_total": 61.0, "temperature_c": 78.0, "package_tjmax_c": 100.0},
        "memory": {"usage_percent": 72.5, "available_gb": 8.6, "swap_usage_percent": 0.0},
        "pressure": {"score": 0.42, "level": "medium", "cpu": 0.3, "memory": 0.1, "io": 0.0,
                     "network_pressure_pct": 4.0},
        "disk": {"pressure_pct": 12.0}}}],
        "count": 240, "detail": "summary", "sampled_from": 360, "step_seconds": 7},
      "benchmark": {}
    }
  }
}
```

**Notes:**
- `metrics.monitor.series` is a summary by default: at most 240 points with the fields
  shown above. Each point merges a time bucket keeping the worst value (highest load and
  pressure, lowest available memory), so a short spike is not averaged away.
  `sampled_from` is the number of raw samples behind it. Findings are computed on the raw
  series either way; `detail=full` returns the raw snapshots (much larger).
- `findings_only=true` without both `from` and `to` returns `RetCode.ARGUMENT_ERROR` with
  `findings_only requires from & to`.
- No scope and no window returns `provide one of job_id/app_id, or from & to`.
- `control_actions` lists what SmarTune did in the window: `platform.control` events
  (limits, freeze, CPU affinity, frequency caps) and memory reclaim
  (`RESOURCE_MEMORY_RECLAIM_*`). Refused requests (`impact: refused`) are left out because
  they changed nothing. For an app scope it keeps that app's actions; otherwise every app's.
  `events` holds these actions too.
- `alerts` in the full context lists the alert occurrences that overlap the evidence window
  (active or since resolved), not whatever is active now.
- `service_lifecycle` holds the SmarTune start / stop / crash events from 60 s before the
  window to 60 s after it, oldest first, whatever the scope. They are info-level host
  events that `events` leaves out for a job or app scope, and they tell a telemetry gap
  caused by downtime from one where the collector stalled (finding
  `incomplete_diagnostics_window`).

---

### GET /diag/monitor-metrics

**Purpose:** Return monitor samples for a selected diagnostics time range.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | from | Yes | int | Window start (epoch seconds) |
| Query | to | Yes | int | Window end (epoch seconds); must be later than `from` |
| Query | summary | No | string | `resource-utilization` returns the aggregated view instead of raw samples |

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "monitor": {
      "samples": [
        {"ts": 1755764400, "cpu": 72.1, "memory": 84.6, "pressure": "high"}
      ]
    }
  }
}
```

**Notes:**
- Both `from` and `to` are mandatory; either missing gives `from and to are required`, and
  `to <= from` gives `to must be later than from`.

---

### GET /diag/sources

**Purpose:** Report whether diagnostics is taking everything in: can the log detector read
its sources (while a source fails, faults recorded there -- kernel and service faults for
`journal`, SmarTune's own errors for `smartune` -- are not detected), is the background
event writer keeping up, and are events reaching the database.

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "sources": [
      {"source": "journal", "last_ok": null, "failing_since": 1792998000.2,
       "last_error": "journalctl scan failed (failed)", "consecutive_failures": 12},
      {"source": "smartune", "last_ok": 1792998300.9, "failing_since": null,
       "last_error": null, "consecutive_failures": 0}
    ],
    "writer": {"mode": "background", "queued": 0, "capacity": 1000, "submitted": 5120,
               "completed": 5118, "failed": 2, "dropped": 0, "last_drop_at": null},
    "storage": {"failed": 2, "last_failed_at": "2026-11-18T09:31:12.408+00:00",
                "last_failed_type": "CONTROL_CPU_APPLIED", "last_status": "DBStatus.FAILED"}
  }
}
```

**Notes:**
- Times in `sources` are epoch seconds. A source appears after the detector's first attempt
  to read it (about 20 s after startup) and is updated every scan (30 s).
- The dashboard warns once `consecutive_failures` reaches 2, so a single slow read does not.
- `writer`: events reported by business code (the balancer's control actions, API audit
  events) are stored by one background thread, in the order they happened, so the control
  path and API requests never wait for SQLite, alert evaluation or process snapshots.
  `mode` is `inline` until the service starts the writer. `dropped` counts events lost to a
  full queue (`capacity`); a limit-lifecycle fact waits up to 2 s for room first. `failed`
  counts queued events that did not reach the database. The counters cover the process
  lifetime.
- `storage`: event writes the database refused (read-only or full disk, lock timeout), from
  every producer, including the detectors and lifecycle hooks that write without the
  writer. A growing `failed` means events are being lost; `last_status` is the store's
  result for the latest one.

---

### GET /diag/digest

**Purpose:** Generate an on-demand diagnostics digest for a day, a week, a boot session, or
an explicit range.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | period | No | string | `day` (default) / `week` / `boot` / `range` |
| Query | date | Conditional | string | `YYYY-MM-DD` in the server's local time zone; required for `day` and `week` |
| Query | boot_id | Conditional | string | Required for `boot`; a `boot_id` from `/diag/boots` |
| Query | from | Conditional | int | Range start (epoch seconds) |
| Query | to | Conditional | int | Range end (epoch seconds) |

**Request Example:**
```
GET /diag/digest?period=day&date=2026-10-13
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "period": {"kind": "day", "from": 1791820800, "to": 1791907200, "boot_id": null},
    "generated_at": "2026-10-13T09:30:00.123456+00:00",
    "event_stats": {"total": 142, "by_severity": {"info": 120, "warning": 18, "error": 4, "critical": 0}},
    "alert_summary": [],
    "range_alerts": [],
    "active_alerts": [],
    "config_changes": [],
    "benchmark_regressions": [],
    "comparison": null,
    "coverage": {"retention_days": 3, "retained_from": 1791624600, "complete": true,
                 "alerts_truncated": false}
  }
}
```

**Notes:**
- `day` covers the given date from 00:00 to the next 00:00 in the server's local time zone,
  and `week` the seven local days ending with it (`comparison.previous_period` is the seven
  days before that). Each boundary is local midnight, so a day with a DST change lasts 23 or
  25 hours. The example above is for a server in UTC+8.
- `generated_at` is UTC; `period.from` / `period.to` are epoch seconds.
- Supplying `from`/`to` forces `period` to `range`, whatever `period` was sent. The two must
  be given together, otherwise `from and to must be provided together`.
- `period=boot` without `boot_id`, or `day`/`week` without `date`, returns
  `RetCode.ARGUMENT_ERROR`.
- A `ValueError` from the generator (e.g. a malformed `date`) also surfaces as
  `RetCode.ARGUMENT_ERROR` with the underlying message.
- `event_stats` are exact counts over the period (digest events excluded), not a sample.
- `range_alerts` holds every alert occurrence that was active at some point in the period
  (including ones resolved since), at most 100; `active_alerts` holds the alerts active
  right now.
- `coverage` says how much of the period the data can cover. The ledger keeps
  `retention_days` of events; `complete` is `false` when the period starts before
  `retained_from`, so its counts cover only the part since then. `alerts_truncated` is
  `true` when more than 100 alert occurrences were active in the period.
- `comparison` (`week` only) carries the previous 7 days' `event_stats`, or `null` with
  `unavailable_reason: "outside_retention"` when those days are older than the retention
  window. With the default 3-day retention that is always the case.
- A `boot` with no recorded events has no time span: `period.from` / `to` are `null` and
  the alert and configuration lists are empty.
