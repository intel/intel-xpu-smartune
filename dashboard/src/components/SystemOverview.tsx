import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import {
  Row,
  Col,
  Card,
  Typography,
  Tag,
  Spin,
  Alert,
  Segmented,
  Space,
  Badge,
  Button,
  Modal,
  Tooltip as AntTooltip,
} from 'antd'
import {
  ThunderboltOutlined,
  AlertOutlined,
  PartitionOutlined,
  QuestionCircleOutlined,
  ExportOutlined,
  InfoCircleOutlined,
  SettingOutlined,
  SlidersOutlined,
} from '@ant-design/icons'
import {
  RadialBarChart,
  RadialBar,
  ResponsiveContainer,
  PolarAngleAxis,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Customized,
} from 'recharts'
import { COLORS, getGpuChartColors, getPressureColor, getPressureLabel } from '../styles/theme'
import { api, getLatestServerTime } from '../api/client'
import type {
  StaticInfoData,
  DynamicInfoData,
  GpuUsageDevice,
  GpuUsageFreq,
  DiskDeviceData,
  NetworkInterfacePressure,
  AppResourceEntry,
  AppInfo,
  DiagAlert,
  DiagSeverity,
  DiagFinding,
} from '../api/types'
import { usePolling } from '../hooks/usePolling'
import { useBenchEvent } from '../hooks/useBenchEvents'
import { useDocumentVisible } from '../hooks/useDocumentVisible'
import { useMonitoredSections } from '../hooks/useMonitoredSections'
import { useGlobalConfigNotices } from '../hooks/useGlobalConfigNotices'
import { createLiveDiagnosticWindow, summarizeVisibleDiagnosticEvents } from '../utils/diagnosticEvents'
import '../styles/performance.css'

const { Text, Title } = Typography

const DEFAULT_REFRESH_INTERVAL_MS = 2000
const INACTIVE_REFRESH_INTERVAL_MS = 10000
const QUIET_PROBE_INTERVAL_MS = 30000
// Diagnostics and controls change slower than live resource metrics and hit heavier
// endpoints, so they refresh on their own relaxed beat.
const COCKPIT_REFRESH_INTERVAL_MS = 10000
const COCKPIT_DIAG_WINDOW_SEC = 3600
// Matches the default row count of the Applications tab this card links into.
const HOME_TOP_APP_COUNT = 3
const HOME_APP_CANDIDATE_COUNT = 10
const REFRESH_INTERVAL_OPTIONS = [
  { label: '2s', value: 2000 },
  { label: '3s', value: 3000 },
  { label: '5s', value: 5000 },
]
const TREND_STORAGE_MAX_POINTS = 300
const ENGINE_ORDER = ['vcs', 'vecs', 'ccs', 'rcs', 'bcs'] as const
const MHZ_TO_GHZ = 1000
const REFRESH_INDICATOR_STYLE: React.CSSProperties = { display: 'inline-flex', width: 18, height: 18, alignItems: 'center', justifyContent: 'center', flexShrink: 0 }
// Fallback used only until the config endpoint responds.
const DEFAULT_MONITORED_DYNAMIC_SECTIONS = ['cpu', 'memory', 'pressure', 'network', 'disk', 'gpu', 'npu'] as const

const PERF_COLORS = {
  cpu: '#4cc9f0',
  memory: '#4ade80',
  disk: '#56c8d8',
  network: '#2dd4bf',
  gpu: '#5aa9ff',
  npu: '#818cf8',
  pressure: '#ff9f1c',
}

const GPU_UTIL_COLORS = [PERF_COLORS.gpu, PERF_COLORS.memory, PERF_COLORS.cpu, PERF_COLORS.network, PERF_COLORS.npu] as const

// Distinct purple family for GPU lines in the utilization trend, so multiple GPUs
// read as one group yet stay separable from CPU/Memory/Disk/Network.
const GPU_TREND_COLORS = ['#a78bfa', '#c084fc', '#e879f9', '#8b5cf6', '#d8b4fe'] as const

const SECTION_LABELS: Record<string, string> = {
  cpu: 'CPU',
  memory: 'Memory',
  pressure: 'Pressure',
  network: 'Network',
  disk: 'Disk',
  gpu: 'GPU',
  npu: 'NPU',
}

type TrendSeries = Record<string, Array<number | null>>

// Per-type pressure level cut-offs from the backend config (0-1 scale). Each
// pressure kind (system / disk / network) is tuned independently, so the mini
// gauges colour their severity zones from their own thresholds instead of a
// shared hard-coded 50/80 split.
type ThresholdSet = { low?: number; medium?: number; high?: number; critical?: number }
interface PressureThresholds {
  system: ThresholdSet
  disk: ThresholdSet
  network: ThresholdSet
}
interface PressureZones {
  medium: number
  high: number
  critical: number
}

// Convert a 0-1 threshold set into ordered percent boundaries, falling back to
// sensible defaults for any value the config omits or hasn't loaded yet.
function toPressureZones(thresholds?: ThresholdSet | null): PressureZones {
  const toPct = (value: number | undefined, fallback: number): number =>
    isNumber(value) && value > 0 ? Math.min(100, value * 100) : fallback
  const medium = toPct(thresholds?.medium, 50)
  const high = Math.max(medium, toPct(thresholds?.high, 80))
  const critical = Math.max(high, toPct(thresholds?.critical, 90))
  return { medium, high, critical }
}

// Home cockpit summaries: compact cross-tab snapshots ("who's consuming, what's
// flagged, what's controlled") so Home can close the discover -> locate -> act loop.
interface DiagCockpitSummary {
  counts: Record<DiagSeverity, number>
  attentionCount: number
  attentionAlert: { severity: string; text: string } | null
  insight: DiagFinding | null
}
interface ControlCockpitSummary {
  auto: number
  manual: number
  featured: { name: string; limit: string | null } | null
  remaining: number
}

const SEVERITY_ORDER: DiagSeverity[] = ['critical', 'error', 'warning', 'info']

function severityRank(severity: string): number {
  const index = SEVERITY_ORDER.indexOf(severity as DiagSeverity)
  return index === -1 ? SEVERITY_ORDER.length : index
}

// Pick the most actionable finding: highest severity first, then the first listed.
function pickTopFinding(findings: DiagFinding[]): DiagFinding | null {
  if (!findings.length) return null
  return [...findings].sort((a, b) => severityRank(a.severity) - severityRank(b.severity))[0]
}

function pickTopAlert(alerts: DiagAlert[]): DiagAlert | null {
  if (!alerts.length) return null
  return [...alerts].sort((a, b) => severityRank(a.severity) - severityRank(b.severity))[0]
}

function alertNeedsAttention(alert: DiagAlert, nowMs = Date.now()): boolean {
  if (alert.acknowledged_at) return false
  if (!alert.silenced_until) return true
  const silencedUntilMs = Date.parse(alert.silenced_until)
  return !Number.isFinite(silencedUntilMs) || silencedUntilMs <= nowMs
}

function formatUptime(bootTime: number | null | undefined): string {
  if (!isNumber(bootTime)) return 'N/A'
  const totalHours = Math.max(0, Math.floor((Date.now() / 1000 - bootTime) / 3600))
  const days = Math.floor(totalHours / 24)
  const hours = totalHours % 24
  return days > 0 ? `${days}d ${hours}h` : `${hours}h`
}

function takeUniqueApps(apps: AppResourceEntry[], limit: number): AppResourceEntry[] {
  const seen = new Set<string>()
  const unique: AppResourceEntry[] = []
  for (const app of apps) {
    const identity = app.app_id || app.app_name
    if (seen.has(identity)) continue
    seen.add(identity)
    unique.push(app)
    if (unique.length === limit) break
  }
  return unique
}

function formatMemoryGb(memoryMb?: number | null): string {
  if (!isNumber(memoryMb)) return 'N/A'
  if (memoryMb >= 1024) return `${(memoryMb / 1024).toFixed(1)} GB`
  return `${Math.round(memoryMb)} MB`
}

function formatEffectiveControl(app: Pick<AppInfo, 'effective'>): string | null {
  const effective = app.effective
  const parts: string[] = []
  const toPercent = (rate: number | null | undefined): string | null =>
    rate == null ? null : `${Math.round(rate * 100)}%`
  const formatIoRate = (value: number | null | undefined, label: string, unit: string): string | null =>
    isNumber(value) ? `${label} ${Number(value.toFixed(1))} ${unit}` : null

  if (effective?.cpu_mem?.limited) {
    const cpu = toPercent(effective.cpu_mem.cpu_rate)
    const memory = toPercent(effective.cpu_mem.mem_rate)
    if (cpu) parts.push(`CPU ${cpu}`)
    if (memory) parts.push(`Memory ${memory}`)
  }
  if (effective?.disk_io?.limited) {
    const ioLimits = [
      formatIoRate(effective.disk_io.read_mb_s, 'R', 'MB/s'),
      formatIoRate(effective.disk_io.write_mb_s, 'W', 'MB/s'),
      formatIoRate(effective.disk_io.read_iops, 'R', 'IOPS'),
      formatIoRate(effective.disk_io.write_iops, 'W', 'IOPS'),
    ].filter((value): value is string => value !== null)
    if (ioLimits.length) parts.push(`Disk I/O ${ioLimits.join(' / ')}`)
  }

  return parts.length ? parts.join(' · ') : null
}

// Left-pad a trend series with nulls to a fixed window length so the newest
// sample always lands on the right edge ("now").  When the series is already at
// or beyond the window it is sliced to the most recent `length` points.
const padSeriesLeft = (values: Array<number | null>, length: number): Array<number | null> => {
  if (values.length >= length) return values.slice(-length)
  return [...Array(length - values.length).fill(null), ...values]
}

// Cut the visible window out of a stored series, anchoring its left edge to the
// last sample taken *before* the window when the window happens to open on a
// gap.
//
// Slots are a fixed `refreshIntervalMs` wide but samples do not always arrive
// that fast: while the tab is in the background the poll drops to 10s, so four
// of every five slots are null. `connectNulls` bridges those gaps invisibly in
// the middle of the chart -- but it cannot bridge a *leading* one, so whenever
// the window's first slots were null the whole curve started a chunk of the way
// in and the left of the chart read as missing data. The sample is real, just
// older than the window; carrying it in makes the leading edge behave like
// every interior gap already does.
//
// A series that simply has not filled the window yet (fresh page load) is left
// alone: there is no earlier sample to anchor to and the blank is honest.
const windowSeries = (values: Array<number | null>, length: number): Array<number | null> => {
  if (values.length <= length) return padSeriesLeft(values, length)
  const slice = values.slice(-length)
  if (isNumber(slice[0])) return slice
  for (let index = values.length - length - 1; index >= 0; index -= 1) {
    if (isNumber(values[index])) {
      slice[0] = values[index]
      break
    }
  }
  return slice
}

// Re-bucket a slot-indexed trend series from one slot width to another, keeping
// the real-time position of every sample.  Used when the refresh cadence changes:
// instead of discarding the buffered history (reset) or reinterpreting it at the
// wrong width (stretched axis), each point keeps its true age and lands in the
// matching slot of the new cadence, so the trend continues seamlessly.
const resampleSeries = (
  values: Array<number | null>,
  oldSlotMs: number,
  newSlotMs: number,
): Array<number | null> => {
  if (values.length === 0 || oldSlotMs === newSlotMs) return values
  const n = values.length
  // Newest sample sits at the right edge (age 0); index i is (n-1-i) slots older.
  const bySlotFromEnd = new Map<number, number>()
  let maxSlot = 0
  for (let i = 0; i < n; i += 1) {
    const v = values[i]
    if (!isNumber(v)) continue
    const ageMs = (n - 1 - i) * oldSlotMs
    const slot = Math.round(ageMs / newSlotMs)
    // Ascending i means a later (more recent) sample wins a shared slot.
    bySlotFromEnd.set(slot, v)
    if (slot > maxSlot) maxSlot = slot
  }
  if (bySlotFromEnd.size === 0) return []
  const length = Math.min(TREND_STORAGE_MAX_POINTS, maxSlot + 1)
  const out: Array<number | null> = Array(length).fill(null)
  bySlotFromEnd.forEach((value, slot) => {
    if (slot < length) out[length - 1 - slot] = value
  })
  return out
}
type EngineKey = (typeof ENGINE_ORDER)[number]
type SparkMode = 'axis' | 'points'
type DataSourceKind = 'static' | 'dynamic'

type GpuStatus = 'OK' | 'Busy' | 'Throttle' | 'Offline'

const ENGINE_COLORS: Record<EngineKey, string> = {
  ccs: PERF_COLORS.gpu,
  rcs: PERF_COLORS.cpu,
  bcs: '#ff6b9d',
  vcs: PERF_COLORS.memory,
  vecs: PERF_COLORS.pressure,
}

interface GpuDeviceView {
  id: string
  label: string
  displayLabel: string
  index: number
  cardKey: string
  available: boolean
  status: GpuStatus
  statusColor: string
  name: string
  devType: string
  pci: string
  driver: string
  utilization: number | null
  frequencies: {
    gt0?: GpuUsageFreq
    gt1?: GpuUsageFreq
  }
  freqBounds: {
    min_mhz: number | null
    max_mhz: number | null
  }
  gtFreqBounds: {
    gt0?: { min_mhz: number | null; max_mhz: number | null }
    gt1?: { min_mhz: number | null; max_mhz: number | null }
  }
  powerGpu: number | null
  powerPkg: number | null
  vramUsage: number | null
  euCount: number | null
  pciId: string | null
  pcieLink: {
    current_speed: string | null
    current_width: string | null
    max_speed: string | null
    max_width: string | null
  }
  engines: EngineKey[]
  engineInstances: string[]
  engineUtil: Record<EngineKey, number | null>
}

type DeviceDetailSelection =
  | { kind: 'cpu' }
  | { kind: 'memory' }
  | { kind: 'disk'; name: string }
  | { kind: 'network'; name: string | null }
  | { kind: 'npu' }
  | { kind: 'gpu'; deviceId: string }

interface Props {
  active: boolean
  // Passed through so React.memo re-renders on theme toggle: inline COLORS-based
  // styles are captured at render time and would otherwise stay stale until the
  // next poll. The value itself is not read; the prop change drives the re-render.
  colorMode?: string
  // Drill-down from the Home cockpit into other tabs (same setActiveTab pattern
  // as onRegister/onOpenHistory). No-op when omitted.
  onNavigate?: (tab: string) => void
  onOpenSettings?: (section: string) => void
  onOpenDiagnostics?: (window: '1h') => void
  // Capability gates: the diagnostics / control cockpit cards only render when
  // the server actually exposes those features.
  diagnosticsEnabled?: boolean
  balancerEnabled?: boolean
}

