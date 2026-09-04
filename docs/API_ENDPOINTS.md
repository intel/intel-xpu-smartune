# Intel XPU SmartTune Backend API Guide

This document provides a comprehensive reference for the Intel XPU SmartTune backend API endpoints.

## Base Information

| Item | Value |
|------|-------|
| Base URL | `https://localhost:9001` |
| Protocol | HTTPS (requires `b_server.crt` / `b_server.key`) |
| Response Format | JSON (default) or Server-Sent Events (SSE) for streaming endpoints |
| Authentication | Token-based (via `/auth/login`) |
| Framework | Flask (WSGI) |
| CORS | Enabled (`Access-Control-Allow-Origin: *`) |

## Unified Response Format

All endpoints return a standardized JSON structure:

```json
{
  "retcode": 0,
  "retmsg": "success",
  "data": { ... }
}
```

### Return Codes (RetCode)

| Code | Name | Description |
|------|------|-------------|
| 0 | SUCCESS | Request completed successfully |
| 10 | NOT_EFFECTIVE | Operation had no effect |
| 100 | EXCEPTION_ERROR | Internal server error / unhandled exception |
| 101 | ARGUMENT_ERROR | Missing or invalid request parameters |
| 102 | DATA_ERROR | Data validation error |
| 103 | OPERATING_ERROR | Operation failed (e.g., resource not found for action) |
| 105 | CONNECTION_ERROR | External connection failure |
| 106 | RUNNING | Process already running |
| 108 | PERMISSION_ERROR | Insufficient permissions |
| 109 | AUTHENTICATION_ERROR | Authentication failed |
| 401 | UNAUTHORIZED | Unauthorized access |
| 404 | NOT_EXISTING | Requested resource does not exist |
| 409 | CONFLICT | Optimistic concurrency conflict |
| 500 | SERVER_ERROR | Internal server error |

---

## API Overview Table

### Part 1: Authentication & Application Management

| Category | Endpoint | Method | Description | Key Features |
|----------|----------|--------|-------------|--------------|
| Auth | `/auth/login` | POST | User authentication | SHA256 token validation |
| App | `/app/get_apps` | GET, POST | List all apps | Optional DB sync |
| App | `/app/set_priority` | POST | Set app priority | OOM score auto-adjustment |
| App | `/app/get_priority_data` | POST | Get priority info | Query by app_id or name |
| App | `/app/set_to_control` | POST | Enable app control | Register with BPF monitor |
| App | `/app/discover_search` | POST | Wizard: search processes | Keyword-based /proc scan |
| App | `/app/discover_extract` | POST | Wizard: extract fields | Derive bpf_name/id from PIDs |
| App | `/app/new_controlled_app` | POST | Wizard: register new app | Config + DB + BPF in one step |
| App | `/app/purge_controlled_app` | POST | Hard-delete app | Removes config + DB + BPF |
| App | `/app/get_controlled_app` | POST | List controlled apps | Full metadata, status |
| App | `/app/check_running_apps` | POST | Scan running processes | Detect pre-existing apps |
| App | `/app/get_pending_app` | POST | List pending apps | Sorted by priority DESC |
| App | `/app/set_oom_score` | POST | Set OOM score | Protect from OOM killer |
| App | `/app/cancel_relaunch` | POST | Cancel app relaunch | By app_id |
| App | `/app/resource_limit` | POST | Set resource limit | cgroup-based, overridable |
| App | `/app/resource_limit_profile` | POST | Get limit profile | Defaults + bounds for UI |
| App | `/app/resource_restore` | POST | Restore resources | Remove limits by app_id |
| App | `/app/auto_limited_apps` | POST | List pressure-driven limits | Rows + live pressure levels |
| App | `/app/auto_limit_restore` | POST | Release one auto limit | Also excludes the app |
| App | `/app/auto_limit_exclusions` | POST | List excluded apps | Runtime only, cleared on restart |
| App | `/app/auto_limit_exclusion_remove` | POST | Undo an exclusion | By key or app_id |
| App | `/app/events` | GET | SSE event stream | Real-time status push |

### Part 2: System Monitor

| Category | Endpoint | Method | Description | Key Features |
|----------|----------|--------|-------------|--------------|
| Monitor | `/monitor/app_resource_stats` | GET | App CPU/memory usage | Background-cached, top-N |
| Monitor | `/monitor/app_disk_io_stats` | GET | App disk I/O usage | Throughput + IOPS |
| Monitor | `/monitor/processes` | GET | All processes | Like `top`, sorted by CPU |
| Monitor | `/monitor/static_info` | GET | System hardware info | BIOS/OS/CPU/GPU/NPU |
| Monitor | `/monitor/static_info/<section>` | GET | Single static section | e.g. `/static_info/gpu`; `all` = full |
| Monitor | `/monitor/dynamic_info` | GET | Live system metrics (CPU, memory, IO, GPU, NPU) | Full snapshot; optional `?sections=` filter |
| Monitor | `/monitor/dynamic_info/<section>` | GET | Single dynamic section | e.g. `/dynamic_info/gpu`; on-demand; `all` = full |
| Monitor | `/monitor/history` | GET | Snapshot history (static/dynamic) | Time-range, type filter, limit, `sections` projection |
| Monitor | `/monitor/history/retention` | GET | Get retention config | Current period + bounds |
| Monitor | `/monitor/history/retention` | POST | Set retention period | Optimistic concurrency |
| Config | `/monitor/config/weights_top` | GET | Get ranking weights | CPU/memory/GPU weights |
| Config | `/monitor/config/weights_top` | POST | Update ranking weights | Optimistic concurrency |
| Config | `/monitor/config/passive_control` | GET | Get passive control state | Enable/disable flag |
| Config | `/monitor/config/passive_control` | POST | Toggle passive control | Optimistic concurrency |
| Config | `/monitor/config/monitored_sections` | GET | Get monitored dynamic sections | Effective + configured + all |
| Config | `/monitor/config/monitored_sections` | POST | Set monitored dynamic sections | Optimistic concurrency |
| Config | `/monitor/config/<section>` | GET, POST | Auto-control config groups (system_pressure, disk_pressure, network_pressure, network_control, limit_policy) | One parametrized endpoint; optimistic concurrency |

