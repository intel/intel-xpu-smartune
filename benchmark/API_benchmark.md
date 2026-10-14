# Benchmark API Guide

REST reference for the `/bench/*` endpoints served by [`service/bench_api.py`](service/bench_api.py).

These drive the Benchmark tab: building the Python environment, listing and downloading
models, starting and watching runs, and reading results. They are mounted only when the
benchmark feature is present and enabled — check `GET /smartune/capabilities`
(`benchmark: 1`) before offering the tab.

Base URL, TLS, authentication and the unified `{retcode, retmsg, data}` envelope are the
same as the rest of the product; see the [Backend API Guide](../docs/API_ENDPOINTS.md) for
those and for the shared `RetCode` table. Auth is applied app-wide by `smartune_api`'s
`before_app_request` gate, so none of these routes carries a per-route token check — with
one exception noted under `GET /bench/events`.

---

## Overview

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/bench/events` | GET | SSE stream: job, log, env, models, results |
| `/bench/env` | GET | Environment status; also bootstraps the base venv |
| `/bench/env/setup` | POST | Install or rebuild the benchmark Python environment |
| `/bench/env/ov` | POST | Switch the active OpenVINO version |
| `/bench/env/setup/log` | GET | Incremental log of the most recent setup job |
| `/bench/models` | GET | Cached model list, searchable |
| `/bench/models/memory` | GET | Weight / KV-cache / logits footprint for one model |
| `/bench/models/disk` | GET | Free space where weights land |
| `/bench/models/refresh` | POST | Rebuild the cached model list |
| `/bench/models/local` | DELETE | Delete downloaded weights for one model |
| `/bench/run` | POST | Start a pipeline run |
| `/bench/run` | GET | Recent runs plus the job holding the slot |
| `/bench/run/plan` | POST | Advanced phase 1: print-only pass |
| `/bench/run/<run_id>/plan` | GET | The commands a plan job produced |
| `/bench/run/advanced` | POST | Advanced phase 2: run with operator-edited commands |
| `/bench/run/<run_id>` | GET | Status of one run, with optional log tail |
| `/bench/run/<run_id>/cancel` | POST | Request cancellation |
| `/bench/run/metrics/latest` | GET | The running run's most recent sampled row |
| `/bench/preflight` | GET | Whether a measured run can start, and what blocks it |
| `/bench/quiet_mode` | GET | Current quiet-mode state |
| `/bench/quiet_mode` | POST | Raise or drop the quiet-mode gate |
| `/bench/results` | GET | Benchmark results tree |
| `/bench/results/matrix` | GET | Results flattened across run directories |
| `/bench/results` | DELETE | Remove named cases |
| `/bench/results/job` | DELETE | Remove everything one run produced |
| `/bench/results/log` | GET | Tail of a per-case `benchmark.log` |
| `/bench/results/timeline` | GET | Hardware samples taken while one case was measured |

### The single job slot

At most **one** job — a setup, a download, a plan or a benchmark — runs at a time. Anything
that would need the slot while it is taken returns `RetCode.CONFLICT` (409) with the
occupying job in `data.current`, so the UI can offer to watch it instead:

```json
{
  "retcode": 409,
  "retmsg": "A benchmark run is already running. Wait for it to finish or cancel it first.",
  "data": {"current": { /* job object */ }}
}
```

### The job object

Returned by every endpoint that starts or reports a job:

| Field | Type | Description |
|-------|------|-------------|
| id | string | Job / run id |
| kind | string | `setup`, `run`, `plan`, `download` |
| status | string | `RUNNING` or a terminal status |
| returncode | int | Process exit code; `null` while running |
| started_at | float | Epoch seconds |
| finished_at | float | Epoch seconds; `null` while running |
| duration | float | Seconds elapsed so far, or total once finished |
| log_path | string | Path of the job's log file |
| meta | object | Per-kind details (models, stage, devices, ov, …) |

---

## Event stream

### GET /bench/events

**Purpose:** Server-sent events for the Benchmark tab, replacing its polling loop. The
stream opens with a `snapshot` event carrying the whole current state, so a client never
needs a separate "what is going on" request — on first load or after a reconnect.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | logs | No | bool | `1` / `true` / `yes` additionally streams the running job's output as incremental `[start, end)` deltas |
| Query | client | No | string | Identifies the browser tab |
| Query | token | Yes | string | Auth token (see note) |

**Response:** `text/event-stream`. Event names: `snapshot`, `job`, `log`, `env`, `models`,
`results`.

**Notes:**
- **This route authenticates via `?token=`, not the `X-Auth-Token` header** — `EventSource`
  cannot set headers. The path must be listed in `smartune_api._SSE_TOKEN_PATHS` for that to
  work.
- `logs=1` should only be set while the tab is on screen. The small events still reach a
  backgrounded tab (that is how it reports a finished run), but a build writes megabytes
  nobody is reading.
- `client` lets a reconnect (which toggling `logs` causes) replace that tab's stream rather
  than stack a second one on a connection the server cannot yet tell is dead.
- Too many concurrent clients returns `RetCode.EXCEPTION_ERROR` as an ordinary JSON
  response, not a stream.

---

## Environment

### GET /bench/env

**Purpose:** Environment status — venv presence, package versions, OpenVINO columns built on
disk, and whether a run is possible at all. This is also where the base environment installs
itself: it is the request the Benchmark tab opens with, so a missing huggingface-only venv is
bootstrapped here.

**Request:** No parameters.

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully retrieved benchmark environment",
  "data": {
    "enabled": true,
    "env_root": "/opt/intel/smartune/benchmark/runtime",
    "venv_exists": true,
    "venv_usable": true,
    "hf_ready": true,
    "genai_ready": true,
    "ready": true,
    "probing": false,
    "versions": {"huggingface_hub": "0.35.0", "transformers": "4.57.0"},
    "ov_versions": ["2026.3.1", "2026.4.1"],
    "active_ov": "2026.4.1",
    "ov_choices": [],
    "setup_job": null,
    "busy": null
  }
}
```

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| ready / hf_ready | bool | The tab is usable once `huggingface_hub` is available |
| venv_usable | bool | `transformers` present too, i.e. a run can be attempted |
| genai_ready | bool | The genai checkout with `tools/llm_bench` is in place |
| probing | bool | Package version probing still in progress; `versions` may be incomplete |
| ov_versions | string[] | OpenVINO columns actually built on disk |
| setup_job | object | The most recent setup job, or `null` |
| busy | object | The job currently holding the slot, or `null` |

