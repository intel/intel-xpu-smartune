// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import axios from 'axios'
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

export const api = {
  // Server capability level: 1 = balancer + monitor, 0 = monitor only.
  getCapabilities: () => get<{ capabilities: number }>('/smartune/capabilities'),
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