---

## Part 1 — Detailed API Specifications

### Authentication

#### POST /auth/login

**Purpose:** Validate user-provided token against the stored hash for authentication.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | pwd | Yes | string | User token for authentication |

**Request Example:**
```json
{
  "pwd": "your-secret-token"
}
```

**Response (Success):**
```json
{
  "retcode": 0,
  "retmsg": "Authentication successful",
  "data": {
    "authenticated": true
  }
}
```

**Response (Invalid Token):**
```json
{
  "retcode": 0,
  "retmsg": "Invalid token",
  "data": {
    "authenticated": false
  }
}
```

---

### Application Management

#### GET/POST /app/get_apps

**Purpose:** Retrieve all system application entries and optionally sync them to the database.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | store | No | boolean | If `true`, sync discovered apps to DB (default: `false`) |

**Request Example:**
```json
{
  "store": true
}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully retrieved app list",
  "data": [
    {
      "app_id": "com.example.app",
      "name": "Example App",
      "commandline": "/usr/bin/example --flag"
    }
  ]
}
```

---

#### POST /app/set_priority

**Purpose:** Set the priority of an application and update the database. Also adjusts OOM score.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | app_id | Yes | string | Application identifier |
| Body | priority | Yes | int | Priority level to set |

**Request Example:**
```json
{
  "app_id": "com.example.app",
  "priority": 5
}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Priority updated successfully",
  "data": {}
}
```

---

#### POST /app/get_priority_data

**Purpose:** Retrieve the priority settings for an app by app_id or name.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | app_id | No* | string | Application identifier |
| Body | app_name | No* | string | Application name |

*At least one of `app_id` or `app_name` must be provided.

**Request Example:**
```json
{
  "app_id": "com.example.app"
}
```

Or query by name:
```json
{
  "app_name": "example"
}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully retrieved priority data",
  "data": {
    "id": 1,
    "app_id": "com.example.app",
    "name": "Example App",
    "priority": 5,
    "cgroup": "/sys/fs/cgroup/example",
    "remark": "Critical service",
    "cmdline": "/usr/bin/example --flag",
    "up_time": "2026-06-17T10:30:00",
    "status": "running"
  }
}
```

---

#### POST /app/set_to_control

**Purpose:** Enable or disable control for an application and register it with the BPF monitor.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | app_id | Yes | string | Application identifier |
| Body | app_name | Yes | string | Application name |
| Body | controlled | No | boolean | Enable/disable control (default: `true`) |
| Body | cgroup | No | string | cgroup path |
| Body | priority | No | int | Priority level (default: `0`) |
| Body | remark | No | string | Remark/description |
| Body | cmdline | No | string | Command line |

**Request Example:**
```json
{
  "app_id": "com.example.app",
  "app_name": "example",
  "controlled": true,
  "priority": 3,
  "cgroup": "",
  "remark": "AI inference workload",
  "cmdline": "/usr/bin/example --mode=inference"
}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "App control enabled and added to monitor",
  "data": {
    "app_name": "example",
    "controlled": true
  }
}
```

---

#### POST /app/discover_search

**Purpose:** Wizard step — scan `/proc` for processes matching user-provided keywords so the UI can display candidate processes for the user to select.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | keywords | Yes | string[] | List of keywords to match against process names/cmdlines |

**Request Example:**
```json
{
  "keywords": ["helicon", "vlm"]
}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Found 5 candidate(s) for keywords ['helicon', 'vlm']",
  "data": {
    "count": 5,
    "candidates": [
      {
        "pid": 12345,
        "comm": "HeliconSearch_a",
        "exe": "/usr/local/heliconsearch/HeliconSearch_agent",
        "cmdline": "/usr/local/heliconsearch/HeliconSearch_agent --mode=gpu",
        "cgroup_unit": "heliconsearch.service"
      }
    ]
  }
}
```

---

#### POST /app/discover_extract

**Purpose:** Wizard step — read `/proc/<pid>` for user-selected PIDs and return aggregated fields (bpf_name, process_names, commandline, id suggestion) the wizard needs to auto-fill the registration form.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | pids | Yes | int[] | List of process IDs selected by the user |
| Body | name | No | string | Display name (used to derive a default `id` slug) |

**Request Example:**
```json
{
  "pids": [12345, 12346],
  "name": "heliconSearch"
}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Extracted fields from 2 pid(s)",
  "data": {
    "bpf_name": ["HeliconSearch_a", "VLMService"],
    "process_names": ["HeliconSearch_agent", "VLMService"],
    "commandline": "/usr/local/heliconsearch/HeliconSearch_agent",
    "id_suggestion": "heliconsearch.service"
  }
}
```

---

#### POST /app/new_controlled_app

**Purpose:** Final wizard step — register a brand-new managed application. Persists to config.yaml, inserts a DB record, and rebuilds the BPF match cache so monitoring starts immediately without a restart.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | name | Yes | string | Display name |
| Body | id | Yes | string | Logical unique identifier (DB primary key). Use a stable `<slug>.id`, not an ephemeral `*.scope`/`*.service` unit |
| Body | priority | No | string | `"low"` / `"medium"` / `"high"` / `"critical"` (default: `"low"`) |
| Body | commandline | No | string | argv[0] of the main process |
| Body | bpf_name | No | string[] | Executable names for BPF exec watch |
| Body | process_names | No | string[] | Program names that make up this app (its identity) |
| Body | remark | No | string | User-defined note |

**Request Example:**
```json
{
  "name": "heliconSearch",
  "id": "heliconsearch.id",
  "priority": "high",
  "commandline": "/usr/local/heliconsearch/HeliconSearch_agent",
  "bpf_name": ["HeliconSearch_a", "VLMService"],
  "process_names": ["HeliconSearch_agent", "VLMService"]
}
```

**Response (Success):**
```json
{
  "retcode": 0,
  "retmsg": "Application 'heliconSearch' added",
  "data": {
    "name": "heliconSearch",
    "id": "heliconsearch.id"
  }
}
```

