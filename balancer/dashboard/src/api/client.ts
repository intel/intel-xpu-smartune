import axios from 'axios'
import type {
  ApiResponse,
  AppResourceStatsData,
  AppDiskIoStatsData,
  ProcessListData,
  AppListData,
  StaticInfoData,
  DynamicInfoData,
  HistoryData,
  HistoryQueryOptions,
  HistoryRetentionData,
  SaveResult,
  SetControlPayload,
  AppIdPayload,
  SetPriorityPayload,
  ResourceLimitPayload,
  ResourceLimitProfileData,
  WeightsTopData,
  PassiveControlData,
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
  getAppResourceStats: (n = 10) => get<AppResourceStatsData>(`/monitor/app_resource_stats?n=${n}`),
  getAppDiskIoStats: (n = 10) => get<AppDiskIoStatsData>(`/monitor/app_disk_io_stats?n=${n}`),
  getProcesses: () => get<ProcessListData>('/monitor/processes'),
  getStaticInfo: () => get<StaticInfoData>('/monitor/static_info'),
  refreshStaticInfo: () => get<StaticInfoData>('/monitor/static_info?force_refresh=1'),
  getDynamicInfo: () => get<DynamicInfoData>('/monitor/dynamic_info'),
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
  getControlledApps: () => post<AppListData>('/app/get_controlled_app'),
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
  removeFromControl: (payload: AppIdPayload) =>
    post<void>('/app/remove_from_control', payload),
  setPriority: (payload: SetPriorityPayload) =>
    post<void>('/app/set_priority', payload),
  setOomScore: (payload: Pick<AppIdPayload, 'app_id'>) =>
    post<void>('/app/set_oom_score', payload),
  cancelRelaunch: (payload: Pick<AppIdPayload, 'app_id'>) =>
    post<void>('/app/cancel_relaunch', payload),
  resourceLimit: (payload: ResourceLimitPayload) =>
    post<void>('/app/resource_limit', payload),
  getResourceLimitProfile: (payload: Pick<ResourceLimitPayload, 'app_id' | 'app_name' | 'priority'>) =>
    post<ResourceLimitProfileData>('/app/resource_limit_profile', payload),
  resourceRestore: (payload: Pick<AppIdPayload, 'app_id'>) =>
    post<void>('/app/resource_restore', payload),
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

  getPassiveControl: () => get<PassiveControlData>('/monitor/config/passive_control'),
  updatePassiveControl: (enabled: boolean, expectedUpdatedAt?: number) =>
    postWithConflict<{
      success: boolean
      enabled: boolean
      updated_at: number
    }>('/monitor/config/passive_control', { enabled, expected_updated_at: expectedUpdatedAt }),
}