function isNumber(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function normalizePercent(value?: number | null): number | null {
  if (!isNumber(value)) return null
  return value
}

function formatNumber(value: number | null, decimals = 1): string {
  if (!isNumber(value)) return 'N/A'
  return value.toFixed(decimals)
}

function formatPercent(value?: number | null, decimals = 1): string {
  if (!isNumber(value)) return 'N/A'
  return `${value.toFixed(decimals)}%`
}

function formatMetric(value?: number | null, unit?: string, decimals = 1): string {
  if (!isNumber(value)) return 'N/A'
  return `${value.toFixed(decimals)}${unit ? ` ${unit}` : ''}`
}

function formatBytesRate(bytesPerSec?: number | null): string {
  if (!isNumber(bytesPerSec)) return 'N/A'
  const bitsPerSec = bytesPerSec * 8
  if (bitsPerSec >= 1_000_000_000) return `${(bitsPerSec / 1_000_000_000).toFixed(2)} Gb/s`
  if (bitsPerSec >= 1_000_000) return `${(bitsPerSec / 1_000_000).toFixed(2)} Mb/s`
  if (bitsPerSec >= 1_000) return `${(bitsPerSec / 1_000).toFixed(1)} Kb/s`
  return `${bitsPerSec.toFixed(0)} b/s`
}

function toMbps(bytesPerSec?: number | null): number | null {
  if (!isNumber(bytesPerSec)) return null
  return (bytesPerSec * 8) / 1_000_000
}

const STATIC_INFO_CACHE_KEY = 'smartune-static-info'

// Static hardware facts (NIC link speeds, core counts, ...) barely change between
// reloads, so seed them from the previous session.  Network utilization in
// particular needs the NIC link speed to turn bytes/sec into a percentage; without
// a seed its trend stays blank until the separate static request returns, leaving
// the line starting a few polls to the right of CPU/Memory after every refresh.
// A fresh fetch always overwrites this cache, so a stale seed self-corrects.
function readCachedStaticInfo(): StaticInfoData | null {
  try {
    const raw = localStorage.getItem(STATIC_INFO_CACHE_KEY)
    return raw ? (JSON.parse(raw) as StaticInfoData) : null
  } catch {
    return null
  }
}

function writeCachedStaticInfo(data: StaticInfoData): void {
  try {
    localStorage.setItem(STATIC_INFO_CACHE_KEY, JSON.stringify(data))
  } catch {
    // Best-effort speedup; ignore quota or serialization failures.
  }
}

function parseNpuRaw(raw: string | null | undefined): Record<string, unknown> | null {
  if (!raw) return null
  try { return JSON.parse(raw) as Record<string, unknown> } catch { return null }
}

function formatPlain(value: unknown): string {
  if (value === null || value === undefined || value === '') return 'N/A'
  if (Array.isArray(value)) return value.length ? value.join(', ') : 'N/A'
  return `${value}`
}

// High utilization is noteworthy but not inherently unhealthy. Critical styling is
// reserved for explicit pressure or throttling signals from the backend/driver.
const BUSY_UTIL_THRESHOLD = 80

function getGpuStatus(available: boolean, throttled: boolean, utilization: number | null): { status: GpuStatus; color: string } {
  if (!available) return { status: 'Offline', color: COLORS.textMuted }
  if (throttled) return { status: 'Throttle', color: COLORS.red }
  if (isNumber(utilization) && utilization >= BUSY_UTIL_THRESHOLD) return { status: 'Busy', color: COLORS.orange }
  return { status: 'OK', color: COLORS.green }
}

function formatFreqRange(min?: number | null, max?: number | null): string {
  if (isNumber(min) && isNumber(max)) return `${Math.round(min)}-${Math.round(max)} MHz`
  if (isNumber(min)) return `min ${Math.round(min)} MHz`
  if (isNumber(max)) return `max ${Math.round(max)} MHz`
  return 'N/A'
}

function formatPcieLink(
  speed?: string | null,
  width?: string | null,
  maxSpeed?: string | null,
  maxWidth?: string | null,
): string {
  const cur = speed && width ? `${speed} x${width}` : (speed || width || null)
  const max = maxSpeed && maxWidth ? `max ${maxSpeed} x${maxWidth}` : null
  if (cur && max) return `${cur} (${max})`
  return cur || max || 'N/A'
}

function summarizeFreqBounds(bounds?: Record<string, { min_mhz?: number | null; max_mhz: number | null }>): string {
  const entries = Object.entries(bounds || {})
  if (!entries.length) return 'N/A'
  return entries
    .map(([name, range]) => `${name}: ${formatFreqRange(range.min_mhz, range.max_mhz)}`)
    .join(' | ')
}

function formatNetworkSpeed(value?: number | null): string {
  if (!isNumber(value) || value <= 0) return 'N/A'
  return `${Math.round(value)} Mbps`
}

function summarizeNetworkSpeeds(speeds?: Record<string, number>): string {
  const entries = Object.entries(speeds || {})
  if (!entries.length) return 'N/A'
  return entries
    .map(([name, speed]) => `${name}: ${formatNetworkSpeed(speed)}`)
    .join(' | ')
}

function normalizeEngineName(name?: string | null): EngineKey | null {
  const lowered = `${name || ''}`.toLowerCase()
  if (!lowered) return null
  if (lowered.includes('vecs') || lowered.includes('video-enhance')) return 'vecs'
  if (lowered.includes('vcs') || lowered.includes('video')) return 'vcs'
  if (lowered.includes('ccs') || lowered.includes('compute')) return 'ccs'
  if (lowered.includes('rcs') || lowered.includes('render')) return 'rcs'
  if (lowered.includes('bcs') || lowered.includes('copy') || lowered.includes('blt')) return 'bcs'
  return null
}

function bytesPerSecToMbps(value?: number | null): number | null {
  if (!isNumber(value)) return null
  return (value * 8) / 1_000_000
}

function summarizeDiskSizes(devices?: Array<{ name: string; size_gb: number | null }>): string {
  if (!devices?.length) return 'N/A'
  return devices
    .map((disk) => `${disk.name}: ${isNumber(disk.size_gb) ? `${disk.size_gb.toFixed(2)} GB` : 'N/A'}`)
    .join(' | ')
}

function getAdaptiveAxis(
  values: Array<number | null>,
  current: number | null,
  options: { lower: number; upper: number; minRange: number; padding: number; step: number }
): { min: number; max: number } {
  const { lower, upper, minRange, padding, step } = options
  const numeric = values.filter(isNumber).concat(isNumber(current) ? [current] : [])
  if (!numeric.length) {
    return { min: lower, max: upper }
  }

  const rawMin = Math.max(lower, Math.min(...numeric) - padding)
  const rawMax = Math.min(upper, Math.max(...numeric) + padding)
  let min = rawMin
  let max = rawMax

  if (max - min < minRange) {
    const center = (max + min) / 2
    min = Math.max(lower, center - minRange / 2)
    max = Math.min(upper, center + minRange / 2)
    if (max - min < minRange) {
      if (min <= lower) {
        max = Math.min(upper, min + minRange)
      } else {
        min = Math.max(lower, max - minRange)
      }
    }
  }

  const snappedMin = Math.max(lower, Math.floor(min / step) * step)
  const snappedMax = Math.min(upper, Math.ceil(max / step) * step)
  if (snappedMax <= snappedMin) {
    return { min: snappedMin, max: Math.min(upper, snappedMin + Math.max(step, minRange)) }
  }
  return { min: snappedMin, max: snappedMax }
}

function buildGpuDevices(staticInfo: StaticInfoData | null, dynamicInfo: DynamicInfoData | null): GpuDeviceView[] {
  const dynamicGpu = dynamicInfo?.gpu
  const staticGpu = staticInfo?.gpu
  const gpuUsage = dynamicGpu?.gpu_usage
  const gpuUsageAvailable = Boolean(gpuUsage?.available)
  const gpuUsageDevices: GpuUsageDevice[] = gpuUsage?.parsed?.devices || []
  const dynamicVramEntries = Object.entries(dynamicGpu?.vram || {})
  const staticVramEntries = Object.entries(staticGpu?.vram || {})
  // Build BDF (short, without domain) -> lspci name lookup
  // e.g. "00:02.0" -> "00:02.0 VGA ... Intel Arc Graphics [8086:7d55]"
  const nameByBdf: Record<string, string> = {}
  ;(staticGpu?.names || []).forEach((line) => {
    const m = line.match(/^([0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])/i)
    if (m) nameByBdf[m[1].toLowerCase()] = line
  })

  // Build reverse map: pci_address -> cardKey (e.g. "0000:00:02.0" -> "card0")
  const pciToCardKey: Record<string, string> = {}
  Object.entries(staticGpu?.pci_addresses || {}).forEach(([cardKey, pciAddr]) => {
    pciToCardKey[pciAddr] = cardKey
  })

  // Build gpu_usage device map: cardKey -> GpuUsageDevice (matched by PCI address)
  // Falls back to position-based if no PCI address match
  const gpuUsageByCardKey: Record<string, GpuUsageDevice> = {}
  gpuUsageDevices.forEach((qdev, idx) => {
    const matched = qdev.pci_dev ? pciToCardKey[qdev.pci_dev] : null
    if (matched) {
      gpuUsageByCardKey[matched] = qdev
    } else {
      // fallback: use sorted card keys by position
      const sortedKeys = Object.keys(staticGpu?.pci_addresses || {}).sort()
      const fallbackKey = sortedKeys[idx]
      if (fallbackKey) gpuUsageByCardKey[fallbackKey] = qdev
    }
  })

  const cardKeySet = new Set<string>()
  Object.keys(staticGpu?.vram || {}).forEach((k) => cardKeySet.add(k))
  Object.keys(staticGpu?.freq_bounds_mhz || {}).forEach((k) => cardKeySet.add(k))
  Object.keys(staticGpu?.pcie || {}).forEach((k) => cardKeySet.add(k))
  Object.keys(staticGpu?.engines || {}).forEach((k) => cardKeySet.add(k))
  Object.keys(staticGpu?.pci_addresses || {}).forEach((k) => cardKeySet.add(k))
  gpuUsageDevices.forEach((_, idx) => { if (!staticInfo) cardKeySet.add(`card${idx}`) })

  const staticCardKeys = Array.from(cardKeySet).sort()
  const total = Math.max(
    gpuUsageDevices.length,
    dynamicVramEntries.length,
    staticVramEntries.length,
    staticCardKeys.length,
    staticGpu?.count || 0,
  )

  const devices: GpuDeviceView[] = []
  let dgpuCounter = 0

  for (let index = 0; index < total; index += 1) {
    const cardKey = staticCardKeys[index] || dynamicVramEntries[index]?.[0] || staticVramEntries[index]?.[0] || `card${index}`
    const qdev = gpuUsageByCardKey[cardKey] || gpuUsageDevices[index]

    const hasStaticCard = Boolean(staticCardKeys[index])
    const hasDynamicCard = Boolean(dynamicVramEntries[index]?.[0] || staticVramEntries[index]?.[0])
    const withinStaticCount = Boolean((staticGpu?.count || 0) > index)
    const hasEvidence = Boolean(qdev || hasStaticCard || hasDynamicCard || withinStaticCount)
    if (!hasEvidence) continue

    const vramDyn = dynamicGpu?.vram?.[cardKey] || dynamicVramEntries[index]?.[1]
    const vramStatic = staticGpu?.vram?.[cardKey] || staticVramEntries[index]?.[1]
    const vramUsage = normalizePercent(vramDyn?.usage_percent ?? vramStatic?.usage_percent ?? null)

    // Determine iGPU vs dGPU. Authoritative source: Intel iGPU is always at
    // bus 00, device 02 (e.g. 0000:00:02.0). Fall back to qdev.dev_type when
    // the PCI address is unavailable.
    const pciAddr = (staticGpu?.pci_addresses?.[cardKey] || qdev?.pci_dev || '').toLowerCase()
    const isIntegratedByPci = /(^|:)00:02\./.test(pciAddr)
    const typeRaw = (qdev?.dev_type || '').toLowerCase()
    const isIntegratedByType = typeRaw.includes('integrated') || typeRaw.includes('igpu')
    const isDiscreteByType = typeRaw.includes('discrete') || typeRaw.includes('dgpu')

    let label: string
    if (pciAddr) {
      label = isIntegratedByPci ? 'iGPU' : `dGPU${dgpuCounter++}`
    } else if (isIntegratedByType) {
      label = 'iGPU'
    } else if (isDiscreteByType) {
      label = `dGPU${dgpuCounter++}`
    } else {
      // No PCI and no dev_type info: assume first card is iGPU (legacy).
      label = index === 0 ? 'iGPU' : `dGPU${dgpuCounter++}`
    }
    // displayLabel computed after loop via reassignment

    const freqs = qdev?.freqs || []
    const gt0 = freqs.find((f) => f.name === 'gt0') || freqs[0]
    const gt1 = freqs.find((f) => f.name === 'gt1') || freqs[1]

    const engineSet = new Set<EngineKey>()
    ;[...(qdev?.engines || []), ...((staticGpu?.engines?.[cardKey] || []) as string[])].forEach((name) => {
      const normalized = normalizeEngineName(name)
      if (normalized) engineSet.add(normalized)
    })
    ENGINE_ORDER.forEach((engine) => {
      if (Object.prototype.hasOwnProperty.call(qdev?.engine_util || {}, engine)) {
        engineSet.add(engine)
      }
    })
    const engines = ENGINE_ORDER.filter((engine) => engineSet.has(engine))

    const engineUtil = ENGINE_ORDER.reduce<Record<EngineKey, number | null>>((acc, key) => {
      acc[key] = normalizePercent(qdev?.engine_util?.[key] as number | null | undefined)
      return acc
    }, {} as Record<EngineKey, number | null>)

    const engineValues = Object.values(engineUtil).filter(isNumber)
    const utilization = engineValues.length ? Math.max(...engineValues) : vramUsage

    const throttle = Boolean(gt0?.throttled || gt1?.throttled || freqs.some((f) => f.throttled))
    const available = Boolean(qdev) && gpuUsageAvailable
    const { status, color } = getGpuStatus(available, throttle, utilization)

    const id = qdev?.pci_dev || `${cardKey}-${index}`
    // Match lspci name by this card's BDF (strip domain prefix from full PCI address)
    const cardPciAddr = staticGpu?.pci_addresses?.[cardKey]  // e.g. "0000:03:00.0"
    const shortBdf = cardPciAddr ? cardPciAddr.replace(/^[0-9a-f]{4}:/i, '').toLowerCase() : null
    const staticName = (shortBdf && nameByBdf[shortBdf]) || staticGpu?.names?.[index]
    // Extract PCIe vendor:device ID e.g. "8086:7d55" from lspci name
    const pciIdMatch = staticName?.match(/\[([0-9a-f]{4}:[0-9a-f]{4})\]\s*(?:\(rev|$)/i)
    const pciId = pciIdMatch ? pciIdMatch[1] : null

    devices.push({
      id,
      label,
      displayLabel: label,  // placeholder, reassigned below
      index,
      cardKey,
      available,
      status,
      statusColor: color,
      name: staticName || qdev?.drv_name || cardKey,
      devType: qdev?.dev_type || 'unknown',
      pci: qdev?.pci_dev || staticGpu?.pcie?.[cardKey]?.current_speed || 'N/A',
      driver: qdev?.drv_name || 'N/A',
      utilization,
      frequencies: { gt0, gt1 },
      freqBounds: {
        min_mhz: staticGpu?.freq_bounds_mhz?.[cardKey]?.min_mhz ?? null,
        max_mhz: staticGpu?.freq_bounds_mhz?.[cardKey]?.max_mhz ?? null,
      },
      gtFreqBounds: {
        gt0: staticGpu?.gt_freq_bounds_mhz?.[cardKey]?.gt0,
        gt1: staticGpu?.gt_freq_bounds_mhz?.[cardKey]?.gt1,
      },
      powerGpu: qdev?.power_w?.gpu ?? null,
      powerPkg: qdev?.power_w?.pkg ?? qdev?.power_w?.card ?? null,
      vramUsage,
      euCount: staticGpu?.eu_count?.[cardKey] ?? null,
      pciId,
      pcieLink: {
        current_speed: staticGpu?.pcie?.[cardKey]?.current_speed ?? null,
        current_width: staticGpu?.pcie?.[cardKey]?.current_width ?? null,
        max_speed: staticGpu?.pcie?.[cardKey]?.max_speed ?? null,
        max_width: staticGpu?.pcie?.[cardKey]?.max_width ?? null,
      },
      engines,
      engineInstances: ((staticGpu?.engines?.[cardKey] || []) as string[]).slice(),
      engineUtil,
    })
  }

  // Deduplicate: if multiple entries share the same PCI address, keep the one
  // with richer data (has qdev / engine data).  This guards against transient
  // state during startup where static and dynamic sources momentarily disagree.
  const seenPci = new Map<string, number>()
  const deduped: GpuDeviceView[] = []
  for (const d of devices) {
    const pciKey = (d.pci && d.pci !== 'N/A') ? d.pci : d.cardKey
    const existing = seenPci.get(pciKey)
    if (existing != null) {
      const prev = deduped[existing]
      const prevScore = (prev.available ? 2 : 0) + (prev.engines.length ? 1 : 0)
      const curScore = (d.available ? 2 : 0) + (d.engines.length ? 1 : 0)
      if (curScore > prevScore) {
        deduped[existing] = d
      }
    } else {
      seenPci.set(pciKey, deduped.length)
      deduped.push(d)
    }
  }

  // Use the actual kernel DRM card identifier (card0, card1, …) in the display
  // label so it matches the sysfs naming shown in tooltips and logs.
  deduped.forEach((d) => {
    const role = d.label === 'iGPU' ? 'iGPU' : 'dGPU'
    d.displayLabel = `${role} (${d.cardKey})`
  })

  return deduped
}

function Sparkline({
  data,
  width = 160,
  height = 40,
  stroke,
  responsive = false,
  mode = 'axis',
  xStartLabel,
  xEndLabel,
  yMin,
  yMax,
  yTickCount = 3,
  showAxisLabels = true,
  areaOpacity = 0.4,
}: {
  data: Array<number | null>
  width?: number
  height?: number
  stroke: string
  responsive?: boolean
  mode?: SparkMode
  xStartLabel?: string
  xEndLabel?: string
  yMin?: number
  yMax?: number
  yTickCount?: number
  showAxisLabels?: boolean
  areaOpacity?: number
}) {
  const id = useId()
  const cleaned = data.map((value) => (isNumber(value) ? value : null))
  const numeric = cleaned.filter(isNumber)
  const hasAxis = mode === 'axis' || mode === 'points'
  const padding = hasAxis && showAxisLabels
    ? { top: 8, right: 8, bottom: 14, left: 30 }
    : { top: 4, right: 4, bottom: 4, left: 4 }
  const chartWidth = Math.max(1, width - padding.left - padding.right)
  const chartHeight = Math.max(1, height - padding.top - padding.bottom)
  const chartLeft = padding.left
  const chartTop = padding.top
  const chartBottom = chartTop + chartHeight
  const chartRight = chartLeft + chartWidth
  const axisMin = isNumber(yMin) ? yMin : 0
  const axisMax = isNumber(yMax) ? yMax : 100
  const tickCount = Math.max(2, Math.round(yTickCount))

  const buildTicks = (minVal: number, maxVal: number) => {
    const safeMax = maxVal <= minVal ? minVal + 1 : maxVal
    return Array.from({ length: tickCount }, (_, i) => {
      const ratio = i / (tickCount - 1)
      const y = chartTop + ratio * chartHeight
      const value = safeMax - ratio * (safeMax - minVal)
      return { y, value, index: i }
    })
  }

  if (numeric.length === 0) {
    const ticks = buildTicks(axisMin, axisMax)
    return (
      <svg
        width={responsive ? '100%' : width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        className="perf-sparkline"
      >
        {hasAxis && (
          <>
            {showAxisLabels && (
              <line x1={chartLeft} y1={chartTop} x2={chartLeft} y2={chartBottom} stroke={COLORS.border} strokeWidth="1" />
            )}
            {ticks.map((tick) => (
              <g key={`tick-empty-${tick.index}`}>
                <line
                  x1={chartLeft}
                  y1={tick.y}
                  x2={chartRight}
                  y2={tick.y}
                  stroke={tick.index === 0 || tick.index === tickCount - 1 ? COLORS.border : `${COLORS.border}88`}
                  strokeWidth="1"
                />
                {showAxisLabels && (
                  <text
                    x={chartLeft - 2}
                    y={tick.y}
                    textAnchor="end"
                    dominantBaseline="middle"
                    className="perf-spark-axis"
                  >
                    {tick.value.toFixed(0)}
                  </text>
                )}
              </g>
            ))}
            {showAxisLabels && (
              <>
                <text x={chartLeft} y={height - 2} textAnchor="start" className="perf-spark-axis">{xStartLabel || ''}</text>
                <text x={chartRight} y={height - 2} textAnchor="end" className="perf-spark-axis">{xEndLabel || ''}</text>
              </>
            )}
          </>
        )}
      </svg>
    )
  }

  // Leading nulls (before the first real sample) stay blank — the line/area
  // only spans from the first numeric slot onward so a freshly-refreshed window
  // fills in from the right instead of drawing a flat carry-forward line.
  const firstIdx = cleaned.findIndex(isNumber)
  let lastValue = numeric[0]
  const normalized = cleaned.map((value) => {
    if (!isNumber(value)) return lastValue
    lastValue = value
    return value
  })

  const computedMin = Math.min(...normalized)
  const computedMax = Math.max(...normalized)
  const min = isNumber(yMin) ? yMin : computedMin
  const max = isNumber(yMax) ? yMax : computedMax
  const clampedMax = max <= min ? min + 1 : max
  const ticks = buildTicks(min, clampedMax)
  const range = clampedMax - min
  const denominator = Math.max(1, normalized.length - 1)

  const pointCoords = normalized
    .map((value, index) => {
      const plottedValue = Math.max(min, Math.min(value, clampedMax))
      const x = chartLeft + (index / denominator) * chartWidth
      const y = chartTop + chartHeight - ((plottedValue - min) / range) * chartHeight
      return { x, y, value, index }
    })
    .filter((p) => p.index >= firstIdx)

  const points = pointCoords.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`)

  const linePath = `M ${points.join(' L ')}`
  const areaLeft = pointCoords.length ? pointCoords[0].x : chartLeft
  const areaPath = `${linePath} L ${chartRight} ${chartBottom} L ${areaLeft} ${chartBottom} Z`
  const labelStep = pointCoords.length <= 20 ? 1 : pointCoords.length <= 40 ? 2 : 4

  return (
    <svg
      width={responsive ? '100%' : width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio={responsive && !hasAxis ? 'none' : 'xMidYMid meet'}
      className="perf-sparkline"
    >
      <defs>
        <linearGradient id={`spark-${id}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={stroke} stopOpacity={areaOpacity} />
          <stop offset="100%" stopColor={stroke} stopOpacity="0" />
        </linearGradient>
      </defs>
      {hasAxis && (
        <>
          {showAxisLabels && (
            <line x1={chartLeft} y1={chartTop} x2={chartLeft} y2={chartBottom} stroke={COLORS.border} strokeWidth="1" />
          )}
          {ticks.map((tick) => (
            <g key={`tick-${tick.index}`}>
              <line
                x1={chartLeft}
                y1={tick.y}
                x2={chartRight}
                y2={tick.y}
                stroke={tick.index === 0 || tick.index === tickCount - 1 ? COLORS.border : `${COLORS.border}88`}
                strokeWidth="1"
              />
              {showAxisLabels && (
                <text
                  x={chartLeft - 2}
                  y={tick.y}
                  textAnchor="end"
                  dominantBaseline="middle"
                  className="perf-spark-axis"
                >
                  {tick.value.toFixed(0)}
                </text>
              )}
            </g>
          ))}
          {showAxisLabels && (
            <>
              <text x={chartLeft} y={height - 2} textAnchor="start" className="perf-spark-axis">{xStartLabel || ''}</text>
              <text x={chartRight} y={height - 2} textAnchor="end" className="perf-spark-axis">{xEndLabel || ''}</text>
            </>
          )}
        </>
      )}
      <path d={areaPath} fill={`url(#spark-${id})`} />
      <path d={linePath} fill="none" stroke={stroke} strokeWidth="2" />
      {(mode === 'points' || pointCoords.length <= 12) &&
        pointCoords.map((point) => (
          <g key={`point-${point.index}`}>
            <circle cx={point.x} cy={point.y} r="2" fill={stroke} opacity="0.9">
              <title>{point.value.toFixed(2)}</title>
            </circle>
            {mode === 'points' && point.index % labelStep === 0 && (
              <text x={point.x} y={Math.max(chartTop + 9, point.y - 6)} textAnchor="middle" className="perf-spark-point-label">
                {point.value.toFixed(0)}
              </text>
            )}
          </g>
        ))}
      {/* Hover crosshair overlay for axis mode with many points */}
      {hasAxis && pointCoords.length > 12 && (
        <SparklineHoverOverlay
          chartLeft={chartLeft}
          chartTop={chartTop}
          chartWidth={chartWidth}
          chartHeight={chartHeight}
          pointCoords={pointCoords}
          stroke={stroke}
          width={width}
          height={height}
          xStartLabel={xStartLabel}
          pointCount={cleaned.length}
        />
      )}
    </svg>
  )
}

/** Hover overlay for sparkline — renders crosshair + value tooltip on mouse move */
function SparklineHoverOverlay({
  chartLeft, chartTop, chartWidth, chartHeight, pointCoords, stroke, width, height,
  xStartLabel, pointCount,
}: {
  chartLeft: number; chartTop: number; chartWidth: number; chartHeight: number
  pointCoords: Array<{ x: number; y: number; value: number; index: number }>
  stroke: string; width: number; height: number
  xStartLabel?: string; pointCount: number
}) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null)

  const onMouseMove = useCallback((e: React.MouseEvent<SVGRectElement>) => {
    const svg = e.currentTarget.ownerSVGElement
    if (!svg) return
    // Use getScreenCTM for accurate mapping regardless of preserveAspectRatio
    const ctm = svg.getScreenCTM()
    let svgX: number
    if (ctm) {
      const inv = ctm.inverse()
      svgX = inv.a * e.clientX + inv.c * e.clientY + inv.e
    } else {
      const rect = svg.getBoundingClientRect()
      svgX = ((e.clientX - rect.left) / rect.width) * width
    }
    // Find nearest point by x
    let bestIdx = 0
    let bestDist = Infinity
    for (let i = 0; i < pointCoords.length; i++) {
      const d = Math.abs(pointCoords[i].x - svgX)
      if (d < bestDist) { bestDist = d; bestIdx = i }
    }
    setHoverIdx(bestIdx)
  }, [pointCoords, width])

  const onMouseLeave = useCallback(() => setHoverIdx(null), [])

  const hp = hoverIdx !== null ? pointCoords[hoverIdx] : null
  const windowMatch = xStartLabel?.match(/^-(\d+)([ms])$/)
  const windowSeconds = windowMatch
    ? Number(windowMatch[1]) * (windowMatch[2] === 'm' ? 60 : 1)
    : null
  const secondsAgo = hp && windowSeconds !== null
    ? Math.max(0, Math.round((1 - hp.index / Math.max(1, pointCount - 1)) * windowSeconds))
    : null
  const relativeTime = secondsAgo === null
    ? null
    : secondsAgo === 0
      ? 'now'
      : secondsAgo >= 60
        ? `-${Math.floor(secondsAgo / 60)}m ${secondsAgo % 60}s`
        : `-${secondsAgo}s`
  const tooltipText = hp ? `${relativeTime ? `${relativeTime} · ` : ''}${hp.value.toFixed(1)}` : ''
  const tooltipWidth = relativeTime ? 76 : 40
  const tooltipOnLeft = hp ? hp.x > chartLeft + chartWidth * 0.72 : false

  return (
    <>
      {hp && (
        <>
          <line x1={hp.x} y1={chartTop} x2={hp.x} y2={chartTop + chartHeight}
            stroke={stroke} strokeWidth="1" strokeDasharray="3 2" opacity="0.6" />
          <circle cx={hp.x} cy={hp.y} r="3" fill={stroke} stroke="#fff" strokeWidth="1" />
          <rect
            x={hp.x + (tooltipOnLeft ? -tooltipWidth - 6 : 6)}
            y={Math.max(chartTop, hp.y - 10)}
            width={tooltipWidth} height="16" rx="3"
            fill="rgba(15,17,23,0.88)" stroke={stroke} strokeWidth="0.5"
          />
          <text
            x={hp.x + (tooltipOnLeft ? -tooltipWidth / 2 - 6 : tooltipWidth / 2 + 6)}
            y={Math.max(chartTop + 5, hp.y - 2)}
            textAnchor="middle"
            dominantBaseline="middle"
            fill="#e4ecff" fontSize="8" fontFamily="monospace"
          >
            {tooltipText}
          </text>
        </>
      )}
      <rect
        x={chartLeft} y={chartTop}
        width={chartWidth} height={chartHeight}
        fill="transparent"
        onMouseMove={onMouseMove}
        onMouseLeave={onMouseLeave}
        style={{ cursor: 'crosshair' }}
      />
    </>
  )
}

/** Hover overlay for multi-line sparklines — shows crosshair + per-series values */
function MultiLineHoverOverlay({
  chartLeft, chartTop, chartWidth, chartHeight,
  series, axisMin, axisMax,
  width, height, maxLen,
}: {
  chartLeft: number; chartTop: number; chartWidth: number; chartHeight: number
  series: Array<{ key: string; label: string; stroke: string; values: number[] }>
  axisMin: number; axisMax: number
  width: number; height: number; maxLen: number
}) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null)

  const onMouseMove = useCallback((e: React.MouseEvent<SVGRectElement>) => {
    const svg = e.currentTarget.ownerSVGElement
    if (!svg) return
    const ctm = svg.getScreenCTM()
    let svgX: number
    if (ctm) {
      const inv = ctm.inverse()
      svgX = inv.a * e.clientX + inv.c * e.clientY + inv.e
    } else {
      const rect = svg.getBoundingClientRect()
      svgX = ((e.clientX - rect.left) / rect.width) * width
    }
    const denominator = Math.max(1, maxLen - 1)
    const idx = Math.round(((svgX - chartLeft) / chartWidth) * denominator)
    setHoverIdx(Math.max(0, Math.min(idx, maxLen - 1)))
  }, [chartLeft, chartWidth, width, maxLen])

  const onMouseLeave = useCallback(() => setHoverIdx(null), [])

  const hp = hoverIdx !== null ? (() => {
    const range = (axisMax - axisMin) || 1
    const denominator = Math.max(1, maxLen - 1)
    const hx = chartLeft + (hoverIdx / denominator) * chartWidth
    const points = series
      .filter((s) => s.values.length > hoverIdx)
      .map((s) => {
        const value = s.values[hoverIdx]
        const plotted = Math.max(axisMin, Math.min(value, axisMax))
        const y = chartTop + chartHeight - ((plotted - axisMin) / range) * chartHeight
        return { key: s.key, label: s.label, stroke: s.stroke, value, y }
      })
    return { hx, points }
  })() : null

  const lineH = 12
  const boxW = 72
  const boxH = hp ? hp.points.length * lineH + 6 : 0

  return (
    <>
      {hp && (
        <>
          <line x1={hp.hx} y1={chartTop} x2={hp.hx} y2={chartTop + chartHeight}
            stroke="rgba(200,220,255,0.4)" strokeWidth="1" strokeDasharray="3 2" />
          {hp.points.map((p) => (
            <circle key={p.key} cx={hp.hx} cy={p.y} r="2.5" fill={p.stroke} stroke="#fff" strokeWidth="0.5" />
          ))}
          <rect
            x={hp.hx + (hp.hx > chartLeft + chartWidth * 0.65 ? -(boxW + 6) : 6)}
            y={Math.max(chartTop, Math.min(chartTop + chartHeight - boxH, chartTop + 4))}
            width={boxW} height={boxH} rx="3"
            fill="rgba(15,17,23,0.92)" stroke="rgba(120,176,255,0.3)" strokeWidth="0.5"
          />
          {hp.points.map((p, i) => (
            <text key={`t-${p.key}`}
              x={hp.hx + (hp.hx > chartLeft + chartWidth * 0.65 ? -(boxW + 2) : 10)}
              y={Math.max(chartTop, Math.min(chartTop + chartHeight - boxH, chartTop + 4)) + 10 + i * lineH}
              fill={p.stroke} fontSize="8" fontFamily="monospace"
            >
              {p.label}: {p.value.toFixed(1)}
            </text>
          ))}
        </>
      )}
      <rect
        x={chartLeft} y={chartTop}
        width={chartWidth} height={chartHeight}
        fill="transparent"
        onMouseMove={onMouseMove}
        onMouseLeave={onMouseLeave}
        style={{ cursor: 'crosshair' }}
      />
    </>
  )
}

function MultiLineSparkline({
  series,
  width = 240,
  height = 56,
  responsive = false,
  mode = 'axis',
  xStartLabel,
  xEndLabel,
  yMin,
  yMax,
  yTickCount = 4,
}: {
  series: Array<{ key: string; label: string; data: Array<number | null>; stroke: string }>
  width?: number
  height?: number
  responsive?: boolean
  mode?: SparkMode
  xStartLabel?: string
  xEndLabel?: string
  yMin?: number
  yMax?: number
  yTickCount?: number
}) {
  const hasAxis = mode === 'axis' || mode === 'points'
  const padding = hasAxis
    ? { top: 8, right: 8, bottom: 14, left: 30 }
    : { top: 0, right: 0, bottom: 0, left: 0 }
  const chartWidth = Math.max(1, width - padding.left - padding.right)
  const chartHeight = Math.max(1, height - padding.top - padding.bottom)
  const chartLeft = padding.left
  const chartTop = padding.top
  const chartBottom = chartTop + chartHeight
  const chartRight = chartLeft + chartWidth
  const tickCount = Math.max(2, Math.round(yTickCount))

  const preparedSeries = series.filter((item) => item.data.length > 0)
  const maxLen = preparedSeries.length
    ? Math.max(...preparedSeries.map((item) => item.data.length))
    : 0

  const normalizedSeries = preparedSeries.map((item) => {
    const padded = item.data.length >= maxLen
      ? item.data
      : Array(maxLen - item.data.length).fill(null).concat(item.data)
    const cleaned = padded.map((value) => (isNumber(value) ? value : null))
    const seed = cleaned.find(isNumber)
    if (!isNumber(seed)) {
      return { ...item, values: [] as number[], firstIdx: 0 }
    }
    const firstIdx = cleaned.findIndex(isNumber)
    let lastValue = seed
    const values = cleaned.map((value) => {
      if (!isNumber(value)) return lastValue
      lastValue = value
      return value
    })
    return { ...item, values, firstIdx }
  })

  const allValues = normalizedSeries.flatMap((item) => item.values)
  const numeric = allValues.filter(isNumber)
  const axisMin = isNumber(yMin) ? yMin : (numeric.length ? Math.min(...numeric) : 0)
  const axisMaxRaw = isNumber(yMax) ? yMax : (numeric.length ? Math.max(...numeric) : 100)
  const axisMax = axisMaxRaw <= axisMin ? axisMin + 1 : axisMaxRaw

  const buildTicks = (minVal: number, maxVal: number) => {
    const safeMax = maxVal <= minVal ? minVal + 1 : maxVal
    return Array.from({ length: tickCount }, (_, i) => {
      const ratio = i / (tickCount - 1)
      const y = chartTop + ratio * chartHeight
      const value = safeMax - ratio * (safeMax - minVal)
      return { y, value, index: i }
    })
  }

  const ticks = buildTicks(axisMin, axisMax)

  if (!preparedSeries.length || !numeric.length) {
    return (
      <svg
        width={responsive ? '100%' : width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        className="perf-sparkline"
      >
        {hasAxis && (
          <>
            <line x1={chartLeft} y1={chartTop} x2={chartLeft} y2={chartBottom} stroke={COLORS.border} strokeWidth="1" />
            {ticks.map((tick) => (
              <g key={`multi-empty-${tick.index}`}>
                <line
                  x1={chartLeft}
                  y1={tick.y}
                  x2={chartRight}
                  y2={tick.y}
                  stroke={tick.index === 0 || tick.index === tickCount - 1 ? COLORS.border : `${COLORS.border}88`}
                  strokeWidth="1"
                />
                <text
                  x={chartLeft - 2}
                  y={tick.y}
                  textAnchor="end"
                  dominantBaseline="middle"
                  className="perf-spark-axis"
                >
                  {tick.value.toFixed(0)}
                </text>
              </g>
            ))}
            <text x={chartLeft} y={height - 2} textAnchor="start" className="perf-spark-axis">{xStartLabel || ''}</text>
            <text x={chartRight} y={height - 2} textAnchor="end" className="perf-spark-axis">{xEndLabel || ''}</text>
          </>
        )}
      </svg>
    )
  }

  const range = axisMax - axisMin
  const denominator = Math.max(1, maxLen - 1)

  const pathBySeries = normalizedSeries.map((item) => {
    // Skip leading nulls so each line only starts at its first real sample,
    // leaving the pre-data left region blank.
    const points = item.values.map((value, index) => {
      if (index < item.firstIdx) return null
      const plotted = Math.max(axisMin, Math.min(value, axisMax))
      const x = chartLeft + (index / denominator) * chartWidth
      const y = chartTop + chartHeight - ((plotted - axisMin) / range) * chartHeight
      return `${x.toFixed(1)},${y.toFixed(1)}`
    }).filter((p): p is string => p !== null)
    return {
      key: item.key,
      stroke: item.stroke,
      path: points.length ? `M ${points.join(' L ')}` : '',
    }
  })

  return (
    <svg
      width={responsive ? '100%' : width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio={responsive && !hasAxis ? 'none' : 'xMidYMid meet'}
      className="perf-sparkline"
    >
      {hasAxis && (
        <>
          <line x1={chartLeft} y1={chartTop} x2={chartLeft} y2={chartBottom} stroke={COLORS.border} strokeWidth="1" />
          {ticks.map((tick) => (
            <g key={`multi-tick-${tick.index}`}>
              <line
                x1={chartLeft}
                y1={tick.y}
                x2={chartRight}
                y2={tick.y}
                stroke={tick.index === 0 || tick.index === tickCount - 1 ? COLORS.border : `${COLORS.border}88`}
                strokeWidth="1"
              />
              <text
                x={chartLeft - 2}
                y={tick.y}
                textAnchor="end"
                dominantBaseline="middle"
                className="perf-spark-axis"
              >
                {tick.value.toFixed(0)}
              </text>
            </g>
          ))}
          <text x={chartLeft} y={height - 2} textAnchor="start" className="perf-spark-axis">{xStartLabel || ''}</text>
          <text x={chartRight} y={height - 2} textAnchor="end" className="perf-spark-axis">{xEndLabel || ''}</text>
        </>
      )}

      {pathBySeries.map((item) =>
        item.path ? <path key={item.key} d={item.path} fill="none" stroke={item.stroke} strokeWidth="2" /> : null
      )}
      {mode === 'points' && pathBySeries.map((item) => {
        const ns = normalizedSeries.find((s) => s.key === item.key)
        if (!ns || !ns.values.length) return null
        const lastIdx = ns.values.length - 1
        const lastVal = ns.values[lastIdx]
        const plotted = Math.max(axisMin, Math.min(lastVal, axisMax))
        const cx = chartLeft + (lastIdx / denominator) * chartWidth
        const cy = chartTop + chartHeight - ((plotted - axisMin) / range) * chartHeight
        return (
          <g key={`pt-${item.key}`}>
            <circle cx={cx} cy={cy} r={3} fill={item.stroke} />
            <text x={cx - 4} y={cy - 5} textAnchor="end" className="perf-spark-axis" fill={item.stroke} style={{ fontSize: 9, fontWeight: 600 }}>
              {lastVal.toFixed(1)}
            </text>
          </g>
        )
      })}
      {/* Hover crosshair overlay for multi-line sparklines */}
      {hasAxis && normalizedSeries.length > 0 && maxLen > 4 && (
        <MultiLineHoverOverlay
          chartLeft={chartLeft}
          chartTop={chartTop}
          chartWidth={chartWidth}
          chartHeight={chartHeight}
          series={normalizedSeries.map((s) => ({ key: s.key, label: s.label, stroke: s.stroke, values: s.values }))}
          axisMin={axisMin}
          axisMax={axisMax}
          width={width}
          height={height}
          maxLen={maxLen}
        />
      )}
    </svg>
  )
}

/** Hover overlay for dual-axis sparklines (util % + freq MHz) */
function DualAxisHoverOverlay({
  chartLeft, chartTop, chartWidth, chartHeight,
  utilSeries, freqSeries,
  utilMin, utilMax, freqMin, freqMax,
  utilStroke, freqStroke,
  showUtil, showFreq,
  width, height, maxLen,
}: {
  chartLeft: number; chartTop: number; chartWidth: number; chartHeight: number
  utilSeries: Array<number | null>; freqSeries: Array<number | null>
  utilMin: number; utilMax: number; freqMin: number; freqMax: number
  utilStroke: string; freqStroke: string
  showUtil: boolean; showFreq: boolean
  width: number; height: number; maxLen: number
}) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null)

  const findLast = (series: Array<number | null>, idx: number, fallback: number) => {
    for (let i = idx; i >= 0; i--) {
      if (isNumber(series[i])) return series[i]!
    }
    return fallback
  }

  const onMouseMove = useCallback((e: React.MouseEvent<SVGRectElement>) => {
    const svg = e.currentTarget.ownerSVGElement
    if (!svg) return
    const ctm = svg.getScreenCTM()
    let svgX: number
    if (ctm) {
      const inv = ctm.inverse()
      svgX = inv.a * e.clientX + inv.c * e.clientY + inv.e
    } else {
      const rect = svg.getBoundingClientRect()
      svgX = ((e.clientX - rect.left) / rect.width) * width
    }
    const denominator = Math.max(1, maxLen - 1)
    const idx = Math.round(((svgX - chartLeft) / chartWidth) * denominator)
    setHoverIdx(Math.max(0, Math.min(idx, maxLen - 1)))
  }, [chartLeft, chartWidth, width, maxLen])

  const onMouseLeave = useCallback(() => setHoverIdx(null), [])

  const hp = hoverIdx !== null ? (() => {
    const denominator = Math.max(1, maxLen - 1)
    const hx = chartLeft + (hoverIdx / denominator) * chartWidth
    const points: Array<{ key: string; label: string; stroke: string; value: number; y: number; text: string }> = []

    if (showUtil) {
      const val = findLast(utilSeries, hoverIdx, utilMin)
      const uRange = (utilMax - utilMin) || 1
      const y = chartTop + chartHeight - ((Math.max(utilMin, Math.min(val, utilMax)) - utilMin) / uRange) * chartHeight
      points.push({ key: 'util', label: 'Util', stroke: utilStroke, value: val, y, text: `${val.toFixed(1)}%` })
    }
    if (showFreq) {
      const val = findLast(freqSeries, hoverIdx, freqMin)
      const fRange = (freqMax - freqMin) || 1
      const y = chartTop + chartHeight - ((Math.max(freqMin, Math.min(val, freqMax)) - freqMin) / fRange) * chartHeight
      points.push({ key: 'freq', label: 'Freq', stroke: freqStroke, value: val, y, text: `${Math.round(val)}MHz` })
    }
    return { hx, points }
  })() : null

  const lineH = 12
  const boxW = 78
  const boxH = hp ? hp.points.length * lineH + 6 : 0

  return (
    <>
      {hp && (
        <>
          <line x1={hp.hx} y1={chartTop} x2={hp.hx} y2={chartTop + chartHeight}
            stroke="rgba(200,220,255,0.4)" strokeWidth="1" strokeDasharray="3 2" />
          {hp.points.map((p) => (
            <circle key={p.key} cx={hp.hx} cy={p.y} r="2.5" fill={p.stroke} stroke="#fff" strokeWidth="0.5" />
          ))}
          <rect
            x={hp.hx + (hp.hx > chartLeft + chartWidth * 0.65 ? -(boxW + 6) : 6)}
            y={Math.max(chartTop, Math.min(chartTop + chartHeight - boxH, chartTop + 4))}
            width={boxW} height={boxH} rx="3"
            fill="rgba(15,17,23,0.92)" stroke="rgba(120,176,255,0.3)" strokeWidth="0.5"
          />
          {hp.points.map((p, i) => (
            <text key={`t-${p.key}`}
              x={hp.hx + (hp.hx > chartLeft + chartWidth * 0.65 ? -(boxW + 2) : 10)}
              y={Math.max(chartTop, Math.min(chartTop + chartHeight - boxH, chartTop + 4)) + 10 + i * lineH}
              fill={p.stroke} fontSize="8" fontFamily="monospace"
            >
              {p.label}: {p.text}
            </text>
          ))}
        </>
      )}
      <rect
        x={chartLeft} y={chartTop}
        width={chartWidth} height={chartHeight}
        fill="transparent"
        onMouseMove={onMouseMove}
        onMouseLeave={onMouseLeave}
        style={{ cursor: 'crosshair' }}
      />
    </>
  )
}