**Response (409 Conflict — duplicate id, name, or overlapping processes):**
```json
{
  "retcode": 409,
  "retmsg": "An app with id 'heliconsearch.service' already exists. ...",
  "data": {
    "conflict": "id",
    "with": "heliconSearch",
    "with_id": "heliconsearch.service"
  }
}
```

---

#### POST /app/purge_controlled_app

**Purpose:** Hard-delete an application from BOTH config.yaml and the DB: this completely wipes the entry — an active manual resource limit is restored first, then config is removed, the DB row is deleted, the OOM score is restored, and the BPF cache is refreshed. If the active manual limit cannot be restored, the purge fails and leaves the application intact. Auto-limited apps must be restored or taken under manual control before deletion. Used when the user wants to re-add an app whose process_names overlap with an existing entry.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | id | Yes | string | Application identifier to purge |

**Request Example:**
```json
{
  "id": "heliconsearch.service"
}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Application 'heliconSearch' purged; you can now re-add it",
  "data": {
    "id": "heliconsearch.service",
    "name": "heliconSearch"
  }
}
```

---

#### POST /app/get_controlled_app

**Purpose:** Return all controlled applications along with their current metadata.

**Request:** Empty JSON body or no body.

**Request Example:**
```json
{}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Found 3 controlled apps",
  "data": [
    {
      "app_id": "com.example.app",
      "app_name": "Example App",
      "controlled": true,
      "priority": 5,
      "oom_score": -500,
      "cmdline": "/usr/bin/example",
      "cgroup": "",
      "process_names": ["example", "example-worker"],
      "remark": "Critical service",
      "status": "running"
    }
  ]
}
```

---

#### POST /app/check_running_apps

**Purpose:** Scan currently running processes to find managed apps that started before the balancer. Called once when the UI balancer tab is first opened.

**Request:** Empty JSON body or no body.

**Request Example:**
```json
{}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Startup scan complete, detected 2 pre-existing monitored app(s)",
  "data": [
    {
      "app_id": "com.example.app",
      "app_name": "example",
      "status": "running"
    }
  ]
}
```

---

#### POST /app/get_pending_app

**Purpose:** Return all applications currently in pending state, ordered by priority (descending).

**Request:** Empty JSON body or no body.

**Request Example:**
```json
{}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Found 2 pending apps (sorted by priority DESC)",
  "data": [
    {
      "app_id": "com.example.app",
      "app_name": "Example App",
      "controlled": true,
      "priority": 5,
      "oom_score": -500,
      "priority_value": 50,
      "cgroup": "",
      "remark": "",
      "status": "pending"
    }
  ]
}
```

---

#### POST /app/set_oom_score

**Purpose:** Set the OOM score for an application to protect it from the Linux OOM killer.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | app_id | Yes | string | Application identifier |

**Request Example:**
```json
{
  "app_id": "com.example.app"
}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "App OOM score set successfully",
  "data": {}
}
```

---

#### POST /app/cancel_relaunch

**Purpose:** Cancel relaunch for a specific app by app_id. Updates status to "stopped".

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | app_id | Yes | string | Application identifier |

**Request Example:**
```json
{
  "app_id": "com.example.app"
}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully found and canceled relaunch",
  "data": {
    "app_id": "com.example.app"
  }
}
```

---

#### POST /app/resource_limit

**Purpose:** Set resource limit (cgroup-based) for a specific app.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | app_id | Yes | string | Application identifier |
| Body | app_name | Yes | string | Application name |
| Body | priority | Yes | string | Priority level |
| Body | target_cgroups | No | string[] | Restrict limiting to selected running cgroup basenames; omitted means all running instances |
| Body | limit_overrides | No | object | Custom limit overrides (key-value) |

**Request Example:**
```json
{
  "app_id": "com.example.app",
  "app_name": "example",
  "priority": "3",
  "target_cgroups": ["app-example.scope", "app-example-worker.scope"],
  "limit_overrides": {
    "cpu_quota": 50,
    "memory_max_mb": 2048
  }
}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully found and set resource limit",
  "data": {}
}
```

---

#### POST /app/resource_limit_profile

**Purpose:** Get the editable resource-limit profile (defaults + bounds) for the UI.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | app_id | Yes | string | Application identifier |
| Body | app_name | Yes | string | Application name |
| Body | priority | No | string | Priority level |

**Request Example:**
```json
{
  "app_id": "com.example.app",
  "app_name": "example",
  "priority": "3"
}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully fetched resource limit profile",
  "data": {
    "cpu_quota": { "default": 50, "min": 10, "max": 100 },
    "memory_max_mb": { "default": 2048, "min": 256, "max": 16384 }
  }
}
```

---

#### POST /app/resource_restore

**Purpose:** Restore (remove) resource limits for a specific app by app_id.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | app_id | Yes | string | Application identifier |

**Request Example:**
```json
{
  "app_id": "com.example.app"
}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully found and restored resource",
  "data": {}
}
```

---

#### POST /app/auto_limited_apps

**Purpose:** List the apps the balancer is currently limiting on its own because system or disk I/O pressure hit critical. Manual limits set from the UI are not included — they show on the controlled app's own row.

