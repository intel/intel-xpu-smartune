import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Collapse,
  DatePicker,
  Drawer,
  Empty,
  Input,
  Row,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { LineChartOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api, getLatestServerTime } from '../api/client'
import type {
  DiagBoot,
  DiagContextData,
  DiagContextQuery,
  DiagAlert,
  DiagControlLifecycle,
  DiagEvent,
  DiagLogRecord,
  DiagMonitorSample,
  DiagSeverity,
} from '../api/types'
import { COLORS } from '../styles/theme'
import '../styles/diagnostics.css'

const { Text } = Typography

interface Props {
  active: boolean
  onOpenHistory: (range: { from: number; to: number }) => void
}

type WindowKey = '15m' | '1h' | '6h' | '24h' | 'custom' | 'entire'
type RefreshInterval = 'off' | '5s' | '10s' | '30s'
type EventActivityKey = 'all' | 'protection' | 'system' | 'benchmark'

const ALL = '__all__'

const WINDOW_SECONDS: Record<Exclude<WindowKey, 'custom' | 'entire'>, number> = {
  '15m': 15 * 60,
  '1h': 60 * 60,
  '6h': 6 * 60 * 60,
  '24h': 24 * 60 * 60,
}

const REFRESH_MILLISECONDS: Record<Exclude<RefreshInterval, 'off'>, number> = {
  '5s': 5000,
  '10s': 10000,
  '30s': 30000,
}

const severityColors: Record<string, string> = {
  debug: COLORS.textMuted,
  info: COLORS.accent,
  warning: COLORS.yellow,
  error: COLORS.orange,
  critical: COLORS.red,
}

// Friendly labels for event categories.
const CATEGORY_LABELS: Record<string, string> = {
  'platform.control': 'Resource control',
  'platform.availability': 'Service health',
  'platform.observability': 'Monitoring',
  'resource.system': 'System pressure',
  'resource.cpu': 'CPU',
  'resource.memory': 'Memory',
  'resource.disk_io': 'Disk I/O',
  'resource.network': 'Network',
  'device.thermal': 'Thermal',
  'device.power': 'Power',
  'device.pcie': 'PCIe',
  'device.gpu': 'GPU',
  'device.npu': 'NPU',
  'workload.benchmark': 'Benchmark',
  service: 'Service',
}

// The two event filters answer DIFFERENT questions and must not be conflated:
//   * category -> WHAT the event is about (the reason-code subject domain)
//   * source   -> WHO produced/observed it (its provenance / trust)
// They correlate in the data (a balancer emit is almost always platform.control),
// which is why raw side-by-side dropdowns felt redundant. We group category by its
// domain prefix, and collapse the raw producer strings into a few trust-oriented
// origin buckets, so the two axes read as distinct rather than duplicated.

// Domain prefix of a category code -> friendly group heading for the grouped Select.
const CATEGORY_GROUP_LABELS: Record<string, string> = {
  platform: 'Platform',
  resource: 'Resource',
  device: 'Device',
  workload: 'Workload',
  service: 'Service',
}
const CATEGORY_GROUP_ORDER = ['Platform', 'Resource', 'Device', 'Workload', 'Service', 'Other']

function categoryGroup(cat: string): string {
  const prefix = cat.includes('.') ? cat.slice(0, cat.indexOf('.')) : cat
  return CATEGORY_GROUP_LABELS[prefix] || 'Other'
}

// Origin buckets: the question a debugger actually asks is "who vouches for this?"
// A kernel/journald fact carries different weight than a SmarTune-derived judgment.
const SOURCE_BUCKETS: { key: string; label: string; sources: string[] }[] = [
  { key: 'smartune', label: 'SmarTune', sources: ['balancer', 'monitor', 'detector', 'smartune', 'diagnostics'] },
  { key: 'system', label: 'System log', sources: ['journal'] },
  { key: 'benchmark', label: 'Benchmark', sources: ['benchmark'] },
]
const SOURCE_BUCKET_BY_SOURCE: Record<string, { key: string; label: string }> = Object.fromEntries(
  SOURCE_BUCKETS.flatMap((bucket) => bucket.sources.map((s) => [s, { key: bucket.key, label: bucket.label }])),
)
const OTHER_BUCKET = { key: 'other', label: 'Other' }

function sourceBucket(source?: string | null): { key: string; label: string } {
  if (!source) return OTHER_BUCKET
  return SOURCE_BUCKET_BY_SOURCE[source] || OTHER_BUCKET
}

const EVENT_ACTIVITY_LABELS: Record<EventActivityKey, string> = {
  all: 'All records',
  protection: 'Resource control',
  system: 'System events',
  benchmark: 'Benchmark runs',
}

function categoryLabel(cat?: string | null): string {
  if (!cat) return '-'
  return CATEGORY_LABELS[cat] || cat
}

// Every event rolls up into exactly one activity bucket, so the three specific tabs
// partition "All records" (their counts sum to it). Control and benchmark are the two
// explicit interventions; everything else -- system pressure, device faults, kernel/OS
// faults, service health -- is a system event, refined further by the Category filter.
function eventActivityKey(event: DiagEvent): Exclude<EventActivityKey, 'all'> {
  if (event.category === 'platform.control') return 'protection'
  if (event.category === 'workload.benchmark') return 'benchmark'
  return 'system'
}

function controlLifecycleStatus(lifecycle: DiagControlLifecycle): { label: string; color: string } {
  if (lifecycle.status === 'active') return { label: 'Active', color: COLORS.green }
  if (lifecycle.status === 'requires_verification') return { label: 'Needs verification', color: COLORS.orange }
  return { label: lifecycle.status || 'Unknown', color: COLORS.textMuted }
}

const RESOURCE_LABELS: Record<string, string> = {
  cpu: 'CPU',
  memory: 'Memory',
  disk_io: 'Disk I/O',
  network: 'Network',
}

function resourceLabel(resource: string): string {
  return RESOURCE_LABELS[resource] || resource
}

function appNameFromEvent(event?: DiagEvent): string | null {
  const appName = event?.attributes?.app_name
  return typeof appName === 'string' && appName.trim() ? appName.trim() : null
}

interface ScopeProcess {
  pid: number
  processName: string
  cmdline: string
}

function scopeDetailsFromEvents(events: DiagEvent[]): { scopes: Map<string, ScopeProcess[]> } {
  const scopes = new Map<string, ScopeProcess[]>()
  for (const event of events) {
    if (event.category === 'platform.control' && event.app_id) {
      scopes.set(event.app_id, scopes.get(event.app_id) || [])
    }
    const attributes = event.attributes
    if (!attributes) continue
    const scopeProcesses = attributes.scope_processes
    if (scopeProcesses && typeof scopeProcesses === 'object' && !Array.isArray(scopeProcesses)) {
      for (const [cgroup, entries] of Object.entries(scopeProcesses)) {
        if (!Array.isArray(entries)) continue
        const current = scopes.get(cgroup) || []
        for (const process of entries) {
          if (!process || typeof process !== 'object') continue
          const pid = (process as { pid?: unknown }).pid
          const processName = (process as { process_name?: unknown; name?: unknown }).process_name
            ?? (process as { name?: unknown }).name
          const cmdline = (process as { cmdline?: unknown }).cmdline
          if (typeof pid === 'number' && typeof processName === 'string') {
            if (!current.some((item) => item.pid === pid)) current.push({
              pid,
              processName,
              cmdline: typeof cmdline === 'string' ? cmdline : '',
            })
          }
        }
        scopes.set(cgroup, current)
      }
    } else if (Array.isArray(scopeProcesses)) {
      const current = scopes.get('') || []
      for (const process of scopeProcesses) {
        if (!process || typeof process !== 'object') continue
        const pid = (process as { pid?: unknown }).pid
        const processName = (process as { process_name?: unknown; name?: unknown }).process_name
          ?? (process as { name?: unknown }).name
        if (typeof pid === 'number' && typeof processName === 'string' && !current.some((item) => item.pid === pid)) {
          current.push({ pid, processName, cmdline: '' })
        }
      }
      scopes.set('', current)
    }
    const eventCgroups = attributes.cgroups
    if (Array.isArray(eventCgroups)) {
      for (const cgroup of eventCgroups) {
        if (typeof cgroup === 'string' && cgroup && !scopes.has(cgroup)) scopes.set(cgroup, [])
      }
    }
  }
  return { scopes }
}

function controlLifecycleAppName(lifecycle: DiagControlLifecycle): string {
  return appNameFromEvent(lifecycle.events.find((event) => appNameFromEvent(event))) || lifecycle.app_id || '-'
}

function protectionAction(event: DiagEvent): string | null {
  if (event.category !== 'platform.control' || !event.protection_id) return null
  const match = event.event_type.match(/_(APPLIED|RECOVERED|FAILED)$/)
  return match?.[1] || null
}

interface DisplayEvent {
  event: DiagEvent
  resources: string[]
}