function DualAxisSparkline({
  utilData,
  freqData,
  width = 220,
  height = 58,
  utilStroke,
  freqStroke,
  responsive = false,
  xStartLabel,
  xEndLabel,
  utilAxis,
  freqAxis,
  utilTickCount = 5,
  freqTickCount = 4,
  showUtil = true,
  showFreq = true,
}: {
  utilData: Array<number | null>
  freqData: Array<number | null>
  width?: number
  height?: number
  utilStroke: string
  freqStroke: string
  responsive?: boolean
  xStartLabel?: string
  xEndLabel?: string
  utilAxis: { min: number; max: number }
  freqAxis: { min: number; max: number }
  utilTickCount?: number
  freqTickCount?: number
  showUtil?: boolean
  showFreq?: boolean
}) {
  const id = useId()
  const padding = { top: 8, right: 34, bottom: 14, left: 30 }
  const chartWidth = Math.max(1, width - padding.left - padding.right)
  const chartHeight = Math.max(1, height - padding.top - padding.bottom)
  const chartLeft = padding.left
  const chartTop = padding.top
  const chartBottom = chartTop + chartHeight
  const chartRight = chartLeft + chartWidth

  const maxLen = Math.max(utilData.length, freqData.length)
  const padSeries = (series: Array<number | null>) => {
    if (series.length >= maxLen) return series
    return Array(maxLen - series.length).fill(null).concat(series)
  }

  const utilSeries = padSeries(utilData).map((value) => (isNumber(value) ? value : null))
  const freqSeries = padSeries(freqData).map((value) => (isNumber(value) ? value : null))
  const hasAnyPoint = utilSeries.some(isNumber) || freqSeries.some(isNumber)

  const utilMin = utilAxis.min
  const utilMax = utilAxis.max <= utilAxis.min ? utilAxis.min + 1 : utilAxis.max
  const freqMin = freqAxis.min
  const freqMax = freqAxis.max <= freqAxis.min ? freqAxis.min + 1 : freqAxis.max

  const buildTicks = (tickCount: number, minVal: number, maxVal: number) => {
    const safeCount = Math.max(2, Math.round(tickCount))
    return Array.from({ length: safeCount }, (_, i) => {
      const ratio = i / (safeCount - 1)
      const y = chartTop + ratio * chartHeight
      const value = maxVal - ratio * (maxVal - minVal)
      return { y, value, index: i, isEdge: i === 0 || i === safeCount - 1 }
    })
  }

  const utilTicks = buildTicks(utilTickCount, utilMin, utilMax)
  const freqTicks = buildTicks(freqTickCount, freqMin, freqMax)

  const buildPath = (series: Array<number | null>, minVal: number, maxVal: number) => {
    const seed = series.find(isNumber) ?? minVal
    // Skip leading nulls so the line starts at the first real sample, leaving
    // the pre-data left region blank.
    const firstIdx = series.findIndex(isNumber)
    let lastValue = seed
    const range = maxVal - minVal
    const denominator = Math.max(1, maxLen - 1)

    const points = series.map((value, index) => {
      const nextValue = isNumber(value) ? value : lastValue
      lastValue = nextValue
      if (firstIdx < 0 || index < firstIdx) return null
      const plotted = Math.max(minVal, Math.min(nextValue, maxVal))
      const x = chartLeft + (index / denominator) * chartWidth
      const y = chartTop + chartHeight - ((plotted - minVal) / range) * chartHeight
      return `${x.toFixed(1)},${y.toFixed(1)}`
    }).filter((p): p is string => p !== null)

    return points.length ? `M ${points.join(' L ')}` : ''
  }

  const utilPath = hasAnyPoint ? buildPath(utilSeries, utilMin, utilMax) : ''
  const freqPath = hasAnyPoint ? buildPath(freqSeries, freqMin, freqMax) : ''

  return (
    <svg
      width={responsive ? '100%' : width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="xMidYMid meet"
      className="perf-sparkline"
    >
      <defs>
        <linearGradient id={`spark-dual-util-${id}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={utilStroke} stopOpacity="0.4" />
          <stop offset="100%" stopColor={utilStroke} stopOpacity="0" />
        </linearGradient>
        <linearGradient id={`spark-dual-freq-${id}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={freqStroke} stopOpacity="0.4" />
          <stop offset="100%" stopColor={freqStroke} stopOpacity="0" />
        </linearGradient>
      </defs>

      <line x1={chartLeft} y1={chartTop} x2={chartLeft} y2={chartBottom} stroke={COLORS.border} strokeWidth="1" />
      <line x1={chartRight} y1={chartTop} x2={chartRight} y2={chartBottom} stroke={COLORS.border} strokeWidth="1" />

      {utilTicks.map((tick) => (
        <g key={`dual-util-tick-${tick.index}`}>
          <line
            x1={chartLeft}
            y1={tick.y}
            x2={chartRight}
            y2={tick.y}
            stroke={tick.isEdge ? COLORS.border : `${COLORS.border}88`}
            strokeWidth="1"
          />
          <text
            x={chartLeft - 2}
            y={tick.y}
            textAnchor="end"
            dominantBaseline="middle"
            className="perf-spark-axis"
          >
            {tick.value.toFixed(0)}
          </text>
        </g>
      ))}

      {freqTicks.map((tick) => (
        <text
          key={`dual-freq-tick-${tick.index}`}
          x={chartRight + 2}
          y={tick.y}
          textAnchor="start"
          dominantBaseline="middle"
          className="perf-spark-axis"
        >
          {tick.value.toFixed(0)}
        </text>
      ))}

      <text x={chartLeft} y={height - 2} textAnchor="start" className="perf-spark-axis">{xStartLabel || ''}</text>
      <text x={chartRight} y={height - 2} textAnchor="end" className="perf-spark-axis">{xEndLabel || ''}</text>

      {freqPath && showFreq !== false && <path d={freqPath} fill="none" stroke={freqStroke} strokeWidth="2" />}
      {utilPath && showUtil !== false && <path d={utilPath} fill="none" stroke={utilStroke} strokeWidth="2" />}
      {/* Hover crosshair overlay */}
      {hasAnyPoint && maxLen > 4 && (
        <DualAxisHoverOverlay
          chartLeft={chartLeft}
          chartTop={chartTop}
          chartWidth={chartWidth}
          chartHeight={chartHeight}
          utilSeries={utilSeries}
          freqSeries={freqSeries}
          utilMin={utilMin}
          utilMax={utilMax}
          freqMin={freqMin}
          freqMax={freqMax}
          utilStroke={utilStroke}
          freqStroke={freqStroke}
          showUtil={showUtil !== false}
          showFreq={showFreq !== false}
          width={width}
          height={height}
          maxLen={maxLen}
        />
      )}
    </svg>
  )
}

function SectionTitle({
  title,
  subtitle,
  help,
  action,
}: {
  title: string
  subtitle?: string
  help?: string
  action?: React.ReactNode
}) {
  return (
    <div className="perf-section-title">
      <div>
        <div className="perf-section-heading">
          <Text className="perf-section-eyebrow">{title}</Text>
          {help && (
            <AntTooltip title={help}>
              <button type="button" className="perf-section-help" aria-label={`About ${title}`}>
                <QuestionCircleOutlined />
              </button>
            </AntTooltip>
          )}
        </div>
        {subtitle && (
          <Text className="perf-section-subtitle">
            {subtitle}
          </Text>
        )}
      </div>
      {action}
    </div>
  )
}

function PressurePointerGauge({
  title,
  valuePct,
  subtitle,
  description,
  levelLabel,
}: {
  title: string
  valuePct: number | null
  subtitle?: string
  description?: string
  levelLabel?: string
}) {
  const hasValue = isNumber(valuePct)
  const pct = hasValue ? Math.max(0, Math.min(valuePct, 100)) : 0
  const normalized = pct / 100
  const normalizedLevel = levelLabel?.trim().toUpperCase()
  const color = normalizedLevel === 'CRITICAL'
    ? COLORS.red
    : normalizedLevel === 'HIGH' || normalizedLevel === 'WARNING'
      ? COLORS.orange
      : normalizedLevel === 'MEDIUM'
        ? COLORS.yellow
        : getPressureColor(normalized)
  const pointerColor = COLORS.gaugeNeedle
  const label = levelLabel ?? (hasValue ? getPressureLabel(normalized) : '0%')
  const needleAngleDeg = 180 - (pct * 180) / 100
  const needleAngleRad = (needleAngleDeg * Math.PI) / 180
  const needleLength = 18
  const needleX2 = 50 + needleLength * Math.cos(needleAngleRad)
  const needleY2 = 64 - needleLength * Math.sin(needleAngleRad)
  const gaugeData = [{ value: pct, fill: color }]

  return (
    <div className="perf-pressure-pointer-card">
      <Text className="perf-pressure-pointer-title">{title}</Text>

      <div className="perf-pressure-pointer-wrap">
        <ResponsiveContainer width="100%" height="100%">
          <RadialBarChart
            cx="50%"
            cy="64%"
            innerRadius="56%"
            outerRadius="80%"
            startAngle={180}
            endAngle={0}
            data={gaugeData}
          >
            <PolarAngleAxis type="number" domain={[0, 100]} tick={false} />
            <RadialBar
              background={{ fill: COLORS.gaugeTrack }}
              dataKey="value"
              cornerRadius={4}
              fill={color}
              stroke="none"
            />
          </RadialBarChart>
        </ResponsiveContainer>

        <svg className="perf-pressure-needle" viewBox="0 0 100 100" preserveAspectRatio="xMidYMid meet" aria-hidden="true">
          <line
            x1="50"
            y1="64"
            x2={needleX2}
            y2={needleY2}
            stroke={pointerColor}
            strokeWidth="2.2"
            strokeLinecap="round"
          />
          <circle cx="50" cy="64" r="2.6" fill={pointerColor} />
        </svg>

        <div className="perf-pressure-pointer-value" style={{ color }}>
          {`${Math.round(pct)}%`}
        </div>
      </div>

      <Tag
        style={{
          color,
          borderColor: color,
          background: `${color}20`,
          fontSize: 11,
          letterSpacing: 1,
          fontWeight: 700,
          marginTop: 6,
        }}
      >
        {label}
      </Tag>

      {subtitle && <Text className="perf-pressure-pointer-subtitle">{subtitle}</Text>}
      {description && <Text className="perf-pressure-pointer-description">{description}</Text>}
    </div>
  )
}

function PressureMiniStat({
  title,
  value,
  level,
  subtitle,
  help,
  zones,
}: {
  title: string
  value: number | null
  level?: string | null
  subtitle?: string
  help?: string
  zones: PressureZones
}) {
  const normalized = isNumber(value) ? Math.max(0, Math.min(value, 100)) : null
  const normalizedLevel = level?.toUpperCase()
  const color = normalizedLevel === 'CRITICAL'
    ? COLORS.red
    : normalizedLevel === 'HIGH' || normalizedLevel === 'WARNING'
      ? COLORS.orange
      : normalizedLevel === 'MEDIUM'
        ? COLORS.yellow
        : normalized === null ? COLORS.textMuted : getPressureColor(normalized / 100)

  // Pointer sits at the exact value; the floating level pill anchors to the
  // nearest edge when the value is near 0/100 so it never clips off the track.
  const pointerLeft = normalized ?? 0

  // Severity backdrop reflects this pressure type's own configured cut-offs.
  const trackGradient = `linear-gradient(90deg,`
    + ` rgba(115, 191, 105, 0.2) 0 ${zones.medium}%,`
    + ` rgba(250, 222, 42, 0.2) ${zones.medium}% ${zones.high}%,`
    + ` rgba(245, 158, 11, 0.22) ${zones.high}% ${zones.critical}%,`
    + ` rgba(196, 22, 42, 0.24) ${zones.critical}% 100%)`

  return (
    <div className="perf-pressure-mini-stat">
      {/* Status pill is pinned top-right: only its colour/text change, never its
          position, so high-frequency value swings never jitter the layout. */}
      <div className="perf-pressure-mini-header">
        <div className="perf-pressure-mini-title-row">
          <Text className="perf-pressure-mini-title">{title}</Text>
          {help && (
            <AntTooltip title={help}>
              <QuestionCircleOutlined className="perf-pressure-mini-help" />
            </AntTooltip>
          )}
        </div>
        <span
          className="perf-pressure-mini-marker-pill"
          style={{ ['--pill-color' as string]: color, borderColor: `${color}80`, background: `${color}1f` } as React.CSSProperties}
        >
          {normalizedLevel || 'NORMAL'}
        </span>
      </div>
      <div className="perf-pressure-mini-content">
        <div className="perf-pressure-mini-value-row">
          <strong style={{ color }}>{normalized === null ? '—' : `${Math.round(normalized)}%`}</strong>
        </div>
        <div className="perf-pressure-mini-gauge">
          <span className="perf-pressure-mini-pointer" style={{ left: `${pointerLeft}%`, borderTopColor: color }} aria-hidden="true" />
          <div className="perf-pressure-mini-track" style={{ background: trackGradient }} aria-label={`${title}: ${normalized === null ? 'unavailable' : `${Math.round(normalized)} percent`}`}>
            <div className="perf-pressure-mini-fill" style={{ width: `${normalized ?? 0}%`, background: color }} />
          </div>
          <div className="perf-pressure-mini-scale" aria-hidden="true">
            <span>0%</span>
            <span>50%</span>
            <span>100%</span>
          </div>
        </div>
      </div>
      {subtitle && <span className="perf-pressure-mini-detail" title={subtitle}>{subtitle}</span>}
    </div>
  )
}

function CockpitCard({
  title,
  onOpen,
  children,
}: {
  title: string
  onOpen?: () => void
  children: React.ReactNode
}) {
  return (
    <Card
      className="perf-card perf-cockpit-card"
      bodyStyle={{ padding: 14 }}
      role="button"
      tabIndex={0}
      aria-label={`Open ${title}`}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen?.()
        }
      }}
    >
      <div className="perf-cockpit-head">
        <span className="perf-cockpit-title">
          <Text className="perf-pressure-summary-eyebrow">{title}</Text>
          <ExportOutlined className="perf-cockpit-entry-icon" aria-hidden="true" />
        </span>
      </div>
      {children}
    </Card>
  )
}

function TopConsumersCard({ apps, onOpen }: { apps: AppResourceEntry[] | null; onOpen?: () => void }) {
  return (
    <CockpitCard title="Top Resource Consumers" onOpen={onOpen}>
      {apps === null ? (
        <div className="perf-cockpit-empty"><Spin size="small" /></div>
      ) : apps.length === 0 ? (
        <div className="perf-cockpit-empty">No active consumers</div>
      ) : (
        <div className="perf-cockpit-apps">
          <div className="perf-cockpit-app-row perf-cockpit-app-head">
            <span className="perf-cockpit-app-label">App</span>
            <span>CPU</span>
            <span>Memory</span>
            <span>Score</span>
          </div>
          {apps.map((app, index) => (
            <div className="perf-cockpit-app-row" key={`${app.app_id || app.app_name}-${index}`}>
              <span className="perf-cockpit-app-name" title={app.app_name}>
                <span className="perf-cockpit-rank">{index + 1}</span>
                {app.app_name}
              </span>
              <span className="perf-cockpit-num">{formatPercent(app.cpu_usage * 100, 1)}</span>
              <span className="perf-cockpit-num">{formatMemoryGb(app.memory_mb)}</span>
              <span className="perf-cockpit-num perf-cockpit-score">{Math.round(app.score)}</span>
            </div>
          ))}
          <div className="perf-cockpit-app-foot">Top {apps.length} by resource score</div>
        </div>
      )}
    </CockpitCard>
  )
}

function DiagnosticsSummaryCard({
  summary,
  status,
  onOpen,
}: {
  summary: DiagCockpitSummary | null
  status: 'loading' | 'ready' | 'unavailable'
  onOpen?: () => void
}) {
  // Built per-render so severity colours follow the active theme (COLORS is a
  // mutable singleton swapped on theme toggle).
  const severityMeta: Array<{ key: DiagSeverity; label: string; color: string }> = [
    { key: 'critical', label: 'Crit', color: COLORS.red },
    { key: 'error', label: 'Err', color: COLORS.orange },
    { key: 'warning', label: 'Warn', color: COLORS.yellow },
    { key: 'info', label: 'Info', color: COLORS.accent },
  ]
  const featuredSeverity = summary?.attentionAlert?.severity || summary?.insight?.severity
  const featuredColor = featuredSeverity === 'critical'
    ? COLORS.red
    : featuredSeverity === 'error'
      ? COLORS.orange
      : featuredSeverity === 'warning'
        ? COLORS.yellow
          : summary?.attentionCount ? COLORS.accent : COLORS.green
        const statusColor = summary?.attentionCount ? COLORS.orange : COLORS.green
        const statusLabel = summary?.attentionCount ? 'Attention' : 'Healthy'
  return (
    <CockpitCard title="System Diagnostics" onOpen={onOpen}>
      {status === 'unavailable' && !summary ? (
        <div className="perf-cockpit-empty">Diagnostics unavailable · retrying</div>
      ) : !summary ? (
        <div className="perf-cockpit-empty"><Spin size="small" /></div>
      ) : (
        <div className="perf-diag-overview">
          <div className="perf-diag-total-row">
            <div className="perf-diag-total">
              <strong>{summary.attentionCount}</strong>
              <span>{summary.attentionCount === 1 ? 'alert needs attention' : 'alerts need attention'}</span>
            </div>
            <span
              className="perf-diag-state"
              style={{ color: statusColor, borderColor: `${statusColor}80`, background: `${statusColor}1f` }}
            >
              {statusLabel}
            </span>
          </div>
          <div className="perf-diag-events">
            <span className="perf-diag-events-label">Events · last 1 hour</span>
            <div className="perf-diag-breakdown">
              {severityMeta.map(({ key, label, color }) => {
                const count = summary.counts[key] ?? 0
                return (
                  <div key={key}>
                    <span>{label}</span>
                    <strong style={{ color: count > 0 ? color : COLORS.textMuted }}>{count}</strong>
                  </div>
                )
              })}
            </div>
          </div>
          <div className={`perf-diag-featured${summary.attentionAlert || summary.insight ? '' : ' perf-diag-featured--healthy'}`}>
            {summary.attentionAlert ? (
              <>
                <span className="perf-diag-featured-dot" style={{ background: featuredColor }} />
                <span title={summary.attentionAlert.text}>{summary.attentionAlert.text}</span>
              </>
            ) : summary.insight ? (
              <>
                <span className="perf-diag-featured-dot" style={{ background: featuredColor }} />
                <span title={summary.insight.observation || summary.insight.title}>
                  {summary.insight.title}
                </span>
              </>
            ) : (
              <span>No findings in the last 1 hour</span>
            )}
          </div>
        </div>
      )}
    </CockpitCard>
  )
}

function ActiveControlsCard({ summary, onOpen }: { summary: ControlCockpitSummary | null; onOpen?: () => void }) {
  const total = summary ? summary.auto + summary.manual : 0
  const statusColor = total === 0
    ? COLORS.green
    : summary?.auto ? COLORS.orange : COLORS.accent
  return (
    <CockpitCard title="Resource Controls" onOpen={onOpen}>
      {!summary ? (
        <div className="perf-cockpit-empty"><Spin size="small" /></div>
      ) : (
        <div className="perf-control-overview">
          <div className="perf-control-total-row">
            <div className="perf-control-total">
              <strong>{total}</strong>
              <span>{total === 1 ? 'app under control' : 'apps under control'}</span>
            </div>
            <span
              className="perf-control-state"
              style={{ color: statusColor, borderColor: `${statusColor}80`, background: `${statusColor}1f` }}
            >
              {total > 0 ? 'Active' : 'Normal'}
            </span>
          </div>
          <div className="perf-control-breakdown">
            <div>
              <span>Auto control</span>
              <strong style={{ color: summary.auto > 0 ? COLORS.orange : COLORS.textMuted }}>{summary.auto}</strong>
            </div>
            <div>
              <span>Manual control</span>
              <strong style={{ color: summary.manual > 0 ? COLORS.accent : COLORS.textMuted }}>{summary.manual}</strong>
            </div>
          </div>
          <div className={`perf-control-featured${summary.featured ? '' : ' perf-control-featured--normal'}`}>
            {summary.featured ? (
              <>
                <span className="perf-control-featured-label">Controlled</span>
                <div
                  className="perf-control-featured-copy"
                  title={summary.featured.limit ? `${summary.featured.name} (${summary.featured.limit})` : summary.featured.name}
                >
                  <strong>{summary.featured.name}</strong>
                  {summary.featured.limit && <span>({summary.featured.limit})</span>}
                </div>
                {summary.remaining > 0 && <span className="perf-control-more">+{summary.remaining} more</span>}
              </>
            ) : (
              <span>All applications running unrestricted</span>
            )}
          </div>
        </div>
      )}
    </CockpitCard>
  )
}

// Width reserved to the right of the plot for the end-of-line series labels.
const TREND_LABEL_GUTTER = 58
// Minimum vertical distance between two labels before they are pushed apart.
const TREND_LABEL_MIN_GAP = 13

/**
 * Names every curve at its right-hand end, with overlap resolved across series.
 *
 * A per-`Line` `LabelList` cannot do this: each one only knows its own point, so
 * two series sitting at the same utilization stack their text on top of each
 * other -- which is the whole problem this solves. `Customized` hands the chart's
 * axis scales and plot offset to one component, so all the end points can be
 * laid out together: sorted by height, then separated by a fixed minimum gap and
 * clamped back inside the plot area.
 */