**Notes:**
- A bootstrap started by this very call is reported in the same response (`busy` and
  `setup_job`), rather than leaving the tab to discover it on the next event.
- A failed bootstrap attempt does not turn reading the status into an error — the status is
  what was asked for.

---

### POST /bench/env/setup

**Purpose:** Install, or with `force` rebuild, the benchmark Python environment.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | ov | No | string | Also install this OpenVINO version's complete column (OpenVINO plus every transformers the model router can pick). Omitted = only the genai checkout and the huggingface base venv |
| Body | force | No | bool | Rebuild even when a usable environment exists (default `false`) |

**Request Example:**
```json
{
  "ov": "2026.4.1",
  "force": false
}
```

**Response:** The job object, with `retmsg: "Environment setup started"`.

**Response (already installed):**
```json
{
  "retcode": 409,
  "retmsg": "A usable benchmark environment already exists. Re-run with force to rebuild it.",
  "data": {"status": { /* env status */ }, "already_installed": true}
}
```

**Notes:**
- Everything asked for already being in place, with `force=false`, answers `CONFLICT` rather
  than silently rebuilding: that moves a working environment aside, so the choice belongs to
  the operator — who gets the environment details back to decide with.
- A busy job slot also returns `CONFLICT`, with `data.current` instead.

---

### POST /bench/env/ov

**Purpose:** Switch the active OpenVINO version of the benchmark environment.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | version | Yes | string | A version present in `ov_versions` from `GET /bench/env` |

**Request Example:**
```json
{
  "version": "2026.3.1"
}
```

**Response:** The fresh environment status, with `retmsg: "Switched to OpenVINO 2026.3.1"`.

**Notes:**
- Only relinks the pre-built pool, so it answers inline with the new status rather than
  starting a job.
- An unknown version is `RetCode.ARGUMENT_ERROR`; a busy job slot is `CONFLICT`.

---

### GET /bench/env/setup/log

**Purpose:** Incremental log of the most recent setup job.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | offset | No | int | Byte offset to resume from (default `0`) |

**Response:** A job view: the job object plus `log` (the `[offset, end)` delta) and the new
`offset`.

**Notes:**
- Returns `RetCode.NOT_EXISTING` when no setup has been run in this session.

---

## Models

### GET /bench/models