**Request:** No parameters (empty body).

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Found 1 auto-limited apps",
  "data": {
    "apps": [
      {
        "app_id": "com.example.app",
        "effective_app_id": "app-com.example.app.scope",
        "app_name": "example",
        "priority": "medium",
        "is_controlled": true,
        "status": "limited",
        "limit_reason": "system_pressure",
        "pressure_level": "critical",
        "limited_at": 1755764400.12,
        "limit_parts": {"cpu_mem_limited": true, "io_limited": false},
        "cgroups": ["app-com.example.app.scope"]
      }
    ],
    "sys_pressure_level": "high",
    "disk_pressure_level": "low"
  }
}
```

**Row fields:**

| Field | Format | Description |
|-------|--------|-------------|
| app_id | string | Public app id; empty for an app that is not under control |
| effective_app_id | string | Registry key (the primary cgroup) — pass this to `/app/auto_limit_restore` |
| app_name | string | Display name |
| priority | string | `critical`/`high`/`medium`/`low`, or `undefined` for uncontrolled apps |
| is_controlled | bool | Whether the app is under Smartune control |
| status | string | `limited`, or `partially_restored` once the first restore stage has run |
| limit_reason | string | `system_pressure` or `disk_pressure` |
| pressure_level | string | Level at the moment the limit was applied (a snapshot, not the current level) |
| limited_at | float | Unix timestamp of the first limit on this app |
| limit_parts | object | `cpu_mem_limited` / `io_limited` — which caps are in place |
| cgroups | array | Cgroups the limit is written to |

**Notes:**
- `sys_pressure_level` / `disk_pressure_level` are the *current* levels, sent along so the UI can render without a second request. They stay fresh through the `pressure_level_changed` SSE event.
- No restore deadline is reported. Auto restore is not a timer: pressure has to fall back to medium/low, hold there for the stability window, and then the limits are lifted in stages.
- On error the same shape is returned with an empty `apps` list.

---

#### POST /app/auto_limit_restore

**Purpose:** Lift one auto limit immediately at the user's request, and take the app out of the auto-limit candidate pool so the next critical tick does not simply re-limit it. Use `/app/resource_restore` for manual limits — it does not touch auto limits.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | app_id | Yes | string | `effective_app_id` or `app_id` from `/app/auto_limited_apps` |

**Request Example:**
```json
{
  "app_id": "app-com.example.app.scope"
}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Restored",
  "data": {}
}
```

**Notes:**
- Returns `RetCode.OPERATING_ERROR` with `No auto-limited app found for this id` when the id matches nothing, or `Failed to restore resources for this app` when the cgroup write fails.
- Emits an `auto_limit_restored_by_user` notify event on `/app/events`.

---

#### POST /app/auto_limit_exclusions

**Purpose:** List the apps the user has excluded from auto-limiting.

**Request:** No parameters (empty body).

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Found 1 auto-limit exclusions",
  "data": [
    {
      "key": "app:com.example.app",
      "kind": "app",
      "app_id": "com.example.app",
      "app_name": "example",
      "priority": "medium",
      "cgroups": ["app-com.example.app.scope"],
      "excluded_at": 1755764500.44
    }
  ]
}
```

**Notes:**
- `kind` is `app` for a controlled app (keyed `app:<app_id>`, covers every instance) or `instance` for an uncontrolled one (keyed `instance:<cgroup>`, covers that instance only).
- Exclusions live in memory. Restarting the balancer clears them.

---

#### POST /app/auto_limit_exclusion_remove

**Purpose:** Put an excluded app back into the auto-limit candidate pool.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | key | No* | string | Exclusion key from `/app/auto_limit_exclusions` |
| Body | app_id | No* | string | App id or cgroup, if the key is not at hand |

\* One of the two is required; `key` wins when both are sent.

**Request Example:**
```json
{
  "key": "app:com.example.app"
}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Auto-limit exclusion removed",
  "data": {}
}
```

---

#### GET /app/events

**Purpose:** Server-Sent Events (SSE) stream for real-time app status changes.

**Request:** No body required. Connect via HTTP GET.

**Response Format:** `text/event-stream`

**Response Headers:**
```
Content-Type: text/event-stream
Cache-Control: no-cache
X-Accel-Buffering: no
Connection: keep-alive
```

**Event Stream:**
```
data: {"type": "connected"}

data: {"app_id": "com.example.app", "app_name": "example", "status": "running", "purpose": "app"}

data: {"app_id": "", "app_name": "", "status": "pressure_level_changed", "sys_level": "high", "disk_level": "low", "purpose": "notify"}

: heartbeat
```

**Notes:**
- Initial connection event is sent immediately
- Heartbeat comments (`: heartbeat`) sent every 30 seconds when idle
- Events are JSON-encoded app status updates
- Connection remains open until client disconnects

**`purpose: "notify"` events used by the auto-limit UI:**

| status | Extra fields | Meaning |
|--------|--------------|---------|
| `pressure_level_changed` | `sys_level`, `disk_level` | Either level changed. `app_id`/`app_name` are empty — this is a system event |
| `auto_limit_changed` | `detail` | The auto-limited list changed in a way no app event covers (a staged restore step, or a failed one). `detail` names the step |
| `auto_limit_restored_by_user` | — | A limit was lifted through `/app/auto_limit_restore` |
| `app_closed_limit_restored` | — | The app exited and its limit was dropped with it |

---

## Part 2 — Monitor API Specifications

### Resource Statistics

#### GET /monitor/app_resource_stats

**Purpose:** Return per-application CPU/memory/GPU resource usage for the dashboard. Background-cached with auto-refresh every 2 seconds.

**Query Parameters:**

| Parameter | Required | Type | Default | Description |
|-----------|----------|------|---------|-------------|
| n | No | int | 10 | Number of top apps to return |

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully retrieved app resource stats",
  "data": {
    "apps": [
      {
        "app_id": "com.example.app",
        "app_name": "Example App",
        "pid": 12345,
        "process_name": "example",
        "cmdline": "/usr/bin/example --flag",
        "cpu_usage": 0.35,
        "memory_mb": 1024.5,
        "io_read_rate": 12.3,
        "io_write_rate": 5.6,
        "score": 85.2,
        "gpu_util": 45.0,
        "gpu_mem_mb": 2048.0
      }
    ]
  }
}
```

**Field Details:**
| Field | Type | Description |
|-------|------|-------------|
| cpu_usage | float | Fraction of total CPU capacity (0-1) |
| memory_mb | float | Resident memory in MB |
| io_read_rate | float | Disk read rate in MB/s |
| io_write_rate | float | Disk write rate in MB/s |
| score | float | Combined ranking score |
| gpu_util | float | Peak GPU engine utilization % (0-100) |
| gpu_mem_mb | float | GPU memory used in MB |

---

#### GET /monitor/app_disk_io_stats

**Purpose:** Return per-application disk I/O usage stats. Background-cached with auto-refresh.

**Query Parameters:**

| Parameter | Required | Type | Default | Description |
|-----------|----------|------|---------|-------------|
| n | No | int | 10 | Number of top apps to return |

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully retrieved app disk I/O stats",
  "data": {
    "apps": [
      {
        "pid": 12345,
        "name": "example",
        "app_name": "Example App",
        "cmdline": "/usr/bin/example",
        "io_read_rate": 50.2,
        "io_write_rate": 30.1,
        "io_read_iops": 1200.0,
        "io_write_iops": 800.0,
        "io_per_disk": {
          "nvme0n1": {
            "read_mb_s": 50.2,
            "write_mb_s": 30.1,
            "read_iops": 1200.0,
            "write_iops": 800.0
          }
        },
        "score": 72.5
      }
    ]
  }
}
```