function TrendEndLabels(props: {
  // Deliberately not named `series` / `data`: Customized clones this element
  // with the chart's own props and state spread over it, so any name recharts
  // already uses would be silently overwritten.
  trendSeries?: Array<{ key: string; label: string; color: string }>
  trendData?: Array<Record<string, number | null>>
  // Injected by recharts' Customized: the y scale keyed by axis id, and the
  // plot rectangle inside the chart's margins.
  yAxisMap?: Record<string, { scale?: (value: number) => number }>
  offset?: { top?: number; left?: number; width?: number; height?: number }
}) {
  const { trendSeries: series = [], trendData: data = [], yAxisMap, offset } = props
  const scale = yAxisMap?.['0']?.scale
  if (!scale || !offset || typeof offset.top !== 'number' || typeof offset.height !== 'number') return null

  const plotTop = offset.top
  const plotBottom = offset.top + offset.height
  const plotRight = (offset.left ?? 0) + (offset.width ?? 0)

  // The newest sample of each series; a series with nothing in the window is
  // simply not labelled rather than being pinned to zero.
  const anchors = series
    .map((item) => {
      for (let index = data.length - 1; index >= 0; index -= 1) {
        const value = data[index]?.[item.key]
        if (isNumber(value)) return { ...item, y: scale(value) }
      }
      return null
    })
    .filter((item): item is { key: string; label: string; color: string; y: number } => (
      item !== null && Number.isFinite(item.y)
    ))

  if (!anchors.length) return null

  // De-overlap: walk top-down pushing collisions apart, then bottom-up to pull
  // back anything that was pushed past the bottom edge.
  const laidOut = [...anchors].sort((left, right) => left.y - right.y)
  const positions = laidOut.map((item) => item.y)
  for (let index = 1; index < positions.length; index += 1) {
    const minimum = positions[index - 1] + TREND_LABEL_MIN_GAP
    if (positions[index] < minimum) positions[index] = minimum
  }
  const lowest = plotBottom - TREND_LABEL_MIN_GAP / 2
  if (positions.length && positions[positions.length - 1] > lowest) {
    positions[positions.length - 1] = lowest
    for (let index = positions.length - 2; index >= 0; index -= 1) {
      const maximum = positions[index + 1] - TREND_LABEL_MIN_GAP
      if (positions[index] > maximum) positions[index] = maximum
    }
  }

  return (
    <g className="perf-trend-end-labels" pointerEvents="none">
      {laidOut.map((item, index) => {
        const labelY = Math.max(plotTop + 5, Math.min(plotBottom, positions[index]))
        return (
          <g key={item.key}>
            {/* Connector from the curve's end to its displaced label. */}
            <polyline
              points={`${plotRight},${item.y} ${plotRight + 6},${labelY} ${plotRight + 10},${labelY}`}
              fill="none"
              stroke={item.color}
              strokeOpacity={0.6}
              strokeWidth={1}
            />
            <text x={plotRight + 13} y={labelY + 3.5} fill={item.color} fontSize={10} fontWeight={600}>
              {item.label}
            </text>
          </g>
        )
      })}
    </g>
  )
}