**Purpose:** The cached, benchmarkable model list.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | search | No | string | Filter by name |
| Query | limit | No | int | Max rows; `0` or absent = no limit |

**Response:** The model list payload, with `retmsg: "Successfully retrieved model list"`.

**Notes:**
- Served from a cache; enumerating models on HuggingFace takes minutes. See
  `benchmark.prefetch_models` and `benchmark.models_cache_ttl_days` in `config.yaml`, and
  `POST /bench/models/refresh` to rebuild on demand.
- The list is served **without** the memory block so the tab loads in seconds — use
  `GET /bench/models/memory` per model.

---

### GET /bench/models/memory

**Purpose:** The weight / KV-cache / logits footprint for one model, computed on demand and
then cached, so a second open is free.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | model | Yes | string | HuggingFace model id, e.g. `Qwen/Qwen3-8B` |

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Memory footprint ready",
  "data": {
    "model": "Qwen/Qwen3-8B",
    "memory": { "weights": 16106127360, "kv_cache": 2147483648 }
  }
}
```

**Notes:**
- The id is a **query parameter, not a path segment**: a HuggingFace id carries a slash that
  a path would split.
- `memory: null` with `retmsg: "Memory footprint unavailable"` is a normal "not known" — an
  offline machine, or a model whose config the hub would not give up — not an error.

---

### GET /bench/models/disk

**Purpose:** Free space where downloaded weights land, for the pre-download check.

**Request:** No parameters.

**Response:** A disk-usage payload (`total` / `free` / `path`).

**Notes:**
- Its own route rather than a field on `GET /bench/env`: the figure is only interesting at
  the moment Download is pressed, and an env payload cached since the tab opened would answer
  with the free space of an hour ago. Reading it is a `statvfs`, so asking per click costs
  nothing.

---

### POST /bench/models/refresh

**Purpose:** Rebuild the cached model list in the background.

**Request:** No parameters.

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Model list refresh started",
  "data": {"started": true, "reason": null}
}
```

**Notes:**
- A refusal is still `retcode: 0` with `started: false` and the reason in both `data.reason`
  and `retmsg`. Not being able to search is a normal state of the machine (no benchmark
  environment, or a search already in flight), and the reason is what the tab shows.

---

### DELETE /bench/models/local

**Purpose:** Give back the disk one model's downloaded weights are holding.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | model | Yes | string | HuggingFace model id |
| Body | precisions | No | string[] | Which precisions to remove; absent = all of them |

**Request Example:**
```json
{
  "model": "Qwen/Qwen3-8B",
  "precisions": ["fp16"]
}
```

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Removed fp16 weights for Qwen/Qwen3-8B",
  "data": {
    "model": "Qwen/Qwen3-8B",
    "removed": ["fp16"],
    "skipped": [],
    "freed_bytes": 16106127360
  }
}
```

**Notes:**
- Deletion is per precision, because that is how they were fetched: one directory each, and
  keeping int4 while dropping fp16 is the ordinary request.
- The model is named by its HuggingFace id, never by a path; the directory is derived the
  same way the download does and confined to the models root.
- A busy job slot returns `CONFLICT` — a download or benchmark in flight is reading exactly
  these files.
- Nothing found by that name and precision returns `RetCode.NOT_EXISTING`: reporting
  "freed 0 bytes" as a success reads as a bug.
- Emits a `model.weights.deleted` action event and republishes the models list over SSE.

---

## Runs

### POST /bench/run

**Purpose:** Start a pipeline run over the selected models.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | models | Yes | string[] | Model ids to run |
| Body | opt / stage | No | string | Pipeline stage (default `all`); `stage` is the alias |
| Body | devices | No | string[] | Devices to run on; absent = every device |
| Body | ov | Conditional | string | OpenVINO version to build/run against. Required for a benchmark, ignored for a build/download |

**Request Example:**
```json
{
  "models": ["Qwen/Qwen3-8B"],
  "stage": "all",
  "devices": ["CPU", "GPU"],
  "ov": "2026.4.1"
}
```

**Response:** The job object, with `retmsg: "Benchmark run started"`.

**Error cases:**

| Condition | retcode | Notes |
|-----------|---------|-------|
| Invalid request (bad models/stage/ov) | `ARGUMENT_ERROR` | |
| Job slot taken | `CONFLICT` | `data.current` carries the occupying job |
| Quiet mode blocked | `CONFLICT` | `data.blocked: true`, `data.blockers` lists what to resolve and where |
| Environment not ready / feature disabled | `NOT_EFFECTIVE` | Actionable, not a server fault |

**Notes:**
- A measured run raises quiet mode, which suspends the balancer's automatic control for its
  duration. See the [Balancer Guide](../docs/balancer_guide.md).

---

### GET /bench/run

**Purpose:** Recent runs plus whichever job currently holds the slot.

**Request:** No parameters.

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully retrieved benchmark runs",
  "data": {
    "current": null,
    "recent": [ /* job objects */ ]
  }
}
```

