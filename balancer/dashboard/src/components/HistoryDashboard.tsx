import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, Card, Checkbox, DatePicker, Divider, Dropdown, Empty, Modal, Popover, Segmented, Select, Space, Tooltip as AntTooltip, Typography, message } from 'antd'
import { DownloadOutlined, FilterOutlined, ReloadOutlined, SettingOutlined, DownOutlined } from '@ant-design/icons'
import ExcelJS from 'exceljs'
import {
  Brush,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import dayjs, { type Dayjs } from 'dayjs'
import { api } from '../api/client'
import type { DynamicInfoData, HistoryData, HistoryRetentionData, HistorySnapshotItem, GpuUsageDevice, StaticInfoData } from '../api/types'
import { COLORS } from '../styles/theme'

const { Text, Title } = Typography

interface Props {
  active: boolean
}

type EngineKey = 'vcs' | 'vecs' | 'ccs' | 'rcs' | 'bcs'

interface PressureTrendPoint {
  timestamp: string
  ts: number
  systemPressure: number | null
  diskPressure: number | null
  networkPressure: number | null
}

interface CpuMemTrendPoint {
  timestamp: string
  ts: number
  cpuUtilization: number | null
  pCoreUtilization: number | null
  eCoreUtilization: number | null
  lpeCoreUtilization: number | null
  memoryUtilization: number | null
  pCoreFreqMhz: number | null
  eCoreFreqMhz: number | null
  lpeCoreFreqMhz: number | null
  cpuTemperatureC: number | null
}

interface DiskTrendPoint {
  timestamp: string
  ts: number
  [diskName: string]: string | number | null
}

interface NetworkTrendPoint {
  timestamp: string
  ts: number
  [nicName: string]: string | number | null
}

interface NpuTrendPoint {
  timestamp: string
  ts: number
  npuUtilization: number | null
  npuFreqMhz: number | null
  npuPowerW: number | null
  npuDdrBwMib: number | null
  npuTemperatureC: number | null
  npuTileCount: number | null
  npuMemoryMb: number | null
}

interface GpuTrendPoint {
  timestamp: string
  ts: number
  vcs: number | null
  vecs: number | null
  ccs: number | null
  rcs: number | null
  bcs: number | null
  gt0Freq: number | null
  gt1Freq: number | null
  gt0Act: number | null
  gt0Req: number | null
  gt1Act: number | null
  gt1Req: number | null
  utilization: number | null
  gpuPower: number | null
  pkgPower: number | null
  memUsage: number | null
  gt0Rc6: number | null
  gt1Rc6: number | null
}

interface GpuTrendSeries {
  id: string
  label: string
  isIntegrated: boolean
  points: GpuTrendPoint[]
}

interface CpuPerCoreTrendPoint {
  timestamp: string
  ts: number
  totalUtil: number | null
  pAvgUtil: number | null
  eAvgUtil: number | null
  lpeAvgUtil: number | null
  pAvgFreq: number | null
  eAvgFreq: number | null
  lpeAvgFreq: number | null
  pkgTemp: number | null
  [key: string]: string | number | null
}

interface CpuPerCoreInfo {
  points: CpuPerCoreTrendPoint[]
  coreCount: number
  tempCoreCount: number
  pCoreIndices: number[]
  eCoreIndices: number[]
  lpeCoreIndices: number[]
}

type RangePreset = '5m' | '15m' | '1h' | '6h' | '24h' | 'custom'

interface HistoryNetworkExtra {
  utilization_percent?: number | null
}

interface HistoryNpuSmiExtra {
  utilization_percent?: number | null
  parsed?: {
    utilization?: number | null
    utilization_percent?: number | null
  } | null
}

const RANGE_PRESET_OPTIONS: Array<{ label: string; value: RangePreset }> = [
  { label: '5m', value: '5m' },
  { label: '15m', value: '15m' },
  { label: '1h', value: '1h' },
  { label: '6h', value: '6h' },
  { label: '24h', value: '24h' },
  { label: 'Custom', value: 'custom' },
]

const RANGE_SECONDS: Record<Exclude<RangePreset, 'custom'>, number> = {
  '5m': 5 * 60,
  '15m': 15 * 60,
  '1h': 60 * 60,
  '6h': 6 * 60 * 60,
  '24h': 24 * 60 * 60,
}

const SNAPSHOT_INTERVAL_SECONDS = 5
const HISTORY_LIMIT_CAP = 100000
const MAX_CHART_POINTS = 800

/**
 * Largest-Triangle-Three-Buckets downsampling.
 * Reduces a time-series array to at most `threshold` points while preserving visual shape.
 */
function downsampleLTTB<T>(data: T[], threshold: number, getY: (d: T) => number): T[] {
  const n = data.length
  if (n <= threshold || threshold < 3) return data

  const out: T[] = [data[0]]
  const bucketSize = (n - 2) / (threshold - 2)
  let prevIdx = 0

  for (let b = 1; b < threshold - 1; b++) {
    const bStart = Math.floor((b - 1) * bucketSize) + 1
    const bEnd = Math.min(Math.floor(b * bucketSize) + 1, n - 1)
    const nStart = Math.floor(b * bucketSize) + 1
    const nEnd = Math.min(Math.floor((b + 1) * bucketSize) + 1, n)
    let avgX = 0
    let avgY = 0
    let cnt = 0
    for (let j = nStart; j < nEnd; j++) { avgX += j; avgY += getY(data[j]); cnt++ }
    if (cnt > 0) { avgX /= cnt; avgY /= cnt }

    const px = prevIdx
    const py = getY(data[prevIdx])
    let maxArea = -1
    let maxIdx = bStart
    for (let j = bStart; j < bEnd; j++) {
      const area = Math.abs((px - avgX) * (getY(data[j]) - py) - (px - j) * (avgY - py))
      if (area > maxArea) { maxArea = area; maxIdx = j }
    }

    out.push(data[maxIdx])
    prevIdx = maxIdx
  }

  out.push(data[n - 1])
  return out
}

/** Sum absolute numeric values in an object, skipping timestamp keys */
function sumMetrics(p: Record<string, unknown>): number {
  let s = 0
  for (const [k, v] of Object.entries(p)) {
    if (k === 'timestamp' || k === 'ts') continue
    if (typeof v === 'number' && Number.isFinite(v)) s += Math.abs(v)
  }
  return s
}

function estimateRequiredLimit(
  rangePreset: RangePreset,
  customRange: [Dayjs | null, Dayjs | null] | null,
): number {
  let seconds = 0
  if (rangePreset === 'custom') {
    const start = customRange?.[0]
    const end = customRange?.[1]
    if (start && end && end.unix() >= start.unix()) {
      seconds = end.unix() - start.unix()
    }
  } else {
    seconds = RANGE_SECONDS[rangePreset]
  }

  if (seconds <= 0) return 100
  const estimated = Math.ceil(seconds / SNAPSHOT_INTERVAL_SECONDS) + 20
  return Math.max(100, Math.min(estimated, HISTORY_LIMIT_CAP))
}

function formatHistoryAxisTick(val: string | number): string {
  const text = String(val)
  if (text.length >= 11) return text.slice(0, 11) // MM-DD HH:mm
  return text
}

const DISK_COLORS = ['#e07b54', '#73bf69', COLORS.accent, COLORS.yellow, COLORS.red, '#b877db', '#56c8d8']
const NETWORK_COLORS = ['#73bf69', COLORS.accent, '#e07b54', COLORS.yellow, '#b877db', '#56c8d8', COLORS.red]

// Fixed per-metric colors for disk and network charts
const METRIC_COLORS = { util: '#56c8d8', read: '#73bf69', write: '#e07b54', rx: '#73bf69', tx: '#e07b54' }

const ENGINE_CURVE_META: Array<{ key: EngineKey; name: string; color: string }> = [
  { key: 'vcs', name: 'VCS %', color: '#f9c74f' },
  { key: 'vecs', name: 'VECS %', color: '#ff9f1c' },
  { key: 'ccs', name: 'CCS %', color: '#5aa9ff' },
  { key: 'rcs', name: 'RCS %', color: '#4cc9f0' },
  { key: 'bcs', name: 'BCS %', color: '#ff6b9d' },
]

const P_CORE_COLORS = ['#ff7f00', '#ffd700', '#a65628', '#d4a017', '#ffa500', '#b8860b', '#cd853f', '#daa520']
const E_CORE_COLORS = ['#377eb8', '#4daf4a', '#17becf', '#1e90ff', '#00ced1', '#7cfc00', '#20b2aa', '#2e8b57', '#4682b4', '#32cd32', '#00ffff', '#008b8b', '#6495ed', '#7fffd4', '#00fa9a', '#87ceeb']
const LPE_CORE_COLORS = ['#984ea3', '#f781bf', '#ba55d3', '#e879f9', '#9370db', '#ff69b4', '#c71585', '#dda0dd']

const SECTION_OPTIONS = [
  { label: 'System Pressure', value: 'pressure' },
  { label: 'CPU & Memory', value: 'cpuMem' },
  { label: 'CPU Per-Core', value: 'cpuPerCore' },
  { label: 'Disk I/O', value: 'disk' },
  { label: 'Network I/O', value: 'network' },
  { label: 'NPU', value: 'npu' },
  { label: 'GPU', value: 'gpu' },
]
const ALL_SECTIONS = SECTION_OPTIONS.map((o) => o.value)

function normalizePercent(value: unknown): number | null {
  if (typeof value !== 'number' || Number.isNaN(value)) return null
  return Math.max(0, Math.min(value, 100))
}

// PSI pressure values come from /proc/pressure as 0-1 fractions; promote to %.
function normalizePressureFraction(value: unknown): number | null {
  if (typeof value !== 'number' || Number.isNaN(value)) return null
  return Math.max(0, Math.min(value * 100, 100))
}

function toNumber(value: unknown): number | null {
  if (typeof value !== 'number' || Number.isNaN(value)) return null
  return value
}

/** Shared tooltip formatter — limits displayed precision to `digits` decimal places */
function tooltipFmt(digits = 2) {
  return (val: number, name: string) => {
    if (typeof val !== 'number' || Number.isNaN(val)) return [val, name]
    return [Number(val.toFixed(digits)), name]
  }
}
const tooltipFmt2 = tooltipFmt(2)

/** Format a numeric hover panel value with its unit string */
function formatHoverValue(value: unknown, unit: string): string {
  const n = typeof value === 'number' ? value : parseFloat(String(value))
  if (Number.isNaN(n)) return '—'
  if (unit === '%') return `${n.toFixed(1)}%`
  if (unit === '°C') return `${n.toFixed(1)}°C`
  return `${n.toFixed(1)} ${unit}`
}

function isNumber(value: number | null): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function toDynamicData(item: HistorySnapshotItem): DynamicInfoData | null {
  if (item.snapshot_type !== 'dynamic') return null
  if (!item.data || typeof item.data !== 'object' || Array.isArray(item.data)) return null
  return item.data as DynamicInfoData
}

function buildTimestamp(item: HistorySnapshotItem): { ts: number; label: string } {
  const tsSec = Number.isFinite(item.create_time) ? item.create_time : 0
  const label = tsSec > 0
    ? dayjs.unix(tsSec).format('MM-DD HH:mm:ss')
    : (item.collected_at || item.create_date || `#${item.id}`)
  return { ts: tsSec, label }
}

function getPressurePeak(dynamic: DynamicInfoData | null): number | null {
  const pressureCpu = normalizePressureFraction(dynamic?.pressure?.cpu)
  const pressureMemory = normalizePressureFraction(dynamic?.pressure?.memory)
  const pressureIo = normalizePressureFraction(dynamic?.pressure?.io)
  const values = [pressureCpu, pressureMemory, pressureIo].filter(isNumber)
  if (!values.length) return null
  return Math.max(...values)
}

function getNetworkUsage(dynamic: DynamicInfoData | null): number | null {
  if (!dynamic) return null
  const network = dynamic.network as DynamicInfoData['network'] & HistoryNetworkExtra
  // utilization_percent is already a percentage (0-100) from backend; use toNumber to avoid normalizePercent ×100
  const utilization = toNumber(network?.utilization_percent)
  if (utilization != null) return utilization
  // Fallback: NetworkMonitor rx/tx fractions (0-1) stored in the pressure section.
  const p = dynamic.pressure as { network_rx?: unknown; network_tx?: unknown } | undefined
  const rx = normalizePressureFraction(p?.network_rx)
  const tx = normalizePressureFraction(p?.network_tx)
  const vals = [rx, tx].filter((v): v is number => v != null)
  return vals.length > 0 ? Math.max(...vals) : null
}

function getNpuUsage(dynamic: DynamicInfoData | null): number | null {
  if (!dynamic?.npu?.npu_smi) return null
  const npuSmi = dynamic.npu.npu_smi as DynamicInfoData['npu']['npu_smi'] & HistoryNpuSmiExtra
  const direct = normalizePercent(npuSmi.utilization_percent)
  if (direct != null) return direct
  return normalizePercent(npuSmi.parsed?.utilization_percent ?? npuSmi.parsed?.utilization)
}

function normalizeEngineUtil(device: GpuUsageDevice, key: EngineKey): number | null {
  return normalizePercent(device.engine_util?.[key])
}

function getFreq(device: GpuUsageDevice, targetName: 'gt0' | 'gt1'): number | null {
  const freq = (device.freqs || []).find((item) => item.name === targetName)
  if (!freq) return null
  return toNumber(freq.act_mhz)
}

function getReqFreq(device: GpuUsageDevice, targetName: 'gt0' | 'gt1'): number | null {
  const freq = (device.freqs || []).find((item) => item.name === targetName)
  if (!freq) return null
  return toNumber(freq.cur_mhz)
}

function getGpuLabel(device: GpuUsageDevice, index: number): string {
  const type = `${device.dev_type || ''}`.toLowerCase()
  const role = type.includes('integrated') || type.includes('igpu')
    ? 'iGPU'
    : (type.includes('discrete') || type.includes('dgpu') ? 'dGPU' : `GPU${index}`)
  const pci = device.pci_dev ? ` (${device.pci_dev})` : ''
  return `${role}${pci}`
}

function buildPressureTrendPoints(items: HistorySnapshotItem[]): PressureTrendPoint[] {
  return [...items].reverse().map((item) => {
    const dynamic = toDynamicData(item)
    const { ts, label } = buildTimestamp(item)
    // System pressure: use persisted composite score (0-1 → 0-100%); fall back to
    // PSI peak for older snapshots that predate the score field being stored.
    const scoreRaw = (dynamic?.pressure as Record<string, unknown> | undefined)?.score
    const systemPressure = isNumber(scoreRaw as number | null)
      ? Math.round((scoreRaw as number) * 100)
      : getPressurePeak(dynamic)
    // Disk pressure: busy-disk ratio from _compute_disk_pressure (busy_disks/total_disks × 100)
    const diskBusyPct = (dynamic?.disk as Record<string, unknown> | undefined)?.busy_pct
    const diskPressure = isNumber(diskBusyPct as number | null) ? (diskBusyPct as number) : null
    // Network pressure: busy-NIC ratio from _compute_network_pressure (busy_nics/total_nics × 100)
    const netBusyPct = (dynamic?.pressure as Record<string, unknown> | undefined)?.network_busy_pct
    const networkPressure = isNumber(netBusyPct as number | null) ? (netBusyPct as number) : null
    return {
      timestamp: label,
      ts,
      systemPressure,
      diskPressure,
      networkPressure,
    }
  })
}

function buildCpuMemTrendPoints(items: HistorySnapshotItem[]): CpuMemTrendPoint[] {
  return [...items].reverse().map((item) => {
    const dynamic = toDynamicData(item)
    const { ts, label } = buildTimestamp(item)
    return {
      timestamp: label,
      ts,
      cpuUtilization: toNumber(dynamic?.cpu?.usage_total),
      pCoreUtilization: toNumber(dynamic?.cpu?.p_core_usage),
      eCoreUtilization: toNumber(dynamic?.cpu?.e_core_usage),
      lpeCoreUtilization: toNumber((dynamic?.cpu as Record<string, unknown> | undefined)?.lpe_core_usage as number | null),
      memoryUtilization: toNumber(dynamic?.memory?.usage_percent),
      pCoreFreqMhz: toNumber((dynamic?.cpu as Record<string, unknown> | undefined)?.p_core_freq_mhz as number | null),
      eCoreFreqMhz: toNumber((dynamic?.cpu as Record<string, unknown> | undefined)?.e_core_freq_mhz as number | null),
      lpeCoreFreqMhz: toNumber((dynamic?.cpu as Record<string, unknown> | undefined)?.lpe_core_freq_mhz as number | null),
      cpuTemperatureC: toNumber((dynamic?.cpu as Record<string, unknown> | undefined)?.temperature_c as number | null),
    }
  })
}

function buildCpuPerCoreTrendPoints(items: HistorySnapshotItem[]): CpuPerCoreInfo {
  const pCoreSet = new Set<number>()
  const eCoreSet = new Set<number>()
  const lpeCoreSet = new Set<number>()
  let maxLogicalCore = -1
  let maxTempCore = -1

  const points = [...items].reverse().map((item) => {
    const dynamic = toDynamicData(item)
    const { ts, label } = buildTimestamp(item)
    const cpu = dynamic?.cpu as Record<string, unknown> | undefined

    const point: CpuPerCoreTrendPoint = {
      timestamp: label,
      ts,
      totalUtil: toNumber(cpu?.usage_total as number | null),
      pAvgUtil: toNumber(cpu?.p_core_usage as number | null),
      eAvgUtil: toNumber(cpu?.e_core_usage as number | null),
      lpeAvgUtil: toNumber(cpu?.lpe_core_usage as number | null),
      pAvgFreq: toNumber(cpu?.p_core_freq_mhz as number | null),
      eAvgFreq: toNumber(cpu?.e_core_freq_mhz as number | null),
      lpeAvgFreq: toNumber(cpu?.lpe_core_freq_mhz as number | null),
      pkgTemp: toNumber(cpu?.temperature_c as number | null),
    }

    const perCoreUsage = (cpu?.per_core_usage as number[] | undefined) || []
    const perCoreFreq = (cpu?.per_core_freq_mhz as Array<number | null> | undefined) || []
    const perCoreTemp = (cpu?.per_core_temperature_c as Array<number | null> | undefined) || []
    const pIndices = (cpu?.p_core_indices as number[] | undefined) || []
    const eIndices = (cpu?.e_core_indices as number[] | undefined) || []
    const lpeIndices = (cpu?.lpe_core_indices as number[] | undefined) || []

    pIndices.forEach((i) => pCoreSet.add(i))
    eIndices.forEach((i) => eCoreSet.add(i))
    lpeIndices.forEach((i) => lpeCoreSet.add(i))

    perCoreUsage.forEach((v, i) => {
      point[`u_${i}`] = normalizePercent(v)
      if (i > maxLogicalCore) maxLogicalCore = i
    })
    perCoreFreq.forEach((v, i) => {
      point[`f_${i}`] = toNumber(v)
      if (i > maxLogicalCore) maxLogicalCore = i
    })
    perCoreTemp.forEach((v, i) => {
      point[`t_${i}`] = toNumber(v)
      if (i > maxTempCore) maxTempCore = i
    })

    return point
  })

  return {
    points,
    coreCount: maxLogicalCore + 1,
    tempCoreCount: maxTempCore + 1,
    pCoreIndices: [...pCoreSet].sort((a, b) => a - b),
    eCoreIndices: [...eCoreSet].sort((a, b) => a - b),
    lpeCoreIndices: [...lpeCoreSet].sort((a, b) => a - b),
  }
}

function buildDiskTrendPoints(items: HistorySnapshotItem[]): { points: DiskTrendPoint[]; diskNames: string[]; diskMaxThroughput: Record<string, number> } {
  const diskNameSet = new Set<string>()
  const maxTpMap: Record<string, number> = {}
  const points = [...items].reverse().map((item) => {
    const dynamic = toDynamicData(item)
    const { ts, label } = buildTimestamp(item)
    const point: DiskTrendPoint = { timestamp: label, ts }

    const disk = dynamic?.disk as (DynamicInfoData['disk'] & {
      per_disk?: Record<string, { util?: number | null; read_mb?: number | null; write_mb?: number | null; max_throughput_mb?: number | null } | number | null>
    }) | undefined
    const perDisk = disk?.per_disk
    if (perDisk && typeof perDisk === 'object') {
      for (const [name, val] of Object.entries(perDisk)) {
        diskNameSet.add(name)
        if (val && typeof val === 'object' && ('util' in val || 'read_mb' in val)) {
          // New format
          point[`${name}:util`] = normalizePercent(val.util)
          point[`${name}:read`] = toNumber(val.read_mb)
          point[`${name}:write`] = toNumber(val.write_mb)
          const tp = toNumber(val.max_throughput_mb)
          if (tp != null && tp > 0) maxTpMap[name] = tp
        } else {
          // Old format: single utilization number
          point[`${name}:util`] = typeof val === 'number' ? normalizePercent(val) : null
          point[`${name}:read`] = null
          point[`${name}:write`] = null
        }
      }
    } else if (disk?.disk_io) {
      for (const [name, diskData] of Object.entries(disk.disk_io)) {
        diskNameSet.add(name)
        point[`${name}:util`] = normalizePercent(diskData?.utilization)
        point[`${name}:read`] = toNumber(diskData?.read_kb_per_sec != null ? diskData.read_kb_per_sec / 1024 : null)
        point[`${name}:write`] = toNumber(diskData?.write_kb_per_sec != null ? diskData.write_kb_per_sec / 1024 : null)
      }
    }
    return point
  })
  return { points, diskNames: Array.from(diskNameSet).sort(), diskMaxThroughput: maxTpMap }
}

function buildNetworkTrendPoints(items: HistorySnapshotItem[]): { points: NetworkTrendPoint[]; nicNames: string[]; nicSpeeds: Record<string, number> } {
  const nicNameSet = new Set<string>()
  const speedMap: Record<string, number> = {}
  const points = [...items].reverse().map((item) => {
    const dynamic = toDynamicData(item)
    const { ts, label } = buildTimestamp(item)
    const point: NetworkTrendPoint = { timestamp: label, ts }

    const network = dynamic?.network as (DynamicInfoData['network'] & {
      per_nic?: Record<string, { util?: number | null; rx_mbps?: number | null; tx_mbps?: number | null; rx?: number | null; tx?: number | null; speed_mbps?: number | null } | number | null>
    }) | undefined
    const perNic = network?.per_nic
    if (perNic && typeof perNic === 'object') {
      for (const [name, val] of Object.entries(perNic)) {
        nicNameSet.add(name)
        if (val && typeof val === 'object') {
          const rxTxMax = val.rx != null || val.tx != null ? Math.max(val.rx ?? 0, val.tx ?? 0) : null
          point[`${name}:util`] = toNumber(val.util) ?? normalizePressureFraction(rxTxMax)
          point[`${name}:rx`] = toNumber(val.rx_mbps) ?? null
          point[`${name}:tx`] = toNumber(val.tx_mbps) ?? null
          const sp = toNumber(val.speed_mbps)
          if (sp != null && sp > 0) speedMap[name] = sp
        } else {
          // Old format: single utilization number
          point[`${name}:util`] = typeof val === 'number' ? normalizePercent(val) : null
          point[`${name}:rx`] = null
          point[`${name}:tx`] = null
        }
      }
    } else {
      // Fallback: use aggregated utilization_percent as single line (already 0-100 from backend)
      const aggUtil = toNumber((network as DynamicInfoData['network'] & HistoryNetworkExtra)?.utilization_percent)
      if (aggUtil != null) {
        nicNameSet.add('total')
        point['total:util'] = aggUtil
        point['total:rx'] = null
        point['total:tx'] = null
      }
    }
    return point
  })
  return { points, nicNames: Array.from(nicNameSet).sort(), nicSpeeds: speedMap }
}

function getNpuFreqMhz(dynamic: DynamicInfoData | null): number | null {
  if (!dynamic?.npu?.npu_smi) return null
  const npuSmi = dynamic.npu.npu_smi as DynamicInfoData['npu']['npu_smi'] & { frequency_mhz?: number | null; parsed?: Record<string, unknown> | null }
  // Try top-level first (from history payload), then parsed
  const direct = toNumber(npuSmi.frequency_mhz)
  if (direct != null) return direct
  return toNumber((npuSmi.parsed as Record<string, unknown> | null)?.frequency_mhz as number | null)
}

function getNpuPowerW(dynamic: DynamicInfoData | null): number | null {
  if (!dynamic?.npu?.npu_smi) return null
  const npuSmi = dynamic.npu.npu_smi as DynamicInfoData['npu']['npu_smi'] & { power_w?: number | null; parsed?: Record<string, unknown> | null }
  const direct = toNumber(npuSmi.power_w)
  if (direct != null) return direct
  return toNumber((npuSmi.parsed as Record<string, unknown> | null)?.power_w as number | null)
}

function getNpuDdrBwMib(dynamic: DynamicInfoData | null): number | null {
  if (!dynamic?.npu?.npu_smi) return null
  const npuSmi = dynamic.npu.npu_smi as DynamicInfoData['npu']['npu_smi'] & { noc_bandwidth_mib_per_s?: number | null; parsed?: Record<string, unknown> | null }
  const direct = toNumber(npuSmi.noc_bandwidth_mib_per_s)
  if (direct != null) return direct
  return toNumber((npuSmi.parsed as Record<string, unknown> | null)?.noc_bandwidth_mib_per_s as number | null)
}

function getNpuTemperatureC(dynamic: DynamicInfoData | null): number | null {
  if (!dynamic?.npu?.npu_smi) return null
  const npuSmi = dynamic.npu.npu_smi as DynamicInfoData['npu']['npu_smi'] & { temperature_c?: number | null; parsed?: Record<string, unknown> | null }
  const direct = toNumber(npuSmi.temperature_c)
  if (direct != null) return direct
  return toNumber((npuSmi.parsed as Record<string, unknown> | null)?.temperature_c as number | null)
}

function getNpuTileCount(dynamic: DynamicInfoData | null): number | null {
  if (!dynamic?.npu?.npu_smi) return null
  const npuSmi = dynamic.npu.npu_smi as DynamicInfoData['npu']['npu_smi'] & { tile_config?: number | null; parsed?: Record<string, unknown> | null }
  const direct = toNumber(npuSmi.tile_config)
  if (direct != null) return direct
  const parsed = npuSmi.parsed as Record<string, unknown> | null
  const tc = parsed?.tile_config
  return typeof tc === 'number' ? tc : null
}

function getNpuMemoryMb(dynamic: DynamicInfoData | null): number | null {
  if (!dynamic?.npu?.npu_smi) return null
  const npuSmi = dynamic.npu.npu_smi as DynamicInfoData['npu']['npu_smi'] & { memory_mb?: number | null; parsed?: Record<string, unknown> | null }
  const direct = toNumber(npuSmi.memory_mb)
  if (direct != null) return direct
  // Fallback: parse from memory_bytes in parsed data
  const parsed = npuSmi.parsed as Record<string, unknown> | null
  const mb = parsed?.memory_bytes
  return typeof mb === 'number' && mb > 0 ? mb / (1024 * 1024) : null
}

function buildNpuTrendPoints(items: HistorySnapshotItem[]): NpuTrendPoint[] {
  return [...items].reverse().map((item) => {
    const dynamic = toDynamicData(item)
    const { ts, label } = buildTimestamp(item)
    return {
      timestamp: label,
      ts,
      npuUtilization: getNpuUsage(dynamic),
      npuFreqMhz: getNpuFreqMhz(dynamic),
      npuPowerW: getNpuPowerW(dynamic),
      npuDdrBwMib: getNpuDdrBwMib(dynamic),
      npuTemperatureC: getNpuTemperatureC(dynamic),
      npuTileCount: getNpuTileCount(dynamic),
      npuMemoryMb: getNpuMemoryMb(dynamic),
    }
  })
}

function isIntegratedGpu(device: GpuUsageDevice): boolean {
  const type = `${device.dev_type || ''}`.toLowerCase()
  return type.includes('integrated') || type.includes('igpu')
}

function buildGpuTrendSeries(items: HistorySnapshotItem[]): GpuTrendSeries[] {
  const seriesMap = new Map<string, GpuTrendSeries>()

  for (const item of [...items].reverse()) {
    const dynamic = toDynamicData(item)
    if (!dynamic) continue
    const devices = dynamic.gpu?.gpu_usage?.parsed?.devices || []
    const { ts, label } = buildTimestamp(item)

    devices.forEach((device, index) => {
      const id = device.pci_dev || `${device.dev_type || 'gpu'}-${index}`
      const seriesLabel = getGpuLabel(device, index)
      const integrated = isIntegratedGpu(device)

      if (!seriesMap.has(id)) {
        seriesMap.set(id, { id, label: seriesLabel, isIntegrated: integrated, points: [] })
      }

      // iGPU uses system memory; dGPU uses dedicated VRAM (keyed by card index in vram map)
      const vramEntry = dynamic.gpu?.vram?.[`card${index}`]
      const memUsage = integrated
        ? normalizePercent(dynamic?.memory?.usage_percent)
        : normalizePercent(vramEntry?.usage_percent ?? dynamic?.memory?.usage_percent)

      const point: GpuTrendPoint = {
        timestamp: label,
        ts,
        vcs: normalizeEngineUtil(device, 'vcs'),
        vecs: normalizeEngineUtil(device, 'vecs'),
        ccs: normalizeEngineUtil(device, 'ccs'),
        rcs: normalizeEngineUtil(device, 'rcs'),
        bcs: normalizeEngineUtil(device, 'bcs'),
        gt0Freq: getFreq(device, 'gt0'),
        gt1Freq: getFreq(device, 'gt1'),
        gt0Act: getFreq(device, 'gt0'),
        gt0Req: getReqFreq(device, 'gt0'),
        gt1Act: getFreq(device, 'gt1'),
        gt1Req: getReqFreq(device, 'gt1'),
        utilization: toNumber(device.utilization ?? null),
        gpuPower: toNumber(device.power_w?.gpu ?? null),
        pkgPower: toNumber(device.power_w?.pkg ?? device.power_w?.card ?? null),
        memUsage,
        gt0Rc6: toNumber((device.freqs || []).find((f) => f.name === 'gt0')?.rc6_pct ?? null),
        gt1Rc6: toNumber((device.freqs || []).find((f) => f.name === 'gt1')?.rc6_pct ?? null),
      }

      seriesMap.get(id)?.points.push(point)
    })
  }

  return Array.from(seriesMap.values()).filter((series) => series.points.length > 0)
}

function LegendToggleItem({
  value,
  color,
  hidden,
  onClick,
  dasharray,
}: {
  value: string
  color: string
  hidden: boolean
  onClick: () => void
  dasharray?: string
}) {
  const lineColor = hidden ? 'rgba(120,176,255,0.25)' : color
  return (
    <span
      onClick={onClick}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        cursor: 'pointer',
        marginRight: 12,
        opacity: hidden ? 0.55 : 1,
        userSelect: 'none',
      }}
    >
      <svg width={24} height={6} style={{ verticalAlign: 'middle' }}>
        <line x1={0} y1={3} x2={24} y2={3} stroke={lineColor} strokeWidth={2.5} strokeDasharray={dasharray} />
      </svg>
      <span style={{ color: hidden ? 'rgba(174,191,223,0.5)' : COLORS.textMuted, fontSize: 11, textDecoration: hidden ? 'line-through' : 'none' }}>{value}</span>
    </span>
  )
}