**Field Details:**
| Field | Type | Description |
|-------|------|-------------|
| io_read_rate | float | Read throughput in MB/s |
| io_write_rate | float | Write throughput in MB/s |
| io_read_iops | float | Read **device requests** per second (see note) |
| io_write_iops | float | Write **device requests** per second (see note) |
| io_per_disk | object | Same four rates broken down per whole disk, keyed by kernel disk name (`nvme0n1`, `sda`). Partitions are folded into their parent disk. Empty when the app did no I/O in the sampling window |
| score | float | Combined I/O ranking score |

**Note on IOPS:** these are block-layer request counts taken from cgroup v2 `io.stat`
(`rios`/`wios`), not `read()`/`write()` syscall counts. The two differ by up to ~8x for
large buffered writes, because the block layer splits each write into `max_sectors_kb`
chunks — so a syscall-derived figure under-reports a large-block or async writer by an
order of magnitude. Run `balancer/test/probe_cgroup_io.py` to reproduce the comparison
on your own hardware.

---

#### GET /monitor/processes

**Purpose:** Return a list of all running processes sorted by CPU usage, similar to `top`.

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully retrieved process list",
  "data": {
    "count": 256,
    "processes": [
      {
        "pid": 12345,
        "name": "example",
        "username": "root",
        "cpu_percent": 45.2,
        "memory_percent": 8.5,
        "mem_rss_kb": 524288,
        "status": "running",
        "cmdline": "/usr/bin/example --mode=inference"
      }
    ]
  }
}
```

---

### System Information

#### GET /monitor/static_info

**Purpose:** Return static system configuration info (hardware, OS, drivers).

**Query Parameters:**

| Parameter | Required | Type | Default | Description |
|-----------|----------|------|---------|-------------|
| force_refresh | No | string | false | Force re-collection (`1`/`true`/`yes`) |

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully retrieved static system info",
  "data": {
    "bios": { "vendor": "...", "version": "...", "release_date": "..." },
    "os": { "name": "...", "version": "...", "kernel": "..." },
    "driver": { "gpu": "...", "version": "..." },
    "cpu": { "model": "...", "cores": 16, "threads": 32 },
    "memory": { "total_gb": 64, "type": "DDR5" },
    "io": { "disks": [...] },
    "gpu": { "name": "...", "memory_mb": 16384 },
    "npu": { "name": "...", "available": true },
    "collected_at": "2026-06-17T10:30:00"
  }
}
```

---

#### GET /monitor/static_info/&lt;section&gt;

**Purpose:** Return a single section of the static config, e.g. `/monitor/static_info/gpu`. Convenience sub-resource so a caller can fetch just the hardware group it needs.

**Path Parameter:**

| Parameter | Required | Type | Description |
|-----------|----------|------|-------------|
| section | Yes | string | One of `bios`, `os`, `driver`, `cpu`, `memory`, `network`, `disk`, `gpu`, `npu`, or `all` (full snapshot, equivalent to `/monitor/static_info`) |

**Query Parameters:** Same as `/monitor/static_info` (`force_refresh`).

**Response:** Same envelope as `/monitor/static_info`, with `data` restricted to the requested section (e.g. `{ "gpu": { ... } }`).

**Response (Invalid section — 101 ARGUMENT_ERROR):**
```json
{
  "retcode": 101,
  "retmsg": "Unknown section 'foo'. Valid sections: bios, os, driver, cpu, memory, network, disk, gpu, npu, all",
  "data": {}
}
```

---

#### GET /monitor/dynamic_info

**Purpose:** Return a dynamic system metrics snapshot. By default returns the **full** snapshot (all sections), served from a background-refreshed cache (refresh every 2 seconds). An optional `sections` query parameter restricts the payload to specific hardware groups.

**Query Parameters:**

| Parameter | Required | Type | Default | Description |
|-----------|----------|------|---------|-------------|
| sections | No | string | — | Comma-separated section list, e.g. `sections=cpu,gpu`. Omitted/empty = full snapshot. Valid: `cpu`, `memory`, `pressure`, `network`, `disk`, `gpu`, `npu` |