---

### POST /bench/run/plan

**Purpose:** Phase 1 of an advanced run — launch a print-only pass. Renders and runs exactly
what `POST /bench/run` would, but with the plan envelope on, so it lists the per-case
benchmark commands without measuring or writing anything.

**Request:** Same body as `POST /bench/run`.

**Response:** The job object, with `retmsg: "Benchmark plan started"`.

**Notes:**
- Then poll `GET /bench/run/<run_id>/plan` for the commands, edit them, and send them back to
  `POST /bench/run/advanced`.
- Same error cases as `POST /bench/run`, minus the quiet-mode block — a plan measures nothing.

---

### GET /bench/run/&lt;run_id&gt;/plan

**Purpose:** The commands a plan job has produced, and whether it has finished.

**Request:** `run_id` in the path.

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully retrieved benchmark plan",
  "data": {
    "job": { /* job object */ },
    "ready": true,
    "commands": {"qwen3-8b__CPU__fp16": "python3 benchmark.py ..."}
  }
}
```

**Notes:**
- `ready` flips true once the plan job leaves `RUNNING`; until then `commands` is whatever it
  has flushed so far.
- The job's own status rides along, so a caller that only polls this endpoint can also see a
  plan that failed.
- An unknown `run_id` returns `RetCode.NOT_EXISTING`.

---

### POST /bench/run/advanced

**Purpose:** Phase 2 of an advanced run — run the request with the operator's edits.

**Request:** Same body as `POST /bench/run`, plus:

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | commands | Yes | object | Map of `case_key` → the edited command |

**Response:** The job object, with `retmsg: "Advanced benchmark run started"`.

**Notes:**
- The body must describe the **same** models / stage / devices / ov the plan was built from,
  so the rendered `case_key`s line up with the keys here. A key that matches no case simply
  never fires.
- `commands` must be an object, otherwise `RetCode.ARGUMENT_ERROR`.
- Same error cases as `POST /bench/run`, quiet-mode block included.

---

### GET /bench/run/&lt;run_id&gt;

**Purpose:** Status of one run, with the log tail when `?offset=` is supplied.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Path | run_id | Yes | string | Job id |
| Query | offset | No | int | Byte offset to resume the log from; **absent means no log is returned at all** |

**Response:** A job view: the job object, plus `log` and the new `offset` when `offset` was
given.

**Notes:**
- An unknown `run_id` returns `RetCode.NOT_EXISTING`.

---

### POST /bench/run/&lt;run_id&gt;/cancel

**Purpose:** Request cancellation of a running job.

**Request:** `run_id` in the path; no body.

**Response:** `{"cancelled": true}` with `retmsg: "Cancellation requested"`.

**Notes:**
- An unknown or already-finished job returns `RetCode.NOT_EFFECTIVE` with
  `{"cancelled": false}`.

---

### GET /bench/run/metrics/latest

**Purpose:** The running run's most recent sampled row — what the Live tiles read under quiet
mode.

**Request:** No parameters.

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully retrieved the latest run sample",
  "data": {
    "row": {"ts": 1755764400, "cpu": 88.2, "memory": 61.0, "gpu": 97.4},
    "sampling": true,
    "period_s": 2.0,
    "quiet_mode": {"active": true, "held": true, "owner": "bench-...", "since": 1755764000, "user_exited": false}
  }
}
```

**Notes:**
- A dict lookup, no collection: the row was already produced for `metrics.csv`. Showing the
  tiles therefore costs one read-only poll rather than bringing the 2 s hardware collector
  back.
- Only CPU / memory / GPU / NPU are present — the sampler collects no disk or network
  columns, so those tiles have no source here and the UI hides them.
- `row: null` with `sampling: false` when no run is sampling.

---

## Quiet mode

### GET /bench/preflight

**Purpose:** Whether a measured run can start right now, and what is in the way. Lets the
Models tab say "resolve these two apps first" before the user commits to a run, instead of
taking the click and returning an error.