function displayEvents(events: DiagEvent[]): DisplayEvent[] {
  const grouped = new Map<string, DisplayEvent>()
  const ungrouped: DisplayEvent[] = []
  for (const event of events) {
    const action = protectionAction(event)
    if (!action) {
      ungrouped.push({ event, resources: event.resource_type ? [event.resource_type] : [] })
      continue
    }
    // Merge only the resources of ONE action, not every action of a protection:
    // CPU+Memory capped together (same second) stay one row, but a disk-IO limit
    // applied minutes later — or a later recovery — is its own row. The ledger
    // emits a separate event per resource, so batch by second-precision timestamp.
    const key = `${event.protection_id}:${action}:${event.ts_utc.slice(0, 19)}`
    const existing = grouped.get(key)
    if (existing) {
      if (event.resource_type && !existing.resources.includes(event.resource_type)) existing.resources.push(event.resource_type)
    } else {
      grouped.set(key, { event, resources: event.resource_type ? [event.resource_type] : [] })
    }
  }
  return [...ungrouped, ...grouped.values()].sort((left, right) => right.event.ts_utc.localeCompare(left.event.ts_utc))
}

function displayEventSummary(item: DisplayEvent): string {
  const action = protectionAction(item.event)
  if (!action || item.resources.length < 2) return item.event.summary || item.event.event_type
  const appName = appNameFromEvent(item.event) || item.event.app_id || 'application'
  return `${item.resources.join(', ')} limits ${action.toLowerCase()} for ${appName}`
}

// An auto-limit APPLIED event records the trigger the balancer acted on
// (attributes.reason + pressure_level, see balancer.py _emit_control_events).
// Surfacing it puts cause and effect on one row -- unlike the separately-debounced
// pressure tracker, whose event may lag or never fire once the limit eases pressure.
function limitTrigger(event: DiagEvent): { label: string; color?: string } | null {
  if (protectionAction(event) !== 'APPLIED') return null
  const reason = event.attributes?.reason
  if (reason !== 'system_pressure' && reason !== 'disk_pressure') return null
  const level = event.attributes?.pressure_level
  const levelText = typeof level === 'string' && level ? `${level} ` : ''
  const kind = reason === 'disk_pressure' ? 'disk pressure' : 'system pressure'
  return { label: `${levelText}${kind}`, color: level === 'critical' ? COLORS.orange : undefined }
}

interface ResourceControlDetail {
  resource: string
  limit: string
}

function controlDetailsFromEvent(event: DiagEvent): ResourceControlDetail[] {
  const attributes = event.attributes
  const resourceParts = attributes?.resource_parts
  const limitRates = attributes?.limit_rates
  if (!resourceParts || typeof resourceParts !== 'object') return []

  const parts = resourceParts as Record<string, unknown>
  const rates = limitRates && typeof limitRates === 'object' ? limitRates as Record<string, unknown> : null
  const action = protectionAction(event)
  const actionLabel = action === 'RECOVERED' ? 'Recovered' : 'Applied'
  const details: ResourceControlDetail[] = []
  if (parts.cpu === true) {
    details.push({ resource: 'CPU', limit: typeof rates?.cpu_rate === 'number' ? `${Math.round(rates.cpu_rate * 100)}% of baseline` : actionLabel })
  }
  if (parts.memory === true) {
    details.push({ resource: 'Memory', limit: typeof rates?.mem_rate === 'number' ? `${Math.round(rates.mem_rate * 100)}% of baseline` : actionLabel })
  }
  if (parts.disk_io === true && rates?.disk_io_rate && typeof rates.disk_io_rate === 'object') {
    const diskRates = rates.disk_io_rate as Record<string, unknown>
    const limits = [
      typeof diskRates.read === 'number' ? `Read ${diskRates.read} MB/s` : null,
      typeof diskRates.write === 'number' ? `Write ${diskRates.write} MB/s` : null,
      typeof diskRates.read_iops === 'number' ? `Read ${diskRates.read_iops} IOPS` : null,
      typeof diskRates.write_iops === 'number' ? `Write ${diskRates.write_iops} IOPS` : null,
    ].filter(Boolean)
    details.push({ resource: 'Disk I/O', limit: limits.join(' · ') || '-' })
  } else if (parts.disk_io === true) {
    details.push({ resource: 'Disk I/O', limit: actionLabel })
  }
  return details
}

function isPressureTransitionEvent(event: DiagEvent): boolean {
  const attributes = event.attributes
  const fromLevel = attributes?.from_level
  const toLevel = attributes?.to_level
  const score = attributes?.score
  return typeof fromLevel === 'string' && typeof toLevel === 'string' && typeof score === 'number'
}

function normalizedSeverity(level: string): keyof typeof severityColors {
  const value = (level || 'info').toLowerCase()
  if (value.includes('critical') || value.includes('fatal')) return 'critical'
  if (value.includes('error')) return 'error'
  if (value.includes('warn')) return 'warning'
  if (value.includes('debug')) return 'debug'
  return 'info'
}

function severityTag(level: string) {
  const normalized = normalizedSeverity(level)
  const color = severityColors[normalized] || COLORS.textMuted
  return (
    <span className='diagnostics-severity-label' style={{ color }}>
      <span className='diagnostics-severity-dot' style={{ background: color }} />
      {(normalized as DiagSeverity).toUpperCase()}
    </span>
  )
}

// Only control actions carry a lifecycle status; informational events (for example,
// pressure level changes) render as plain rows without an applied/recovered state.
function statusFromEvent(ev: DiagEvent): { label: string; color: string } | null {
  const t = ev.event_type || ''
  if (t.endsWith('_RECOVERED')) return { label: 'Recovered', color: COLORS.green }
  if (t.endsWith('_APPLIED')) return { label: 'Applied', color: COLORS.accent }
  if (t.endsWith('_FAILED')) return { label: 'Failed', color: COLORS.red }
  return null
}

function formatDuration(seconds?: number | null): string | null {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return null
  const s = Math.round(seconds)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const rem = s % 60
  if (m < 60) return rem ? `${m}m ${rem}s` : `${m}m`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

function formatLogTs(rec: DiagLogRecord): string {
  if (rec.ts_iso) {
    const d = dayjs(rec.ts_iso)
    if (d.isValid()) return d.format('MM-DD HH:mm:ss.SSS')
  }
  return dayjs.unix(Math.floor(rec.ts_epoch || 0)).format('MM-DD HH:mm:ss.SSS')
}

function formatActivityTime(value?: string | null): string {
  if (!value) return '-'
  const parsed = dayjs(value)
  return parsed.isValid() ? parsed.format('MM-DD HH:mm:ss') : '-'
}

// A coalesced run of near-identical log lines. Kernel audit spam (apparmor
// "DENIED" ...) differs only by timestamp / audit serial / pid, so without this the
// table is flooded with hundreds of rows that carry one fact. rep is the newest
// occurrence; items keeps every raw line so the expanded row loses nothing.
interface LogGroup {
  key: string
  rep: DiagLogRecord
  count: number
  firstTs: number
  lastTs: number
  items: DiagLogRecord[]
}

// Group key that masks volatile numbers so successive audit lines collapse, while
// a different profile / path / level / source stays its own group.
function logSignature(r: DiagLogRecord): string {
  const masked = (r.message || '').replace(/\d+/g, '#').trim()
  return `${normalizedSeverity(r.level)}|${r.source}|${r.service || r.logger || ''}|${masked}`
}

function groupLogRecords(records: DiagLogRecord[], enabled: boolean): LogGroup[] {
  if (!enabled) {
    return records.map((r, i) => ({
      key: `${r.source}-${r.logger}-${r.ts_epoch}-${i}`,
      rep: r, count: 1, firstTs: r.ts_epoch, lastTs: r.ts_epoch, items: [r],
    }))
  }
  const groups = new Map<string, LogGroup>()
  for (const r of records) { // records arrive newest-first, so the first seen is the rep
    const sig = logSignature(r)
    const existing = groups.get(sig)
    if (existing) {
      existing.count += 1
      existing.items.push(r)
      existing.firstTs = Math.min(existing.firstTs, r.ts_epoch)
      existing.lastTs = Math.max(existing.lastTs, r.ts_epoch)
    } else {
      groups.set(sig, { key: sig, rep: r, count: 1, firstTs: r.ts_epoch, lastTs: r.ts_epoch, items: [r] })
    }
  }
  return Array.from(groups.values()).sort((a, b) => b.lastTs - a.lastTs)
}

// Label a boot session for the selector, e.g. "Boot 0 (current) · Sep 13 09:12–now"
// or "Boot -1 · Sep 13 08:03–09:04". Falls back to a short id when the timestamps
// are unknown (older journalctl without -o json for --list-boots, journal.py §text).
function formatBootLabel(boot: DiagBoot): string {
  const name = boot.index === null ? 'Boot' : `Boot ${boot.index}${boot.running ? ' (current)' : ''}`
  if (!boot.first_ts) return `${name} · ${boot.boot_id.slice(0, 12)}`
  const start = dayjs.unix(boot.first_ts).format('MMM D HH:mm')
  const end = boot.running ? 'now' : boot.last_ts ? dayjs.unix(boot.last_ts).format('HH:mm') : '?'
  return `${name} · ${start}–${end}`
}

interface PressurePoint {
  ts: number
  system: number | null
  disk: number | null
  network: number | null
}

// Extract per-channel pressure trends (0-100) from the monitor samples embedded in
// a /diag/context response (they carry ts_epoch + raw dynamic-snapshot data). The
// system score is 0-1 (scaled here); disk and network are already percent-scaled.
// A channel missing from a sample is null so its line gaps instead of reading zero.
function monitorPressurePoints(samples: DiagMonitorSample[]): PressurePoint[] {
  const pct = (value: unknown, scale = 1): number | null =>
    typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.min(100, value * scale)) : null
  const out: PressurePoint[] = []
  for (const sample of samples) {
    const data = sample.data as { pressure?: Record<string, unknown>; disk?: Record<string, unknown> } | null
    const pressure = data?.pressure
    const system = pct(pressure?.score, 100)
    const disk = pct(data?.disk?.pressure_pct)
    const network = pct(pressure?.network_pressure_pct)
    if (system === null && disk === null && network === null) continue
    out.push({ ts: sample.ts_epoch, system, disk, network })
  }
  return out.sort((a, b) => a.ts - b.ts)
}