/* ─── Configurable Chart ─── */

interface SeriesConfig {
  key: string
  name: string
  color: string
  unit: string          // e.g. '%', 'MHz', '°C', 'W', 'MiB/s'
  dasharray?: string
  defaultOn?: boolean   // true = shown by default
}

function usePersistedSet(storageKey: string, defaults: string[]): [Set<string>, (key: string) => void, Set<string>] {
  const [active, setActive] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem(storageKey)
      if (raw) {
        const arr = JSON.parse(raw)
        if (Array.isArray(arr)) return new Set(arr as string[])
      }
    } catch { /* ignore */ }
    return new Set(defaults)
  })

  const toggle = useCallback((key: string) => {
    setActive((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      localStorage.setItem(storageKey, JSON.stringify([...next]))
      return next
    })
  }, [storageKey])

  return [active, toggle, new Set(defaults)]
}

function ConfigurableChart<T extends { timestamp: string }>({
  title,
  storageKey,
  allSeries,
  data,
  height = 280,
  showBrush = false,
  hoverPanel = false,
}: {
  title: string
  storageKey: string
  allSeries: SeriesConfig[]
  data: T[]
  height?: number
  showBrush?: boolean
  hoverPanel?: boolean
}) {
  const defaults = useMemo(() => allSeries.filter((s) => s.defaultOn !== false).map((s) => s.key), [allSeries])
  const [active, toggle] = usePersistedSet(storageKey, defaults)
  const [hoverData, setHoverData] = useState<Array<{ name: string; value: unknown; color: string; unit: string }> | null>(null)

  // Determine which unit groups are active, assign left/right Y-axis (max 2)
  const { leftUnit, rightUnit, activeSeries } = useMemo(() => {
    const selected = allSeries.filter((s) => active.has(s.key))
    const unitOrder: string[] = []
    for (const s of selected) {
      if (!unitOrder.includes(s.unit)) unitOrder.push(s.unit)
    }
    const left = unitOrder[0] || '%'
    const right = unitOrder[1] || null

    const mapped = selected.map((s) => ({
      ...s,
      yAxisId: s.unit === left ? 'left' : s.unit === right ? 'right' : 'left',
    }))
    return { leftUnit: left, rightUnit: right, activeSeries: mapped }
  }, [allSeries, active])

  const unitFormat = (unit: string) => (val: string | number) => {
    const n = typeof val === 'number' ? val : parseFloat(String(val))
    if (Number.isNaN(n)) return String(val)
    if (unit === '%') return `${n}%`
    if (unit === '°C') return `${n}°C`
    if (unit === 'W') return `${n}W`
    return `${n}`
  }

  const domainFor = (unit: string | null): [number, string | number] | undefined => {
    if (!unit) return undefined
    if (unit === '%') return [0, 100]
    return [0, 'auto']
  }
  const leftDomain = domainFor(leftUnit)
  const rightDomain = domainFor(rightUnit)

  // Build a colour map keyed by series key for the hover panel
  const colorByKey = useMemo(() => {
    const map: Record<string, string> = {}
    allSeries.forEach((s) => { map[s.key] = s.color })
    return map
  }, [allSeries])

  const unitByKey = useMemo(() => {
    const map: Record<string, string> = {}
    allSeries.forEach((s) => { map[s.key] = s.unit })
    return map
  }, [allSeries])

  const handleMouseMove = useCallback((chartData: { activePayload?: Array<{ dataKey: string; name: string; value: unknown }> }) => {
    if (!hoverPanel) return
    if (chartData?.activePayload?.length) {
      setHoverData(
        chartData.activePayload.map((p) => ({
          name: p.name,
          value: p.value,
          color: colorByKey[p.dataKey] ?? COLORS.textMuted,
          unit: unitByKey[p.dataKey] ?? '',
        }))
      )
    }
  }, [hoverPanel, colorByKey, unitByKey])

  const handleMouseLeave = useCallback(() => {
    if (!hoverPanel) return
    setHoverData(null)
  }, [hoverPanel])

  return (
    <Card
      style={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, borderRadius: 6, marginTop: 16 }}
      bodyStyle={{ padding: 16 }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
        <Text style={{ color: COLORS.textMuted }}>{title}</Text>
      </div>

      {/* Series selector tags */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 0', marginBottom: 10 }}>
        {allSeries.map((s) => {
          const on = active.has(s.key)
          return (
            <span
              key={s.key}
              onClick={() => toggle(s.key)}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 5,
                cursor: 'pointer',
                marginRight: 10,
                padding: '2px 8px',
                borderRadius: 4,
                background: on ? 'rgba(120,176,255,0.08)' : 'rgba(120,176,255,0.02)',
                border: `1px solid ${on ? s.color + '66' : COLORS.border}`,
                opacity: on ? 1 : 0.6,
                userSelect: 'none',
                transition: 'all 0.15s ease',
              }}
            >
              <svg width={16} height={6} style={{ verticalAlign: 'middle' }}>
                <line x1={0} y1={3} x2={16} y2={3} stroke={on ? s.color : COLORS.textMuted} strokeWidth={2} strokeDasharray={s.dasharray} />
              </svg>
              <span style={{
                color: on ? COLORS.text : COLORS.textMuted,
                fontSize: 10,
                letterSpacing: '0.02em',
              }}>
                {on ? '' : '+ '}{s.name} {s.unit}
              </span>
            </span>
          )
        })}
      </div>

      {activeSeries.length === 0 ? (
        <div style={{ height, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <Text style={{ color: COLORS.textMuted }}>Select at least one metric to display</Text>
        </div>
      ) : (
        <div style={{ width: '100%', height }}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart
              data={data}
              margin={{ top: 4, right: rightUnit ? 32 : 16, left: 16, bottom: 0 }}
              onMouseMove={hoverPanel ? handleMouseMove : undefined}
              onMouseLeave={hoverPanel ? handleMouseLeave : undefined}
            >
              <CartesianGrid stroke={`${COLORS.border}99`} strokeDasharray="3 3" />
              <XAxis
                dataKey="timestamp"
                tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                minTickGap={36}
                tickFormatter={formatHistoryAxisTick}
              />
              <YAxis
                yAxisId="left"
                domain={leftDomain}
                tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                tickFormatter={unitFormat(leftUnit)}
                width={64}
                label={{ value: leftUnit, angle: -90, position: 'insideLeft', fill: COLORS.textMuted, fontSize: 12, fontWeight: 600, dy: -4 }}
              />
              {rightUnit && (
                <YAxis
                  yAxisId="right"
                  orientation="right"
                  domain={rightDomain}
                  tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                  tickFormatter={unitFormat(rightUnit)}
                  width={64}
                  label={{ value: rightUnit, angle: 90, position: 'insideRight', fill: COLORS.textMuted, fontSize: 12, fontWeight: 600, dy: -4 }}
                />
              )}
              {!hoverPanel && (
                <Tooltip
                  contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text }}
                  wrapperStyle={{ zIndex: 100 }}
                  formatter={tooltipFmt2}
                />
              )}
              {hoverPanel && (
                <Tooltip
                  cursor={{ stroke: COLORS.accent, strokeWidth: 1, strokeDasharray: '4 2' }}
                  content={() => null}
                />
              )}
              {activeSeries.map((s) => (
                <Line
                  key={s.key}
                  type="monotone"
                  yAxisId={s.yAxisId}
                  dataKey={s.key}
                  name={`${s.name} ${s.unit}`}
                  stroke={s.color}
                  strokeDasharray={s.dasharray}
                  dot={false}
                  strokeWidth={2}
                />
              ))}
              {showBrush && (
                <Brush dataKey="timestamp" height={24} stroke={COLORS.accent} fill={COLORS.bg} travellerWidth={8} />
              )}
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Inline hover panel (replaces floating tooltip when hoverPanel=true) */}
      {hoverPanel && (
        <div
          style={{
            minHeight: 28,
            marginTop: 8,
            padding: '4px 8px',
            borderRadius: 4,
            background: 'rgba(120,176,255,0.04)',
            border: `1px solid ${COLORS.border}`,
            display: 'flex',
            flexWrap: 'wrap',
            gap: '2px 12px',
            alignItems: 'center',
          }}
        >
          {hoverData && hoverData.length > 0 ? (
            hoverData.map((item) => {
              const formatted = formatHoverValue(item.value, item.unit)
              return (
                <span key={item.name} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                  <span style={{ width: 8, height: 8, borderRadius: '50%', background: item.color, flexShrink: 0, display: 'inline-block' }} />
                  <Text style={{ color: COLORS.textMuted, fontSize: 10 }}>{item.name}</Text>
                  <Text style={{ color: item.color, fontSize: 10, fontWeight: 600 }}>{formatted}</Text>
                </span>
              )
            })
          ) : (
            <Text style={{ color: COLORS.textMuted, fontSize: 10 }}>Hover over chart to see values</Text>
          )}
        </div>
      )}
    </Card>
  )
}

