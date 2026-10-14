# Diagnostics API Guide

REST reference for the `/diag/*` endpoints served by [`diag_api.py`](diag_api.py).

These are read-mostly endpoints over the event ledger, the derived alerts, the unified
log-query façade and the context assembler. They are mounted only when the diagnostics
feature is present in the deployment — check `GET /smartune/capabilities` (`diagnostics: 1`)
before offering the tab.

Base URL, TLS, authentication and the unified `{retcode, retmsg, data}` envelope are the
same as the rest of the product; see the [Backend API Guide](../docs/API_ENDPOINTS.md) for
those and for the shared `RetCode` table. Auth is applied app-wide by `smartune_api`'s
`before_app_request` gate, so none of these routes carries a per-route token check.

---

## Overview

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/diag/event-catalog` | GET | Built-in diagnostic event metadata for Settings |
| `/diag/events` | GET | Filtered operational-event query |
| `/diag/alerts` | GET | Derived alerts, active-only by default |
| `/diag/alerts/<dedup_key>/acknowledge` | POST | Acknowledge one active alert |
| `/diag/alerts/<dedup_key>/silence` | POST | Silence one active alert for 30 or 120 minutes |
| `/diag/control-lifecycles` | GET | Resource-limit lifecycles reconstructed from events |
| `/diag/control-lifecycles/clear-without-runtime-state` | POST | Close an orphaned lifecycle |
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

**Purpose:** List all built-in diagnostic event metadata, so Settings can render the
per-event enable switches without hardcoding the catalogue.

**Request:** No parameters.

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "events": [
      {
        "event_type": "RESOURCE_SYSTEM_PRESSURE_CHANGED",
        "category": "resource",
        "severity": "info",
        "description": "System pressure level transitioned"
      }
    ]
  }
}
```

---

### GET /diag/events

**Purpose:** Filtered query over the operational-event ledger. Every filter is optional and
they are combined with AND.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | event_id | No | string | A single event by id |
| Query | severity | No | string | `info` / `warning` / `error` / … |
| Query | category | No | string | Event category |
| Query | source | No | string | Emitting subsystem |
| Query | event_type | No | string | Catalogue event type |
| Query | app_id | No | string | Scope to one app |
| Query | job_id | No | string | Scope to one benchmark job |
| Query | impact | No | string | Impact classification |
| Query | resource_type | No | string | `cpu` / `memory` / `disk_io` / … |
| Query | protection_id | No | string | Scope to one control lifecycle |
| Query | episode_id | No | string | Scope to one pressure episode |
| Query | boot_id | No | string | Scope to one boot session |
| Query | keyword | No | string | Free-text match |
| Query | from | No | int | Window start (epoch seconds) |
| Query | to | No | int | Window end (epoch seconds) |
| Query | limit | No | int | Max rows (default `200`) |
| Query | offset | No | int | Rows to skip (default `0`) |

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
        "event_id": "evt-01H...",
        "ts": 1755764400,
        "severity": "warning",
        "category": "resource",
        "source": "balancer",
        "event_type": "RESOURCE_LIMIT_APPLIED",
        "app_id": "com.example.app",
        "protection_id": "auto:com.example.app:...",
        "message": "CPU/memory limit applied under critical pressure"
      }
    ],
    "count": 1
  }
}
```

**Notes:**
- `count` is the number of rows in this page, not the total matching the filter.

---

## Alerts

### GET /diag/alerts

**Purpose:** List derived diagnostics alerts. Defaults to the currently active ones.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | active | No | bool | `false` / `0` / `no` includes resolved alerts (default `true`) |
| Query | notifyable | No | bool | `true` restricts to alerts eligible for notification (default `false`) |
| Query | limit | No | int | Max rows (default `100`) |

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "alerts": [
      {
        "dedup_key": "pressure:critical:sustained",
        "severity": "warning",
        "first_seen": "2026-10-13T09:12:00+00:00",
        "last_seen": "2026-10-13T09:40:00+00:00",
        "count": 7,
        "acknowledged_at": null,
        "silenced_until": null
      }
    ],
    "count": 1
  }
}
```

---

### POST /diag/alerts/&lt;dedup_key&gt;/acknowledge

**Purpose:** Mark one active alert as acknowledged. The alert stays in the list; it is
simply no longer treated as unseen.

**Request:** `dedup_key` in the path; no body.

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {"acknowledged": true}
}
```

**Notes:**
- Returns `RetCode.ARGUMENT_ERROR` with `Active alert not found` when the key matches no
  active alert — including when it matches an alert that has already resolved.

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
- Returns `RetCode.ARGUMENT_ERROR` with `Control lifecycle still has recoverable state`
  when the limit is in fact still live. Probe first with
  `POST /app/interrupted_limit_status` and restore it through the normal path instead of
  clearing the record.

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
| Query | job_id | No | string | Scope to one benchmark job |
| Query | boot_id | No | string | Scope to one boot session |
| Query | keyword | No | string | Free-text match |
| Query | limit | No | int | Max lines (default `500`) |

**Request Example:**
```
GET /diag/logs?source=smartune,journald&level=error&from=1755760000&to=1755770000
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": {
    "lines": [
      {"ts": 1755764400, "source": "smartune", "level": "error", "message": "..."}
    ],
    "sources": ["smartune", "journald"],
    "truncated": false
  }
}
```

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
        "boot_id": "3f2a...",
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
    "monitor": {}
  }
}
```

**Notes:**
- `findings_only=true` without both `from` and `to` returns `RetCode.ARGUMENT_ERROR` with
  `findings_only requires from & to`.
- No scope and no window returns `provide one of job_id/app_id, or from & to`.

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

### GET /diag/digest

**Purpose:** Generate an on-demand diagnostics digest for a day, a week, a boot session, or
an explicit range.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | period | No | string | `day` (default) / `week` / `boot` / `range` |
| Query | date | Conditional | string | `YYYY-MM-DD`; required for `day` and `week` |
| Query | boot_id | Conditional | string | Required for `boot` |
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
    "period": "day",
    "window": {"from": 1755734400, "to": 1755820800},
    "summary": {"events": 142, "alerts": 3, "control_lifecycles": 5},
    "sections": []
  }
}
```

**Notes:**
- Supplying `from`/`to` forces `period` to `range`, whatever `period` was sent. The two must
  be given together, otherwise `from and to must be provided together`.
- `period=boot` without `boot_id`, or `day`/`week` without `date`, returns
  `RetCode.ARGUMENT_ERROR`.
- A `ValueError` from the generator (e.g. a malformed `date`) also surfaces as
  `RetCode.ARGUMENT_ERROR` with the underlying message.