// The scope kinds /diag/context accepts, used to drive the investigation drawer.
type ContextTarget = { kind: 'job' | 'app'; value: string; label?: string }

function contextTargetLabel(target: ContextTarget): string {
  if (target.kind === 'app' && target.label) return target.label
  const noun = target.kind === 'job' ? 'Job' : 'Application'
  return `${noun} ${target.value}`
}

function contextTargetForEvent(event: DiagEvent): ContextTarget | null {
  if (event.app_id) return { kind: 'app', value: event.app_id, label: appNameFromEvent(event) || undefined }
  if (event.job_id) return { kind: 'job', value: event.job_id }
  return null
}

export default function Diagnostics({ active, onOpenHistory }: Props) {
  // Data
  const [events, setEvents] = useState<DiagEvent[]>([])
  const [controlLifecycles, setControlLifecycles] = useState<DiagControlLifecycle[]>([])
  const [records, setRecords] = useState<DiagLogRecord[]>([])
  const [logCount, setLogCount] = useState(0)
  const [logTruncated, setLogTruncated] = useState(false)
  const [availableSources, setAvailableSources] = useState<string[]>([])

  // Loading / error
  const [eventsLoading, setEventsLoading] = useState(false)
  const [eventsError, setEventsError] = useState<string | null>(null)
  const [controlLifecyclesLoading, setControlLifecyclesLoading] = useState(false)
  const [logsLoading, setLogsLoading] = useState(false)
  const [controlLifecyclesError, setControlLifecyclesError] = useState<string | null>(null)
  const [logError, setLogError] = useState<string | null>(null)
  // Seed from the server time the app-wide /monitor polling has already observed,
  // so the very first events/logs fetch uses the skew-corrected window. Left null
  // only on a cold start (no response seen yet), where it settles on first fetch.
  const [clockSkewSec, setClockSkewSec] = useState<number | null>(() => {
    const serverTime = getLatestServerTime()
    return serverTime === null ? null : serverTime - Math.floor(Date.now() / 1000)
  })

  const [windowKey, setWindowKey] = useState<WindowKey>('1h')
  const [rangeEndEpoch, setRangeEndEpoch] = useState(() => Math.floor(Date.now() / 1000))
  const [customRange, setCustomRange] = useState<{ from: number; to: number } | null>(null)

  // Boot scope changes the range anchor without replacing the duration presets.
  const [bootScopeEnabled, setBootScopeEnabled] = useState(false)
  const [boots, setBoots] = useState<DiagBoot[]>([])
  const [selectedBootId, setSelectedBootId] = useState<string | null>(null)
  const [bootCustomRange, setBootCustomRange] = useState<{ from: number; to: number } | null>(null)
  const [bootsLoading, setBootsLoading] = useState(false)
  const [refreshInterval, setRefreshInterval] = useState<RefreshInterval>('off')
  const [eventActivity, setEventActivity] = useState<EventActivityKey>('all')
  const [eventKeyword, setEventKeyword] = useState('')
  const [eventSeverity, setEventSeverity] = useState<string | undefined>(undefined)
  const [eventCategory, setEventCategory] = useState<string | undefined>(undefined)
  const [eventSource, setEventSource] = useState<string | undefined>(undefined)
  const [eventPage, setEventPage] = useState(1)
  const [showAllActiveControls, setShowAllActiveControls] = useState(false)

  // Object filters — never typed by hand; set only by clicking a related-object
  // chip, surfaced as removable tags.
  const [jobId, setJobId] = useState<string | undefined>(undefined)
  const [appId, setAppId] = useState<string | undefined>(undefined)

  // Log-search panel (secondary)
  const [logsOpen, setLogsOpen] = useState(false)
  const [logSource, setLogSource] = useState<string | undefined>(undefined)
  const [logKeyword, setLogKeyword] = useState('')
  const [logLevel, setLogLevel] = useState<string | undefined>(undefined)
  const [groupRepeats, setGroupRepeats] = useState(true)

  // Drawers
  const [selectedEvent, setSelectedEvent] = useState<DiagEvent | null>(null)

  // Investigation context drawer (the evidence chain for one job/app,
  // assembled read-only by GET /diag/context).
  const [contextTarget, setContextTarget] = useState<ContextTarget | null>(null)
  const [contextData, setContextData] = useState<DiagContextData | null>(null)
  const [contextLoading, setContextLoading] = useState(false)
  const [contextError, setContextError] = useState<string | null>(null)
  const wasActive = useRef(false)
  // Monotonic token so an older in-flight refresh (superseded by a tab switch,
  // interval tick or filter change) cannot land its response after a newer one
  // and desync the view.
  const refreshSeq = useRef(0)
  // Logs fetch on Search/refresh, not per keystroke: the keyword lives in a ref so
  // typing does not change loadLogs' identity (and so does not trigger a refetch).
  const logSeq = useRef(0)
  const logKeywordRef = useRef(logKeyword)

  const selectedBoot = useMemo(
    () => boots.find((b) => b.boot_id === selectedBootId) ?? null,
    [boots, selectedBootId],
  )

  const timeRange = useMemo(() => {
    const now = rangeEndEpoch + (clockSkewSec ?? 0)
    if (bootScopeEnabled && selectedBoot?.first_ts) {
      const bootFrom = selectedBoot.first_ts
      const bootTo = selectedBoot.running ? now : selectedBoot.last_ts ?? now
      if (windowKey === 'entire') return { from: bootFrom, to: bootTo }
      if (windowKey === 'custom' && bootCustomRange) {
        const from = Math.max(bootFrom, Math.min(bootCustomRange.from, bootTo))
        const to = Math.max(from, Math.min(bootCustomRange.to, bootTo))
        return { from, to }
      }
      const span = windowKey === 'custom' ? WINDOW_SECONDS['1h'] : WINDOW_SECONDS[windowKey]
      return { from: Math.max(bootFrom, bootTo - span), to: bootTo }
    }
    if (windowKey === 'custom' && customRange) return customRange
    const span = windowKey === 'custom' || windowKey === 'entire' ? WINDOW_SECONDS['1h'] : WINDOW_SECONDS[windowKey]
    return { from: now - span, to: now }
  }, [windowKey, customRange, rangeEndEpoch, clockSkewSec, bootScopeEnabled, selectedBoot, bootCustomRange])

  const updateClockSkew = useCallback(() => {
    const serverTime = getLatestServerTime()
    if (serverTime === null) return
    const clientTime = Math.floor(Date.now() / 1000)
    setClockSkewSec((current) => {
      const next = serverTime - clientTime
      return current === null || Math.abs(current - next) >= 5 ? next : current
    })
  }, [])

  // The overview counts (from events) and the "active now" summary (from control
  // lifecycles) are two regions of the same header, so they must move together:
  // fetch both in one coordinated pass, apply their results atomically, and drop
  // the whole batch if a newer refresh has already superseded it. This removes the
  // stagger (each fetch flipping its own loading flag at a different time) and the
  // out-of-order races that made the header disagree with the table.
  const loadEventsAndLifecycles = useCallback(async () => {
    const token = ++refreshSeq.current
    setEventsLoading(true)
    setControlLifecyclesLoading(true)
    setEventsError(null)
    setControlLifecyclesError(null)
    const [eventsResult, lifecyclesResult] = await Promise.allSettled([
      api.getDiagEvents({
        job_id: jobId,
        app_id: appId,
        from: timeRange.from,
        to: timeRange.to,
        limit: 300,
      }),
      api.getDiagControlLifecycles({ limit: 100 }),
    ])
    if (token !== refreshSeq.current) return
    if (eventsResult.status === 'fulfilled') {
      setEvents(eventsResult.value.events || [])
    } else {
      setEvents([])
      setEventsError(eventsResult.reason instanceof Error ? eventsResult.reason.message : 'Failed to query diagnostic events')
    }
    if (lifecyclesResult.status === 'fulfilled') {
      setControlLifecycles(lifecyclesResult.value.lifecycles || [])
    } else {
      setControlLifecycles([])
      setControlLifecyclesError(lifecyclesResult.reason instanceof Error ? lifecyclesResult.reason.message : 'Failed to query protection status')
    }
    setEventsLoading(false)
    setControlLifecyclesLoading(false)
    updateClockSkew()
  }, [jobId, appId, timeRange, updateClockSkew])

  const loadBoots = useCallback(async () => {
    setBootsLoading(true)
    try {
      const res = await api.getDiagBoots(15)
      const list = res.boots || []
      setBoots(list)
      // Default to the newest boot so the scope resolves immediately; the user
      // switches to an earlier one (e.g. Boot -1) to investigate a pre-reboot fault.
      setSelectedBootId((current) => current ?? list[0]?.boot_id ?? null)
    } catch {
      setBoots([])
    } finally {
      setBootsLoading(false)
    }
  }, [])

  const loadLogs = useCallback(async () => {
    const token = ++logSeq.current
    setLogsLoading(true)
    setLogError(null)
    try {
      const res = await api.getDiagLogs({
        source: logSource,
        level: logLevel,
        job_id: jobId,
        // Only the journal source honours boot_id; smartune/benchmark ignore it and
        // stay scoped by the derived from/to (the contract confirmed in journal.py).
        boot_id: bootScopeEnabled ? selectedBoot?.boot_id : undefined,
        keyword: logKeywordRef.current.trim() || undefined,
        from: timeRange.from,
        to: timeRange.to,
        limit: 800,
      })
      if (token !== logSeq.current) return  // superseded by a newer log query
      setRecords(res.records || [])
      setLogCount(res.count || 0)
      setLogTruncated(Boolean(res.truncated))
      setAvailableSources(res.available_sources || [])
      updateClockSkew()
    } catch (err) {
      if (token !== logSeq.current) return
      setRecords([])
      setLogCount(0)
      setLogTruncated(false)
      setLogError(err instanceof Error ? err.message : 'Failed to query logs')
    } finally {
      if (token === logSeq.current) setLogsLoading(false)
    }
  }, [logSource, logLevel, jobId, bootScopeEnabled, selectedBoot, timeRange, updateClockSkew])


  // Event investigations end at the event timestamp, then look back across the
  // selected duration so the evidence predates the observed symptom.
  const openContext = useCallback(async (target: ContextTarget, anchorEvent?: DiagEvent) => {
    setContextTarget(target)
    setContextData(null)
    setContextError(null)
    setContextLoading(true)
    try {
      const duration = timeRange.to - timeRange.from
      const anchorTime = anchorEvent ? dayjs(anchorEvent.ts_utc).unix() : timeRange.to
      const params: DiagContextQuery = { from: anchorTime - duration, to: anchorTime }
      if (target.kind === 'job') params.job_id = target.value
      else params.app_id = target.value
      setContextData(await api.getDiagContext(params))
    } catch (err) {
      setContextData(null)
      setContextError(err instanceof Error ? err.message : 'Failed to assemble investigation context')
    } finally {
      setContextLoading(false)
    }
  }, [timeRange])

  // Land the drawer's scope onto the main view: set the matching object filter,
  // reveal the events + logs that back it, then close the drawer.
  const applyContextToMainView = useCallback((target: ContextTarget) => {
    if (target.kind === 'job') setJobId(target.value)
    else setAppId(target.value)
    setLogsOpen(true)
    setContextTarget(null)
  }, [])

  const eventActivityCounts = useMemo(() => {
    const displayed = displayEvents(events)
    const counts: Record<EventActivityKey, number> = { all: displayed.length, protection: 0, system: 0, benchmark: 0 }
    for (const item of displayed) {
      counts[eventActivityKey(item.event)] += 1
    }
    return counts
  }, [events])

  // Count in resources, not protections, so this KPI reads in the same unit as the
  // per-resource "Current active controls" list below (one app capping CPU + Disk
  // I/O is "2 active", matching its two rows).
  const protectionSummary = useMemo(() => {
    const resourceCount = (lifecycle: DiagControlLifecycle) =>
      (lifecycle.active_resources.length ? lifecycle.active_resources : lifecycle.applied_resources).length
    let activeResources = 0
    let resourcesNeedingVerification = 0
    for (const lifecycle of controlLifecycles) {
      if (lifecycle.status === 'active') activeResources += resourceCount(lifecycle)
      else if (lifecycle.status === 'requires_verification') resourcesNeedingVerification += resourceCount(lifecycle)
    }
    return { activeResources, resourcesNeedingVerification }
  }, [controlLifecycles])

  const currentControlLifecycles = useMemo(
    () => controlLifecycles.filter((lifecycle) => lifecycle.status === 'active' || lifecycle.status === 'requires_verification'),
    [controlLifecycles],
  )

  // One row per (protection, resource): each currently-limited resource stands on
  // its own, so recovering disk-IO removes exactly its row while CPU/Memory stay.
  // active_resources is already the time-correct "latest action == applied" set
  // from the backend, so a recover-then-relimit resource reappears here.
  const activeControlRows = useMemo(
    () => currentControlLifecycles.flatMap((lifecycle) => {
      const resources = lifecycle.active_resources.length ? lifecycle.active_resources : lifecycle.applied_resources
      const appName = controlLifecycleAppName(lifecycle)
      const status = controlLifecycleStatus(lifecycle)
      return resources.map((resource) => ({
        key: `${lifecycle.protection_id}:${resource}`,
        appName,
        resource,
        status,
        lastUpdatedAt: lifecycle.last_updated_at,
      }))
    }),
    [currentControlLifecycles],
  )

  const visibleActiveControlRows = useMemo(
    () => showAllActiveControls ? activeControlRows : activeControlRows.slice(0, 3),
    [activeControlRows, showAllActiveControls],
  )

  const visibleEvents = useMemo(
    () => eventActivity === 'all' ? events : events.filter((event) => eventActivityKey(event) === eventActivity),
    [eventActivity, events],
  )

  // Grouped by domain prefix (Platform / Resource / Device / …) so the ~14 raw
  // codes read as a small subject taxonomy. Derived from the events the active
  // tab actually shows, so the list never offers a category with zero rows.
  const eventCategoryOptions = useMemo(() => {
    const byGroup = new Map<string, { label: string; value: string }[]>()
    for (const category of new Set(visibleEvents.map((event) => event.category).filter(Boolean))) {
      const group = categoryGroup(category!)
      const items = byGroup.get(group) || []
      items.push({ label: categoryLabel(category), value: category! })
      byGroup.set(group, items)
    }
    const groups = CATEGORY_GROUP_ORDER
      .filter((group) => byGroup.has(group))
      .map((group) => ({ label: group, options: byGroup.get(group)!.sort((a, b) => a.label.localeCompare(b.label)) }))
    return [{ label: 'All categories', value: ALL }, ...groups]
  }, [visibleEvents])

  // Collapsed to the trust-oriented origin buckets, again only those present.
  const eventSourceOptions = useMemo(() => {
    const present = new Set(visibleEvents.map((event) => sourceBucket(event.source).key))
    const buckets = [...SOURCE_BUCKETS, OTHER_BUCKET]
      .filter((bucket) => present.has(bucket.key))
      .map((bucket) => ({ label: bucket.label, value: bucket.key }))
    return [{ label: 'All sources', value: ALL }, ...buckets]
  }, [visibleEvents])

  const filteredEvents = useMemo(() => {
    const keyword = eventKeyword.trim().toLowerCase()
    return visibleEvents.filter((event) => {
      if (eventSeverity && normalizedSeverity(event.severity) !== eventSeverity) return false
      if (eventCategory && event.category !== eventCategory) return false
      if (eventSource && sourceBucket(event.source).key !== eventSource) return false
      if (!keyword) return true
      return [
        event.summary,
        event.event_type,
        event.source,
        event.service,
        event.app_id,
        event.job_id,
        appNameFromEvent(event),
      ].some((value) => value?.toLowerCase().includes(keyword))
    })
  }, [eventCategory, eventKeyword, eventSeverity, eventSource, visibleEvents])

  const displayedEvents = useMemo(() => displayEvents(filteredEvents), [filteredEvents])

  // Memoized so the render functions are not rebuilt on every parent re-render
  // (refresh tick, filter keystroke); only ``openContext`` changing rebuilds them.
  const eventColumns = useMemo(() => [
    { title: 'Time', width: 170, render: (_: unknown, item: DisplayEvent) => dayjs(item.event.ts_utc).format('MM-DD HH:mm:ss') },
    { title: 'Severity', width: 120, render: (_: unknown, item: DisplayEvent) => severityTag(item.event.severity) },
    { title: 'Category', width: 170, render: (_: unknown, item: DisplayEvent) => categoryLabel(item.event.category) },
    { title: 'Source', width: 130, render: (_: unknown, item: DisplayEvent) => (
      <Tooltip title={item.event.source || undefined}>{sourceBucket(item.event.source).label}</Tooltip>
    ) },
    { title: 'Application', width: 160, ellipsis: true, render: (_: unknown, item: DisplayEvent) => appNameFromEvent(item.event) || item.event.app_id || '-' },
    {
      title: 'Summary',
      width: 460,
      render: (_: unknown, item: DisplayEvent) => {
        const trigger = limitTrigger(item.event)
        const summary = displayEventSummary(item)
        return (
          <div className='diagnostics-summary-cell'>
            <Text className='diagnostics-summary-text' ellipsis={{ tooltip: summary }}>{summary}</Text>
            {trigger ? <span className='diagnostics-trigger-note' style={{ color: trigger.color || COLORS.textMuted }}>({trigger.label})</span> : null}
          </div>
        )
      },
    },
    {
      title: 'Status',
      width: 140,
      render: (_: unknown, item: DisplayEvent) => {
        const ev = item.event
        const st = statusFromEvent(ev)
        const dur = formatDuration(
          (ev.attributes?.duration_seconds as number | undefined) ??
            (ev.attributes?.duration_s as number | undefined),
        )
        if (!st) return dur ? <Text type='secondary'>{dur}</Text> : <Text type='secondary'>-</Text>
        return (
          <Space size={6}>
            <span className='diagnostics-status-label' style={{ color: st.color }}>{st.label}</span>
            {dur ? <Text type='secondary'>{dur}</Text> : null}
          </Space>
        )
      },
    },
    {
      title: 'Action',
      width: 200,
      render: (_: unknown, item: DisplayEvent) => {
        const target = contextTargetForEvent(item.event)
        return (
          <Space size={8}>
            <Button size='small' onClick={() => setSelectedEvent(item.event)}>
              Details
            </Button>
            {target ? (
              <Button type='primary' size='small' icon={<SearchOutlined />} onClick={() => openContext(target, item.event)}>
                Investigate
              </Button>
            ) : null}
          </Space>
        )
      },
    },
  ], [openContext])

  const sourceOptions = useMemo(() => {
    const merged = new Set<string>(availableSources)
    for (const r of records) if (r.source) merged.add(r.source)
    return [
      { label: 'All sources', value: ALL },
      ...Array.from(merged).sort().map((s) => ({ label: s, value: s })),
    ]
  }, [availableSources, records])

  const groupedRecords = useMemo(() => groupLogRecords(records, groupRepeats), [records, groupRepeats])

  const objectFilters = useMemo(
    () => [
      jobId ? { key: 'job', label: `job: ${jobId}`, clear: () => setJobId(undefined) } : null,
      appId ? { key: 'app', label: `app: ${appId}`, clear: () => setAppId(undefined) } : null,
    ].filter(Boolean) as { key: string; label: string; clear: () => void }[],
    [jobId, appId],
  )

  const contextPressurePoints = useMemo(
    () => monitorPressurePoints(contextData?.metrics?.monitor?.series ?? []),
    [contextData],
  )
  const contextAlerts = contextData?.alerts ?? []
  const contextControlActions = useMemo(
    () => displayEvents(contextData?.control_actions ?? []),
    [contextData],
  )

  const refreshAll = useCallback(() => {
    setRangeEndEpoch(Math.floor(Date.now() / 1000))
  }, [])

  useEffect(() => {
    if (active && !wasActive.current) setRangeEndEpoch(Math.floor(Date.now() / 1000))
    wasActive.current = active
  }, [active])

  // Keep the fetch-time keyword current without making it a loadLogs dependency.
  useEffect(() => { logKeywordRef.current = logKeyword }, [logKeyword])

  // Events + lifecycles, plus the shared auto-refresh clock.
  useEffect(() => {
    if (!active) return undefined
    loadEventsAndLifecycles()
    if (refreshInterval === 'off') return undefined
    const timer = window.setInterval(() => {
      refreshAll()
    }, REFRESH_MILLISECONDS[refreshInterval])
    return () => window.clearInterval(timer)
  }, [active, refreshInterval, loadEventsAndLifecycles, refreshAll])

  // Logs load only while the panel is open. Kept in a separate effect so a log
  // filter change (source/level/scope/time) or a Search does not also refetch the
  // events table -- and, with the keyword held in a ref, typing does not refetch.
  useEffect(() => {
    if (!active || !logsOpen) return
    loadLogs()
  }, [active, logsOpen, loadLogs])

  const openContextHistory = useCallback(() => {
    if (!contextData?.window.from || !contextData.window.to) return
    onOpenHistory({ from: contextData.window.from, to: contextData.window.to })
    setContextTarget(null)
  }, [contextData, onOpenHistory])

  // Scoping to a job means the user is chasing evidence -- surface
  // the log panel (which already receives those filters) instead of hiding it.
  useEffect(() => {
    if (jobId) setLogsOpen(true)
  }, [jobId])

  // A boot-scoped investigation needs host logs as well as diagnostic events.
  useEffect(() => {
    if (!bootScopeEnabled) return
    if (!boots.length && !bootsLoading) loadBoots()
    setLogsOpen(true)
  }, [bootScopeEnabled, boots.length, bootsLoading, loadBoots])

  return (
    <Card className='diagnostics-page'>
      <div className='diagnostics-toolbar'>
        <Row className='diagnostics-filter-row' gutter={[8, 8]} align='middle' justify='space-between'>
          <Col flex='auto'>
            <Space className='diagnostics-filter-group' wrap size={8}>
              <Space className='diagnostics-range-controls' wrap size={8}>
                <Text className='diagnostics-control-label' strong>Select time range</Text>
                <Segmented
                  value={windowKey}
                  onChange={(value) => setWindowKey(value as WindowKey)}
                  options={bootScopeEnabled ? [
                    { label: '15m', value: '15m' },
                    { label: '1h', value: '1h' },
                    { label: '6h', value: '6h' },
                    { label: '24h', value: '24h' },
                    { label: 'Custom', value: 'custom' },
                    { label: 'Entire boot', value: 'entire' },
                  ] : [
                    { label: '15m', value: '15m' },
                    { label: '1h', value: '1h' },
                    { label: '6h', value: '6h' },
                    { label: '24h', value: '24h' },
                    { label: 'Custom', value: 'custom' },
                  ]}
                />
                <Button
                  className='diagnostics-boot-toggle'
                  type={bootScopeEnabled ? 'primary' : 'default'}
                  onClick={() => {
                    setBootScopeEnabled((enabled) => !enabled)
                    if (windowKey === 'entire') setWindowKey('1h')
                  }}
                >
                  Boot
                </Button>
                {windowKey === 'custom' ? (
                  <DatePicker.RangePicker
                    showTime={{ format: 'HH:mm' }}
                    format='MM-DD HH:mm'
                    value={(bootScopeEnabled ? bootCustomRange : customRange)
                      ? [dayjs.unix((bootScopeEnabled ? bootCustomRange : customRange)!.from), dayjs.unix((bootScopeEnabled ? bootCustomRange : customRange)!.to)]
                      : null}
                    disabledDate={bootScopeEnabled && selectedBoot?.first_ts ? (date) => {
                      const from = dayjs.unix(selectedBoot.first_ts!).startOf('day')
                      const to = dayjs.unix(selectedBoot.running ? rangeEndEpoch + (clockSkewSec ?? 0) : selectedBoot.last_ts ?? rangeEndEpoch).endOf('day')
                      return date.endOf('day').isBefore(from) || date.startOf('day').isAfter(to)
                    } : undefined}
                    onChange={(vals) => {
                      if (vals && vals[0] && vals[1]) {
                        const range = { from: vals[0].unix(), to: vals[1].unix() }
                        if (bootScopeEnabled) setBootCustomRange(range)
                        else setCustomRange(range)
                      } else {
                        if (bootScopeEnabled) setBootCustomRange(null)
                        else setCustomRange(null)
                      }
                    }}
                  />
                ) : null}
                {bootScopeEnabled ? (
                  <Select
                    aria-label='Boot session'
                    className='diagnostics-boot-select'
                    placeholder='Select a boot session'
                    loading={bootsLoading}
                    value={selectedBootId ?? undefined}
                    onChange={(value) => {
                      setSelectedBootId(value)
                      setBootCustomRange(null)
                    }}
                    options={boots.map((b) => ({ label: formatBootLabel(b), value: b.boot_id }))}
                    notFoundContent={bootsLoading ? 'Loading…' : 'No boot sessions (journald unavailable)'}
                  />
                ) : null}
                {bootScopeEnabled && !selectedBoot ? (
                  <Text className='diagnostics-range-summary' type='warning'>Select a boot session (showing the selected recent range)</Text>
                ) : bootScopeEnabled && selectedBoot && !selectedBoot.first_ts ? (
                  <Text className='diagnostics-range-summary' type='warning'>This boot has no known time bounds; showing the selected recent range</Text>
                ) : bootScopeEnabled && windowKey === 'custom' && !bootCustomRange ? (
                  <Text className='diagnostics-range-summary' type='warning'>Select a range within this boot (showing the last hour)</Text>
                ) : !bootScopeEnabled && windowKey === 'custom' && !customRange ? (
                  <Text className='diagnostics-range-summary' type='warning'>Select a custom range (showing the last hour)</Text>
                ) : (
                  <Text className='diagnostics-range-summary' type='secondary'>
                    {dayjs.unix(timeRange.from).format('MM-DD HH:mm:ss')} - {dayjs.unix(timeRange.to).format('MM-DD HH:mm:ss')}
                    {bootScopeEnabled && selectedBoot ? ` · ${formatBootLabel(selectedBoot)}` : ''}
                  </Text>
                )}
              </Space>
            </Space>
          </Col>
          <Col>
            <Space className='diagnostics-toolbar-actions' size={8} wrap>
              <Select
                aria-label='Auto refresh interval'
                className='diagnostics-refresh-select'
                value={refreshInterval}
                onChange={(value) => setRefreshInterval(value)}
                options={[
                  { label: 'Refresh off', value: 'off' },
                  { label: 'Every 5s', value: '5s' },
                  { label: 'Every 10s', value: '10s' },
                  { label: 'Every 30s', value: '30s' },
                ]}
              />
              <Tooltip title='Refresh diagnostics'>
                <Button aria-label='Refresh diagnostics' icon={<ReloadOutlined />} onClick={refreshAll} loading={eventsLoading} />
              </Tooltip>
            </Space>
          </Col>
        </Row>
        <Space className='diagnostics-range' wrap size={8}>
          {objectFilters.length > 0 ? (
            <Space size={4} wrap>
              <Text type='secondary'>Related to:</Text>
              {objectFilters.map((f) => (
                <Tag key={f.key} closable onClose={f.clear} color='blue'>{f.label}</Tag>
              ))}
            </Space>
          ) : null}
        </Space>
      </div>

      {clockSkewSec !== null && Math.abs(clockSkewSec) >= 60 ? (
        <Alert
          className='diagnostics-clock-skew'
          type='warning'
          showIcon
          message={`Server clock is ${clockSkewSec > 0 ? 'ahead' : 'behind'} by about ${Math.round(Math.abs(clockSkewSec) / 60)} min. Events and logs use server time.`}
        />
      ) : null}

      <section className='diagnostics-overview' aria-label='Diagnostic overview'>
        <div className='diagnostics-event-activity'>
          <Text className='diagnostics-overview-heading' type='secondary'>Event activity</Text>
          <div className='diagnostics-event-metrics' role='tablist' aria-label='Event category'>
            <button
              type='button'
              role='tab'
              aria-selected={eventActivity === 'all'}
              className='diagnostics-overview-metric'
              onClick={() => { setEventActivity('all'); setEventPage(1) }}
            >
              <Text className='diagnostics-overview-value'>{(eventsLoading && events.length === 0) || eventsError ? '-' : eventActivityCounts.all}</Text>
              <Text type='secondary'>All records</Text>
            </button>
            <button
              type='button'
              role='tab'
              aria-selected={eventActivity === 'protection'}
              className='diagnostics-overview-metric'
              onClick={() => { setEventActivity('protection'); setEventPage(1) }}
            >
              <Text className='diagnostics-overview-value'>{eventsLoading && events.length === 0 ? '-' : eventActivityCounts.protection}</Text>
              <Text type='secondary'>Resource control</Text>
              {(!controlLifecyclesLoading || controlLifecycles.length > 0) && !controlLifecyclesError ? (
                <Text className='diagnostics-metric-sub' type={protectionSummary.resourcesNeedingVerification > 0 ? 'warning' : 'secondary'}>
                  {protectionSummary.activeResources} active now
                  {protectionSummary.resourcesNeedingVerification > 0 ? ` · ${protectionSummary.resourcesNeedingVerification} need verification` : ''}
                </Text>
              ) : null}
            </button>
            <button
              type='button'
              role='tab'
              aria-selected={eventActivity === 'system'}
              className='diagnostics-overview-metric'
              onClick={() => { setEventActivity('system'); setEventPage(1) }}
            >
              <Text className='diagnostics-overview-value'>{eventsLoading && events.length === 0 ? '-' : eventActivityCounts.system}</Text>
              <Text type='secondary'>System events</Text>
            </button>
            <button
              type='button'
              role='tab'
              aria-selected={eventActivity === 'benchmark'}
              className='diagnostics-overview-metric'
              onClick={() => { setEventActivity('benchmark'); setEventPage(1) }}
            >
              <Text className='diagnostics-overview-value'>{eventsLoading && events.length === 0 ? '-' : eventActivityCounts.benchmark}</Text>
              <Text type='secondary'>Benchmark runs</Text>
            </button>
          </div>
        </div>
      </section>

      {controlLifecyclesError ? (
        <Alert
          className='diagnostics-activity-empty'
          type='warning'
          showIcon
          message='Current control status is unavailable'
          description={controlLifecyclesError}
        />
      ) : null}

      {!eventsLoading && eventsError ? (
        <Alert
          className='diagnostics-activity-empty'
          type='error'
          showIcon
          message='Diagnostic events could not be loaded'
          description={eventsError}
          action={<Button size='small' onClick={loadEventsAndLifecycles}>Retry</Button>}
        />
      ) : !eventsLoading && events.length === 0 && currentControlLifecycles.length === 0 ? (
        <Alert
          className='diagnostics-activity-empty'
          type='success'
          showIcon
          message='No event records in the selected range'
          description='The system reported no abnormal events for this period. Try widening the time range if you expected activity.'
        />
      ) : null}


      {eventsLoading || events.length > 0 || currentControlLifecycles.length > 0 ? (
      <Card
        size='small'
        title={`Event records · ${EVENT_ACTIVITY_LABELS[eventActivity]} (${eventsLoading ? '-' : displayedEvents.length})`}
        className='diagnostics-panel'
      >
        {/* Current active controls (host-wide point-in-time state), folded above the
            control event history since the two answer "what is controlled now" and
            "how did it get here" together. */}
        {eventActivity === 'protection' ? (
          <div className='diagnostics-active-controls'>
            <div className='diagnostics-active-controls-head'>
              <div>
                <Text strong>Current active controls{controlLifecyclesLoading || controlLifecyclesError ? '' : ` (${activeControlRows.length})`}</Text>
                <Text className='diagnostics-active-controls-scope' type='secondary'>Host-wide</Text>
              </div>
            </div>
            {controlLifecyclesLoading ? (
              <Text type='secondary'>Loading…</Text>
            ) : controlLifecyclesError ? (
              <Text type='warning'>{controlLifecyclesError}</Text>
            ) : activeControlRows.length === 0 ? (
              <Text type='secondary'>No controls are active right now</Text>
            ) : (
              <div className='diagnostics-active-controls-list'>
                {visibleActiveControlRows.map((row) => (
                  <div className='diagnostics-active-control-row' key={row.key}>
                    <Text className='diagnostics-active-control-app' ellipsis={{ tooltip: row.appName }}>{row.appName}</Text>
                    <span className='diagnostics-active-control-sep'>·</span>
                    <Text type='secondary'>{resourceLabel(row.resource)}</Text>
                    <span className='diagnostics-active-control-sep'>·</span>
                    <span className='diagnostics-status-label' style={{ color: row.status.color }}>{row.status.label}</span>
                    <span className='diagnostics-active-control-sep'>·</span>
                    <Text type='secondary'>Updated {formatActivityTime(row.lastUpdatedAt)}</Text>
                  </div>
                ))}
                {activeControlRows.length > 3 ? (
                  <Button
                    className='diagnostics-active-controls-toggle'
                    type='link'
                    size='small'
                    onClick={() => setShowAllActiveControls((visible) => !visible)}
                  >
                    {showAllActiveControls ? 'Show less' : `Show ${activeControlRows.length - 3} more active controls`}
                  </Button>
                ) : null}
              </div>
            )}
          </div>
        ) : null}
        <Space className='diagnostics-event-controls' wrap size={8}>
          <Input.Search
            allowClear
            placeholder='Search events'
            className='diagnostics-search'
            value={eventKeyword}
            onChange={(event) => {
              setEventKeyword(event.target.value)
              setEventPage(1)
            }}
          />
          <Select
            className='diagnostics-severity-select'
            aria-label='Event severity'
            value={eventSeverity || ALL}
            onChange={(value) => {
              setEventSeverity(value === ALL ? undefined : value)
              setEventPage(1)
            }}
            options={[
              { label: 'All severities', value: ALL },
              { label: 'INFO', value: 'info' },
              { label: 'WARNING', value: 'warning' },
              { label: 'ERROR', value: 'error' },
              { label: 'CRITICAL', value: 'critical' },
            ]}
          />
          <Select
            className='diagnostics-category-select'
            aria-label='Event category'
            value={eventCategory || ALL}
            onChange={(value) => {
              setEventCategory(value === ALL ? undefined : value)
              setEventPage(1)
            }}
            options={eventCategoryOptions}
          />
          <Select
            className='diagnostics-log-source-select'
            aria-label='Event source'
            value={eventSource || ALL}
            onChange={(value) => {
              setEventSource(value === ALL ? undefined : value)
              setEventPage(1)
            }}
            options={eventSourceOptions}
          />
          <Text className='diagnostics-event-count' type='secondary'>
            {eventKeyword || eventSeverity || eventCategory || eventSource
              ? `${displayedEvents.length} matching of ${displayEvents(visibleEvents).length} ${displayEvents(visibleEvents).length === 1 ? 'event record' : 'event records'}`
              : `${displayedEvents.length} ${displayedEvents.length === 1 ? 'event record' : 'event records'}`}
          </Text>
        </Space>
        <Table
          rowKey='event_id'
          loading={eventsLoading}
          dataSource={displayedEvents}
          pagination={{ current: eventPage, pageSize: 15, showSizeChanger: false, onChange: setEventPage }}
          size='small'
          className='diagnostics-table diagnostics-events-table'
          scroll={{ x: 1300 }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={`No ${EVENT_ACTIVITY_LABELS[eventActivity].toLowerCase()} recorded in the selected range`} /> }}
          columns={eventColumns}
        />
      </Card>
      ) : null}

      <Collapse
        activeKey={logsOpen ? ['logs'] : []}
        onChange={(keys) => setLogsOpen((Array.isArray(keys) ? keys : [keys]).includes('logs'))}
        className='diagnostics-logs'
        items={[
          {
            key: 'logs',
            label: objectFilters.length
              ? `Logs · filtered by ${objectFilters.map((f) => f.label).join(', ')}`
              : 'Logs in selected time range',
            children: (
              <Space direction='vertical' size={10} style={{ width: '100%' }}>
                <Space className='diagnostics-log-controls' wrap size={8}>
                  <Input.Search
                    allowClear
                    placeholder='Search logs in selected range'
                    className='diagnostics-search'
                    value={logKeyword}
                    onChange={(e) => setLogKeyword(e.target.value)}
                    onSearch={(value) => { logKeywordRef.current = value; loadLogs() }}
                  />
                  <Select
                    className='diagnostics-severity-select'
                    aria-label='Log level'
                    value={logLevel || ALL}
                    onChange={(v) => setLogLevel(v === ALL ? undefined : v)}
                    options={[
                      { label: 'All log levels', value: ALL },
                      { label: 'INFO', value: 'info' },
                      { label: 'WARNING+', value: 'warning' },
                      { label: 'ERROR+', value: 'error' },
                      { label: 'CRITICAL', value: 'critical' },
                    ]}
                  />
                  <Select
                    allowClear
                    aria-label='Log source'
                    placeholder='Source'
                    className='diagnostics-log-source-select'
                    value={logSource || ALL}
                    onChange={(v) => setLogSource(v === ALL ? undefined : v)}
                    options={sourceOptions}
                  />
                  <Button icon={<ReloadOutlined />} onClick={loadLogs} loading={logsLoading}>Search</Button>
                  <Checkbox checked={groupRepeats} onChange={(e) => setGroupRepeats(e.target.checked)}>
                    Group repeats
                  </Checkbox>
                  <Text className='diagnostics-log-count' type='secondary'>
                    {logCount} logs{logTruncated ? ' (truncated)' : ''} in selected range
                    {groupRepeats && groupedRecords.length < records.length ? ` · ${groupedRecords.length} groups` : ''}
                  </Text>
                </Space>

                {logError ? (
                  <Alert type='warning' showIcon message='Log query failed' description={logError} />
                ) : null}

                <Table
                  rowKey={(g: LogGroup) => g.key}
                  loading={logsLoading}
                  dataSource={groupedRecords}
                  pagination={{ pageSize: 20, showSizeChanger: false }}
                  size='small'
                  className='diagnostics-table'
                  scroll={{ x: 860 }}
                  columns={[
                    {
                      title: 'Time',
                      width: 180,
                      render: (_: unknown, g: LogGroup) => g.count > 1
                        ? <Tooltip title={`${formatLogTs(g.items[g.items.length - 1])} → ${formatLogTs(g.rep)}`}><span>{formatLogTs(g.rep)}</span></Tooltip>
                        : formatLogTs(g.rep),
                    },
                    { title: 'Level', width: 120, render: (_: unknown, g: LogGroup) => severityTag(g.rep.level) },
                    { title: 'Service', width: 180, render: (_: unknown, g: LogGroup) => g.rep.service || g.rep.logger || '-' },
                    { title: 'Source', width: 110, render: (_: unknown, g: LogGroup) => g.rep.source || '-' },
                    {
                      title: 'Message',
                      render: (_: unknown, g: LogGroup) => (
                        <div className='diagnostics-summary-cell'>
                          {g.count > 1 ? <Tag className='diagnostics-trigger-tag' color={COLORS.accent}>×{g.count}</Tag> : null}
                          <Text className='diagnostics-summary-text' style={{ color: COLORS.text }} ellipsis={{ tooltip: g.rep.message }}>{g.rep.message}</Text>
                        </div>
                      ),
                    },
                  ]}
                  expandable={{
                    expandedRowRender: (g: LogGroup) => (
                      <Space direction='vertical' size={6} style={{ width: '100%' }}>
                        {g.count > 1 ? (
                          <>
                            <Text type='secondary'>{g.count} occurrences · {formatLogTs(g.items[g.items.length - 1])} → {formatLogTs(g.rep)}</Text>
                            <div className='diagnostics-log-excerpts'>
                              {g.items.slice(0, 50).map((r, i) => (
                                <div className='diagnostics-log-excerpt' key={`${g.key}-${r.ts_epoch}-${i}`}>
                                  <Text className='diagnostics-evidence-time' type='secondary'>{formatLogTs(r)}</Text>
                                  <Text style={{ color: COLORS.text }}>{r.message}</Text>
                                </div>
                              ))}
                              {g.items.length > 50 ? <Text type='secondary'>Showing the first 50 of {g.count} occurrences.</Text> : null}
                            </div>
                          </>
                        ) : (
                          <pre className='diagnostics-code-block'>{g.rep.message || ''}</pre>
                        )}
                        {g.rep.app_id || g.rep.job_id ? (
                          <Text type='secondary'>app={g.rep.app_id || '-'} · job={g.rep.job_id || '-'}</Text>
                        ) : null}
                        {g.rep.fields && Object.keys(g.rep.fields).length ? (
                          <pre className='diagnostics-code-block'>{JSON.stringify(g.rep.fields, null, 2)}</pre>
                        ) : null}
                      </Space>
                    ),
                  }}
                />
              </Space>
            ),
          },
        ]}
      />

      <Drawer title='Event Detail' open={!!selectedEvent} onClose={() => setSelectedEvent(null)} width={700}>
        {selectedEvent ? (
          <Space direction='vertical' size={10} style={{ width: '100%' }}>
            <Space wrap>
              {severityTag(selectedEvent.severity)}
            </Space>
            <Text strong>Detected: {dayjs(selectedEvent.ts_utc).format('YYYY-MM-DD HH:mm:ss')}</Text>
            <Text strong>Category: {categoryLabel(selectedEvent.category)}</Text>
            <Text strong>Type: {selectedEvent.event_type}</Text>
            <Text strong>Source: {sourceBucket(selectedEvent.source).label}{selectedEvent.source ? ` (${selectedEvent.source})` : ''}</Text>
            <Text strong>Summary:</Text>
            <Text>{selectedEvent.summary}</Text>
            {(() => {
              const controlDetails = controlDetailsFromEvent(selectedEvent)
              return controlDetails.length ? (
                <>
                  <Text strong>Resource control:</Text>
                  <Table
                    className='diagnostics-control-details-table'
                    columns={[
                      { title: 'Resource', dataIndex: 'resource', width: 150 },
                      { title: 'Applied limit', dataIndex: 'limit' },
                    ]}
                    dataSource={controlDetails}
                    pagination={false}
                    rowKey='resource'
                    size='small'
                  />
                </>
              ) : null
            })()}
            <Text strong>Related objects:</Text>
            <Space size={6} wrap>
              {selectedEvent.app_id ? <Tag className='diagnostics-related-tag' onClick={() => { setAppId(selectedEvent.app_id || undefined); setSelectedEvent(null) }}>app: {selectedEvent.app_id}</Tag> : null}
              {selectedEvent.job_id ? <Tag className='diagnostics-related-tag' color='purple' onClick={() => { setJobId(selectedEvent.job_id || undefined); setSelectedEvent(null) }}>job: {selectedEvent.job_id}</Tag> : null}
              {!selectedEvent.app_id && !selectedEvent.job_id ? <Text type='secondary'>-</Text> : null}
            </Space>
            {!controlDetailsFromEvent(selectedEvent).length && !isPressureTransitionEvent(selectedEvent) ? (
              <>
                <Text strong>Attributes:</Text>
                <pre className='diagnostics-code-block'>{JSON.stringify(selectedEvent.attributes || {}, null, 2)}</pre>
              </>
            ) : null}
          </Space>
        ) : null}
      </Drawer>

      <Drawer
        title={contextTarget ? `Investigation · ${contextTargetLabel(contextTarget)}` : 'Investigation'}
        open={!!contextTarget}
        onClose={() => setContextTarget(null)}
        width={760}
        extra={contextTarget ? (
          <Space size={8}>
            <Button icon={<LineChartOutlined />} size='small' disabled={!contextData?.window.from || !contextData.window.to} onClick={openContextHistory}>
              Open in History
            </Button>
            <Button type='primary' size='small' onClick={() => applyContextToMainView(contextTarget)}>
              Open in main view
            </Button>
          </Space>
        ) : null}
      >
        {contextLoading ? (
          <Text type='secondary'>Assembling evidence…</Text>
        ) : contextError ? (
          <Alert
            type='error'
            showIcon
            message='Investigation context could not be loaded'
            description={contextError}
            action={contextTarget ? <Button size='small' onClick={() => openContext(contextTarget)}>Retry</Button> : null}
          />
        ) : contextData ? (
          <Space direction='vertical' size={16} style={{ width: '100%' }}>
            <div className='diagnostics-investigation-summary'>
              <Space className='diagnostics-investigation-summary-header' align='start'>
                <Text className='diagnostics-investigation-heading' strong>
                  {contextTarget ? contextTargetLabel(contextTarget) : 'Investigation'}
                </Text>
              </Space>
              <Text type='secondary'>
                Scope
                {' · '}
                {contextData.window.from ? dayjs.unix(contextData.window.from).format('MM-DD HH:mm:ss') : '-'}
                {' → '}
                {contextData.window.to ? dayjs.unix(contextData.window.to).format('MM-DD HH:mm:ss') : '-'}
              </Text>
            </div>

            {(() => {
              const scopeDetails = scopeDetailsFromEvents(contextControlActions.map((item) => item.event))
              if (!scopeDetails.scopes.size) return null
              return (
                <div className='diagnostics-investigation-section'>
                  <Text className='diagnostics-investigation-heading' strong>Scopes (cgroups)</Text>
                  {[...scopeDetails.scopes.entries()].map(([cgroup, processes]) => (
                    <div className='diagnostics-scope-detail' key={cgroup || 'snapshot'}>
                      <Text code>{cgroup || 'Scope snapshot'}</Text>
                      <Text type='secondary'>
                        {' '}
                        {processes.length
                          ? processes.map((process) => `${process.processName} (PID ${process.pid})`).join(', ')
                          : '—'}
                      </Text>
                    </div>
                  ))}
                </div>
              )
            })()}

            {contextData.concurrent_jobs.length ? (
              <div className='diagnostics-investigation-section'>
                <Text className='diagnostics-investigation-heading' strong>Concurrent jobs</Text>
                <Space size={4} wrap>
                  {contextData.concurrent_jobs.map((j) => (
                    <Tag key={j} className='diagnostics-related-tag' color='purple' onClick={() => openContext({ kind: 'job', value: j })}>
                      job: {j}
                    </Tag>
                  ))}
                </Space>
              </div>
            ) : null}

            <div className='diagnostics-investigation-section'>
              <div className='diagnostics-investigation-section-header'>
                <Text className='diagnostics-investigation-heading' strong>Resource protection actions</Text>
                <Text type='secondary'>{contextControlActions.length}</Text>
              </div>
              {contextControlActions.length ? (
                <div className='diagnostics-evidence-timeline'>
                  {contextControlActions.map((item) => {
                    const trigger = limitTrigger(item.event)
                    return (
                    <div className='diagnostics-evidence-item diagnostics-evidence-item-interactive' key={item.event.event_id} onClick={() => setSelectedEvent(item.event)}>
                      <Space size={8} wrap>
                        <Text className='diagnostics-evidence-time' type='secondary'>{dayjs(item.event.ts_utc).format('MM-DD HH:mm:ss')}</Text>
                        {severityTag(item.event.severity)}
                        <Text>{displayEventSummary(item)}</Text>
                        {trigger ? <span className='diagnostics-trigger-note' style={{ color: trigger.color || COLORS.textMuted }}>{trigger.label}</span> : null}
                      </Space>
                    </div>
                    )
                  })}
                </div>
              ) : (
                <Text type='secondary'>No resource protection actions in window</Text>
              )}
            </div>

            <div className='diagnostics-investigation-section'>
              <div className='diagnostics-investigation-section-header'>
                <Text className='diagnostics-investigation-heading' strong>Event history</Text>
                <Text type='secondary'>{contextData.events.length}</Text>
              </div>
              {contextData.events.length ? (
                <div className='diagnostics-evidence-timeline'>
                  {contextData.events.map((ev) => (
                    <div className='diagnostics-evidence-item diagnostics-evidence-item-interactive' key={ev.event_id} onClick={() => setSelectedEvent(ev)}>
                      <Space size={8} wrap>
                        <Text className='diagnostics-evidence-time' type='secondary'>{dayjs(ev.ts_utc).format('MM-DD HH:mm:ss')}</Text>
                        {severityTag(ev.severity)}
                        <Text type='secondary'>{categoryLabel(ev.category)}</Text>
                        <Text>{ev.summary || ev.event_type}</Text>
                      </Space>
                    </div>
                  ))}
                </div>
              ) : (
                <Text type='secondary'>No event history in window</Text>
              )}
            </div>

            <div className='diagnostics-investigation-section'>
              <Text className='diagnostics-investigation-heading' strong>Resource pressure (window)</Text>
              {contextPressurePoints.length ? (
                <div className='diagnostics-pressure-chart'>
                  <ResponsiveContainer>
                    <LineChart data={contextPressurePoints} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                      <CartesianGrid strokeDasharray='3 3' stroke='rgba(255,255,255,0.12)' />
                      <XAxis dataKey='ts' type='number' domain={['dataMin', 'dataMax']} tickFormatter={(value) => dayjs.unix(Number(value)).format('HH:mm')} stroke={COLORS.textMuted} />
                      <YAxis domain={[0, 100]} width={44} allowDecimals={false} tickFormatter={(value) => `${value}%`} stroke={COLORS.textMuted} />
                      <RechartsTooltip
                        contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, borderRadius: 6 }}
                        labelFormatter={(value) => dayjs.unix(Number(value)).format('MM-DD HH:mm:ss')}
                        formatter={(value, name) => [typeof value === 'number' ? `${Math.round(value)}%` : '-', name]}
                      />
                      <Legend wrapperStyle={{ fontSize: 12 }} />
                      <Line type='monotone' dataKey='system' name='System' stroke={COLORS.accent} strokeWidth={2} dot={false} isAnimationActive={false} />
                      <Line type='monotone' dataKey='disk' name='Disk I/O' stroke={COLORS.yellow} strokeWidth={2} dot={false} isAnimationActive={false} />
                      <Line type='monotone' dataKey='network' name='Network' stroke={COLORS.green} strokeWidth={2} dot={false} isAnimationActive={false} />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <Text type='secondary'>No metric samples in window</Text>
              )}
            </div>

            <div className='diagnostics-investigation-section'>
              <div className='diagnostics-investigation-section-header'>
                <Text className='diagnostics-investigation-heading' strong>System alerts</Text>
                <Text type='secondary'>{contextAlerts.length}</Text>
              </div>
              {contextAlerts.length ? (
                <div className='diagnostics-evidence-timeline'>
                  {contextAlerts.map((alert) => (
                    <div className='diagnostics-evidence-item' key={alert.dedup_key}>
                      <Space size={8} wrap>
                        <Text className='diagnostics-evidence-time' type='secondary'>{formatActivityTime(alert.last_fired_at)}</Text>
                        {severityTag(alert.severity)}
                        <Text>{alert.summary || alert.event_type}</Text>
                        <Tag color={alert.acknowledged_at ? COLORS.green : COLORS.orange}>{alert.acknowledged_at ? 'Acknowledged' : 'Unacknowledged'}</Tag>
                      </Space>
                    </div>
                  ))}
                </div>
              ) : (
                <Text type='secondary'>No system alerts in window</Text>
              )}
            </div>

            <div className='diagnostics-investigation-section'>
              <div className='diagnostics-investigation-section-header'>
                <Text className='diagnostics-investigation-heading' strong>Related logs</Text>
                <Text type='secondary'>{contextData.logs.count}{contextData.logs.truncated ? ' (truncated)' : ''}</Text>
              </div>
              {contextData.logs.records.length ? (
                <div className='diagnostics-log-excerpts'>
                  {contextData.logs.records.slice(0, 5).map((r) => (
                    <div
                      className='diagnostics-log-excerpt'
                      key={`${r.source}-${r.ts_epoch}-${r.message.slice(0, 24)}`}
                    >
                      <Space size={8} wrap>
                        <Text className='diagnostics-evidence-time' type='secondary'>{formatLogTs(r)}</Text>
                        {severityTag(r.level)}
                        <Text type='secondary'>{r.service || r.logger || r.source}</Text>
                      </Space>
                      <Text style={{ color: COLORS.text }}>{r.message}</Text>
                    </div>
                  ))}
                  {contextData.logs.truncated || contextData.logs.records.length > 5 ? (
                    <Text type='secondary' className='diagnostics-log-truncation'>
                      Showing the first 5 related logs. Open in main view to inspect the filtered log query.
                    </Text>
                  ) : null}
                </div>
              ) : (
                <Text type='secondary'>No logs in window</Text>
              )}
            </div>
          </Space>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description='No context available for this scope' />
        )}
      </Drawer>

    </Card>
  )
}
