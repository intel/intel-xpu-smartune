// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import axios from 'axios'
import type { AxiosRequestConfig } from 'axios'
import type {
  ApiResponse,
  AppResourceStatsData,
  AppDiskIoStatsData,
  ProcessListData,
  ProcessDetailData,
  AppListData,
  AutoLimitedAppsData,
  AutoLimitExclusionsData,
  StaticInfoData,
  DynamicInfoData,
  HistoryData,
  HistoryQueryOptions,
  HistoryRetentionData,
  SaveResult,
  SetControlPayload,
  AppIdPayload,
  SetPriorityPayload,
  SetNetworkPriorityPayload,
  ResourceLimitPayload,
  ResourceLimitProfileData,
  WeightsTopData,
  PassiveControlData,
  MonitoredSectionsData,
  DiscoverSearchData,
  DiscoverExtractData,
  WizardCommitPayload,
  WizardCommitData,
  BenchActionResult,
  BenchDevice,
  BenchEnvData,
  BenchJob,
  BenchModelsData,
  BenchPrecision,
  BenchMatrixData,
  BenchResultsData,
  BenchTimelineData,
  BenchRunsData,
  BenchStage,
  BenchPreflightData,
  BenchQuietModeState,
  BenchRunSampleData,
} from './types'

// Server uses RetCode.CONFLICT (409) for optimistic-concurrency mismatches
// on shared global config (weights_top, history retention).  Kept in sync
// with balancer/utils/http_utils.py.
const RETCODE_CONFLICT = 409

const client = axios.create({
  baseURL: '/api',
  timeout: 10000,
  headers: { 'Content-Type': 'application/json' },
})

// --- Access token ---------------------------------------------------------
// The server (balancer + monitor) enforces an X-Auth-Token on every endpoint.
// We keep the token the operator handed the user in localStorage, attach it to
// every request, and expose helpers for the login gate and SSE stream.
const TOKEN_STORAGE_KEY = 'smartune_api_token'
const AUTH_HEADER = 'X-Auth-Token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_STORAGE_KEY)
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_STORAGE_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_STORAGE_KEY)
}

/**
 * Consume the one-shot token supplied by the desktop launcher in the URL hash.
 * Removing it immediately prevents credentials from lingering in the address bar
 * or being accidentally reused after a refresh.
 */
