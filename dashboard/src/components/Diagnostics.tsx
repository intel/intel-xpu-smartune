import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Collapse,
  DatePicker,
  Dropdown,
  Drawer,
  Empty,
  Input,
  message,
  Modal,
  Row,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { BellOutlined, BulbOutlined, ClockCircleOutlined, DownOutlined, DownloadOutlined, FileTextOutlined, InfoCircleOutlined, LineChartOutlined, ReloadOutlined, RollbackOutlined, SearchOutlined, UpOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import {
  Bar,
  BarChart,
  Brush,
  CartesianGrid,
  Cell,
  ComposedChart,
  LabelList,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api, getLatestServerTime } from '../api/client'
import { indexMetrics, formatWithUnit, metricLabel } from '../utils/benchMetrics'
import type {
  DiagBenchCase,
  DiagBoot,
  DiagContextData,
  DiagContextQuery,
  DiagDigest,
  DiagAlert,
  DiagControlLifecycle,
  DiagEvent,
  DiagFinding,
  DiagLogRecord,
  DiagResourceUtilization,
  DiagResourceTrendPoint,
  DiagSeverity,
  LimitSnapshotData,
} from '../api/types'
import { COLORS } from '../styles/theme'
import '../styles/diagnostics.css'

// Benchmark KPI results for the investigated job, rendered in the investigation
// drawer. The numbers arrive on the diagnostics context whether the run is still
// on disk ("disk") or was deleted from the Results tab and read back from the
// copy frozen on its completion event ("persisted") -- the point of the feature
// is that a deleted job still shows here, just without its logs and timeline.
function BenchmarkResultsSection({
  bench,
}: {
  bench: DiagContextData['metrics']['benchmark']
}) {
  const summary = bench.summary
  if (!summary || !summary.cases?.length) return null
  const index = indexMetrics(summary.metrics)
  const columns = [
    {
      title: 'Case',
      key: 'case',
      fixed: 'left' as const,
      render: (_: unknown, row: DiagBenchCase) => (
        <div>
          <Typography.Text style={{ color: COLORS.text }}>{row.case || row.model}</Typography.Text>
          {row.failure_reason ? (
            <div>
              <Typography.Text type='danger' style={{ fontSize: 12 }}>{row.failure_reason}</Typography.Text>
            </div>
          ) : null}
        </div>
      ),
    },
    {
      title: 'Status',
      key: 'status',
      render: (_: unknown, row: DiagBenchCase) => (
        <Tag color={row.status === 'ok' ? COLORS.green : COLORS.orange}>{row.status || '-'}</Tag>
      ),
    },
    ...summary.metrics.map((meta) => ({
      title: metricLabel(meta.key, index),
      key: meta.key,
      align: 'right' as const,
      render: (_: unknown, row: DiagBenchCase) => {
        const value = row.metrics?.[meta.key]
        return typeof value === 'number' ? formatWithUnit(value, meta) : '-'
      },
    })),
  ]
  return (
    <div className='diagnostics-investigation-section'>
      <div className='diagnostics-investigation-section-header'>
        <Typography.Text className='diagnostics-investigation-heading' strong>Benchmark results</Typography.Text>
        {bench.source === 'persisted' ? (
          <Tag color={COLORS.orange}>Persisted summary · logs deleted</Tag>
        ) : bench.source === 'disk' ? (
          <Tag color={COLORS.green}>Live</Tag>
        ) : null}
      </div>
      <Space size={6} wrap style={{ marginBottom: 8 }}>
        <Tag color={COLORS.green}>{summary.counts.ok} ok</Tag>
        {summary.counts.failed ? <Tag color={COLORS.orange}>{summary.counts.failed} failed</Tag> : null}
        {summary.meta.ov?.map((ov) => <Tag key={`ov-${ov}`}>OV {ov}</Tag>)}
        {summary.meta.devices?.map((device) => <Tag key={`dev-${device}`}>{device.toUpperCase()}</Tag>)}
      </Space>
      <Table
        size='small'
        rowKey={(row, i) => `${row.case}-${i}`}
        columns={columns}
        dataSource={summary.cases}
        pagination={false}
        scroll={{ x: 'max-content' }}
      />
    </div>
  )
}

const { Text } = Typography

interface Props {
  active: boolean
  openAlertsSignal?: number
  onOpenHistory: (range: { from: number; to: number }) => void
}

type WindowKey = '15m' | '1h' | '6h' | '24h' | 'custom' | 'entire'
type RefreshInterval = 'off' | '5s' | '10s' | '30s'

const ALL = '__all__'

const WINDOW_SECONDS: Record<Exclude<WindowKey, 'custom' | 'entire'>, number> = {
  '15m': 15 * 60,
  '1h': 60 * 60,
  '6h': 6 * 60 * 60,
  '24h': 24 * 60 * 60,
}

// A live window's upper bound tracks "now", but "now" is only known to ±1-2s via the
// server-clock skew estimate (whole-second Date header, refreshed on drift). Querying
// [from, now] therefore drops an event stamped in the last second or two -- e.g. the
// RECOVERED emitted by Resume. Pad the *query* upper bound (not the displayed range)
// a few minutes ahead so a just-emitted event and skew jitter always fall inside.
const LIVE_WINDOW_LEAD_SECONDS = 5 * 60
const INVESTIGATION_EVENT_WINDOW_SECONDS = 5 * 60

const REFRESH_MILLISECONDS: Record<Exclude<RefreshInterval, 'off'>, number> = {
  '5s': 5000,
  '10s': 10000,
  '30s': 30000,
}

const DIAGNOSTIC_ERROR_COLOR = '#f06b7b'

const severityColors: Record<string, string> = {
  debug: COLORS.textMuted,
  info: COLORS.accent,
  warning: COLORS.yellow,
  error: DIAGNOSTIC_ERROR_COLOR,
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

const EVENT_DOMAIN_OPTIONS = [
  { value: 'compute', label: 'Compute & accelerators' },
  { value: 'data-path', label: 'Memory, storage & network' },
  { value: 'services', label: 'Services & workloads' },
  { value: 'hardware', label: 'Host system' },
  { value: 'platform', label: 'Platform management' },
  { value: 'other', label: 'Other' },
] as const

const EVENT_DOMAIN_CATALOG = [
  { label: 'Compute & accelerators', description: 'CPU, GPU/XPU and NPU performance, pressure or faults.' },
  { label: 'Memory, storage & network', description: 'Memory pressure, OOM, disk I/O, network and PCIe issues.' },
  { label: 'Services & workloads', description: 'Service lifecycle and benchmark job results.' },
  { label: 'Host system', description: 'System pressure, OS, kernel, drivers, temperature, power and device faults.' },
  { label: 'Platform management', description: 'SmarTune limits, monitoring, configuration and service availability.' },
  { label: 'Other', description: 'Events that cannot yet be mapped to a known domain.' },
] as const

const domainDonutColors: Record<string, string> = {
  'Compute & accelerators': COLORS.accent,
  'Memory, storage & network': '#6cc6d8',
  'Services & workloads': COLORS.green,
  'Host system': COLORS.yellow,
  'Platform management': '#b18be8',
  Other: COLORS.textMuted,
}

const EVENT_KIND_OPTIONS = [
  { value: 'pressure', label: 'Pressure' },
  { value: 'control', label: 'Control action' },
  { value: 'hardware-fault', label: 'Hardware fault' },
  { value: 'lifecycle', label: 'Lifecycle' },
  { value: 'availability', label: 'Availability' },
  { value: 'configuration', label: 'Configuration' },
  { value: 'observability', label: 'Observability' },
  { value: 'status', label: 'Status' },
] as const

type EventDomain = typeof EVENT_DOMAIN_OPTIONS[number]['value']
type EventKind = typeof EVENT_KIND_OPTIONS[number]['value']
type FatalSignature = 'oom' | 'kernel-panic' | 'gpu-hang'

const FATAL_SIGNATURE_OPTIONS: { value: FatalSignature; label: string }[] = [
  { value: 'oom', label: 'OOM' },
  { value: 'kernel-panic', label: 'Kernel panic' },
  { value: 'gpu-hang', label: 'GPU hang' },
]

const ALERT_POLICY_SUMMARIES = [
  { eventType: 'PLATFORM_KERNEL_PANIC', severity: 'critical', behavior: 'Kernel panic; requires manual investigation.' },
  { eventType: 'RESOURCE_MEMORY_OOM_KILL', severity: 'critical', behavior: 'Kernel OOM kill; requires manual investigation.' },
  { eventType: 'DEVICE_GPU_HANG', severity: 'critical', behavior: 'GPU hang; requires manual investigation.' },
  { eventType: 'PLATFORM_SERVICE_CRASHED', severity: 'critical', behavior: 'Resolves when the service starts.' },
  { eventType: 'PLATFORM_SYSTEMD_RESTART_LOOP', severity: 'error', behavior: 'Resolves after 120 seconds without a new observation; escalates after 3 fires in 5 minutes.' },
  { eventType: 'CONTROL_CPU_LIMIT_FAILED', severity: 'error', behavior: 'Resolves when the CPU limit recovers.' },
  { eventType: 'CONTROL_MEMORY_LIMIT_FAILED', severity: 'error', behavior: 'Resolves when the memory limit recovers.' },
  { eventType: 'CONTROL_DISK_IO_LIMIT_FAILED', severity: 'error', behavior: 'Resolves when the disk I/O limit recovers.' },
] as const

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

function categoryLabel(cat?: string | null): string {
  if (!cat) return '-'
  return CATEGORY_LABELS[cat] || cat
}

function eventAttributeString(event: DiagEvent, name: string): string | null {
  const value = event.attributes?.[name]
  return typeof value === 'string' ? value : null
}

function eventAttributeStrings(event: DiagEvent, name: string): string[] {
  const value = event.attributes?.[name]
  return Array.isArray(value) && value.every((item) => typeof item === 'string') ? value : []
}

function eventDomain(event: DiagEvent): EventDomain {
  const configuredDomain = eventAttributeString(event, 'domain')
  if (EVENT_DOMAIN_OPTIONS.some((option) => option.value === configuredDomain)) return configuredDomain as EventDomain
  const type = event.event_type.toUpperCase()
  if (type.includes('OOM') || event.category === 'resource.memory') return 'data-path'
  if (type.includes('KERNEL_PANIC') || event.category === 'device.thermal' || event.category === 'device.power' || event.category === 'device.pcie') return 'hardware'
  if (event.category === 'resource.cpu' || event.category === 'device.gpu' || event.category === 'device.npu') return 'compute'
  if (event.category === 'resource.system') return 'hardware'
  if (event.category === 'resource.disk_io' || event.category === 'resource.network') return 'data-path'
  if (event.category === 'workload.benchmark' || event.category === 'service') return 'services'
  if (event.category.startsWith('platform.')) return 'platform'
  return 'other'
}

function eventKind(event: DiagEvent): EventKind {
  const configuredKind = eventAttributeString(event, 'event_kind')
  if (EVENT_KIND_OPTIONS.some((option) => option.value === configuredKind)) return configuredKind as EventKind
  const type = event.event_type.toUpperCase()
  if (event.category === 'resource.system' || type.includes('OOM')) return 'pressure'
  if (event.category === 'platform.control' || type.startsWith('CONTROL_')) return 'control'
  if (event.category === 'device.thermal' || event.category === 'device.power' || event.category === 'device.pcie' || type.includes('PANIC') || type.includes('HANG')) return 'hardware-fault'
  if (event.category === 'platform.availability') return 'availability'
  if (event.category === 'platform.config') return 'configuration'
  if (event.category === 'platform.observability') return 'observability'
  if (event.category === 'workload.benchmark' || event.category === 'service' || type.includes('START') || type.includes('STOP') || type.includes('RESTART') || type.includes('CRASH')) return 'lifecycle'
  return 'status'
}

function eventDomainLabel(event: DiagEvent): string {
  const domain = eventDomain(event)
  return EVENT_DOMAIN_OPTIONS.find((option) => option.value === domain)?.label || 'Other'
}

function eventKindLabel(event: DiagEvent): string {
  const kind = eventKind(event)
  return EVENT_KIND_OPTIONS.find((option) => option.value === kind)?.label || 'Status'
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

function limitedResourceLabels(snapshot: LimitSnapshotData): string[] {
  const resources: string[] = []
  if (snapshot.effective?.cpu_mem.limited || snapshot.limit_parts?.cpu_mem_limited) {
    resources.push('CPU and Memory')
  }
  if (snapshot.effective?.disk_io.limited || snapshot.limit_parts?.io_limited) {
    resources.push('Disk I/O')
  }
  return resources
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

function displayEventKey(item: DisplayEvent): string {
  const action = protectionAction(item.event)
  return action
    ? `${item.event.protection_id}:${action}:${item.event.ts_utc.slice(0, 19)}`
    : item.event.event_id
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

// The Type line shows the machine reason-code(s). A merged batch spans several
// per-resource events, so list every reason-code in it rather than only the
// representative event's -- otherwise a "cpu, memory" row opens a CPU-only Type.
function displayEventTypes(item: DisplayEvent): string {
  const action = protectionAction(item.event)
  if (!action || item.resources.length < 2) return item.event.event_type
  return item.resources.map((resource) => `CONTROL_${resource.toUpperCase()}_LIMIT_${action}`).join(', ')
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

// Render one detail row for a single resource. All per-resource events of a batch
// share the same limit_rates dict (it carries cpu_rate + mem_rate + disk_io_rate
// together), so any event of the batch can describe every resource in it.
function resourceControlRow(resource: string, rates: Record<string, unknown> | null, actionLabel: string): ResourceControlDetail | null {
  if (resource === 'cpu') {
    return { resource: 'CPU', limit: typeof rates?.cpu_rate === 'number' ? `${Math.round(rates.cpu_rate * 100)}% of baseline` : actionLabel }
  }
  if (resource === 'memory') {
    return { resource: 'Memory', limit: typeof rates?.mem_rate === 'number' ? `${Math.round(rates.mem_rate * 100)}% of baseline` : actionLabel }
  }
  if (resource === 'disk_io') {
    if (rates?.disk_io_rate && typeof rates.disk_io_rate === 'object') {
      const diskRates = rates.disk_io_rate as Record<string, unknown>
      const limits = [
        typeof diskRates.read === 'number' ? `Read ${diskRates.read} MB/s` : null,
        typeof diskRates.write === 'number' ? `Write ${diskRates.write} MB/s` : null,
        typeof diskRates.read_iops === 'number' ? `Read ${diskRates.read_iops} IOPS` : null,
        typeof diskRates.write_iops === 'number' ? `Write ${diskRates.write_iops} IOPS` : null,
      ].filter(Boolean)
      return { resource: 'Disk I/O', limit: limits.join(' · ') || actionLabel }
    }
    return { resource: 'Disk I/O', limit: actionLabel }
  }
  return null
}

// The backend emits a separate CPU / MEMORY / DISK_IO event per capped resource,
// all sharing one protection_id; the list merges the ones from a single batch into
// one row (see displayEvents). `resources` is that batch's resource set -- when the
// detail panel opens a merged row it must describe every resource in the batch, not
// just the representative event's, so the detail stays consistent with the row.
function controlDetailsFromEvent(event: DiagEvent, resources?: string[]): ResourceControlDetail[] {
  const attributes = event.attributes
  const action = protectionAction(event)
  if (!attributes || action === null) return []
  const list = (resources && resources.length ? resources : (event.resource_type ? [event.resource_type] : []))
    .map((resource) => resource.toLowerCase())
  if (!list.length) return []

  const rates = attributes.limit_rates && typeof attributes.limit_rates === 'object'
    ? attributes.limit_rates as Record<string, unknown> : null
  const actionLabel = action === 'RECOVERED' ? 'Recovered' : action === 'FAILED' ? 'Failed' : 'Applied'
  return list.map((resource) => resourceControlRow(resource, rates, actionLabel)).filter((row): row is ResourceControlDetail => row !== null)
}

// Attributes already surfaced elsewhere in the detail view (resource-control table,
// process table) or carrying only the internal control schema -- kept out of the
// generic key/value table so it shows human-facing context, not duplicated internals.
const HIDDEN_ATTR_KEYS = new Set(['limit_rates', 'limit_overrides', 'parts', 'resource_parts', 'limit_parts', 'scope_processes'])

const ATTR_LABELS: Record<string, string> = {
  app_name: 'Application',
  priority: 'Application priority',
  reason: 'Trigger',
  pressure_level: 'Pressure level',
  from_level: 'From level',
  to_level: 'To level',
  score: 'Pressure score',
  cgroups: 'Cgroups',
  pids: 'PIDs',
  boot_id: 'Boot ID',
  duration_seconds: 'Duration',
  duration_s: 'Duration',
}

const REASON_LABELS: Record<string, string> = { disk_pressure: 'Disk pressure', system_pressure: 'System pressure' }

function humanizeAttrKey(key: string): string {
  return ATTR_LABELS[key] || key.replace(/_/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase())
}

function formatAttrValue(key: string, value: unknown): string {
  if (value == null) return '-'
  if (key === 'reason' && typeof value === 'string') return REASON_LABELS[value] || value
  if ((key === 'duration_seconds' || key === 'duration_s') && typeof value === 'number') return formatDuration(value) || `${value}s`
  if (key === 'pids' && typeof value === 'string') return value.replace(/[{}]/g, '').trim() || '-'
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) {
    return value
      .map((item) => (item && typeof item === 'object')
        ? Object.entries(item as Record<string, unknown>).map(([k, v]) => `${k}: ${v}`).join(', ')
        : String(item))
      .join(', ') || '-'
  }
  if (typeof value === 'object') {
    // Render one level of nesting so a nested dict shows its fields instead of
    // "[object Object]" (e.g. {cpu: {enabled, rate}} -> "Cpu: enabled: true, rate: 0.5").
    return Object.entries(value as Record<string, unknown>).map(([k, v]) => {
      const inner = (v && typeof v === 'object')
        ? Object.entries(v as Record<string, unknown>).map(([ik, iv]) => `${ik}: ${iv}`).join(', ')
        : String(v)
      return `${humanizeAttrKey(k)}: ${inner}`
    }).join(' · ') || '-'
  }
  return String(value)
}

interface AttrRow { key: string; label: string; value: string }

function attributeRows(event: DiagEvent): AttrRow[] {
  const attributes = event.attributes
  if (!attributes) return []
  const rows: AttrRow[] = []
  for (const [key, value] of Object.entries(attributes)) {
    if (HIDDEN_ATTR_KEYS.has(key)) continue
    if (value == null || value === '') continue
    const formatted = formatAttrValue(key, value)
    if (!formatted || formatted === '-') continue
    rows.push({ key, label: humanizeAttrKey(key), value: formatted })
  }
  return rows
}

interface ScopeProcessRow extends ScopeProcess { scope: string }

function scopeProcessRows(event: DiagEvent): ScopeProcessRow[] {
  const scopeProcesses = event.attributes?.scope_processes
  if (!scopeProcesses || typeof scopeProcesses !== 'object') return []
  const rows: ScopeProcessRow[] = []
  const push = (scope: string, entries: unknown) => {
    if (!Array.isArray(entries)) return
    for (const process of entries) {
      if (!process || typeof process !== 'object') continue
      const pid = (process as { pid?: unknown }).pid
      const processName = (process as { process_name?: unknown; name?: unknown }).process_name
        ?? (process as { name?: unknown }).name
      const cmdline = (process as { cmdline?: unknown }).cmdline
      if (typeof pid === 'number' && typeof processName === 'string') {
        rows.push({ scope, pid, processName, cmdline: typeof cmdline === 'string' ? cmdline : '' })
      }
    }
  }
  if (Array.isArray(scopeProcesses)) push('', scopeProcesses)
  else for (const [scope, entries] of Object.entries(scopeProcesses)) push(scope, entries)
  return rows
}

function normalizedSeverity(level: string): keyof typeof severityColors {
  const value = (level || 'info').toLowerCase()
  if (value.includes('critical') || value.includes('fatal')) return 'critical'
  if (value.includes('error')) return 'error'
  if (value.includes('warn')) return 'warning'
  if (value.includes('debug')) return 'debug'
  return 'info'
}

const EVENT_TIMELINE_LABEL_WIDTH = 176
const EVENT_TIMELINE_LEFT_GUTTER = 12
const EVENT_TIMELINE_PLOT_LEFT = EVENT_TIMELINE_LABEL_WIDTH + EVENT_TIMELINE_LEFT_GUTTER

type EventLane = string

interface EventTimelinePoint {
  x: number
  y: number
  lane: EventLane
  item: DisplayEvent
  items: DisplayEvent[]
  from: number
  to: number
}

interface EventBrushRange {
  startIndex: number
  endIndex: number
}

interface EventZoomSelection {
  from: number
  to: number
}

function eventLane(event: DiagEvent): EventLane {
  return eventDomainLabel(event)
}

function severityRank(level: string): number {
  const severity = normalizedSeverity(level)
  if (severity === 'critical') return 4
  if (severity === 'error') return 3
  if (severity === 'warning') return 2
  if (severity === 'info') return 1
  return 0
}

function isOomEvent(event: DiagEvent): boolean {
  const text = `${event.event_type || ''} ${event.summary || ''}`.toLowerCase()
  return /\boom\b/.test(text) || /out[-\s]?of[-\s]?memory/.test(text)
}

function isPanicEvent(event: DiagEvent): boolean {
  const text = `${event.event_type || ''} ${event.summary || ''}`.toLowerCase()
  return /kernel\s+panic/.test(text) || /\bpanic\b/.test(text)
}

function isGpuHangEvent(event: DiagEvent): boolean {
  const text = `${event.event_type || ''} ${event.summary || ''}`.toLowerCase()
  return /gpu\s+hang/.test(text) || /device_gpu_hang/.test(text)
}

function hasFatalSignature(event: DiagEvent, signature: FatalSignature): boolean {
  if (eventAttributeStrings(event, 'fatal_signatures').includes(signature)) return true
  if (signature === 'oom') return isOomEvent(event)
  if (signature === 'kernel-panic') return isPanicEvent(event)
  return isGpuHangEvent(event)
}

function formatDiagnosticValue(value: unknown): string {
  if (value === null || value === undefined) return 'None'
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value)
}

function configChangeEntries(changeSummary: unknown): Array<{ scope: string; field: string; change: string; previous: unknown; current: unknown }> {
  if (!changeSummary || typeof changeSummary !== 'object') return []
  const summary = changeSummary as Record<string, unknown>
  return ['hw', 'sw'].flatMap((scope) => {
    const changes = summary[scope]
    if (!Array.isArray(changes)) return []
    return changes.flatMap((change) => {
      if (!change || typeof change !== 'object') return []
      const entry = change as Record<string, unknown>
      return [{
        scope: scope === 'hw' ? 'Hardware' : 'Software',
        field: typeof entry.field === 'string' ? entry.field : 'Unknown field',
        change: typeof entry.change === 'string' ? entry.change : 'changed',
        previous: entry.previous,
        current: entry.current,
      }]
    })
  })
}

function eventTimelinePoints(items: DisplayEvent[], rangeFrom: number, rangeTo: number): {
  lanes: EventLane[]
  laneCounts: Map<EventLane, number>
  points: EventTimelinePoint[]
} {
  const grouped = new Map<EventLane, DisplayEvent[]>()
  for (const item of items) {
    const event = item.event
    const lane = eventLane(event)
    const entries = grouped.get(lane) || []
    entries.push(item)
    grouped.set(lane, entries)
  }
  const lanes = Array.from(grouped.keys()).sort((left, right) => {
    const leftIndex = EVENT_DOMAIN_OPTIONS.findIndex((option) => option.label === left)
    const rightIndex = EVENT_DOMAIN_OPTIONS.findIndex((option) => option.label === right)
    return (leftIndex === -1 ? EVENT_DOMAIN_OPTIONS.length : leftIndex)
      - (rightIndex === -1 ? EVENT_DOMAIN_OPTIONS.length : rightIndex)
      || left.localeCompare(right)
  })
  const bucketSeconds = Math.max(1, Math.ceil((rangeTo - rangeFrom) / 120))
  return {
    lanes,
    laneCounts: new Map(lanes.map((lane) => [lane, grouped.get(lane)?.length || 0])),
    points: lanes.flatMap((lane, y) => {
      const laneItems = (grouped.get(lane) || [])
        .map((item) => ({ item, x: dayjs(item.event.ts_utc).unix() }))
        .filter(({ x }) => Number.isFinite(x))
        .sort((left, right) => left.x - right.x)
      const buckets = new Map<number, Array<{ item: DisplayEvent; x: number }>>()
      for (const entry of laneItems) {
        const bucket = Math.floor((entry.x - rangeFrom) / bucketSeconds)
        const cluster = buckets.get(bucket) || []
        cluster.push(entry)
        buckets.set(bucket, cluster)
      }
      return Array.from(buckets.values()).map((cluster) => ({
        x: cluster.reduce((sum, entry) => sum + entry.x, 0) / cluster.length,
        y,
        lane,
        item: cluster.reduce((highest, entry) =>
          severityRank(entry.item.event.severity) > severityRank(highest.event.severity) ? entry.item : highest,
        cluster[0].item),
        items: cluster.map((entry) => entry.item),
        from: cluster[0].x,
        to: cluster[cluster.length - 1].x,
      }))
    }),
  }
}

function eventTimelineTooltip({ active, payload }: {
  active?: boolean
  payload?: Array<{ payload?: unknown }>
}) {
  const point = payload?.map((entry) => entry.payload).find((entry): entry is EventTimelinePoint => {
    const candidate = entry as Partial<EventTimelinePoint> | undefined
    return typeof candidate?.x === 'number' && !!candidate.item && Array.isArray(candidate.items)
  })
  if (!active || !point || typeof point.x !== 'number' || !point.item) return null
  return (
    <div className='diagnostics-event-tooltip'>
      <Text strong>
        {dayjs.unix(point.from).format('MM-DD HH:mm:ss')}
        {point.to !== point.from ? ` - ${dayjs.unix(point.to).format('MM-DD HH:mm:ss')}` : ''}
      </Text>
      <Text type='secondary'>{point.lane}</Text>
      {point.items.length > 1 ? <Text type='secondary'>{point.items.length} events in this burst</Text> : null}
      {point.items.slice(0, 6).map((item) => (
        <div className='diagnostics-event-tooltip-item' key={item.event.event_id}>
          {severityTag(item.event.severity)}
          <Text className='diagnostics-event-tooltip-summary'>{displayEventSummary(item)}</Text>
        </div>
      ))}
      {point.items.length > 6 ? <Text type='secondary'>Select to inspect all {point.items.length} events</Text> : null}
    </div>
  )
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
function monitorPressurePoints(samples: DiagContextData['metrics']['monitor']['series']): PressurePoint[] {
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

function resourceUtilizationColor(label: string): string {
  if (label === 'CPU') return COLORS.accent
  if (label === 'Memory') return COLORS.green
  if (label === 'NPU') return COLORS.orange
  if (label === 'Disk') return COLORS.yellow
  if (label === 'Network') return '#6cc6d8'
  return '#b18be8'
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

export default function Diagnostics({ active, openAlertsSignal, onOpenHistory }: Props) {
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
  const [bootScopeEnabled] = useState(true)
  const [boots, setBoots] = useState<DiagBoot[]>([])
  const [selectedBootId, setSelectedBootId] = useState<string | null>(null)
  const [bootCustomRange, setBootCustomRange] = useState<{ from: number; to: number } | null>(null)
  const [bootsLoading, setBootsLoading] = useState(false)
  const [refreshInterval, setRefreshInterval] = useState<RefreshInterval>('off')
  const [eventKeyword, setEventKeyword] = useState('')
  const [eventSeverity, setEventSeverity] = useState<string | undefined>(undefined)
  const [eventDomainFilter, setEventDomainFilter] = useState<EventDomain | undefined>(undefined)
  const [eventKindFilter, setEventKindFilter] = useState<EventKind | undefined>(undefined)
  const [eventFatalSignature, setEventFatalSignature] = useState<FatalSignature | undefined>(undefined)
  const [eventLaneFilter, setEventLaneFilter] = useState<EventLane | undefined>(undefined)
  const [eventSource, setEventSource] = useState<string | undefined>(undefined)
  const [eventPage, setEventPage] = useState(1)
  const [timelineEventKeys, setTimelineEventKeys] = useState<string[] | null>(null)
  const [eventBrushRange, setEventBrushRange] = useState<EventBrushRange | null>(null)
  const [eventZoomRange, setEventZoomRange] = useState<EventZoomSelection | null>(null)
  const [eventZoomSelection, setEventZoomSelection] = useState<EventZoomSelection | null>(null)
  const [suppressEventTooltip, setSuppressEventTooltip] = useState(false)
  const [showAllActiveControls, setShowAllActiveControls] = useState(false)
  const [resumingAppId, setResumingAppId] = useState<string | null>(null)

  // Object filters — never typed by hand; set only by clicking a related-object
  // chip, surfaced as removable tags.
  const [jobId, setJobId] = useState<string | undefined>(undefined)
  const [appId, setAppId] = useState<string | undefined>(undefined)

  // Log-search panel (secondary)
  const [logsOpen, setLogsOpen] = useState(false)
  const [eventRecordsOpen, setEventRecordsOpen] = useState(true)
  const [logSource, setLogSource] = useState<string | undefined>(undefined)
  const [logKeyword, setLogKeyword] = useState('')
  const [logLevel, setLogLevel] = useState<string | undefined>(undefined)
  const [groupRepeats, setGroupRepeats] = useState(true)

  const [selectedEvent, setSelectedEvent] = useState<DisplayEvent | null>(null)

  // Investigation context drawer (the evidence chain for one job/app,
  // assembled read-only by GET /diag/context).
  const [contextTarget, setContextTarget] = useState<ContextTarget | null>(null)
  const [contextData, setContextData] = useState<DiagContextData | null>(null)
  const [contextLoading, setContextLoading] = useState(false)
  const [contextError, setContextError] = useState<string | null>(null)
  const [rangeFindings, setRangeFindings] = useState<DiagFinding[]>([])
  const [rangeResourceUtilization, setRangeResourceUtilization] = useState<DiagResourceUtilization[]>([])
  const [rangeResourceTrend, setRangeResourceTrend] = useState<DiagResourceTrendPoint[]>([])
  const [rangeMonitorSampleCount, setRangeMonitorSampleCount] = useState(0)
  const [rangeMonitorLoading, setRangeMonitorLoading] = useState(false)
  const [resourceLanesOpen, setResourceLanesOpen] = useState(true)
  const [activeResourceTrendLabels, setActiveResourceTrendLabels] = useState<Set<string>>(
    () => new Set(['CPU', 'Memory', 'iGPU', 'dGPU', 'GPU0']),
  )
  const [reportResourceTrendLabels, setReportResourceTrendLabels] = useState<Set<string>>(
    () => new Set(),
  )
  const [alerts, setAlerts] = useState<DiagAlert[]>([])
  const [alertsLoading, setAlertsLoading] = useState(false)
  const [insightsOpen, setInsightsOpen] = useState(false)
  const [reportOpen, setReportOpen] = useState(false)
  const [report, setReport] = useState<DiagDigest | null>(null)
  const [reportEvents, setReportEvents] = useState<DiagEvent[]>([])
  const [reportFindings, setReportFindings] = useState<DiagFinding[]>([])
  const [reportResourceUtilization, setReportResourceUtilization] = useState<DiagResourceUtilization[]>([])
  const [reportResourceTrend, setReportResourceTrend] = useState<DiagResourceTrendPoint[]>([])
  const [reportResourceSampleCount, setReportResourceSampleCount] = useState(0)
  const [reportLoading, setReportLoading] = useState(false)
  const [reportError, setReportError] = useState<string | null>(null)
  const [reportRange, setReportRange] = useState<{ from: number; to: number } | null>(null)
  const [alertsOpen, setAlertsOpen] = useState(false)
  const [alertFilter, setAlertFilter] = useState<'range' | 'all' | 'needs_attention' | 'acknowledged' | 'silenced' | 'resolved'>('all')
  const [alertActionKey, setAlertActionKey] = useState<string | null>(null)
  const wasActive = useRef(false)
  const activeControlsRef = useRef<HTMLDivElement>(null)
  const eventsTableRef = useRef<HTMLDivElement>(null)
  const eventZoomDraggedRef = useRef(false)
  // Independent monotonic tokens prevent older event or lifecycle requests from
  // landing after a newer refresh without making the fast event list wait for
  // slower lifecycle reconstruction and cgroup verification.
  const eventsRefreshSeq = useRef(0)
  const controlLifecyclesRefreshSeq = useRef(0)
  // Logs fetch on Search/refresh, not per keystroke: the keyword lives in a ref so
  // typing does not change loadLogs' identity (and so does not trigger a refetch).
  const logSeq = useRef(0)
  const reportSeq = useRef(0)
  const logKeywordRef = useRef(logKeyword)

  const resetEventViewForTimeRange = () => {
    setEventKeyword('')
    setEventSeverity(undefined)
    setEventDomainFilter(undefined)
    setEventKindFilter(undefined)
    setEventFatalSignature(undefined)
    setEventLaneFilter(undefined)
    setEventSource(undefined)
    setTimelineEventKeys(null)
    setEventBrushRange(null)
    setEventZoomRange(null)
    setEventZoomSelection(null)
    setEventPage(1)
  }

  const selectedBoot = useMemo(
    () => boots.find((b) => b.boot_id === selectedBootId) ?? null,
    [boots, selectedBootId],
  )

  const timeRange = useMemo(() => {
    const now = rangeEndEpoch + (clockSkewSec ?? 0)
    // `to` is the semantic end shown to the user; `queryTo` is what the fetches use.
    // They differ only for live windows, whose upper bound tracks "now" and so needs
    // the lead (see LIVE_WINDOW_LEAD_SECONDS); fixed/historical ends query as-is.
    const live = (from: number, to: number) => ({ from, to, queryTo: to + LIVE_WINDOW_LEAD_SECONDS })
    const fixed = (from: number, to: number) => ({ from, to, queryTo: to })
    if (bootScopeEnabled && selectedBoot?.first_ts) {
      const bootFrom = selectedBoot.first_ts
      const running = selectedBoot.running
      const bootTo = running ? now : selectedBoot.last_ts ?? now
      const bound = running ? live : fixed
      if (windowKey === 'entire') return bound(bootFrom, bootTo)
      if (windowKey === 'custom' && bootCustomRange) {
        const from = Math.max(bootFrom, Math.min(bootCustomRange.from, bootTo))
        const to = Math.max(from, Math.min(bootCustomRange.to, bootTo))
        return fixed(from, to)
      }
      const span = windowKey === 'custom' ? WINDOW_SECONDS['1h'] : WINDOW_SECONDS[windowKey]
      return bound(Math.max(bootFrom, bootTo - span), bootTo)
    }
    if (windowKey === 'custom' && customRange) return fixed(customRange.from, customRange.to)
    const span = windowKey === 'custom' || windowKey === 'entire' ? WINDOW_SECONDS['1h'] : WINDOW_SECONDS[windowKey]
    return live(now - span, now)
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

  const loadEvents = useCallback(async () => {
    const token = ++eventsRefreshSeq.current
    setEventsLoading(true)
    setEventsError(null)
    try {
      const result = await api.getDiagEvents({
        job_id: jobId,
        app_id: appId,
        from: timeRange.from,
        to: timeRange.queryTo,
        limit: 100,
      })
      if (token !== eventsRefreshSeq.current) return
      setEvents(result.events || [])
    } catch (error) {
      if (token !== eventsRefreshSeq.current) return
      setEvents([])
      setEventsError(error instanceof Error ? error.message : 'Failed to query diagnostic events')
    } finally {
      if (token === eventsRefreshSeq.current) setEventsLoading(false)
      updateClockSkew()
    }
  }, [jobId, appId, timeRange, updateClockSkew])

  const loadControlLifecycles = useCallback(async () => {
    const token = ++controlLifecyclesRefreshSeq.current
    setControlLifecyclesLoading(true)
    setControlLifecyclesError(null)
    try {
      const result = await api.getDiagControlLifecycles({ limit: 100 })
      if (token !== controlLifecyclesRefreshSeq.current) return
      const lifecycles = result.lifecycles || []
      const verified = await Promise.all(lifecycles.map(async (lifecycle) => {
        if (lifecycle.status !== 'requires_verification' || !lifecycle.cgroups?.length) return lifecycle
        try {
          const result = await api.getInterruptedLimitStatus({ cgroups: lifecycle.cgroups })
          if (!result.available) return lifecycle
          if (result.resources.length === 0) return { ...lifecycle, status: 'recovered', active_resources: [] }
          return { ...lifecycle, status: 'active', active_resources: result.resources }
        } catch {
          return lifecycle
        }
      }))
      if (token !== controlLifecyclesRefreshSeq.current) return
      setControlLifecycles(verified)
    } catch (error) {
      if (token !== controlLifecyclesRefreshSeq.current) return
      setControlLifecycles([])
      setControlLifecyclesError(error instanceof Error ? error.message : 'Failed to query protection status')
    } finally {
      if (token === controlLifecyclesRefreshSeq.current) setControlLifecyclesLoading(false)
    }
  }, [])

  const loadEventsAndLifecycles = useCallback(
    () => Promise.all([loadEvents(), loadControlLifecycles()]),
    [loadEvents, loadControlLifecycles],
  )

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
        to: timeRange.queryTo,
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


  // An investigation focuses on the immediate evidence surrounding its anchor;
  // a selected boot can span months and would hide the causal signal.
  const openContext = useCallback(async (target: ContextTarget, anchorEvent?: DiagEvent) => {
    setContextTarget(target)
    setContextData(null)
    setContextError(null)
    setContextLoading(true)
    try {
      const anchorTime = anchorEvent ? dayjs(anchorEvent.ts_utc).unix() : timeRange.to
      const params: DiagContextQuery = {
        from: anchorTime - INVESTIGATION_EVENT_WINDOW_SECONDS,
        to: anchorTime + INVESTIGATION_EVENT_WINDOW_SECONDS,
      }
      if (target.kind === 'job') params.job_id = target.value
      else params.app_id = target.value
      setContextData(await api.getDiagContext(params))
    } catch (err) {
      setContextData(null)
      setContextError(err instanceof Error ? err.message : 'Failed to assemble investigation context')
    } finally {
      setContextLoading(false)
    }
  }, [timeRange.to])

  // Land the drawer's scope onto the main view: set the matching object filter,
  // reveal the events + logs that back it, then close the drawer.
  const applyContextToMainView = useCallback((target: ContextTarget) => {
    if (target.kind === 'job') setJobId(target.value)
    else setAppId(target.value)
    setLogsOpen(true)
    setContextTarget(null)
  }, [])

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
        appId: lifecycle.app_id,
        protectionId: lifecycle.protection_id,
        hasCgroups: Boolean(lifecycle.cgroups?.length),
        appName,
        resource,
        // An orphaned requires_verification lifecycle (no cgroups left to check)
        // can never resolve itself via verification, but resumeControl's
        // no-live-limit branch already knows how to clear it -- so let it act
        // as "resumable" too, or the row is stuck forever with no way to dismiss it.
        isActive: lifecycle.status === 'active' || (lifecycle.status === 'requires_verification' && !lifecycle.cgroups?.length),
        status,
        lastUpdatedAt: lifecycle.last_updated_at,
      }))
    }),
    [currentControlLifecycles],
  )

  const resumeControl = useCallback(async (appId: string, protectionId: string, hasCgroups: boolean) => {
    setResumingAppId(appId)
    try {
      const snapshot = await api.getLimitSnapshot({ app_id: appId })
      if (!snapshot.limited || !snapshot.source) {
        if (!hasCgroups) {
          await api.clearControlLifecycleWithoutRuntimeState({ protection_id: protectionId })
          message.info('Historical control record cleared; no live limit was found')
        } else {
          message.warning('This control is no longer active')
        }
        // Advance the live window to now so the just-emitted RECOVERED/cleared
        // event falls inside it -- the events list is time-bounded (unlike the
        // lifecycle overview), so without this the record would land past `to`
        // and the two views disagree. In custom/historical mode this is a no-op
        // and the awaited reload still refreshes the overview.
        setRangeEndEpoch(Math.floor(Date.now() / 1000))
        await loadEventsAndLifecycles()
        setResumingAppId(null)
        return
      }
      const resources = limitedResourceLabels(snapshot)
      Modal.confirm({
        title: 'Resume resources?',
        content: `This will restore ${resources.join(' and ') || 'the active resource limits'} for ${appId}.`,
        okText: 'Resume',
        cancelText: 'Cancel',
        okButtonProps: { danger: true },
        onCancel: () => setResumingAppId(null),
        onOk: async () => {
          try {
            if (snapshot.source === 'auto') await api.autoLimitRestore({ app_id: appId })
            else await api.resourceRestore({ app_id: appId })
            message.success(`Resources resumed for ${appId}`)
            // Advance the live window so the RECOVERED event just emitted by the
            // restore is inside it; otherwise the overview updates but the event
            // records (time-bounded) miss the new row. See the clear path above.
            setRangeEndEpoch(Math.floor(Date.now() / 1000))
            await loadEventsAndLifecycles()
          } catch (error) {
            // Surface the failure explicitly -- Modal.confirm swallows a rejected
            // onOk into a closed dialog otherwise, leaving the user thinking the
            // resume succeeded when the restore call actually failed.
            message.error(error instanceof Error ? error.message : 'Failed to resume resources')
            throw error
          } finally {
            setResumingAppId(null)
          }
        },
      })
    } catch (error) {
      message.error(error instanceof Error ? error.message : 'Failed to resume resources')
      setResumingAppId(null)
    }
  }, [loadEventsAndLifecycles])

  const visibleActiveControlRows = useMemo(
    () => showAllActiveControls ? activeControlRows : activeControlRows.slice(0, 3),
    [activeControlRows, showAllActiveControls],
  )

  const eventDomainOptions = useMemo(() => {
    const present = new Set(events.map(eventDomain))
    return [{ label: 'All domains', value: ALL }, ...EVENT_DOMAIN_OPTIONS.filter((option) => present.has(option.value))]
  }, [events])

  const eventKindOptions = useMemo(() => {
    const present = new Set(events.map(eventKind))
    return [{ label: 'All event kinds', value: ALL }, ...EVENT_KIND_OPTIONS.filter((option) => present.has(option.value))]
  }, [events])

  // Collapsed to the trust-oriented origin buckets, again only those present.
  const eventSourceOptions = useMemo(() => {
    const present = new Set(events.map((event) => sourceBucket(event.source).key))
    const buckets = [...SOURCE_BUCKETS, OTHER_BUCKET]
      .filter((bucket) => present.has(bucket.key))
      .map((bucket) => ({ label: bucket.label, value: bucket.key }))
    return [{ label: 'All sources', value: ALL }, ...buckets]
  }, [events])

  const filteredEvents = useMemo(() => {
    const keyword = eventKeyword.trim().toLowerCase()
    return events.filter((event) => {
      const severity = normalizedSeverity(event.severity)
      if (eventSeverity === 'important' && !['warning', 'error', 'critical'].includes(severity)) return false
      if (eventSeverity && eventSeverity !== 'important' && severity !== eventSeverity) return false
      if (eventDomainFilter && eventDomain(event) !== eventDomainFilter) return false
      if (eventKindFilter && eventKind(event) !== eventKindFilter) return false
      if (eventFatalSignature && !hasFatalSignature(event, eventFatalSignature)) return false
      if (eventLaneFilter && eventLane(event) !== eventLaneFilter) return false
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
  }, [eventDomainFilter, eventFatalSignature, eventKeyword, eventKindFilter, eventLaneFilter, eventSeverity, eventSource, events])

  const timelineDisplayedEvents = useMemo(() => displayEvents(filteredEvents), [filteredEvents])

  const displayedEvents = useMemo(() => {
    if (!timelineEventKeys) return timelineDisplayedEvents
    const selected = new Set(timelineEventKeys)
    return timelineDisplayedEvents.filter((item) => selected.has(displayEventKey(item)))
      .sort((left, right) => left.event.ts_utc.localeCompare(right.event.ts_utc))
  }, [timelineDisplayedEvents, timelineEventKeys])

  const allDisplayedEvents = useMemo(() => displayEvents(events), [events])

  const eventBrushPoints = useMemo(() => {
    const steps = 120
    const duration = timeRange.to - timeRange.from
    return Array.from({ length: steps + 1 }, (_, index) => ({
      x: timeRange.from + duration * index / steps,
    }))
  }, [timeRange.from, timeRange.to])

  useEffect(() => {
    setEventBrushRange(null)
    setEventZoomRange(null)
    setEventZoomSelection(null)
  }, [eventBrushPoints])

  const eventViewRange = useMemo(() => {
    if (eventZoomRange) return eventZoomRange
    if (!eventBrushRange || eventBrushPoints.length === 0) return { from: timeRange.from, to: timeRange.to }
    const clamp = (index: number) => Math.max(0, Math.min(index, eventBrushPoints.length - 1))
    const from = eventBrushPoints[clamp(eventBrushRange.startIndex)]?.x
    const to = eventBrushPoints[clamp(eventBrushRange.endIndex)]?.x
    if (from === undefined || to === undefined || to <= from) return { from: timeRange.from, to: timeRange.to }
    return { from, to }
  }, [eventBrushPoints, eventBrushRange, eventZoomRange, timeRange.from, timeRange.to])

  const resourceTrendLabels = useMemo(() => {
    const available = new Set<string>()
    for (const point of rangeResourceTrend) {
      Object.keys(point.values).forEach((label) => available.add(label))
    }
    return ['CPU', 'Memory', ...Array.from(available).filter((label) => label === 'iGPU' || label === 'dGPU' || label.startsWith('GPU')), 'NPU', 'Disk', 'Network']
      .filter((label, index, labels) => available.has(label) && labels.indexOf(label) === index)
  }, [rangeResourceTrend])

  const activeResourceTrendSeries = useMemo(
    () => resourceTrendLabels.filter((label) => activeResourceTrendLabels.has(label)),
    [activeResourceTrendLabels, resourceTrendLabels],
  )

  const visibleResourceTrend = useMemo(() => rangeResourceTrend.filter((point) => (
    point.ts_epoch >= eventViewRange.from && point.ts_epoch <= eventViewRange.to
  )), [eventViewRange.from, eventViewRange.to, rangeResourceTrend])

  const overviewTimelineEvents = useMemo(
    () => allDisplayedEvents.filter((item) => {
      const timestamp = dayjs(item.event.ts_utc).unix()
      return timestamp >= eventViewRange.from && timestamp <= eventViewRange.to
    }),
    [allDisplayedEvents, eventViewRange.from, eventViewRange.to],
  )

  const visibleTimelineEvents = useMemo(
    () => timelineDisplayedEvents.filter((item) => {
      const timestamp = dayjs(item.event.ts_utc).unix()
      return timestamp >= eventViewRange.from && timestamp <= eventViewRange.to
    }),
    [eventViewRange.from, eventViewRange.to, timelineDisplayedEvents],
  )

  const eventTimeline = useMemo(
    () => eventTimelinePoints(visibleTimelineEvents, eventViewRange.from, eventViewRange.to),
    [eventViewRange.from, eventViewRange.to, visibleTimelineEvents],
  )

  const handleEventBrushChange = useCallback((range: { startIndex?: number; endIndex?: number }) => {
    if (range.startIndex === undefined || range.endIndex === undefined) return
    setEventZoomRange(null)
    setEventBrushRange({ startIndex: range.startIndex, endIndex: range.endIndex })
  }, [])

  const beginEventZoom = useCallback((state: { xValue?: unknown }) => {
    if (typeof state.xValue !== 'number' || !Number.isFinite(state.xValue)) return
    eventZoomDraggedRef.current = false
    setSuppressEventTooltip(true)
    setEventZoomSelection({ from: state.xValue, to: state.xValue })
  }, [])

  const updateEventZoom = useCallback((state: { xValue?: unknown }) => {
    if (typeof state.xValue !== 'number' || !Number.isFinite(state.xValue)) return
    setEventZoomSelection((selection) => selection ? { ...selection, to: state.xValue as number } : null)
  }, [])

  const finishEventZoom = useCallback(() => {
    if (!eventZoomSelection) return
    const from = Math.min(eventZoomSelection.from, eventZoomSelection.to)
    const to = Math.max(eventZoomSelection.from, eventZoomSelection.to)
    setEventZoomSelection(null)
    if (to - from < Math.max(1, (eventViewRange.to - eventViewRange.from) / 500)) {
      setSuppressEventTooltip(false)
      return
    }
    eventZoomDraggedRef.current = true
    setEventZoomRange({ from, to })
    const duration = timeRange.to - timeRange.from
    const lastIndex = eventBrushPoints.length - 1
    const toIndex = (timestamp: number) => Math.max(0, Math.min(lastIndex,
      Math.round((timestamp - timeRange.from) / duration * lastIndex)))
    setEventBrushRange({ startIndex: toIndex(from), endIndex: toIndex(to) })
    window.setTimeout(() => { eventZoomDraggedRef.current = false }, 0)
  }, [eventBrushPoints.length, eventViewRange.from, eventViewRange.to, eventZoomSelection, timeRange.from, timeRange.to])

  const resetEventZoom = useCallback(() => {
    setEventZoomSelection(null)
    setEventZoomRange(null)
    setEventBrushRange(null)
  }, [])

  const selectTimelineCluster = useCallback((point: EventTimelinePoint) => {
    setTimelineEventKeys(point.items.map(displayEventKey))
    setEventPage(1)
    window.setTimeout(() => eventsTableRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0)
  }, [])

  const eventActivityTick = useCallback((ts: number) => {
    const duration = eventViewRange.to - eventViewRange.from
    return dayjs.unix(ts).format(duration <= 24 * 60 * 60 ? 'HH:mm' : 'MM-DD HH:mm')
  }, [eventViewRange.from, eventViewRange.to])

  const eventOverview = useMemo(() => {
    const severityCounts: Record<keyof typeof severityColors, number> = {
      debug: 0,
      info: 0,
      warning: 0,
      error: 0,
      critical: 0,
    }
    let oom = 0
    let panic = 0
    let gpuHang = 0
    const laneCounts = new Map<string, number>()
    const laneSeverityCounts = new Map<string, Record<'info' | 'warning' | 'error' | 'critical', number>>()
    for (const item of overviewTimelineEvents) {
      const severity = normalizedSeverity(item.event.severity)
      severityCounts[severity] += 1
      if (isOomEvent(item.event)) oom += 1
      if (isPanicEvent(item.event)) panic += 1
      if (isGpuHangEvent(item.event)) gpuHang += 1
      const lane = eventLane(item.event)
      laneCounts.set(lane, (laneCounts.get(lane) || 0) + 1)
      const counts = laneSeverityCounts.get(lane) || { info: 0, warning: 0, error: 0, critical: 0 }
      if (severity !== 'debug') counts[severity as keyof typeof counts] += 1
      laneSeverityCounts.set(lane, counts)
    }
    const total = overviewTimelineEvents.length
    const categories = Array.from(laneCounts, ([lane, count]) => ({ lane, count, severityCounts: laneSeverityCounts.get(lane) }))
      .sort((left, right) => right.count - left.count || left.lane.localeCompare(right.lane))
    return { total, categories, severityCounts, incidents: { oom, panic, gpuHang } }
  }, [overviewTimelineEvents])

  const overviewDomains = useMemo(() => {
    const domainEvents = eventSeverity && eventSeverity !== 'important'
      ? overviewTimelineEvents.filter((item) => normalizedSeverity(item.event.severity) === eventSeverity)
      : overviewTimelineEvents
    const counts = new Map<string, { count: number; severityCounts: Record<'info' | 'warning' | 'error' | 'critical', number> }>()
    for (const item of domainEvents) {
      const lane = eventLane(item.event)
      const current = counts.get(lane) || { count: 0, severityCounts: { info: 0, warning: 0, error: 0, critical: 0 } }
      current.count += 1
      const severity = normalizedSeverity(item.event.severity)
      if (severity !== 'debug') current.severityCounts[severity as keyof typeof current.severityCounts] += 1
      counts.set(lane, current)
    }
    return EVENT_DOMAIN_CATALOG.map((domain) => ({
      ...domain,
      count: counts.get(domain.label)?.count || 0,
      severityCounts: counts.get(domain.label)?.severityCounts || { info: 0, warning: 0, error: 0, critical: 0 },
    })).sort((left, right) => right.count - left.count || left.label.localeCompare(right.label))
  }, [eventSeverity, overviewTimelineEvents])

  const domainEventTotal = useMemo(
    () => overviewDomains.reduce((total, domain) => total + domain.count, 0),
    [overviewDomains],
  )

  const domainBarData = useMemo(() => (
    overviewDomains
      .map((domain) => ({ name: domain.label, value: domain.count, color: domainDonutColors[domain.label] }))
  ), [overviewDomains])

  const severityDonutData = useMemo(() => {
    return [
      { name: 'Info', value: eventOverview.severityCounts.info, color: COLORS.accent },
      { name: 'Warning', value: eventOverview.severityCounts.warning, color: COLORS.yellow },
      { name: 'Error', value: eventOverview.severityCounts.error, color: DIAGNOSTIC_ERROR_COLOR },
      { name: 'Critical', value: eventOverview.severityCounts.critical, color: COLORS.red },
    ].filter((item) => item.value > 0)
  }, [eventOverview.severityCounts.critical, eventOverview.severityCounts.error, eventOverview.severityCounts.info, eventOverview.severityCounts.warning])

  const selectEventDomain = (domainLabel: string) => {
    setEventLaneFilter(eventLaneFilter === domainLabel ? undefined : domainLabel)
    setTimelineEventKeys(null)
    setEventPage(1)
  }

  const latestBenchmarkEvent = useMemo(
    () => events.find((event) => event.category === 'workload.benchmark') ?? null,
    [events],
  )

  // Memoized so the render functions are not rebuilt on every parent re-render
  // (refresh tick, filter keystroke); only ``openContext`` changing rebuilds them.
  const eventColumns = useMemo(() => [
    { title: 'Time', width: 170, render: (_: unknown, item: DisplayEvent) => dayjs(item.event.ts_utc).format('MM-DD HH:mm:ss') },
    { title: 'Severity', width: 120, render: (_: unknown, item: DisplayEvent) => severityTag(item.event.severity) },
    { title: 'Domain', width: 190, render: (_: unknown, item: DisplayEvent) => eventDomainLabel(item.event) },
    { title: 'Event kind', width: 150, render: (_: unknown, item: DisplayEvent) => eventKindLabel(item.event) },
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
            <Button size='small' onClick={() => setSelectedEvent(item)}>
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
  const averageResourceUtilization = useMemo(() => rangeResourceUtilization.map((resource) => ({
    ...resource,
    color: resourceUtilizationColor(resource.label),
  })), [rangeResourceUtilization])
  const contextAlerts = contextData?.alerts ?? []
  const rangeAlerts = useMemo(() => alerts.filter((alert) => {
    const timestamp = dayjs(alert.last_fired_at).unix()
    return Number.isFinite(timestamp) && timestamp >= timeRange.from && timestamp <= timeRange.to
  }), [alerts, timeRange.from, timeRange.to])
  const currentAlerts = useMemo(
    () => alerts.filter((alert) => alert.status === 'active'),
    [alerts],
  )
  const currentlySilenced = useCallback((alert: DiagAlert) =>
    !!alert.silenced_until && dayjs(alert.silenced_until).isAfter(dayjs()), [])
  const displayedAlerts = useMemo(() => {
    if (alertFilter === 'range') return rangeAlerts
    if (alertFilter === 'needs_attention') {
      return currentAlerts.filter((alert) => !alert.acknowledged_at && !currentlySilenced(alert))
    }
    if (alertFilter === 'acknowledged') {
      return currentAlerts.filter((alert) => !!alert.acknowledged_at && !currentlySilenced(alert))
    }
    if (alertFilter === 'silenced') return currentAlerts.filter(currentlySilenced)
    if (alertFilter === 'resolved') return rangeAlerts.filter((alert) => alert.status === 'resolved')
    const resolvedInRange = rangeAlerts.filter((alert) => alert.status === 'resolved')
    return [...currentAlerts, ...resolvedInRange]
  }, [alertFilter, currentAlerts, currentlySilenced, rangeAlerts])
  const alertFilterLabel = {
    range: 'Selected time range',
    all: 'Current + resolved',
    needs_attention: 'Needs attention',
    acknowledged: 'Acknowledged',
    silenced: 'Silenced',
    resolved: 'Resolved',
  }[alertFilter]
  const contextControlActions = useMemo(
    () => displayEvents(contextData?.control_actions ?? []),
    [contextData],
  )

  const refreshAll = useCallback(() => {
    setRangeEndEpoch(Math.floor(Date.now() / 1000))
  }, [])

  const reportQueryRange = reportRange ?? { from: timeRange.from, to: timeRange.to }
  const reportDisplayedEvents = useMemo(() => displayEvents(reportEvents), [reportEvents])
  const rangeEventTimesById = useMemo(() => new Map(
    events.map((event) => [event.event_id, event.ts_utc]),
  ), [events])
  const rangeAdviceTimeLabel = useCallback((advice: DiagFinding) => {
    const relatedTimes = advice.related_event_ids
      .map((eventId) => rangeEventTimesById.get(eventId))
      .filter((timestamp): timestamp is string => Boolean(timestamp))
      .map((timestamp) => dayjs(timestamp))
      .sort((left, right) => left.valueOf() - right.valueOf())
    if (relatedTimes.length) {
      const first = relatedTimes[0].format('MMM D, HH:mm:ss')
      const latest = relatedTimes[relatedTimes.length - 1].format('MMM D, HH:mm:ss')
      return first === latest ? `Observed ${first}` : `Observed ${first} - ${latest}`
    }
    const from = advice.time_window?.from
    const to = advice.time_window?.to
    if (typeof from === 'number' && typeof to === 'number') {
      return `Observed ${dayjs.unix(from).format('MMM D, HH:mm')} - ${dayjs.unix(to).format('MMM D, HH:mm')}`
    }
    return 'Derived from selected-range telemetry'
  }, [rangeEventTimesById])
  const reportEventOverview = useMemo(() => {
    const severityCounts: Record<keyof typeof severityColors, number> = { debug: 0, info: 0, warning: 0, error: 0, critical: 0 }
    const laneCounts = new Map<string, number>()
    const laneSeverityCounts = new Map<string, Record<'info' | 'warning' | 'error' | 'critical', number>>()
    for (const item of reportDisplayedEvents) {
      const severity = normalizedSeverity(item.event.severity)
      severityCounts[severity] += 1
      const lane = eventLane(item.event)
      laneCounts.set(lane, (laneCounts.get(lane) || 0) + 1)
      const counts = laneSeverityCounts.get(lane) || { info: 0, warning: 0, error: 0, critical: 0 }
      if (severity !== 'debug') counts[severity as keyof typeof counts] += 1
      laneSeverityCounts.set(lane, counts)
    }
    const categories = Array.from(laneCounts, ([lane, count]) => ({ lane, count, severityCounts: laneSeverityCounts.get(lane) }))
      .sort((left, right) => right.count - left.count || left.lane.localeCompare(right.lane))
    return { total: reportDisplayedEvents.length, severityCounts, categories }
  }, [reportDisplayedEvents])
  const reportOverviewDomains = useMemo(() => {
    const counts = new Map<string, { count: number; severityCounts: Record<'info' | 'warning' | 'error' | 'critical', number> }>()
    for (const item of reportDisplayedEvents) {
      const lane = eventLane(item.event)
      const current = counts.get(lane) || { count: 0, severityCounts: { info: 0, warning: 0, error: 0, critical: 0 } }
      current.count += 1
      const severity = normalizedSeverity(item.event.severity)
      if (severity !== 'debug') current.severityCounts[severity as keyof typeof current.severityCounts] += 1
      counts.set(lane, current)
    }
    return EVENT_DOMAIN_CATALOG.map((domain) => ({
      ...domain,
      count: counts.get(domain.label)?.count || 0,
      severityCounts: counts.get(domain.label)?.severityCounts || { info: 0, warning: 0, error: 0, critical: 0 },
    })).sort((left, right) => right.count - left.count || left.label.localeCompare(right.label))
  }, [reportDisplayedEvents])
  const reportDomainEventTotal = useMemo(
    () => reportOverviewDomains.reduce((total, domain) => total + domain.count, 0),
    [reportOverviewDomains],
  )
  const reportTimeline = useMemo(
    () => eventTimelinePoints(reportDisplayedEvents, reportQueryRange.from, reportQueryRange.to),
    [reportDisplayedEvents, reportQueryRange.from, reportQueryRange.to],
  )
  const reportEventTimesById = useMemo(() => new Map(
    reportEvents.map((event) => [event.event_id, event.ts_utc]),
  ), [reportEvents])
  const reportAdviceTimeLabel = useCallback((advice: DiagFinding) => {
    const relatedTimes = advice.related_event_ids
      .map((eventId) => reportEventTimesById.get(eventId))
      .filter((timestamp): timestamp is string => Boolean(timestamp))
      .map((timestamp) => dayjs(timestamp))
      .sort((left, right) => left.valueOf() - right.valueOf())
    if (relatedTimes.length) {
      const first = relatedTimes[0].format('MMM D, HH:mm:ss')
      const latest = relatedTimes[relatedTimes.length - 1].format('MMM D, HH:mm:ss')
      return first === latest ? `Observed ${first}` : `Observed ${first} - ${latest}`
    }
    const from = advice.time_window?.from
    const to = advice.time_window?.to
    if (typeof from === 'number' && typeof to === 'number') {
      return `Observed ${dayjs.unix(from).format('MMM D, HH:mm')} - ${dayjs.unix(to).format('MMM D, HH:mm')}`
    }
    return 'Derived from report-range telemetry'
  }, [reportEventTimesById])
  const reportAvailableResourceTrendLabels = useMemo(() => {
    const available = new Set<string>()
    for (const point of reportResourceTrend) Object.keys(point.values).forEach((label) => available.add(label))
    return ['CPU', 'Memory', ...Array.from(available).filter((label) => label === 'iGPU' || label === 'dGPU' || label.startsWith('GPU')), 'NPU', 'Disk', 'Network']
      .filter((label, index, labels) => available.has(label) && labels.indexOf(label) === index)
  }, [reportResourceTrend])
  const reportResourceTrendSeries = useMemo(
    () => reportAvailableResourceTrendLabels.filter((label) => reportResourceTrendLabels.has(label)),
    [reportAvailableResourceTrendLabels, reportResourceTrendLabels],
  )
  const reportAverageResourceUtilization = useMemo(() => reportResourceUtilization.map((resource) => ({
    ...resource,
    color: resourceUtilizationColor(resource.label),
  })), [reportResourceUtilization])
  const reportEventActivityTick = useCallback((ts: number) => {
    const duration = reportQueryRange.to - reportQueryRange.from
    return dayjs.unix(ts).format(duration <= 24 * 60 * 60 ? 'HH:mm' : 'MM-DD HH:mm')
  }, [reportQueryRange.from, reportQueryRange.to])

  const loadReport = useCallback(async () => {
    const token = ++reportSeq.current
    setReportLoading(true)
    setReportError(null)
    try {
      const [digest, eventData, findingsData, resourceData] = await Promise.all([
        api.getDiagReport(reportQueryRange.from, reportQueryRange.to),
        api.getDiagEvents({ from: reportQueryRange.from, to: reportQueryRange.to, limit: 5000 }),
        api.getDiagContext({ from: reportQueryRange.from, to: reportQueryRange.to, findings_only: true }),
        api.getDiagResourceUtilization(reportQueryRange.from, reportQueryRange.to),
      ])
      if (token !== reportSeq.current) return
      const resourceTrend = resourceData.monitor.trend || []
      const resourceLabels = new Set(resourceTrend.flatMap((point) => Object.keys(point.values)))
      setReport(digest)
      setReportEvents(eventData.events || [])
      setReportFindings(findingsData.findings || [])
      setReportResourceUtilization(resourceData.monitor.resources || [])
      setReportResourceTrend(resourceTrend)
      setReportResourceSampleCount(resourceData.monitor.count || 0)
      setReportResourceTrendLabels(resourceLabels)
    } catch (error) {
      if (token !== reportSeq.current) return
      setReport(null)
      setReportEvents([])
      setReportFindings([])
      setReportResourceUtilization([])
      setReportResourceTrend([])
      setReportResourceSampleCount(0)
      setReportError(error instanceof Error ? error.message : 'Failed to generate report')
    } finally {
      if (token === reportSeq.current) setReportLoading(false)
    }
  }, [reportQueryRange.from, reportQueryRange.to])

  const openReport = useCallback(() => {
    setReportRange({ from: timeRange.from, to: timeRange.to })
    setReportResourceTrendLabels(new Set())
    setReportOpen(true)
    setReport(null)
    setReportError(null)
  }, [resourceTrendLabels, timeRange.from, timeRange.to])

  useEffect(() => {
    if (reportOpen) void loadReport()
  }, [loadReport, reportOpen])

  const reportPayload = useMemo(() => {
    if (!report) return null
    return {
      ...report,
      report_snapshot: {
        overview: {
          total_events: reportEventOverview.total,
          errors: reportEventOverview.severityCounts.error + reportEventOverview.severityCounts.critical,
          affected_domains: reportEventOverview.categories.length,
        },
        events_by_severity: reportEventOverview.severityCounts,
        events_by_domain: reportOverviewDomains.map((domain) => ({ domain: domain.label, count: domain.count })),
        event_activity: {
          filtered_event_count: reportDisplayedEvents.length,
          timeline_event_count: reportTimeline.points.length,
          range: reportQueryRange,
        },
        alerts: {
          range_activity: report.range_alerts.length,
          active_currently: report.active_alerts.length,
          alert_summary_count: report.alert_summary.length,
        },
        advices: reportFindings.map((finding) => ({
          id: finding.id,
          severity: finding.severity,
          title: finding.title,
          confidence: finding.confidence,
          observation: finding.observation,
          recommendation: finding.recommendations?.[0] ?? null,
          validation_step: finding.validation_steps?.[0] ?? null,
        })),
      },
    }
  }, [report, reportDisplayedEvents.length, reportEventOverview, reportFindings, reportOverviewDomains, reportQueryRange, reportTimeline.points.length])

  const reportSeverityDonutData = useMemo(() => {
    if (!report) return []
    return [
      { name: 'Info', value: report.event_stats.by_severity.info, color: COLORS.accent },
      { name: 'Warning', value: report.event_stats.by_severity.warning, color: COLORS.yellow },
      { name: 'Error', value: report.event_stats.by_severity.error, color: DIAGNOSTIC_ERROR_COLOR },
      { name: 'Critical', value: report.event_stats.by_severity.critical, color: COLORS.red },
    ]
  }, [report])

  const downloadReport = useCallback(() => {
    if (!reportPayload) return
    const url = URL.createObjectURL(new Blob([JSON.stringify(reportPayload, null, 2)], {
      type: 'application/json;charset=utf-8',
    }))
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `diagnostics-report-${dayjs.unix(reportQueryRange.from).format('YYYYMMDD-HHmm')}-${dayjs.unix(reportQueryRange.to).format('YYYYMMDD-HHmm')}.json`
    document.body.appendChild(anchor)
    anchor.click()
    document.body.removeChild(anchor)
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  }, [reportPayload, reportQueryRange.from, reportQueryRange.to])

  useEffect(() => {
    if (active && !wasActive.current) setRangeEndEpoch(Math.floor(Date.now() / 1000))
    wasActive.current = active
  }, [active])

  useEffect(() => {
    if (!active) return undefined
    let cancelled = false
    api.getDiagContext({ from: timeRange.from, to: timeRange.to, findings_only: true })
      .then((data) => {
        if (!cancelled) setRangeFindings(data.findings || [])
      })
      .catch(() => {
        if (!cancelled) setRangeFindings([])
      })
    return () => { cancelled = true }
  }, [active, timeRange.from, timeRange.to])

  useEffect(() => {
    if (!active) return undefined
    let cancelled = false
    setRangeMonitorLoading(true)
    api.getDiagResourceUtilization(timeRange.from, timeRange.to)
      .then((data) => {
        if (!cancelled) {
          setRangeResourceUtilization(data.monitor.resources || [])
          setRangeResourceTrend(data.monitor.trend || [])
          setRangeMonitorSampleCount(data.monitor.count || 0)
        }
      })
      .catch(() => {
        if (!cancelled) {
          setRangeResourceUtilization([])
          setRangeResourceTrend([])
          setRangeMonitorSampleCount(0)
        }
      })
      .finally(() => {
        if (!cancelled) setRangeMonitorLoading(false)
      })
    return () => { cancelled = true }
  }, [active, timeRange.from, timeRange.to])

  useEffect(() => {
    if (!active) return undefined
    let cancelled = false
    setAlertsLoading(true)
    api.getDiagAlerts(false, 300)
      .then((data) => {
        if (!cancelled) setAlerts(data.alerts || [])
      })
      .catch(() => {
        if (!cancelled) setAlerts([])
      })
      .finally(() => {
        if (!cancelled) setAlertsLoading(false)
      })
    return () => { cancelled = true }
  }, [active, rangeEndEpoch])

  const investigateFinding = useCallback((finding: DiagFinding) => {
    const event = events.find((item) => finding.related_event_ids?.includes(item.event_id))
    const target = event ? contextTargetForEvent(event) : null
    if (event && target) void openContext(target, event)
  }, [events, openContext])

  const investigateAlert = useCallback(async (alert: DiagAlert) => {
    if (!alert.last_event_id) return
    const event = await api.getDiagEvent(alert.last_event_id)
    if (!event) return
    const target = contextTargetForEvent(event)
    if (target) void openContext(target, event)
    else setSelectedEvent(displayEvents([event])[0])
  }, [openContext])

  const refreshAlerts = useCallback(async () => {
    const data = await api.getDiagAlerts(false, 300)
    setAlerts(data.alerts || [])
  }, [])

  const acknowledgeAlert = useCallback(async (alert: DiagAlert) => {
    setAlertActionKey(alert.dedup_key)
    try {
      await api.acknowledgeDiagAlert(alert.dedup_key)
      await refreshAlerts()
      message.success('Alert acknowledged')
    } catch (error) {
      message.error(error instanceof Error ? error.message : 'Unable to acknowledge alert')
    } finally {
      setAlertActionKey(null)
    }
  }, [refreshAlerts])

  const silenceAlert = useCallback(async (alert: DiagAlert, minutes: 30 | 120) => {
    setAlertActionKey(alert.dedup_key)
    try {
      await api.silenceDiagAlert(alert.dedup_key, minutes)
      await refreshAlerts()
      message.success(`Alert silenced for ${minutes === 30 ? '30 minutes' : '2 hours'}`)
    } catch (error) {
      message.error(error instanceof Error ? error.message : 'Unable to silence alert')
    } finally {
      setAlertActionKey(null)
    }
  }, [refreshAlerts])

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

  useEffect(() => {
    if (!openAlertsSignal) return
    setAlertFilter('needs_attention')
    setAlertsOpen(true)
  }, [openAlertsSignal])

  return (
    <Card className='diagnostics-page'>
      <div className='diagnostics-toolbar'>
        <Row className='diagnostics-filter-row' gutter={[8, 8]} align='middle' justify='space-between'>
          <Col flex='auto'>
            <div className='diagnostics-time-selection'>
              <Space className='diagnostics-boot-controls' wrap size={8}>
                <Tooltip title='Choose the boot cycle that bounds all time ranges. The latest boot is selected automatically.'>
                  <Text className='diagnostics-control-label' strong>Boot cycle</Text>
                </Tooltip>
                <Select
                  aria-label='Boot cycle'
                  className='diagnostics-boot-select'
                  placeholder='Select a boot cycle'
                  loading={bootsLoading}
                  value={selectedBootId ?? undefined}
                  onChange={(value) => {
                    setSelectedBootId(value)
                    setBootCustomRange(null)
                    resetEventViewForTimeRange()
                  }}
                  options={boots.map((b) => ({ label: formatBootLabel(b), value: b.boot_id }))}
                  notFoundContent={bootsLoading ? 'Loading…' : 'No boot sessions (journald unavailable)'}
                />
              </Space>
              <Space className='diagnostics-window-controls' wrap size={8}>
                <Tooltip title='Select how far back to look from now, or from the selected boot end time for a completed boot.'>
                  <Text className='diagnostics-control-label' strong>Time range</Text>
                </Tooltip>
                <Segmented
                  className='diagnostics-time-range-segmented'
                  value={windowKey}
                  onChange={(value) => {
                    setWindowKey(value as WindowKey)
                    resetEventViewForTimeRange()
                  }}
                  options={[
                    { label: '15m', value: '15m' },
                    { label: '1h', value: '1h' },
                    { label: '6h', value: '6h' },
                    { label: '24h', value: '24h' },
                    { label: 'All', value: 'entire' },
                    { label: 'Custom', value: 'custom' },
                  ]}
                />
              </Space>
              {windowKey === 'custom' ? (
                <DatePicker.RangePicker
                  className='diagnostics-custom-range'
                  showTime={{ format: 'HH:mm' }}
                  format='MM-DD HH:mm'
                  value={bootCustomRange ? [dayjs.unix(bootCustomRange.from), dayjs.unix(bootCustomRange.to)] : null}
                  disabledDate={selectedBoot?.first_ts ? (date) => {
                    const from = dayjs.unix(selectedBoot.first_ts!).startOf('day')
                    const to = dayjs.unix(selectedBoot.running ? rangeEndEpoch + (clockSkewSec ?? 0) : selectedBoot.last_ts ?? rangeEndEpoch).endOf('day')
                    return date.endOf('day').isBefore(from) || date.startOf('day').isAfter(to)
                  } : undefined}
                  onChange={(vals) => {
                    const range = vals && vals[0] && vals[1]
                      ? { from: vals[0].unix(), to: vals[1].unix() }
                      : null
                    setBootCustomRange(range)
                    setCustomRange(range)
                    resetEventViewForTimeRange()
                  }}
                />
              ) : null}
            </div>
          </Col>
          <Col>
            <Space className='diagnostics-toolbar-actions' size={8} wrap>
              <Input.Search
                allowClear
                aria-label='Search events and log messages'
                className='diagnostics-toolbar-search'
                placeholder='Search events and logs'
                value={eventKeyword}
                onChange={(event) => {
                  setEventKeyword(event.target.value)
                  setEventPage(1)
                }}
                onSearch={(value) => {
                  setLogKeyword(value)
                  logKeywordRef.current = value
                  setLogsOpen(true)
                  void loadLogs()
                }}
              />
              <Tooltip title={`Current alerts${currentAlerts.length ? ` (${currentAlerts.length})` : ''}. Open it to view active alerts, resolved history, and alert policy.`}>
                <Button
                  aria-label='Alert history'
                  icon={<BellOutlined />}
                  onClick={() => {
                    setAlertFilter('all')
                    setAlertsOpen(true)
                  }}
                  loading={alertsLoading}
                />
              </Tooltip>
              <Tooltip title={`Advices${rangeFindings.length ? ` (${rangeFindings.length})` : ''}. Advice is generated by deterministic rules from metrics and supported built-in event types; custom-rule events do not independently create advice.`}>
                <Button aria-label='Advices' icon={<BulbOutlined />} onClick={() => setInsightsOpen(true)} />
              </Tooltip>
              <Tooltip title='Diagnostics report'>
                <Button aria-label='Diagnostics report' icon={<FileTextOutlined />} onClick={openReport} />
              </Tooltip>
              <Dropdown
                trigger={['click']}
                menu={{
                  selectable: true,
                  selectedKeys: [refreshInterval],
                  items: [
                    { label: 'Refresh off', key: 'off' },
                    { label: 'Every 5 seconds', key: '5s' },
                    { label: 'Every 10 seconds', key: '10s' },
                    { label: 'Every 30 seconds', key: '30s' },
                  ],
                  onClick: ({ key }) => setRefreshInterval(key as RefreshInterval),
                }}
              >
                <Tooltip title={`Auto refresh: ${refreshInterval === 'off' ? 'off' : `every ${refreshInterval}`}`}>
                  <Button icon={<ClockCircleOutlined />}>
                    {refreshInterval === 'off' ? 'Refresh off' : `Every ${refreshInterval}`}
                  </Button>
                </Tooltip>
              </Dropdown>
              <Tooltip title='Refresh diagnostics'>
                <Button aria-label='Refresh diagnostics' icon={<ReloadOutlined />} onClick={refreshAll} loading={eventsLoading} />
              </Tooltip>
            </Space>
          </Col>
        </Row>
        <Space className='diagnostics-range' wrap size={8}>
          <Text className='diagnostics-range-summary' type='secondary'>
            {dayjs.unix(timeRange.from).format('MMM D HH:mm:ss')} - {dayjs.unix(timeRange.to).format('MMM D HH:mm:ss')}
          </Text>
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

      <section className='diagnostics-event-overview' aria-label='Selected time range overview'>
        <div className='diagnostics-overview-header'>
          <Text className='diagnostics-overview-heading'>Selected time range overview</Text>
        </div>
        {eventsLoading ? null : eventOverview.total === 0 ? (
          <div className='diagnostics-overview-empty'>
            <div>
              <Text strong>No events in this view</Text>
              <Text type='secondary'>No diagnostic events were recorded for the current time window and filters.</Text>
            </div>
            {eventSeverity || eventDomainFilter || eventKindFilter || eventFatalSignature || eventLaneFilter ? (
              <Button size='small' onClick={() => {
                setEventSeverity(undefined)
                setEventDomainFilter(undefined)
                setEventKindFilter(undefined)
                setEventFatalSignature(undefined)
                setEventLaneFilter(undefined)
                setTimelineEventKeys(null)
                setEventPage(1)
              }}>Clear event filters</Button>
            ) : null}
          </div>
        ) : (
          <div className='diagnostics-overview-sections'>
            <div className='diagnostics-overview-summary-strip'>
              <div className='diagnostics-overview-inline-group' aria-label='Event profile'>
                <Tooltip title='Show all events in the selected range'>
                  <button
                    className='diagnostics-overview-inline-action diagnostics-overview-inline-events'
                    type='button'
                    aria-pressed={!eventSeverity && !eventDomainFilter && !eventKindFilter && !eventFatalSignature && !eventLaneFilter && !timelineEventKeys}
                    onClick={() => {
                    setEventSeverity(undefined)
                    setEventDomainFilter(undefined)
                    setEventKindFilter(undefined)
                    setEventFatalSignature(undefined)
                    setEventLaneFilter(undefined)
                    setTimelineEventKeys(null)
                    setEventPage(1)
                    }}
                  >
                    <FileTextOutlined /> <strong>{eventOverview.total}</strong> events
                  </button>
                </Tooltip>
                <span className='diagnostics-overview-inline-severity' aria-label='Event severity distribution'>
                  {[
                    ['critical', 'Crit', COLORS.red],
                    ['error', 'Err', DIAGNOSTIC_ERROR_COLOR],
                    ['warning', 'Warn', COLORS.yellow],
                    ['info', 'Info', COLORS.accent],
                  ].map(([severity, label, color]) => {
                    const count = eventOverview.severityCounts[severity as keyof typeof eventOverview.severityCounts]
                    return <Tooltip key={severity} title={`Filter events by ${severity} severity`}>
                      <button
                        type='button'
                        className={`diagnostics-overview-inline-action diagnostics-overview-inline-severity-item diagnostics-overview-inline-severity-item--${severity}`}
                        data-empty={count === 0 || undefined}
                        data-active={count > 0 || undefined}
                        aria-pressed={eventSeverity === severity}
                        onClick={() => {
                          setEventSeverity(eventSeverity === severity ? undefined : severity)
                          setEventLaneFilter(undefined)
                          setTimelineEventKeys(null)
                          setEventPage(1)
                        }}
                      >
                        <span className='diagnostics-filter-chip-dot' style={{ background: color }} />
                        <strong>{count}</strong> {label}
                      </button>
                    </Tooltip>
                  })}
                </span>
              </div>

              <div className='diagnostics-overview-inline-divider' />

              <Tooltip title='View alerts in the selected range'>
                <button className='diagnostics-overview-inline-action diagnostics-overview-inline-alert' type='button' onClick={() => {
                  setAlertFilter('range')
                  setAlertsOpen(true)
                }}>
                  <BellOutlined /> <strong>{alertsLoading ? '-' : rangeAlerts.length}</strong> alerts
                </button>
              </Tooltip>

              <div className='diagnostics-overview-inline-divider' />

              <Tooltip title='View advice items for the selected range'>
                <button className='diagnostics-overview-inline-action' type='button' onClick={() => setInsightsOpen(true)}>
                  <BulbOutlined /> <strong>{rangeFindings.length}</strong> advice items
                </button>
              </Tooltip>

              <div className='diagnostics-overview-inline-divider' />

              <Tooltip title='Domains with at least one event in the selected range. Filter individual domains below.'>
                <span className='diagnostics-overview-inline-scope'>
                  <InfoCircleOutlined /> <strong>{eventOverview.categories.length}</strong> affected domain{eventOverview.categories.length === 1 ? '' : 's'}
                </span>
              </Tooltip>
            </div>

          <section className='diagnostics-overview-section diagnostics-overview-domains' aria-label='Event counts by domain'>
            <div className='diagnostics-overview-section-heading'>
              <Text className='diagnostics-overview-section-title'>Events by domain ({domainEventTotal})</Text>
            </div>
            <div className='diagnostics-domain-distribution'>
              {overviewDomains.map((domain) => {
                const percentage = domainEventTotal ? domain.count / domainEventTotal * 100 : 0
                const severityDescription = [
                  `${domain.severityCounts.info} info`,
                  `${domain.severityCounts.warning} warning`,
                  `${domain.severityCounts.error} error`,
                  `${domain.severityCounts.critical} critical`,
                ].join(' · ')
                return <Tooltip key={domain.label} title={`${domain.description} ${severityDescription}. Click to filter events.`}>
                  <button
                    type='button'
                    className='diagnostics-domain-distribution-row'
                    data-empty={domain.count === 0 || undefined}
                    aria-label={`${domain.label}: ${domain.count} events`}
                    aria-pressed={eventLaneFilter === domain.label}
                    onClick={() => selectEventDomain(domain.label)}
                  >
                    <span className='diagnostics-domain-distribution-label'>{domain.label}</span>
                    <span className='diagnostics-domain-distribution-track'>
                      <span className='diagnostics-domain-distribution-fill' style={{ width: `${percentage}%` }}>
                        {[
                          ['info', COLORS.accent],
                          ['warning', COLORS.yellow],
                          ['error', DIAGNOSTIC_ERROR_COLOR],
                          ['critical', COLORS.red],
                        ].map(([severity, color]) => {
                          const count = domain.severityCounts[severity as keyof typeof domain.severityCounts]
                          return count ? <span
                            className='diagnostics-domain-distribution-severity'
                            key={severity}
                            style={{ width: `${count / domain.count * 100}%`, background: color }}
                          /> : null
                        })}
                      </span>
                    </span>
                    <span className='diagnostics-domain-distribution-value'>{domain.count} ({percentage.toFixed(1)}%)</span>
                  </button>
                </Tooltip>
              })}
            </div>
          </section>

          <section className='diagnostics-overview-section diagnostics-overview-utilization' aria-label='Resource average utilization'>
            <Text className='diagnostics-overview-section-title'>Resource average utilization (%)</Text>
            {rangeMonitorLoading ? <Text type='secondary'>Loading resource samples...</Text> : averageResourceUtilization.length ? (
              <div className='diagnostics-utilization-rows'>
                {averageResourceUtilization.map((resource) => (
                  <div className='diagnostics-utilization-row' key={resource.label}>
                    <span className='diagnostics-utilization-label'>{resource.label}</span>
                    <span className='diagnostics-utilization-track'>
                      <span className='diagnostics-utilization-average' style={{ width: `${Math.min(100, resource.value)}%`, background: resource.color }} />
                      <span className='diagnostics-utilization-peak' style={{ left: `${Math.min(100, resource.peak)}%`, borderColor: resource.color }} />
                    </span>
                    <span className='diagnostics-utilization-value'>{resource.value.toFixed(1)}% <span>(Peak {resource.peak.toFixed(1)}%)</span></span>
                  </div>
                ))}
              </div>
            ) : <Text type='secondary'>No resource samples in this range</Text>}
            <Text className='diagnostics-utilization-caption' type='secondary'>Solid: average · marker: peak · selected range{rangeMonitorSampleCount ? ` · ${rangeMonitorSampleCount} samples` : ''}</Text>
          </section>
        </div>
        )}
        {activeControlRows.length > 0 || latestBenchmarkEvent ? (
          <div className='diagnostics-overview-links'>
            {activeControlRows.length > 0 ? (
              <Button type='link' onClick={() => activeControlsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>
                {activeControlRows.length} resource limit{activeControlRows.length === 1 ? '' : 's'} in effect
              </Button>
            ) : null}
            {latestBenchmarkEvent ? (
              <Button type='link' onClick={() => {
                const target = contextTargetForEvent(latestBenchmarkEvent)
                if (target) void openContext(target, latestBenchmarkEvent)
              }}>
                Latest benchmark: {displayEventSummary(displayEvents([latestBenchmarkEvent])[0])}
              </Button>
            ) : null}
          </div>
        ) : null}
      </section>

      {eventsLoading || eventOverview.total > 0 ? (
      <section className='diagnostics-event-activity' aria-label='Event activity timeline'>
        <div className='diagnostics-panel-heading'>
          <Text strong>Event activity</Text>
          <Space size={8}>
            {eventBrushRange || eventZoomRange ? (
              <Tooltip title='Reset zoom'>
                <Button size='small' type='text' aria-label='Reset event timeline zoom' icon={<RollbackOutlined />} onClick={resetEventZoom} />
              </Tooltip>
            ) : null}
          </Space>
        </div>
        {eventsLoading ? null : displayedEvents.length ? (
          <>
            <div className='diagnostics-event-timeline diagnostics-event-timeline--selectable' style={{ height: Math.max(220, eventTimeline.lanes.length * 46 + 72) }}>
              <span className='diagnostics-event-domain-heading'>Domain</span>
              <ResponsiveContainer width='100%' height='100%'>
                <ScatterChart
                  margin={{ top: 8, right: 16, left: EVENT_TIMELINE_LEFT_GUTTER, bottom: 0 }}
                  onMouseDown={beginEventZoom}
                  onMouseMove={updateEventZoom}
                  onMouseUp={finishEventZoom}
                  onMouseLeave={() => {
                    eventZoomDraggedRef.current = false
                    setEventZoomSelection(null)
                    setSuppressEventTooltip(false)
                  }}
                  onDoubleClick={resetEventZoom}
                >
                  <CartesianGrid stroke={`${COLORS.border}99`} strokeDasharray='3 3' />
                  <XAxis
                    type='number'
                    dataKey='x'
                    domain={[eventViewRange.from, eventViewRange.to]}
                    tickFormatter={eventActivityTick}
                    tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                    minTickGap={36}
                  />
                  <YAxis
                    type='number'
                    dataKey='y'
                    yAxisId='domain'
                    domain={[-0.5, Math.max(0.5, eventTimeline.lanes.length - 0.5)]}
                    ticks={eventTimeline.lanes.map((_, index) => index)}
                    tickFormatter={(index) => {
                      const lane = eventTimeline.lanes[index]
                      return lane ? `${lane} (${eventTimeline.laneCounts.get(lane) || 0})` : ''
                    }}
                    tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                    width={EVENT_TIMELINE_LABEL_WIDTH}
                    reversed
                  />
                  <RechartsTooltip
                    active={suppressEventTooltip ? false : undefined}
                    content={eventTimelineTooltip}
                    allowEscapeViewBox={{ y: true }}
                    wrapperStyle={{ zIndex: 30 }}
                  />
                  <Legend
                    verticalAlign='top'
                    align='right'
                    height={28}
                    content={() => (
                      <div className='diagnostics-event-severity-legend' aria-label='Event severity legend'>
                        <span className='diagnostics-event-severity-legend-title'>Severity</span>
                        {[
                          ['Info', COLORS.accent],
                          ['Warning', COLORS.yellow],
                          ['Error', DIAGNOSTIC_ERROR_COLOR],
                          ['Critical', COLORS.red],
                        ].map(([label, color]) => (
                          <span className='diagnostics-event-severity-legend-item' key={label}>
                            <i style={{ backgroundColor: color }} />
                            {label}
                          </span>
                        ))}
                      </div>
                    )}
                  />
                  {eventZoomSelection ? (
                    <ReferenceArea
                      x1={Math.min(eventZoomSelection.from, eventZoomSelection.to)}
                      x2={Math.max(eventZoomSelection.from, eventZoomSelection.to)}
                      fill={COLORS.accent}
                      fillOpacity={0.16}
                      stroke={COLORS.accent}
                      strokeOpacity={0.8}
                    />
                  ) : null}
                  <Scatter
                    data={eventTimeline.points}
                    name='Events'
                    yAxisId='domain'
                    shape={(shapeProps: unknown) => {
                      const props = shapeProps as {
                        payload?: unknown
                        cx?: number
                        cy?: number
                        fill?: string
                        onMouseEnter?: React.MouseEventHandler<SVGGElement>
                        onMouseLeave?: React.MouseEventHandler<SVGGElement>
                      }
                      const point = props.payload as EventTimelinePoint | undefined
                      const cx = typeof props.cx === 'number' ? props.cx : 0
                      const cy = typeof props.cy === 'number' ? props.cy : 0
                      const fill = typeof props.fill === 'string' ? props.fill : COLORS.accent
                      if (!point) return <g />
                      const count = point.items.length
                      const markerHeight = count === 1 ? 16 : 28
                      const severityCounts = point.items.reduce<Record<string, number>>((counts, item) => {
                        const severity = normalizedSeverity(item.event.severity)
                        counts[severity] = (counts[severity] || 0) + 1
                        return counts
                      }, {})
                      let segmentY = cy - markerHeight / 2
                      return (
                        <g
                          onMouseEnter={props.onMouseEnter}
                          onMouseLeave={props.onMouseLeave}
                          onClick={(event) => {
                            if (eventZoomDraggedRef.current) {
                              event.stopPropagation()
                              return
                            }
                            selectTimelineCluster(point)
                          }}
                          style={{ cursor: 'pointer' }}
                        >
                          {(['critical', 'error', 'warning', 'info', 'debug'] as const).map((severity) => {
                            const severityCount = severityCounts[severity] || 0
                            if (!severityCount) return null
                            const height = markerHeight * severityCount / count
                            const rect = (
                              <rect
                                key={severity}
                                x={cx - (count === 1 ? 2 : 4)}
                                y={segmentY}
                                width={count === 1 ? 4 : 8}
                                height={height}
                                fill={severityColors[severity] || fill}
                                stroke={COLORS.panelBg}
                                strokeWidth={1}
                              />
                            )
                            segmentY += height
                            return rect
                          })}
                          <text x={cx + 7} y={cy + 4} fill={COLORS.text} fontSize={10} fontWeight={600}>{count}</text>
                          <rect x={cx - 7} y={cy - 18} width={count > 1 ? 28 : 14} height={36} fill='transparent' />
                        </g>
                      )
                    }}
                  />
                </ScatterChart>
              </ResponsiveContainer>
            </div>
            <div className='diagnostics-event-brush' aria-label='Event timeline zoom control'>
              <ResponsiveContainer width='100%' height='100%'>
                <LineChart data={eventBrushPoints} margin={{ top: 0, right: 16, left: EVENT_TIMELINE_PLOT_LEFT, bottom: 0 }}>
                  <Line dataKey='x' stroke='transparent' dot={false} isAnimationActive={false} />
                  <Brush
                    dataKey='x'
                    height={28}
                    stroke={COLORS.accent}
                    fill={COLORS.bg}
                    travellerWidth={8}
                    alwaysShowText
                    tickFormatter={(timestamp) => eventActivityTick(Number(timestamp))}
                    {...(eventBrushRange ? {
                      startIndex: eventBrushRange.startIndex,
                      endIndex: eventBrushRange.endIndex,
                    } : {})}
                    onChange={handleEventBrushChange}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
            {resourceTrendLabels.length > 0 ? (
              <Collapse
                className='diagnostics-logs diagnostics-resource-lanes'
                activeKey={resourceLanesOpen ? ['resource-trends'] : []}
                onChange={(keys) => setResourceLanesOpen((Array.isArray(keys) ? keys : [keys]).includes('resource-trends'))}
                items={[
                  {
                    key: 'resource-trends',
                    label: 'Resource trends',
                    extra: (
                      <Button
                        type='text'
                        size='small'
                        onClick={(event) => {
                          event.stopPropagation()
                          setActiveResourceTrendLabels(new Set(resourceTrendLabels))
                        }}
                      >
                        All
                      </Button>
                    ),
                    children: (
                      <>
                    <div className='diagnostics-resource-lanes-legend'>
                      {resourceTrendLabels.map((label, index) => {
                        const active = activeResourceTrendLabels.has(label)
                        const dashed = index >= 3
                        return <button
                          key={label}
                          type='button'
                          className='diagnostics-resource-lane-toggle'
                          data-active={active || undefined}
                          onClick={() => setActiveResourceTrendLabels((selected) => {
                            const next = new Set(selected)
                            if (next.has(label)) next.delete(label)
                            else next.add(label)
                            return next
                          })}
                        >
                          <i style={{ borderTopColor: active ? resourceUtilizationColor(label) : COLORS.textMuted, borderTopStyle: dashed ? 'dashed' : 'solid' }} />
                          {active ? '' : '+ '}{label}
                        </button>
                      })}
                    </div>
                    {activeResourceTrendSeries.length ? (
                      <div className='diagnostics-resource-lanes-chart'>
                        <ResponsiveContainer width='100%' height='100%'>
                          <LineChart data={visibleResourceTrend} margin={{ top: 8, right: 16, bottom: 0, left: EVENT_TIMELINE_PLOT_LEFT - 48 }}>
                            <CartesianGrid stroke={`${COLORS.border}99`} strokeDasharray='3 3' vertical={false} />
                            <XAxis type='number' dataKey='ts_epoch' domain={[eventViewRange.from, eventViewRange.to]} tickFormatter={eventActivityTick} tick={{ fill: COLORS.textMuted, fontSize: 11 }} minTickGap={36} />
                            <YAxis type='number' domain={[0, 100]} ticks={[0, 50, 100]} width={48} tick={{ fill: COLORS.textMuted, fontSize: 11 }} tickFormatter={(value) => `${value}%`} label={{ value: 'Utilization', angle: -90, position: 'insideLeft', fill: COLORS.textMuted, fontSize: 11 }} />
                            <RechartsTooltip formatter={(value: number) => `${value.toFixed(1)}%`} labelFormatter={(timestamp) => dayjs.unix(Number(timestamp)).format('YYYY-MM-DD HH:mm:ss')} contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text }} cursor={{ stroke: COLORS.accent, strokeWidth: 1, strokeDasharray: '4 2' }} allowEscapeViewBox={{ x: true, y: true }} wrapperStyle={{ zIndex: 30 }} />
                            {eventTimeline.points.map((point) => <ReferenceLine key={`event-${point.x}`} x={point.x} stroke={COLORS.textMuted} strokeOpacity={0.45} strokeDasharray='3 3' />)}
                            {activeResourceTrendSeries.map((label, index) => <Line key={label} name={label} type='monotone' dataKey={`values.${label}`} stroke={resourceUtilizationColor(label)} strokeDasharray={index >= 3 ? '5 3' : undefined} strokeWidth={2} dot={false} connectNulls isAnimationActive={false} />)}
                          </LineChart>
                        </ResponsiveContainer>
                      </div>
                    ) : <Text className='diagnostics-resource-lanes-empty' type='secondary'>Select a resource to display its trend</Text>}
                      </>
                    ),
                  },
                ]}
              />
            ) : null}
          </>
        ) : (
          <Empty className='diagnostics-event-activity-empty' image={Empty.PRESENTED_IMAGE_SIMPLE} description='No events in the selected range' />
        )}
      </section>
      ) : null}

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
      ) : null}


      {activeControlRows.length > 0 ? (
        <section className='diagnostics-active-controls' ref={activeControlsRef}>
          <div className='diagnostics-active-controls-head'>
            <div>
              <Text strong>Resource limits in effect ({activeControlRows.length})</Text>
              <Text className='diagnostics-active-controls-scope' type='secondary'>Current host state</Text>
            </div>
          </div>
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
                {row.isActive && row.appId ? (
                  <Tooltip title='Restore all active limits for this application'>
                    <Button size='small' danger icon={<RollbackOutlined />} loading={resumingAppId === row.appId} onClick={() => void resumeControl(row.appId!, row.protectionId, row.hasCgroups)}>Resume</Button>
                  </Tooltip>
                ) : null}
              </div>
            ))}
            {activeControlRows.length > 3 ? (
              <Button className='diagnostics-active-controls-toggle' type='link' size='small' onClick={() => setShowAllActiveControls((visible) => !visible)}>
                {showAllActiveControls ? 'Show less' : `Show ${activeControlRows.length - 3} more resource limits`}
              </Button>
            ) : null}
          </div>
        </section>
      ) : null}

      {eventsLoading || events.length > 0 || currentControlLifecycles.length > 0 ? (
      <div ref={eventsTableRef}>
      <Collapse
        className='diagnostics-logs diagnostics-event-records'
        activeKey={eventRecordsOpen ? ['events'] : []}
        onChange={(keys) => setEventRecordsOpen((Array.isArray(keys) ? keys : [keys]).includes('events'))}
        items={[
          {
            key: 'events',
            label: (
              <Space size={4}>
                <span>{`Recent events (${eventsLoading ? '-' : displayedEvents.length}${timelineEventKeys ? ' selected' : ''})`}</span>
                <Tooltip title='Each row is a structured event. Event type is its stable machine identifier; Domain and Event kind only group and filter events. A custom rule creates CUSTOM_RULE::<Rule ID>.'>
                  <InfoCircleOutlined style={{ color: COLORS.textMuted }} />
                </Tooltip>
              </Space>
            ),
            children: (
              <>
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
              { label: 'IMPORTANT', value: 'important' },
              { label: 'INFO', value: 'info' },
              { label: 'WARNING', value: 'warning' },
              { label: 'ERROR', value: 'error' },
              { label: 'CRITICAL', value: 'critical' },
            ]}
          />
          <Select
            className='diagnostics-category-select'
            aria-label='Event domain'
            value={eventDomainFilter || ALL}
            onChange={(value) => {
              setEventDomainFilter(value === ALL ? undefined : value as EventDomain)
              setEventLaneFilter(undefined)
              setEventPage(1)
            }}
            options={eventDomainOptions}
          />
          <Select
            className='diagnostics-category-select'
            aria-label='Event kind'
            value={eventKindFilter || ALL}
            onChange={(value) => {
              setEventKindFilter(value === ALL ? undefined : value as EventKind)
              setEventPage(1)
            }}
            options={eventKindOptions}
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
            {eventKeyword || eventSeverity || eventDomainFilter || eventKindFilter || eventFatalSignature || eventSource
              ? `${displayedEvents.length} matching of ${displayEvents(events).length} ${displayEvents(events).length === 1 ? 'event record' : 'event records'}`
              : `${displayedEvents.length} ${displayedEvents.length === 1 ? 'event record' : 'event records'}`}
          </Text>
          {timelineEventKeys ? (
            <Tag closable onClose={() => setTimelineEventKeys(null)} color='blue'>Timeline selection · oldest first</Tag>
          ) : null}
        </Space>
        <Table
          rowKey='event_id'
          loading={eventsLoading}
          dataSource={displayedEvents}
          pagination={{ current: eventPage, pageSize: 15, showSizeChanger: false, onChange: setEventPage }}
          size='small'
          className='diagnostics-table diagnostics-events-table'
          scroll={{ x: 1300 }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description='No events match the selected filters' /> }}
          columns={eventColumns}
        />
              </>
            ),
          },
        ]}
      />
      </div>
      ) : null}

      <Collapse
        activeKey={logsOpen ? ['logs'] : []}
        onChange={(keys) => setLogsOpen((Array.isArray(keys) ? keys : [keys]).includes('logs'))}
        className='diagnostics-logs'
        items={[
          {
            key: 'logs',
            label: objectFilters.length
              ? `Evidence search · filtered by ${objectFilters.map((f) => f.label).join(', ')}`
              : 'Evidence search',
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

      <Drawer
        title={`Advices · selected range${rangeFindings.length ? ` (${rangeFindings.length})` : ''}`}
        open={insightsOpen}
        onClose={() => setInsightsOpen(false)}
        width={560}
      >
        {rangeFindings.length ? (
          <Space direction='vertical' size={16} style={{ width: '100%' }}>
            {rangeFindings.map((finding) => (
              <section className='diagnostics-insight-drawer-item' key={finding.id}>
                <Space size={6} wrap>
                  {severityTag(finding.severity)}
                  <Text strong>{finding.title}</Text>
                </Space>
                <Text className='diagnostics-report-advice-time' type='secondary'>{rangeAdviceTimeLabel(finding)}</Text>
                <Text>{finding.observation}</Text>
                <Text type='secondary'>Evidence confidence {Math.round(finding.confidence * 100)}%</Text>
                {finding.recommendations?.length ? (
                  <div>
                    <Text strong>Recommended next step</Text>
                    <Text className='diagnostics-drawer-list-item'>{finding.recommendations[0]}</Text>
                  </div>
                ) : null}
                {finding.validation_steps?.length ? (
                  <div>
                    <Text strong>Verify</Text>
                    <Text className='diagnostics-drawer-list-item'>{finding.validation_steps[0]}</Text>
                  </div>
                ) : null}
                <Button type='primary' size='small' icon={<SearchOutlined />} onClick={() => {
                  investigateFinding(finding)
                  setInsightsOpen(false)
                }}>
                  Investigate
                </Button>
              </section>
            ))}
          </Space>
        ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description='No advices for the selected range' />}
      </Drawer>

      <Drawer
        title="Diagnostics report"
        open={reportOpen}
        onClose={() => setReportOpen(false)}
        width='min(1180px, calc(100vw - 24px))'
        extra={(
          <Tooltip title="Download the complete report as JSON">
            <Button icon={<DownloadOutlined />} disabled={!reportPayload} onClick={downloadReport}>
              Download JSON
            </Button>
          </Tooltip>
        )}
      >
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Space wrap>
            <DatePicker.RangePicker
              aria-label='Report time range'
              showTime={{ format: 'HH:mm' }}
              format='MM-DD-YYYY HH:mm'
              value={[dayjs.unix(reportQueryRange.from), dayjs.unix(reportQueryRange.to)]}
              onChange={(values) => {
                if (!values?.[0] || !values[1]) return
                setReportRange({ from: values[0].unix(), to: values[1].unix() })
                setReport(null)
              }}
            />
            <Button loading={reportLoading} onClick={() => void loadReport()}>Refresh</Button>
          </Space>
          {reportError ? <Alert type="error" showIcon message={reportError} /> : null}
          {reportLoading && !report ? <Empty description="Generating report" /> : null}
          {!report && !reportLoading && !reportError ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="Generating report for the selected range" />
          ) : null}
          {reportPayload ? (
            <>
              <>
                <div className='diagnostics-overview-summary-strip diagnostics-report-overview-strip'>
                  <span className='diagnostics-overview-inline-action'><strong>{reportEventOverview.total}</strong> events</span>
                  {[
                    ['critical', 'Critical', reportEventOverview.severityCounts.critical, COLORS.red],
                    ['error', 'Error', reportEventOverview.severityCounts.error, DIAGNOSTIC_ERROR_COLOR],
                    ['warning', 'Warning', reportEventOverview.severityCounts.warning, COLORS.yellow],
                    ['info', 'Info', reportEventOverview.severityCounts.info, COLORS.accent],
                  ].map(([severity, label, count, color]) => <span
                    className={`diagnostics-overview-inline-severity-item diagnostics-overview-inline-severity-item--${severity}`}
                    data-empty={count === 0 || undefined}
                    key={String(label)}
                  >
                    <i className='diagnostics-filter-chip-dot' style={{ background: String(color) }} /> <strong>{count}</strong> {label}
                  </span>)}
                  <span className='diagnostics-overview-inline-divider' />
                  <span className='diagnostics-overview-inline-scope diagnostics-overview-inline-alert'>
                    <BellOutlined /> <strong>{reportPayload.range_alerts.length}</strong> alerts
                  </span>
                  <span className='diagnostics-overview-inline-divider' />
                  <span className='diagnostics-overview-inline-scope'>
                    <BulbOutlined /> <strong>{reportFindings.length}</strong> advice items
                  </span>
                  <span className='diagnostics-overview-inline-divider' />
                  <span className='diagnostics-overview-inline-scope'>
                    <InfoCircleOutlined /> <strong>{reportEventOverview.categories.length}</strong> affected domain{reportEventOverview.categories.length === 1 ? '' : 's'}
                  </span>
                </div>
                <div className='diagnostics-report-overview-grid'>
                  <section className='diagnostics-overview-section diagnostics-overview-domains'>
                    <Text className='diagnostics-overview-section-title'>Events by domain ({reportDomainEventTotal})</Text>
                    <div className='diagnostics-domain-distribution'>
                      {reportOverviewDomains.map((domain) => {
                        const percentage = reportDomainEventTotal ? domain.count / reportDomainEventTotal * 100 : 0
                        return <div className='diagnostics-domain-distribution-row' data-empty={domain.count === 0 || undefined} key={domain.label}>
                          <span className='diagnostics-domain-distribution-label'>{domain.label}</span>
                          <span className='diagnostics-domain-distribution-track'>
                            <span className='diagnostics-domain-distribution-fill' style={{ width: `${percentage}%` }}>
                              {[
                                ['info', COLORS.accent], ['warning', COLORS.yellow], ['error', DIAGNOSTIC_ERROR_COLOR], ['critical', COLORS.red],
                              ].map(([severity, color]) => {
                                const count = domain.severityCounts[severity as keyof typeof domain.severityCounts]
                                return count ? <span className='diagnostics-domain-distribution-severity' key={severity} style={{ width: `${count / domain.count * 100}%`, background: color }} /> : null
                              })}
                            </span>
                          </span>
                          <span className='diagnostics-domain-distribution-value'>{domain.count} ({percentage.toFixed(1)}%)</span>
                        </div>
                      })}
                    </div>
                  </section>
                  <section className='diagnostics-overview-section diagnostics-overview-utilization'>
                    <Text className='diagnostics-overview-section-title'>Resource average utilization (%)</Text>
                    {reportAverageResourceUtilization.length ? <div className='diagnostics-utilization-rows'>
                      {reportAverageResourceUtilization.map((resource) => <div className='diagnostics-utilization-row' key={resource.label}>
                        <span className='diagnostics-utilization-label'>{resource.label}</span>
                        <span className='diagnostics-utilization-track'>
                          <span className='diagnostics-utilization-average' style={{ width: `${Math.min(100, resource.value)}%`, background: resource.color }} />
                          <span className='diagnostics-utilization-peak' style={{ left: `${Math.min(100, resource.peak)}%`, borderColor: resource.color }} />
                        </span>
                        <span className='diagnostics-utilization-value'>{resource.value.toFixed(1)}% <span>(Peak {resource.peak.toFixed(1)}%)</span></span>
                      </div>)}
                    </div> : <Text type='secondary'>No resource samples in this range</Text>}
                    <Text className='diagnostics-utilization-caption' type='secondary'>Solid: average · marker: peak · {reportResourceSampleCount} samples</Text>
                  </section>
                </div>
              </>

              <section className='diagnostics-report-findings'>
                <div className='diagnostics-report-findings-heading'>
                  <div>
                    <Text strong>Actionable findings</Text>
                    <Text type='secondary'>Alerts and recommended next steps for this report range</Text>
                    <Text className='diagnostics-report-findings-range' type='secondary'>
                      {dayjs.unix(reportQueryRange.from).format('MMM D, YYYY HH:mm')} - {dayjs.unix(reportQueryRange.to).format('MMM D, YYYY HH:mm')}
                    </Text>
                  </div>
                  <Space size={12} wrap>
                    <Text className='diagnostics-report-finding-count diagnostics-overview-inline-alert'><BellOutlined /> {reportPayload.range_alerts.length} alert{reportPayload.range_alerts.length === 1 ? '' : 's'}</Text>
                    <Text className='diagnostics-report-finding-count'><BulbOutlined /> {reportFindings.length} advice item{reportFindings.length === 1 ? '' : 's'}</Text>
                  </Space>
                </div>
                <div className='diagnostics-report-findings-grid'>
                  <section className='diagnostics-report-finding-panel diagnostics-report-alert-panel'>
                    <div className='diagnostics-report-finding-panel-heading'>
                      <Text strong>Alert activity</Text>
                      <Text type='secondary'>Currently active: {reportPayload.active_alerts.length}</Text>
                    </div>
                    {reportPayload.alert_summary.length ? (
                      <Table
                        size="small"
                        rowKey="dedup_key"
                        pagination={false}
                        dataSource={reportPayload.alert_summary}
                        columns={[
                          { title: 'Severity', width: 110, render: (_: unknown, alert: DiagAlert) => severityTag(alert.severity) },
                          { title: 'Alert', render: (_: unknown, alert: DiagAlert) => alert.summary || alert.event_type },
                          { title: 'Observed', width: 146, render: (_: unknown, alert: DiagAlert) => <span className='diagnostics-report-alert-time'>First {formatActivityTime(alert.first_fired_at)}<br />Latest {formatActivityTime(alert.last_fired_at)}</span> },
                          { title: 'Count', dataIndex: 'fire_count', width: 76, align: 'right' as const },
                        ]}
                      />
                    ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No alert activity in the selected range" />}
                  </section>
                  <section className='diagnostics-report-finding-panel'>
                    <div className='diagnostics-report-finding-panel-heading'>
                      <Text strong>Advice items</Text>
                      <Text type='secondary'>Derived from this report range</Text>
                    </div>
                    {reportFindings.length ? (
                      <Space direction='vertical' size={6} style={{ width: '100%' }}>
                        {reportFindings.slice(0, 5).map((advice) => (
                          <section className='diagnostics-report-advice' key={advice.id}>
                            <Text strong>{String(advice.severity).toUpperCase()} · {advice.title}</Text>
                            <Text className='diagnostics-report-advice-time' type='secondary'>{reportAdviceTimeLabel(advice)}</Text>
                            <Text className='diagnostics-drawer-list-item'>{advice.observation}</Text>
                            {advice.recommendations?.[0] ? <Text className='diagnostics-drawer-list-item'><strong>Recommended next step:</strong> {advice.recommendations[0]}</Text> : null}
                            {advice.validation_steps?.[0] ? <Text className='diagnostics-drawer-list-item'><strong>Verify:</strong> {advice.validation_steps[0]}</Text> : null}
                          </section>
                        ))}
                      </Space>
                    ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description='No advice in range' />}
                  </section>
                </div>
              </section>

              <div>
                <Text strong>Event activity</Text>
                <Text className='diagnostics-drawer-list-item' type='secondary'>
                  {reportDisplayedEvents.length} events, {reportTimeline.points.length} timeline points.
                </Text>
                <div className='diagnostics-report-timeline-legend' aria-label='Event severity legend'>
                  <span>Severity</span>
                  {[
                    ['Info', COLORS.accent],
                    ['Warning', COLORS.yellow],
                    ['Error', DIAGNOSTIC_ERROR_COLOR],
                    ['Critical', COLORS.red],
                  ].map(([label, color]) => <span key={String(label)}><i style={{ background: String(color) }} />{label}</span>)}
                </div>
                <div className='diagnostics-report-timeline'>
                  <ResponsiveContainer width='100%' height='100%'>
                    <ScatterChart margin={{ top: 12, right: 12, left: EVENT_TIMELINE_LEFT_GUTTER, bottom: 0 }}>
                      <CartesianGrid stroke={`${COLORS.border}99`} strokeDasharray='3 3' />
                      <XAxis type='number' dataKey='x' domain={[reportQueryRange.from, reportQueryRange.to]} tickFormatter={reportEventActivityTick} tick={{ fill: COLORS.textMuted, fontSize: 10 }} minTickGap={36} />
                      <YAxis type='number' dataKey='y' domain={[-0.5, Math.max(0.5, reportTimeline.lanes.length - 0.5)]} ticks={reportTimeline.lanes.map((_, index) => index)} tickFormatter={(index) => reportTimeline.lanes[index] || ''} tick={{ fill: COLORS.textMuted, fontSize: 10 }} width={EVENT_TIMELINE_LABEL_WIDTH} reversed />
                      <RechartsTooltip content={eventTimelineTooltip} allowEscapeViewBox={{ y: true }} wrapperStyle={{ zIndex: 30 }} />
                      <Scatter
                        data={reportTimeline.points}
                        name='Events'
                        shape={(shapeProps: unknown) => {
                          const props = shapeProps as { payload?: unknown; cx?: number; cy?: number }
                          const point = props.payload as EventTimelinePoint | undefined
                          if (!point || typeof props.cx !== 'number' || typeof props.cy !== 'number') return <g />
                          return <circle cx={props.cx} cy={props.cy} r={4} fill={severityColors[normalizedSeverity(point.item.event.severity)]} />
                        }}
                      />
                    </ScatterChart>
                  </ResponsiveContainer>
                </div>
              </div>

              <div className='diagnostics-report-resource-trends'>
                <Text strong>Resource trends</Text>
                <Text className='diagnostics-drawer-list-item' type='secondary'>Selected report range</Text>
                <div className='diagnostics-resource-lanes-legend diagnostics-report-resource-legend'>
                  <button type='button' className='diagnostics-resource-lane-toggle' data-active={reportResourceTrendSeries.length === reportAvailableResourceTrendLabels.length || undefined} onClick={() => setReportResourceTrendLabels(new Set(reportAvailableResourceTrendLabels))}>All</button>
                  {reportAvailableResourceTrendLabels.map((label, index) => {
                    const active = reportResourceTrendLabels.has(label)
                    return <button
                      key={label}
                      type='button'
                      className='diagnostics-resource-lane-toggle'
                      data-active={active || undefined}
                      onClick={() => setReportResourceTrendLabels((selected) => {
                        const next = new Set(selected)
                        if (next.has(label)) next.delete(label)
                        else next.add(label)
                        return next
                      })}
                    >
                      <i style={{ borderTopColor: active ? resourceUtilizationColor(label) : COLORS.textMuted, borderTopStyle: index >= 3 ? 'dashed' : 'solid' }} />
                      {active ? '' : '+ '}{label}
                    </button>
                  })}
                </div>
                {reportResourceTrendSeries.length ? <div className='diagnostics-resource-lanes-chart'>
                  <ResponsiveContainer width='100%' height='100%'>
                    <LineChart data={reportResourceTrend} margin={{ top: 8, right: 16, bottom: 0, left: EVENT_TIMELINE_PLOT_LEFT - 48 }}>
                      <CartesianGrid stroke={`${COLORS.border}99`} strokeDasharray='3 3' vertical={false} />
                      <XAxis type='number' dataKey='ts_epoch' domain={[reportQueryRange.from, reportQueryRange.to]} tickFormatter={reportEventActivityTick} tick={{ fill: COLORS.textMuted, fontSize: 11 }} minTickGap={36} />
                      <YAxis type='number' domain={[0, 100]} ticks={[0, 50, 100]} width={48} tick={{ fill: COLORS.textMuted, fontSize: 11 }} tickFormatter={(value) => `${value}%`} label={{ value: 'Utilization', angle: -90, position: 'insideLeft', fill: COLORS.textMuted, fontSize: 11 }} />
                      <RechartsTooltip formatter={(value: number) => `${value.toFixed(1)}%`} labelFormatter={(timestamp) => dayjs.unix(Number(timestamp)).format('YYYY-MM-DD HH:mm:ss')} contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text }} cursor={{ stroke: COLORS.accent, strokeWidth: 1, strokeDasharray: '4 2' }} allowEscapeViewBox={{ x: true, y: true }} wrapperStyle={{ zIndex: 30 }} />
                      {reportDisplayedEvents.map((item) => <ReferenceLine key={`report-event-${displayEventKey(item)}`} x={dayjs(item.event.ts_utc).unix()} stroke={COLORS.textMuted} strokeOpacity={0.45} strokeDasharray='3 3' />)}
                      {reportResourceTrendSeries.map((label, index) => <Line key={label} name={label} type='monotone' dataKey={`values.${label}`} stroke={resourceUtilizationColor(label)} strokeDasharray={index >= 3 ? '5 3' : undefined} strokeWidth={2} dot={false} connectNulls isAnimationActive={false} />)}
                    </LineChart>
                  </ResponsiveContainer>
                </div> : <Text className='diagnostics-resource-lanes-empty' type='secondary'>{reportAvailableResourceTrendLabels.length ? 'Select a resource to display its trend' : 'No resource samples in the selected report range'}</Text>}
              </div>

              <div>
                <Text strong>Configuration changes in range</Text>
                {reportPayload.config_changes.length ? (
                  <Space direction='vertical' size={8} style={{ width: '100%', marginTop: 6 }}>
                    {reportPayload.config_changes.map((revision, revisionIndex) => {
                      const entries = configChangeEntries(revision.change_summary)
                      return <section className='diagnostics-report-config-change' key={String(revision.revision_id || revisionIndex)}>
                        <Text type='secondary'>
                          {revision.created_at ? dayjs(String(revision.created_at)).format('MMM D, YYYY HH:mm:ss') : 'Recorded configuration change'}
                        </Text>
                        {entries.map((entry, entryIndex) => (
                          <Text className='diagnostics-drawer-list-item' key={`${entry.scope}-${entry.field}-${entryIndex}`}>
                            {entry.scope} · {entry.change} · {entry.field}: {formatDiagnosticValue(entry.previous)} → {formatDiagnosticValue(entry.current)}
                          </Text>
                        ))}
                      </section>
                    })}
                  </Space>
                ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description='No configuration changes in range' />}
              </div>

              <Text type="secondary">Benchmark regressions: {reportPayload.benchmark_regressions.length}</Text>
              <Text type="secondary">Generated {dayjs(reportPayload.generated_at).format('MMM D, YYYY HH:mm:ss')}</Text>
            </>
          ) : null}
        </Space>
      </Drawer>

      <Drawer
        title={`Alert history · ${alertFilterLabel}${displayedAlerts.length ? ` (${displayedAlerts.length})` : ''}`}
        open={alertsOpen}
        onClose={() => setAlertsOpen(false)}
        width={560}
      >
        <Collapse
          className='diagnostics-logs diagnostics-alert-policy'
          items={[
            {
              key: 'alert-policy',
              label: 'Alert policy (8 event types)',
              children: (
                <Space direction='vertical' size={8} style={{ width: '100%' }}>
                  <Text type='secondary'>Only these event types create Alerts. Other events, including custom rules, remain diagnostic evidence even when their Severity is error or critical.</Text>
                  {ALERT_POLICY_SUMMARIES.map((policy) => (
                    <div key={policy.eventType}>
                      <Space size={6} wrap>
                        <Tag color={policy.severity === 'critical' ? COLORS.red : DIAGNOSTIC_ERROR_COLOR}>{policy.severity.toUpperCase()}</Tag>
                        <Text code>{policy.eventType}</Text>
                      </Space>
                      <Text className='diagnostics-drawer-list-item' type='secondary'>{policy.behavior}</Text>
                    </div>
                  ))}
                  <Text type='secondary'>Repeated observations with the same event type and scope are merged. Notifications for the same Alert are cooled down for 5 minutes.</Text>
                </Space>
              ),
            },
          ]}
          style={{ marginBottom: 16 }}
        />
        <Segmented
          className='diagnostics-alert-filter'
          block
          value={alertFilter}
          onChange={(value) => setAlertFilter(value as typeof alertFilter)}
          options={[
            { label: 'In range', value: 'range' },
            { label: 'Current + resolved', value: 'all' },
            { label: 'Needs attention', value: 'needs_attention' },
            { label: 'Acknowledged', value: 'acknowledged' },
            { label: 'Silenced', value: 'silenced' },
            { label: 'Resolved', value: 'resolved' },
          ]}
          style={{ marginBottom: 16 }}
        />
        {displayedAlerts.length ? (
          <Space direction='vertical' size={16} style={{ width: '100%' }}>
            {displayedAlerts.map((alert) => (
              <section className='diagnostics-insight-drawer-item' key={alert.dedup_key}>
                <Space size={6} wrap>
                  {severityTag(alert.severity)}
                  <Text strong>{alert.summary || alert.event_type}</Text>
                </Space>
                <Text type='secondary'>First observed {formatActivityTime(alert.first_fired_at)} · Latest {formatActivityTime(alert.last_fired_at)}</Text>
                <Text type='secondary'>Observed {alert.fire_count} time{alert.fire_count === 1 ? '' : 's'} · Scope {alert.scope || '-'}</Text>
                <Text type='secondary'>{alert.status === 'active' ? 'Current' : `Resolved ${formatActivityTime(alert.resolved_at)}`}</Text>
                {alert.acknowledged_at ? <Tag color={COLORS.green}>Acknowledged</Tag> : null}
                {alert.silenced_until ? <Tag color={COLORS.yellow}>Silenced until {formatActivityTime(alert.silenced_until)}</Tag> : null}
                {alert.status === 'active' ? (
                  <Space size={8} wrap>
                    <Button
                      size='small'
                      disabled={!!alert.acknowledged_at}
                      loading={alertActionKey === alert.dedup_key}
                      onClick={() => void acknowledgeAlert(alert)}
                    >
                      {alert.acknowledged_at ? 'Acknowledged' : 'Acknowledge'}
                    </Button>
                    <Dropdown
                      trigger={['click']}
                      disabled={alertActionKey === alert.dedup_key}
                      menu={{
                        items: [
                          { key: '30', label: '30 minutes' },
                          { key: '120', label: '2 hours' },
                        ],
                        onClick: ({ key }) => void silenceAlert(alert, Number(key) as 30 | 120),
                      }}
                    >
                      <Button size='small' loading={alertActionKey === alert.dedup_key}>Silence</Button>
                    </Dropdown>
                  </Space>
                ) : null}
                {alert.last_event_id ? (
                  <Button type='primary' size='small' icon={<SearchOutlined />} onClick={() => {
                    void investigateAlert(alert)
                    setAlertsOpen(false)
                  }}>
                    Investigate
                  </Button>
                ) : null}
              </section>
            ))}
          </Space>
        ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={alertFilter === 'range' || alertFilter === 'resolved' ? 'No alerts in the selected range' : alertFilter === 'needs_attention' ? 'No alerts need attention' : `No ${alertFilterLabel.toLowerCase()} alerts`} />}
      </Drawer>

      <Drawer title='Event Detail' open={!!selectedEvent} onClose={() => setSelectedEvent(null)} width={700}>
        {selectedEvent ? (() => {
          // Read through the representative event, but describe the whole batch:
          // Type/Summary/Resource-control cover every resource in selectedEvent.resources
          // so the detail stays consistent with the merged list row that opened it.
          const detailEvent = selectedEvent.event
          return (
          <Space direction='vertical' size={10} style={{ width: '100%' }}>
            <Space wrap>
              {severityTag(detailEvent.severity)}
            </Space>
            <Text strong>Detected: {dayjs(detailEvent.ts_utc).format('YYYY-MM-DD HH:mm:ss')}</Text>
            <Text strong>Category: {categoryLabel(detailEvent.category)}</Text>
            <Text strong>Type: {displayEventTypes(selectedEvent)}</Text>
            <Text strong>Source: {sourceBucket(detailEvent.source).label}{detailEvent.source ? ` (${detailEvent.source})` : ''}</Text>
            <Text strong>Summary:</Text>
            <Text>{displayEventSummary(selectedEvent)}</Text>
            {(() => {
              const controlDetails = controlDetailsFromEvent(detailEvent, selectedEvent.resources)
              return controlDetails.length ? (
                <>
                  <Text strong>Resource control:</Text>
                  <Table
                    className='diagnostics-control-details-table'
                    columns={[
                      { title: 'Resource', dataIndex: 'resource', width: 150 },
                      { title: 'Limit', dataIndex: 'limit' },
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
              {detailEvent.app_id ? <Tag className='diagnostics-related-tag' onClick={() => { setAppId(detailEvent.app_id || undefined); setSelectedEvent(null) }}>app: {detailEvent.app_id}</Tag> : null}
              {detailEvent.job_id ? <Tag className='diagnostics-related-tag' color='purple' onClick={() => { setJobId(detailEvent.job_id || undefined); setSelectedEvent(null) }}>job: {detailEvent.job_id}</Tag> : null}
              {!detailEvent.app_id && !detailEvent.job_id ? <Text type='secondary'>-</Text> : null}
            </Space>
            {(() => {
              const rows = attributeRows(detailEvent)
              return rows.length ? (
                <>
                  <Text strong>Details:</Text>
                  <Table
                    className='diagnostics-control-details-table'
                    columns={[
                      { title: 'Attribute', dataIndex: 'label', width: 150 },
                      { title: 'Value', dataIndex: 'value', render: (value: string) => (
                        <span style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{value}</span>
                      ) },
                    ]}
                    dataSource={rows}
                    pagination={false}
                    rowKey='key'
                    size='small'
                  />
                </>
              ) : null
            })()}
            {(() => {
              const procRows = scopeProcessRows(detailEvent)
              return procRows.length ? (
                <>
                  <Text strong>Processes:</Text>
                  <Table
                    className='diagnostics-control-details-table'
                    columns={[
                      { title: 'PID', dataIndex: 'pid', width: 88 },
                      { title: 'Process', dataIndex: 'processName', width: 130 },
                      { title: 'Command', dataIndex: 'cmdline', render: (value: string) => (
                        <span style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{value || '-'}</span>
                      ) },
                    ]}
                    dataSource={procRows}
                    pagination={false}
                    rowKey={(row: ScopeProcessRow) => `${row.scope}:${row.pid}`}
                    size='small'
                  />
                </>
              ) : null
            })()}
          </Space>
          )
        })() : null}
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
                    <div className='diagnostics-evidence-item diagnostics-evidence-item-interactive' key={item.event.event_id} onClick={() => setSelectedEvent(item)}>
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
                    <div className='diagnostics-evidence-item diagnostics-evidence-item-interactive' key={ev.event_id} onClick={() => setSelectedEvent({ event: ev, resources: ev.resource_type ? [ev.resource_type] : [] })}>
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
              <Text className='diagnostics-investigation-heading' strong>Resource pressure (event window)</Text>
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

            <BenchmarkResultsSection bench={contextData.metrics.benchmark} />

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