**Behavior:**
- **No `sections`** → full snapshot. Starts the full-collection background thread and serves from its warm cache.
- **With `sections`** → returns only those sections. Monitored sections are sliced from the warm cache; non-monitored sections are collected on demand (with a short per-section TTL cache) and do **not** start the full background collector — so e.g. requesting only `gpu` never touches CPU/NPU/disk.
- Invalid section names return `101 ARGUMENT_ERROR`.

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully retrieved dynamic system info",
  "data": {
    "collected_at": "2026-06-17 10:30:00",
    "cpu": { "usage_total": 35.2, "per_core_usage": [...], "per_core_freq_mhz": [...], "p_core_usage": 45.0, "e_core_usage": 20.0, "lpe_core_usage": 10.0, "temperature_c": 65.0, "per_core_temperature_c": [...], "..." },
    "memory": { "usage_percent": 50.0, "total_gb": 64.0, "available_gb": 32.0, "swap_total_gb": 8.0, "swap_used_gb": 1.2 },
    "pressure": { "level": "medium", "score": 45.2, "cpu": 12.5, "memory": 8.0, "io": 3.2, "network_busy_level": "medium", "..." },
    "network": { "per_nic": {"eth0": {"tx_mbps": 120.5, "rx_mbps": 85.3}} },
    "disk": { "is_stressed": false, "level": "low", "score": 0.12, "busy_level": "LOW", "busy_pct": 0.0, "pressure_pct": 12.0, "stressed_disks": [], "disk_io": {"sda": {"read_mbps": 50.0, "write_mbps": 30.0, "pressure": 0.12, "disk_type": "sata_ssd", "is_busy": false}}, "..." },
    "gpu": { "vram": {"card0": {"used_mb": 1024, "total_mb": 8192, "free_mb": 7168}}, "gpu_usage": {"devices": [{"engines": ["rcs","bcs","vcs","vecs","ccs"], "engine_util": {"rcs": 45.2, "vcs": 80.5, "..."}, "freqs": [{"name": "gt0", "cur_mhz": 1200, "act_mhz": 1150, "max_mhz": 1500, "rc6_pct": 85.0}], "power_w": {"gpu": 15.2, "pkg": 28.0}}]} },
    "npu": { "npu_smi": {} },
    "monitored_sections_updated_at": 1718600000
  }
}
```

**Field Notes:**
- `monitored_sections_updated_at` — Unix timestamp that advances only when the effective monitored-sections set changes. Clients can watch it to detect a config change and re-sync (see `/monitor/config/monitored_sections`).
- A section-filtered response contains only the requested section keys plus `collected_at` and `monitored_sections_updated_at`.

---

#### GET /monitor/dynamic_info/&lt;section&gt;

**Purpose:** Return a single hardware section of the dynamic snapshot — a convenience sub-resource for the common single-section case, e.g. `/monitor/dynamic_info/cpu`. Collects only that section on demand.

**Path Parameter:**

| Parameter | Required | Type | Description |
|-----------|----------|------|-------------|
| section | Yes | string | One of `cpu`, `memory`, `pressure`, `network`, `disk`, `gpu`, `npu`, or `all` (full snapshot, equivalent to `/monitor/dynamic_info`) |

**Response:** Same envelope as `/monitor/dynamic_info`; `data` contains only the requested section (plus `collected_at`, `monitored_sections_updated_at`). Invalid names return `101 ARGUMENT_ERROR`.

---

### History & Retention

#### GET /monitor/history

**Purpose:** Query monitor snapshot history with time-range filtering.

**Query Parameters:**

| Parameter | Required | Type | Default | Description |
|-----------|----------|------|---------|-------------|
| snapshot_type | No | string | "all" | Filter: `static`, `dynamic`, or `all` |
| sections | No | string | — | Comma-separated field projection, e.g. `sections=cpu,gpu`. Restricts each row's `data` payload to those sections. Valid: `bios`, `os`, `driver`, `cpu`, `memory`, `pressure`, `network`, `disk`, `gpu`, `npu` |
| limit | No | int | 100 | Max rows to return (1-20000) |
| start_time | No | int | — | Unix timestamp (seconds), range start |
| end_time | No | int | — | Unix timestamp (seconds), range end |
| range_seconds | No | int | — | Window length anchored to server clock (avoids clock-skew issues) |

**Notes:**
- `range_seconds` is only used when both `start_time` and `end_time` are omitted
- Server anchors the window to its own clock to avoid client clock-skew issues
- `sections` only trims serialization/transfer; rows are still read from the DB in full. Omitting it (or `null`) returns every stored field. Invalid section names return `101 ARGUMENT_ERROR`

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully retrieved monitor history",
  "data": {
    "snapshot_type": "dynamic",
    "sections": null,
    "limit": 100,
    "start_time": 1718600000,
    "end_time": 1718603600,
    "server_time": 1718603650,
    "count": 50,
    "items": [
      {
        "id": 1,
        "snapshot_type": "dynamic",
        "source": "auto",
        "collected_at": 1718600120,
        "create_time": 1718600120,
        "update_time": 1718600120,
        "create_date": "2026-06-17",
        "update_date": "2026-06-17",
        "data": { "cpu": { ... }, "memory": { ... } }
      }
    ]
  }
}
```

---

#### GET /monitor/history/retention

**Purpose:** Get current MonitorSnapshot retention period and allowed options.

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully retrieved retention settings",
  "data": {
    "retention_days": 3,
    "default_days": 3,
    "min_days": 1,
    "max_days": 7,
    "updated_at": 1718600000
  }
}
```

---

#### POST /monitor/history/retention

**Purpose:** Update the MonitorSnapshot retention period and trigger immediate cleanup.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | retention_days | Yes | int | Retention period (1-7 days) |
| Body | expected_updated_at | No | int | Unix timestamp from prior GET (optimistic concurrency) |

**Request Example:**
```json
{
  "retention_days": 5,
  "expected_updated_at": 1718600000
}
```

**Response (Success):**
```json
{
  "retcode": 0,
  "retmsg": "Retention set to 5 day(s)",
  "data": {
    "retention_days": 5,
    "deleted": 120,
    "updated_at": 1718603700
  }
}
```

**Response (409 Conflict):**
```json
{
  "retcode": 409,
  "retmsg": "Retention was modified by another client; please reload.",
  "data": {
    "current": {
      "retention_days": 3,
      "default_days": 3,
      "min_days": 1,
      "max_days": 7,
      "updated_at": 1718602500
    }
  }
}
```

---

### Configuration

#### GET /monitor/config/weights_top

**Purpose:** Get current ranking weights configuration (used for app resource scoring).

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully retrieved weights_top configuration",
  "data": {
    "cpu": 40,
    "memory": 30,
    "io": 10,
    "gpu": 20,
    "updated_at": 1718600000
  }
}
```

---

#### POST /monitor/config/weights_top

**Purpose:** Update ranking weights configuration with optimistic concurrency control.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | cpu | No | int | CPU weight (non-negative) |
| Body | memory | No | int | Memory weight (non-negative) |
| Body | gpu | No | int | GPU weight (non-negative) |
| Body | expected_updated_at | No | int | Unix timestamp from prior GET |

**Note:** I/O weight is not configurable via this API — disk I/O ranking uses pure throughput (MB/s).

**Request Example:**
```json
{
  "cpu": 50,
  "memory": 30,
  "gpu": 20,
  "expected_updated_at": 1718600000
}
```

**Response (Success):**
```json
{
  "retcode": 0,
  "retmsg": "Successfully updated weights_top configuration",
  "data": {
    "success": true,
    "updated_weights": {
      "cpu": 50,
      "memory": 30,
      "gpu": 20,
      "updated_at": 1718603800
    },
    "updated_at": 1718603800
  }
}
```