function GpuHistoryCard({ series }: { series: GpuTrendSeries }) {
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const toggle = useCallback((key: string) => {
    setHidden((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  // Shared brush state so both charts stay aligned
  const [brushRange, setBrushRange] = useState<{ startIndex: number; endIndex: number } | null>(null)
  const handleBrushChange = useCallback((range: { startIndex?: number; endIndex?: number }) => {
    if (range.startIndex !== undefined && range.endIndex !== undefined) {
      setBrushRange({ startIndex: range.startIndex, endIndex: range.endIndex })
    }
  }, [])

  const memLabel = series.isIntegrated ? 'Sys Mem %' : 'VRAM %'

  const engineLines: Array<{ key: string; name: string; color: string; dasharray?: string; yAxisId: string; strokeWidth?: number }> = [
    { key: 'utilization', name: 'GPU Util %', color: '#ff6b6b', yAxisId: 'util', strokeWidth: 2 },
    ...ENGINE_CURVE_META.map((e) => ({ key: e.key, name: e.name, color: e.color, yAxisId: 'util' })),
    { key: 'memUsage', name: memLabel, color: '#4ade80', dasharray: '5 3', yAxisId: 'util' },
    { key: 'gt0Act', name: 'GT0 Act MHz', color: '#cbd5e1', yAxisId: 'freq' },
    { key: 'gt0Req', name: 'GT0 Req MHz', color: '#cbd5e1', dasharray: '6 4', yAxisId: 'freq' },
    { key: 'gt1Act', name: 'GT1 Act MHz', color: '#34d399', yAxisId: 'freq' },
    { key: 'gt1Req', name: 'GT1 Req MHz', color: '#34d399', dasharray: '4 4', yAxisId: 'freq' },
    { key: 'gt0Rc6', name: 'GT0 RC6 %', color: '#2dd4bf', dasharray: '3 2', yAxisId: 'util' },
    { key: 'gt1Rc6', name: 'GT1 RC6 %', color: '#a3e635', dasharray: '3 2', yAxisId: 'util' },
  ]

  const pkgPowerLabel = series.isIntegrated ? 'Pkg Power W' : 'Card Power W'
  const powerLines: Array<{ key: string; name: string; color: string; dasharray?: string }> = [
    { key: 'gpuPower', name: 'GPU Power W', color: '#4cc9f0' },
    { key: 'pkgPower', name: pkgPowerLabel, color: '#ff9f1c', dasharray: '5 3' },
  ]

  return (
    <Card
      style={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, borderRadius: 6, marginBottom: 16 }}
      bodyStyle={{ padding: 16 }}
    >
      {/* Chart 1: Engine utilization + Mem% + Frequency */}
      <Text style={{ color: COLORS.textMuted, display: 'block', marginBottom: 6 }}>
        {series.label} — Engine Utilization &amp; GT Frequency
      </Text>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 0', marginBottom: 8 }}>
        {engineLines.map((l) => (
          <LegendToggleItem
            key={l.key}
            value={l.name}
            color={l.color}
            hidden={hidden.has(l.key)}
            onClick={() => toggle(l.key)}
            dasharray={l.dasharray}
          />
        ))}
      </div>
      <div style={{ width: '100%', height: 280 }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={series.points} margin={{ top: 4, right: 20, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={`${COLORS.border}99`} strokeDasharray="3 3" />
            <XAxis
              dataKey="timestamp"
              tick={{ fill: COLORS.textMuted, fontSize: 11 }}
              minTickGap={36}
              tickFormatter={formatHistoryAxisTick}
            />
            <YAxis
              yAxisId="util"
              domain={[0, 100]}
              tick={{ fill: COLORS.textMuted, fontSize: 11 }}
              tickFormatter={(val: string | number) => `${val}%`}
              width={52}
              label={{ value: '%', angle: -90, position: 'insideLeft', fill: COLORS.textMuted, fontSize: 11, offset: 14 }}
            />
            <YAxis
              yAxisId="freq"
              orientation="right"
              tick={{ fill: COLORS.textMuted, fontSize: 11 }}
              width={56}
              label={{ value: 'MHz', angle: 90, position: 'insideRight', fill: COLORS.textMuted, fontSize: 11, offset: 10 }}
            />
            <Tooltip
              contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text }}
              formatter={tooltipFmt2}
            />
            {engineLines.map((l) => (
              <Line
                key={l.key}
                type="monotone"
                yAxisId={l.yAxisId}
                dataKey={l.key}
                name={l.name}
                stroke={l.color}
                strokeDasharray={l.dasharray}
                dot={false}
                strokeWidth={2}
                hide={hidden.has(l.key)}
              />
            ))}
            <Brush dataKey="timestamp" height={24} stroke={COLORS.accent} fill={COLORS.bg} travellerWidth={8}
              {...(brushRange ? { startIndex: brushRange.startIndex, endIndex: brushRange.endIndex } : {})}
              onChange={handleBrushChange} />
          </LineChart>
        </ResponsiveContainer>
      </div>

      {/* Chart 2: Power */}
      <Text style={{ color: COLORS.textMuted, display: 'block', marginTop: 16, marginBottom: 6 }}>
        {series.label} — Power (W)
      </Text>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 0', marginBottom: 8 }}>
        {powerLines.map((l) => (
          <LegendToggleItem
            key={l.key}
            value={l.name}
            color={l.color}
            hidden={hidden.has(l.key)}
            onClick={() => toggle(l.key)}
            dasharray={l.dasharray}
          />
        ))}
      </div>
      <div style={{ width: '100%', height: 160 }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={series.points} margin={{ top: 4, right: 20, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={`${COLORS.border}99`} strokeDasharray="3 3" />
            <XAxis
              dataKey="timestamp"
              tick={{ fill: COLORS.textMuted, fontSize: 11 }}
              minTickGap={36}
              tickFormatter={formatHistoryAxisTick}
            />
            <YAxis
              tick={{ fill: COLORS.textMuted, fontSize: 11 }}
              tickFormatter={(val: string | number) => `${val}W`}
              width={56}
              label={{ value: 'W', angle: -90, position: 'insideLeft', fill: COLORS.textMuted, fontSize: 11, offset: 14 }}
            />
            <Tooltip
              contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text }}
              formatter={(val: number, name: string) => [`${typeof val === 'number' ? val.toFixed(2) : val} W`, name]}
            />
            {powerLines.map((l) => (
              <Line
                key={l.key}
                type="monotone"
                dataKey={l.key}
                name={l.name}
                stroke={l.color}
                strokeDasharray={l.dasharray}
                dot={false}
                strokeWidth={2}
                hide={hidden.has(l.key)}
              />
            ))}
            <Brush dataKey="timestamp" height={24} stroke={COLORS.accent} fill={COLORS.bg} travellerWidth={8}
              {...(brushRange ? { startIndex: brushRange.startIndex, endIndex: brushRange.endIndex } : {})}
              onChange={handleBrushChange} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </Card>
  )
}

function CpuPerCoreHistoryCard({ info }: { info: CpuPerCoreInfo }) {
  const { points, coreCount, pCoreIndices, eCoreIndices, lpeCoreIndices } = info

  // Check which cores actually have temperature data in at least one point
  const coresWithTemp = useMemo(() => {
    const has = new Set<number>()
    for (const pt of points) {
      for (let i = 0; i < coreCount; i++) {
        if (!has.has(i) && typeof pt[`t_${i}`] === 'number') has.add(i)
      }
      if (has.size >= coreCount) break
    }
    return has
  }, [points, coreCount])

  // P-Core chart: Util % solid, Freq MHz dashed; per-core use palette, Avg uses white
  const pCoreSeries: SeriesConfig[] = useMemo(() => {
    if (!pCoreIndices.length) return []
    return [
      { key: 'pAvgUtil', name: 'P Avg Util', color: '#ffffff', unit: '%' },
      ...pCoreIndices.map((logIdx, i) => ({
        key: `u_${logIdx}`,
        name: `P${i} Util`,
        color: P_CORE_COLORS[i % P_CORE_COLORS.length],
        unit: '%',
      })),
      { key: 'pAvgFreq', name: 'P Avg Freq', color: '#ffffff', unit: 'MHz', dasharray: '6 3', defaultOn: false },
      ...pCoreIndices.map((logIdx, i) => ({
        key: `f_${logIdx}`,
        name: `P${i} Freq`,
        color: P_CORE_COLORS[i % P_CORE_COLORS.length],
        unit: 'MHz',
        dasharray: '6 3',
        defaultOn: false,
      })),
    ]
  }, [pCoreIndices])

  // E-Core chart: same dual-style approach (solid util, dashed freq)
  const eCoreSeries: SeriesConfig[] = useMemo(() => {
    if (!eCoreIndices.length) return []
    return [
      { key: 'eAvgUtil', name: 'E Avg Util', color: '#ffffff', unit: '%' },
      ...eCoreIndices.map((logIdx, i) => ({
        key: `u_${logIdx}`,
        name: `E${i} Util`,
        color: E_CORE_COLORS[i % E_CORE_COLORS.length],
        unit: '%',
      })),
      { key: 'eAvgFreq', name: 'E Avg Freq', color: '#ffffff', unit: 'MHz', dasharray: '6 3', defaultOn: false },
      ...eCoreIndices.map((logIdx, i) => ({
        key: `f_${logIdx}`,
        name: `E${i} Freq`,
        color: E_CORE_COLORS[i % E_CORE_COLORS.length],
        unit: 'MHz',
        dasharray: '6 3',
        defaultOn: false,
      })),
    ]
  }, [eCoreIndices])

  // LPE-Core chart: same dual-style approach
  const lpeCoreSeries: SeriesConfig[] = useMemo(() => {
    if (!lpeCoreIndices.length) return []
    return [
      { key: 'lpeAvgUtil', name: 'LPE Avg Util', color: '#ffffff', unit: '%' },
      ...lpeCoreIndices.map((logIdx, i) => ({
        key: `u_${logIdx}`,
        name: `LPE${i} Util`,
        color: LPE_CORE_COLORS[i % LPE_CORE_COLORS.length],
        unit: '%',
      })),
      { key: 'lpeAvgFreq', name: 'LPE Avg Freq', color: '#ffffff', unit: 'MHz', dasharray: '6 3', defaultOn: false },
      ...lpeCoreIndices.map((logIdx, i) => ({
        key: `f_${logIdx}`,
        name: `LPE${i} Freq`,
        color: LPE_CORE_COLORS[i % LPE_CORE_COLORS.length],
        unit: 'MHz',
        dasharray: '6 3',
        defaultOn: false,
      })),
    ]
  }, [lpeCoreIndices])

  // Temperature chart: Package + per-core temps (P/E/LPE labeled), skip cores with no data
  const tempSeries: SeriesConfig[] = useMemo(() => {
    const series: SeriesConfig[] = [
      { key: 'pkgTemp', name: 'Package', color: '#fbbf24', unit: '°C' },
    ]
    pCoreIndices.forEach((logIdx, i) => {
      if (coresWithTemp.has(logIdx)) {
        series.push({ key: `t_${logIdx}`, name: `P${i}`, color: P_CORE_COLORS[i % P_CORE_COLORS.length], unit: '°C', defaultOn: false })
      }
    })
    eCoreIndices.forEach((logIdx, i) => {
      if (coresWithTemp.has(logIdx)) {
        series.push({ key: `t_${logIdx}`, name: `E${i}`, color: E_CORE_COLORS[i % E_CORE_COLORS.length], unit: '°C', defaultOn: false })
      }
    })
    lpeCoreIndices.forEach((logIdx, i) => {
      if (coresWithTemp.has(logIdx)) {
        series.push({ key: `t_${logIdx}`, name: `LPE${i}`, color: LPE_CORE_COLORS[i % LPE_CORE_COLORS.length], unit: '°C', defaultOn: false })
      }
    })
    return series
  }, [pCoreIndices, eCoreIndices, lpeCoreIndices, coresWithTemp])

  if (coreCount === 0) return null

  return (
    <>
      {pCoreSeries.length > 0 && (
        <ConfigurableChart
          title={`P-Core (${pCoreIndices.length}) — Utilization & Frequency`}
          storageKey="history-cpu-pcore"
          data={points}
          allSeries={pCoreSeries}
          height={240}
          showBrush
          hoverPanel
        />
      )}
      {eCoreSeries.length > 0 && (
        <ConfigurableChart
          title={`E-Core (${eCoreIndices.length}) — Utilization & Frequency`}
          storageKey="history-cpu-ecore"
          data={points}
          allSeries={eCoreSeries}
          height={240}
          showBrush
          hoverPanel
        />
      )}
      {lpeCoreSeries.length > 0 && (
        <ConfigurableChart
          title={`LPE-Core (${lpeCoreIndices.length}) — Utilization & Frequency`}
          storageKey="history-cpu-lpecore"
          data={points}
          allSeries={lpeCoreSeries}
          height={240}
          showBrush
          hoverPanel
        />
      )}
      {tempSeries.length > 1 && (
        <ConfigurableChart
          title="CPU Temperature (°C)"
          storageKey="history-cpu-temp"
          data={points}
          allSeries={tempSeries}
          height={200}
          showBrush
        />
      )}
    </>
  )
}

export default function HistoryDashboard({ active }: Props) {
  const [rangePreset, setRangePreset] = useState<RangePreset>('15m')
  const [customRange, setCustomRange] = useState<[Dayjs | null, Dayjs | null] | null>(null)
  const [history, setHistory] = useState<HistoryData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastFetchAt, setLastFetchAt] = useState<string | null>(null)
  const [visibleSections, setVisibleSections] = useState<string[]>(ALL_SECTIONS)

  // Static info for NIC link speeds & disk throughput caps
  const [staticInfo, setStaticInfo] = useState<StaticInfoData | null>(null)
  useEffect(() => {
    if (!active) return
    api.getStaticInfo().then(setStaticInfo).catch(() => {})
  }, [active])

  // Retention settings state
  const [retention, setRetention] = useState<HistoryRetentionData | null>(null)
  const [pendingRetentionDays, setPendingRetentionDays] = useState<number | null>(null)
  const [savingRetention, setSavingRetention] = useState(false)
  const [messageApi, messageContextHolder] = message.useMessage()

  // Client/server clock skew (seconds, signed: positive = server ahead of
  // client).  Updated from the server_time field every successful fetch.
  // Only surfaced when |skew| exceeds the threshold below to avoid noise on
  // healthy deployments.
  const [clockSkewSec, setClockSkewSec] = useState<number | null>(null)
  const CLOCK_SKEW_WARN_SEC = 60

  const customRangeReady = useMemo(() => {
    if (rangePreset !== 'custom') return true
    const start = customRange?.[0]
    const end = customRange?.[1]
    return Boolean(start && end && end.unix() >= start.unix())
  }, [rangePreset, customRange])

  const fetchHistory = useCallback(async () => {
    if (!active) return
    if (rangePreset === 'custom' && !customRangeReady) return
    setLoading(true)
    setHistory(null)

    let startTime: number | null = null
    let endTime: number | null = null
    let rangeSeconds: number | null = null

    if (rangePreset === 'custom') {
      // Custom range: caller picked specific timestamps in the date picker;
      // pass them through verbatim.  Skew correction is intentionally not
      // applied here — if the user typed "16:00 today" they meant their own
      // wall clock, not server time, so honoring their input is least
      // surprising.  The skew banner (below) signals when the chart's time
      // axis won't match what the user expected.
      const start = customRange?.[0]
      const end = customRange?.[1]
      if (start && end) {
        startTime = start.unix()
        endTime = end.unix()
      }
    } else {
      // Preset path: ask the server to anchor the window to its own clock.
      // This is per-request: another tab on a different preset still gets
      // its own range_seconds → its own window.
      const preset = rangePreset as Exclude<RangePreset, 'custom'>
      rangeSeconds = RANGE_SECONDS[preset]
    }

    const queryLimit = estimateRequiredLimit(rangePreset, customRange)

    try {
      const data = await api.getHistory({
        snapshotType: 'dynamic',
        limit: queryLimit,
        startTime,
        endTime,
        rangeSeconds,
      })
      setHistory(data)
      setError(null)
      setLastFetchAt(dayjs().format('YYYY-MM-DD HH:mm:ss'))
      if (typeof data.server_time === 'number' && Number.isFinite(data.server_time)) {
        const clientNow = Math.floor(Date.now() / 1000)
        setClockSkewSec(data.server_time - clientNow)
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to fetch history data')
    } finally {
      setLoading(false)
    }
  }, [active, rangePreset, customRange, customRangeReady])

  useEffect(() => {
    if (!active) return
    if (rangePreset === 'custom' && !customRangeReady) return
    fetchHistory()
  }, [active, fetchHistory, rangePreset, customRangeReady])

  const fetchRetention = useCallback(async () => {
    if (!active) return
    try {
      const data = await api.getHistoryRetention()
      setRetention(data)
      setPendingRetentionDays(data.retention_days)
    } catch {
      // Non-critical — ignore errors silently
    }
  }, [active])

  useEffect(() => {
    if (!active) return
    fetchRetention()
  }, [active, fetchRetention])

  const handleSaveRetention = useCallback(async () => {
    if (pendingRetentionDays === null) return
    setSavingRetention(true)
    try {
      const result = await api.setHistoryRetention(pendingRetentionDays, retention?.updated_at)

      if (result.status === 'conflict') {
        const current = (result.current ?? {}) as HistoryRetentionData
        const newTs = current.updated_at
        const tsLabel = newTs
          ? new Date(newTs * 1000).toLocaleString()
          : 'unknown time'
        Modal.confirm({
          title: 'Retention changed by another client',
          content: (
            <div>
              <p>
                Retention was updated to <b>{current.retention_days} day(s)</b> at <b>{tsLabel}</b> while you were editing.
                Reloading will replace the form with the latest server value.
              </p>
            </div>
          ),
          okText: 'Reload latest values',
          cancelText: 'Cancel',
          onOk: () => {
            setRetention((prev) => prev ? { ...prev, ...current } : current)
            setPendingRetentionDays(current.retention_days)
          },
        })
        return
      }

      const data = result.data
      setRetention((prev) =>
        prev
          ? { ...prev, retention_days: data.retention_days, updated_at: data.updated_at }
          : prev,
      )
      const msg = data.deleted
        ? `Retention set to ${data.retention_days} day(s). ${data.deleted} old record(s) deleted.`
        : `Retention set to ${data.retention_days} day(s).`
      messageApi.success(msg)
      // Refresh history so the UI reflects any newly-removed rows.
      fetchHistory()
    } catch (e: unknown) {
      messageApi.error(e instanceof Error ? e.message : 'Failed to save retention setting')
    } finally {
      setSavingRetention(false)
    }
  }, [pendingRetentionDays, retention?.updated_at, messageApi, fetchHistory])

  const dynamicItems = useMemo<HistorySnapshotItem[]>(
    () => (history?.items ?? []).filter((item: HistorySnapshotItem) => item.snapshot_type === 'dynamic'),
    [history],
  )

  const pressureTrendPoints = useMemo(() => {
    const raw = buildPressureTrendPoints(dynamicItems)
    return downsampleLTTB(raw, MAX_CHART_POINTS, (p) => Math.abs(p.systemPressure ?? 0) + Math.abs(p.diskPressure ?? 0) + Math.abs(p.networkPressure ?? 0))
  }, [dynamicItems])
  const cpuMemTrendPoints = useMemo(() => {
    const raw = buildCpuMemTrendPoints(dynamicItems)
    return downsampleLTTB(raw, MAX_CHART_POINTS, (p) => Math.abs(p.cpuUtilization ?? 0) + Math.abs(p.memoryUtilization ?? 0) + Math.abs(p.pCoreFreqMhz ?? 0) + Math.abs(p.cpuTemperatureC ?? 0))
  }, [dynamicItems])
  const cpuPerCoreInfo = useMemo<CpuPerCoreInfo>(() => {
    const raw = buildCpuPerCoreTrendPoints(dynamicItems)
    return {
      ...raw,
      points: downsampleLTTB(raw.points, MAX_CHART_POINTS, (p) =>
        Math.abs(p.totalUtil as number ?? 0) + Math.abs((p.pkgTemp as number) ?? 0) + Math.abs((p.pAvgFreq as number) ?? 0) / 100,
      ),
    }
  }, [dynamicItems])
  const { points: diskTrendPoints, diskNames, diskMaxThroughput } = useMemo(() => {
    const { points, diskNames: dn, diskMaxThroughput: tp } = buildDiskTrendPoints(dynamicItems)
    return { points: downsampleLTTB(points, MAX_CHART_POINTS, (p) => sumMetrics(p as unknown as Record<string, unknown>)), diskNames: dn, diskMaxThroughput: tp }
  }, [dynamicItems])
  const { points: networkTrendPoints, nicNames, nicSpeeds } = useMemo(() => {
    const { points, nicNames: nn, nicSpeeds: sp } = buildNetworkTrendPoints(dynamicItems)
    // Merge with static info NIC speeds as fallback (history data may lack speed_mbps)
    const merged = { ...sp }
    if (staticInfo?.io.network_speeds_mbps) {
      for (const [name, speed] of Object.entries(staticInfo.io.network_speeds_mbps)) {
        if (speed > 0 && !merged[name]) merged[name] = speed
      }
    }
    return { points: downsampleLTTB(points, MAX_CHART_POINTS, (p) => sumMetrics(p as unknown as Record<string, unknown>)), nicNames: nn, nicSpeeds: merged }
  }, [dynamicItems, staticInfo])
  const npuTrendPoints = useMemo(() => {
    const raw = buildNpuTrendPoints(dynamicItems)
    return downsampleLTTB(raw, MAX_CHART_POINTS, (p) => Math.abs(p.npuUtilization ?? 0) + Math.abs(p.npuFreqMhz ?? 0) + Math.abs(p.npuPowerW ?? 0) + Math.abs(p.npuTemperatureC ?? 0) + Math.abs(p.npuMemoryMb ?? 0))
  }, [dynamicItems])
  const gpuTrendSeries = useMemo(() => {
    const raw = buildGpuTrendSeries(dynamicItems)
    return raw
      .map((s) => ({
        ...s,
        points: downsampleLTTB(s.points, MAX_CHART_POINTS, (p) => Math.abs(p.utilization ?? 0) + Math.abs(p.vcs ?? 0) + Math.abs(p.vecs ?? 0) + Math.abs(p.gt0Act ?? 0) + Math.abs(p.gpuPower ?? 0)),
      }))
      .sort((a, b) => (a.isIntegrated ? 0 : 1) - (b.isIntegrated ? 0 : 1))
  }, [dynamicItems])

  const hasNpuData = useMemo(() => npuTrendPoints.some((p) => p.npuUtilization != null || p.npuFreqMhz != null || p.npuTemperatureC != null), [npuTrendPoints])
  const hasNpuPmtData = useMemo(
    () => npuTrendPoints.some((p) => p.npuPowerW != null || p.npuDdrBwMib != null || p.npuTemperatureC != null),
    [npuTrendPoints],
  )

  const [pressureHidden, setPressureHidden] = useState<Set<string>>(new Set())
  const togglePressure = useCallback((key: string) => {
    setPressureHidden((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  const [diskHidden, setDiskHidden] = useState<Set<string>>(new Set())
  const toggleDisk = useCallback((key: string) => {
    setDiskHidden((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  const [networkHidden, setNetworkHidden] = useState<Set<string>>(new Set())
  const toggleNetwork = useCallback((key: string) => {
    setNetworkHidden((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  const rangeHint = useMemo(() => {
    if (rangePreset !== 'custom') return `Range: ${rangePreset}`
    const start = customRange?.[0]
    const end = customRange?.[1]
    if (!start || !end) return 'Range: custom (select start/end)'
    return `Range: ${start.format('MM-DD HH:mm')} ~ ${end.format('MM-DD HH:mm')}`
  }, [rangePreset, customRange])

  const exportBasename = useCallback(() => {
    const stamp = dayjs().format('YYYYMMDD-HHmmss')
    const range = rangePreset === 'custom' ? 'custom' : rangePreset
    return `history-${range}-${stamp}`
  }, [rangePreset])

  const triggerDownload = useCallback((filename: string, blob: Blob) => {
    try {
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) {
      messageApi.error(e instanceof Error ? e.message : 'Failed to export')
    }
  }, [messageApi])

  const handleExportJSON = useCallback(() => {
    if (dynamicItems.length === 0) {
      messageApi.warning('No history data to export')
      return
    }
    const payload = {
      exported_at: dayjs().toISOString(),
      range: rangePreset,
      custom_range: rangePreset === 'custom' && customRange?.[0] && customRange?.[1]
        ? { start: customRange[0].toISOString(), end: customRange[1].toISOString() }
        : null,
      count: dynamicItems.length,
      items: dynamicItems,
    }
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' })
    triggerDownload(`${exportBasename()}.json`, blob)
    messageApi.success(`Exported ${dynamicItems.length} snapshot(s) as JSON`)
  }, [dynamicItems, rangePreset, customRange, exportBasename, triggerDownload, messageApi])

  const handleExportXLSX = useCallback(async () => {
    if (dynamicItems.length === 0) {
      messageApi.warning('No history data to export')
      return
    }

    const sorted = [...dynamicItems].sort((a, b) => (a.create_time || 0) - (b.create_time || 0))
    const dyn = sorted.map((it) => ({ item: it, data: toDynamicData(it) }))

    const wb = new ExcelJS.Workbook()
    wb.creator = 'Resource Balancer Dashboard'
    wb.created = new Date()

    const tsColumns = (): Array<Partial<ExcelJS.Column>> => [
      { header: 'id', key: 'id', width: 8 },
      { header: 'timestamp', key: 'timestamp', width: 22 },
      { header: 'unix_ts', key: 'unix_ts', width: 12 },
    ]

    const baseRow = (item: HistorySnapshotItem) => ({
      id: item.id,
      timestamp: item.create_time ? dayjs.unix(item.create_time).format('YYYY-MM-DD HH:mm:ss') : item.collected_at ?? '',
      unix_ts: item.create_time,
    })

    const addSheet = (
      name: string,
      extraColumns: Array<Partial<ExcelJS.Column>>,
      getExtras: (d: DynamicInfoData | null) => Record<string, unknown>,
    ) => {
      const sheet = wb.addWorksheet(name)
      sheet.columns = [...tsColumns(), ...extraColumns]
      sheet.getRow(1).font = { bold: true }
      sheet.views = [{ state: 'frozen', ySplit: 1 }]
      for (const { item, data } of dyn) {
        sheet.addRow({ ...baseRow(item), ...getExtras(data) })
      }
    }

    // ── Summary ───────────────────────────────────────────────────────────
    {
      const sum = wb.addWorksheet('Summary')
      sum.columns = [
        { header: 'Field', key: 'k', width: 22 },
        { header: 'Value', key: 'v', width: 60 },
      ]
      sum.getRow(1).font = { bold: true }
      sum.addRows([
        { k: 'Exported at', v: dayjs().format('YYYY-MM-DD HH:mm:ss') },
        { k: 'Range preset', v: rangePreset },
        { k: 'Custom range', v: rangePreset === 'custom' && customRange?.[0] && customRange?.[1]
          ? `${customRange[0].format('YYYY-MM-DD HH:mm')} ~ ${customRange[1].format('YYYY-MM-DD HH:mm')}`
          : '-' },
        { k: 'Snapshot count', v: dynamicItems.length },
        { k: 'First timestamp', v: sorted[0]?.create_time ? dayjs.unix(sorted[0].create_time).format('YYYY-MM-DD HH:mm:ss') : '-' },
        { k: 'Last timestamp', v: sorted[sorted.length - 1]?.create_time ? dayjs.unix(sorted[sorted.length - 1].create_time).format('YYYY-MM-DD HH:mm:ss') : '-' },
      ])
    }

    // ── CPU ───────────────────────────────────────────────────────────────
    addSheet(
      'CPU',
      [
        { header: 'util_pct', key: 'util', width: 10 },
        { header: 'p_core_util_pct', key: 'p_util', width: 16 },
        { header: 'e_core_util_pct', key: 'e_util', width: 16 },
        { header: 'lpe_core_util_pct', key: 'lpe_util', width: 18 },
        { header: 'p_core_freq_mhz', key: 'p_freq', width: 16 },
        { header: 'e_core_freq_mhz', key: 'e_freq', width: 16 },
        { header: 'lpe_core_freq_mhz', key: 'lpe_freq', width: 18 },
        { header: 'temperature_c', key: 'temp', width: 14 },
      ],
      (d) => ({
        util: d?.cpu?.usage_total ?? null,
        p_util: d?.cpu?.p_core_usage ?? null,
        e_util: d?.cpu?.e_core_usage ?? null,
        lpe_util: (d?.cpu as Record<string, unknown> | undefined)?.lpe_core_usage ?? null,
        p_freq: d?.cpu?.p_core_freq_mhz ?? null,
        e_freq: d?.cpu?.e_core_freq_mhz ?? null,
        lpe_freq: (d?.cpu as Record<string, unknown> | undefined)?.lpe_core_freq_mhz ?? null,
        temp: d?.cpu?.temperature_c ?? null,
      }),
    )

    // ── CPU Per-Core ──────────────────────────────────────────────────────
    {
      const coreCount = dyn.reduce((m, { data }) => {
        const n = data?.cpu?.per_core_usage?.length ?? 0
        return n > m ? n : m
      }, 0)
      if (coreCount > 0) {
        let pIdx: Set<number> = new Set()
        let eIdx: Set<number> = new Set()
        let lpeIdx: Set<number> = new Set()
        for (const { data } of dyn) {
          if (data?.cpu?.p_core_indices?.length) pIdx = new Set(data.cpu.p_core_indices)
          if (data?.cpu?.e_core_indices?.length) eIdx = new Set(data.cpu.e_core_indices)
          const lpeArr = (data?.cpu as Record<string, unknown> | undefined)?.lpe_core_indices as number[] | undefined
          if (lpeArr?.length) lpeIdx = new Set(lpeArr)
          if (pIdx.size || eIdx.size || lpeIdx.size) break
        }
        const coreType = (i: number) => pIdx.has(i) ? 'P' : eIdx.has(i) ? 'E' : lpeIdx.has(i) ? 'LPE' : 'Core'
        const cols: Array<Partial<ExcelJS.Column>> = []
        for (let i = 0; i < coreCount; i++) {
          cols.push(
            { header: `core${i}_type`, key: `c${i}_type`, width: 10 },
            { header: `core${i}_util_pct`, key: `c${i}_util`, width: 14 },
            { header: `core${i}_freq_mhz`, key: `c${i}_freq`, width: 14 },
            { header: `core${i}_temp_c`, key: `c${i}_temp`, width: 14 },
          )
        }
        addSheet('CPU Per-Core', cols, (d) => {
          const row: Record<string, unknown> = {}
          const usage = d?.cpu?.per_core_usage ?? []
          const freq = d?.cpu?.per_core_freq_mhz ?? []
          const temp = d?.cpu?.per_core_temperature_c ?? []
          for (let i = 0; i < coreCount; i++) {
            row[`c${i}_type`] = coreType(i)
            row[`c${i}_util`] = usage[i] ?? null
            row[`c${i}_freq`] = freq[i] ?? null
            row[`c${i}_temp`] = temp[i] ?? null
          }
          return row
        })
      }
    }

    // ── Memory ────────────────────────────────────────────────────────────
    addSheet(
      'Memory',
      [
        { header: 'mem_util_pct', key: 'util', width: 14 },
        { header: 'mem_total_gb', key: 'total', width: 14 },
        { header: 'mem_available_gb', key: 'avail', width: 16 },
        { header: 'swap_util_pct', key: 'swap', width: 14 },
        { header: 'swap_total_gb', key: 'swap_total', width: 14 },
      ],
      (d) => ({
        util: d?.memory?.usage_percent ?? null,
        total: d?.memory?.total_gb ?? null,
        avail: d?.memory?.available_gb ?? null,
        swap: d?.memory?.swap_usage_percent ?? null,
        swap_total: d?.memory?.swap_total_gb ?? null,
      }),
    )

    // ── Pressure ──────────────────────────────────────────────────────────
    addSheet(
      'Pressure',
      [
        { header: 'cpu_pct', key: 'cpu', width: 10 },
        { header: 'memory_pct', key: 'mem', width: 12 },
        { header: 'io_pct', key: 'io', width: 10 },
      ],
      (d) => {
        const p = d?.pressure as { cpu?: unknown; memory?: unknown; io?: unknown } | undefined
        const num = (v: unknown) => (typeof v === 'number' ? v : null)
        return { cpu: num(p?.cpu), mem: num(p?.memory), io: num(p?.io) }
      },
    )

    // ── Network ───────────────────────────────────────────────────────────
    {
      const nicNames = new Set<string>()
      for (const { data } of dyn) {
        const ifs = data?.network?.interfaces ?? {}
        Object.keys(ifs).forEach((n) => nicNames.add(n))
      }
      const nics = [...nicNames].sort()
      const netCols: Array<Partial<ExcelJS.Column>> = [
        { header: 'total_rx_bytes_per_sec', key: 'total_rx', width: 20 },
        { header: 'total_tx_bytes_per_sec', key: 'total_tx', width: 20 },
      ]
      for (const n of nics) {
        netCols.push(
          { header: `${n}_rx_bytes_per_sec`, key: `${n}_rx`, width: 22 },
          { header: `${n}_tx_bytes_per_sec`, key: `${n}_tx`, width: 22 },
        )
      }
      addSheet('Network', netCols, (d) => {
        const row: Record<string, unknown> = {
          total_rx: d?.network?.total?.rx_bytes_per_sec ?? null,
          total_tx: d?.network?.total?.tx_bytes_per_sec ?? null,
        }
        for (const n of nics) {
          const ni = d?.network?.interfaces?.[n]
          row[`${n}_rx`] = ni?.rx_bytes_per_sec ?? null
          row[`${n}_tx`] = ni?.tx_bytes_per_sec ?? null
        }
        return row
      })
    }

    // ── NPU ───────────────────────────────────────────────────────────────
    if (dyn.some(({ data }) => getNpuUsage(data) != null)) {
      addSheet(
        'NPU',
        [
          { header: 'util_pct', key: 'util', width: 10 },
        ],
        (d) => ({ util: getNpuUsage(d) }),
      )
    }

    // ── GPU: one sheet per device ────────────────────────────────────────
    const maxDevices = dyn.reduce((m, { data }) => {
      const n = data?.gpu?.gpu_usage?.parsed?.devices?.length ?? 0
      return n > m ? n : m
    }, 0)

    const sheetLabels = new Map<number, string>()
    for (let i = 0; i < maxDevices; i++) {
      let role = `GPU${i}`
      let pci = ''
      for (const { data } of dyn) {
        const dev = data?.gpu?.gpu_usage?.parsed?.devices?.[i]
        if (!dev) continue
        const t = (dev.dev_type || '').toLowerCase()
        role = t.includes('integrated') || t.includes('igpu')
          ? 'iGPU'
          : t.includes('discrete') || t.includes('dgpu') ? 'dGPU' : `GPU${i}`
        pci = dev.pci_dev || ''
        break
      }
      // Excel sheet name: max 31 chars, cannot contain : \ / ? * [ ]
      const raw = pci ? `GPU-${role}-${pci}` : `GPU-${role}-${i}`
      const safe = raw.replace(/[:\\/?*[\]]/g, '-').slice(0, 31)
      sheetLabels.set(i, safe)
    }

    // Sort: iGPU sheets first
    const gpuOrder = Array.from({ length: maxDevices }, (_, i) => i).sort((a, b) => {
      const la = sheetLabels.get(a) || ''
      const lb = sheetLabels.get(b) || ''
      const ai = la.includes('iGPU') ? 0 : 1
      const bi = lb.includes('iGPU') ? 0 : 1
      return ai - bi
    })

    for (const i of gpuOrder) {
      const name = sheetLabels.get(i) || `GPU-${i}`
      addSheet(
        name,
        [
          { header: 'label', key: 'label', width: 28 },
          { header: 'util_pct', key: 'util', width: 10 },
          { header: 'rcs_pct', key: 'rcs', width: 10 },
          { header: 'bcs_pct', key: 'bcs', width: 10 },
          { header: 'vcs_pct', key: 'vcs', width: 10 },
          { header: 'vecs_pct', key: 'vecs', width: 10 },
          { header: 'ccs_pct', key: 'ccs', width: 10 },
          { header: 'gt0_act_mhz', key: 'gt0_act', width: 14 },
          { header: 'gt1_act_mhz', key: 'gt1_act', width: 14 },
          { header: 'gt0_rc6_pct', key: 'gt0_rc6', width: 14 },
          { header: 'gt1_rc6_pct', key: 'gt1_rc6', width: 14 },
          { header: 'gpu_power_w', key: 'gpu_w', width: 14 },
          { header: 'pkg_power_w', key: 'pkg_w', width: 14 },
        ],
        (d) => {
          const dev = d?.gpu?.gpu_usage?.parsed?.devices?.[i]
          if (!dev) return {}
          const findFreq = (name: string) => (dev.freqs || []).find((f) => f.name === name)
          return {
            label: getGpuLabel(dev, i),
            util: dev.utilization ?? null,
            rcs: dev.engine_util?.rcs ?? null,
            bcs: dev.engine_util?.bcs ?? null,
            vcs: dev.engine_util?.vcs ?? null,
            vecs: dev.engine_util?.vecs ?? null,
            ccs: dev.engine_util?.ccs ?? null,
            gt0_act: findFreq('gt0')?.act_mhz ?? null,
            gt1_act: findFreq('gt1')?.act_mhz ?? null,
            gt0_rc6: findFreq('gt0')?.rc6_pct ?? null,
            gt1_rc6: findFreq('gt1')?.rc6_pct ?? null,
            gpu_w: dev.power_w?.gpu ?? null,
            pkg_w: dev.power_w?.pkg ?? dev.power_w?.card ?? null,
          }
        },
      )
    }

    const buffer = await wb.xlsx.writeBuffer()
    const blob = new Blob([buffer], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })
    triggerDownload(`${exportBasename()}.xlsx`, blob)
    messageApi.success(`Exported ${dynamicItems.length} snapshot(s) as Excel`)
  }, [dynamicItems, rangePreset, customRange, exportBasename, triggerDownload, messageApi])

  return (
    <div style={{ padding: '16px 0' }}>
      {messageContextHolder}
      {error && (
        <Alert
          message="History API Error"
          description={error}
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
        />
      )}

      {clockSkewSec !== null && Math.abs(clockSkewSec) >= CLOCK_SKEW_WARN_SEC && (
        <Alert
          message="Client/server clock skew detected"
          description={(() => {
            const ahead = clockSkewSec > 0 ? 'ahead of' : 'behind'
            const mins = Math.round(Math.abs(clockSkewSec) / 60)
            return `Server clock is ${ahead} this device by about ${mins} minute(s). Preset ranges (15 min / 1 h / ...) use server time, so the data shown is correct; only the date picker for "custom" still uses your local clock.`
          })()}
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
        />
      )}

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, gap: 12, flexWrap: 'wrap' }}>
        <Title level={5} style={{ color: COLORS.text, margin: 0 }}>
          History Trends
        </Title>

        <Space size={12} wrap>
          <Segmented
            options={RANGE_PRESET_OPTIONS}
            value={rangePreset}
            onChange={(val: string | number) => setRangePreset(val as RangePreset)}
          />

          {rangePreset === 'custom' && (
            <DatePicker.RangePicker
              showTime={{ format: 'HH:mm' }}
              allowClear
              format="MM-DD HH:mm"
              value={customRange}
              onCalendarChange={(values) => {
                // Auto-apply as soon as both dates are selected, no need to click OK
                if (values?.[0] && values?.[1]) {
                  setCustomRange([values[0], values[1]])
                }
              }}
              onChange={(values) => {
                setCustomRange(values ? [values[0], values[1]] : null)
              }}
            />
          )}

          <AntTooltip title="Manual refresh">
            <Button
              icon={<ReloadOutlined />}
              onClick={() => fetchHistory()}
              disabled={rangePreset === 'custom' && !customRangeReady}
              aria-label="Manual refresh"
            />
          </AntTooltip>

          <Popover
            trigger="click"
            title="Chart Sections"
            content={
              <div>
                <Checkbox
                  indeterminate={visibleSections.length > 0 && visibleSections.length < ALL_SECTIONS.length}
                  checked={visibleSections.length === ALL_SECTIONS.length}
                  onChange={(e) => setVisibleSections(e.target.checked ? [...ALL_SECTIONS] : [])}
                >
                  Select All
                </Checkbox>
                <Divider style={{ margin: '6px 0' }} />
                <Checkbox.Group
                  options={SECTION_OPTIONS}
                  value={visibleSections}
                  onChange={(checked) => setVisibleSections(checked as string[])}
                  style={{ display: 'flex', flexDirection: 'column', gap: 6 }}
                />
              </div>
            }
          >
            <Button
              icon={<FilterOutlined />}
              style={visibleSections.length < ALL_SECTIONS.length ? { borderColor: COLORS.accent, color: COLORS.accent } : undefined}
            >
              Filter{visibleSections.length < ALL_SECTIONS.length ? ` (${visibleSections.length}/${ALL_SECTIONS.length})` : ''} <DownOutlined style={{ fontSize: 10 }} />
            </Button>
          </Popover>

          <Popover
            trigger="click"
            // Re-fetch on open so a stale tab picks up another client's changes
            // before showing the form.  Without this, Save stays disabled when
            // the local pending value matches the server's pre-change value.
            onOpenChange={(open) => { if (open) fetchRetention() }}
            title="History Data Retention"
            content={
              <div style={{ minWidth: 260 }}>
                <Text style={{ color: COLORS.textMuted, fontSize: 12, display: 'block', marginBottom: 8 }}>
                  Snapshots older than the selected period are automatically deleted
                  (once at startup and then every hour). Changing the setting triggers
                  an immediate cleanup.
                </Text>
                <Divider style={{ margin: '8px 0' }} />
                <Space>
                  <Text style={{ color: COLORS.text }}>Retain for:</Text>
                  <Select
                    value={pendingRetentionDays ?? retention?.retention_days ?? 3}
                    onChange={setPendingRetentionDays}
                    style={{ width: 110 }}
                    options={Array.from(
                      { length: (retention?.max_days ?? 7) - (retention?.min_days ?? 1) + 1 },
                      (_, i) => {
                        const d = (retention?.min_days ?? 1) + i
                        return { value: d, label: `${d} day${d === 1 ? '' : 's'}` }
                      },
                    )}
                  />
                  <Button
                    type="primary"
                    size="small"
                    loading={savingRetention}
                    disabled={
                      pendingRetentionDays === null ||
                      pendingRetentionDays === retention?.retention_days
                    }
                    onClick={handleSaveRetention}
                  >
                    Save
                  </Button>
                </Space>
                <Text style={{ color: COLORS.textMuted, fontSize: 11, display: 'block', marginTop: 8 }}>
                  Last updated: {retention?.updated_at
                    ? new Date(retention.updated_at * 1000).toLocaleString()
                    : 'Not yet saved'}
                </Text>
              </div>
            }
          >
            <AntTooltip title="Retention settings">
              <Button icon={<SettingOutlined />} aria-label="Retention settings" />
            </AntTooltip>
          </Popover>

          <Dropdown
            trigger={['click']}
            menu={{
              items: [
                { key: 'xlsx', label: 'Export as Excel (.xlsx)' },
                { key: 'json', label: 'Export as JSON' },
              ],
              onClick: ({ key }) => {
                if (key === 'xlsx') handleExportXLSX()
                else if (key === 'json') handleExportJSON()
              },
            }}
            disabled={dynamicItems.length === 0}
          >
            <Button icon={<DownloadOutlined />} disabled={dynamicItems.length === 0}>
              Export <DownOutlined style={{ fontSize: 10 }} />
            </Button>
          </Dropdown>
        </Space>
      </div>

      <Text style={{ color: COLORS.textMuted, fontSize: 12, display: 'block', marginBottom: 12 }}>
        {rangeHint}{lastFetchAt ? ` | Last fetch: ${lastFetchAt}` : ''}{retention ? ` | Retention: ${retention.retention_days} day${retention.retention_days === 1 ? '' : 's'}` : ''}
      </Text>

      {/* System Pressure */}
      {visibleSections.includes('pressure') && <Card
        style={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, borderRadius: 6 }}
        bodyStyle={{ padding: 16 }}
      >
        <Text style={{ color: COLORS.textMuted, display: 'block', marginBottom: 6 }}>
          System Pressure
        </Text>
        {pressureTrendPoints.length > 0 && (() => {
          const lines: Array<{ key: string; name: string; color: string; dasharray?: string }> = [
            { key: 'systemPressure', name: 'System Pressure %', color: COLORS.accent },
            { key: 'diskPressure', name: 'Disk Pressure %', color: COLORS.red, dasharray: '5 3' },
            { key: 'networkPressure', name: 'Network Pressure %', color: COLORS.green, dasharray: '5 3' },
          ]
          return (
            <>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 0', marginBottom: 8 }}>
                {lines.map((l) => (
                  <LegendToggleItem
                    key={l.key}
                    value={l.name}
                    color={l.color}
                    hidden={pressureHidden.has(l.key)}
                    onClick={() => togglePressure(l.key)}
                    dasharray={l.dasharray}
                  />
                ))}
              </div>
              <div style={{ width: '100%', height: 240 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={pressureTrendPoints} margin={{ top: 4, right: 20, left: 0, bottom: 0 }}>
                    <CartesianGrid stroke={`${COLORS.border}99`} strokeDasharray="3 3" />
                    <XAxis
                      dataKey="timestamp"
                      tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                      minTickGap={36}
                      tickFormatter={formatHistoryAxisTick}
                    />
                    <YAxis
                      domain={[0, 100]}
                      tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                      tickFormatter={(val: string | number) => `${val}%`}
                      width={52}
                      label={{ value: '%', angle: -90, position: 'insideLeft', fill: COLORS.textMuted, fontSize: 11, offset: 14 }}
                    />
                    <Tooltip
                      contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text }}
                      formatter={tooltipFmt2}
                    />
                    {lines.map((l) => (
                      <Line
                        key={l.key}
                        type="monotone"
                        dataKey={l.key}
                        name={l.name}
                        stroke={l.color}
                        strokeDasharray={l.dasharray}
                        dot={false}
                        strokeWidth={2}
                        hide={pressureHidden.has(l.key)}
                      />
                    ))}
                    <Brush dataKey="timestamp" height={24} stroke={COLORS.accent} fill={COLORS.bg} travellerWidth={8} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </>
          )
        })()}
        {loading && (
          <div style={{ color: COLORS.textMuted, paddingTop: 48, textAlign: 'center' }}>Loading history...</div>
        )}
        {!loading && pressureTrendPoints.length === 0 && (
          <Empty description="No history data" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        )}
      </Card>}

      {/* CPU & Memory — all metrics in one configurable chart */}
      {visibleSections.includes('cpuMem') && !loading && cpuMemTrendPoints.length > 0 && (
        <ConfigurableChart
          title="CPU & Memory"
          storageKey="history-cpu-series"
          data={cpuMemTrendPoints}
          height={280}
          showBrush
          allSeries={[
            { key: 'cpuUtilization', name: 'CPU Total', color: COLORS.accent, unit: '%' },
            { key: 'pCoreUtilization', name: 'P-Core Util', color: '#e07b54', unit: '%' },
            ...(cpuPerCoreInfo.eCoreIndices.length
              ? [{ key: 'eCoreUtilization', name: 'E-Core Util', color: COLORS.green, unit: '%' }]
              : []),
            ...(cpuPerCoreInfo.lpeCoreIndices.length
              ? [{ key: 'lpeCoreUtilization', name: 'LPE-Core Util', color: '#b19cd9', unit: '%' }]
              : []),
            { key: 'memoryUtilization', name: 'Memory', color: '#4ade80', unit: '%' },
            { key: 'pCoreFreqMhz', name: 'P-Core Freq', color: '#ff9f1c', unit: 'MHz', defaultOn: false },
            ...(cpuPerCoreInfo.eCoreIndices.length
              ? [{ key: 'eCoreFreqMhz', name: 'E-Core Freq', color: '#7ae582', unit: 'MHz', dasharray: '6 3', defaultOn: false }]
              : []),
            ...(cpuPerCoreInfo.lpeCoreIndices.length
              ? [{ key: 'lpeCoreFreqMhz', name: 'LPE-Core Freq', color: '#c7a8ff', unit: 'MHz', dasharray: '6 3', defaultOn: false }]
              : []),
            { key: 'cpuTemperatureC', name: 'CPU Temp', color: '#fbbf24', unit: '°C', dasharray: '4 2', defaultOn: false },
          ]}
        />
      )}

      {/* CPU Per-Core — utilization, frequency, temperature */}
      {visibleSections.includes('cpuPerCore') && !loading && cpuPerCoreInfo.coreCount > 0 && (
        <CpuPerCoreHistoryCard info={cpuPerCoreInfo} />
      )}

      {/* Disk — Utilization & Bandwidth per device */}
      {visibleSections.includes('disk') && !loading && diskNames.length > 0 && diskNames.map((diskName) => {
        const diskLines: Array<{ key: string; name: string; color: string; dasharray?: string; yAxisId: string }> = [
          { key: `${diskName}:util`, name: 'Util %', color: METRIC_COLORS.util, yAxisId: 'util' },
          { key: `${diskName}:read`, name: 'Read MB/s', color: METRIC_COLORS.read, yAxisId: 'bw' },
          { key: `${diskName}:write`, name: 'Write MB/s', color: METRIC_COLORS.write, yAxisId: 'bw' },
        ]
        return (
        <Card
          key={`disk-${diskName}`}
          style={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, borderRadius: 6, marginTop: 16 }}
          bodyStyle={{ padding: 16 }}
        >
          {(() => {
            const maxTp = diskMaxThroughput[diskName]
            const bwDomain: [number, number | ((max: number) => number)] = maxTp ? [0, maxTp] : [0, (max: number) => Math.max(max, 100)]
            const diskTypeLabel = maxTp ? (diskName.startsWith('nvme') ? 'NVMe' : maxTp >= 500 ? 'SSD' : 'HDD') : null
            return (
          <>
          <Text style={{ color: COLORS.textMuted, display: 'block', marginBottom: 6 }}>
            Disk I/O ({diskName}){diskTypeLabel ? ` — ${diskTypeLabel} ~${maxTp} MB/s` : ''}
          </Text>
              <>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 0', marginBottom: 8 }}>
                  {diskLines.map((l) => (
                    <LegendToggleItem
                      key={l.key}
                      value={l.name}
                      color={l.color}
                      hidden={diskHidden.has(l.key)}
                      onClick={() => toggleDisk(l.key)}
                      dasharray={l.dasharray}
                    />
                  ))}
                </div>
                <div style={{ width: '100%', height: 260 }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={diskTrendPoints} margin={{ top: 4, right: 20, left: 0, bottom: 0 }}>
                      <CartesianGrid stroke={`${COLORS.border}99`} strokeDasharray="3 3" />
                      <XAxis
                        dataKey="timestamp"
                        tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                        minTickGap={36}
                        tickFormatter={formatHistoryAxisTick}
                      />
                      <YAxis
                        yAxisId="util"
                        domain={[0, 100]}
                        tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                        tickFormatter={(val: string | number) => `${val}%`}
                        width={52}
                        label={{ value: '%', angle: -90, position: 'insideLeft', fill: COLORS.textMuted, fontSize: 11, offset: 14 }}
                      />
                      <YAxis
                        yAxisId="bw"
                        orientation="right"
                        domain={bwDomain}
                        tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                        tickFormatter={(val: string | number) => typeof val === 'number' ? val.toFixed(1) : `${val}`}
                        width={56}
                        label={{ value: 'MB/s', angle: 90, position: 'insideRight', fill: COLORS.textMuted, fontSize: 11, offset: 10 }}
                      />
                      <Tooltip
                        contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text }}
                        formatter={tooltipFmt2}
                      />
                      {diskLines.map((l) => (
                        <Line
                          key={l.key}
                          type="monotone"
                          yAxisId={l.yAxisId}
                          dataKey={l.key}
                          name={l.name}
                          stroke={l.color}
                          strokeDasharray={l.dasharray}
                          dot={false}
                          strokeWidth={2}
                          hide={diskHidden.has(l.key)}
                        />
                      ))}
                      <Brush dataKey="timestamp" height={24} stroke={COLORS.accent} fill={COLORS.bg} travellerWidth={8} />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              </>
          </>
            )
          })()}
        </Card>
        )
      })}

      {/* Network — Utilization & Bandwidth per NIC */}
      {visibleSections.includes('network') && !loading && nicNames.length > 0 && nicNames.map((nicName) => {
        const nicLines: Array<{ key: string; name: string; color: string; dasharray?: string; yAxisId: string }> = [
          { key: `${nicName}:util`, name: 'Util %', color: METRIC_COLORS.util, yAxisId: 'util' },
          { key: `${nicName}:rx`, name: 'RX Mbps', color: METRIC_COLORS.rx, yAxisId: 'bw' },
          { key: `${nicName}:tx`, name: 'TX Mbps', color: METRIC_COLORS.tx, yAxisId: 'bw' },
        ]
        return (
        <Card
          key={`net-${nicName}`}
          style={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, borderRadius: 6, marginTop: 16 }}
          bodyStyle={{ padding: 16 }}
        >
          {(() => {
            const linkSpeed = nicSpeeds[nicName]
            const bwDomain: [number, number | ((max: number) => number)] = linkSpeed ? [0, linkSpeed] : [0, (max: number) => Math.max(max, 100)]
            return (
          <>
          <Text style={{ color: COLORS.textMuted, display: 'block', marginBottom: 6 }}>
            Network I/O ({nicName}){linkSpeed ? ` — Link ${linkSpeed} Mbps` : ''}
          </Text>
              <>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 0', marginBottom: 8 }}>
                  {nicLines.map((l) => (
                    <LegendToggleItem
                      key={l.key}
                      value={l.name}
                      color={l.color}
                      hidden={networkHidden.has(l.key)}
                      onClick={() => toggleNetwork(l.key)}
                      dasharray={l.dasharray}
                    />
                  ))}
                </div>
                <div style={{ width: '100%', height: 260 }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={networkTrendPoints} margin={{ top: 4, right: 20, left: 0, bottom: 0 }}>
                      <CartesianGrid stroke={`${COLORS.border}99`} strokeDasharray="3 3" />
                      <XAxis
                        dataKey="timestamp"
                        tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                        minTickGap={36}
                        tickFormatter={formatHistoryAxisTick}
                      />
                      <YAxis
                        yAxisId="util"
                        domain={[0, 100]}
                        tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                        tickFormatter={(val: string | number) => `${val}%`}
                        width={52}
                        label={{ value: '%', angle: -90, position: 'insideLeft', fill: COLORS.textMuted, fontSize: 11, offset: 14 }}
                      />
                      <YAxis
                        yAxisId="bw"
                        orientation="right"
                        domain={bwDomain}
                        tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                        tickFormatter={(val: string | number) => typeof val === 'number' ? val.toFixed(1) : `${val}`}
                        width={56}
                        label={{ value: 'Mbps', angle: 90, position: 'insideRight', fill: COLORS.textMuted, fontSize: 11, offset: 10 }}
                      />
                      <Tooltip
                        contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text }}
                        formatter={tooltipFmt2}
                      />
                      {nicLines.map((l) => (
                        <Line
                          key={l.key}
                          type="monotone"
                          yAxisId={l.yAxisId}
                          dataKey={l.key}
                          name={l.name}
                          stroke={l.color}
                          strokeDasharray={l.dasharray}
                          dot={false}
                          strokeWidth={2}
                          hide={networkHidden.has(l.key)}
                        />
                      ))}
                      <Brush dataKey="timestamp" height={24} stroke={COLORS.accent} fill={COLORS.bg} travellerWidth={8} />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              </>
          </>
            )
          })()}
        </Card>
        )
      })}

      {/* NPU — split into unit-grouped charts so axes aren't mixed */}
      {visibleSections.includes('npu') && !loading && hasNpuData && (
        <>
          <ConfigurableChart
            title="NPU — Utilization & Frequency"
            storageKey="history-npu-util-freq"
            data={npuTrendPoints}
            height={240}
            showBrush
            allSeries={[
              { key: 'npuUtilization', name: 'Utilization', color: COLORS.accent, unit: '%' },
              { key: 'npuFreqMhz', name: 'Frequency', color: '#7ae582', unit: 'MHz', dasharray: '6 4' },
            ]}
          />
          {hasNpuPmtData && (
            <ConfigurableChart
              title="NPU — Power, DDR Bandwidth & Temperature"
              storageKey="history-npu-power-bw-temp"
              data={npuTrendPoints}
              height={240}
              showBrush
              allSeries={[
                { key: 'npuPowerW', name: 'Power', color: '#4cc9f0', unit: 'W' },
                { key: 'npuDdrBwMib', name: 'DDR BW', color: '#a78bfa', unit: 'MiB/s', dasharray: '5 3' },
                { key: 'npuTemperatureC', name: 'Temperature', color: '#fbbf24', unit: '°C' },
              ]}
            />
          )}
          <ConfigurableChart
            title={hasNpuPmtData ? 'NPU — Memory Used & Tiles' : 'NPU — Memory Used'}
            storageKey="history-npu-mem-tiles"
            data={npuTrendPoints}
            height={240}
            showBrush
            allSeries={hasNpuPmtData
              ? [
                  { key: 'npuMemoryMb', name: 'Memory Used', color: '#56c8d8', unit: 'MB' },
                  { key: 'npuTileCount', name: 'Tiles', color: '#b877db', unit: 'tiles', dasharray: '5 3', defaultOn: false },
                ]
              : [
                  { key: 'npuMemoryMb', name: 'Memory Used', color: '#56c8d8', unit: 'MB' },
                ]}
          />
        </>
      )}

      {/* GPU History */}
      {visibleSections.includes('gpu') && <div style={{ marginTop: 16 }}>
        {loading ? (
          <Card
            style={{
              background: COLORS.panelBg,
              border: `1px solid ${COLORS.border}`,
              borderRadius: 6,
            }}
            bodyStyle={{ padding: 16 }}
          >
            <div style={{ color: COLORS.textMuted, textAlign: 'center' }}>Loading GPU history...</div>
          </Card>
        ) : gpuTrendSeries.length === 0 ? (
          <Card
            style={{
              background: COLORS.panelBg,
              border: `1px solid ${COLORS.border}`,
              borderRadius: 6,
            }}
            bodyStyle={{ padding: 16 }}
          >
            <Empty description="No GPU history data" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          </Card>
        ) : (
          gpuTrendSeries.map((series: GpuTrendSeries) => (
            <GpuHistoryCard key={series.id} series={series} />
          ))
        )}
      </div>}
    </div>
  )
}
