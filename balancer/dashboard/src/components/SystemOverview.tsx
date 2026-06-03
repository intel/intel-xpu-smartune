import React, { useCallback, useEffect, useId, useMemo, useState } from 'react'
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
} from 'antd'
import {
  ThunderboltOutlined,
  AlertOutlined,
  PartitionOutlined,
  DownOutlined,
  UpOutlined,
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
} from 'recharts'
import { COLORS, getPressureColor, getPressureLabel } from '../styles/theme'
import { api } from '../api/client'
import type {
  StaticInfoData,
  DynamicInfoData,
  GpuUsageDevice,
  GpuUsageFreq,
  DiskDeviceData,
} from '../api/types'
import { usePolling } from '../hooks/usePolling'
import '../styles/performance.css'

const { Text, Title } = Typography

// UI-selectable refresh intervals for dynamic_info polling.
// The backend pre-caches data every ~2 s, so polls at any interval are cheap.
const DEFAULT_REFRESH_INTERVAL_MS = 2000
const REFRESH_INTERVAL_OPTIONS = [
  { label: '1s', value: 1000 },
  { label: '2s', value: 2000 },
  { label: '3s', value: 3000 },
  { label: '5s', value: 5000 },
]
// Store enough points for 5 min at the fastest polling rate (1 s = 300 points)
const TREND_STORAGE_MAX_POINTS = 300
const ENGINE_ORDER = ['vcs', 'vecs', 'ccs', 'rcs', 'bcs'] as const
const MHZ_TO_GHZ = 1000
const REFRESH_INDICATOR_STYLE: React.CSSProperties = { display: 'inline-flex', width: 18, height: 18, alignItems: 'center', justifyContent: 'center', flexShrink: 0 }

const PERF_COLORS = {
  cpu: '#4cc9f0',
  memory: '#4ade80',
  disk: '#56c8d8',
  network: '#2dd4bf',
  gpu: '#5aa9ff',
  npu: '#7ae582',
  pressure: '#ff9f1c',
}

const GPU_UTIL_COLORS = [PERF_COLORS.gpu, PERF_COLORS.memory, PERF_COLORS.cpu, PERF_COLORS.network, PERF_COLORS.npu] as const

type TrendSeries = Record<string, Array<number | null>>
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