**Response (409 Conflict):**
```json
{
  "retcode": 409,
  "retmsg": "Configuration was modified by another client; please reload.",
  "data": {
    "success": false,
    "current": {
      "cpu": 40,
      "memory": 30,
      "gpu": 20,
      "updated_at": 1718602500
    }
  }
}
```

---

#### GET /monitor/config/passive_control

**Purpose:** Get the current passive resource-control switch state. When disabled, the balancer skips pressure-driven auto-limit/auto-restore; manual per-app limits and network controller remain active.

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully retrieved passive_resource_control configuration",
  "data": {
    "enabled": true,
    "updated_at": 1718600000
  }
}
```

---

#### POST /monitor/config/passive_control

**Purpose:** Toggle the passive resource-control switch with optimistic concurrency.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | enabled | Yes | boolean | Enable/disable passive control |
| Body | expected_updated_at | No | int | Unix timestamp from prior GET |

**Request Example:**
```json
{
  "enabled": false,
  "expected_updated_at": 1718600000
}
```

**Response (Success):**
```json
{
  "retcode": 0,
  "retmsg": "Successfully updated passive_resource_control configuration",
  "data": {
    "success": true,
    "enabled": false,
    "updated_at": 1718603900
  }
}
```

**Response (409 Conflict):**
```json
{
  "retcode": 409,
  "retmsg": "Configuration was modified by another client; please reload.",
  "data": {
    "success": false,
    "current": {
      "enabled": true,
      "updated_at": 1718602500
    }
  }
}
```

---

#### GET /monitor/config/network_control

**Purpose:** Get network-control policy including the global switch and per-class bandwidth ratio ranges. This is the class-level policy shared by all apps with the same network priority.

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully retrieved network_control configuration",
  "data": {
    "enable_network_control": true,
    "enable_network_pressure_shaping": true,
    "config_network_bw": {
      "system": { "min": 0.05, "max": 0.10 },
      "critical": { "min": 0.55, "max": 0.90 },
      "high": { "min": 0.30, "max": 0.80 },
      "low": { "min": 0.10, "max": 0.30 }
    },
    "updated_at": 1718600000
  }
}
```

---

#### POST /monitor/config/network_control

**Purpose:** Update network control behavior with optimistic concurrency.

- Passive/global network shaping switch: `enable_network_control`
- Pressure-driven dynamic shaping switch: `enable_network_pressure_shaping`
- Class-level ratio policy shared by all apps: `config_network_bw`

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | enable_network_control | No | boolean | Enable/disable global network shaping |
| Body | enable_network_pressure_shaping | No | boolean | Enable/disable pressure-driven dynamic throttle/recovery |
| Body | config_network_bw | No | object | Per-class `min`/`max` ratio updates |
| Body | expected_updated_at | No | int | Unix timestamp from prior GET |

**Request Example:**
```json
{
  "enable_network_control": true,
  "enable_network_pressure_shaping": true,
  "config_network_bw": {
    "critical": { "min": 0.55, "max": 0.90 },
    "high": { "min": 0.30, "max": 0.80 },
    "low": { "min": 0.10, "max": 0.30 }
  },
  "expected_updated_at": 1718600000
}
```

**Response (Success):**
```json
{
  "retcode": 0,
  "retmsg": "Successfully updated network_control configuration",
  "data": {
    "success": true,
    "enable_network_control": true,
    "enable_network_pressure_shaping": true,
    "config_network_bw": {
      "system": { "min": 0.05, "max": 0.10 },
      "critical": { "min": 0.55, "max": 0.90 },
      "high": { "min": 0.30, "max": 0.80 },
      "low": { "min": 0.10, "max": 0.30 }
    },
    "updated_at": 1718603900
  }
}
```

**Response (409 Conflict):**
```json
{
  "retcode": 409,
  "retmsg": "Configuration was modified by another client; please reload.",
  "data": {
    "success": false,
    "current": {
      "enable_network_control": false,
      "enable_network_pressure_shaping": true,
      "config_network_bw": {
        "system": { "min": 0.05, "max": 0.10 },
        "critical": { "min": 0.55, "max": 0.90 },
        "high": { "min": 0.30, "max": 0.80 },
        "low": { "min": 0.10, "max": 0.30 }
      },
      "updated_at": 1718602500
    }
  }
}
```

---

#### GET /monitor/config/monitored_sections