**Request:** No parameters.

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Successfully retrieved benchmark preflight state",
  "data": {
    "blocked": false,
    "blockers": [],
    "quiet_mode": {"active": false, "held": false, "owner": null, "since": null, "user_exited": false}
  }
}
```

**Notes:**
- Advisory only — `POST /bench/run` re-checks, because nothing holds the machine's state
  still between this call and that one.

---

### GET /bench/quiet_mode

**Purpose:** Current quiet-mode state.

**Request:** No parameters.

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| active | bool | The gate is up: automatic control and the hardware collector are suspended |
| held | bool | A run owns quiet mode (it may have dropped the gate) |
| owner | string | The run holding it, or `null` |
| since | float | Epoch seconds the hold started |
| user_exited | bool | The operator dropped the gate during this hold |

---

### POST /bench/quiet_mode

**Purpose:** Raise or drop the gate for the run that currently holds it.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | active | Yes | bool | `true` raises the gate, `false` drops it |

**Request Example:**
```json
{
  "active": false
}
```

**Response:** The new quiet-mode state, with `retmsg` either `Quiet mode restored` or
`Quiet mode exited; full monitoring resumed`.

**Notes:**
- Dropping the gate is a deliberate choice with a cost the UI spells out first: full
  monitoring comes back, **including the balancer's automatic control**, and the run's numbers
  stop being comparable with other runs.
- Refused with `RetCode.NOT_EFFECTIVE` when no run holds quiet mode — there is nothing to
  toggle outside a run, and silently accepting would let the UI believe it had changed
  something.
- A missing `active` key is `RetCode.ARGUMENT_ERROR`.

---

## Results

### GET /bench/results

**Purpose:** The benchmark results tree.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | backend | No | string | Restrict to one backend; must be a known value |

**Notes:**
- An unknown `backend` returns `RetCode.ARGUMENT_ERROR` listing the valid ones.

---

### GET /bench/results/matrix

**Purpose:** The same cases as `/bench/results`, flattened across run directories, with the
axes and metric descriptors shipped alongside.

**Request:** Same `backend` parameter as above.

**Notes:**
- A run directory holds one device, so comparing devices means reading several of them; this
  view does that join server-side.

---

### DELETE /bench/results

**Purpose:** Remove the named cases from the results tree. Used when a configuration that has
already been measured is re-run and the operator says the old measurement should not be kept.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | cases | Yes | string[] | Case directories, as carried in the matrix the UI is displaying. Must be non-empty |

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Removed 3 benchmark case(s)",
  "data": {"removed": 3, "runs_removed": 1, "skipped": []}
}
```

**Notes:**
- Every case is confined to the benchmarks tree before anything is removed.
- Emits a `benchmark.cases.deleted` action event and republishes results over SSE.

---

### DELETE /bench/results/job

**Purpose:** Remove everything one press of Run produced. The whole job rather than a list of
cases: the Results tab lists by job, so that is the unit the operator is looking at when they
decide a run was not worth keeping.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Body | job | Yes | string | Job name, as listed on the Results tab |

**Response:**
```json
{
  "retcode": 0,
  "retmsg": "Removed 12 case(s) in 2 run directory(ies)",
  "data": {"job": "bench-20261013-0912", "removed_runs": 2, "removed_cases": 12, "skipped": []}
}
```

**Notes:**
- The name is matched against the directories that exist, so nothing the browser sends is
  joined onto a path.
- Nothing by that name returns `RetCode.NOT_EXISTING` rather than reporting "removed 0" as a
  success.

---

### GET /bench/results/log

**Purpose:** Tail of a per-case `benchmark.log`, named by a summary row.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | path | Yes | string | Log path from the results row |

**Response:** `{"path": "...", "content": "..."}`.

**Notes:**
- A missing log returns `RetCode.NOT_EXISTING`.

---

### GET /bench/results/timeline

**Purpose:** The hardware samples taken while one case was being measured. The results tables
report a median per case; this is what that median was taken over.

**Request:**

| Type | Parameter | Required | Format | Description |
|------|-----------|----------|--------|-------------|
| Query | case | Yes | string | Case directory |

**Notes:**
- `RetCode.NOT_EXISTING` covers every honest absence at once — a case with no measurement
  window, a run whose sampling CSV is gone, an old tree that predates in-process sampling —
  so the tab can say so rather than drawing an empty chart.