function SystemHealthTrend({
  data,
  trendWindow,
  series,
  onOpenHistory,
}: {
  data: Array<Record<string, number | null>>
  trendWindow: '1m' | '5m'
  series: Array<{ key: string; label: string; color: string }>
  onOpenHistory?: () => void
}) {
  const [hiddenSeries, setHiddenSeries] = useState<Set<string>>(() => new Set())
  const xTicks = data.length > 1 ? [0, data.length - 1] : [0]
  const visibleSeries = series.filter((item) => !hiddenSeries.has(item.key))
  const toggleSeries = (key: string) => {
    setHiddenSeries((current) => {
      const next = new Set(current)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <Card className="perf-card perf-system-health" bodyStyle={{ padding: 14 }}>
      <div className="perf-system-health-head">
        <Text className="perf-system-health-title">UTILIZATION TREND</Text>
        <div className="perf-system-health-controls">
          <div className="perf-system-health-legend">
            {series.map((item) => (
              <button
                key={item.key}
                type="button"
                className={`perf-system-health-legend-item${hiddenSeries.has(item.key) ? ' perf-system-health-legend-item--hidden' : ''}`}
                aria-pressed={!hiddenSeries.has(item.key)}
                onClick={() => toggleSeries(item.key)}
              >
                <i style={{ background: item.color }} />{item.label}
              </button>
            ))}
          </div>
          <AntTooltip title="Open history">
            <Button
              type="text"
              size="small"
              className="perf-system-health-history"
              icon={<ExportOutlined />}
              aria-label="Open utilization history"
              onClick={onOpenHistory}
            />
          </AntTooltip>
        </div>
      </div>
      <div className="perf-system-health-chart">
        <ResponsiveContainer width="100%" height="100%">
          {/* The right margin is the gutter TrendEndLabels draws its names into. */}
          <LineChart data={data} margin={{ top: 4, right: TREND_LABEL_GUTTER, bottom: 8, left: 8 }}>
            <CartesianGrid vertical={false} stroke={`${COLORS.border}88`} strokeDasharray="3 3" />
            <YAxis
              domain={[0, 100]}
              ticks={[0, 25, 50, 75, 100]}
              width={30}
              tick={{ fill: COLORS.textMuted, fontSize: 10 }}
              tickLine={false}
              axisLine={{ stroke: COLORS.border }}
              tickFormatter={(value) => `${value}%`}
            />
            <XAxis
              dataKey="index"
              ticks={xTicks}
              tick={{ fill: COLORS.textMuted, fontSize: 10 }}
              tickLine={false}
              axisLine={{ stroke: COLORS.border }}
              tickFormatter={(value) => Number(value) === data.length - 1 ? 'Now' : `${trendWindow} ago`}
            />
            <Tooltip
              contentStyle={{ background: COLORS.panelBg, borderColor: COLORS.border, color: COLORS.text, fontSize: 12 }}
              labelFormatter={(value) => Number(value) === data.length - 1 ? 'Now' : `${trendWindow} ago`}
              formatter={(value: number, name: string) => [`${value.toFixed(1)}%`, name]}
            />
            {visibleSeries.map((item) => (
              <Line key={item.key} type="monotone" dataKey={item.key} name={item.label} stroke={item.color} strokeWidth={2} dot={false} connectNulls />
            ))}
            {/* Last, so the labels paint above the lines. */}
            <Customized component={TrendEndLabels} trendSeries={visibleSeries} trendData={data} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </Card>
  )
}

function DetailItem({
  label,
  value,
  source,
}: {
  label: string
  value: string
  source?: DataSourceKind
}) {
  return (
    <div className="perf-detail-item">
      <div className="perf-detail-label-row">
        <Text className="perf-detail-label">{label}</Text>
      </div>
      <Text className="perf-detail-value" title={value}>
        {value}
      </Text>
    </div>
  )
}

type DetailRow = { label: string; value: string; source?: DataSourceKind; divider?: boolean }

function TrendPanel({
  title,
  accent,
  value,
  unit,
  statusColor,
  series,
  details,
  subtitle,
  sparkMode,
  trendWindow,
  multiSeries,
  splitBars,
  multiSeriesYMin,
  multiSeriesYMax,
  multiSeriesYTickCount,
  secondaryChart,
  secondaryChartPosition = 'bottom',
  compact = false,
  centerBody = false,
  compactDetails = false,
  primaryChartHeight = 80,
  secondaryChartGap = 10,
  detailTopMargin = 12,
  primaryChartLabel,
  alert,
}: {
  title: string
  accent: string
  value: number | null
  unit?: string
  statusColor?: string
  series: Array<number | null>
  details: Array<{ label: string; value: string; source?: DataSourceKind; divider?: boolean }>
  subtitle?: string
  sparkMode?: SparkMode
  trendWindow?: '1m' | '5m'
  multiSeries?: Array<{ key: string; label: string; data: Array<number | null>; stroke: string }>
  splitBars?: Array<{ key: string; label: string; value: number | null; color: string; sublabel?: string }>
  multiSeriesYMin?: number
  multiSeriesYMax?: number
  multiSeriesYTickCount?: number
  secondaryChart?: React.ReactNode
  secondaryChartPosition?: 'top' | 'bottom'
  compact?: boolean
  centerBody?: boolean
  compactDetails?: boolean
  primaryChartHeight?: number
  secondaryChartGap?: number
  detailTopMargin?: number
  primaryChartLabel?: string
  alert?: { text: string; color: string }
}) {
  const gaugeValue = isNumber(value) ? Math.max(0, Math.min(value, 100)) : 0
  const hasValue = isNumber(value)
  const gaugeColor = statusColor || accent
  const suffix = unit || '%'
  const valueText = hasValue ? `${formatNumber(gaugeValue, 1)}${suffix}` : 'N/A'
  const showMultiSeries = Boolean(multiSeries && multiSeries.length > 1)
  const hasSplitBars = Boolean(splitBars && splitBars.length > 0)

  return (
    <Card className={`perf-card perf-rise perf-trend-card ${compact ? 'perf-trend-card--compact' : ''}`} bodyStyle={{ padding: 16 }}>
      <div className="perf-trend-head">
        <Space size={8}>
          <span className="perf-trend-dot" style={{ background: accent }} />
          <Text style={{ color: COLORS.text, fontSize: 13, fontWeight: 600 }}>{title}</Text>
        </Space>
      </div>
      {alert && (
        <div
          style={{
            marginTop: 8,
            padding: '6px 10px',
            borderRadius: 6,
            border: `1px solid ${alert.color}`,
            background: `${alert.color}1a`,
            color: alert.color,
            fontSize: 11,
            fontWeight: 600,
            lineHeight: 1.4,
          }}
        >
          {alert.text}
        </div>
      )}
      <div className={`perf-trend-body ${hasSplitBars ? 'perf-trend-body--split' : ''} ${centerBody ? 'perf-trend-body--center' : ''}`}>
        <div className="perf-trend-main">
          <div className="perf-insight-metric">
            {!hasSplitBars && (
              <>
                <div className="perf-insight-value-row">
                  <Text className="perf-insight-value">{valueText}</Text>
                </div>
                <div className="perf-insight-bar-track">
                  <div
                    className="perf-insight-bar-fill"
                    style={{
                      width: `${Math.round(gaugeValue)}%`,
                      background: `linear-gradient(90deg, ${gaugeColor}66, ${gaugeColor})`,
                      boxShadow: `0 0 14px ${gaugeColor}66`,
                    }}
                  />
                </div>
                <div className="perf-insight-scale">
                  <span>0</span>
                  <span>50</span>
                  <span>100</span>
                </div>
              </>
            )}
            {hasSplitBars && (
              <div style={{ marginTop: 4, display: 'grid', gap: 8 }}>
                {(splitBars ?? []).map((bar) => {
                  const splitGauge = isNumber(bar.value) ? Math.max(0, Math.min(bar.value, 100)) : 0
                  return (
                    <div key={`${title}-${bar.key}`}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                        <div>
                          <Text style={{ color: COLORS.textMuted, fontSize: 10, display: 'block', lineHeight: 1.4 }}>{bar.label}</Text>
                          {bar.sublabel && (
                            <Text style={{ color: COLORS.textMuted, fontSize: 10, display: 'block', lineHeight: 1.2 }}>{bar.sublabel}</Text>
                          )}
                        </div>
                        <Text style={{ color: bar.color, fontSize: 24, fontWeight: 700, lineHeight: 1 }}>
                          {isNumber(bar.value) ? bar.value.toFixed(1) : '—'}
                          <span style={{ color: COLORS.textMuted, fontSize: 11, marginLeft: 2 }}>%</span>
                        </Text>
                      </div>
                      <div className="perf-insight-bar-track">
                        <div
                          className="perf-insight-bar-fill"
                          style={{
                            width: `${Math.round(splitGauge)}%`,
                            background: `linear-gradient(90deg, ${bar.color}66, ${bar.color})`,
                            boxShadow: `0 0 10px ${bar.color}55`,
                          }}
                        />
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
          {subtitle && (
            <Text className="perf-trend-subtitle" title={subtitle}>
              {subtitle}
            </Text>
          )}
        </div>
        <div className="perf-trend-chart">
          {secondaryChart && secondaryChartPosition === 'top' && (
            <div style={{ marginBottom: secondaryChartGap }}>{secondaryChart}</div>
          )}
          {showMultiSeries && multiSeries ? (
            <>
              <div className="perf-series-legend">
                {multiSeries.map((item) => (
                  <span className="perf-series-legend-item" key={`${title}-${item.key}`}>
                    <span className="perf-series-legend-dot" style={{ background: item.stroke }} />
                    {item.label}
                  </span>
                ))}
              </div>
              <MultiLineSparkline
                series={multiSeries}
                width={320}
                height={primaryChartHeight}
                responsive
                mode={sparkMode || 'axis'}
                xStartLabel={trendWindow ? `-${trendWindow}` : ''}
                xEndLabel="now"
                yMin={isNumber(multiSeriesYMin) ? multiSeriesYMin : 0}
                yMax={isNumber(multiSeriesYMax) ? multiSeriesYMax : 100}
                yTickCount={isNumber(multiSeriesYTickCount) ? Math.max(2, Math.round(multiSeriesYTickCount)) : 4}
              />
            </>
          ) : (
            <>
            {primaryChartLabel && (
              <Text style={{ color: COLORS.textMuted, fontSize: 11, display: 'block', marginBottom: 4 }}>{primaryChartLabel}</Text>
            )}
            <Sparkline
              data={series}
              width={320}
              height={primaryChartHeight}
              stroke={accent}
              responsive
              mode={sparkMode}
              xStartLabel={trendWindow ? `-${trendWindow}` : ''}
              xEndLabel="now"
              yMin={0}
              yMax={100}
              yTickCount={5}
            />
            </>
          )}
          {secondaryChart && secondaryChartPosition !== 'top' && (
            <div style={{ marginTop: secondaryChartGap }}>{secondaryChart}</div>
          )}
        </div>
      </div>
      <div className={`perf-detail-grid ${compactDetails ? 'perf-detail-grid--compact' : ''}`} style={{ marginTop: detailTopMargin }}>
        {details.map((item) =>
          item.divider ? (
            <div
              key={`${title}-divider-${item.label}`}
              style={{
                gridColumn: '1 / -1',
                borderTop: `1px solid ${COLORS.border}`,
                marginTop: 4,
                marginBottom: 2,
                display: 'flex',
                alignItems: 'center',
                gap: 6,
              }}
            >
              <Text style={{ color: COLORS.textMuted, fontSize: 11, fontWeight: 600, paddingTop: 4 }}>
                {item.label}
              </Text>
            </div>
          ) : (
            <DetailItem key={`${title}-${item.label}`} label={item.label} value={item.value} source={item.source} />
          )
        )}
      </div>
    </Card>
  )
}

type SummaryCardStatus = 'ok' | 'warning' | 'critical' | 'offline'

function getNetworkSummaryState(
  pressure: NetworkInterfacePressure | null | undefined,
  utilization: number | null,
): { status: SummaryCardStatus; color: string; reason?: string } {
  const directions = [pressure?.rx, pressure?.tx]
  const critical = directions.find((direction) => direction?.level?.toUpperCase() === 'CRITICAL')
  if (critical) {
    return { status: 'critical', color: COLORS.red, reason: critical.reason || 'Network pressure critical' }
  }
  const warning = directions.find((direction) => ['WARNING', 'HIGH'].includes(direction?.level?.toUpperCase() || ''))
  if (warning) {
    return { status: 'warning', color: COLORS.orange, reason: warning.reason || 'Network pressure elevated' }
  }
  if (isNumber(utilization) && utilization >= BUSY_UTIL_THRESHOLD) {
    return { status: 'warning', color: COLORS.orange, reason: `High load: ${utilization.toFixed(1)}%` }
  }
  if (!isNumber(utilization)) return { status: 'offline', color: COLORS.textMuted }
  return { status: 'ok', color: COLORS.green }
}

function formatGpuSummaryName(device: GpuDeviceView): string {
  const bdf = device.pci.match(/(?:[0-9a-f]{4}:)?([0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])/i)?.[1]
    || device.name.match(/^([0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])/i)?.[1]
  const productName = device.label === 'iGPU'
    ? 'Intel Graphics'
    : device.name
        .replace(/^[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]\s+/i, '')
        .replace(/^(?:VGA compatible|Display) controller(?:\s+\[[^\]]+\])?:\s*/i, '')
        .replace(/\s*\[[0-9a-f]{4}:[0-9a-f]{4}\].*$/i, '')
        .replace(/^Intel Corporation\s+/i, 'Intel ')
        .trim()

  const friendlyName = productName && !/^Intel Device$/i.test(productName) ? productName : 'Intel Graphics'
  return bdf ? `${friendlyName} (${bdf})` : friendlyName
}

function ResourceSummaryCard({
  title,
  subtitle,
  accent,
  status,
  statusColor,
  primaryLabel,
  primaryValue,
  primaryUnit,
  metrics,
  series,
  trendWindow,
  statusReason,
  onOpen,
}: {
  title: string
  subtitle?: string
  accent: string
  status: SummaryCardStatus
  statusColor: string
  primaryLabel: string
  primaryValue: number | null
  primaryUnit: string
  metrics: Array<{ label: string; value: string }>
  series: Array<number | null>
  trendWindow: '1m' | '5m'
  statusReason?: string
  onOpen?: () => void
}) {
  const utilizationPercent = isNumber(primaryValue) ? Math.max(0, Math.min(primaryValue, 100)) : 0

  return (
    <Card
      className={`perf-card perf-rise perf-gpu-summary-card perf-gpu-summary-card--${status}${onOpen ? ' perf-summary-card--interactive' : ''}`}
      bodyStyle={{ padding: 10 }}
      role={onOpen ? 'button' : undefined}
      tabIndex={onOpen ? 0 : undefined}
      aria-label={onOpen ? `Open ${title} details` : undefined}
      onClick={onOpen}
      onKeyDown={onOpen ? (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onOpen()
        }
      } : undefined}
    >
      <div className="perf-gpu-summary-head">
        <div className="perf-gpu-summary-identity">
          <AntTooltip title={statusReason || (status === 'offline' ? 'Telemetry unavailable' : 'Operating normally')}>
            <span className="perf-gpu-summary-status-dot" style={{ background: statusColor, color: statusColor }} />
          </AntTooltip>
          <div>
            <div className="perf-gpu-summary-title-row">
              <Text className="perf-gpu-summary-title">{title}</Text>
              {statusReason && status !== 'ok' && (
                <Text className={`perf-gpu-summary-reason perf-gpu-summary-reason--${status}`} title={statusReason}>
                  {statusReason}
                </Text>
              )}
            </div>
            {subtitle && <Text className="perf-gpu-summary-name" title={subtitle}>{subtitle}</Text>}
          </div>
        </div>
      </div>

      <div className="perf-gpu-summary-main">
        <div className="perf-gpu-summary-metric">
          <Text className="perf-gpu-summary-metric-label">{primaryLabel}</Text>
          <div className="perf-gpu-summary-value">
            {primaryValue === null ? '—' : primaryValue.toFixed(1)}<span>{primaryUnit}</span>
          </div>
          <div className="perf-gpu-summary-util-track" aria-label={`${primaryLabel}: ${primaryValue ?? 'unavailable'}${primaryUnit}`}>
            <div className="perf-gpu-summary-util-fill" style={{ width: `${utilizationPercent}%` }} />
          </div>
        </div>
      </div>

      <div className="perf-gpu-summary-meta">
        {metrics.map((metric) => (
          <span key={metric.label}><Text>{metric.label}</Text><strong title={metric.value}>{metric.value}</strong></span>
        ))}
      </div>
    </Card>
  )
}

function GpuSummaryCard({
  device,
  accent,
  utilizationSeries,
  trendWindow,
  onOpen,
}: {
  device: GpuDeviceView
  accent: string
  utilizationSeries: Array<number | null>
  trendWindow: '1m' | '5m'
  onOpen: () => void
}) {
  const utilization = isNumber(device.utilization) ? device.utilization : null
  const memoryUsage = isNumber(device.vramUsage) ? device.vramUsage : null
  const frequencyValues = [device.frequencies.gt0?.act_mhz, device.frequencies.gt1?.act_mhz].filter(isNumber)
  const frequencyMhz = frequencyValues.length ? Math.max(...frequencyValues) : null

  return (
    <ResourceSummaryCard
      title={device.displayLabel}
      subtitle={formatGpuSummaryName(device)}
      accent={accent}
      status={device.status === 'Throttle' ? 'critical' : device.status === 'Busy' ? 'warning' : device.status.toLowerCase() as SummaryCardStatus}
      statusColor={device.statusColor}
      primaryLabel="Util"
      primaryValue={utilization}
      primaryUnit="%"
      metrics={[
        { label: 'Power', value: formatMetric(device.powerGpu, 'W', 2) },
        { label: device.label === 'iGPU' ? 'System Memory' : 'VRAM Usage', value: memoryUsage === null ? 'N/A' : `${memoryUsage.toFixed(1)}%` },
        { label: 'Frequency', value: formatMetric(frequencyMhz, 'MHz', 0) },
      ]}
      series={utilizationSeries}
      trendWindow={trendWindow}
      statusReason={device.status === 'Throttle'
        ? 'Throttling detected'
        : device.status === 'Busy' && utilization !== null
          ? `High load: ${utilization.toFixed(1)}%`
          : undefined}
      onOpen={onOpen}
    />
  )
}

function CoreCell({
  index,
  usage,
  freq,
  temp,
  type,
  trend,
  freqTrend,
  trendWindow,
  utilAxis,
  freqAxis,
}: {
  index: number
  usage: number | null
  freq: number | null
  temp: number | null
  type: 'P' | 'E' | 'LPE' | 'Core'
  trend: Array<number | null>
  freqTrend: Array<number | null>
  trendWindow: '1m' | '5m'
  utilAxis: { min: number; max: number }
  freqAxis: { min: number; max: number }
}) {
  const normalized = isNumber(usage) ? Math.max(0, Math.min(usage, 100)) : 0
  const barColor = normalized >= 80 ? COLORS.red : normalized >= 60 ? COLORS.orange : COLORS.accent
  const freqColor = PERF_COLORS.memory
  const [showUtil, setShowUtil] = useState(true)
  const [showFreq, setShowFreq] = useState(true)

  return (
    <div className="perf-core-item">
      <div className="perf-core-head">
        <Text style={{ color: COLORS.text, fontSize: 12 }}>Core {index}</Text>
        <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>{formatPercent(usage)}</Text>
      </div>
      <div className="perf-core-bar-track">
        <div className="perf-core-bar-fill" style={{ width: `${normalized}%`, background: barColor }} />
      </div>
      <div className="perf-core-meta">
        <Text style={{ color: COLORS.textMuted, fontSize: 10 }}>{type}</Text>
        <Text style={{ color: COLORS.textMuted, fontSize: 10 }}>{formatMetric(freq, 'MHz', 0)}</Text>
        {isNumber(temp) && <Text style={{ color: temp >= 65 ? '#ff9f1c' : '#ffffff', fontSize: 10 }}>{temp.toFixed(0)}°C</Text>}
      </div>

      <div className="perf-core-trend-legend">
        <span
          className="perf-core-legend-item"
          style={{ cursor: 'pointer', opacity: showUtil ? 1 : 0.35, userSelect: 'none' }}
          onClick={() => setShowUtil((v) => !v)}
        >
          <span className="perf-core-legend-dot" style={{ background: showUtil ? barColor : 'rgba(120,176,255,0.2)' }} />
          <span style={{ textDecoration: showUtil ? 'none' : 'line-through' }}>Util %</span>
        </span>
        <span
          className="perf-core-legend-item"
          style={{ cursor: 'pointer', opacity: showFreq ? 1 : 0.35, userSelect: 'none' }}
          onClick={() => setShowFreq((v) => !v)}
        >
          <span className="perf-core-legend-dot" style={{ background: showFreq ? freqColor : 'rgba(120,176,255,0.2)' }} />
          <span style={{ textDecoration: showFreq ? 'none' : 'line-through' }}>Freq MHz</span>
        </span>
      </div>

      <div className="perf-core-trend-row">
        <Text className="perf-core-trend-title">util / freq</Text>
        <DualAxisSparkline
          utilData={trend}
          freqData={freqTrend}
          width={220}
          height={58}
          utilStroke={barColor}
          freqStroke={freqColor}
          responsive
          xStartLabel={`-${trendWindow}`}
          xEndLabel="now"
          utilAxis={utilAxis}
          freqAxis={freqAxis}
          utilTickCount={5}
          freqTickCount={4}
          showUtil={showUtil}
          showFreq={showFreq}
        />
      </div>
    </div>
  )
}

function ChartLegend({
  items,
  hidden,
  onToggle,
}: {
  items: Array<{ key: string; name: string; color: string; dasharray?: string }>
  hidden: Set<string>
  onToggle: (key: string) => void
}) {
  return (
    <div className="perf-chart-legend">
      {items.map((item) => {
        const isHidden = hidden.has(item.key)
        return (
          <span
            key={item.key}
            onClick={() => onToggle(item.key)}
            className={`perf-chart-legend-item ${isHidden ? 'perf-chart-legend-item--hidden' : ''}`}
          >
            <svg width={20} height={6} style={{ verticalAlign: 'middle' }}>
              <line x1={0} y1={3} x2={20} y2={3} stroke={isHidden ? 'rgba(120,176,255,0.2)' : item.color} strokeWidth={2.5} strokeDasharray={item.dasharray} />
            </svg>
            <span className="perf-chart-legend-label">{item.name}</span>
          </span>
        )
      })}
    </div>
  )
}

function NpuDetailCard({
  npuParsed,
  npuName,
  npuFreqMinMhz,
  npuFreqMaxMhz,
  getSeries,
  trendWindow,
}: {
  npuParsed: Record<string, unknown> | null
  npuName: string
  npuFreqMinMhz: number | null
  npuFreqMaxMhz: number | null
  getSeries: (key: string) => Array<number | null>
  trendWindow: '1m' | '5m'
}) {
  const util = typeof npuParsed?.utilization_percent === 'number' ? normalizePercent(npuParsed.utilization_percent) : null
  const powerW = typeof npuParsed?.power_w === 'number' ? npuParsed.power_w : null
  const freqMhz = typeof npuParsed?.frequency_mhz === 'number' ? npuParsed.frequency_mhz : null
  const tempC = typeof npuParsed?.temperature_c === 'number' ? npuParsed.temperature_c : null
  const bandwidthMib = typeof npuParsed?.noc_bandwidth_mib_per_s === 'number' ? npuParsed.noc_bandwidth_mib_per_s : null
  const memoryMb = typeof npuParsed?.memory_bytes === 'number' ? (npuParsed.memory_bytes as number) / (1024 * 1024) : null
  const tileConfig = npuParsed?.tile_config != null ? `${npuParsed.tile_config}` : 'N/A'
  const pmtAvailable = npuParsed?.pmt_available !== false

  const utilSeries = getSeries('npu:util')
  const freqSeries = getSeries('npu:freq_mhz')
  const powerSeries = getSeries('npu:power_w')
  const ddrBwSeries = getSeries('npu:bandwidth_mib')
  const tempSeries = getSeries('npu:temp_c')
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const toggle = useCallback((key: string) => setHidden((prev) => {
    const next = new Set(prev); next.has(key) ? next.delete(key) : next.add(key); return next
  }), [])

  const numStyle: React.CSSProperties = { color: COLORS.text, fontSize: 11, fontWeight: 600 }
  const labelStyle: React.CSSProperties = { color: COLORS.textMuted, fontSize: 10 }

  // Chart 1: Utilization % + Freq MHz (matching History layout)
  const ufItems = [
    { key: 'npuUtil', name: 'NPU Utilization %', color: COLORS.accent },
    { key: 'freqMhz', name: 'NPU Freq MHz', color: PERF_COLORS.npu },
  ]

  const ufMaxLen = Math.max(utilSeries.length, freqSeries.length)
  const padUf = (arr: Array<number | null>) => {
    const diff = ufMaxLen - arr.length
    return diff > 0 ? [...Array(diff).fill(null), ...arr] : arr.slice(-ufMaxLen)
  }
  const utilFreqData = ufMaxLen > 0 ? Array.from({ length: ufMaxLen }, (_, i) => ({
    i,
    npuUtil: padUf(utilSeries)[i] ?? null,
    freqMhz: padUf(freqSeries)[i] ?? null,
  })) : []

  // Chart 2: Power W + DDR BW MiB/s + Temperature °C
  const pbItems = [
    { key: 'npuPower', name: 'NPU Power W', color: '#4cc9f0' },
    { key: 'ddrBw', name: 'DDR BW MiB/s', color: '#cbd5e1', dasharray: '5 3' },
    { key: 'npuTemp', name: 'Temperature °C', color: '#fbbf24' },
  ]
  const pbMaxLen = Math.max(powerSeries.length, ddrBwSeries.length, tempSeries.length)
  const padPb = (arr: Array<number | null>) => {
    const diff = pbMaxLen - arr.length
    return diff > 0 ? [...Array(diff).fill(null), ...arr] : arr.slice(-pbMaxLen)
  }
  const powerBwData = pbMaxLen > 0 ? Array.from({ length: pbMaxLen }, (_, i) => ({
    i,
    npuPower: padPb(powerSeries)[i] ?? null,
    ddrBw: padPb(ddrBwSeries)[i] ?? null,
    npuTemp: padPb(tempSeries)[i] ?? null,
  })) : []

  return (
    <Card className="perf-card perf-rise" bodyStyle={{ padding: 16 }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 8 }}>
        <div>
          <Text style={{ color: COLORS.text, fontSize: 13, fontWeight: 600 }}>NPU</Text>
          <Text style={{ color: COLORS.textMuted, fontSize: 10, display: 'block', marginTop: 2 }}>
            {npuName.replace(/\s*\[[0-9a-f]{4}:[0-9a-f]{4}\]\s*$/i, '').trim() || 'Intel NPU'}
          </Text>
          {isNumber(npuFreqMaxMhz) && (
            <Text style={{ color: COLORS.textMuted, fontSize: 10, display: 'block', marginTop: 2 }}>
              Max Freq: {Math.round(npuFreqMaxMhz)} MHz
            </Text>
          )}
        </div>
        <div style={{ textAlign: 'right' }}>
          <Text style={{ color: COLORS.textMuted, fontSize: 10, display: 'block' }}>Util</Text>
          <Text style={{ color: PERF_COLORS.npu, fontSize: 22, fontWeight: 700 }}>
            {isNumber(util) ? util.toFixed(1) : '—'}
            <span style={{ color: COLORS.textMuted, fontSize: 12, marginLeft: 4 }}>%</span>
          </Text>
        </div>
      </div>

      {/* Utilization trend sparkline */}
      <div style={{ position: 'relative', marginBottom: 10 }}>
        <Text style={{ color: COLORS.textMuted, fontSize: 10, position: 'absolute', top: 2, left: 2, zIndex: 1 }}>Util %</Text>
        <Sparkline
          data={utilSeries}
          width={640}
          height={60}
          stroke={PERF_COLORS.npu}
          responsive
          mode="axis"
          xStartLabel={`-${trendWindow}`}
          xEndLabel="now"
          yMin={0}
          yMax={100}
          yTickCount={5}
        />
      </div>

      {/* Numbers row (GPU-style: label + color dot + value, flowing inline) */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '3px 16px', alignItems: 'center', marginBottom: 8 }}>
        {(() => {
          const items: Array<{ key: string; label: string; value: string; color: string }> = []
          items.push({ key: 'util', label: 'Util', value: isNumber(util) ? `${util.toFixed(1)} %` : 'N/A', color: COLORS.accent })
          items.push({ key: 'freq', label: 'Freq', value: isNumber(freqMhz) ? `${freqMhz.toFixed(0)} MHz` : 'N/A', color: PERF_COLORS.npu })
          items.push({ key: 'mem', label: 'Memory Used', value: isNumber(memoryMb) ? `${memoryMb.toFixed(2)} MB` : 'N/A', color: COLORS.text })
          if (pmtAvailable) {
            items.push({ key: 'power', label: 'Power', value: isNumber(powerW) ? `${powerW.toFixed(2)} W` : 'N/A', color: '#4cc9f0' })
            items.push({ key: 'bw', label: 'DDR BW', value: isNumber(bandwidthMib) ? `${(bandwidthMib / 1024).toFixed(2)} GB/s` : 'N/A', color: '#cbd5e1' })
            items.push({ key: 'temp', label: 'Temp', value: isNumber(tempC) ? `${tempC.toFixed(0)} °C` : 'N/A', color: '#fbbf24' })
            items.push({ key: 'tile', label: 'Tile Conf', value: tileConfig, color: COLORS.textMuted })
          }
          return items.map((it) => (
            <span key={it.key} style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}>
              <span style={{ width: 6, height: 6, borderRadius: '50%', background: it.color, flexShrink: 0, display: 'inline-block' }} />
              <Text style={labelStyle}>{it.label}</Text>
              <Text style={{ ...numStyle, color: it.color }}>{it.value}</Text>
            </span>
          ))
        })()}
      </div>

      {/* Chart 1: Utilization % (left Y) + Freq MHz (right Y) — matching History */}
      {utilFreqData.length > 0 && (
        <>
          <ChartLegend items={ufItems} hidden={hidden} onToggle={toggle} />
          <div style={{ width: '100%', height: 120 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={utilFreqData} margin={{ top: 2, right: 44, left: 0, bottom: 16 }}>
                <CartesianGrid stroke={`${COLORS.border}55`} strokeDasharray="3 3" />
                <XAxis dataKey="i" ticks={[0, utilFreqData.length - 1]} tick={{ fill: COLORS.textMuted, fontSize: 10 }} tickFormatter={(val: number) => val === 0 ? `-${trendWindow}` : 'now'} />
                <YAxis yAxisId="util" domain={[0, 100]} tick={{ fill: COLORS.textMuted, fontSize: 10 }} tickFormatter={(v) => `${v}%`} width={44}
                  label={{ value: '%', angle: -90, position: 'insideLeft', fill: COLORS.textMuted, fontSize: 10, offset: 14 }} />
                <YAxis yAxisId="freq" orientation="right" domain={[0, (max: number) => Math.max(max, 1)]} tick={{ fill: COLORS.textMuted, fontSize: 10 }} width={50}
                  tickFormatter={(v: number) => `${Math.round(v)}`}
                  label={{ value: 'MHz', angle: 90, position: 'insideRight', fill: COLORS.textMuted, fontSize: 10, offset: 10 }} />
                <Tooltip
                  contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text, fontSize: 11 }}
                  formatter={(val: unknown, name: string) => {
                    const n = typeof val === 'number' ? val : null
                    if (n === null) return ['—', name]
                    return name.endsWith('MHz') ? [`${n.toFixed(0)} MHz`, name] : [`${n.toFixed(1)}%`, name]
                  }}
                  labelFormatter={() => ''}
                />
                <Line yAxisId="util" type="monotone" dataKey="npuUtil" name="NPU Utilization %"
                  stroke={COLORS.accent} dot={false} strokeWidth={2} isAnimationActive={false} connectNulls
                  hide={hidden.has('npuUtil')} />
                <Line yAxisId="freq" type="monotone" dataKey="freqMhz" name="NPU Freq MHz"
                  stroke={PERF_COLORS.npu} dot={false} strokeWidth={2} isAnimationActive={false} connectNulls
                  hide={hidden.has('freqMhz')} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </>
      )}

      {/* Chart 2: Power W + Temperature °C (left Y) + DDR BW MiB/s (right Y) — matching History */}
      {pmtAvailable && powerBwData.length > 0 && (
        <>
          <ChartLegend items={pbItems} hidden={hidden} onToggle={toggle} />
          <div style={{ width: '100%', height: 120 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={powerBwData} margin={{ top: 2, right: 70, left: 0, bottom: 16 }}>
                <CartesianGrid stroke={`${COLORS.border}55`} strokeDasharray="3 3" />
                <XAxis dataKey="i" ticks={[0, powerBwData.length - 1]} tick={{ fill: COLORS.textMuted, fontSize: 10 }} tickFormatter={(val: number) => val === 0 ? `-${trendWindow}` : 'now'} />
                <YAxis yAxisId="left" domain={[0, (max: number) => Math.max(max, 1)]} tick={{ fill: COLORS.textMuted, fontSize: 10 }} tickFormatter={(v) => `${Number(v).toFixed(0)}`} width={50}
                  label={{ value: 'W / °C', angle: -90, position: 'insideLeft', fill: COLORS.textMuted, fontSize: 10, offset: 14 }} />
                <YAxis yAxisId="bw" orientation="right" domain={[0, (max: number) => Math.max(max, 1)]} tick={{ fill: COLORS.textMuted, fontSize: 10 }}
                  tickFormatter={(v) => {
                    const n = Number(v)
                    if (!Number.isFinite(n)) return String(v)
                    if (n >= 1000) return `${(n / 1024).toFixed(1)}G`
                    return n.toFixed(0)
                  }}
                  width={58}
                  label={{ value: 'MiB/s', angle: 90, position: 'insideRight', fill: COLORS.textMuted, fontSize: 10, offset: 10 }} />
                <Tooltip
                  contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text, fontSize: 11 }}
                  formatter={(val: unknown, name: string) => {
                    const n = typeof val === 'number' ? val : null
                    if (n === null) return ['—', name]
                    if (name.includes('BW')) return [`${n.toFixed(2)} MiB/s`, name]
                    if (name.includes('Temperature')) return [`${n.toFixed(1)} °C`, name]
                    return [`${n.toFixed(2)} W`, name]
                  }}
                  labelFormatter={() => ''}
                />
                <Line yAxisId="left" type="monotone" dataKey="npuPower" name="NPU Power W"
                  stroke={'#4cc9f0'} dot={false} strokeWidth={2} isAnimationActive={false} connectNulls
                  hide={hidden.has('npuPower')} />
                <Line yAxisId="bw" type="monotone" dataKey="ddrBw" name="DDR BW MiB/s"
                  stroke={'#cbd5e1'} strokeDasharray="5 3" dot={false} strokeWidth={2} isAnimationActive={false} connectNulls
                  hide={hidden.has('ddrBw')} />
                <Line yAxisId="left" type="monotone" dataKey="npuTemp" name="Temperature °C"
                  stroke={'#fbbf24'} dot={false} strokeWidth={2} isAnimationActive={false} connectNulls
                  hide={hidden.has('npuTemp')} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </>
      )}

      {!npuParsed && (
        <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>No NPU telemetry available</Text>
      )}
    </Card>
  )
}

function GpuDeviceCard({
  device,
  trendWindow,
  getSeries,
  sparkMode,
}: {
  device: GpuDeviceView
  trendWindow: '1m' | '5m'
  getSeries: (key: string) => Array<number | null>
  sparkMode: SparkMode
}) {
  const utilSeries = getSeries(`gpu:${device.id}:util`)
  const gt0FreqSeries = getSeries(`gpu:${device.id}:freq:gt0:cur_mhz`)
  const gt1FreqSeries = getSeries(`gpu:${device.id}:freq:gt1:cur_mhz`)
  const gpuPowerSeries = getSeries(`gpu:${device.id}:power:gpu_cur_power`)
  const pkgPowerSeries = getSeries(`gpu:${device.id}:power:pkg_cur_power`)
  const gt0ActSeries = getSeries(`gpu:${device.id}:freq:gt0:act_mhz`)
  const gt0ReqSeries = getSeries(`gpu:${device.id}:freq:gt0:req_mhz`)
  const gt1ActSeries = getSeries(`gpu:${device.id}:freq:gt1:act_mhz`)
  const gt1ReqSeries = getSeries(`gpu:${device.id}:freq:gt1:req_mhz`)
  const gt0Rc6Series = getSeries(`gpu:${device.id}:freq:gt0:rc6_pct`)
  const gt1Rc6Series = getSeries(`gpu:${device.id}:freq:gt1:rc6_pct`)
  const detailColors = getGpuChartColors()
  const engineColors: Record<EngineKey, string> = {
    ccs: detailColors.ccs,
    rcs: detailColors.rcs,
    bcs: detailColors.bcs,
    vcs: detailColors.vcs,
    vecs: detailColors.vecs,
  }
  const enginesToShow = device.engines.length
    ? device.engines
    : ENGINE_ORDER.filter((engine) => isNumber(device.engineUtil[engine]))

  const gtFreqItems = [
    { key: `${device.id}-freq-gt0-act`, label: 'GT0 Act', value: device.frequencies.gt0?.act_mhz ?? null, stroke: detailColors.gt0 },
    { key: `${device.id}-freq-gt0-req`, label: 'GT0 Req', value: device.frequencies.gt0?.cur_mhz ?? null, stroke: detailColors.gt0 },
    { key: `${device.id}-freq-gt1-act`, label: 'GT1 Act', value: device.frequencies.gt1?.act_mhz ?? null, stroke: detailColors.gt1 },
    { key: `${device.id}-freq-gt1-req`, label: 'GT1 Req', value: device.frequencies.gt1?.cur_mhz ?? null, stroke: detailColors.gt1 },
  ].filter((s) => isNumber(s.value))

  const pkgLabel = device.label === 'iGPU' ? 'Pkg' : 'Card'
  const powerSeries = [
    { key: `${device.id}-power-gpu`, label: 'GPU', data: gpuPowerSeries, stroke: detailColors.gpuPower },
    { key: `${device.id}-power-pkg`, label: pkgLabel, data: pkgPowerSeries, stroke: detailColors.packagePower },
  ]

  const engineSeries = enginesToShow.map((engine) => {
    const cnt = device.engineInstances.filter((n) => normalizeEngineName(n) === engine).length
    return {
      key: `${device.id}-engine-${engine}`,
      label: cnt > 1 ? `${engine.toUpperCase()}\u00d7${cnt}` : engine.toUpperCase(),
      data: getSeries(`gpu:${device.id}:engine:${engine}`),
      stroke: engineColors[engine as EngineKey] ?? COLORS.accent,
    }
  })

  const [hidden, setHidden] = useState<Set<string>>(() => {
    const ranked = [...enginesToShow].sort(
      (a, b) => (device.engineUtil[b] ?? -1) - (device.engineUtil[a] ?? -1),
    )
    const visible = new Set<EngineKey>(['ccs', 'bcs', 'rcs'])
    ranked.slice(0, 2).forEach((engine) => visible.add(engine))
    return new Set(enginesToShow
      .filter((engine) => !visible.has(engine))
      .map((engine) => `${device.id}-engine-${engine}`))
  })
  const toggle = useCallback((key: string) => setHidden((prev) => {
    const next = new Set(prev); next.has(key) ? next.delete(key) : next.add(key); return next
  }), [])

  // Use static bounds first; fall back to dynamic gpu_usage min/max freq when static is missing
  const gt0Min = device.gtFreqBounds.gt0?.min_mhz ?? device.frequencies.gt0?.min_mhz ?? null
  const gt0Max = device.gtFreqBounds.gt0?.max_mhz ?? device.frequencies.gt0?.max_mhz ?? null
  const gt1Min = device.gtFreqBounds.gt1?.min_mhz ?? device.frequencies.gt1?.min_mhz ?? null
  const gt1Max = device.gtFreqBounds.gt1?.max_mhz ?? device.frequencies.gt1?.max_mhz ?? null
  const gt0RangeText = (isNumber(gt0Min) || isNumber(gt0Max)) ? formatFreqRange(gt0Min, gt0Max) : null
  const gt1RangeText = (isNumber(gt1Min) || isNumber(gt1Max)) ? formatFreqRange(gt1Min, gt1Max) : null

  return (
    <Card className="perf-card perf-rise" bodyStyle={{ padding: 16 }}>
      <div className="perf-gpu-head">
        <div>
          <Space size={8}>
            <PartitionOutlined style={{ color: PERF_COLORS.gpu }} />
            <Text style={{ color: COLORS.text, fontSize: 14, fontWeight: 600 }}>{device.displayLabel}</Text>
          </Space>
          <Text style={{ color: COLORS.textMuted, fontSize: 11, display: 'block', marginTop: 4 }}>
            {device.name}
          </Text>
          <Text style={{ color: COLORS.textMuted, fontSize: 11, display: 'block' }}>
            PCI: {device.pci} | Driver: {device.driver} | Type: {device.devType}
          </Text>
        </div>
        <div className="perf-gpu-util">
          <Text style={{ color: COLORS.textMuted, fontSize: 10, textAlign: 'right', display: 'block' }}>Util</Text>
          <Text style={{ color: COLORS.text, fontSize: 22, fontWeight: 700 }}>
            {formatNumber(device.utilization)}
            <span style={{ color: COLORS.textMuted, fontSize: 12, marginLeft: 6 }}>%</span>
          </Text>
          {null}
        </div>
      </div>

      {/* Static info line */}
      <Text style={{ color: COLORS.textMuted, fontSize: 10, display: 'block', marginTop: 6 }}>
        {[
          gt0RangeText ? `GT0 (Compute) ${gt0RangeText}` : null,
          gt1RangeText ? `GT1 (Media) ${gt1RangeText}` : null,
          !gt0RangeText && !gt1RangeText && (device.freqBounds.min_mhz || device.freqBounds.max_mhz)
            ? `Freq ${formatFreqRange(device.freqBounds.min_mhz, device.freqBounds.max_mhz)}`
            : null,
          device.label === 'iGPU' && isNumber(device.euCount) ? `EU ${device.euCount}` : null,
        ].filter(Boolean).join(' | ') || 'No static info'}
      </Text>

      {/* Utilization trend */}
      <div className="perf-gpu-chart" style={{ marginTop: 8, position: 'relative' }}>
        <Text style={{ color: COLORS.textMuted, fontSize: 10, position: 'absolute', top: 2, left: 2, zIndex: 1 }}>Util %</Text>
        <Sparkline
          data={utilSeries}
          width={640}
          height={60}
          stroke={PERF_COLORS.gpu}
          responsive
          mode="axis"
          xStartLabel={`-${trendWindow}`}
          xEndLabel="now"
          yMin={0}
          yMax={100}
          yTickCount={5}
        />
      </div>

      {/* Combined dynamic data: numbers + recharts trend charts */}
      <div style={{ marginTop: 10, paddingTop: 8, borderTop: '1px dashed rgba(120,176,255,0.16)' }}>
        {/* Numbers row */}
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '3px 16px', alignItems: 'center', marginBottom: 8 }}>
          {/* Freq */}
          <Text style={{ color: COLORS.textMuted, fontSize: 10, flexShrink: 0 }}>Freq</Text>
          {gtFreqItems.map((s) => (
            <span key={s.key} style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}>
              <span style={{ width: 6, height: 6, borderRadius: '50%', background: s.stroke, flexShrink: 0, display: 'inline-block' }} />
              <Text style={{ color: COLORS.textMuted, fontSize: 10 }}>{s.label}</Text>
              <Text style={{ color: s.stroke, fontSize: 11, fontWeight: 600 }}>
                {formatMetric(s.value, 'MHz', 0)}
              </Text>
            </span>
          ))}
          {/* RC6 */}
          {(isNumber(device.frequencies.gt0?.rc6_pct) || isNumber(device.frequencies.gt1?.rc6_pct)) && (
            <>
              <Text style={{ color: COLORS.textMuted, fontSize: 10, flexShrink: 0, marginLeft: 4 }}>RC6</Text>
              {isNumber(device.frequencies.gt0?.rc6_pct) && (
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}>
                  <Text style={{ color: COLORS.textMuted, fontSize: 10 }}>GT0</Text>
                  <Text style={{ color: COLORS.text, fontSize: 11, fontWeight: 600 }}>
                    {formatPercent(device.frequencies.gt0?.rc6_pct ?? null)}
                  </Text>
                </span>
              )}
              {isNumber(device.frequencies.gt1?.rc6_pct) && (
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}>
                  <Text style={{ color: COLORS.textMuted, fontSize: 10 }}>GT1</Text>
                  <Text style={{ color: COLORS.text, fontSize: 11, fontWeight: 600 }}>
                    {formatPercent(device.frequencies.gt1?.rc6_pct ?? null)}
                  </Text>
                </span>
              )}
            </>
          )}
          {/* Mem */}
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}>
            <Text style={{ color: COLORS.textMuted, fontSize: 10 }}>
              {device.label === 'iGPU' ? 'Sys Mem' : 'VRAM'}
            </Text>
            <Text style={{ color: COLORS.text, fontSize: 11, fontWeight: 600 }}>
              {formatPercent(device.vramUsage)}
            </Text>
          </span>
          {/* Engine */}
          <Text style={{ color: COLORS.textMuted, fontSize: 10, flexShrink: 0, marginLeft: 4 }}>Engine</Text>
          {enginesToShow.length === 0 ? (
            <Text style={{ color: COLORS.textMuted, fontSize: 10 }}>—</Text>
          ) : (
            enginesToShow.map((engine) => {
              const instanceCount = device.engineInstances.filter((n) => normalizeEngineName(n) === engine).length
              return (
                <span key={`${device.id}-${engine}`} style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}>
                  <Text style={{ color: COLORS.textMuted, fontSize: 10, textTransform: 'uppercase' }}>
                    {engine}{instanceCount > 1 ? `\u00d7${instanceCount}` : ''}
                  </Text>
                  <Text style={{ color: engineColors[engine as EngineKey] ?? COLORS.accent, fontSize: 11, fontWeight: 600 }}>
                    {formatPercent(device.engineUtil[engine])}
                  </Text>
                </span>
              )
            })
          )}
          {/* Power */}
          <Text style={{ color: COLORS.textMuted, fontSize: 10, flexShrink: 0, marginLeft: 4 }}>Power</Text>
          {powerSeries.map((s) => (
            <span key={s.key} style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}>
              <span style={{ width: 6, height: 6, borderRadius: '50%', background: s.stroke, flexShrink: 0, display: 'inline-block' }} />
              <Text style={{ color: COLORS.textMuted, fontSize: 10 }}>{s.label}</Text>
              <Text style={{ color: s.stroke, fontSize: 11, fontWeight: 600 }}>
                {formatMetric(s.label === 'GPU' ? device.powerGpu : device.powerPkg, 'W', 2)}
              </Text>
            </span>
          ))}
        </div>

        {/* Chart 1: Engine % + Mem % (left Y) + Freq MHz (right Y) */}
        {(() => {
          const vramSeries = getSeries(`gpu:${device.id}:vram_usage`)
          const allSeriesData = [...engineSeries.map((s) => s.data), vramSeries, gt0ActSeries, gt0ReqSeries, gt1ActSeries, gt1ReqSeries, gt0Rc6Series, gt1Rc6Series]
          const maxLen = Math.max(0, ...allSeriesData.map((d) => d?.length ?? 0))
          if (maxLen === 0) return null

          const pad = (arr: Array<number | null>) => {
            const diff = maxLen - arr.length
            return diff > 0 ? [...Array(diff).fill(null), ...arr] : arr.slice(-maxLen)
          }
          const data = Array.from({ length: maxLen }, (_, i) => {
            const pt: Record<string, number | null> = { i }
            engineSeries.forEach((s) => { pt[s.key] = pad(s.data)[i] ?? null })
            pt['mem'] = pad(vramSeries)[i] ?? null
            pt['gt0Act'] = pad(gt0ActSeries)[i] ?? null
            pt['gt0Req'] = pad(gt0ReqSeries)[i] ?? null
            pt['gt1Act'] = pad(gt1ActSeries)[i] ?? null
            pt['gt1Req'] = pad(gt1ReqSeries)[i] ?? null
            pt['gt0Rc6'] = pad(gt0Rc6Series)[i] ?? null
            pt['gt1Rc6'] = pad(gt1Rc6Series)[i] ?? null
            return pt
          })
          const memLabel = device.label === 'iGPU' ? 'Sys Mem %' : 'VRAM %'

          const chart1Items = [
            ...engineSeries.map((s) => ({ key: s.key, name: `${s.label} %`, color: s.stroke })),
            { key: 'mem', name: memLabel, color: PERF_COLORS.memory, dasharray: '5 3' },
            { key: 'gt0Act', name: 'GT0 Act MHz', color: detailColors.gt0 },
            { key: 'gt0Req', name: 'GT0 Req MHz', color: detailColors.gt0, dasharray: '6 4' },
            { key: 'gt1Act', name: 'GT1 Act MHz', color: detailColors.gt1 },
            { key: 'gt1Req', name: 'GT1 Req MHz', color: detailColors.gt1, dasharray: '4 4' },
            { key: 'gt0Rc6', name: 'GT0 RC6 %', color: detailColors.gt0Rc6, dasharray: '3 2' },
            { key: 'gt1Rc6', name: 'GT1 RC6 %', color: detailColors.gt1Rc6, dasharray: '3 2' },
          ]

          return (
            <>
              <ChartLegend items={chart1Items} hidden={hidden} onToggle={toggle} />
              <div style={{ width: '100%', height: 160, marginBottom: 4 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={data} margin={{ top: 2, right: 4, left: 0, bottom: 16 }}>
                    <CartesianGrid stroke={`${COLORS.border}55`} strokeDasharray="3 3" />
                    <XAxis dataKey="i" ticks={[0, data.length - 1]} tick={{ fill: COLORS.textMuted, fontSize: 10 }} tickFormatter={(val: number) => val === 0 ? `-${trendWindow}` : 'now'} />
                    <YAxis yAxisId="pct" domain={[0, 100]} tick={{ fill: COLORS.textMuted, fontSize: 10 }} tickFormatter={(v) => `${v}%`} width={44}
                      label={{ value: '%', angle: -90, position: 'insideLeft', fill: COLORS.textMuted, fontSize: 10, offset: 14 }} />
                    <YAxis yAxisId="mhz" orientation="right" domain={[0, (max: number) => Math.max(max, 1)]} tick={{ fill: COLORS.textMuted, fontSize: 10 }} tickFormatter={(v: number) => `${Math.round(v)}`} width={56}
                      label={{ value: 'MHz', angle: 90, position: 'insideRight', fill: COLORS.textMuted, fontSize: 10, offset: 10 }} />
                    <Tooltip
                      contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text, fontSize: 11 }}
                      formatter={(val: unknown, name: string) => {
                        const n = typeof val === 'number' ? val : null
                        if (n === null) return ['—', name]
                        return name.endsWith('MHz') ? [`${n.toFixed(0)} MHz`, name] : [`${n.toFixed(1)}%`, name]
                      }}
                      labelFormatter={() => ''}
                    />
                    {engineSeries.map((s) => (
                      <Line key={s.key} yAxisId="pct" type="monotone" dataKey={s.key} name={`${s.label} %`}
                        stroke={s.stroke} dot={false} strokeWidth={2} isAnimationActive={false} connectNulls
                        hide={hidden.has(s.key)} />
                    ))}
                    <Line yAxisId="pct" type="monotone" dataKey="mem" name={memLabel}
                      stroke={PERF_COLORS.memory} strokeDasharray="5 3" dot={false} strokeWidth={2} isAnimationActive={false} connectNulls
                      hide={hidden.has('mem')} />
                    <Line yAxisId="mhz" type="monotone" dataKey="gt0Act" name="GT0 Act MHz"
                      stroke={detailColors.gt0} dot={false} strokeWidth={2} isAnimationActive={false} connectNulls
                      hide={hidden.has('gt0Act')} />
                    <Line yAxisId="mhz" type="monotone" dataKey="gt0Req" name="GT0 Req MHz"
                      stroke={detailColors.gt0} strokeDasharray="6 4" dot={false} strokeWidth={2} isAnimationActive={false} connectNulls
                      hide={hidden.has('gt0Req')} />
                    <Line yAxisId="mhz" type="monotone" dataKey="gt1Act" name="GT1 Act MHz"
                      stroke={detailColors.gt1} dot={false} strokeWidth={2} isAnimationActive={false} connectNulls
                      hide={hidden.has('gt1Act')} />
                    <Line yAxisId="mhz" type="monotone" dataKey="gt1Req" name="GT1 Req MHz"
                      stroke={detailColors.gt1} strokeDasharray="4 4" dot={false} strokeWidth={2} isAnimationActive={false} connectNulls
                      hide={hidden.has('gt1Req')} />
                    <Line yAxisId="pct" type="monotone" dataKey="gt0Rc6" name="GT0 RC6 %"
                      stroke={detailColors.gt0Rc6} strokeDasharray="3 2" dot={false} strokeWidth={2} isAnimationActive={false} connectNulls
                      hide={hidden.has('gt0Rc6')} />
                    <Line yAxisId="pct" type="monotone" dataKey="gt1Rc6" name="GT1 RC6 %"
                      stroke={detailColors.gt1Rc6} strokeDasharray="3 2" dot={false} strokeWidth={2} isAnimationActive={false} connectNulls
                      hide={hidden.has('gt1Rc6')} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </>
          )
        })()}

        {/* Chart 2: Power W */}
        {(() => {
          const maxLen = Math.max(gpuPowerSeries.length, pkgPowerSeries.length)
          if (maxLen === 0) return null
          const pad = (arr: Array<number | null>) => {
            const diff = maxLen - arr.length
            return diff > 0 ? [...Array(diff).fill(null), ...arr] : arr.slice(-maxLen)
          }
          const data = Array.from({ length: maxLen }, (_, i) => ({
            i,
            gpuPower: pad(gpuPowerSeries)[i] ?? null,
            pkgPower: pad(pkgPowerSeries)[i] ?? null,
          }))
          const chart2Items = [
            { key: 'gpuPower', name: 'GPU Power W', color: detailColors.gpuPower },
            { key: 'pkgPower', name: `${pkgLabel} Power W`, color: detailColors.packagePower, dasharray: '5 3' },
          ]
          return (
            <>
              <ChartLegend items={chart2Items} hidden={hidden} onToggle={toggle} />
              <div style={{ width: '100%', height: 100 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={data} margin={{ top: 2, right: 8, left: 0, bottom: 16 }}>
                    <CartesianGrid stroke={`${COLORS.border}55`} strokeDasharray="3 3" />
                    <XAxis dataKey="i" ticks={[0, data.length - 1]} tick={{ fill: COLORS.textMuted, fontSize: 10 }} tickFormatter={(val: number) => val === 0 ? `-${trendWindow}` : 'now'} />
                    <YAxis domain={[0, (max: number) => Math.ceil(Math.max(max, 1))]} tick={{ fill: COLORS.textMuted, fontSize: 10 }} tickFormatter={(v: number) => `${Number(v.toFixed(1))}W`} width={52}
                      label={{ value: 'W', angle: -90, position: 'insideLeft', fill: COLORS.textMuted, fontSize: 10, offset: 14 }} />
                    <Tooltip
                      contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text, fontSize: 11 }}
                      formatter={(val: unknown, name: string) => {
                        const n = typeof val === 'number' ? val : null
                        return n === null ? ['—', name] : [`${n.toFixed(2)} W`, name]
                      }}
                      labelFormatter={() => ''}
                    />
                    <Line type="monotone" dataKey="gpuPower" name="GPU Power W"
                      stroke={detailColors.gpuPower} dot={false} strokeWidth={2} isAnimationActive={false} connectNulls
                      hide={hidden.has('gpuPower')} />
                    <Line type="monotone" dataKey="pkgPower" name={`${pkgLabel} Power W`}
                      stroke={detailColors.packagePower} strokeDasharray="5 3" dot={false} strokeWidth={2} isAnimationActive={false} connectNulls
                      hide={hidden.has('pkgPower')} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </>
          )
        })()}
      </div>
    </Card>
  )
}