interface Props {
  active: boolean
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

function parseNpuRaw(raw: string | null | undefined): Record<string, unknown> | null {
  if (!raw) return null
  try { return JSON.parse(raw) as Record<string, unknown> } catch { return null }
}

function formatPlain(value: unknown): string {
  if (value === null || value === undefined || value === '') return 'N/A'
  if (Array.isArray(value)) return value.length ? value.join(', ') : 'N/A'
  return `${value}`
}

function getGpuStatus(available: boolean, throttled: boolean, utilization: number | null): { status: GpuStatus; color: string } {
  if (!available) return { status: 'Offline', color: COLORS.textMuted }
  if (throttled) return { status: 'Throttle', color: COLORS.orange }
  if (isNumber(utilization) && utilization >= 75) return { status: 'Busy', color: COLORS.red }
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

function summarizeFreqBounds(bounds?: Record<string, { min_mhz: number | null; max_mhz: number | null }>): string {
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
  const gpuUsageAvailable = Boolean(dynamicInfo?.gpu.gpu_usage.available)
  const gpuUsageDevices: GpuUsageDevice[] = dynamicInfo?.gpu.gpu_usage.parsed?.devices || []
  const dynamicVramEntries = Object.entries(dynamicInfo?.gpu.vram || {})
  const staticVramEntries = Object.entries(staticInfo?.gpu.vram || {})
  // Build BDF (short, without domain) -> lspci name lookup
  // e.g. "00:02.0" -> "00:02.0 VGA ... Intel Arc Graphics [8086:7d55]"
  const nameByBdf: Record<string, string> = {}
  ;(staticInfo?.gpu.names || []).forEach((line) => {
    const m = line.match(/^([0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])/i)
    if (m) nameByBdf[m[1].toLowerCase()] = line
  })

  // Build reverse map: pci_address -> cardKey (e.g. "0000:00:02.0" -> "card0")
  const pciToCardKey: Record<string, string> = {}
  Object.entries(staticInfo?.gpu.pci_addresses || {}).forEach(([cardKey, pciAddr]) => {
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
      const sortedKeys = Object.keys(staticInfo?.gpu.pci_addresses || {}).sort()
      const fallbackKey = sortedKeys[idx]
      if (fallbackKey) gpuUsageByCardKey[fallbackKey] = qdev
    }
  })

  const cardKeySet = new Set<string>()
  Object.keys(staticInfo?.gpu.vram || {}).forEach((k) => cardKeySet.add(k))
  Object.keys(staticInfo?.gpu.freq_bounds_mhz || {}).forEach((k) => cardKeySet.add(k))
  Object.keys(staticInfo?.gpu.pcie || {}).forEach((k) => cardKeySet.add(k))
  Object.keys(staticInfo?.gpu.engines || {}).forEach((k) => cardKeySet.add(k))
  Object.keys(staticInfo?.gpu.pci_addresses || {}).forEach((k) => cardKeySet.add(k))
  gpuUsageDevices.forEach((_, idx) => { if (!staticInfo) cardKeySet.add(`card${idx}`) })

  const staticCardKeys = Array.from(cardKeySet).sort()
  const total = Math.max(
    gpuUsageDevices.length,
    dynamicVramEntries.length,
    staticVramEntries.length,
    staticCardKeys.length,
    staticInfo?.gpu.count || 0,
  )

  const devices: GpuDeviceView[] = []
  let dgpuCounter = 0

  for (let index = 0; index < total; index += 1) {
    const cardKey = staticCardKeys[index] || dynamicVramEntries[index]?.[0] || staticVramEntries[index]?.[0] || `card${index}`
    const qdev = gpuUsageByCardKey[cardKey] || gpuUsageDevices[index]

    const hasStaticCard = Boolean(staticCardKeys[index])
    const hasDynamicCard = Boolean(dynamicVramEntries[index]?.[0] || staticVramEntries[index]?.[0])
    const withinStaticCount = Boolean((staticInfo?.gpu.count || 0) > index)
    const hasEvidence = Boolean(qdev || hasStaticCard || hasDynamicCard || withinStaticCount)
    if (!hasEvidence) continue

    const vramDyn = dynamicInfo?.gpu.vram?.[cardKey] || dynamicVramEntries[index]?.[1]
    const vramStatic = staticInfo?.gpu.vram?.[cardKey] || staticVramEntries[index]?.[1]
    const vramUsage = normalizePercent(vramDyn?.usage_percent ?? vramStatic?.usage_percent ?? null)

    // Determine iGPU vs dGPU. Authoritative source: Intel iGPU is always at
    // bus 00, device 02 (e.g. 0000:00:02.0). Fall back to qdev.dev_type when
    // the PCI address is unavailable.
    const pciAddr = (staticInfo?.gpu.pci_addresses?.[cardKey] || qdev?.pci_dev || '').toLowerCase()
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
    ;[...(qdev?.engines || []), ...((staticInfo?.gpu.engines?.[cardKey] || []) as string[])].forEach((name) => {
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
    const cardPciAddr = staticInfo?.gpu.pci_addresses?.[cardKey]  // e.g. "0000:03:00.0"
    const shortBdf = cardPciAddr ? cardPciAddr.replace(/^[0-9a-f]{4}:/i, '').toLowerCase() : null
    const staticName = (shortBdf && nameByBdf[shortBdf]) || staticInfo?.gpu.names[index]
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
      pci: qdev?.pci_dev || staticInfo?.gpu.pcie?.[cardKey]?.current_speed || 'N/A',
      driver: qdev?.drv_name || 'N/A',
      utilization,
      frequencies: { gt0, gt1 },
      freqBounds: {
        min_mhz: staticInfo?.gpu.freq_bounds_mhz?.[cardKey]?.min_mhz ?? null,
        max_mhz: staticInfo?.gpu.freq_bounds_mhz?.[cardKey]?.max_mhz ?? null,
      },
      gtFreqBounds: {
        gt0: staticInfo?.gpu.gt_freq_bounds_mhz?.[cardKey]?.gt0,
        gt1: staticInfo?.gpu.gt_freq_bounds_mhz?.[cardKey]?.gt1,
      },
      powerGpu: qdev?.power_w?.gpu ?? null,
      powerPkg: qdev?.power_w?.pkg ?? qdev?.power_w?.card ?? null,
      vramUsage,
      euCount: staticInfo?.gpu.eu_count?.[cardKey] ?? null,
      pciId,
      pcieLink: {
        current_speed: staticInfo?.gpu.pcie?.[cardKey]?.current_speed ?? null,
        current_width: staticInfo?.gpu.pcie?.[cardKey]?.current_width ?? null,
        max_speed: staticInfo?.gpu.pcie?.[cardKey]?.max_speed ?? null,
        max_width: staticInfo?.gpu.pcie?.[cardKey]?.max_width ?? null,
      },
      engines,
      engineInstances: ((staticInfo?.gpu.engines?.[cardKey] || []) as string[]).slice(),
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
}) {
  const id = useId()
  const cleaned = data.map((value) => (isNumber(value) ? value : null))
  const numeric = cleaned.filter(isNumber)
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
            <line x1={chartLeft} y1={chartTop} x2={chartLeft} y2={chartBottom} stroke={COLORS.border} strokeWidth="1" />
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

  const pointCoords = normalized.map((value, index) => {
    const plottedValue = Math.max(min, Math.min(value, clampedMax))
    const x = chartLeft + (index / denominator) * chartWidth
    const y = chartTop + chartHeight - ((plottedValue - min) / range) * chartHeight
    return { x, y, value, index }
  })

  const points = pointCoords.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`)

  const linePath = `M ${points.join(' L ')}`
  const areaPath = `${linePath} L ${chartRight} ${chartBottom} L ${chartLeft} ${chartBottom} Z`
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
          <stop offset="0%" stopColor={stroke} stopOpacity="0.4" />
          <stop offset="100%" stopColor={stroke} stopOpacity="0" />
        </linearGradient>
      </defs>
      {hasAxis && (
        <>
          <line x1={chartLeft} y1={chartTop} x2={chartLeft} y2={chartBottom} stroke={COLORS.border} strokeWidth="1" />
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
        />
      )}
    </svg>
  )
}

/** Hover overlay for sparkline — renders crosshair + value tooltip on mouse move */
function SparklineHoverOverlay({
  chartLeft, chartTop, chartWidth, chartHeight, pointCoords, stroke, width, height,
}: {
  chartLeft: number; chartTop: number; chartWidth: number; chartHeight: number
  pointCoords: Array<{ x: number; y: number; value: number; index: number }>
  stroke: string; width: number; height: number
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

  return (
    <>
      {hp && (
        <>
          <line x1={hp.x} y1={chartTop} x2={hp.x} y2={chartTop + chartHeight}
            stroke={stroke} strokeWidth="1" strokeDasharray="3 2" opacity="0.6" />
          <circle cx={hp.x} cy={hp.y} r="3" fill={stroke} stroke="#fff" strokeWidth="1" />
          <rect
            x={hp.x + (hp.x > chartLeft + chartWidth * 0.75 ? -46 : 6)}
            y={Math.max(chartTop, hp.y - 10)}
            width="40" height="16" rx="3"
            fill="rgba(15,17,23,0.88)" stroke={stroke} strokeWidth="0.5"
          />
          <text
            x={hp.x + (hp.x > chartLeft + chartWidth * 0.75 ? -26 : 26)}
            y={Math.max(chartTop + 5, hp.y - 2)}
            textAnchor="middle"
            dominantBaseline="middle"
            fill="#e4ecff" fontSize="8" fontFamily="monospace"
          >
            {hp.value.toFixed(1)}
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
      return { ...item, values: [] as number[] }
    }
    let lastValue = seed
    const values = cleaned.map((value) => {
      if (!isNumber(value)) return lastValue
      lastValue = value
      return value
    })
    return { ...item, values }
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
    const points = item.values.map((value, index) => {
      const plotted = Math.max(axisMin, Math.min(value, axisMax))
      const x = chartLeft + (index / denominator) * chartWidth
      const y = chartTop + chartHeight - ((plotted - axisMin) / range) * chartHeight
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
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
    let lastValue = seed
    const range = maxVal - minVal
    const denominator = Math.max(1, maxLen - 1)

    const points = series.map((value, index) => {
      const nextValue = isNumber(value) ? value : lastValue
      lastValue = nextValue
      const plotted = Math.max(minVal, Math.min(nextValue, maxVal))
      const x = chartLeft + (index / denominator) * chartWidth
      const y = chartTop + chartHeight - ((plotted - minVal) / range) * chartHeight
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })

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
  action,
}: {
  title: string
  subtitle?: string
  action?: React.ReactNode
}) {
  return (
    <div className="perf-section-title">
      <div>
        <Text className="perf-section-eyebrow">{title}</Text>
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
  const color = getPressureColor(normalized)
  const pointerColor = COLORS.text
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
              background={{ fill: `${COLORS.border}aa` }}
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

function TrendPanel({
  title,
  accent,
  value,
  unit,
  status,
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
  hideTotalBar,
  primaryChartLabel,
}: {
  title: string
  accent: string
  value: number | null
  unit?: string
  status?: string
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
        <Space size={6}>
          {status && (
            <Tag
              style={{
                color: statusColor || accent,
                borderColor: statusColor || accent,
                background: `${statusColor || accent}22`,
                fontSize: 10,
                letterSpacing: 1,
              }}
            >
              {status}
            </Tag>
          )}
        </Space>
      </div>
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
                {splitBars.map((bar) => {
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
  type: 'P' | 'E' | 'Core'
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
                  stroke={COLORS.accent} dot={false} strokeWidth={2} isAnimationActive={false}
                  hide={hidden.has('npuUtil')} />
                <Line yAxisId="freq" type="monotone" dataKey="freqMhz" name="NPU Freq MHz"
                  stroke={PERF_COLORS.npu} dot={false} strokeWidth={2} isAnimationActive={false}
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
                  stroke={'#4cc9f0'} dot={false} strokeWidth={2} isAnimationActive={false}
                  hide={hidden.has('npuPower')} />
                <Line yAxisId="bw" type="monotone" dataKey="ddrBw" name="DDR BW MiB/s"
                  stroke={'#cbd5e1'} strokeDasharray="5 3" dot={false} strokeWidth={2} isAnimationActive={false}
                  hide={hidden.has('ddrBw')} />
                <Line yAxisId="left" type="monotone" dataKey="npuTemp" name="Temperature °C"
                  stroke={'#fbbf24'} dot={false} strokeWidth={2} isAnimationActive={false}
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
  const enginesToShow = device.engines.length
    ? device.engines
    : ENGINE_ORDER.filter((engine) => isNumber(device.engineUtil[engine]))

  const gtFreqItems = [
    { key: `${device.id}-freq-gt0-act`, label: 'GT0 Act', value: device.frequencies.gt0?.act_mhz ?? null, stroke: '#cbd5e1' },
    { key: `${device.id}-freq-gt0-req`, label: 'GT0 Req', value: device.frequencies.gt0?.cur_mhz ?? null, stroke: '#cbd5e1' },
    { key: `${device.id}-freq-gt1-act`, label: 'GT1 Act', value: device.frequencies.gt1?.act_mhz ?? null, stroke: '#34d399' },
    { key: `${device.id}-freq-gt1-req`, label: 'GT1 Req', value: device.frequencies.gt1?.cur_mhz ?? null, stroke: '#34d399' },
  ].filter((s) => isNumber(s.value))

  const pkgLabel = device.label === 'iGPU' ? 'Pkg' : 'Card'
  const powerSeries = [
    { key: `${device.id}-power-gpu`, label: 'GPU', data: gpuPowerSeries, stroke: PERF_COLORS.cpu },
    { key: `${device.id}-power-pkg`, label: pkgLabel, data: pkgPowerSeries, stroke: PERF_COLORS.pressure },
  ]

  const engineSeries = enginesToShow.map((engine) => {
    const cnt = device.engineInstances.filter((n) => normalizeEngineName(n) === engine).length
    return {
      key: `${device.id}-engine-${engine}`,
      label: cnt > 1 ? `${engine.toUpperCase()}\u00d7${cnt}` : engine.toUpperCase(),
      data: getSeries(`gpu:${device.id}:engine:${engine}`),
      stroke: ENGINE_COLORS[engine as EngineKey] ?? PERF_COLORS.gpu,
    }
  })

  const [hidden, setHidden] = useState<Set<string>>(new Set())
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
            <Tag
              style={{
                color: device.statusColor,
                borderColor: device.statusColor,
                background: `${device.statusColor}22`,
                fontSize: 10,
                letterSpacing: 1,
              }}
            >
              {device.status}
            </Tag>
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
                  <Text style={{ color: ENGINE_COLORS[engine as EngineKey] ?? PERF_COLORS.gpu, fontSize: 11, fontWeight: 600 }}>
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
            { key: 'gt0Act', name: 'GT0 Act MHz', color: '#cbd5e1' },
            { key: 'gt0Req', name: 'GT0 Req MHz', color: '#cbd5e1', dasharray: '6 4' },
            { key: 'gt1Act', name: 'GT1 Act MHz', color: '#34d399' },
            { key: 'gt1Req', name: 'GT1 Req MHz', color: '#34d399', dasharray: '4 4' },
            { key: 'gt0Rc6', name: 'GT0 RC6 %', color: '#2dd4bf', dasharray: '3 2' },
            { key: 'gt1Rc6', name: 'GT1 RC6 %', color: '#a3e635', dasharray: '3 2' },
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
                    <YAxis yAxisId="mhz" orientation="right" domain={[0, (max: number) => Math.max(max, 1)]} tick={{ fill: COLORS.textMuted, fontSize: 10 }} tickFormatter={(v) => `${v}`} width={56}
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
                        stroke={s.stroke} dot={false} strokeWidth={2} isAnimationActive={false}
                        hide={hidden.has(s.key)} />
                    ))}
                    <Line yAxisId="pct" type="monotone" dataKey="mem" name={memLabel}
                      stroke={PERF_COLORS.memory} strokeDasharray="5 3" dot={false} strokeWidth={2} isAnimationActive={false}
                      hide={hidden.has('mem')} />
                    <Line yAxisId="mhz" type="monotone" dataKey="gt0Act" name="GT0 Act MHz"
                      stroke={'#cbd5e1'} dot={false} strokeWidth={2} isAnimationActive={false}
                      hide={hidden.has('gt0Act')} />
                    <Line yAxisId="mhz" type="monotone" dataKey="gt0Req" name="GT0 Req MHz"
                      stroke={'#cbd5e1'} strokeDasharray="6 4" dot={false} strokeWidth={2} isAnimationActive={false}
                      hide={hidden.has('gt0Req')} />
                    <Line yAxisId="mhz" type="monotone" dataKey="gt1Act" name="GT1 Act MHz"
                      stroke={'#34d399'} dot={false} strokeWidth={2} isAnimationActive={false}
                      hide={hidden.has('gt1Act')} />
                    <Line yAxisId="mhz" type="monotone" dataKey="gt1Req" name="GT1 Req MHz"
                      stroke={'#34d399'} strokeDasharray="4 4" dot={false} strokeWidth={2} isAnimationActive={false}
                      hide={hidden.has('gt1Req')} />
                    <Line yAxisId="pct" type="monotone" dataKey="gt0Rc6" name="GT0 RC6 %"
                      stroke={'#2dd4bf'} strokeDasharray="3 2" dot={false} strokeWidth={2} isAnimationActive={false}
                      hide={hidden.has('gt0Rc6')} />
                    <Line yAxisId="pct" type="monotone" dataKey="gt1Rc6" name="GT1 RC6 %"
                      stroke={'#a3e635'} strokeDasharray="3 2" dot={false} strokeWidth={2} isAnimationActive={false}
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
            { key: 'gpuPower', name: 'GPU Power W', color: '#4cc9f0' },
            { key: 'pkgPower', name: `${pkgLabel} Power W`, color: '#ff9f1c', dasharray: '5 3' },
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
                      stroke={'#4cc9f0'} dot={false} strokeWidth={2} isAnimationActive={false}
                      hide={hidden.has('gpuPower')} />
                    <Line type="monotone" dataKey="pkgPower" name={`${pkgLabel} Power W`}
                      stroke={'#ff9f1c'} strokeDasharray="5 3" dot={false} strokeWidth={2} isAnimationActive={false}
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

export default function SystemOverview({ active }: Props) {
  const [staticInfo, setStaticInfo] = useState<StaticInfoData | null>(null)
  const [dynamicInfo, setDynamicInfo] = useState<DynamicInfoData | null>(null)
  const [loadingDynamic, setLoadingDynamic] = useState(false)
  const [errorStatic, setErrorStatic] = useState<string | null>(null)
  const [errorDynamic, setErrorDynamic] = useState<string | null>(null)

  const [refreshIntervalMs, setRefreshIntervalMs] = useState<number>(DEFAULT_REFRESH_INTERVAL_MS)
  const [trendWindow, setTrendWindow] = useState<'1m' | '5m'>('1m')
  const [gpuFilter, setGpuFilter] = useState<string>('all')
  const sparkMode: SparkMode = 'axis'
  const [showCpuDetails, setShowCpuDetails] = useState(true)
  const [showGpuDetails, setShowGpuDetails] = useState(true)
  const [showNpuDetails, setShowNpuDetails] = useState(true)
  const [trends, setTrends] = useState<TrendSeries>({})

  const pushTrendPoints = useCallback((updates: Record<string, number | null>) => {
    setTrends((prev) => {
      const next: TrendSeries = { ...prev }
      Object.entries(updates).forEach(([key, value]) => {
        const current = prev[key] || []
        const updated = [...current, value]
        if (updated.length > TREND_STORAGE_MAX_POINTS) {
          updated.splice(0, updated.length - TREND_STORAGE_MAX_POINTS)
        }
        next[key] = updated
      })
      return next
    })
  }, [])

  const fetchStatic = useCallback(async () => {
    if (!active) return
    try {
      const data = await api.getStaticInfo()
      setStaticInfo(data)
      setErrorStatic(null)
    } catch (e: unknown) {
      setErrorStatic(e instanceof Error ? e.message : 'Failed to fetch static info')
    }
  }, [active])

  const fetchDynamic = useCallback(async () => {
    setLoadingDynamic(true)
    try {
      const data = await api.getDynamicInfo()
      setDynamicInfo(data)
      setErrorDynamic(null)

      const updates: Record<string, number | null> = {
        'pressure:score': isNumber(data.pressure?.score) ? data.pressure.score! * 100 : null,
        'npu:availability': data.npu.npu_smi.available ? 100 : 0,
        'cpu:p': normalizePercent(data.cpu.p_core_usage),
        'cpu:e': normalizePercent(data.cpu.e_core_usage),
        'cpu:lpe': normalizePercent(data.cpu.lpe_core_usage),
        'util:cpu': normalizePercent(data.cpu.usage_total),
        'util:memory': normalizePercent(data.memory.usage_percent),
      }

      const npuRawParsed = parseNpuRaw(data.npu.npu_smi.raw)
      if (npuRawParsed) {
        updates['npu:util'] = typeof npuRawParsed.utilization_percent === 'number' ? normalizePercent(npuRawParsed.utilization_percent as number) : null
        updates['npu:power_w'] = typeof npuRawParsed.power_w === 'number' ? npuRawParsed.power_w : null
        updates['npu:freq_mhz'] = typeof npuRawParsed.frequency_mhz === 'number' ? npuRawParsed.frequency_mhz : null
        updates['npu:temp_c'] = typeof npuRawParsed.temperature_c === 'number' ? npuRawParsed.temperature_c : null
        updates['npu:bandwidth_mib'] = typeof npuRawParsed.noc_bandwidth_mib_per_s === 'number' ? npuRawParsed.noc_bandwidth_mib_per_s : null
        updates['npu:memory_mb'] = typeof npuRawParsed.memory_bytes === 'number' ? (npuRawParsed.memory_bytes as number) / (1024 * 1024) : null
      }

      updates['pressure:peak'] = updates['pressure:score'] ?? null

      data.cpu.per_core_usage.forEach((value, index) => {
        updates[`cpu:core:${index}`] = normalizePercent(value)
        updates[`cpu:core_freq:${index}`] = isNumber(data.cpu.per_core_freq_mhz?.[index])
          ? data.cpu.per_core_freq_mhz[index]
          : null
      })

      const dynamicDiskDevices = Object.values(data.disk?.disk_io || {})
      updates['util:disk'] = dynamicDiskDevices.length
        ? Math.max(...dynamicDiskDevices.map((disk) => normalizePercent(disk.utilization) || 0))
        : null

      Object.entries(data.disk?.disk_io || {}).forEach(([diskName, diskData]) => {
        updates[`disk:${diskName}:util`] = normalizePercent(diskData.utilization)
      })

      // Per-NIC trend data for each valid physical NIC
      const validNics = staticInfo?.io.valid_nics || []
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
        const fallbackRx = isNumber(totalNet?.rx_bytes_per_sec) ? totalNet.rx_bytes_per_sec : null
        const fallbackTx = isNumber(totalNet?.tx_bytes_per_sec) ? totalNet.tx_bytes_per_sec : null
        const rxMbps = toMbps(fallbackRx)
        const txMbps = toMbps(fallbackTx)
        updates['bw:network:rx_mbps'] = rxMbps
        updates['bw:network:tx_mbps'] = txMbps
        const staticPeakMbps = isNumber(staticInfo?.io.network_peak_mbps) && staticInfo.io.network_peak_mbps > 0
          ? staticInfo.io.network_peak_mbps : null
        const rxUtil = isNumber(rxMbps) && isNumber(staticPeakMbps) && staticPeakMbps > 0 ? Math.min(rxMbps / staticPeakMbps * 100, 100) : null
        const txUtil = isNumber(txMbps) && isNumber(staticPeakMbps) && staticPeakMbps > 0 ? Math.min(txMbps / staticPeakMbps * 100, 100) : null
        updates['util:network:rx'] = rxUtil
        updates['util:network:tx'] = txUtil
        const fallbackUtils = [rxUtil, txUtil].filter(isNumber)
        if (fallbackUtils.length) allNetworkUtils.push(Math.max(...fallbackUtils))
      }
      updates['util:network'] = allNetworkUtils.length ? Math.max(...allNetworkUtils) : null

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

      pushTrendPoints(updates)
    } catch (e: unknown) {
      setErrorDynamic(e instanceof Error ? e.message : 'Failed to fetch dynamic info')
    } finally {
      setLoadingDynamic(false)
    }
  }, [pushTrendPoints, staticInfo])

  useEffect(() => {
    fetchStatic()
  }, [fetchStatic])

  usePolling(fetchDynamic, refreshIntervalMs, true)

  const gpuDevices = useMemo(() => buildGpuDevices(staticInfo, dynamicInfo), [staticInfo, dynamicInfo])

  useEffect(() => {
    if (gpuFilter !== 'all' && !gpuDevices.some((d) => d.id === gpuFilter)) {
      setGpuFilter('all')
    }
  }, [gpuFilter, gpuDevices])

  // Trend window: number of points = window duration / polling interval
  const trendPoints = useMemo(() => {
    const windowMs = trendWindow === '1m' ? 60_000 : 5 * 60_000
    return Math.max(1, Math.round(windowMs / refreshIntervalMs))
  }, [trendWindow, refreshIntervalMs])

  const getSeries = useCallback(
    (key: string) => {
      const values = trends[key] || []
      return values.slice(-trendPoints)
    },
    [trends, trendPoints],
  )

  const filteredGpuDevices = useMemo(() => {
    if (gpuFilter === 'all') return gpuDevices
    return gpuDevices.filter((d) => d.id === gpuFilter)
  }, [gpuDevices, gpuFilter])

  const validNics = useMemo(() => staticInfo?.io.valid_nics || [], [staticInfo?.io.valid_nics])

  // Per-NIC series helper: generates utilization and bandwidth series for a given NIC name.
  // When nicName is null, uses the legacy fallback keys (util:network:rx, bw:network:rx_mbps).
  const getNetworkNicSeries = useCallback((nicName: string | null) => {
    const rxUtilKey = nicName ? `util:network:${nicName}:rx` : 'util:network:rx'
    const txUtilKey = nicName ? `util:network:${nicName}:tx` : 'util:network:tx'
    const rxBwKey = nicName ? `bw:network:${nicName}:rx_mbps` : 'bw:network:rx_mbps'
    const txBwKey = nicName ? `bw:network:${nicName}:tx_mbps` : 'bw:network:tx_mbps'

    const utilSeries = [
      { key: `${nicName || 'net'}-rx-util`, label: 'RX Util %', data: getSeries(rxUtilKey), stroke: PERF_COLORS.network },
      { key: `${nicName || 'net'}-tx-util`, label: 'TX Util %', data: getSeries(txUtilKey), stroke: PERF_COLORS.gpu },
    ]
    const bwSeries = [
      { key: `${nicName || 'net'}-rx-bw-kbps`, label: 'RX BW Kb/s', data: getSeries(rxBwKey).map((v) => (isNumber(v) ? v * 1000 : null)), stroke: PERF_COLORS.network },
      { key: `${nicName || 'net'}-tx-bw-kbps`, label: 'TX BW Kb/s', data: getSeries(txBwKey).map((v) => (isNumber(v) ? v * 1000 : null)), stroke: PERF_COLORS.gpu },
    ]
    const bwValues = [...bwSeries[0].data, ...bwSeries[1].data].filter(isNumber)
    const bwMax = bwValues.length ? Math.max(...bwValues) : 0
    const bwAxisMax = Math.max(100, Math.ceil(bwMax / 100) * 100)

    return { utilSeries, bwSeries, bwAxisMax }
  }, [getSeries])

  // Legacy series (used when no valid NICs or for fallback)
  const networkUtilizationSeries = useMemo(
    () => getNetworkNicSeries(validNics.length === 1 ? validNics[0].name : null).utilSeries,
    [getNetworkNicSeries, validNics],
  )
  const networkBandwidthKbpsSeries = useMemo(
    () => getNetworkNicSeries(validNics.length === 1 ? validNics[0].name : null).bwSeries,
    [getNetworkNicSeries, validNics],
  )
  const networkBandwidthKbpsAxisMax = useMemo(() => {
    const values = [...networkBandwidthKbpsSeries[0].data, ...networkBandwidthKbpsSeries[1].data].filter(isNumber)
    const dynamicMax = values.length ? Math.max(...values) : 0
    if (dynamicMax <= 0) return 100
    return Math.max(100, Math.ceil(dynamicMax / 100) * 100)
  }, [networkBandwidthKbpsSeries])

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
    if (isNumber(gpuAggregateUtil) && gpuAggregateUtil >= 80) return 'Busy'
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
    const pf = staticInfo?.cpu.freq_mhz.p_core_freq_mhz
    const ef = staticInfo?.cpu.freq_mhz.e_core_freq_mhz
    const lf = staticInfo?.cpu.freq_mhz.lpe_core_freq_mhz
    const overall = formatFreqRange(staticInfo?.cpu.freq_mhz.min_mhz, staticInfo?.cpu.freq_mhz.max_mhz)
    const parts: string[] = []
    if (pf) parts.push(`P: ${formatFreqRange(pf.min_mhz, pf.max_mhz)}`)
    if (ef) parts.push(`E: ${formatFreqRange(ef.min_mhz, ef.max_mhz)}`)
    if (lf) parts.push(`LPE: ${formatFreqRange(lf.min_mhz, lf.max_mhz)}`)
    return parts.length ? parts.join(' / ') : overall
  }, [staticInfo?.cpu.freq_mhz])

  const cpuSnapshotMeta = staticInfo?.cpu.model_name
    ? `${staticInfo.cpu.core_count.logical} cores | ${cpuFreqRangeMeta} | ${staticInfo.cpu.model_name}`
    : dynamicInfo?.cpu.per_core_usage?.length
      ? `${dynamicInfo.cpu.per_core_usage.length} cores`
      : 'No data'

  const memorySnapshotMeta = useMemo(() => {
    if (staticInfo?.memory) {
      const parts: string[] = []
      if (isNumber(staticInfo.memory.total_gb)) parts.push(`${staticInfo.memory.total_gb.toFixed(1)} GB`)
      // Memory type (e.g. LPDDR5) from devices or ddr_speeds
      const memTypes = [...new Set(
        (staticInfo.memory.devices?.devices ?? []).map((d) => d.type).filter(Boolean) as string[]
      )]
      if (memTypes.length) parts.push(memTypes.join('/'))
      // Prefer actual configured speed over rated speed
      const firstDev = staticInfo.memory.devices?.devices?.[0]
      const configuredSpeed = firstDev?.configured_speed
      const speedStr = (configuredSpeed && configuredSpeed !== 'Unknown')
        ? configuredSpeed
        : (staticInfo.memory.ddr_speeds.length ? staticInfo.memory.ddr_speeds.join('/') : null)
      if (speedStr) parts.push(speedStr)
      else if (!memTypes.length) parts.push('DDR N/A')
      return parts.join(' | ')
    }
    return isNumber(dynamicInfo?.memory.total_gb)
      ? `${dynamicInfo.memory.total_gb.toFixed(1)} GB total`
      : 'No data'
  }, [staticInfo?.memory, dynamicInfo?.memory.total_gb])

  // Build per-NIC render data
  const networkNicCards = useMemo(() => {
    if (!validNics.length) return []
    return validNics.map((nic) => {
      const nicName = nic.name
      const nicData = dynamicInfo?.network?.interfaces?.[nicName]
      const rxRate = isNumber(nicData?.rx_bytes_per_sec) ? nicData.rx_bytes_per_sec : null
      const txRate = isNumber(nicData?.tx_bytes_per_sec) ? nicData.tx_bytes_per_sec : null
      const bandwidth = nic.speed_mbps > 0 ? nic.speed_mbps : null
      // Use static NIC link speed for utilization
      const rxMbps = toMbps(rxRate)
      const txMbps = toMbps(txRate)
      const rxUtil = isNumber(rxMbps) && isNumber(bandwidth) && bandwidth > 0 ? Math.min(rxMbps / bandwidth * 100, 100) : null
      const txUtil = isNumber(txMbps) && isNumber(bandwidth) && bandwidth > 0 ? Math.min(txMbps / bandwidth * 100, 100) : null
      const utilValues = [rxUtil, txUtil].filter(isNumber)
      const utilMax = utilValues.length ? Math.max(...utilValues) : null
      return { nicName, bandwidth, rxRate, txRate, rxUtil, txUtil, utilMax }
    })
  }, [validNics, dynamicInfo])

  // Fallback for when no valid NICs exist (keep legacy single-card behavior)
  const primaryInterfaceName = typeof staticInfo?.io.primary_interface === 'string'
    ? staticInfo.io.primary_interface
    : null
  const fallbackNetworkRates = primaryInterfaceName && dynamicInfo?.network?.interfaces?.[primaryInterfaceName]
    ? dynamicInfo.network.interfaces[primaryInterfaceName]
    : dynamicInfo?.network?.total
  const fallbackRxRate = isNumber(fallbackNetworkRates?.rx_bytes_per_sec) ? fallbackNetworkRates.rx_bytes_per_sec : null
  const fallbackTxRate = isNumber(fallbackNetworkRates?.tx_bytes_per_sec) ? fallbackNetworkRates.tx_bytes_per_sec : null
  // Use static peak bandwidth for fallback utilization
  const fbRxMbps = toMbps(fallbackRxRate)
  const fbTxMbps = toMbps(fallbackTxRate)
  const fbStaticPeakMbps = isNumber(staticInfo?.io.network_peak_mbps) && staticInfo.io.network_peak_mbps > 0
    ? staticInfo.io.network_peak_mbps : null
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
    if (staticInfo?.npu.pciid) parts.push(`[${staticInfo.npu.pciid}]`)
    const freqEntries = Object.values(staticInfo?.npu.freq_bounds_mhz || {})
    const maxFreq = freqEntries.length ? freqEntries[0]?.max_mhz : null
    if (isNumber(maxFreq)) parts.push(`Freq ${Math.round(maxFreq)} MHz`)
    if (parts.length) return parts.join(' | ')
    return staticInfo?.npu.names?.length
      ? staticInfo.npu.names.join(', ')
      : dynamicInfo?.npu.npu_smi.error || 'npu-smi'
  })()

  const gpuSnapshotMeta = gpuDevices.length
    ? gpuDevices.map((d) => {
        const parts: string[] = [`${d.displayLabel}(${d.driver !== 'N/A' ? d.driver : '?'})`]
        if (d.label === 'iGPU' && isNumber(d.euCount)) parts.push(`EU ${d.euCount}`)
        if (d.pcieLink.current_speed) parts.push(`PCIe ${formatPcieLink(d.pcieLink.current_speed, d.pcieLink.current_width, d.pcieLink.max_speed, d.pcieLink.max_width)}`)
        return parts.join(' ')
      }).join(' | ')
      + (staticInfo?.gpu.names.length ? ` | ${staticInfo.gpu.names.join(' / ')}` : '')
    : (staticInfo?.gpu
        ? `${staticInfo.gpu.count} GPU | ${staticInfo.gpu.names.join(' / ') || 'Unknown'}`
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

  const cpuUsagePct = normalizePercent(dynamicInfo?.cpu.usage_total ?? null)
  const memoryUsagePct = normalizePercent(dynamicInfo?.memory.usage_percent ?? null)

  // System pressure: use the weighted composite score from SystemPressureMonitor when available
  const pressureScore = isNumber(dynamicInfo?.pressure?.score) ? (dynamicInfo.pressure.score as number) * 100 : null
  const pressureLevel = dynamicInfo?.pressure?.level ?? null
  const fallbackSystemPressure = isNumber(cpuUsagePct) && isNumber(memoryUsagePct)
    ? (cpuUsagePct + memoryUsagePct) / 2
    : null
  const systemPressurePct = pressureScore ?? fallbackSystemPressure

  // Disk IO: use is_disk_io_stressed data
  const diskIsStressed = dynamicInfo?.disk?.is_stressed ?? false
  const diskIoWait = isNumber(dynamicInfo?.disk?.iowait) ? (dynamicInfo.disk.iowait as number) : null
  // Disk IO pressure: read pre-computed values from backend
  const diskBusyNames = dynamicInfo?.disk?.busy_disks ?? []
  const diskTotalCount = dynamicInfo?.disk?.total_disks ?? 0
  const diskBusyCount = diskBusyNames.length
  const diskBusyPct = dynamicInfo?.disk?.busy_pct ?? null
  const diskBusyLevelLabel = dynamicInfo?.disk?.busy_level ?? 'NO DATA'

  // Network pressure: read pre-computed values from backend
  const networkBusyNics = dynamicInfo?.pressure?.network_busy_nics ?? []
  const networkTotalCount = dynamicInfo?.pressure?.network_total_nics ?? 0
  const networkBusyCount = networkBusyNics.length
  const networkPressurePct = dynamicInfo?.pressure?.network_busy_pct ?? null
  const networkBusyLevelLabel = dynamicInfo?.pressure?.network_busy_level ?? 'NO DATA'

  const npuParsed = useMemo(() => parseNpuRaw(dynamicInfo?.npu.npu_smi.raw), [dynamicInfo?.npu.npu_smi.raw])
  const npuUtilValue = useMemo(() => {
    if (!dynamicInfo?.npu.npu_smi.available) return null
    return typeof npuParsed?.utilization_percent === 'number' ? normalizePercent(npuParsed.utilization_percent as number) : null
  }, [dynamicInfo, npuParsed])
  const npuValue = npuUtilValue
  const cpuTrendValue = cpuUsagePct
  const memoryTrendValue = memoryUsagePct
  const cpuBusy = isNumber(cpuTrendValue) ? cpuTrendValue >= 80 : false
  const memoryBusy = isNumber(memoryTrendValue) ? memoryTrendValue >= 80 : false
  const cpuTrendStatus = isNumber(cpuTrendValue) ? (cpuBusy ? 'Busy' : 'OK') : undefined
  const memoryTrendStatus = isNumber(memoryTrendValue) ? (memoryBusy ? 'Busy' : 'OK') : undefined
  const cpuTrendStatusColor = cpuTrendStatus ? (cpuBusy ? COLORS.red : COLORS.green) : COLORS.textMuted
  const memoryTrendStatusColor = memoryTrendStatus ? (memoryBusy ? COLORS.red : COLORS.green) : COLORS.textMuted

  const coreGroups = useMemo(() => {
    const usage = dynamicInfo?.cpu.per_core_usage || []
    const freqs = dynamicInfo?.cpu.per_core_freq_mhz || []
    if (!usage.length) return [] as Array<{ label: string; type: 'P' | 'E' | 'LPE' | 'Core'; indices: number[] }>

    const p = dynamicInfo?.cpu.p_core_indices || []
    const e = dynamicInfo?.cpu.e_core_indices || []
    const lpe = dynamicInfo?.cpu.lpe_core_indices || []

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
        indices: usage.map((_, i) => i).filter((i) => i < freqs.length || freqs.length === 0),
      },
    ]
  }, [dynamicInfo])

  const coreFreqAxisMax = useMemo(() => {
    const staticMax = staticInfo?.cpu.freq_mhz.max_mhz
    const dynamicMax = Math.max(
      0,
      ...(dynamicInfo?.cpu.per_core_freq_mhz || []).map((value) => (isNumber(value) ? value : 0))
    )
    const candidate = isNumber(staticMax) ? staticMax : dynamicMax
    if (!isNumber(candidate) || candidate <= 0) return 1000
    return Math.max(1000, Math.ceil(candidate / 100) * 100)
  }, [staticInfo?.cpu.freq_mhz.max_mhz, dynamicInfo?.cpu.per_core_freq_mhz])

  const cpuStaticFreqText = cpuFreqRangeMeta

  const npuStaticFreqText = useMemo(
    () => summarizeFreqBounds(staticInfo?.npu.freq_bounds_mhz),
    [staticInfo?.npu.freq_bounds_mhz]
  )

  const networkPeakText = useMemo(
    () => formatNetworkSpeed(staticInfo?.io.network_peak_mbps),
    [staticInfo?.io.network_peak_mbps]
  )

  const networkSpeedMapText = useMemo(
    () => summarizeNetworkSpeeds(staticInfo?.io.network_speeds_mbps),
    [staticInfo?.io.network_speeds_mbps]
  )

  const diskStaticTotalText = useMemo(
    () => (isNumber(staticInfo?.disk.total_size_gb) ? `${staticInfo?.disk.total_size_gb.toFixed(2)} GB` : 'N/A'),
    [staticInfo?.disk.total_size_gb]
  )

  const diskStaticDevicesText = useMemo(
    () => `${staticInfo?.disk.device_count ?? 0} device(s) | ${summarizeDiskSizes(staticInfo?.disk.devices)}`,
    [staticInfo?.disk.device_count, staticInfo?.disk.devices]
  )

  // Map disk name → size_gb for use in per-disk TrendPanels
  const diskSizeLookup = useMemo(() => {
    const map: Record<string, number | null> = {}
    staticInfo?.disk.devices?.forEach((d) => { map[d.name] = d.size_gb })
    return map
  }, [staticInfo?.disk.devices])

  const diskSnapshotMeta = busiestDisk?.[0]
    ? `${busiestDisk[0]} | Total ${diskStaticTotalText} | ${staticInfo?.disk.device_count ?? '?'} device(s)`
    : staticInfo?.disk
      ? `Total ${diskStaticTotalText} | ${staticInfo.disk.device_count} device(s)`
      : 'No static data'

  const gpuFilterOptions = useMemo(() => {
    const options: Array<{ label: string; value: string }> = [{ label: 'All GPU', value: 'all' }]
    // Sort iGPU first, then dGPU
    const sorted = [...gpuDevices].sort((a, b) => {
      const aIsIgpu = a.label === 'iGPU' ? 0 : 1
      const bIsIgpu = b.label === 'iGPU' ? 0 : 1
      return aIsIgpu - bIsIgpu
    })
    sorted.forEach((device) => {
      options.push({ label: device.displayLabel, value: device.id })
    })
    return options
  }, [gpuDevices])

  return (
    <div className="perf-root">
      <div className="perf-ambient perf-ambient--blue" />
      <div className="perf-ambient perf-ambient--orange" />
      <div className="perf-ambient perf-ambient--teal" />

      <div className="perf-header">
        <div>
          <Title level={3} style={{ color: COLORS.text, margin: 0 }}>
            Performance
          </Title>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
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

      <Card className="perf-card perf-rise" bodyStyle={{ padding: 18, marginBottom: 18 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <Space size={8}>
            <AlertOutlined style={{ color: PERF_COLORS.pressure }} />
            <Text style={{ color: COLORS.text, fontSize: 13, fontWeight: 600 }}>Pressure Overview</Text>
          </Space>
        </div>
        <Row gutter={[16, 12]}>
          {/* Disk IO Pressure: fraction of busy disks; subtitle lists busy vs OK devices */}
          <Col xs={24} md={8}>
            <PressurePointerGauge
              title="Disk IO Pressure"
              valuePct={diskBusyPct}
              levelLabel={diskBusyLevelLabel}
              subtitle={[
                diskBusyCount > 0 ? `Busy: ${diskBusyNames.join(', ')}` : null,
                diskTotalCount > 0 ? `${diskBusyCount}/${diskTotalCount} disks busy` : null,
                isNumber(diskIoWait) ? `iowait: ${diskIoWait.toFixed(1)}%` : null,
              ].filter(Boolean).join(' | ')}
              description="Fraction of busy disks across all devices"
            />
          </Col>

          {/* System Pressure (middle): weighted composite score from SystemPressureMonitor */}
          <Col xs={24} md={8}>
            <PressurePointerGauge
              title="System Pressure"
              valuePct={systemPressurePct}
              subtitle={pressureLevel
                ? `Level: ${pressureLevel.toUpperCase()} | Score: ${isNumber(pressureScore) ? (pressureScore / 100).toFixed(2) : 'N/A'}`
                : undefined}
              description="Weighted composite score (PSI × resource usage)"
            />
          </Col>

          {/* Network IO Pressure: fraction of busy NICs based on actual link speed */}
          <Col xs={24} md={8}>
            <PressurePointerGauge
              title="Network IO Pressure"
              valuePct={networkPressurePct}
              levelLabel={networkBusyLevelLabel}
              subtitle={[
                networkBusyCount > 0 ? `Busy: ${networkBusyNics.join(', ')}` : null,
                networkTotalCount > 0 ? `${networkBusyCount}/${networkTotalCount} NICs busy` : null,
              ].filter(Boolean).join(' | ')}
              description="Fraction of busy NICs based on actual link speed"
            />
          </Col>
        </Row>
      </Card>

      <SectionTitle
        title="Utilization Insights"
        subtitle="Overall utilization + trend details in one unified view"
        action={loadingDynamic && !dynamicInfo ? <Spin size="small" /> : null}
      />

      <Row className="perf-insights-grid" gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={24} md={12} xl={8}>
          <TrendPanel
            title="CPU"
            accent={PERF_COLORS.cpu}
            value={cpuTrendValue}
            unit="%"
            status={cpuTrendStatus}
            statusColor={cpuTrendStatusColor}
            series={getSeries('util:cpu')}
            subtitle={cpuSnapshotMeta}
            details={[
              { label: 'P-Core Usage', value: formatPercent(dynamicInfo?.cpu.p_core_usage), source: 'dynamic' },
              ...(isNumber(dynamicInfo?.cpu.e_core_usage)
                ? [{ label: 'E-Core Usage', value: formatPercent(dynamicInfo?.cpu.e_core_usage), source: 'dynamic' as DataSourceKind }]
                : []),
              ...(isNumber(dynamicInfo?.cpu.lpe_core_usage)
                ? [{ label: 'LPE-Core Usage', value: formatPercent(dynamicInfo?.cpu.lpe_core_usage), source: 'dynamic' as DataSourceKind }]
                : []),
              { label: 'P-Core Freq', value: formatMetric(dynamicInfo?.cpu.p_core_freq_mhz, 'MHz', 0), source: 'dynamic' },
              ...(isNumber(dynamicInfo?.cpu.e_core_freq_mhz)
                ? [{ label: 'E-Core Freq', value: formatMetric(dynamicInfo?.cpu.e_core_freq_mhz, 'MHz', 0), source: 'dynamic' as DataSourceKind }]
                : []),
              ...(isNumber(dynamicInfo?.cpu.lpe_core_freq_mhz)
                ? [{ label: 'LPE-Core Freq', value: formatMetric(dynamicInfo?.cpu.lpe_core_freq_mhz, 'MHz', 0), source: 'dynamic' as DataSourceKind }]
                : []),
              { label: 'Temperature', value: formatMetric(dynamicInfo?.cpu.temperature_c, '°C', 1), source: 'dynamic' },
            ]}
            sparkMode={sparkMode}
            trendWindow={trendWindow}
          />
        </Col>

        <Col xs={24} md={12} xl={8}>
          <TrendPanel
            title="Memory"
            accent={PERF_COLORS.memory}
            value={memoryTrendValue}
            unit="%"
            status={memoryTrendStatus}
            statusColor={memoryTrendStatusColor}
            series={getSeries('util:memory')}
            subtitle={memorySnapshotMeta}
            details={[
              { label: 'Available', value: formatMetric(dynamicInfo?.memory.available_gb, 'GB', 1), source: 'dynamic' },
              {
                label: 'Usage',
                value: (() => {
                  const totalGb = dynamicInfo?.memory.total_gb ?? staticInfo?.memory.total_gb
                  const usedGb = isNumber(totalGb) && isNumber(memoryTrendValue)
                    ? totalGb * memoryTrendValue / 100
                    : null
                  if (isNumber(usedGb) && isNumber(memoryTrendValue))
                    return `${usedGb.toFixed(1)} GB (${memoryTrendValue.toFixed(0)}%)`
                  return formatPercent(memoryTrendValue)
                })(),
                source: 'dynamic',
              },
              { label: 'Swap', value: dynamicInfo?.memory.swap_total_gb != null ? `${formatMetric(dynamicInfo.memory.swap_used_gb, 'GB', 1)} / ${dynamicInfo.memory.swap_total_gb.toFixed(1)} GB (${formatPercent(dynamicInfo.memory.swap_usage_percent)})` : 'N/A', source: 'dynamic' },
            ]}
            sparkMode={sparkMode}
            trendWindow={trendWindow}
          />
        </Col>

        {diskDevices.map(([diskName, diskData]) => (
          <Col xs={24} md={12} xl={8} key={diskName}>
            <TrendPanel
              title={`Disk: ${diskName}`}
              accent={PERF_COLORS.disk}
              value={normalizePercent(diskData.utilization)}
              unit="%"
              status={diskData.is_busy ? 'Busy' : 'OK'}
              statusColor={diskData.is_busy ? COLORS.red : COLORS.green}
              series={getSeries(`disk:${diskName}:util`)}
              subtitle={diskSizeLookup[diskName] != null ? `${formatMetric(diskSizeLookup[diskName], 'GB', 1)}` : undefined}
              details={[
                { label: 'Size', value: formatMetric(diskSizeLookup[diskName], 'GB', 2), source: 'static' },
                { label: 'Read', value: formatMetric(diskData.read_kb_per_sec, 'KB/s', 1), source: 'dynamic' },
                { label: 'Write', value: formatMetric(diskData.write_kb_per_sec, 'KB/s', 1), source: 'dynamic' },
                { label: 'Read IOPS', value: formatMetric(diskData.read_iops, 'IOPS', 1), source: 'dynamic' },
                { label: 'Write IOPS', value: formatMetric(diskData.write_iops, 'IOPS', 1), source: 'dynamic' },
                { label: 'Utilization', value: formatPercent(diskData.utilization), source: 'dynamic' },
              ]}
              sparkMode={sparkMode}
              trendWindow={trendWindow}
            />
          </Col>
        ))}

        {networkNicCards.length > 1 ? networkNicCards.map((nic) => {
          const nicSeries = getNetworkNicSeries(nic.nicName)
          return (
            <Col xs={24} md={12} xl={8} key={`nic-${nic.nicName}`}>
              <TrendPanel
                title={`Network: ${nic.nicName}`}
                accent={PERF_COLORS.network}
                value={nic.utilMax}
                unit="%"
                status={isNumber(nic.utilMax) ? (nic.utilMax >= 80 ? 'Busy' : 'OK') : undefined}
                statusColor={isNumber(nic.utilMax) && nic.utilMax >= 80 ? COLORS.red : PERF_COLORS.network}
                series={getSeries(`util:network:${nic.nicName}`)}
                splitBars={[
                  { key: 'rx-bar', label: 'RX', value: nic.rxUtil, color: PERF_COLORS.network, sublabel: `${formatBytesRate(nic.rxRate)}` },
                  { key: 'tx-bar', label: 'TX', value: nic.txUtil, color: PERF_COLORS.gpu, sublabel: `${formatBytesRate(nic.txRate)}` },
                ]}
                subtitle={`Peak BW: ${formatNetworkSpeed(nic.bandwidth)}`}
                details={[
                  { label: 'Util', value: formatPercent(nic.utilMax), source: 'dynamic' },
                  { label: 'RX BW', value: formatMetric(toMbps(nic.rxRate), 'Mb/s', 2), source: 'dynamic' },
                  { label: 'TX BW', value: formatMetric(toMbps(nic.txRate), 'Mb/s', 2), source: 'dynamic' },
                ]}
                secondaryChart={(
                  <div>
                    <Text style={{ color: COLORS.textMuted, fontSize: 11, display: 'block', marginBottom: 4 }}>
                      Bandwidth Trend (Kb/s)
                    </Text>
                    <div className="perf-series-legend" style={{ marginBottom: 6 }}>
                      {nicSeries.bwSeries.map((item) => (
                        <span className="perf-series-legend-item" key={`network-bw-${item.key}`}>
                          <span className="perf-series-legend-dot" style={{ background: item.stroke }} />
                          {item.label}
                        </span>
                      ))}
                    </div>
                    <MultiLineSparkline
                      series={nicSeries.bwSeries}
                      width={320}
                      height={52}
                      responsive
                      mode={sparkMode}
                      xStartLabel={trendWindow ? `-${trendWindow}` : ''}
                      xEndLabel="now"
                      yMin={0}
                      yMax={nicSeries.bwAxisMax}
                      yTickCount={3}
                    />
                  </div>
                )}
                secondaryChartPosition="top"
                compact
                centerBody
                compactDetails
                primaryChartHeight={68}
                secondaryChartGap={6}
                detailTopMargin={2}
                sparkMode={sparkMode}
                trendWindow={trendWindow}
                primaryChartLabel="Utilization Trend"
              />
            </Col>
          )
        }) : (
          <Col xs={24} md={12} xl={8}>
            <TrendPanel
              title={networkNicCards.length === 1 ? `Network: ${networkNicCards[0].nicName}` : 'Network'}
              accent={PERF_COLORS.network}
              value={networkNicCards.length === 1 ? networkNicCards[0].utilMax : fallbackUtilMax}
              unit="%"
              status={isNumber(networkUtilMax) ? (networkUtilMax >= 80 ? 'Busy' : 'OK') : undefined}
              statusColor={isNumber(networkUtilMax) && networkUtilMax >= 80 ? COLORS.red : PERF_COLORS.network}
              series={getSeries(networkNicCards.length === 1 ? `util:network:${networkNicCards[0].nicName}` : 'util:network')}
              splitBars={[
                { key: 'rx-bar', label: 'RX', value: networkNicCards.length === 1 ? networkNicCards[0].rxUtil : fallbackRxUtil, color: PERF_COLORS.network, sublabel: formatBytesRate(networkNicCards.length === 1 ? networkNicCards[0].rxRate : fallbackRxRate) },
                { key: 'tx-bar', label: 'TX', value: networkNicCards.length === 1 ? networkNicCards[0].txUtil : fallbackTxUtil, color: PERF_COLORS.gpu, sublabel: formatBytesRate(networkNicCards.length === 1 ? networkNicCards[0].txRate : fallbackTxRate) },
              ]}
              subtitle={networkNicCards.length === 1
                ? `Peak BW: ${formatNetworkSpeed(networkNicCards[0].bandwidth)}`
                : staticInfo?.io
                  ? [
                      `NIC ${formatPlain(staticInfo.io.nic_count)}`,
                      staticInfo.io.network_speeds_mbps ? summarizeNetworkSpeeds(staticInfo.io.network_speeds_mbps) : null,
                    ].filter(Boolean).join(' | ')
                  : 'No data'
              }
              details={[
                { label: 'Util', value: formatPercent(networkNicCards.length === 1 ? networkNicCards[0].utilMax : fallbackUtilMax), source: 'dynamic' },
                { label: 'RX BW', value: formatMetric(toMbps(networkNicCards.length === 1 ? networkNicCards[0].rxRate : fallbackRxRate), 'Mb/s', 2), source: 'dynamic' },
                { label: 'TX BW', value: formatMetric(toMbps(networkNicCards.length === 1 ? networkNicCards[0].txRate : fallbackTxRate), 'Mb/s', 2), source: 'dynamic' },
              ]}
              secondaryChart={(
                <div>
                  <Text style={{ color: COLORS.textMuted, fontSize: 11, display: 'block', marginBottom: 4 }}>
                    Bandwidth Trend (Kb/s)
                  </Text>
                  <div className="perf-series-legend" style={{ marginBottom: 6 }}>
                    {networkBandwidthKbpsSeries.map((item) => (
                      <span className="perf-series-legend-item" key={`network-bw-${item.key}`}>
                        <span className="perf-series-legend-dot" style={{ background: item.stroke }} />
                        {item.label}
                      </span>
                    ))}
                  </div>
                  <MultiLineSparkline
                    series={networkBandwidthKbpsSeries}
                    width={320}
                    height={52}
                    responsive
                    mode={sparkMode}
                    xStartLabel={trendWindow ? `-${trendWindow}` : ''}
                    xEndLabel="now"
                    yMin={0}
                    yMax={networkBandwidthKbpsAxisMax}
                    yTickCount={3}
                  />
                </div>
              )}
              secondaryChartPosition="top"
              compact
              centerBody
              compactDetails
              primaryChartHeight={68}
              secondaryChartGap={6}
              detailTopMargin={2}
              sparkMode={sparkMode}
              trendWindow={trendWindow}
              primaryChartLabel="Utilization Trend"
            />
          </Col>
        )}

        <Col xs={24} md={12} xl={8}>
          <TrendPanel
            title="NPU"
            accent={PERF_COLORS.npu}
            value={npuValue}
            unit="%"
            status={dynamicInfo ? (dynamicInfo.npu.npu_smi.available ? 'OK' : 'Offline') : undefined}
            statusColor={dynamicInfo?.npu.npu_smi.available ? COLORS.green : COLORS.textMuted}
            series={getSeries('npu:util')}
            subtitle={npuSnapshotMeta}
            details={[
              { label: 'Util', value: formatPercent(npuUtilValue), source: 'dynamic' },
              ...(npuParsed?.pmt_available !== false
                ? [{ label: 'Power', value: npuParsed?.power_w != null ? `${(npuParsed.power_w as number).toFixed(2)} W` : 'N/A', source: 'dynamic' as DataSourceKind }]
                : []),
              { label: 'Freq', value: npuParsed?.frequency_mhz != null ? `${Math.round(npuParsed.frequency_mhz as number)} MHz` : 'N/A', source: 'dynamic' },
              { label: 'Memory Used', value: npuParsed?.memory_bytes != null ? `${((npuParsed.memory_bytes as number) / (1024 * 1024)).toFixed(2)} MB` : 'N/A', source: 'dynamic' },
            ]}
            compact
            centerBody
            compactDetails
            primaryChartHeight={68}
            detailTopMargin={2}
            sparkMode={sparkMode}
            trendWindow={trendWindow}
          />
        </Col>

        {gpuDevices.length === 0 ? null : [...gpuDevices].sort((a, b) => {
          const aIsIgpu = a.label === 'iGPU' ? 0 : 1
          const bIsIgpu = b.label === 'iGPU' ? 0 : 1
          return aIsIgpu - bIsIgpu
        }).map((device, index) => (
          <Col xs={24} md={12} xl={8} key={device.id}>
            <TrendPanel
              title={device.displayLabel}
              accent={GPU_UTIL_COLORS[index % GPU_UTIL_COLORS.length]}
              value={device.utilization}
              unit="%"
              status={device.status !== 'Offline' ? device.status : undefined}
              statusColor={device.statusColor}
              series={getSeries(`gpu:${device.id}:util`)}
              subtitle={(() => {
                const parts: string[] = []
                if (device.driver !== 'N/A') parts.push(device.driver)
                if (device.pciId) parts.push(`[${device.pciId}]`)
                if (isNumber(device.euCount)) parts.push(`EU ${device.euCount}`)
                const gt0b = device.gtFreqBounds.gt0
                const gt1b = device.gtFreqBounds.gt1
                if (gt0b && (isNumber(gt0b.min_mhz) || isNumber(gt0b.max_mhz))) parts.push(`GT0 ${formatFreqRange(gt0b.min_mhz, gt0b.max_mhz)}`)
                if (gt1b && (isNumber(gt1b.min_mhz) || isNumber(gt1b.max_mhz))) parts.push(`GT1 ${formatFreqRange(gt1b.min_mhz, gt1b.max_mhz)}`)
                return parts.length ? parts.join(' | ') : device.name
              })()}
              details={[
                {
                  label: 'GT0 Actual Freq',
                  value: formatMetric(device.frequencies.gt0?.act_mhz, 'MHz', 0),
                  source: 'dynamic',
                },
                {
                  label: 'GT0 Request Freq',
                  value: formatMetric(device.frequencies.gt0?.cur_mhz, 'MHz', 0),
                  source: 'dynamic',
                },
                {
                  label: 'GT1 Actual Freq',
                  value: formatMetric(device.frequencies.gt1?.act_mhz, 'MHz', 0),
                  source: 'dynamic',
                },
                {
                  label: 'GT1 Request Freq',
                  value: formatMetric(device.frequencies.gt1?.cur_mhz, 'MHz', 0),
                  source: 'dynamic',
                },
                { label: 'GT0 RC6', value: formatPercent(device.frequencies.gt0?.rc6_pct ?? null), source: 'dynamic' },
                { label: 'GT1 RC6', value: formatPercent(device.frequencies.gt1?.rc6_pct ?? null), source: 'dynamic' },
                { label: 'GPU Power', value: formatMetric(device.powerGpu, 'W', 2), source: 'dynamic' },
                { label: device.label === 'iGPU' ? 'Pkg Power' : 'Card Power', value: formatMetric(device.powerPkg, 'W', 2), source: 'dynamic' },
                {
                  label: device.label === 'iGPU' ? 'Sys Mem' : 'VRAM',
                  value: formatPercent(device.vramUsage),
                  source: 'dynamic',
                },
              ]}
              sparkMode={sparkMode}
              trendWindow={trendWindow}
            />
          </Col>
        ))}

      </Row>

      <SectionTitle
        title="Per-Core CPU"
        subtitle="Grouped by P / E cores with dual-axis util/freq trends"
        action={
          <Button
            size="small"
            type="text"
            className="perf-toggle-btn"
            icon={showCpuDetails ? <UpOutlined /> : <DownOutlined />}
            onClick={() => setShowCpuDetails((prev) => !prev)}
          >
            {showCpuDetails ? 'Collapse' : 'Expand'}
          </Button>
        }
      />

      {showCpuDetails && (
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        {coreGroups.length === 0 ? (
          <Col span={24}>
            <Card className="perf-card" bodyStyle={{ padding: 16 }}>
              <Text style={{ color: COLORS.textMuted }}>No per-core CPU data</Text>
            </Card>
          </Col>
        ) : (
          coreGroups.map((group) => (
            <Col key={group.label} xs={24} xl={12}>
              <Card className="perf-card perf-rise" bodyStyle={{ padding: 16 }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
                  <Space size={8}>
                    <ThunderboltOutlined style={{ color: PERF_COLORS.cpu }} />
                    <Text style={{ color: COLORS.text, fontWeight: 600 }}>{group.label}</Text>
                    {(() => {
                      const freqBounds = group.type === 'P'
                        ? staticInfo?.cpu.freq_mhz.p_core_freq_mhz
                        : group.type === 'E'
                          ? staticInfo?.cpu.freq_mhz.e_core_freq_mhz
                          : group.type === 'LPE'
                            ? staticInfo?.cpu.freq_mhz.lpe_core_freq_mhz
                            : null
                      const txt = freqBounds
                        ? formatFreqRange(freqBounds.min_mhz, freqBounds.max_mhz)
                        : formatFreqRange(staticInfo?.cpu.freq_mhz.min_mhz, staticInfo?.cpu.freq_mhz.max_mhz)
                      return txt ? <Text style={{ color: COLORS.textMuted, fontSize: 10 }}>{txt}</Text> : null
                    })()}
                  </Space>
                  <Tag style={{ fontSize: 10 }}>{group.indices.length} cores</Tag>
                </div>
                {(() => {
                  const groupCores = group.indices.map((coreIndex) => ({
                    coreIndex,
                    usage: dynamicInfo?.cpu.per_core_usage?.[coreIndex] ?? null,
                    freq: dynamicInfo?.cpu.per_core_freq_mhz?.[coreIndex] ?? null,
                    temp: dynamicInfo?.cpu.per_core_temperature_c?.[coreIndex] ?? null,
                    trend: getSeries(`cpu:core:${coreIndex}`),
                    freqTrend: getSeries(`cpu:core_freq:${coreIndex}`),
                  }))
                  const freqValues = groupCores.flatMap((item) => [item.freq, ...item.freqTrend])
                  const groupUtilAxis = { min: 0, max: 100 }
                  const groupFreqAxis = getAdaptiveAxis(freqValues, null, {
                    lower: 0,
                    upper: Math.max(1000, coreFreqAxisMax),
                    minRange: 200,
                    padding: 60,
                    step: 20,
                  })

                  return (
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
                      utilAxis={groupUtilAxis}
                      freqAxis={groupFreqAxis}
                    />
                  ))}
                </div>
                  )
                })()}
              </Card>
            </Col>
          ))
        )}
      </Row>
      )}

      <SectionTitle
        title="NPU Details"
        subtitle="Intel NPU telemetry with utilization, power, frequency and bandwidth trends"
        action={
          <Button
            size="small"
            type="text"
            className="perf-toggle-btn"
            icon={showNpuDetails ? <UpOutlined /> : <DownOutlined />}
            onClick={() => setShowNpuDetails((prev) => !prev)}
          >
            {showNpuDetails ? 'Collapse' : 'Expand'}
          </Button>
        }
      />

      {showNpuDetails && (
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col span={24}>
          <NpuDetailCard
            npuParsed={npuParsed}
            npuName={staticInfo?.npu.names?.[0] || 'Intel NPU'}
            npuFreqMinMhz={(() => {
              const bounds = Object.values(staticInfo?.npu.freq_bounds_mhz || {})
              const val = bounds[0]?.min_mhz
              return typeof val === 'number' ? val : null
            })()}
            npuFreqMaxMhz={(() => {
              const bounds = Object.values(staticInfo?.npu.freq_bounds_mhz || {})
              const val = bounds[0]?.max_mhz
              return typeof val === 'number' ? val : null
            })()}
            getSeries={getSeries}
            trendWindow={trendWindow}
          />
        </Col>
      </Row>
      )}

      <SectionTitle
        title="GPU Devices"
        subtitle="Device-level telemetry (iGPU / dGPU) with engine trends"
        action={
          <Space wrap>
            <Button
              size="small"
              type="text"
              className="perf-toggle-btn"
              icon={showGpuDetails ? <UpOutlined /> : <DownOutlined />}
              onClick={() => setShowGpuDetails((prev) => !prev)}
            >
              {showGpuDetails ? 'Collapse' : 'Expand'}
            </Button>
            <Segmented
              value={gpuFilter}
              onChange={(value) => setGpuFilter(value as string)}
              options={gpuFilterOptions}
            />
          </Space>
        }
      />

      {showGpuDetails && (
      <Row className="perf-gpu-devices-row" gutter={[16, 16]} style={{ marginBottom: 24 }}>
        {filteredGpuDevices.length === 0 ? (
          <Col span={24}>
            <Card className="perf-card" bodyStyle={{ padding: 16 }}>
              <Text style={{ color: COLORS.textMuted }}>No GPU device data</Text>
            </Card>
          </Col>
        ) : (
          [...filteredGpuDevices].sort((a, b) => {
            const aIsIgpu = a.label === 'iGPU' ? 0 : 1
            const bIsIgpu = b.label === 'iGPU' ? 0 : 1
            return aIsIgpu - bIsIgpu
          }).map((device) => (
            <Col span={24} key={device.id}>
              <GpuDeviceCard
                device={device}
                trendWindow={trendWindow}
                getSeries={getSeries}
                sparkMode={sparkMode}
              />
            </Col>
          ))
        )}
      </Row>
      )}

      {!dynamicInfo && (
        <div style={{ marginTop: 16 }}>
          <Spin size="small" />
        </div>
      )}
    </div>
  )
}