export function consumeUrlToken(): string | null {
  const hash = window.location.hash.startsWith('#')
    ? window.location.hash.slice(1)
    : window.location.hash
  const token = new URLSearchParams(hash).get('token')
  if (token) {
    window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`)
  }
  return token || null
}

// Registered by App so a 401 anywhere can bounce the user back to the login gate.
let onUnauthorized: (() => void) | null = null
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler
}

// Attach the token to every outgoing request.
client.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers = config.headers ?? {}
    ;(config.headers as Record<string, string>)[AUTH_HEADER] = token
  }
  return config
})

// --- Backend reachability tracking ---------------------------------------
// Count consecutive "server unreachable" failures. Two shapes mean the backend
// is down: (a) a network-level error with no HTTP response (ECONNREFUSED,
// timeout, ...) — this is what the browser sees in production; (b) a gateway
// error (502/503/504) — this is what the Vite dev proxy synthesizes when it
// cannot reach the upstream (see vite.config.ts), so a dead backend arrives as
// an HTTP response rather than a network error. Any other response — even a 401
// or 500 from the real backend — means the server is reachable and resets the
// count. Pollers (see usePolling) consult isBackendUnreachable() to back off
// instead of hammering a dead backend.
const MAX_CONSECUTIVE_ERRORS = 3
const GATEWAY_ERROR_STATUSES = new Set([502, 503, 504])
let consecutiveNetworkErrors = 0

export function isBackendUnreachable(): boolean {
  return consecutiveNetworkErrors >= MAX_CONSECUTIVE_ERRORS
}

// A 401 means the token is missing/invalid/revoked: drop it and prompt re-login.
client.interceptors.response.use(
  (res) => {
    consecutiveNetworkErrors = 0
    return res
  },
  (error) => {
    const status = error?.response?.status
    if (status === undefined || GATEWAY_ERROR_STATUSES.has(status)) {
      // No response (network-level failure) or a gateway error from the dev
      // proxy → the backend is down / unreachable.
      consecutiveNetworkErrors += 1
    } else {
      // Got a real HTTP response from the backend → server is reachable.
      consecutiveNetworkErrors = 0
      if (status === 401) {
        clearToken()
        onUnauthorized?.()
      }
    }
    return Promise.reject(error)
  },
)

/**
 * Validate a token against the server via /auth/login. On success the token is
 * persisted so subsequent requests carry it. The login endpoint itself is
 * exempt from the token gate, so this can run before any token is stored.
 */
export async function login(token: string): Promise<boolean> {
  const res = await client.post<ApiResponse<{ authenticated: boolean }>>(
    '/auth/login',
    { pwd: token },
    { headers: { [AUTH_HEADER]: token } },
  )
  const ok = res.data.retcode === 0 && res.data.data?.authenticated === true
  if (ok) setToken(token)
  return ok
}

/**
 * URL for the SSE stream with the token in the query string. EventSource cannot
 * set custom headers, so the server also accepts the token via ?token= for it.
 */
export function appEventsUrl(): string {
  const token = getToken()
  return token ? `/api/app/events?token=${encodeURIComponent(token)}` : '/api/app/events'
}

// Identifies this browser tab to the benchmark event stream for as long as the
// page is loaded. Toggling the log channel reconnects, and the server uses this
// to retire the connection being replaced rather than counting both against its
// client limit — it cannot otherwise tell a superseded stream from a live one
// until the next heartbeat write fails.
const BENCH_CLIENT_ID = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`

/**
 * URL for the Benchmark tab's SSE stream (benchmark/service/events.py).
 *
 * `withLogs` asks the server to also stream the running job's output. It is off
 * while the tab is off screen: job/env/result events still arrive (that is how a
 * backgrounded dashboard reports a finished run), but a build writes megabytes
 * of log that nobody is looking at.
 */
export function benchEventsUrl(withLogs: boolean): string {
  const params = new URLSearchParams()
  const token = getToken()
  if (token) params.set('token', token)
  if (withLogs) params.set('logs', '1')
  params.set('client', BENCH_CLIENT_ID)
  return `/api/bench/events?${params.toString()}`
}

export function sendHeartbeat(sessionId: string): Promise<void> {
  return post<void>('/smartune/ui/heartbeat', { session_id: sessionId })
}

export function sendUiRelease(sessionId: string): void {
  const token = getToken()
  void fetch('/api/smartune/ui/release', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { [AUTH_HEADER]: token } : {}),
    },
    body: JSON.stringify({ session_id: sessionId }),
    keepalive: true,
  }).catch(() => {})
}

async function get<T>(url: string): Promise<T> {
  const res = await client.get<ApiResponse<T>>(url)
  if (res.data.retcode !== 0) throw new Error(res.data.retmsg)
  return res.data.data
}

async function post<T>(url: string, body: object = {}): Promise<T> {
  const res = await client.post<ApiResponse<T>>(url, body)
  if (res.data.retcode !== 0) throw new Error(res.data.retmsg)
  return res.data.data
}

async function del<T>(url: string, body: object = {}): Promise<T> {
  // Axios puts a DELETE body under `data`, not as the second positional
  // argument -- the one place the verb helpers here are not interchangeable.
  const res = await client.delete<ApiResponse<T>>(url, { data: body })
  if (res.data.retcode !== 0) throw new Error(res.data.retmsg)
  return res.data.data
}

// post-with-conflict: same as post() but returns a tagged union instead of
// throwing on 409 so the UI can prompt the user to reload latest values.
// Other non-zero retcodes still throw, matching the legacy contract.
async function postWithConflict<TOk>(url: string, body: object): Promise<SaveResult<TOk>> {
  const res = await client.post<ApiResponse<TOk & { current?: unknown }>>(url, body)
  if (res.data.retcode === 0) {
    return { status: 'ok', data: res.data.data as TOk }
  }
  if (res.data.retcode === RETCODE_CONFLICT) {
    const payload = (res.data.data ?? {}) as { current?: unknown }
    return { status: 'conflict', current: payload.current ?? null, message: res.data.retmsg }
  }
  throw new Error(res.data.retmsg)
}