**Purpose:** Report which dynamic-info sections the background collector continuously monitors. Driven by the `monitored_sections` config key:
- **unset (`null`)** → monitor **all** sections (default)
- **list** (e.g. `["cpu","gpu"]`) → monitor only those; others are on-demand only
- **empty (`[]`)** → nothing monitored; no background collector runs (pure on-demand)

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully retrieved monitored_sections configuration",
  "data": {
    "sections": ["cpu", "gpu"],
    "configured_sections": ["cpu", "gpu"],
    "all_sections": ["cpu", "memory", "pressure", "network", "disk", "gpu", "npu"],
    "updated_at": 1718600000
  }
}
```

**Field Details:**
| Field | Type | Description |
|-------|------|-------------|
| sections | string[] | Effective monitored sections, in canonical order (resolves `null` → all) |
| configured_sections | string[] \| null | Raw config value; `null` means "all sections" |
| all_sections | string[] | Full list of supported dynamic sections |
| updated_at | int | Unix timestamp; advances only when the effective set changes |

---

#### POST /monitor/config/monitored_sections

**Purpose:** Update the dynamic-info sections the background collector continuously monitors. Persists to `config.yaml`, updates the live config, and (re)starts the background collector when the new set is non-empty. Uses optimistic concurrency against the change-driven `updated_at` returned by the GET.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | sections | Yes | string[] | Sections to monitor; subset of `all_sections`. An empty list means "pure on-demand" (no background collector). Unknown names are rejected. |
| Body | expected_updated_at | No | int | Unix timestamp from the prior GET (optimistic concurrency) |

**Request Example:**
```json
{
  "sections": ["cpu", "gpu"],
  "expected_updated_at": 1718600000
}
```

**Response (Success):**
```json
{
  "retcode": 0,
  "retmsg": "Successfully updated monitored_sections configuration",
  "data": {
    "success": true,
    "sections": ["cpu", "gpu"],
    "configured_sections": ["cpu", "gpu"],
    "all_sections": ["cpu", "memory", "pressure", "network", "disk", "gpu", "npu"],
    "updated_at": 1718603800
  }
}
```

**Response (409 Conflict):**
```json
{
  "retcode": 409,
  "retmsg": "Configuration was modified by another client; please reload.",
  "data": {
    "success": false,
    "current": {
      "sections": ["cpu"],
      "configured_sections": ["cpu"],
      "all_sections": ["cpu", "memory", "pressure", "network", "disk", "gpu", "npu"],
      "updated_at": 1718602500
    }
  }
}
```

---

### Auto-control tuning — `/monitor/config/<section>` (GET, POST)

A single parametrized endpoint covering the auto-control config groups below. All values are hot-read by the balancer/monitor, so a save takes effect without a restart (exception: the monitor's pressure-cache TTL derived from `regular_update_sys_pressure_time` is re-read on restart).

`GET /monitor/config/<section>` returns that group's current fields plus a change-tracking `updated_at`. `POST` accepts the group's fields plus `expected_updated_at` (optimistic concurrency: 409 `CONFLICT` on mismatch) and echoes the full group back with `success: true` and the new `updated_at`. Unknown sections and invalid values return 101 `ARGUMENT_ERROR`. The pre-existing dedicated routes (`weights_top`, `passive_control`, `monitored_sections`, `history/retention`) keep their own handlers and take priority over this dynamic route.

**Valid sections:**

Sections are grouped to match the Settings UI cards. Every field is optional in a `POST` (send any subset); at least one valid field is required.

| Section | Fields | Constraints |
|---------|--------|-------------|
| `system_pressure` | `regular_update_sys_pressure_time`, `thresholds{low,medium,high,critical}`, `weights{cpu,memory,io}`, `mem_gate_steepness`, `memory_busy_threshold`, `cpu_busy_threshold` | interval seconds `1–3600`; thresholds in `(0,1]` and ordered `low ≤ medium ≤ high ≤ critical`; weights non-negative integers; steepness `1–50`; busy thresholds percent `0–100` |
| `disk_pressure` | `disk_thresholds{low,medium,high,critical}`, `disk_pressure_model{sub_weights{latency,queue,util}, sigmoid_k, max_p_weight}` | Disk bands in `(0,1]` and ordered, kept separate from the system `thresholds`; sub-weights in `[0,1]` and summing to `≤ 1`; `sigmoid_k` `1–50`; `max_p_weight` in `[0,1]` |
| `network_pressure` | `network_thresholds{low,medium,high,critical}` | In `(0,1]`, ordered, and all four required |
| `limit_policy` | `policy` (`combined`/`separated`); `cpu`/`memory` = `{enabled, rate:{high,medium,low,undefined}}` (fractions `(0,1]`); `disk_io` = `{enabled, rate:{<priority>:{write,read,write_iops,read_iops}}}` (integers `≥ 1`), plus `media_scale:{<media>: coefficient}` and `candidate_floor:{<media>:{mb_s,iops}}` | Nested; any subset of leaves accepted. `<media>` is one of `nvme`, `sata_ssd`, `mmc`, `hdd`, `usb`, `unknown`; coefficients in `(0,1]`, floors `> 0` |

`network_control` is a section of this same endpoint too; its fields are documented in detail above.

`GET disk_pressure` answers with the values actually in force — `config.disk_pressure_model` merged over the built-in defaults — so a knob absent from `config.yaml` reads back as the default the model uses rather than `null`. The per-media half-point tables (`await_half_ms`, `queue_half`, `util_half_pct`), `activity_util_pct` and `disk_psi_weights` are not writable here: they are calibrated per device class and stay config-only.

`disk_io.rate` is calibrated for NVMe. `media_scale` multiplies it before the cap is written to a disk of that media class, and `candidate_floor` is the per-disk rate an app must reach before it is considered worth throttling at all (either `mb_s` or `iops` clearing the floor qualifies). Both are per-class merges over the built-in defaults, so a partial `POST` leaves the other classes alone. `unknown` covers a device that matches no rule and defaults to the HDD numbers.

**Request Example (`POST /monitor/config/system_pressure`):**
```json
{ "thresholds": { "high": 0.75 }, "weights": { "memory": 6 }, "expected_updated_at": 1718600000 }
```

**Response (Success):**
```json
{
  "retcode": 0,
  "retmsg": "Successfully updated system_pressure configuration",
  "data": {
    "thresholds": { "low": 0.4, "medium": 0.6, "high": 0.75, "critical": 1.0 },
    "weights": { "cpu": 2, "memory": 6, "io": 1 },
    "mem_gate_steepness": 8.0,
    "memory_busy_threshold": 80.0,
    "cpu_busy_threshold": 90.0,
    "success": true,
    "updated_at": 1718603800
  }
}
```

---

## Notes

### Background Caching

Several monitor endpoints use background threads for performance:
- `/monitor/dynamic_info` — full-snapshot collector refreshes every 2 seconds. It only runs when `monitored_sections` is non-empty, and collects just the monitored sections. When `monitored_sections` is `[]`, no background collector runs and all section requests are served on demand.
- `/monitor/app_resource_stats` — refreshes every 2 seconds (parks when idle >5.5s)
- `/monitor/app_disk_io_stats` — refreshes every 2 seconds (same cache thread)

Section-scoped requests (`/monitor/dynamic_info?sections=...`, `/monitor/dynamic_info/<section>`) use a short per-section TTL cache to coalesce rapid on-demand polls and never start the full collector.

### SSE Connection

The `/app/events` endpoint maintains a persistent connection:
- Sends a `{"type": "connected"}` event on connection
- Sends heartbeat comments every 30 seconds during idle periods
- App status change events include: `app_id`, `app_name`, `status`, `purpose`
- `purpose: "notify"` events may carry extra keys (`sys_level`, `disk_level`, `detail`) — see the `/app/events` table above. The Balancer tab relies on them instead of polling: it loads the auto-limited list once and then updates it from these events