function SystemOverview({ active, onNavigate, onOpenSettings, onOpenDiagnostics, diagnosticsEnabled, balancerEnabled }: Props) {
  // Poll at the fast cadence only when this tab is selected AND the page is
  // actually visible; a backgrounded tab or minimized window drops to the
  // slower inactive cadence (browsers can't detect occlusion by another window,
  // so a covered-but-not-minimized page still reads as visible).
  const documentVisible = useDocumentVisible()
  const foreground = active && documentVisible
  const [staticInfo, setStaticInfo] = useState<StaticInfoData | null>(readCachedStaticInfo)
  const [pressureThresholds, setPressureThresholds] = useState<PressureThresholds | null>(null)
  const [topApps, setTopApps] = useState<AppResourceEntry[] | null>(null)
  const [diagSummary, setDiagSummary] = useState<DiagCockpitSummary | null>(null)
  const [diagSummaryStatus, setDiagSummaryStatus] = useState<'loading' | 'ready' | 'unavailable'>('loading')
  const [controlSummary, setControlSummary] = useState<ControlCockpitSummary | null>(null)
  const [dynamicInfo, setDynamicInfo] = useState<DynamicInfoData | null>(null)
  const [loadingDynamic, setLoadingDynamic] = useState(false)
  const [errorStatic, setErrorStatic] = useState<string | null>(null)
  const [errorDynamic, setErrorDynamic] = useState<string | null>(null)
  // Whether a measured benchmark run currently holds quiet mode.  Read off the
  // snapshot rather than fetched: the endpoint says so in every reply, so
  // knowing it costs no extra request -- which matters here, because the whole
  // point is to stop making them.
  const [quietActive, setQuietActive] = useState(false)

  const [refreshIntervalMs, setRefreshIntervalMs] = useState<number>(DEFAULT_REFRESH_INTERVAL_MS)
  const [trendWindow, setTrendWindow] = useState<'1m' | '5m'>('1m')
  const [deviceDetail, setDeviceDetail] = useState<DeviceDetailSelection | null>(null)
  const sparkMode: SparkMode = 'axis'
  const [trends, setTrends] = useState<TrendSeries>({})
  const { publishNotice } = useGlobalConfigNotices()
  const monitoredSectionsUpdatedAt = dynamicInfo?.monitored_sections_updated_at ?? null
  const {
    sections: monitoredSectionList,
    sectionSet: monitoredSections,
    lastChange: monitoredSectionsChange,
    clearLastChange: clearMonitoredSectionsChange,
  } =
    useMonitoredSections(active, [...DEFAULT_MONITORED_DYNAMIC_SECTIONS], monitoredSectionsUpdatedAt)
  // Fail-open: gate on the resolved section set, which starts from the full
  // fallback and only narrows once the config endpoint responds.  If that
  // endpoint is unavailable (older backend, transient error) every card stays
  // visible — the pre-feature behaviour — rather than the dashboard going blank.
  const showCpuSection = monitoredSections.has('cpu')
  const showMemorySection = monitoredSections.has('memory')
  const showDiskSection = monitoredSections.has('disk')
  const showGpuSection = monitoredSections.has('gpu')
  const showNpuSection = monitoredSections.has('npu')
  const showPressureSection = monitoredSections.has('pressure')
  const showNetworkSection = monitoredSections.has('network')
  // With no monitored sections (config `monitored_sections: []`) there is
  // nothing to display, and a bare /dynamic_info request would otherwise be
  // read by the backend as "collect everything".  Skip polling entirely; the
  // fallback set is non-empty, so this only ever goes false once the config
  // endpoint has actually resolved to an empty set.
  const hasMonitoredSections = monitoredSectionList.length > 0

  // Wall-clock of the previous sample, so a slower cadence (e.g. the 10s
  // background interval) advances the trend by the right number of fixed
  // `refreshIntervalMs` slots instead of a single one.  Without this a 10s
  // sample would be plotted 2s to the right of the last, compressing the time
  // axis whenever the poll rate differs from the slot unit.
  const lastPushAtRef = useRef<number | null>(null)

  const pushTrendPoints = useCallback((updates: Record<string, number | null>) => {
    const now = Date.now()
    const last = lastPushAtRef.current
    lastPushAtRef.current = now
    // How many slots (each = refreshIntervalMs) elapsed since the last sample.
    // The skipped slots are filled with null so the new point lands on its true
    // time coordinate; foreground steady polling yields 1 (unchanged behaviour).
    const slots = last == null
      ? 1
      : Math.max(1, Math.min(TREND_STORAGE_MAX_POINTS, Math.round((now - last) / refreshIntervalMs)))

    setTrends((prev) => {
      const next: TrendSeries = { ...prev }
      Object.entries(updates).forEach(([key, value]) => {
        const current = prev[key] || []
        // Pad the (slots - 1) skipped intervals with null, then the sample.
        const updated = slots > 1
          ? [...current, ...Array(slots - 1).fill(null), value]
          : [...current, value]
        if (updated.length > TREND_STORAGE_MAX_POINTS) {
          updated.splice(0, updated.length - TREND_STORAGE_MAX_POINTS)
        }
        next[key] = updated
      })
      return next
    })
  }, [refreshIntervalMs])

  // Trend points are stored as fixed-width slots (each = refreshIntervalMs), so
  // changing the cadence would otherwise reinterpret every buffered point at the
  // new slot width (a stretched/compressed time axis).  Instead, re-bucket the
  // existing history to the new slot width: each sample keeps its true age, the
  // trend stays continuous, and new samples keep appending at the chosen rate.
  const prevIntervalRef = useRef(refreshIntervalMs)
  useEffect(() => {
    const oldSlotMs = prevIntervalRef.current
    if (oldSlotMs === refreshIntervalMs) return
    prevIntervalRef.current = refreshIntervalMs
    setTrends((prev) => {
      const next: TrendSeries = {}
      for (const key of Object.keys(prev)) {
        next[key] = resampleSeries(prev[key], oldSlotMs, refreshIntervalMs)
      }
      return next
    })
  }, [refreshIntervalMs])

  const fetchStatic = useCallback(async () => {
    if (!active) return
    try {
      const data = await api.getStaticInfo()
      setStaticInfo(data)
      writeCachedStaticInfo(data)
      setErrorStatic(null)
    } catch (e: unknown) {
      setErrorStatic(e instanceof Error ? e.message : 'Failed to fetch static info')
    }
  }, [active])

  const fetchDynamic = useCallback(async () => {
    setLoadingDynamic(true)
    try {
      const data = await api.getDynamicInfo(monitoredSectionList)
      const quiet = Boolean(data.quiet_mode?.active)
      setQuietActive(quiet)
      setDynamicInfo((prev) => ({
        ...data,
        // Keep previous CPU payload if CPU is enabled and this tick briefly omits it.
        cpu: data.cpu ?? (showCpuSection ? prev?.cpu : undefined),
      }))
      setErrorDynamic(null)

      const updates: Record<string, number | null> = {
        'npu:availability': data.npu?.npu_smi?.available ? 100 : 0,
      }
      if (showCpuSection) {
        updates['cpu:p'] = normalizePercent(data.cpu?.p_core_usage)
        updates['cpu:e'] = normalizePercent(data.cpu?.e_core_usage)
        updates['cpu:lpe'] = normalizePercent(data.cpu?.lpe_core_usage)
        updates['util:cpu'] = normalizePercent(data.cpu?.usage_total)
      }
      if (showMemorySection) {
        updates['util:memory'] = normalizePercent(data.memory?.usage_percent)
      }
      if (showPressureSection) {
        updates['pressure:score'] = isNumber(data.pressure?.score) ? data.pressure.score! * 100 : null
      }

      const npuRawParsed = parseNpuRaw(data.npu?.npu_smi?.raw)
      if (npuRawParsed) {
        updates['npu:util'] = typeof npuRawParsed.utilization_percent === 'number' ? normalizePercent(npuRawParsed.utilization_percent as number) : null
        updates['npu:power_w'] = typeof npuRawParsed.power_w === 'number' ? npuRawParsed.power_w : null
        updates['npu:freq_mhz'] = typeof npuRawParsed.frequency_mhz === 'number' ? npuRawParsed.frequency_mhz : null
        updates['npu:temp_c'] = typeof npuRawParsed.temperature_c === 'number' ? npuRawParsed.temperature_c : null
        updates['npu:bandwidth_mib'] = typeof npuRawParsed.noc_bandwidth_mib_per_s === 'number' ? npuRawParsed.noc_bandwidth_mib_per_s : null
        updates['npu:memory_mb'] = typeof npuRawParsed.memory_bytes === 'number' ? (npuRawParsed.memory_bytes as number) / (1024 * 1024) : null
      }

      if (showPressureSection) {
        updates['pressure:peak'] = updates['pressure:score'] ?? null
      }

      if (showCpuSection && data.cpu?.per_core_usage) {
        data.cpu.per_core_usage.forEach((value, index) => {
          updates[`cpu:core:${index}`] = normalizePercent(value)
          updates[`cpu:core_freq:${index}`] = isNumber(data.cpu?.per_core_freq_mhz?.[index])
            ? data.cpu!.per_core_freq_mhz[index]
            : null
        })
      }

      if (showDiskSection) {
        const dynamicDiskDevices = Object.values(data.disk?.disk_io || {})
        updates['util:disk'] = dynamicDiskDevices.length
          ? Math.max(...dynamicDiskDevices.map((disk) => normalizePercent(disk.utilization) || 0))
          : null

        Object.entries(data.disk?.disk_io || {}).forEach(([diskName, diskData]) => {
          updates[`disk:${diskName}:util`] = normalizePercent(diskData.utilization)
        })
      }

      if (showNetworkSection) {
        // Per-NIC trend data for each valid physical NIC
        const validNics = staticInfo?.network?.valid_nics || []
        const allNetworkUtils: number[] = []
        for (const nic of validNics) {
          const nicName = nic.name
          const nicData = data.network?.interfaces?.[nicName]
          const nicRx = isNumber(nicData?.rx_bytes_per_sec) ? nicData.rx_bytes_per_sec : null
          const nicTx = isNumber(nicData?.tx_bytes_per_sec) ? nicData.tx_bytes_per_sec : null
          const rxMbps = toMbps(nicRx)
          const txMbps = toMbps(nicTx)
          // Store raw Mbps for bandwidth sparklines
          updates[`bw:network:${nicName}:rx_mbps`] = rxMbps
          updates[`bw:network:${nicName}:tx_mbps`] = txMbps
          // Compute utilization against static NIC link speed
          const nicSpeedMbps = nic.speed_mbps > 0 ? nic.speed_mbps : null
          const nicRxUtil = isNumber(rxMbps) && isNumber(nicSpeedMbps) && nicSpeedMbps > 0 ? Math.min(rxMbps / nicSpeedMbps * 100, 100) : null
          const nicTxUtil = isNumber(txMbps) && isNumber(nicSpeedMbps) && nicSpeedMbps > 0 ? Math.min(txMbps / nicSpeedMbps * 100, 100) : null
          updates[`util:network:${nicName}:rx`] = nicRxUtil
          updates[`util:network:${nicName}:tx`] = nicTxUtil
          const nicUtils = [nicRxUtil, nicTxUtil].filter(isNumber)
          const nicUtilMax = nicUtils.length ? Math.max(...nicUtils) : null
          updates[`util:network:${nicName}`] = nicUtilMax
          if (isNumber(nicUtilMax)) allNetworkUtils.push(nicUtilMax)
        }
        // Fallback: if no valid NICs, use aggregated total with static peak bandwidth
        if (validNics.length === 0) {
          const totalNet = data.network?.total
          const fallbackRx = isNumber(totalNet?.rx_bytes_per_sec) ? totalNet?.rx_bytes_per_sec : null
          const fallbackTx = isNumber(totalNet?.tx_bytes_per_sec) ? totalNet?.tx_bytes_per_sec : null
          const rxMbps = toMbps(fallbackRx)
          const txMbps = toMbps(fallbackTx)
          updates['bw:network:rx_mbps'] = rxMbps
          updates['bw:network:tx_mbps'] = txMbps
          const staticPeak = staticInfo?.network?.network_peak_mbps
          const staticPeakMbps = isNumber(staticPeak) && staticPeak > 0 ? staticPeak : null
          const rxUtil = isNumber(rxMbps) && isNumber(staticPeakMbps) && staticPeakMbps > 0 ? Math.min(rxMbps / staticPeakMbps * 100, 100) : null
          const txUtil = isNumber(txMbps) && isNumber(staticPeakMbps) && staticPeakMbps > 0 ? Math.min(txMbps / staticPeakMbps * 100, 100) : null
          updates['util:network:rx'] = rxUtil
          updates['util:network:tx'] = txUtil
          const fallbackUtils = [rxUtil, txUtil].filter(isNumber)
          if (fallbackUtils.length) allNetworkUtils.push(Math.max(...fallbackUtils))
        }
        updates['util:network'] = allNetworkUtils.length ? Math.max(...allNetworkUtils) : null
      }

      if (showGpuSection) {
        const gpuDevices = buildGpuDevices(staticInfo, data)
        const gpuUtils: number[] = []
        gpuDevices.forEach((device) => {
          updates[`gpu:${device.id}:util`] = device.utilization
          updates[`gpu:${device.id}:power:gpu_cur_power`] = device.powerGpu
          updates[`gpu:${device.id}:power:pkg_cur_power`] = device.powerPkg
          updates[`gpu:${device.id}:freq:gt0:cur_mhz`] = device.frequencies.gt0?.act_mhz ?? null
          updates[`gpu:${device.id}:freq:gt1:cur_mhz`] = device.frequencies.gt1?.act_mhz ?? null
          updates[`gpu:${device.id}:freq:gt0:act_mhz`] = device.frequencies.gt0?.act_mhz ?? null
          updates[`gpu:${device.id}:freq:gt0:req_mhz`] = device.frequencies.gt0?.cur_mhz ?? null
          updates[`gpu:${device.id}:freq:gt1:act_mhz`] = device.frequencies.gt1?.act_mhz ?? null
          updates[`gpu:${device.id}:freq:gt1:req_mhz`] = device.frequencies.gt1?.cur_mhz ?? null
          updates[`gpu:${device.id}:freq:gt0:rc6_pct`] = device.frequencies.gt0?.rc6_pct ?? null
          updates[`gpu:${device.id}:freq:gt1:rc6_pct`] = device.frequencies.gt1?.rc6_pct ?? null
          updates[`gpu:${device.id}:vram_usage`] = device.vramUsage
          if (isNumber(device.utilization)) gpuUtils.push(device.utilization)
          ENGINE_ORDER.forEach((engine) => {
            updates[`gpu:${device.id}:engine:${engine}`] = device.engineUtil[engine]
          })
        })
        updates['gpu:aggregate'] = gpuUtils.length ? Math.max(...gpuUtils) : null
      }

      // A frozen cache is not a measurement.  Feeding it to the trends would
      // draw the machine as flat-lining at whatever it was doing when the run
      // started, which is a worse lie than the gap the paused poll leaves.
      if (!quiet) pushTrendPoints(updates)
    } catch (e: unknown) {
      setErrorDynamic(e instanceof Error ? e.message : 'Failed to fetch dynamic info')
    } finally {
      setLoadingDynamic(false)
    }
  }, [
    pushTrendPoints,
    staticInfo,
    monitoredSectionList,
    showCpuSection,
    showMemorySection,
    showDiskSection,
    showGpuSection,
    showNpuSection,
    showPressureSection,
    showNetworkSection,
  ])

  useEffect(() => {
    fetchStatic()
  }, [fetchStatic])

  // Pressure level cut-offs are config, not telemetry: fetch once when the
  // pressure cards are shown so each gauge draws its own type's severity zones.
  useEffect(() => {
    if (!active || !showPressureSection) return
    let cancelled = false
    Promise.allSettled([
      api.getConfig<{ thresholds?: ThresholdSet }>('system_pressure'),
      api.getConfig<{ disk_thresholds?: ThresholdSet }>('disk_pressure'),
      api.getConfig<{ network_thresholds?: ThresholdSet }>('network_pressure'),
    ]).then(([sys, disk, net]) => {
      if (cancelled) return
      setPressureThresholds({
        system: sys.status === 'fulfilled' ? sys.value.thresholds ?? {} : {},
        disk: disk.status === 'fulfilled' ? disk.value.disk_thresholds ?? {} : {},
        network: net.status === 'fulfilled' ? net.value.network_thresholds ?? {} : {},
      })
    })
    return () => { cancelled = true }
  }, [active, showPressureSection])

  const fetchTopApps = useCallback(async () => {
    const data = await api.getAppResourceStats(HOME_APP_CANDIDATE_COUNT)
    setTopApps(takeUniqueApps(data.apps ?? [], HOME_TOP_APP_COUNT))
  }, [])

  // Diagnostics starts only after capabilities resolve. Keeping it in its own
  // poller makes that false -> true transition trigger an immediate first fetch.
  const fetchDiagnosticsSummary = useCallback(async () => {
    if (!diagnosticsEnabled) return
    const nowSec = getLatestServerTime() ?? Math.floor(Date.now() / 1000)
    const bootTime = staticInfo?.host?.boot_time
    const fromSec = Math.max(nowSec - COCKPIT_DIAG_WINDOW_SEC, typeof bootTime === 'number' ? bootTime : 0)
    const window = createLiveDiagnosticWindow(fromSec, nowSec)
    await Promise.all([
      api.getDiagReport(window.from, window.to),
      api.getDiagEvents({ from: window.from, to: window.queryTo, limit: 100 }).catch(() => null),
    ]).then(async ([digest, eventData]) => {
      const attentionAlerts = (digest?.active_alerts ?? []).filter((alert) => alertNeedsAttention(alert))
      const attentionAlert = pickTopAlert(attentionAlerts)
      setDiagSummary({
        counts: eventData
          ? summarizeVisibleDiagnosticEvents(eventData.events ?? [], window.from, window.to)
          : digest?.event_stats?.by_severity ?? { info: 0, warning: 0, error: 0, critical: 0 },
        attentionCount: attentionAlerts.length,
        attentionAlert: attentionAlert
          ? { severity: attentionAlert.severity, text: attentionAlert.summary || attentionAlert.event_type }
          : null,
        insight: null,
      })
      setDiagSummaryStatus('ready')

      if (attentionAlert) return
      try {
        const context = await api.getDiagContext({ from: window.from, to: window.to, findings_only: true })
        const insight = pickTopFinding(context.findings ?? [])
        setDiagSummary((current) => current ? { ...current, insight } : current)
      } catch {
        // The digest remains useful when optional insight evaluation fails.
      }
    }, () => {
      setDiagSummaryStatus((current) => current === 'ready' ? current : 'unavailable')
    })
  }, [diagnosticsEnabled, staticInfo?.host?.boot_time])

  const fetchControlSummary = useCallback(async () => {
    if (!active || !balancerEnabled) return
    // Registered apps and engine-throttled unregistered apps come from separate endpoints;
    // count both, the same way the Control center table merges them.
    await Promise.allSettled([api.getControlledApps(), api.getAutoLimitedApps()]).then(
      ([controlledResult, autoResult]) => {
        if (controlledResult.status === 'rejected' && autoResult.status === 'rejected') return
        const list = controlledResult.status === 'fulfilled' && Array.isArray(controlledResult.value)
          ? controlledResult.value
          : []
        const limited = list.filter((app) => ['AUTO_LIMITED', 'MANUAL_LIMITED'].includes(app.control_status || ''))
        const controlledIds = new Set(list.map((app) => app.app_id))
        const unregisteredAuto = autoResult.status === 'fulfilled'
          ? (autoResult.value.apps ?? []).filter(
            (app) => !app.is_controlled
              && !controlledIds.has(app.app_id)
              && !controlledIds.has(app.effective_app_id),
          )
          : []
        const autoApps = [
          ...limited.filter((app) => app.control_status === 'AUTO_LIMITED'),
          ...unregisteredAuto,
        ]
        const manualCount = limited.filter((app) => app.control_status === 'MANUAL_LIMITED').length
        const featured = autoApps[0] ?? limited[0]
        const nextSummary: ControlCockpitSummary = {
          auto: autoApps.length,
          manual: manualCount,
          featured: featured
            ? { name: featured.app_name || featured.app_id, limit: formatEffectiveControl(featured) }
            : null,
          remaining: Math.max(0, autoApps.length + manualCount - 1),
        }
        setControlSummary((current) => {
          const unchanged = current
            && current.auto === nextSummary.auto
            && current.manual === nextSummary.manual
            && current.remaining === nextSummary.remaining
            && current.featured?.name === nextSummary.featured?.name
            && current.featured?.limit === nextSummary.featured?.limit
          return unchanged ? current : nextSummary
        })
      },
    )
  }, [active, balancerEnabled])

  usePolling(
    fetchTopApps,
    foreground ? refreshIntervalMs : INACTIVE_REFRESH_INTERVAL_MS,
    !quietActive,
  )

  usePolling(
    fetchDiagnosticsSummary,
    COCKPIT_REFRESH_INTERVAL_MS,
    !quietActive && diagnosticsEnabled,
  )

  usePolling(
    fetchControlSummary,
    COCKPIT_REFRESH_INTERVAL_MS,
    active && !quietActive && balancerEnabled,
  )

  usePolling(
    fetchDynamic,
    quietActive
      ? QUIET_PROBE_INTERVAL_MS
      : foreground
        ? refreshIntervalMs
        : INACTIVE_REFRESH_INTERVAL_MS,
    // Under quiet mode a backgrounded page makes no requests at all.  usePolling
    // fires once as soon as `enabled` goes true again, so coming back to this tab
    // both refreshes it and clears `quietActive` on its own once the run ends.
    hasMonitoredSections && (!quietActive || foreground),
  )

  // Refetch immediately when the *content* of the monitored section set changes
  // (not on every fetchDynamic identity change) so newly enabled cards fill in
  // without waiting a full poll.  Keyed on the joined section names and calling
  // through a ref keeps this decoupled from fetchDynamic's identity — otherwise
  // every re-render that rebuilt fetchDynamic (e.g. a window-focus resync, or
  // dev StrictMode) would fire an extra request on top of the poll.  The first
  // run is skipped because the initial fetch is already driven by usePolling.
  const fetchDynamicRef = useRef(fetchDynamic)
  fetchDynamicRef.current = fetchDynamic
  const monitoredSectionsKey = monitoredSectionList.join('|')
  const skipFirstSectionsFetch = useRef(true)
  useEffect(() => {
    if (!active) return
    if (skipFirstSectionsFetch.current) {
      skipFirstSectionsFetch.current = false
      return
    }
    // Nothing monitored → nothing to fetch (a bare request would pull everything).
    if (!monitoredSectionList.length) return
    fetchDynamicRef.current()
  }, [active, monitoredSectionsKey, monitoredSectionList.length])

  // A run ending is the moment collection resumes, and it arrives on the
  // benchmark event stream App already holds open.  Without it this page would
  // sit on the slow probe (or, backgrounded, on nothing at all) for up to half a
  // minute after the machine went live again.  Only subscribed while quiet.
  useBenchEvent(
    useCallback(
      (event) => {
        if (event.type !== 'job' || event.job.status === 'running') return
        setQuietActive(false)
        if (active) fetchDynamicRef.current()
      },
      [active],
    ),
    quietActive,
  )

  const gpuDevices = useMemo(() => buildGpuDevices(staticInfo, dynamicInfo), [staticInfo, dynamicInfo])
  const selectedGpuDevice = deviceDetail?.kind === 'gpu'
    ? gpuDevices.find((device) => device.id === deviceDetail.deviceId) ?? null
    : null

  // Trend window: number of points = window duration / polling interval
  const trendPoints = useMemo(() => {
    const windowMs = trendWindow === '1m' ? 60_000 : 5 * 60_000
    return Math.max(1, Math.round(windowMs / refreshIntervalMs))
  }, [trendWindow, refreshIntervalMs])

  const getSeries = useCallback(
    (key: string) => {
      // Left-pad to a fixed window so the newest sample always sits at the
      // right edge ("now") and the not-yet-collected left portion stays blank
      // until data fills in from the right, rather than stretching whatever
      // points exist across the full chart width.
      const values = trends[key] || []
      return windowSeries(values, trendPoints)
    },
    [trends, trendPoints],
  )

  const validNics = useMemo(() => staticInfo?.network?.valid_nics || [], [staticInfo?.network?.valid_nics])

  const gpuUtilizationSeries = useMemo(
    () => gpuDevices.map((device, index) => ({
      key: `gpu-util-${device.id}`,
      label: `${device.displayLabel} Util %`,
      data: getSeries(`gpu:${device.id}:util`),
      stroke: GPU_UTIL_COLORS[index % GPU_UTIL_COLORS.length],
    })),
    [gpuDevices, getSeries],
  )

  const gpuSplitBars = useMemo(
    () => gpuDevices.map((device, index) => ({
      key: `gpu-bar-${device.id}`,
      label: device.displayLabel,
      value: device.utilization,
      color: GPU_UTIL_COLORS[index % GPU_UTIL_COLORS.length],
    })),
    [gpuDevices],
  )

  const gpuAggregateUtil = useMemo(() => {
    const values = gpuDevices.map((device) => device.utilization).filter(isNumber)
    return values.length ? Math.max(...values) : null
  }, [gpuDevices])

  const gpuHasThrottle = useMemo(() => gpuDevices.some((device) => device.status === 'Throttle'), [gpuDevices])

  const gpuCombinedStatus = useMemo(() => {
    if (!gpuDevices.length) return undefined
    if (gpuHasThrottle) return 'Throttle'
    if (isNumber(gpuAggregateUtil) && gpuAggregateUtil >= BUSY_UTIL_THRESHOLD) return 'Busy'
    return 'OK'
  }, [gpuDevices, gpuHasThrottle, gpuAggregateUtil])

  const gpuCombinedStatusColor = useMemo(() => {
    if (!gpuCombinedStatus) return COLORS.textMuted
    if (gpuCombinedStatus === 'Throttle') return COLORS.orange
    if (gpuCombinedStatus === 'Busy') return COLORS.red
    return COLORS.green
  }, [gpuCombinedStatus])

  const gpuCombinedDetails = useMemo(
    () => gpuDevices.flatMap((device) => [
      { label: device.displayLabel, value: '', divider: true },
      // Dynamic items only — static (EU Count, PCIe) are in gpuSnapshotMeta subtitle
      {
        label: `${device.displayLabel} GT0 Act Freq`,
        value: formatMetric(device.frequencies.gt0?.act_mhz, 'MHz', 0),
        source: 'dynamic' as DataSourceKind,
      },
      {
        label: `${device.displayLabel} GT0 Req Freq`,
        value: formatMetric(device.frequencies.gt0?.cur_mhz, 'MHz', 0),
        source: 'dynamic' as DataSourceKind,
      },
      {
        label: `${device.displayLabel} GT1 Act Freq`,
        value: formatMetric(device.frequencies.gt1?.act_mhz, 'MHz', 0),
        source: 'dynamic' as DataSourceKind,
      },
      {
        label: `${device.displayLabel} GT1 Req Freq`,
        value: formatMetric(device.frequencies.gt1?.cur_mhz, 'MHz', 0),
        source: 'dynamic' as DataSourceKind,
      },
      {
        label: `${device.displayLabel} GPU Power`,
        value: formatMetric(device.powerGpu, 'W', 2),
        source: 'dynamic' as DataSourceKind,
      },
      {
        label: `${device.displayLabel} ${device.label === 'iGPU' ? 'Pkg' : 'Card'} Power`,
        value: formatMetric(device.powerPkg, 'W', 2),
        source: 'dynamic' as DataSourceKind,
      },
      {
        label: device.label === 'iGPU' ? `${device.displayLabel} Sys Mem` : `${device.displayLabel} VRAM`,
        value: formatPercent(device.vramUsage),
        source: 'dynamic' as DataSourceKind,
      },
    ]),
    [gpuDevices],
  )

  const cpuFreqRangeMeta = useMemo(() => {
    const freqMhz = staticInfo?.cpu?.freq_mhz
    const pf = freqMhz?.p_core_freq_mhz
    const ef = freqMhz?.e_core_freq_mhz
    const lf = freqMhz?.lpe_core_freq_mhz
    const overall = formatFreqRange(freqMhz?.min_mhz, freqMhz?.max_mhz)
    const parts: string[] = []
    if (pf) parts.push(`P: ${formatFreqRange(pf.min_mhz, pf.max_mhz)}`)
    if (ef) parts.push(`E: ${formatFreqRange(ef.min_mhz, ef.max_mhz)}`)
    if (lf) parts.push(`LPE: ${formatFreqRange(lf.min_mhz, lf.max_mhz)}`)
    return parts.length ? parts.join(' / ') : overall
  }, [staticInfo?.cpu?.freq_mhz])

  const cpuSnapshotMeta = staticInfo?.cpu?.model_name
    ? `${staticInfo?.cpu?.core_count?.logical ?? 0} cores | ${cpuFreqRangeMeta} | ${staticInfo?.cpu?.model_name}`
    : ((dynamicInfo?.cpu?.per_core_usage?.length ?? 0) > 0)
      ? `${dynamicInfo?.cpu?.per_core_usage?.length ?? 0} cores`
      : 'No data'

  const staticMemory = staticInfo?.memory
  const dynamicTotalGb = dynamicInfo?.memory?.total_gb
  const dynamicSwapTotalGb = dynamicInfo?.memory?.swap_total_gb
  const memorySnapshotMeta = useMemo(() => {
    if (staticMemory) {
      const parts: string[] = []
      const totalGb = staticMemory.total_gb
      if (totalGb != null && isNumber(totalGb)) parts.push(`${totalGb.toFixed(1)} GB`)
      const swapTotalGb = staticMemory.swap_total_gb ?? dynamicSwapTotalGb
      if (isNumber(swapTotalGb)) parts.push(`Swap ${swapTotalGb.toFixed(1)} GB`)
      // Memory type (e.g. LPDDR5) from devices or ddr_speeds
      const memTypes = [...new Set(
        (staticMemory.devices?.devices ?? []).map((d) => d.type).filter(Boolean) as string[]
      )]
      if (memTypes.length) parts.push(memTypes.join('/'))
      // Prefer actual configured speed over rated speed
      const firstDev = staticMemory.devices?.devices?.[0]
      const configuredSpeed = firstDev?.configured_speed
      const speedStr = (configuredSpeed && configuredSpeed !== 'Unknown')
        ? configuredSpeed
        : (staticMemory.ddr_speeds?.length ? staticMemory.ddr_speeds.join('/') : null)
      if (speedStr) parts.push(speedStr)
      else if (!memTypes.length) parts.push('DDR N/A')
      // Theoretical peak DRAM bandwidth, after capacity/type/speed.
      const peak = staticMemory.max_bandwidth_gb_s
      if (isNumber(peak)) parts.push(`${peak.toFixed(0)} GB/s`)
      return parts.join(' | ')
    }
    const parts: string[] = []
    if (isNumber(dynamicTotalGb)) parts.push(`${dynamicTotalGb.toFixed(1)} GB total`)
    if (isNumber(dynamicSwapTotalGb)) parts.push(`Swap ${dynamicSwapTotalGb.toFixed(1)} GB`)
    return parts.length ? parts.join(' | ') : 'No data'
  }, [staticMemory, dynamicTotalGb, dynamicSwapTotalGb])

  // Build per-NIC render data
  const networkNicCards = useMemo(() => {
    if (!validNics.length) return []
    return validNics.map((nic) => {
      const nicName = nic.name
      const nicData = dynamicInfo?.network?.interfaces?.[nicName]
      const rxRate = isNumber(nicData?.rx_bytes_per_sec) ? nicData?.rx_bytes_per_sec : null
      const txRate = isNumber(nicData?.tx_bytes_per_sec) ? nicData?.tx_bytes_per_sec : null
      const bandwidth = nic.speed_mbps > 0 ? nic.speed_mbps : null
      // Use static NIC link speed for utilization
      const rxMbps = toMbps(rxRate)
      const txMbps = toMbps(txRate)
      const rxUtil = isNumber(rxMbps) && isNumber(bandwidth) && bandwidth > 0 ? Math.min(rxMbps / bandwidth * 100, 100) : null
      const txUtil = isNumber(txMbps) && isNumber(bandwidth) && bandwidth > 0 ? Math.min(txMbps / bandwidth * 100, 100) : null
      const utilValues = [rxUtil, txUtil].filter(isNumber)
      const utilMax = utilValues.length ? Math.max(...utilValues) : null
      const pressure = dynamicInfo?.pressure?.network_interfaces?.[nicName] ?? null
      return { nicName, bandwidth, rxRate, txRate, rxUtil, txUtil, utilMax, pressure }
    })
  }, [validNics, dynamicInfo])

  // Fallback for when no valid NICs exist (keep legacy single-card behavior)
  const primaryInterface = staticInfo?.network?.primary_interface
  const primaryInterfaceName = typeof primaryInterface === 'string'
    ? primaryInterface
    : null
  const fallbackNetworkRates = primaryInterfaceName && dynamicInfo?.network?.interfaces?.[primaryInterfaceName]
    ? dynamicInfo.network.interfaces[primaryInterfaceName]
    : dynamicInfo?.network?.total
  const fallbackRxRate = isNumber(fallbackNetworkRates?.rx_bytes_per_sec) ? fallbackNetworkRates?.rx_bytes_per_sec : null
  const fallbackTxRate = isNumber(fallbackNetworkRates?.tx_bytes_per_sec) ? fallbackNetworkRates?.tx_bytes_per_sec : null
  // Use static peak bandwidth for fallback utilization
  const fbRxMbps = toMbps(fallbackRxRate)
  const fbTxMbps = toMbps(fallbackTxRate)
  const fbStaticPeak = staticInfo?.network?.network_peak_mbps
  const fbStaticPeakMbps = isNumber(fbStaticPeak) && fbStaticPeak > 0 ? fbStaticPeak : null
  const fallbackRxUtil = isNumber(fbRxMbps) && isNumber(fbStaticPeakMbps) && fbStaticPeakMbps > 0 ? Math.min(fbRxMbps / fbStaticPeakMbps * 100, 100) : null
  const fallbackTxUtil = isNumber(fbTxMbps) && isNumber(fbStaticPeakMbps) && fbStaticPeakMbps > 0 ? Math.min(fbTxMbps / fbStaticPeakMbps * 100, 100) : null
  const fallbackUtilValues = [fallbackRxUtil, fallbackTxUtil].filter(isNumber)
  const fallbackUtilMax = fallbackUtilValues.length ? Math.max(...fallbackUtilValues) : null

  // Aggregate network util across all NICs (for pressure gauge)
  const nicUtilValues = networkNicCards.map((n) => n.utilMax).filter(isNumber)
  const networkUtilMax = nicUtilValues.length
    ? Math.max(...nicUtilValues)
    : fallbackUtilMax

  const npuSnapshotMeta = (() => {
    const parts: string[] = []
    if (staticInfo?.npu?.pciid) parts.push(`[${staticInfo.npu.pciid}]`)
    const freqEntries = Object.values(staticInfo?.npu?.freq_bounds_mhz || {})
    const maxFreq = freqEntries.length ? freqEntries[0]?.max_mhz : null
    if (isNumber(maxFreq)) parts.push(`Freq ${Math.round(maxFreq)} MHz`)
    if (parts.length) return parts.join(' | ')
    return staticInfo?.npu?.names?.length
      ? staticInfo.npu.names.join(', ')
      : dynamicInfo?.npu?.npu_smi?.error || 'npu-smi'
  })()

  const gpuSnapshotMeta = gpuDevices.length
    ? gpuDevices.map((d) => {
        const parts: string[] = [`${d.displayLabel}(${d.driver !== 'N/A' ? d.driver : '?'})`]
        if (d.label === 'iGPU' && isNumber(d.euCount)) parts.push(`EU ${d.euCount}`)
        if (d.pcieLink.current_speed) parts.push(`PCIe ${formatPcieLink(d.pcieLink.current_speed, d.pcieLink.current_width, d.pcieLink.max_speed, d.pcieLink.max_width)}`)
        return parts.join(' ')
      }).join(' | ')
      + (staticInfo?.gpu?.names?.length ? ` | ${staticInfo.gpu.names.join(' / ')}` : '')
    : (staticInfo?.gpu
        ? `${staticInfo.gpu.count ?? 0} GPU | ${staticInfo.gpu.names?.join(' / ') || 'Unknown'}`
        : 'No data')

  const diskDevices: [string, DiskDeviceData][] = dynamicInfo?.disk?.disk_io ? Object.entries(dynamicInfo.disk.disk_io) : []
  const busiestDisk = diskDevices.length
    ? [...diskDevices].sort((a, b) => {
        const left = normalizePercent(a[1]?.utilization) || 0
        const right = normalizePercent(b[1]?.utilization) || 0
        return right - left
      })[0]
    : null
  const maxDiskUtil = busiestDisk ? normalizePercent(busiestDisk[1]?.utilization) : null

  const cpuUsagePct = normalizePercent(dynamicInfo?.cpu?.usage_total ?? null)
  const memoryUsagePct = normalizePercent(dynamicInfo?.memory?.usage_percent ?? null)

  // System pressure: use the weighted composite score from SystemPressureMonitor when available
  const pressureScoreRaw = dynamicInfo?.pressure?.score
  const pressureScore = isNumber(pressureScoreRaw) ? pressureScoreRaw * 100 : null
  const pressureLevel = dynamicInfo?.pressure?.level ?? null
  const fallbackSystemPressure = isNumber(cpuUsagePct) && isNumber(memoryUsagePct)
    ? (cpuUsagePct + memoryUsagePct) / 2
    : null
  const systemPressurePct = pressureScore ?? fallbackSystemPressure

  // Disk IO: use is_disk_io_stressed data
  const diskIsStressed = dynamicInfo?.disk?.is_stressed ?? false
  // Disk IO pressure: read pre-computed values from backend
  const diskBusyNames = dynamicInfo?.disk?.busy_disks ?? []
  const diskTotalCount = dynamicInfo?.disk?.total_disks ?? 0
  const diskBusyCount = diskBusyNames.length
  const diskBusyPct = dynamicInfo?.disk?.busy_pct ?? null
  const diskBusyLevelLabel = dynamicInfo?.disk?.busy_level ?? 'NO DATA'
  // Gauge shows severity (PSI-gated pressure), not breadth; fall back to busy_pct
  // when severity is unavailable (USE-only path with no pressure tick).
  const diskPressurePct = dynamicInfo?.disk?.pressure_pct ?? diskBusyPct

  // Network pressure is the worst fused RX/TX score across controlled NICs;
  // busy-NIC count separately describes how broadly the pressure is spread.
  const networkBusyNics = dynamicInfo?.pressure?.network_busy_nics ?? []
  const networkTotalCount = dynamicInfo?.pressure?.network_total_nics ?? 0
  const networkBusyCount = networkBusyNics.length
  const networkPressurePct = dynamicInfo?.pressure?.network_pressure_pct ?? null
  const networkPressureLevelLabel = dynamicInfo?.pressure?.network_pressure_level ?? 'NO DATA'
  const networkWorstNic = dynamicInfo?.pressure?.network_worst_nic ?? null
  const networkWorstDirection = dynamicInfo?.pressure?.network_worst_direction ?? null
  // Short "why" for the overview gauge when it turns critical, pulled from the worst NIC's
  // worst direction so the summary card is self-explanatory without opening the NIC card.
  const networkWorstReason = (() => {
    if (!networkWorstNic || !networkWorstDirection) return null
    const dir = networkWorstDirection.toLowerCase() === 'tx' ? 'tx' : 'rx'
    return dynamicInfo?.pressure?.network_interfaces?.[networkWorstNic]?.[dir]?.reason ?? null
  })()

  // Severity zones per pressure type, from config (with defaults until loaded).
  const systemPressureZones = useMemo(() => toPressureZones(pressureThresholds?.system), [pressureThresholds])
  const diskPressureZones = useMemo(() => toPressureZones(pressureThresholds?.disk), [pressureThresholds])
  const networkPressureZones = useMemo(() => toPressureZones(pressureThresholds?.network), [pressureThresholds])

  const npuSmiRaw = dynamicInfo?.npu?.npu_smi?.raw
  const npuParsed = useMemo(() => parseNpuRaw(npuSmiRaw), [npuSmiRaw])
  const npuUtilValue = useMemo(() => {
    if (!dynamicInfo?.npu?.npu_smi?.available) return null
    return typeof npuParsed?.utilization_percent === 'number' ? normalizePercent(npuParsed?.utilization_percent as number) : null
  }, [dynamicInfo, npuParsed])
  const npuValue = npuUtilValue
  const cpuTrendValue = cpuUsagePct
  const memoryTrendValue = memoryUsagePct
  const cpuBusy = isNumber(cpuTrendValue) ? cpuTrendValue >= BUSY_UTIL_THRESHOLD : false
  const memoryBusy = isNumber(memoryTrendValue) ? memoryTrendValue >= BUSY_UTIL_THRESHOLD : false
  const cpuTrendStatusColor = cpuBusy ? COLORS.orange : isNumber(cpuTrendValue) ? COLORS.green : COLORS.textMuted
  const memoryTrendStatusColor = memoryBusy ? COLORS.orange : isNumber(memoryTrendValue) ? COLORS.green : COLORS.textMuted
  // NPU now follows the same utilization rule as other devices (was availability-only).
  const npuAvailable = Boolean(dynamicInfo?.npu?.npu_smi?.available)
  const npuBusy = npuAvailable && isNumber(npuUtilValue) && npuUtilValue >= BUSY_UTIL_THRESHOLD
  const npuStatusColor = !npuAvailable ? COLORS.textMuted : npuBusy ? COLORS.orange : COLORS.green
  // One trend line per GPU device so multi-GPU hosts show every card instead of
  // only the first.  A single GPU keeps the plain "GPU" label to match the rest.
  // Keys avoid dots (device.id can be a PCI address) since recharts reads a
  // dotted dataKey as a nested path.
  const gpuTrendSeries = gpuDevices.map((device, index) => ({
    key: `gpu${index}`,
    seriesKey: `gpu:${device.id}:util`,
    label: gpuDevices.length === 1 ? 'GPU' : device.displayLabel,
    color: GPU_TREND_COLORS[index % GPU_TREND_COLORS.length],
  }))

  const trendSeriesMeta = [
    { key: 'cpu', label: 'CPU', color: '#60a5fa' },
    ...gpuTrendSeries.map(({ key, label, color }) => ({ key, label, color })),
    { key: 'npu', label: 'NPU', color: PERF_COLORS.npu },
    { key: 'memory', label: 'Memory', color: '#4ade80' },
    { key: 'disk', label: 'Disk', color: '#fbbf24' },
    { key: 'network', label: 'Network', color: '#22d3ee' },
  ]

  const systemHealthTrendData = (() => {
    const sources: Record<string, Array<number | null>> = {
      cpu: getSeries('util:cpu'),
      npu: getSeries('npu:util'),
      memory: getSeries('util:memory'),
      disk: busiestDisk ? getSeries(`disk:${busiestDisk[0]}:util`) : [],
      network: getSeries(networkNicCards.length === 1 ? `util:network:${networkNicCards[0].nicName}` : 'util:network'),
    }
    gpuTrendSeries.forEach(({ key, seriesKey }) => {
      sources[key] = getSeries(seriesKey)
    })
    const length = Math.max(1, ...Object.values(sources).map((series) => series.length))
    return Array.from({ length }, (_, index) => {
      const row: Record<string, number | null> = { index }
      for (const key of Object.keys(sources)) {
        row[key] = sources[key][index] ?? null
      }
      return row
    })
  })()

  const cpuSecondaryFrequency = isNumber(dynamicInfo?.cpu?.e_core_freq_mhz)
    ? { label: 'E-Core Freq', value: formatMetric(dynamicInfo.cpu.e_core_freq_mhz, 'MHz', 0) }
    : isNumber(dynamicInfo?.cpu?.lpe_core_freq_mhz)
      ? { label: 'LPE-Core Freq', value: formatMetric(dynamicInfo.cpu.lpe_core_freq_mhz, 'MHz', 0) }
      : { label: 'Logical Cores', value: formatPlain(staticInfo?.cpu?.core_count?.logical) }

  const coreGroups = useMemo<Array<{ label: string; type: 'P' | 'E' | 'LPE' | 'Core'; indices: number[] }>>(() => {
    const usage = dynamicInfo?.cpu?.per_core_usage || []
    const freqs = dynamicInfo?.cpu?.per_core_freq_mhz || []
    if (!usage.length) return [] as Array<{ label: string; type: 'P' | 'E' | 'LPE' | 'Core'; indices: number[] }>

    const p = dynamicInfo?.cpu?.p_core_indices || []
    const e = dynamicInfo?.cpu?.e_core_indices || []
    const lpe = dynamicInfo?.cpu?.lpe_core_indices || []

    if (p.length || e.length || lpe.length) {
      const groups: Array<{ label: string; type: 'P' | 'E' | 'LPE' | 'Core'; indices: number[] }> = []
      if (p.length) groups.push({ label: 'P-Cores', type: 'P', indices: p })
      if (e.length) groups.push({ label: 'E-Cores', type: 'E', indices: e })
      if (lpe.length) groups.push({ label: 'LPE-Cores', type: 'LPE', indices: lpe })
      return groups
    }

    return [
      {
        label: 'CPU Cores',
        type: 'Core',
        indices: usage.map((_, index) => index).filter((index) => index < freqs.length || freqs.length === 0),
      },
    ]
  }, [dynamicInfo])

  const coreFreqAxisMax = useMemo(() => {
    const staticMax = staticInfo?.cpu?.freq_mhz?.max_mhz
    const dynamicMax = Math.max(
      0,
      ...(dynamicInfo?.cpu?.per_core_freq_mhz || []).map((value) => (isNumber(value) ? value : 0))
    )
    const candidate = isNumber(staticMax) ? staticMax : dynamicMax
    if (!isNumber(candidate) || candidate <= 0) return 1000
    return Math.max(1000, Math.ceil(candidate / 100) * 100)
  }, [staticInfo?.cpu?.freq_mhz?.max_mhz, dynamicInfo?.cpu?.per_core_freq_mhz])

  const cpuStaticFreqText = cpuFreqRangeMeta

  const npuStaticFreqText = useMemo(
    () => summarizeFreqBounds(staticInfo?.npu?.freq_bounds_mhz),
    [staticInfo?.npu?.freq_bounds_mhz]
  )

  const networkPeakText = useMemo(
    () => formatNetworkSpeed(staticInfo?.network?.network_peak_mbps),
    [staticInfo?.network?.network_peak_mbps]
  )

  const networkSpeedMapText = useMemo(
    () => summarizeNetworkSpeeds(staticInfo?.network?.network_speeds_mbps),
    [staticInfo?.network?.network_speeds_mbps]
  )

  const diskStaticTotalText = useMemo(
    () => {
      const diskTotalGb = staticInfo?.disk?.total_size_gb
      return isNumber(diskTotalGb) ? `${diskTotalGb.toFixed(2)} GB` : 'N/A'
    },
    [staticInfo?.disk?.total_size_gb]
  )

  const diskStaticDevicesText = useMemo(
    () => `${staticInfo?.disk?.device_count ?? 0} device(s) | ${summarizeDiskSizes(staticInfo?.disk?.devices)}`,
    [staticInfo?.disk?.device_count, staticInfo?.disk?.devices]
  )

  const diskSizeLookup = useMemo(() => {
    const sizes: Record<string, number | null> = {}
    staticInfo?.disk?.devices?.forEach((device) => { sizes[device.name] = device.size_gb })
    return sizes
  }, [staticInfo?.disk?.devices])

  const diskSnapshotMeta = busiestDisk?.[0]
    ? `${busiestDisk[0]} | Total ${diskStaticTotalText} | ${staticInfo?.disk?.device_count ?? '?'} device(s)`
    : staticInfo?.disk
      ? `Total ${diskStaticTotalText} | ${staticInfo.disk.device_count ?? '?'} device(s)`
      : 'No static data'

  const hostContext = useMemo(() => {
    const osLabel = staticInfo?.os?.version?.replace(/\s*\([^)]*\)\s*$/, '').trim() || 'N/A'
    return {
      host: staticInfo?.host?.hostname || 'N/A',
      os: osLabel,
      kernel: staticInfo?.driver?.kernel_version || 'N/A',
      uptime: formatUptime(staticInfo?.host?.boot_time),
    }
  }, [staticInfo, dynamicInfo?.collected_at])

  const monitoredChangeMessage = useMemo(() => {
    if (!monitoredSectionsChange) return null
    const added = monitoredSectionsChange.added
      .map((name) => SECTION_LABELS[name] || name)
      .join(', ')
    const removed = monitoredSectionsChange.removed
      .map((name) => SECTION_LABELS[name] || name)
      .join(', ')
    const chunks: string[] = []
    if (added) chunks.push(`Enabled: ${added}`)
    if (removed) chunks.push(`Disabled: ${removed}`)
    return chunks.join(' | ')
  }, [monitoredSectionsChange])

  useEffect(() => {
    if (!monitoredSectionsChange || !monitoredChangeMessage) return
    publishNotice({
      title: 'Monitoring configuration updated',
      description: monitoredChangeMessage,
      scope: 'monitoring_sections',
      updatedAt: monitoredSectionsChange.updatedAt,
    })
    clearMonitoredSectionsChange()
  }, [
    monitoredSectionsChange,
    monitoredChangeMessage,
    clearMonitoredSectionsChange,
    publishNotice,
  ])

  return (
    <div className="perf-root">
      <div className="perf-ambient perf-ambient--blue" />
      <div className="perf-ambient perf-ambient--teal" />

      <div className="perf-header perf-header--sticky">
        <div className="perf-header-title">
          <Text className="perf-section-eyebrow">System Health</Text>
          {/* Only the host opens About; the rest is read-only context. Making
              every chip a link meant OS / kernel / uptime all navigated away. */}
          <div className="perf-host-context">
            <AntTooltip title="Open system information">
              <button
                type="button"
                className="perf-host-context-link"
                aria-label={`Open About for host ${hostContext.host}`}
                onClick={() => onNavigate?.('about')}
              >
                <b>Host</b>
                <span>{hostContext.host}</span>
                <InfoCircleOutlined className="perf-host-context-icon" aria-hidden="true" />
              </button>
            </AntTooltip>
            <span className="perf-host-context-facts">
              <span className="perf-host-context-item"><b>OS</b>{hostContext.os}</span>
              <span className="perf-host-context-sep" aria-hidden="true">-</span>
              <span className="perf-host-context-item"><b>Kernel</b>{hostContext.kernel}</span>
              <span className="perf-host-context-sep" aria-hidden="true">-</span>
              <span className="perf-host-context-item"><b>Uptime</b>{hostContext.uptime}</span>
            </span>
          </div>
        </div>
        <div className="perf-header-controls">
            <Space size={6}>
              <span style={REFRESH_INDICATOR_STYLE}>
                {loadingDynamic ? <Spin size="small" /> : <Badge status="processing" color={COLORS.green} />}
              </span>
              <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>Refresh</Text>
              <Segmented
                size="small"
                options={REFRESH_INTERVAL_OPTIONS}
                value={refreshIntervalMs}
                onChange={(value) => setRefreshIntervalMs(Number(value))}
              />
            </Space>
            <Space size={6}>
              <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>Trend</Text>
              <Segmented
                size="small"
                options={[
                  { label: 'Last 1 min', value: '1m' },
                  { label: 'Last 5 min', value: '5m' },
                ]}
                value={trendWindow}
                onChange={(value) => setTrendWindow(value as '1m' | '5m')}
              />
            </Space>
            {dynamicInfo?.collected_at && (
              <Tag style={{ fontSize: 10, color: COLORS.textMuted, borderColor: COLORS.border }}>
                {dynamicInfo.collected_at}
              </Tag>
            )}
        </div>
      </div>

      {errorStatic && (
        <Alert message="Static Info Error" description={errorStatic} type="warning" showIcon style={{ marginBottom: 12 }} />
      )}

      {errorDynamic && (
        <Alert message="Dynamic Info Error" description={errorDynamic} type="error" showIcon style={{ marginBottom: 12 }} />
      )}

      {quietActive && (
        <Alert
          message="Monitoring is paused for a benchmark run"
          description={
            <>
              Quiet mode is holding SmarTune's own collection down so the run competes with a
              constant machine. Everything below is the snapshot from just before it started,
              not the machine as it is now, and the trends are not being extended. This page
              refreshes itself when the run finishes.
              <br />
              The run is still recording the platform at 2&nbsp;Hz into its own metrics.csv. To
              watch live instead, turn Quiet mode off on the Benchmark tab — at the cost of that
              run's comparability.
            </>
          }
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
        />
      )}

      {!hasMonitoredSections && (
        <Alert
          message="No sections are being monitored"
          description="Dynamic monitoring is disabled (monitored_sections is empty in the server config), so live metrics polling and history collection are paused. Enable one or more sections in the configuration to resume."
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
        />
      )}

      <section className="perf-health-overview">
        <SystemHealthTrend
          data={systemHealthTrendData}
          trendWindow={trendWindow}
          series={trendSeriesMeta}
          onOpenHistory={() => onNavigate?.('history')}
        />
        {showPressureSection && (
          <Card className="perf-card perf-pressure-summary" bodyStyle={{ padding: 14 }}>
            <div className="perf-pressure-summary-head">
              <Text className="perf-pressure-summary-eyebrow">Pressure Overview</Text>
              <AntTooltip title="Configure pressure thresholds">
                <Button
                  type="text"
                  size="small"
                  className="perf-pressure-settings-action"
                  icon={<SlidersOutlined />}
                  aria-label="Configure pressure thresholds"
                  onClick={() => onOpenSettings?.('system_pressure')}
                />
              </AntTooltip>
            </div>
            <div className="perf-pressure-mini-list">
              <PressureMiniStat
                title="System Pressure"
                value={systemPressurePct}
                level={pressureLevel}
                subtitle={isNumber(pressureScore)
                  ? `Score: ${(pressureScore / 100).toFixed(2)}`
                  : undefined}
                help="Weighted composite score (PSI x resource usage)."
                zones={systemPressureZones}
              />
              <PressureMiniStat
                title="Disk IO Pressure"
                value={diskPressurePct}
                level={diskBusyLevelLabel}
                subtitle={[
                  diskBusyCount > 0 ? `Busy: ${diskBusyNames.join(', ')}` : null,
                  diskTotalCount > 0 ? `${diskBusyCount}/${diskTotalCount} disks busy` : null,
                ].filter(Boolean).join(' | ')}
                help="IO pressure severity; affected disks are listed in the subtitle."
                zones={diskPressureZones}
              />
              <PressureMiniStat
                title="Network Pressure"
                value={networkPressurePct}
                level={networkPressureLevelLabel}
                subtitle={[
                  networkPressureLevelLabel === 'CRITICAL' && networkWorstReason
                    ? `${networkWorstNic || ''}${networkWorstDirection ? ` (${networkWorstDirection})` : ''}: ${networkWorstReason}`
                    : (networkBusyCount > 0 && networkWorstNic ? `Busy: ${networkWorstNic}${networkWorstDirection ? ` (${networkWorstDirection})` : ''}` : null),
                  networkTotalCount > 0 ? `${networkBusyCount}/${networkTotalCount} NICs busy` : null,
                ].filter(Boolean).join(' | ')}
                help="Worst fused RX/TX pressure across NICs; affected NICs in the subtitle."
                zones={networkPressureZones}
              />
            </div>
          </Card>
        )}
      </section>

      <SectionTitle
        title="Resource Details"
        help="Current utilization and device state"
        action={(
          <Space size={4}>
            {loadingDynamic && !dynamicInfo ? <Spin size="small" /> : null}
            <AntTooltip title="Configure monitored resources">
              <Button
                type="text"
                size="small"
                className="perf-settings-shortcut"
                icon={<SettingOutlined />}
                aria-label="Configure monitored resources"
                onClick={() => onOpenSettings?.('overview')}
              />
            </AntTooltip>
          </Space>
        )}
      />

      <Row className="perf-insights-grid" gutter={[16, 16]} style={{ marginBottom: 24 }}>
        {showCpuSection && (
        <Col xs={24} md={12} xxl={8} className="perf-resource-order--cpu">
          <ResourceSummaryCard
            title="CPU"
            subtitle={cpuSnapshotMeta}
            accent={PERF_COLORS.cpu}
            status={!isNumber(cpuTrendValue) ? 'offline' : cpuBusy ? 'warning' : 'ok'}
            statusColor={cpuTrendStatusColor}
            primaryLabel="Util"
            primaryValue={cpuTrendValue}
            primaryUnit="%"
            metrics={[
              { label: 'P-Core Freq', value: formatMetric(dynamicInfo?.cpu?.p_core_freq_mhz, 'MHz', 0) },
              cpuSecondaryFrequency,
              { label: 'Temperature', value: formatMetric(dynamicInfo?.cpu?.temperature_c, '°C', 1) },
              { label: 'Pkg Power', value: formatMetric(dynamicInfo?.cpu?.pkg_power_w, 'W', 2) },
              { label: 'IOWait', value: formatMetric(dynamicInfo?.cpu?.iowait_total, '%', 1) },
            ]}
            series={getSeries('util:cpu')}
            trendWindow={trendWindow}
            statusReason={cpuBusy && isNumber(cpuTrendValue) ? `High load: ${cpuTrendValue.toFixed(1)}%` : undefined}
            onOpen={() => setDeviceDetail({ kind: 'cpu' })}
          />
        </Col>
        )}

        {showMemorySection && (
        <Col xs={24} md={12} xxl={8} className="perf-resource-order--memory">
          <ResourceSummaryCard
            title="Memory"
            subtitle={memorySnapshotMeta}
            accent={PERF_COLORS.memory}
            status={!isNumber(memoryTrendValue) ? 'offline' : memoryBusy ? 'warning' : 'ok'}
            statusColor={memoryTrendStatusColor}
            primaryLabel="Util"
            primaryValue={memoryTrendValue}
            primaryUnit="%"
            metrics={[
              { label: 'Available', value: formatMetric(dynamicInfo?.memory?.available_gb, 'GB', 1) },
              { label: 'Bandwidth', value: formatMetric(dynamicInfo?.memory?.bandwidth?.total_gb_s, 'GB/s', 1) },
              { label: 'Swap Used', value: formatMetric(dynamicInfo?.memory?.swap_used_gb, 'GB', 1) },
            ]}
            series={getSeries('util:memory')}
            trendWindow={trendWindow}
            statusReason={memoryBusy && isNumber(memoryTrendValue) ? `High usage: ${memoryTrendValue.toFixed(1)}%` : undefined}
            onOpen={() => setDeviceDetail({ kind: 'memory' })}
          />
        </Col>
        )}

        {showDiskSection && diskDevices.map(([diskName, diskData]) => (
          <Col xs={24} md={12} xxl={8} key={diskName} className="perf-resource-order--disk">
            <ResourceSummaryCard
              title={`Disk: ${diskName}`}
              subtitle={diskSizeLookup[diskName] != null ? `${formatMetric(diskSizeLookup[diskName], 'GB', 1)} total capacity` : undefined}
              accent={PERF_COLORS.disk}
              status={!isNumber(normalizePercent(diskData.utilization))
                ? 'offline'
                : diskData.is_busy && diskBusyLevelLabel.toUpperCase() === 'CRITICAL' ? 'critical'
                  : diskData.is_busy ? 'warning' : 'ok'}
              statusColor={diskData.is_busy && diskBusyLevelLabel.toUpperCase() === 'CRITICAL'
                ? COLORS.red
                : diskData.is_busy ? COLORS.orange : COLORS.green}
              primaryLabel="Util"
              primaryValue={normalizePercent(diskData.utilization)}
              primaryUnit="%"
              metrics={[
                { label: 'Used', value: formatMetric(diskData.used_size_gb, 'GB', 2) },
                { label: 'Read', value: formatMetric(diskData.read_kb_per_sec, 'KB/s', 1) },
                { label: 'Write', value: formatMetric(diskData.write_kb_per_sec, 'KB/s', 1) },
              ]}
              series={getSeries(`disk:${diskName}:util`)}
              trendWindow={trendWindow}
              statusReason={diskData.is_busy
                ? diskBusyLevelLabel.toUpperCase() === 'CRITICAL' ? 'Disk pressure critical' : 'Disk busy'
                : undefined}
              onOpen={() => setDeviceDetail({ kind: 'disk', name: diskName })}
            />
          </Col>
        ))}

        {showNetworkSection && (networkNicCards.length > 1 ? networkNicCards.map((nic) => {
          const networkState = getNetworkSummaryState(nic.pressure, nic.utilMax)
          return (
            <Col xs={24} md={12} xxl={8} key={`nic-${nic.nicName}`} className="perf-resource-order--network">
              <ResourceSummaryCard
                title={`Network: ${nic.nicName}`}
                subtitle={`Peak bandwidth ${formatNetworkSpeed(nic.bandwidth)}`}
                accent={PERF_COLORS.network}
                status={networkState.status}
                statusColor={networkState.color}
                primaryLabel="Util"
                primaryValue={nic.utilMax}
                primaryUnit="%"
                metrics={[
                  { label: 'Receive (RX)', value: formatBytesRate(nic.rxRate) },
                  { label: 'Send (TX)', value: formatBytesRate(nic.txRate) },
                  { label: 'Link Speed', value: formatNetworkSpeed(nic.bandwidth) },
                ]}
                series={getSeries(`util:network:${nic.nicName}`)}
                trendWindow={trendWindow}
                statusReason={networkState.reason}
                onOpen={() => setDeviceDetail({ kind: 'network', name: nic.nicName })}
              />
            </Col>
          )
        }) : (
          <Col xs={24} md={12} xxl={8} className="perf-resource-order--network">
            <ResourceSummaryCard
              title={networkNicCards.length === 1 ? `Network: ${networkNicCards[0].nicName}` : 'Network'}
              subtitle={networkNicCards.length === 1
                ? `Peak bandwidth ${formatNetworkSpeed(networkNicCards[0].bandwidth)}`
                : staticInfo?.network
                  ? [
                      `${formatPlain(staticInfo.network.nic_count ?? 0)} NIC(s)`,
                      staticInfo.network.network_speeds_mbps ? summarizeNetworkSpeeds(staticInfo.network.network_speeds_mbps) : null,
                    ].filter(Boolean).join(' | ')
                  : 'No data'
              }
              accent={PERF_COLORS.network}
              status={getNetworkSummaryState(
                networkNicCards.length === 1 ? networkNicCards[0].pressure : undefined,
                networkNicCards.length === 1 ? networkNicCards[0].utilMax : fallbackUtilMax,
              ).status}
              statusColor={getNetworkSummaryState(
                networkNicCards.length === 1 ? networkNicCards[0].pressure : undefined,
                networkNicCards.length === 1 ? networkNicCards[0].utilMax : fallbackUtilMax,
              ).color}
              primaryLabel="Util"
              primaryValue={networkNicCards.length === 1 ? networkNicCards[0].utilMax : fallbackUtilMax}
              primaryUnit="%"
              metrics={[
                { label: 'Receive (RX)', value: formatBytesRate(networkNicCards.length === 1 ? networkNicCards[0].rxRate : fallbackRxRate) },
                { label: 'Send (TX)', value: formatBytesRate(networkNicCards.length === 1 ? networkNicCards[0].txRate : fallbackTxRate) },
                { label: 'Link Speed', value: formatNetworkSpeed(networkNicCards.length === 1 ? networkNicCards[0].bandwidth : fbStaticPeakMbps) },
              ]}
              series={getSeries(networkNicCards.length === 1 ? `util:network:${networkNicCards[0].nicName}` : 'util:network')}
              trendWindow={trendWindow}
              statusReason={getNetworkSummaryState(
                networkNicCards.length === 1 ? networkNicCards[0].pressure : undefined,
                networkNicCards.length === 1 ? networkNicCards[0].utilMax : fallbackUtilMax,
              ).reason}
              onOpen={() => setDeviceDetail({ kind: 'network', name: networkNicCards.length === 1 ? networkNicCards[0].nicName : null })}
            />
          </Col>
        ))}

        {showNpuSection && (
        <Col xs={24} md={12} xxl={8} className="perf-resource-order--npu">
          <ResourceSummaryCard
            title="NPU"
            subtitle={npuSnapshotMeta}
            accent={PERF_COLORS.npu}
            status={!npuAvailable ? 'offline' : npuBusy ? 'warning' : 'ok'}
            statusColor={npuStatusColor}
            primaryLabel="Util"
            primaryValue={npuValue}
            primaryUnit="%"
            metrics={[
              { label: 'Power', value: npuParsed?.pmt_available !== false && npuParsed?.power_w != null ? `${(npuParsed.power_w as number).toFixed(2)} W` : 'N/A' },
              { label: 'Frequency', value: npuParsed?.frequency_mhz != null ? `${Math.round(npuParsed.frequency_mhz as number)} MHz` : 'N/A' },
              { label: 'Memory Used', value: npuParsed?.memory_bytes != null ? `${((npuParsed.memory_bytes as number) / (1024 * 1024)).toFixed(2)} MB` : 'N/A' },
            ]}
            series={getSeries('npu:util')}
            trendWindow={trendWindow}
            statusReason={npuBusy && isNumber(npuValue) ? `High load: ${npuValue.toFixed(1)}%` : undefined}
            onOpen={() => setDeviceDetail({ kind: 'npu' })}
          />
        </Col>
        )}

        {showGpuSection && [...gpuDevices].sort((a, b) => {
          const aIsIgpu = a.label === 'iGPU' ? 0 : 1
          const bIsIgpu = b.label === 'iGPU' ? 0 : 1
          return aIsIgpu - bIsIgpu
        }).map((device, index) => (
          <Col xs={24} md={12} xxl={8} key={device.id} className="perf-resource-order--gpu">
            <GpuSummaryCard
              device={device}
              accent={GPU_UTIL_COLORS[index % GPU_UTIL_COLORS.length]}
              utilizationSeries={getSeries(`gpu:${device.id}:util`)}
              trendWindow={trendWindow}
              onOpen={() => setDeviceDetail({ kind: 'gpu', deviceId: device.id })}
            />
          </Col>
        ))}

      </Row>

      <SectionTitle
        title="Workloads & Diagnostics"
        help="Who's consuming, what's flagged, what's controlled"
      />
      <div
        className={`perf-cockpit-grid${
          diagnosticsEnabled && balancerEnabled
            ? ''
            : diagnosticsEnabled || balancerEnabled
              ? ' perf-cockpit-grid--two'
              : ' perf-cockpit-grid--single'
        }`}
        style={{ marginBottom: 24 }}
      >
        <TopConsumersCard apps={topApps} onOpen={() => onNavigate?.('workloads')} />
        {(diagnosticsEnabled || balancerEnabled) && (
          <div className="perf-cockpit-side">
            {diagnosticsEnabled && (
              <DiagnosticsSummaryCard
                summary={diagSummary}
                status={diagSummaryStatus}
                onOpen={() => onOpenDiagnostics?.('1h')}
              />
            )}
            {balancerEnabled && (
              <ActiveControlsCard summary={controlSummary} onOpen={() => onNavigate?.('optimizer')} />
            )}
          </div>
        )}
      </div>

      {!dynamicInfo && (
        <div style={{ marginTop: 16 }}>
          <Spin size="small" />
        </div>
      )}

      {/* Device drill-down: the deep per-device telemetry opens here on demand
          instead of always occupying the page (design doc §7).  A centred modal
          rather than a side drawer -- the per-core and per-engine grids need
          most of the window's width to be readable.  The drawer class is kept so
          the existing light-theme rules for this content still apply. */}
      <Modal
        open={deviceDetail !== null}
        onCancel={() => setDeviceDetail(null)}
        width="min(1240px, 80vw)"
        centered
        footer={null}
        destroyOnClose
        rootClassName="perf-device-drawer perf-device-modal"
        styles={{ body: { padding: 16, maxHeight: '76vh', overflowY: 'auto' } }}
        title={deviceDetail?.kind === 'gpu'
          ? `${selectedGpuDevice?.displayLabel || 'GPU'} - device detail`
          : deviceDetail?.kind === 'disk'
            ? `${deviceDetail.name} - disk detail`
            : deviceDetail?.kind === 'network'
              ? `${deviceDetail.name || 'Network'} - network detail`
              : deviceDetail
                ? `${deviceDetail.kind.toUpperCase()} - device detail`
                : undefined}
      >
        {deviceDetail?.kind === 'cpu' && (
          <div className="perf-device-detail-stack">
            {coreGroups.length === 0 ? (
              <Card className="perf-card" bodyStyle={{ padding: 16 }}>
                <Text style={{ color: COLORS.textMuted }}>No per-core CPU data</Text>
              </Card>
            ) : coreGroups.map((group) => {
              const freqBounds = group.type === 'P'
                ? staticInfo?.cpu?.freq_mhz?.p_core_freq_mhz
                : group.type === 'E'
                  ? staticInfo?.cpu?.freq_mhz?.e_core_freq_mhz
                  : group.type === 'LPE'
                    ? staticInfo?.cpu?.freq_mhz?.lpe_core_freq_mhz
                    : null
              const frequencyRange = freqBounds
                ? formatFreqRange(freqBounds.min_mhz, freqBounds.max_mhz)
                : formatFreqRange(staticInfo?.cpu?.freq_mhz?.min_mhz, staticInfo?.cpu?.freq_mhz?.max_mhz)
              const groupCores = group.indices.map((coreIndex) => ({
                coreIndex,
                usage: dynamicInfo?.cpu?.per_core_usage?.[coreIndex] ?? null,
                freq: dynamicInfo?.cpu?.per_core_freq_mhz?.[coreIndex] ?? null,
                temp: dynamicInfo?.cpu?.per_core_temperature_c?.[coreIndex] ?? null,
                trend: getSeries(`cpu:core:${coreIndex}`),
                freqTrend: getSeries(`cpu:core_freq:${coreIndex}`),
              }))
              const frequencyValues = groupCores.flatMap((item) => [item.freq, ...item.freqTrend])
              const frequencyAxis = getAdaptiveAxis(frequencyValues, null, {
                lower: 0,
                upper: Math.max(1000, coreFreqAxisMax),
                minRange: 200,
                padding: 60,
                step: 20,
              })

              return (
                <Card key={group.label} className="perf-card perf-rise" bodyStyle={{ padding: 16 }}>
                  <div className="perf-device-detail-heading">
                    <Space size={8}>
                      <ThunderboltOutlined style={{ color: PERF_COLORS.cpu }} />
                      <Text style={{ color: COLORS.text, fontWeight: 600 }}>{group.label}</Text>
                      {frequencyRange && <Text style={{ color: COLORS.textMuted, fontSize: 10 }}>{frequencyRange}</Text>}
                    </Space>
                    <Tag style={{ fontSize: 10 }}>{group.indices.length} cores</Tag>
                  </div>
                  <div className="perf-core-grid">
                    {groupCores.map((core) => (
                      <CoreCell
                        key={`${group.label}-${core.coreIndex}`}
                        index={core.coreIndex}
                        usage={core.usage}
                        freq={core.freq}
                        temp={core.temp}
                        type={group.type}
                        trend={core.trend}
                        freqTrend={core.freqTrend}
                        trendWindow={trendWindow}
                        utilAxis={{ min: 0, max: 100 }}
                        freqAxis={frequencyAxis}
                      />
                    ))}
                  </div>
                </Card>
              )
            })}
          </div>
        )}
        {deviceDetail?.kind === 'memory' && (
          <TrendPanel
            title="Memory"
            accent={PERF_COLORS.memory}
            value={memoryTrendValue}
            unit="%"
            statusColor={memoryTrendStatusColor}
            series={getSeries('util:memory')}
            subtitle={memorySnapshotMeta}
            details={[
              { label: 'Available', value: formatMetric(dynamicInfo?.memory?.available_gb, 'GB', 1), source: 'dynamic' },
              { label: 'Total', value: formatMetric(dynamicInfo?.memory?.total_gb ?? staticInfo?.memory?.total_gb, 'GB', 1), source: 'dynamic' },
              { label: 'Swap Used', value: formatMetric(dynamicInfo?.memory?.swap_used_gb, 'GB', 1), source: 'dynamic' },
              { label: 'Swap Usage', value: formatPercent(dynamicInfo?.memory?.swap_usage_percent), source: 'dynamic' },
              { label: 'BW Total', value: formatMetric(dynamicInfo?.memory?.bandwidth?.total_gb_s, 'GB/s', 1), source: 'dynamic' },
              { label: 'BW Read', value: formatMetric(dynamicInfo?.memory?.bandwidth?.read_gb_s, 'GB/s', 1), source: 'dynamic' },
              { label: 'BW Write', value: formatMetric(dynamicInfo?.memory?.bandwidth?.write_gb_s, 'GB/s', 1), source: 'dynamic' },
            ]}
            sparkMode={sparkMode}
            trendWindow={trendWindow}
          />
        )}
        {deviceDetail?.kind === 'disk' && (() => {
          const diskData = dynamicInfo?.disk?.disk_io?.[deviceDetail.name]
          if (!diskData) return <Text style={{ color: COLORS.textMuted }}>No disk data</Text>
          return (
            <TrendPanel
              title={deviceDetail.name}
              accent={PERF_COLORS.disk}
              value={normalizePercent(diskData.utilization)}
              unit="%"
              statusColor={diskData.is_busy ? COLORS.red : COLORS.green}
              series={getSeries(`disk:${deviceDetail.name}:util`)}
              subtitle={diskSizeLookup[deviceDetail.name] != null ? `${formatMetric(diskSizeLookup[deviceDetail.name], 'GB', 1)} total capacity` : undefined}
              details={[
                { label: 'Size', value: formatMetric(diskSizeLookup[deviceDetail.name], 'GB', 2), source: 'static' },
                { label: 'Used', value: formatMetric(diskData.used_size_gb, 'GB', 2), source: 'dynamic' },
                { label: 'Capacity Usage', value: formatPercent(diskData.usage_percent), source: 'dynamic' },
                { label: 'Read', value: formatMetric(diskData.read_kb_per_sec, 'KB/s', 1), source: 'dynamic' },
                { label: 'Write', value: formatMetric(diskData.write_kb_per_sec, 'KB/s', 1), source: 'dynamic' },
                { label: 'Read IOPS', value: formatMetric(diskData.read_iops, 'IOPS', 1), source: 'dynamic' },
                { label: 'Write IOPS', value: formatMetric(diskData.write_iops, 'IOPS', 1), source: 'dynamic' },
              ]}
              sparkMode={sparkMode}
              trendWindow={trendWindow}
            />
          )
        })()}
        {deviceDetail?.kind === 'network' && (() => {
          const nic = deviceDetail.name ? networkNicCards.find((item) => item.nicName === deviceDetail.name) : null
          const rxRate = nic?.rxRate ?? fallbackRxRate
          const txRate = nic?.txRate ?? fallbackTxRate
          const utilization = nic?.utilMax ?? fallbackUtilMax
          const pressure = deviceDetail.name
            ? dynamicInfo?.pressure?.network_interfaces?.[deviceDetail.name]
            : null
          return (
            <TrendPanel
              title={deviceDetail.name || 'Network'}
              accent={PERF_COLORS.network}
              value={utilization}
              unit="%"
              statusColor={isNumber(utilization) && utilization >= BUSY_UTIL_THRESHOLD ? COLORS.red : PERF_COLORS.network}
              series={getSeries(deviceDetail.name ? `util:network:${deviceDetail.name}` : 'util:network')}
              subtitle={`Peak bandwidth ${formatNetworkSpeed(nic?.bandwidth ?? fbStaticPeakMbps)}`}
              details={[
                { label: 'Receive Bandwidth', value: formatBytesRate(rxRate), source: 'dynamic' },
                { label: 'Receive Utilization', value: formatPercent(nic?.rxUtil ?? fallbackRxUtil), source: 'dynamic' },
                { label: 'Receive Packet Loss', value: formatPercent(pressure?.rx?.drop_ratio_pct ?? null), source: 'dynamic' },
                { label: 'Receive Buffer Overflow', value: formatPercent(pressure?.rx?.hw_overflow_ratio_pct ?? null), source: 'dynamic' },
                { label: 'Send Bandwidth', value: formatBytesRate(txRate), source: 'dynamic' },
                { label: 'Send Utilization', value: formatPercent(nic?.txUtil ?? fallbackTxUtil), source: 'dynamic' },
                { label: 'Send Packet Loss', value: formatPercent(pressure?.tx?.drop_ratio_pct ?? null), source: 'dynamic' },
                { label: 'Send Buffer Overflow', value: formatPercent(pressure?.tx?.fifo_ratio_pct ?? null), source: 'dynamic' },
              ]}
              sparkMode={sparkMode}
              trendWindow={trendWindow}
            />
          )
        })()}
        {deviceDetail?.kind === 'npu' && (
          <NpuDetailCard
            npuParsed={npuParsed}
            npuName={staticInfo?.npu?.names?.[0] || 'Intel NPU'}
            npuFreqMinMhz={Object.values(staticInfo?.npu?.freq_bounds_mhz || {})[0]?.min_mhz ?? null}
            npuFreqMaxMhz={Object.values(staticInfo?.npu?.freq_bounds_mhz || {})[0]?.max_mhz ?? null}
            getSeries={getSeries}
            trendWindow={trendWindow}
          />
        )}
        {deviceDetail?.kind === 'gpu' && selectedGpuDevice && (
          <GpuDeviceCard
            device={selectedGpuDevice}
            trendWindow={trendWindow}
            getSeries={getSeries}
            sparkMode={sparkMode}
          />
        )}
      </Modal>
    </div>
  )
}

export default React.memo(SystemOverview)