// Bench actions treat 409 as an answerable state rather than a failure: the slot
// is busy, or an environment already exists and rebuilding it needs confirmation.
// Every other non-zero retcode still throws, like post().
async function postBench<TOk>(
  url: string,
  body: object = {},
  config?: AxiosRequestConfig,
): Promise<BenchActionResult<TOk>> {
  const res = await client.post<ApiResponse<TOk>>(url, body, config)
  if (res.data.retcode === 0) return { status: 'ok', data: res.data.data }
  if (res.data.retcode === RETCODE_CONFLICT) {
    return { status: 'conflict', data: res.data.data, message: res.data.retmsg }
  }
  throw new Error(res.data.retmsg)
}

export const api = {
  // Server capability level: 1 = balancer + monitor, 0 = monitor only.
  // `benchmark`: 1 = the /bench API is mounted here (NOT that its environment is
  // installed — that comes from getBenchEnv().ready).
  getCapabilities: () =>
    get<{ capabilities: number; benchmark?: number }>('/smartune/capabilities'),
  getAppResourceStats: (n = 10) => get<AppResourceStatsData>(`/monitor/app_resource_stats?n=${n}`),
  getAppDiskIoStats: (n = 10) => get<AppDiskIoStatsData>(`/monitor/app_disk_io_stats?n=${n}`),
  getProcesses: (gpu = false, io = false) => {
    const params = [gpu && 'gpu=1', io && 'io=1'].filter(Boolean)
    return get<ProcessListData>(`/monitor/processes${params.length ? `?${params.join('&')}` : ''}`)
  },
  getProcessDetail: (pid: number) =>
    get<ProcessDetailData>(`/monitor/process_detail?pid=${pid}`),
  getStaticInfo: () => get<StaticInfoData>('/monitor/static_info'),
  refreshStaticInfo: () => get<StaticInfoData>('/monitor/static_info?force_refresh=1'),
  getDynamicInfo: (sections?: string[]) => {
    const sectionList = sections?.map((s) => s.trim()).filter(Boolean) || []
    const query = sectionList.length
      ? `?sections=${encodeURIComponent(sectionList.join(','))}`
      : ''
    return get<DynamicInfoData>(`/monitor/dynamic_info${query}`)
  },
  getHistory: (options: HistoryQueryOptions = {}) => {
    const snapshotType = options.snapshotType ?? 'dynamic'
    const limit = Math.max(1, Math.min(options.limit ?? 100, 20000))
    const params = new URLSearchParams({
      snapshot_type: snapshotType,
      limit: String(limit),
    })

    const hasExplicitRange =
      (typeof options.startTime === 'number' && Number.isFinite(options.startTime)) ||
      (typeof options.endTime === 'number' && Number.isFinite(options.endTime))

    if (typeof options.startTime === 'number' && Number.isFinite(options.startTime)) {
      params.set('start_time', String(Math.floor(options.startTime)))
    }
    if (typeof options.endTime === 'number' && Number.isFinite(options.endTime)) {
      params.set('end_time', String(Math.floor(options.endTime)))
    }
    // range_seconds is only meaningful when the caller did not pin
    // start_time/end_time (custom range path).  The server gives explicit
    // timestamps precedence anyway, but skipping the param keeps URLs tidy.
    if (
      !hasExplicitRange &&
      typeof options.rangeSeconds === 'number' &&
      Number.isFinite(options.rangeSeconds) &&
      options.rangeSeconds > 0
    ) {
      params.set('range_seconds', String(Math.floor(options.rangeSeconds)))
    }

    return get<HistoryData>(`/monitor/history?${params.toString()}`)
  },

  getHistoryRetention: () => get<HistoryRetentionData>('/monitor/history/retention'),
  setHistoryRetention: (days: number, expectedUpdatedAt?: number) =>
    postWithConflict<{ retention_days: number; deleted: number; updated_at: number }>(
      '/monitor/history/retention',
      { retention_days: days, expected_updated_at: expectedUpdatedAt },
    ),

  checkRunningApps: () => post<AppListData>('/app/check_running_apps'),
  getApps: () => post<AppListData>('/app/get_apps'),
  getControlledApps: () =>
    post<AppListData>('/app/get_controlled_app').catch((e: Error) => {
      if (e.message === 'No controlled apps found') return [] as AppListData
      throw e
    }),
  // The server returns retcode=404 (NOT_EXISTING, "No pending apps found") when the
  // pending queue is empty, which makes post() throw.  Treat that specific case as an
  // empty list so the UI clears the pending queue card when the last app goes running.
  // Other errors (network failures, server errors) are re-thrown so callers can handle them.
  getPendingApps: () =>
    post<AppListData>('/app/get_pending_app').catch((e: Error) => {
      if (e.message === 'No pending apps found') return [] as AppListData
      throw e
    }),

  setToControl: (payload: SetControlPayload) =>
    post<void>('/app/set_to_control', payload),
  setPriority: (payload: SetPriorityPayload) =>
    post<void>('/app/set_priority', payload),
  setNetworkPriority: (payload: SetNetworkPriorityPayload) =>
    post<void>('/app/set_network_priority', payload),
  setOomScore: (payload: Pick<AppIdPayload, 'app_id'>) =>
    post<void>('/app/set_oom_score', payload),
  killProcess: (pid: number, force = false) =>
    post<void>('/app/kill_process', { pid, force }),
  suspendProcess: (pid: number, resume = false) =>
    post<void>('/app/suspend_process', { pid, resume }),
  cancelRelaunch: (payload: Pick<AppIdPayload, 'app_id'>) =>
    post<void>('/app/cancel_relaunch', payload),
  // Server returns {skipped: true} (with retmsg = human-readable reason) when
  // the app has negligible usage and no limit was actually applied. That's a
  // successful evaluation, not an error, so post() resolves and the caller can
  // distinguish "applied" vs "skipped" via the response shape.
  resourceLimit: async (payload: ResourceLimitPayload) => {
    const res = await client.post<ApiResponse<{ skipped?: boolean }>>('/app/resource_limit', payload)
    if (res.data.retcode !== 0) throw new Error(res.data.retmsg)
    return { skipped: res.data.data?.skipped === true, message: res.data.retmsg }
  },
  getResourceLimitProfile: (payload: Pick<ResourceLimitPayload, 'app_id' | 'app_name' | 'priority'>) =>
    post<ResourceLimitProfileData>('/app/resource_limit_profile', payload),
  resourceRestore: (payload: Pick<AppIdPayload, 'app_id'>) =>
    post<void>('/app/resource_restore', payload),
  getAutoLimitedApps: () => post<AutoLimitedAppsData>('/app/auto_limited_apps'),
  // Lifts a pressure-driven limit and excludes the app from future ones. Not the same as
  // resourceRestore, which only handles manual limits.
  autoLimitRestore: (payload: Pick<AppIdPayload, 'app_id'>) =>
    post<void>('/app/auto_limit_restore', payload),
  // Safe handoff: flips an auto-limited app to a manual limit WITHOUT releasing its
  // cgroup caps, so the manual buttons unlock with no crash window. See backend
  // lock_to_manual.
  lockToManual: (payload: Pick<AppIdPayload, 'app_id'>) =>
    post<void>('/app/lock_to_manual', payload),
  // "Take Control": adopt a running auto-limit into a newly-controlled app identity so the
  // limit follows the app into management (no release, no duplicate row). See adopt_auto_limit.
  adoptAutoLimit: (payload: { effective_app_id: string; app_id: string; app_name?: string; priority?: string }) =>
    post<void>('/app/adopt_auto_limit', payload),
  getAutoLimitExclusions: () => post<AutoLimitExclusionsData>('/app/auto_limit_exclusions'),
  removeAutoLimitExclusion: (key: string) =>
    post<void>('/app/auto_limit_exclusion_remove', { key }),
  getWeightsTop: () => get<WeightsTopData>('/monitor/config/weights_top'),
  updateWeightsTop: (
    weights: { cpu?: number; memory?: number; gpu?: number },
    expectedUpdatedAt?: number,
  ) =>
    postWithConflict<{
      success: boolean
      updated_weights: WeightsTopData
      updated_at: number
    }>('/monitor/config/weights_top', { ...weights, expected_updated_at: expectedUpdatedAt }),

  // "Add Application" wizard endpoints — see balancer/monitor/app_discovery.py
  // and the /app/discover_* + /app/wizard_commit routes in BalanceService.py.
  discoverSearch: (keywords: string[]) =>
    post<DiscoverSearchData>('/app/discover_search', { keywords }),
  discoverExtract: (pids: number[], name = '') =>
    post<DiscoverExtractData>('/app/discover_extract', { pids, name }),
  // newControlledApp uses a tagged-union return so the wizard can distinguish
  // success / 409-conflict / other-error without try/catch around message
  // parsing.  On conflict the backend includes "with_id" — used by the
  // purge-and-retry path — which would be lost if we threw on retcode != 0.
  newControlledApp: async (payload: WizardCommitPayload):
    Promise<
      | { status: 'ok'; data: WizardCommitData }
      | { status: 'conflict'; conflict: 'id' | 'name' | 'processes';
          withName: string; withId: string; shared?: string[]; message: string }
      | { status: 'error'; message: string }
    > => {
    const res = await client.post<ApiResponse<WizardCommitData & {
      conflict?: 'id' | 'name' | 'processes'
      with?: string
      with_id?: string
      shared?: string[]
    }>>('/app/new_controlled_app', payload)
    if (res.data.retcode === 0) {
      return { status: 'ok', data: res.data.data as WizardCommitData }
    }
    if (res.data.retcode === RETCODE_CONFLICT) {
      const d = res.data.data ?? ({} as Record<string, unknown>)
      return {
        status: 'conflict',
        conflict: (d.conflict as 'id' | 'name' | 'processes') ?? 'id',
        withName: d.with ?? '',
        withId: d.with_id ?? '',
        shared: d.shared,
        message: res.data.retmsg,
      }
    }
    return { status: 'error', message: res.data.retmsg }
  },
  mergeControlledAppProcesses: (payload: Pick<WizardCommitPayload, 'id' | 'process_names' | 'bpf_name'>) =>
    post<{ id: string; name: string; process_names: string[]; bpf_name: string[] }>(
      '/app/merge_controlled_app_processes', payload,
    ),
  // Full replace (add + remove) of an app's identities. The server rejects (throws)
  // when the caller tries to drop a program name that is currently under a live limit.
  setControlledAppProcesses: (payload: Pick<WizardCommitPayload, 'id' | 'process_names' | 'bpf_name'>) =>
    post<{ id: string; name: string; process_names: string[]; bpf_name: string[] }>(
      '/app/set_controlled_app_processes', payload,
    ),
  purgeControlledApp: (id: string) =>
    post<{ id: string; name: string }>('/app/purge_controlled_app', { id }),

  getPassiveControl: () => get<PassiveControlData>('/monitor/config/passive_control'),
  getMonitoredSections: () => get<MonitoredSectionsData>('/monitor/config/monitored_sections'),
  updateMonitoredSections: (sections: string[], expectedUpdatedAt?: number) =>
    postWithConflict<{
      success: boolean
      sections: string[]
      configured_sections: string[] | null
      all_sections: string[]
      updated_at: number
    }>('/monitor/config/monitored_sections', { sections, expected_updated_at: expectedUpdatedAt }),
  updatePassiveControl: (enabled: boolean, expectedUpdatedAt?: number) =>
    postWithConflict<{
      success: boolean
      enabled: boolean
      updated_at: number
    }>('/monitor/config/passive_control', { enabled, expected_updated_at: expectedUpdatedAt }),

  // Generic auto-control config get/set (thresholds, weights, pressure_detection,
  // collection, limit_policy).  These share one parametrized backend endpoint;
  // the section-specific shapes are provided by the caller via the type param.
  // --- Benchmark ---------------------------------------------------------
  getBenchEnv: () => get<BenchEnvData>('/bench/env'),
  // force=false against an existing environment answers 'conflict' so the caller
  // can confirm before spending an hour and tens of GB rebuilding it. Deciding
  // that means reading the venv's package list, which the server refuses to guess
  // at and which costs a cold torch import on the first call after a restart --
  // well past the default client timeout.
  setupBenchEnv: (force = false) =>
    postBench<BenchJob>('/bench/env/setup', { force }, { timeout: 150_000 }),
  getBenchSetupLog: (offset = 0) => get<BenchJob>(`/bench/env/setup/log?offset=${offset}`),
  // Switch the active OpenVINO version. Relinks the pre-built pool, so it returns
  // the fresh environment status directly; a busy execution slot comes back as a
  // 'conflict' the caller surfaces rather than an error toast.
  switchBenchOv: (version: string) =>
    postBench<BenchEnvData>('/bench/env/ov', { version }),

  // The whole cached list, once per session: it runs to a few hundred entries,
  // which is small enough to filter in the browser and saves a request per
  // keystroke. The server still accepts search/limit for other callers.
  getBenchModels: () => get<BenchModelsData>('/bench/models'),
  // A refusal comes back as a 200 with `reason` set -- not being able to search
  // is a state of the machine, not a failed request.
  refreshBenchModels: () =>
    post<{ started: boolean; reason: string | null }>('/bench/models/refresh'),

  // `devices` names the devices to benchmark on; omitting it means all of them,
  // which is what the pipeline did before the choice existed. `ov` is the
  // OpenVINO version a benchmark builds/runs against (chosen on the Models tab,
  // built on demand); omitted for a pure download.
  startBenchRun: (
    // `args` is free-form extra CLI arguments appended to every benchmark
    // run_case for the model (validated server-side in runner.py); ignored by a
    // pure build/download, which never runs a case.
    models: { id: string; build?: BenchPrecision[]; args?: string }[],
    opt: BenchStage,
    devices?: BenchDevice[],
    ov?: string,
  ) => postBench<BenchJob>('/bench/run', { models, opt, devices, ov }),
  getBenchRuns: () => get<BenchRunsData>('/bench/run'),
  getBenchRun: (runId: string, offset?: number) =>
    get<BenchJob>(
      `/bench/run/${encodeURIComponent(runId)}${offset === undefined ? '' : `?offset=${offset}`}`,
    ),
  cancelBenchRun: (runId: string) =>
    post<{ cancelled: boolean }>(`/bench/run/${encodeURIComponent(runId)}/cancel`),

  // Whether a measured run can start right now. Advisory: startBenchRun checks
  // again server-side, because nothing holds the machine's state still between
  // the two calls. A block comes back from startBenchRun as a 409 carrying the
  // same blockers.
  getBenchPreflight: () => get<BenchPreflightData>('/bench/preflight'),

  // Quiet mode is entered automatically when a measured run starts; these are
  // for the in-run control that lets a user trade the run's comparability for
  // live system data.
  getBenchQuietMode: () => get<BenchQuietModeState>('/bench/quiet_mode'),
  setBenchQuietMode: (active: boolean) =>
    post<BenchQuietModeState>('/bench/quiet_mode', { active }),

  // The running run's latest 2 Hz sample. A read of memory the sampler already
  // filled for metrics.csv -- no hardware is queried -- which is what lets the
  // Live tiles be shown under quiet mode without restoring the background
  // collector.
  getBenchRunSample: () => get<BenchRunSampleData>('/bench/run/metrics/latest'),

  getBenchResults: (backend?: string) =>
    get<BenchResultsData>(`/bench/results${backend ? `?backend=${backend}` : ''}`),
  getBenchMatrix: (backend?: string) =>
    get<BenchMatrixData>(`/bench/results/matrix${backend ? `?backend=${backend}` : ''}`),
  getBenchCaseLog: (path: string) =>
    get<{ path: string; content: string }>(
      `/bench/results/log?path=${encodeURIComponent(path)}`,
    ),
  // The samples behind one case's medians. 404s for a case that was never
  // sampled -- an old run, or one whose sampling CSV has since been cleaned up
  // -- which the drawer reports as such rather than as an error.
  getBenchCaseTimeline: (caseDir: string) =>
    get<BenchTimelineData>(
      `/bench/results/timeline?case=${encodeURIComponent(caseDir)}`,
    ),
  // Cases are named by their directories, which the caller has from the matrix.
  // Used when a configuration is re-run and its previous measurement is not
  // worth keeping; a run directory whose last case goes is removed with it.
  deleteBenchCases: (cases: string[]) =>
    del<{ removed: number; runs_removed: number; skipped: string[] }>(
      '/bench/results',
      { cases },
    ),

  getConfig: <T>(section: string) => get<T>(`/monitor/config/${section}`),
  updateConfig: <T extends { updated_at?: number }>(
    section: string,
    values: Record<string, unknown>,
    expectedUpdatedAt?: number,
  ) =>
    postWithConflict<T & { success: boolean; updated_at: number }>(
      `/monitor/config/${section}`,
      { ...values, expected_updated_at: expectedUpdatedAt },
    ),
}
