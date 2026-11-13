import React, { useState, useCallback, useEffect, useRef, useMemo } from 'react'
import {
  Row,
  Col,
  Card,
  Table,
  Tag,
  Button,
  Select,
  Input,
  Typography,
  Alert,
  Space,
  Tooltip,
  Modal,
  message,
  Checkbox,
  InputNumber,
  Popconfirm,
  Tabs,
  Drawer,
  Descriptions,
  Radio,
  Spin,
  Divider,
} from 'antd'
import {
  PlusOutlined,
  DeleteOutlined,
  DeleteFilled,
  ThunderboltOutlined,
  CloseOutlined,
  HeartOutlined,
  DatabaseOutlined,
  ReloadOutlined,
  QuestionCircleOutlined,
  InfoCircleOutlined,
  SearchOutlined,
  RightOutlined,
  DownOutlined,
  RollbackOutlined,
  LockOutlined,
  EditOutlined,
  ControlOutlined,
  PauseCircleOutlined,
  ClusterOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { COLORS } from '../styles/theme'
import { MEMORY_RECOVERY_HINT, FREEZE_WHY, FREEZE_RISKS } from '../utils/messages'
import { api } from '../api/client'

// '0-1,4' -> [0, 1, 4]
function expandCpuList(spec: string): number[] {
  return spec.split(',').flatMap((part) => {
    const [a, b] = part.split('-').map(Number)
    if (!Number.isFinite(a)) return []
    return Array.from({ length: (Number.isFinite(b) ? b : a) - a + 1 }, (_, i) => a + i)
  })
}

type AffinityChoice = AffinityDialogState['choice']

// What to preselect when the dialog opens: the app's actual current pinning when it has one,
// otherwise the last selection applied successfully (remembered by the server, so every open UI
// agrees), and only if it is still valid on this machine.
function initialAffinityChoice(
  classes: Record<string, string>, pinned: string[], last: { class: string; cpus: string } | null,
): { choice: AffinityChoice; selected: number[] } {
  const key = (cores: number[]) => [...new Set(cores)].sort((a, b) => a - b).join(',')
  if (pinned.length > 0) {
    const cores = pinned.flatMap(expandCpuList)
    const match = (['p', 'e', 'lpe'] as const).find(
      (c) => c in classes && pinned.length === 1 && key(expandCpuList(classes[c])) === key(cores))
    return match
      ? { choice: match, selected: [] }
      : { choice: 'custom', selected: [...new Set(cores)].sort((a, b) => a - b) }
  }
  if (!last) return { choice: '', selected: [] }
  const valid = new Set(Object.values(classes).flatMap(expandCpuList))
  if (last.class === 'custom') {
    const selected = expandCpuList(last.cpus).filter((c) => valid.has(c))
    return selected.length ? { choice: 'custom', selected } : { choice: '', selected: [] }
  }
  return last.class in classes ? { choice: last.class as AffinityChoice, selected: [] } : { choice: '', selected: [] }
}

// Distinct cores across the cpu lists an app is pinned to.
function pinnedCoreCount(lists: string[] | undefined): number {
  return new Set((lists ?? []).flatMap(expandCpuList)).size
}

// Same wording as the server's restore messages (controller/cpu.py NO_RESTRICTION).  Not "all
// cores": an unrestricted cgroup inherits its parent's cpus, and the parent may be confined.
const NO_RESTRICTION = 'no restriction'

// How one cgroup is confined right now.  A restriction SmarTune did not write came from outside
// (an admin, systemd AllowedCPUs), so it is labelled as such.
function affinityNow(c: CpuAffinityCgroup): string {
  if (c.bound) return `CPU ${c.cpus ?? c.configured}`
  return c.configured ? `CPU ${c.configured} (set outside SmarTune)` : NO_RESTRICTION
}

// What Restore leaves the cgroup with: Restore undoes SmarTune's pinning only, so it goes back
// to the value seen before SmarTune pinned it, and a cgroup SmarTune never pinned is untouched.
function affinityAfterRestore(c: CpuAffinityCgroup): string {
  if (c.bound) return c.original ? `CPU ${c.original} (its value before SmarTune)` : NO_RESTRICTION
  return c.configured ? `CPU ${c.configured} (set outside SmarTune, unchanged)` : NO_RESTRICTION
}

// One text when every cgroup agrees (an app without cgroups counts as unrestricted),
// otherwise one row per cgroup -- an app's cgroups can have different original values.
function describeCgroups(
  cgroups: CpuAffinityCgroup[], describe: (c: CpuAffinityCgroup) => string,
): { uniform: string | null; rows: { cgroup: string; text: string }[] } {
  const rows = cgroups.map((c) => ({ cgroup: c.cgroup, text: describe(c) }))
  const distinct = new Set(rows.map((r) => r.text))
  if (distinct.size === 0) return { uniform: NO_RESTRICTION, rows }
  return { uniform: distinct.size === 1 ? rows[0].text : null, rows }
}

function AffinityCurrent({ cgroups }: { cgroups: CpuAffinityCgroup[] }) {
  const { uniform, rows } = describeCgroups(cgroups, affinityNow)
  const tag = (text: string) => (text === NO_RESTRICTION
    ? <Tag>not restricted</Tag>
    : <Tag color="blue">restricted to {text}</Tag>)
  if (uniform !== null) {
    return <Typography.Paragraph style={{ fontSize: 13 }}>Currently: {tag(uniform)}</Typography.Paragraph>
  }
  return (
    <Typography.Paragraph style={{ fontSize: 13 }}>
      Currently (per cgroup):
      {rows.map((r) => (
        <div key={r.cgroup} style={{ marginTop: 4 }}>
          <Typography.Text code>{r.cgroup}</Typography.Text> {tag(r.text)}
        </div>
      ))}
    </Typography.Paragraph>
  )
}

function RestorePreview({ cgroups }: { cgroups: CpuAffinityCgroup[] }) {
  const { uniform, rows } = describeCgroups(cgroups, affinityAfterRestore)
  const pinned = cgroups.some((c) => c.bound)
  const lead = pinned ? "Undo SmarTune's restriction" : 'Not pinned by SmarTune, nothing to undo'
  if (uniform === NO_RESTRICTION) return <>{lead}; the OS scheduler decides where the app runs.</>
  if (uniform !== null) return <>{lead}{pinned ? `: back to ${uniform}.` : ` (${uniform}).`}</>
  return (
    <>
      {lead}; each cgroup ends up with:
      {rows.map((r) => <div key={r.cgroup}>• {r.cgroup}: {r.text}</div>)}
    </>
  )
}

interface AffinityDialogState {
  app: AppInfo | null
  status: 'loading' | 'ready' | 'error'
  classes: Record<string, string>   // core classes present on this machine -> cpu list
  cgroups: CpuAffinityCgroup[]      // the app's cgroups this dialog acts on
  choice: '' | 'p' | 'e' | 'lpe' | 'custom' | 'restore'
  selected: number[]
}

const AFFINITY_DIALOG_CLOSED: AffinityDialogState = {
  app: null, status: 'loading', classes: {}, cgroups: [], choice: '', selected: [],
}

const AFFINITY_CLASS_INFO = {
  p: { title: 'P cores', hint: 'Performance cores: fastest per core, highest power. Best for heavy or latency-sensitive work.' },
  e: { title: 'E cores', hint: 'Efficient cores: slower but power-efficient. Good for background work.' },
  lpe: { title: 'LPE cores', hint: 'Low-power efficient cores on the SoC tile (no L3 cache): lowest power and speed. Keeps P cores free for other apps.' },
} as const
import type {
  AppInfo,
  AutoLimitedApp,
  AutoLimitedAppsData,
  AutoLimitExclusion,
  ControlStatus,
  CpuAffinityCgroup,
  ResourceLimitProfileData,
  PassiveControlData,
  ProcessStatusRow,
} from '../api/types'
import { useAppEvents } from '../hooks/useAppEvents'
import { AddAppWizard } from './AddAppWizard'
import { EditAppProcessesModal } from './EditAppProcessesModal'
import { useIsCompactWorkspace } from '../hooks/useWorkspaceWidth'

const { Text } = Typography
const { Option } = Select

const APP_STATUS = {
  RUNNING: 'running',
  STOPPED: 'stopped',
  LIMITED: 'limited',
  A_LIMITED: 'a_limited',
  PENDING: 'pending',
  NA: 'NA',
} as const

// Lists a background refresh can target; see queueRefresh in Balance.
type RefreshKey = 'controlled' | 'autoLimited' | 'pending'
// Long enough to fold the burst of events one action emits into one request.
const REFRESH_COALESCE_MS = 300
// While the tab is open the controlled list is re-fetched once it is this old (see
// the effect using it); checked every CONTROLLED_POLL_CHECK_MS, without a request.
const CONTROLLED_POLL_MS = 3000
const CONTROLLED_POLL_CHECK_MS = 500

interface Props {
  active: boolean
  // false = monitor-only server: the balancer is not available, so this tab
  // renders a notice and performs no balancer calls.
  balancerEnabled?: boolean
  // Set by the Processes tab's "Add to balancer"; opens the wizard pre-filled.
  registerKeyword?: string | null
  onRegisterConsumed?: () => void
}

interface LimitDialogState {
  app: AppInfo | null
  open: boolean
  submitting: boolean
  loadingProfile: boolean
}

interface LimitFormValues {
  applyResourceLimit: boolean
  networkPriority: string
  cpuEnabled: boolean
  cpuPercent: number
  cpuMin: number
  cpuMax: number
  cpuOptions: number[]
  memEnabled: boolean
  memPercent: number
  memMin: number
  memMax: number
  memOptions: number[]
  diskEnabled: boolean
  diskDetected: boolean
  writeMbps: number
  writeMbpsMax: number
  readMbps: number
  readMbpsMax: number
  writeIops: number
  writeIopsMax: number
  readIops: number
  readIopsMax: number
  processNames: string[]
  cgroupIds: string[]
  targetProcesses: Array<{ pid: number; name: string }>
}

const PRIORITY_OPTIONS = [
  { value: 'low', label: 'Low', color: COLORS.green },
  { value: 'medium', label: 'Medium', color: COLORS.yellow },
  { value: 'high', label: 'High', color: COLORS.orange },
  { value: 'critical', label: 'Critical', color: COLORS.red },
]

const NETWORK_PRIORITY_COLORS: Record<'low' | 'high' | 'critical', string> = {
  low: COLORS.green,
  high: COLORS.orange,
  critical: COLORS.red,
}

const NETWORK_PRIORITY_OPTIONS = [
  { value: 'low', label: 'Low', color: NETWORK_PRIORITY_COLORS.low },
  { value: 'high', label: 'High', color: NETWORK_PRIORITY_COLORS.high },
  { value: 'critical', label: 'Critical', color: NETWORK_PRIORITY_COLORS.critical },
]

type NetworkClassKey = 'critical' | 'high' | 'low' | 'system'

const NETWORK_CLASS_ORDER: NetworkClassKey[] = ['critical', 'high', 'low', 'system']

const DEFAULT_NETWORK_BW_RANGES: Record<NetworkClassKey, { min: number; max: number }> = {
  critical: { min: 0.6, max: 0.9 },
  high: { min: 0.3, max: 0.8 },
  low: { min: 0.1, max: 0.3 },
  system: { min: 0.05, max: 0.1 },
}

function normalizeNetworkPriority(value?: string): string {
  const normalized = (value ?? '').toLowerCase()
  if (normalized === 'medium') return 'low'
  const allowed = new Set(NETWORK_PRIORITY_OPTIONS.map((opt) => opt.value))
  return allowed.has(normalized) ? normalized : 'low'
}

function sanitizeNetworkBandwidthRanges(
  raw?: Record<string, { min?: number; max?: number }>
): Record<NetworkClassKey, { min: number; max: number }> {
  const next = { ...DEFAULT_NETWORK_BW_RANGES }
  if (!raw) return next

  for (const key of NETWORK_CLASS_ORDER) {
    const item = raw[key]
    if (!item) continue
    const min = Number(item.min)
    const max = Number(item.max)
    if (Number.isFinite(min) && min >= 0) next[key].min = min
    if (Number.isFinite(max) && max >= 0) next[key].max = max
  }
  return next
}

function formatPercentNumber(value: number): string {
  return (value * 100).toFixed(0)
}

function priorityColor(p?: string): string {
  switch (p?.toLowerCase()) {
    case 'low': return COLORS.green
    case 'medium': return COLORS.yellow
    case 'high': return COLORS.orange
    case 'critical': return COLORS.red
    default: return COLORS.textMuted
  }
}

function networkPriorityColor(p?: string): string {
  const key = (p ?? '').toLowerCase() as 'low' | 'high' | 'critical'
  return NETWORK_PRIORITY_COLORS[key] ?? COLORS.textMuted
}

function normalizePercentOptions(options: number[] | undefined, fallback: number): number[] {
  const base = [...(options ?? []), fallback]
    .map((v) => Number(v))
    .filter((v) => Number.isFinite(v) && v > 0)
  return Array.from(new Set(base)).sort((a, b) => a - b)
}

function PriorityTag({ priority }: { priority?: string }) {
  const color = priorityColor(priority)
  return (
    <Tag
      style={{
        color,
        borderColor: color,
        background: `${color}18`,
        fontSize: 11,
        fontWeight: 600,
        textTransform: 'uppercase',
      }}
    >
      {priority ?? 'N/A'}
    </Tag>
  )
}

function runtimeHintTag(runtime?: string) {
  switch (runtime) {
    case 'Running':
      return <Tag color="success" style={{ marginInlineEnd: 0 }}>Running</Tag>
    case 'Pending':
      return <Tag color="processing" style={{ marginInlineEnd: 0 }}>Pending</Tag>
    case 'Stopped':
      return <Tag color="default" style={{ marginInlineEnd: 0 }}>Stopped</Tag>
    default:
      return <Tag color="default" style={{ marginInlineEnd: 0 }}>-</Tag>
  }
}

function deriveCombinedStatus(record: AppInfo): {
  runtime: 'Running' | 'Stopped' | 'Pending'
  limitSummary: 'Limited' | 'Partial Limited' | 'Not Limited' | 'N/A'
} {
  const isPending = record.status === APP_STATUS.PENDING || record.runtime_hint === 'Pending'
  if (isPending) {
    return { runtime: 'Pending', limitSummary: 'N/A' }
  }

  const summary = record.app_summary_status
    ?? ((record.status === APP_STATUS.LIMITED || record.status === APP_STATUS.A_LIMITED)
      ? 'Limited'
      : record.status === APP_STATUS.RUNNING
        ? 'Not Limited'
        : 'No Running Process')

  if (summary === 'No Running Process') {
    return { runtime: 'Stopped', limitSummary: 'N/A' }
  }

  return {
    runtime: 'Running',
    limitSummary: summary === 'Limited' || summary === 'Partial Limited' || summary === 'Not Limited'
      ? summary
      : 'N/A',
  }
}

function limitSummaryTag(summary: 'Limited' | 'Partial Limited' | 'Not Limited' | 'N/A') {
  switch (summary) {
    case 'Limited':
      return <Tag color="warning" style={{ marginInlineEnd: 0 }}>Limited</Tag>
    case 'Partial Limited':
      return <Tag color="gold" style={{ marginInlineEnd: 0 }}>Partial Limited</Tag>
    case 'Not Limited':
      return <Tag color="success" style={{ marginInlineEnd: 0 }}>Not Limited</Tag>
    default:
      return <Tag color="default" style={{ marginInlineEnd: 0 }}>N/A</Tag>
  }
}

function formatPassiveControlTimestamp(ts: number | undefined | null): string {
  if (!ts) return 'unknown time'
  return new Date(ts * 1000).toLocaleString()
}

const EXCLUDED_APPS_TOOLTIP =
  'Apps restored by hand are never auto-limited under critical pressure. Remove one to make ' +
  'it eligible again. This list lives in memory only and is cleared when Smartune restarts.'

// The "⛔ Excluded" tab body: apps the user hand-restored from an auto limit, which the
// pressure loop leaves alone for the rest of this service run. Manual-limit exemptions are
// intentionally NOT shown here — they already appear under Manual Control, so listing them
// again would double-count the same app. Presentational only; the parent owns the data and
// the remove call so the tab count stays in sync.
function ExcludedAppsTable({
  rows,
  loading,
  onRemove,
}: {
  rows: AutoLimitExclusion[]
  loading: boolean
  onRemove: (row: AutoLimitExclusion) => Promise<void>
}) {
  const compactWorkspace = useIsCompactWorkspace()
  const [removing, setRemoving] = useState<Record<string, boolean>>({})

  const remove = async (row: AutoLimitExclusion) => {
    setRemoving((prev) => ({ ...prev, [row.key]: true }))
    try {
      await onRemove(row)
    } finally {
      setRemoving((prev) => ({ ...prev, [row.key]: false }))
    }
  }

  const columns: ColumnsType<AutoLimitExclusion> = [
    {
      title: 'App',
      dataIndex: 'app_name',
      key: 'app_name',
      width: 460,
      render: (name: string, row) => (
        <Tooltip title={row.cgroups?.length ? row.cgroups.join(', ') : row.app_id}>
          <Text>{name || row.app_id}</Text>
        </Tooltip>
      ),
    },
    {
      title: 'Scope',
      dataIndex: 'kind',
      key: 'kind',
      width: 150,
      render: (kind: AutoLimitExclusion['kind']) =>
        kind === 'app' ? (
          <Tooltip title="Controlled app: every instance of it is excluded.">
            <Tag color="blue">All instances</Tag>
          </Tooltip>
        ) : (
          <Tooltip title="Only this process instance (this cgroup) is excluded — other processes with the same name are still eligible.">
            <Tag>This instance</Tag>
          </Tooltip>
        ),
    },
    {
      title: 'Excluded At',
      dataIndex: 'excluded_at',
      key: 'excluded_at',
      width: 190,
      render: (ts: number) => <Text type="secondary">{formatPassiveControlTimestamp(ts)}</Text>,
    },
    {
      title: '',
      key: 'actions',
      width: 110,
      align: 'right' as const,
      render: (_: unknown, row) => (
        <Popconfirm
          title="Allow auto-limiting again?"
          description="The balancer may throttle this app the next time pressure reaches critical."
          okText="Remove"
          cancelText="Cancel"
          onConfirm={() => remove(row)}
        >
          <Button size="small" danger icon={<DeleteOutlined />} loading={removing[row.key]}>
            Remove
          </Button>
        </Popconfirm>
      ),
    },
  ]

  return (
    <div style={{ maxWidth: 960, padding: '10px 16px 12px' }}>
      <Table
        columns={columns}
        dataSource={rows.map((row) => ({ ...row, key: row.key }))}
        scroll={compactWorkspace ? { x: 'max-content' } : undefined}
        size="small"
        loading={loading}
        pagination={false}
        tableLayout="fixed"
        locale={{
          emptyText: (
            <div style={{ padding: 30, color: COLORS.textMuted, textAlign: 'center' }}>
              No apps are excluded from auto-limiting.
            </div>
          ),
        }}
      />
    </div>
  )
}

// Elapsed time, not a countdown: auto restore waits for pressure to ease and then runs
// in stages, so there is no deadline to count down to. sinceMs comes from
// limitedSinceRef, which the frontend owns (see below).
function formatElapsed(sinceMs: number | null | undefined, nowMs: number): string {
  if (!sinceMs) return '—'
  const total = Math.max(0, Math.floor((nowMs - sinceMs) / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

const AUTO_LIMIT_REASON_LABEL: Record<string, string> = {
  system_pressure: 'System pressure',
  disk_pressure: 'Disk I/O pressure',
}

const PRESSURE_LEVEL_TAG_COLOR: Record<string, string> = {
  critical: 'error',
  high: 'warning',
  medium: 'gold',
  low: 'success',
}

// Shared wording so the tooltip, the confirm prompt and the toast all promise the
// same thing about what Restore does.
// A row in the unified management table. Controlled apps come through as plain AppInfo;
// an app the pressure engine throttled but that was never taken under management is folded
// in as a synthetic row carrying its original AutoLimitedApp under __auto (so its actions
// can Take Control / Restore it). controlled === false marks the synthetic ones.
type ControlRow = AppInfo & { __auto?: AutoLimitedApp; key?: string }

const AUTO_LIMIT_EXCLUSION_HINT =
  'Restored apps stay excluded from auto-limiting until you clear them under ' +
  'Balancer → Application Control Center → Excluded (also cleared on Smartune restart).'

function deriveDisplayProcessName(row: ProcessStatusRow): string {
  const rawName = (row.process_name || '').trim()
  const cmdline = (row.cmdline || '').trim()
  if (!cmdline) return rawName || '-'

  // Strip common wrappers so we can show the actual target process/script.
  const withoutSudo = cmdline.replace(/^sudo\s+/, '')
  const tokens = withoutSudo.split(/\s+/).filter(Boolean)
  const executable = (tokens[0] || '').split('/').pop()?.toLowerCase() || ''
  if (/^(?:python\d*(?:\.\d+)?|node(?:js)?)$/.test(executable)) {
    const script = tokens.slice(1).find((token) => !token.startsWith('-'))?.split('/').pop()
    if (script) return script
  }

  if (/^(?:bash|sh|dash|zsh|fish)$/.test(executable)) {
    const scriptFlag = tokens.findIndex((token) => ['--init-file', '--rcfile', '--file'].includes(token))
    const flaggedScript = scriptFlag >= 0 ? tokens[scriptFlag + 1] : undefined
    const positionalScript = tokens.slice(1).find((token) => !token.startsWith('-'))
    const script = (flaggedScript || positionalScript)?.split('/').pop()
    if (script) return script
  }

  if (rawName && !['sudo', 'python', 'python2', 'python3', 'node', 'nodejs', 'bash', 'sh', 'dash', 'zsh', 'fish'].includes(rawName.toLowerCase())) {
    return rawName
  }

  const firstToken = withoutSudo.split(/\s+/)[0] || rawName
  return firstToken.split('/').pop() || firstToken || '-'
}

export default function Balance({
  active,
  balancerEnabled = true,
  registerKeyword,
  onRegisterConsumed,
}: Props) {
  const compactWorkspace = useIsCompactWorkspace()
  const [allApps, setAllApps] = useState<AppInfo[]>([])
  const [controlledApps, setControlledApps] = useState<AppInfo[]>([])
  const [pendingApps, setPendingApps] = useState<AppInfo[]>([])
  const [autoLimitedApps, setAutoLimitedApps] = useState<AutoLimitedApp[]>([])
  // Live pressure levels. Seeded by the auto-limited request on tab open, then kept
  // current by the server's 'pressure_level_changed' notification. Never polled.
  const [pressureLevels, setPressureLevels] = useState<{ sys: string; disk: string }>({
    sys: '',
    disk: '',
  })
  // Start time (ms) per row for the "Limited For" column, keyed by effective_app_id.
  // Stamped locally when a row first appears and never touched again while it lives, so
  // a re-limit cannot rewind the counter. A row that leaves loses its entry.
  const limitedSinceRef = useRef<Record<string, number>>({})
  // Drives the "Limited For" column only. Ticks locally so the elapsed time advances
  // without polling the server for it.
  const [nowMs, setNowMs] = useState(() => Date.now())
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [messageApi, contextHolder] = message.useMessage()

  const [wizardOpen, setWizardOpen] = useState(false)
  // Keyword the wizard was opened with from inside this tab.
  // Kept apart from the `registerKeyword` prop, which comes from the Processes tab.
  const [wizardKeyword, setWizardKeyword] = useState<string | null>(null)
  const [expandedProcessRows, setExpandedProcessRows] = useState<React.Key[]>([])
  const [selectedTargetCgroups, setSelectedTargetCgroups] = useState<Record<string, string[]>>({})
  // Status quick-filter over the unified management table. 'auto' = apps the pressure
  // engine currently holds; 'manual' = every other controlled app (normal + manual limit);
  // 'excluded' = apps hand-restored from an auto limit that the engine now leaves alone.
  const [controlTab, setControlTab] = useState<'auto' | 'manual' | 'excluded'>('manual')
  // Apps exempt from auto-limiting (see ExcludedAppsTable). Loaded with the rest of the tab
  // data; the "⛔ Excluded" tab shows only the user_restore subset.
  const [exclusions, setExclusions] = useState<AutoLimitExclusion[]>([])
  // The app whose control breakdown the right-side detail drawer is showing (null = closed).
  const [detailApp, setDetailApp] = useState<AppInfo | null>(null)
  // The controlled app whose process-identity editor is open (null = closed). Lets the
  // operator widen an app's name-based identity so it controls more processes/cgroups.
  const [editApp, setEditApp] = useState<AppInfo | null>(null)
  // Row briefly flashed after a duplicate-add attempt scrolls it into view.
  const [highlightAppId, setHighlightAppId] = useState<string | null>(null)
  // Opened from the Processes tab: pop the Add-App wizard pre-filled.
  useEffect(() => {
    if (registerKeyword) {
      setWizardKeyword(null)
      setWizardOpen(true)
    }
  }, [registerKeyword])

  const [networkControlEnabled, setNetworkControlEnabled] = useState(true)
  const [networkBandwidthRanges, setNetworkBandwidthRanges] = useState<Record<NetworkClassKey, { min: number; max: number }>>(
    DEFAULT_NETWORK_BW_RANGES
  )
  const [actionLoading, setActionLoading] = useState<Record<string, boolean>>({})
  // CPU affinity dialog. Nothing is offered until the machine's core layout has been read, so a
  // wrong placeholder list is never shown.
  const [affinityDialog, setAffinityDialog] = useState<AffinityDialogState>(AFFINITY_DIALOG_CLOSED)
  const [limitDialog, setLimitDialog] = useState<LimitDialogState>({
    app: null,
    open: false,
    submitting: false,
    loadingProfile: false,
  })
  const [resourceSectionExpanded, setResourceSectionExpanded] = useState(false)
  const [networkSectionExpanded, setNetworkSectionExpanded] = useState(false)
  const [limitForm, setLimitForm] = useState<LimitFormValues>({
    applyResourceLimit: true,
    networkPriority: 'low',
    cpuEnabled: true,
    cpuPercent: 30,
    cpuMin: 1,
    cpuMax: 100,
    cpuOptions: [30],
    memEnabled: true,
    memPercent: 10,
    memMin: 1,
    memMax: 100,
    memOptions: [10],
    diskEnabled: true,
    diskDetected: false,
    writeMbps: 50,
    writeMbpsMax: 50,
    readMbps: 60,
    readMbpsMax: 60,
    writeIops: 2200,
    writeIopsMax: 2200,
    readIops: 20000,
    readIopsMax: 20000,
    processNames: [],
    cgroupIds: [],
    targetProcesses: [],
  })

  // Store the rows + the pressure levels that came with them, and stamp any row we have
  // not seen before. The server's `limited_at` is not used: the balancer rewrites it on
  // every re-apply, which is why the column sat at 0 s. The cost of counting locally is
  // that a page reload restarts from zero.
  const applyAutoLimited = useCallback((data: AutoLimitedAppsData | null | undefined) => {
    // Tolerate the older backend, which returned a bare array: otherwise a service that
    // has not been restarted blanks the card and drops every accumulated base.
    const apps = Array.isArray(data)
      ? (data as AutoLimitedApp[])
      : data?.apps ?? []
    const now = Date.now()
    const bases: Record<string, number> = {}
    for (const app of apps) {
      bases[app.effective_app_id] = limitedSinceRef.current[app.effective_app_id] ?? now
    }
    limitedSinceRef.current = bases
    setAutoLimitedApps(apps)
    if (data && !Array.isArray(data)) {
      setPressureLevels({ sys: data.sys_pressure_level, disk: data.disk_pressure_level })
    }
  }, [])

  const applyControlled = useCallback((ctrl: AppInfo[]) => {
    setControlledApps(ctrl)
  }, [])

  // Background refreshes (SSE events, the controlled-list poll, expanding a row) are
  // queued rather than sent: one action often emits several events, each wanting the
  // same list, and the action's own fetchData() follows with everything. Queued keys
  // wait REFRESH_COALESCE_MS, are held while an action is in flight, and are dropped
  // when a direct fetch of the same list starts first.
  const queuedRefresh = useRef(new Set<RefreshKey>())
  const refreshTimer = useRef<number | null>(null)
  const actionsInFlight = useRef(0)
  const lastControlledFetch = useRef(0)

  // Narrow refreshes: each pulls exactly the slice an event can have changed.
  const refreshAutoLimited = useCallback(async () => {
    queuedRefresh.current.delete('autoLimited')
    try {
      applyAutoLimited(await api.getAutoLimitedApps())
    } catch (e: unknown) {
      console.error('[Balance] auto-limited refresh failed:', e)
    }
  }, [applyAutoLimited])

  const refreshControlled = useCallback(async () => {
    queuedRefresh.current.delete('controlled')
    lastControlledFetch.current = Date.now()
    try {
      applyControlled((await api.getControlledApps()) ?? [])
    } catch (e: unknown) {
      console.error('[Balance] controlled refresh failed:', e)
    }
  }, [applyControlled])

  const refreshPending = useCallback(async () => {
    queuedRefresh.current.delete('pending')
    try {
      setPendingApps((await api.getPendingApps()) ?? [])
    } catch (e: unknown) {
      console.error('[Balance] pending refresh failed:', e)
    }
  }, [])

  const refreshRunners = useRef({ controlled: refreshControlled, autoLimited: refreshAutoLimited, pending: refreshPending })
  refreshRunners.current = { controlled: refreshControlled, autoLimited: refreshAutoLimited, pending: refreshPending }

  const flushRefreshes = useCallback(() => {
    refreshTimer.current = null
    // holdRefreshes() re-arms the timer once the last action settles.
    if (actionsInFlight.current > 0) return
    const keys = Array.from(queuedRefresh.current)
    queuedRefresh.current.clear()
    keys.forEach((key) => void refreshRunners.current[key]())
  }, [])

  const queueRefresh = useCallback((...keys: RefreshKey[]) => {
    keys.forEach((key) => queuedRefresh.current.add(key))
    if (refreshTimer.current === null && queuedRefresh.current.size > 0) {
      refreshTimer.current = window.setTimeout(flushRefreshes, REFRESH_COALESCE_MS)
    }
  }, [flushRefreshes])

  useEffect(() => () => {
    if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current)
  }, [])

  /** Hold background refreshes for the duration of `fn`; its own refresh covers them. */
  const holdRefreshes = useCallback(async <T,>(fn: () => Promise<T>): Promise<T> => {
    actionsInFlight.current += 1
    try {
      return await fn()
    } finally {
      actionsInFlight.current -= 1
      queueRefresh()
    }
  }, [queueRefresh])

  const refreshExclusions = useCallback(async () => {
    try {
      setExclusions((await api.getAutoLimitExclusions()) ?? [])
    } catch (e: unknown) {
      console.error('[Balance] exclusions refresh failed:', e)
    }
  }, [])

  const handleRemoveExclusion = useCallback(async (row: AutoLimitExclusion) => {
    try {
      await api.removeAutoLimitExclusion(row.key)
      setExclusions((prev) => prev.filter((item) => item.key !== row.key))
      messageApi.success(`${row.app_name || row.app_id} can be auto-limited again`)
    } catch (error) {
      messageApi.error(error instanceof Error ? error.message : 'Failed to remove exclusion')
    }
  }, [messageApi])

  const fetchData = useCallback(async () => {
    // Everything a queued refresh would fetch is fetched here.
    queuedRefresh.current.clear()
    lastControlledFetch.current = Date.now()
    try {
      const [apps, controlled, pending, autoLimited, exclusionRows, networkControl] = await Promise.allSettled([
        api.getApps(),
        api.getControlledApps(),
        api.getPendingApps(),
        api.getAutoLimitedApps(),
        api.getAutoLimitExclusions(),
        api.getConfig<{
          enable_network_control: boolean
          config_network_bw?: Record<string, { min?: number; max?: number }>
        }>('network_control'),
      ])

      if (apps.status === 'fulfilled') setAllApps(apps.value ?? [])
      if (controlled.status === 'fulfilled') applyControlled(controlled.value ?? [])
      if (pending.status === 'fulfilled') setPendingApps(pending.value ?? [])
      if (autoLimited.status === 'fulfilled') applyAutoLimited(autoLimited.value)
      if (exclusionRows.status === 'fulfilled') setExclusions(exclusionRows.value ?? [])
      if (networkControl.status === 'fulfilled') {
        setNetworkControlEnabled(Boolean(networkControl.value.enable_network_control))
        setNetworkBandwidthRanges(sanitizeNetworkBandwidthRanges(networkControl.value.config_network_bw))
      }

      setError(null)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to fetch app data')
    } finally {
      setLoading(false)
    }
  }, [applyAutoLimited, applyControlled])

  // Track whether the startup scan has been triggered for this session.
  // The scan only runs once — the first time this tab becomes active — to detect
  // managed apps that were already running before the balancer service started.
  // After that initial check, BPF handles all start/stop events as usual.
  const startupScanDone = useRef(false)

  // Initial data fetch on mount / when tab becomes active
  useEffect(() => {
    if (active) {
      if (!startupScanDone.current) {
        startupScanDone.current = true
        // Two-stage load: render whatever the DB has now (typically "NA"
        // immediately after a service start because reset_app_status() ran),
        // then refetch once the startup scan finishes so the row picks up
        // the real "running"/"stopped" status the scan just persisted.
        // Without the second fetch the user has to switch tabs and back to
        // see correct statuses, because the SSE channel may not be up yet
        // when scan_already_running_apps() emits its callbacks.
        fetchData()
        api.checkRunningApps()
          .catch((e: unknown) => {
            console.error('[Balance] startup scan failed:', e)
          })
          .finally(() => fetchData())
      } else {
        fetchData()
      }
    }
  }, [active, fetchData])

  // Per app, remember which running cgroups are selected as limit targets.
  // Defaults to "all running" and tracks process churn over time.
  useEffect(() => {
    setSelectedTargetCgroups((prev) => {
      const next: Record<string, string[]> = {}
      for (const app of controlledApps) {
        const available = Array.from(new Set(
          (app.process_status_rows ?? [])
            .filter((row) => row.runtime_status === 'Running')
            .map((row) => (row.cgroup || '').trim())
            .filter(Boolean)
        ))

        if (available.length === 0) continue

        const existing = prev[app.app_id]
        if (!existing || existing.length === 0) {
          const limited = (app.process_status_rows ?? [])
            .filter((row) => row.runtime_status === 'Running' && row.limit_status === 'Limited')
            .map((row) => (row.cgroup || '').trim())
            .filter(Boolean)
          next[app.app_id] = app.control_status === 'MANUAL_LIMITED' && limited.length > 0
            ? Array.from(new Set(limited))
            : available
          continue
        }

        const filtered = existing.filter((cg) => available.includes(cg))
        next[app.app_id] = filtered.length > 0 ? filtered : available
      }
      return next
    })
  }, [controlledApps])

  // App-level SSE events do not report a process scope exiting while another configured
  // process remains alive. Refresh the per-scope snapshot while this tab is visible.
  useEffect(() => {
    if (!active) return
    // Measured from the last fetch of any kind, not a fixed beat: the list is never
    // older than CONTROLLED_POLL_MS, and a fetch an event or action just made pushes
    // the next poll back instead of being repeated. The check itself is local.
    const timer = window.setInterval(() => {
      if (Date.now() - lastControlledFetch.current < CONTROLLED_POLL_MS - REFRESH_COALESCE_MS) return
      queueRefresh('controlled')
    }, CONTROLLED_POLL_CHECK_MS)
    return () => window.clearInterval(timer)
  }, [active, queueRefresh])

  // Advance the "Limited For" column. Local only, no request, and only while there is
  // something to count, so an idle tab does no work.
  useEffect(() => {
    if (!active || autoLimitedApps.length === 0) return
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [active, autoLimitedApps.length])

  // SSE: the server pushes every update; this tab does not poll
  useAppEvents(
    useCallback((event) => {
      if (event.purpose === 'app' && event.app_id) {
        // Update the app's status in the controlled list
        setControlledApps((prev) =>
          prev.map((app) =>
            app.app_id === event.app_id ? { ...app, status: event.status } : app
          )
        )

        // Show a toast that mirrors the Python register_notification() logic:
        //   - 'limited' (auto, balancer-initiated) → "system busy, will auto-restore" warning
        //   - 'a_limited' (manual, user-initiated) → no toast here; submitResourceLimit
        //     already shows "Resource limit applied to ...". A second message would be
        //     duplicate and the auto-restore wording is wrong for manual limits.
        //   - other transitions → generic status-updated info
        if (event.status === APP_STATUS.LIMITED) {
          messageApi.warning(
            `System busy: ${event.app_name} resource usage has been temporarily limited. It will be restored when resources become available.`
          )
        } else if (event.status !== APP_STATUS.A_LIMITED) {
          const statusLabel: Record<string, string> = {
            running: 'Running',
            stopped: 'Stopped',
            pending: 'Pending',
          }
          const label = statusLabel[event.status] ?? event.status
          messageApi.info(`App ${event.app_name} status updated: ${label}`)
        }

        // When an app transitions away from pending (e.g. running, stopped, limited),
        // remove it from the pending queue immediately without waiting for the refresh
        // to complete.  This prevents the card from showing a stale entry during the
        // async round-trip.
        // Note: the reverse (status === PENDING) needs a pending refresh, because adding
        // to pendingApps requires a full AppInfo object the SSE payload does not carry.
        if (event.status !== APP_STATUS.PENDING) {
          setPendingApps((prev) => prev.filter((app) => app.app_id !== event.app_id))
        } else {
          queueRefresh('pending')
        }

        // Refresh only what this event can have changed. The controlled list carries the
        // per-process rows and limit flags, so it is refreshed for every status change;
        // the auto-limited list only moves when a limit is applied or lifted.
        queueRefresh('controlled')
        if (
          event.status === APP_STATUS.LIMITED ||
          event.status === APP_STATUS.A_LIMITED ||
          event.status === APP_STATUS.RUNNING ||
          event.status === APP_STATUS.STOPPED
        ) {
          queueRefresh('autoLimited')
        }
      } else if (event.purpose === 'notify') {
        // Live pressure level for the Auto Limited card — state only, no request.
        if (event.status === 'pressure_level_changed') {
          const sys = event.sys_level ?? ''
          const disk = event.disk_level ?? ''
          // Skip no-op updates so a flapping level does not re-render the tab for nothing.
          setPressureLevels((prev) =>
            prev.sys === sys && prev.disk === disk ? prev : { sys, disk }
          )
          return
        }
        // A staged restore relaxed or lifted one channel's cap without changing the
        // app's status, so this is the only signal that the list has moved.
        if (
          event.status === 'auto_limit_changed' ||
          event.status === 'auto_limit_restored_by_user' ||
          event.status === 'app_closed_limit_restored'
        ) {
          queueRefresh('autoLimited')
          return
        }
        // Another client changed Freeze or CPU Affinity (or the reaper unpinned an exited app):
        // the Frozen / CPU tags and the Freeze button are the only things that move, and no
        // other event announces it.
        if (event.status === 'cpu_affinity_changed' || event.status === 'app_frozen_changed') {
          queueRefresh('controlled')
          return
        }
        // System-level notifications (no specific app_id)
        if (event.status === 'manual_app_limit_by_user') {
          messageApi.warning(
            'System busy: a critical app is running. Consider manually adjusting resource allocation.'
          )
        } else if (event.status === 'high_usage_by_multiple_instances') {
          messageApi.warning(
            'System busy: multiple apps are consuming high resources. Consider reducing the number of running apps.'
          )
        }
      }
    }, [messageApi, queueRefresh]),
    active
  )

  function withLoading(key: string, fn: () => Promise<void>) {
    return async () => {
      setActionLoading((prev) => ({ ...prev, [key]: true }))
      try {
        // fetchData() refreshes every list, so `fn` need not refresh any itself.
        await holdRefreshes(async () => {
          await fn()
          await fetchData()
        })
      } catch (e: unknown) {
        messageApi.error(e instanceof Error ? e.message : 'Operation failed')
      } finally {
        setActionLoading((prev) => ({ ...prev, [key]: false }))
      }
    }
  }

  // Scroll a management-table row into view and flash it — used when a duplicate add is
  // rejected so the user is shown the existing row instead of getting an error.
  const focusControlledRow = useCallback((appId: string, controlStatus?: ControlStatus) => {
    // Jump to the tab that actually contains the row: auto-limited apps live under
    // "Auto Control", everything else under "Manual Control".
    setControlTab(controlStatus === 'AUTO_LIMITED' ? 'auto' : 'manual')
    setHighlightAppId(appId)
    window.setTimeout(() => {
      document
        .querySelector(`.ant-table-row[data-row-key="${appId}"]`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 0)
    window.setTimeout(() => setHighlightAppId((cur) => (cur === appId ? null : cur)), 2400)
  }, [])

  const handleDelete = (app: AppInfo) =>
    withLoading(`delete-${app.app_id}`, async () => {
      await api.purgeControlledApp(app.app_id)
      messageApi.success(`Deleted ${app.app_name}`)
    })()

  function applyLimitProfile(profile: ResourceLimitProfileData, defaultNetworkPriority: string) {
    const cpuOptions = normalizePercentOptions(profile.cpu.options, profile.cpu.value)
    const memOptions = normalizePercentOptions(profile.memory.options, profile.memory.value)
    const cgIds = profile.cgroup_ids ?? []

    const baseForm: LimitFormValues = {
      applyResourceLimit: true,
      networkPriority: normalizeNetworkPriority(defaultNetworkPriority),
      cpuEnabled: profile.cpu.enabled,
      cpuPercent: Number(profile.cpu.value),
      cpuMin: profile.cpu.min,
      cpuMax: profile.cpu.max,
      cpuOptions,
      memEnabled: profile.memory.enabled,
      memPercent: Number(profile.memory.value),
      memMin: profile.memory.min,
      memMax: profile.memory.max,
      memOptions,
      diskEnabled: Boolean(profile.disk_io.enabled),
      diskDetected: Boolean(profile.disk_io.is_io_limit),
      writeMbps: profile.disk_io.write.value,
      writeMbpsMax: profile.disk_io.write.max,
      readMbps: profile.disk_io.read.value,
      readMbpsMax: profile.disk_io.read.max,
      writeIops: profile.disk_io.write_iops.value,
      writeIopsMax: profile.disk_io.write_iops.max,
      readIops: profile.disk_io.read_iops.value,
      readIopsMax: profile.disk_io.read_iops.max,
      processNames: profile.process_names ?? [],
      cgroupIds: cgIds,
      targetProcesses: (profile.target_processes ?? []).map((x) => ({
        pid: Number(x.pid),
        name: (x.name || '').trim(),
      })).filter((x) => Number.isFinite(x.pid) && x.pid > 0),
    }

    setLimitForm(baseForm)

  }

  // Freeze is a separate step after Limit, so it asks for confirmation (it costs the user
  // something); Unfreeze is the way back and needs none.  Restore stays disabled while the
  // app is frozen, so a failed Unfreeze is simply retried by clicking the button again.
  const handleFreeze = (app: AppInfo) => {
    Modal.confirm({
      title: `Freeze ${app.app_name}?`,
      width: 560,
      content: (
        <div style={{ fontSize: 13 }}>
          <p>{FREEZE_WHY}</p>
          <ul style={{ paddingLeft: 18, marginBottom: 0 }}>
            {FREEZE_RISKS.map((risk) => <li key={risk}>{risk}</li>)}
          </ul>
        </div>
      ),
      okText: 'Freeze',
      onOk: withLoading(`freeze-${app.app_id}`, async () => {
        await api.freezeApp({ app_id: app.app_id, freeze: true })
        messageApi.success(`${app.app_name} is frozen`)
      }),
    })
  }

  const handleUnfreeze = (app: AppInfo) =>
    withLoading(`freeze-${app.app_id}`, async () => {
      await api.freezeApp({ app_id: app.app_id, freeze: false })
      messageApi.success(`${app.app_name} is running again (limits kept)`)
    })()

  // CPU affinity is its own step, independent of Limit/Restore.  The dialog always opens first and
  // reads the core layout from the server; SmarTune's pinning is undone by choosing "Restore
  // original", or automatically when the app exits.
  const openAffinityDialog = async (app: AppInfo) => {
    setAffinityDialog({ ...AFFINITY_DIALOG_CLOSED, app, status: 'loading' })
    const isCurrent = (prev: AffinityDialogState) => prev.app?.app_id === app.app_id
    try {
      const st = await api.cpuAffinity({ app_id: app.app_id, class: 'status' })
      if (!st || Object.keys(st.classes).length === 0) throw new Error('core layout unavailable')
      // Same scope as the request submitAffinity sends: only the cgroups picked in the table, if any.
      const targets = new Set((selectedTargetCgroups[app.app_id] ?? []).map((c) => c.split('/').pop()))
      const cgroups = targets.size ? st.cgroups.filter((c) => targets.has(c.cgroup)) : st.cgroups
      const pinned = Array.from(new Set(
        cgroups.filter((c) => c.bound && c.cpus).map((c) => c.cpus as string)))
      const initial = initialAffinityChoice(st.classes, pinned, st.last ?? null)
      setAffinityDialog((prev) => (isCurrent(prev)
        ? { ...prev, status: 'ready', classes: st.classes, cgroups, ...initial } : prev))
    } catch {
      setAffinityDialog((prev) => (isCurrent(prev) ? { ...prev, status: 'error' } : prev))
    }
  }

  const submitAffinity = () => {
    const { app, choice, selected } = affinityDialog
    if (!app || !choice) return
    setAffinityDialog(AFFINITY_DIALOG_CLOSED)
    const cores = [...selected].sort((a, b) => a - b)
    withLoading(`affinity-${app.app_id}`, async () => {
      const result = await api.applyCpuAffinity({
        app_id: app.app_id,
        class: choice,
        cpus: choice === 'custom' ? cores.join(',') : undefined,
        target_cgroups: selectedTargetCgroups[app.app_id],
      })
      // Restore reports per cgroup what it put back, which need not be "all cores".
      messageApi.success(
        choice === 'restore' ? result
          : choice === 'custom' ? `${app.app_name} is restricted to CPU ${cores.join(', ')}`
            : `${app.app_name} is restricted to ${choice.toUpperCase()} cores`)
    })()
  }

  const handleResourceLimit = async (app: AppInfo) => {
    setLimitDialog({ app, open: true, loadingProfile: true, submitting: false })
    setResourceSectionExpanded(true)
    setNetworkSectionExpanded(true)
    try {
      const priority = app.priority ?? 'medium'
      const defaultNetworkPriority = normalizeNetworkPriority(app.network_priority ?? app.priority ?? priority)
      const profile = await api.getResourceLimitProfile({
        app_id: app.app_id,
        app_name: app.app_name,
        priority,
      })
      applyLimitProfile(profile, defaultNetworkPriority)
    } catch (e: unknown) {
      setLimitDialog({ app: null, open: false, loadingProfile: false, submitting: false })
      messageApi.error(e instanceof Error ? e.message : 'Failed to load limit profile')
    } finally {
      setLimitDialog((prev) => ({ ...prev, loadingProfile: false }))
    }
  }

  // Ends with fetchData(), like withLoading actions, so events it triggers wait for that.
  const submitResourceLimit = () => holdRefreshes(applyLimitDialog)

  const applyLimitDialog = async () => {
    if (!limitDialog.app) return

    setLimitDialog((prev) => ({ ...prev, submitting: true }))
    try {
      const priority = limitDialog.app.priority ?? 'medium'
      const networkPriority = normalizeNetworkPriority(limitForm.networkPriority)
      const shouldApplyResourceLimit = Boolean(limitForm.applyResourceLimit)
      const shouldUpdateNetworkPriority = networkControlEnabled
      if (!shouldApplyResourceLimit && !shouldUpdateNetworkPriority) {
        messageApi.warning('Please select at least one action to apply.')
        setLimitDialog((prev) => ({ ...prev, submitting: false }))
        return
      }

      const targetCgroups = selectedDialogCgroups
      if (shouldApplyResourceLimit && targetCgroups.length === 0) {
        messageApi.warning('No running process selected. Expand the app row and tick at least one process scope first.')
        setLimitDialog((prev) => ({ ...prev, submitting: false }))
        return
      }
      const isMultiTarget = targetCgroups.length > 1
      let resourceApplied = false
      let resourceSkippedMessage: string | null = null

      if (shouldUpdateNetworkPriority) {
        await api.setNetworkPriority({
          app_id: limitDialog.app.app_id,
          network_priority: networkPriority,
        })
        messageApi.success(`Network priority updated for ${limitDialog.app.app_name}`)
      }

      if (shouldApplyResourceLimit) {
        // No toast here: the dialog stays open with an in-place Alert saying the
        // same thing, and it lasts exactly as long as the request does.
        const res = await api.resourceLimit({
          app_id: limitDialog.app.app_id,
          app_name: limitDialog.app.app_name,
          priority,
          target_cgroups: targetCgroups,
          limit_overrides: {
            cpu: { enabled: limitForm.cpuEnabled, rate: limitForm.cpuPercent / 100 },
            memory: { enabled: limitForm.memEnabled, rate: limitForm.memPercent / 100 },
            disk_io: {
              enabled: limitForm.diskEnabled,
              rate: {
                write: limitForm.writeMbps,
                read: limitForm.readMbps,
                write_iops: limitForm.writeIops,
                read_iops: limitForm.readIops,
              },
            },
          },
        })
        if (res.skipped) {
          // Server intentionally skipped the limit (negligible usage / undetectable
          // process). Surface the server-provided reason and still close the dialog.
          resourceSkippedMessage = res.message
        } else {
          resourceApplied = true
        }
      }

      if (resourceSkippedMessage) {
        messageApi.warning(resourceSkippedMessage)
      }
      if (resourceApplied) {
        messageApi.success(
          isMultiTarget
            ? `Unified resource limit applied to ${limitDialog.app.app_name} across ${targetCgroups.length} cgroups`
            : `Resource limit applied to ${limitDialog.app.app_name}`
        )
      }

      setLimitDialog({ app: null, open: false, loadingProfile: false, submitting: false })
      await fetchData()
    } catch (e: unknown) {
      messageApi.error(e instanceof Error ? e.message : 'Failed to apply resource limit')
    } finally {
      setLimitDialog((prev) => ({ ...prev, submitting: false }))
    }
  }

  const handleResourceRestore = (app: AppInfo) =>
    withLoading(`restore-${app.app_id}`, async () => {
      await api.resourceRestore({ app_id: app.app_id })
      messageApi.success(`Resources restored for ${app.app_name}`)
      messageApi.info({
        content: `${app.app_name}: ${MEMORY_RECOVERY_HINT}`,
        duration: 8,
      })
    })()

  const handleKeepAlive = (app: AppInfo) =>
    withLoading(`keepalive-${app.app_id}`, async () => {
      await api.setOomScore({ app_id: app.app_id })
      messageApi.success(`Keep-alive set for ${app.app_name}`)
    })()

  const handleCancelRelaunch = (app: AppInfo) =>
    withLoading(`cancel-${app.app_id}`, async () => {
      await api.cancelRelaunch({ app_id: app.app_id })
      messageApi.success(`Relaunch cancelled for ${app.app_name}`)
    })()

  // Restoring by hand also opts the app out of auto-limiting until the exclusion is removed
  // or the service restarts. Registered apps return to Manual Control; new discoveries leave
  // the table entirely; withLoading's fetchData() refreshes both sources.
  const handleAutoLimitRestore = (row: { app_id: string; app_name?: string; effective_app_id?: string }) =>
    withLoading(`autolimit-restore-${row.effective_app_id ?? row.app_id}`, async () => {
      await api.autoLimitRestore({ app_id: row.app_id })
      setAutoLimitedApps((prev) =>
        prev.filter((item) => item.effective_app_id !== (row.effective_app_id ?? row.app_id))
      )
      // Forget the elapsed-time base too: if this app is ever auto-limited again it
      // must start counting from zero, not from the limit the user just lifted.
      delete limitedSinceRef.current[row.effective_app_id ?? row.app_id]
      messageApi.success(`Resources restored for ${row.app_name || row.app_id}`)
      messageApi.info({
        content: `${row.app_name || row.app_id}: ${MEMORY_RECOVERY_HINT}`,
        duration: 8,
      })
      messageApi.warning({ content: AUTO_LIMIT_EXCLUSION_HINT, duration: 8 })
    })()

  // Take Control (controlled auto-limited row): flip an auto-limited app to a manual limit
  // WITHOUT releasing its cgroup caps. The app stays at its safe throttled water line while
  // ownership moves to the operator, so no crash window opens. It leaves Auto Control and
  // lands under Manual Control with the manual Limit/Restore/Edit buttons unlocked — no extra
  // step. Accepts either an AutoLimitedApp row or a controlled AppInfo row.
  const handleTakeControl = (row: { app_id: string; app_name?: string; effective_app_id?: string }) =>
    withLoading(`lock-manual-${row.effective_app_id ?? row.app_id}`, async () => {
      await api.lockToManual({ app_id: row.app_id })
      messageApi.success(
        `${row.app_name || row.app_id} moved to manual control — limit kept in place, manual controls unlocked`,
      )
    })()

  // An unmanaged auto-limited row has no persistent identity yet. Extract stable identity
  // fields from the limited PID snapshot, then register it (or enable its existing config
  // entry) before adopting the live cgroup limit.
  const handleAddAutoLimitedToControl = (row: AutoLimitedApp) =>
    withLoading(`take-control-${row.effective_app_id}`, async () => {
      const appName = row.app_name || row.app_id
      const knownApp = allApps.find((app) =>
        app.app_id === row.app_id
        || app.app_name.trim().toLowerCase() === appName.trim().toLowerCase(),
      )
      if (knownApp) {
        await api.setToControl({
          app_id: knownApp.app_id,
          app_name: knownApp.app_name,
          priority: row.priority || 'medium',
          network_priority: row.priority || 'medium',
          controlled: true,
          remark: knownApp.remark || '',
          cmdline: knownApp.cmdline || '',
          cgroup: knownApp.cgroup || 'user',
        })
        await api.adoptAutoLimit({
          effective_app_id: row.effective_app_id,
          app_id: knownApp.app_id,
          app_name: knownApp.app_name,
          priority: row.priority,
        })
        setControlTab('manual')
        messageApi.success(
          `${knownApp.app_name} is now managed as a manual limit — its caps were carried over`,
        )
        return
      }

      const representativePid = row.representative_pid
      if (!representativePid) {
        throw new Error('The limited process is no longer running, so it cannot be added to manual control')
      }
      const extracted = await api.discoverExtract([representativePid], appName)
      if (!extracted.id_suggestion || !extracted.process_names.length) {
        throw new Error('Could not derive a stable application identity from the limited process')
      }
      const registrationName = extracted.process_names[0]
      const registration = await api.newControlledApp({
        name: registrationName,
        id: extracted.id_suggestion,
        priority: row.priority || 'medium',
        remark: '',
        commandline: extracted.commandline[0] || '',
        bpf_name: extracted.bpf_name,
        process_names: extracted.process_names,
      })
      let controlledAppId: string
      let controlledAppName: string
      let createdControl = false
      if (registration.status === 'ok') {
        controlledAppId = registration.data.id
        controlledAppName = registration.data.name
        createdControl = true
      } else if (registration.status === 'conflict' && registration.withId) {
        controlledAppId = registration.withId
        controlledAppName = registration.withName || registrationName
        await api.setToControl({
          app_id: controlledAppId,
          app_name: controlledAppName,
          priority: row.priority || 'medium',
          network_priority: row.priority || 'medium',
          controlled: true,
          remark: '',
          cmdline: extracted.commandline[0] || '',
          cgroup: 'user',
        })
      } else {
        throw new Error(registration.message || 'Failed to persist the application for manual control')
      }
      try {
        await api.adoptAutoLimit({
          effective_app_id: row.effective_app_id,
          app_id: controlledAppId,
          app_name: controlledAppName,
          priority: row.priority,
        })
      } catch (error) {
        if (createdControl) {
          try {
            await api.purgeControlledApp(controlledAppId)
          } catch (rollbackError) {
            console.error('[Balance] Take Control rollback failed:', rollbackError)
          }
        }
        throw error
      }
      setControlTab('manual')
      messageApi.success(
        `${row.app_name || row.app_id} is now managed as a manual limit — its caps were carried over, and you can edit or restore it directly`,
      )
    })()

  const controlledColumns: ColumnsType<ControlRow> = [
    {
      title: 'App Name',
      dataIndex: 'app_name',
      key: 'app_name',
      width: 210,
      render: (name: string, record) => {
        const displayName = name || record.app_id
        const tooltipContent = record.remark ? `${displayName} — ${record.remark}` : displayName
        const isAutoLimited = record.control_status === 'AUTO_LIMITED'
        const isManagedAuto = isAutoLimited && record.controlled !== false
        return (
          <Space direction="vertical" size={2} style={{ lineHeight: 1.25 }}>
            <Space size={6} wrap>
              <Tooltip title={tooltipContent}>
                <div style={{ color: COLORS.accent, fontWeight: 500 }}>{displayName}</div>
              </Tooltip>
              {isAutoLimited && (
                <Tooltip title={isManagedAuto
                  ? 'Already registered for Manual Control. When automatic recovery releases this limit, it returns to Manual Control.'
                  : 'Newly discovered, unregistered instance. When automatic recovery releases this limit, it disappears from Auto Control.'}>
                  <Tag color={isManagedAuto ? 'processing' : 'gold'} style={{ marginInlineEnd: 0 }}>
                    {isManagedAuto ? 'Registered' : 'New discovery'}
                  </Tag>
                </Tooltip>
              )}
            </Space>
          </Space>
        )
      },
    },
    {
      title: 'Priority',
      key: 'priority',
      width: 120,
      render: (_: unknown, record: ControlRow) => <PriorityTag priority={record.priority} />,
    },
    {
      title: 'Status',
      key: 'status',
      width: 290,
      render: (_: unknown, record: AppInfo) => {
        const combined = deriveCombinedStatus(record)

        return (
          <Space size={6} wrap>
            {runtimeHintTag(combined.runtime)}
            {limitSummaryTag(combined.limitSummary)}
            {(record.cpu_affinity?.length ?? 0) > 0 && (
              <Tooltip title={`Restricted by CPU Affinity to CPU ${record.cpu_affinity?.join(' ; ')}. Restore does not undo this; use CPU Affinity > All cores.`}>
                <Tag color="geekblue" icon={<ClusterOutlined />} style={{ marginInlineEnd: 0 }}>
                  CPU {record.cpu_affinity?.join(' ; ')}
                </Tag>
              </Tooltip>
            )}
            {record.frozen && record.control_status === 'MANUAL_LIMITED' && (
              <Tag color="blue" icon={<PauseCircleOutlined />} style={{ marginInlineEnd: 0 }}>
                Frozen
              </Tag>
            )}
            {combined.runtime === 'Pending' && (
              <Tooltip title="Cancel Relaunch">
                <Button
                  size="small"
                  icon={<CloseOutlined />}
                  loading={actionLoading[`cancel-${record.app_id}`]}
                  onClick={() => handleCancelRelaunch(record)}
                  style={{ borderColor: COLORS.accent, color: COLORS.accent }}
                />
              </Tooltip>
            )}
          </Space>
        )
      },
    },
    {
      title: 'Remark',
      dataIndex: 'remark',
      key: 'remark',
      width: 220,
      render: (v: string) => (
        <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>{v || '—'}</Text>
      ),
    },
    {
      title: 'Actions',
      key: 'actions',
      width: 240,
      align: 'center',
      render: (_: unknown, record: ControlRow) => {
        // Read-only detail view — always available on every row regardless of control state.
        const detailsButton = (
          <Tooltip title="View current limits, trigger, and recovery state">
            <Button
              size="small"
              icon={<InfoCircleOutlined />}
              aria-label={`View control details for ${record.app_name || record.app_id}`}
              onClick={() => setDetailApp(record)}
              style={{ borderColor: COLORS.accent, color: COLORS.accent }}
            >
              Details
            </Button>
          </Tooltip>
        )

        // Unmanaged auto-limited row: it has no DB entry, so the managed actions (Edit /
        // Delete / Keep-Alive) don't apply yet. Offer exactly
        // two paths — take it under management (its live limit follows it, then the full
        // managed toolset unlocks), or release the limit now.
        if (record.controlled === false && record.__auto) {
          const auto = record.__auto
          return (
            <Space size={4} wrap={false}>
              {detailsButton}
              <Tooltip title="Take this app under management — its current limit is kept in place and handed to you as a manual limit (no release, no crash window). It moves to Manual Control, where you can edit or restore it right away.">
                <Button
                  size="small"
                  type="primary"
                  ghost
                  icon={<LockOutlined />}
                  loading={actionLoading[`take-control-${auto.effective_app_id}`]}
                  onClick={() => handleAddAutoLimitedToControl(auto)}
                >
                  Take Control
                </Button>
              </Tooltip>
              <Popconfirm
                title="Restore now?"
                description={<div style={{ maxWidth: 320 }}>{AUTO_LIMIT_EXCLUSION_HINT}</div>}
                okText="Restore"
                cancelText="Cancel"
                onConfirm={() => handleAutoLimitRestore(auto)}
              >
                <Tooltip title="Release this limit immediately">
                  <Button
                    size="small"
                    danger
                    icon={<RollbackOutlined />}
                    loading={actionLoading[`autolimit-restore-${auto.effective_app_id}`]}
                  >
                    Restore
                  </Button>
                </Tooltip>
              </Popconfirm>
            </Space>
          )
        }

        if (record.control_status === 'AUTO_LIMITED') {
          return (
            <Space size={4} wrap={false}>
              {detailsButton}
              <Tooltip title="Take control: move this app to Manual Control with its current limit kept in place. It leaves Auto Control with no cgroup release, and manual actions unlock.">
                <Button
                  size="small"
                  type="primary"
                  ghost
                  icon={<LockOutlined />}
                  loading={actionLoading[`lock-manual-${record.app_id}`]}
                  onClick={() => handleTakeControl(record)}
                >
                  Take Control
                </Button>
              </Tooltip>
              <Popconfirm
                title="Restore now?"
                description={<div style={{ maxWidth: 320 }}>{AUTO_LIMIT_EXCLUSION_HINT}</div>}
                okText="Restore"
                cancelText="Cancel"
                onConfirm={() => handleAutoLimitRestore(record)}
              >
                <Tooltip title="Release this automatic limit now">
                  <Button
                    size="small"
                    danger
                    icon={<RollbackOutlined />}
                    loading={actionLoading[`autolimit-restore-${record.app_id}`]}
                  >
                    Restore
                  </Button>
                </Tooltip>
              </Popconfirm>
            </Space>
          )
        }

        const isRunning = record.runtime_hint === 'Running'
          || record.status?.toLowerCase() === APP_STATUS.RUNNING
        const selectedCgroups = selectedTargetCgroups[record.app_id]
        const hasSelectedRunningCgroup = selectedCgroups === undefined
          ? (record.process_status_rows ?? []).some(
              (row) => row.runtime_status === 'Running' && Boolean((row.cgroup || '').trim()),
            )
          : selectedCgroups.length > 0
        const isCritical = (record.priority ?? '').toLowerCase() === 'critical'
        const isLimited = record.control_status === 'MANUAL_LIMITED'

        return (
          <Space size={4} wrap={false}>
            {detailsButton}
            <Divider type="vertical" />
            {/* Group 1: how much the app may use (resource limits, freeze) */}
            {isLimited ? (
              <>
              <Tooltip title={record.frozen ? 'Unfreeze first, then restore the limits' : 'Restore resource limits (does not undo CPU Affinity)'}>
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  disabled={Boolean(record.frozen)}
                  loading={actionLoading[`restore-${record.app_id}`]}
                  onClick={() => handleResourceRestore(record)}
                  style={{ borderColor: COLORS.accent, color: COLORS.accent }}
                >
                  Restore
                </Button>
              </Tooltip>
              <Tooltip title={record.frozen
                ? 'Unfreeze this app (the resource limits stay in place)'
                : 'Freeze this app to free the GPU/NPU for others (use only if the limit is not enough)'}>
                <Button
                  size="small"
                  icon={<PauseCircleOutlined />}
                  loading={actionLoading[`freeze-${record.app_id}`]}
                  onClick={() => (record.frozen ? handleUnfreeze(record) : handleFreeze(record))}
                  style={{ borderColor: COLORS.accent, color: COLORS.accent }}
                >
                  {record.frozen ? 'Unfreeze' : 'Freeze'}
                </Button>
              </Tooltip>
              </>
            ) : (
              <Tooltip title={hasSelectedRunningCgroup ? 'Apply Resource Limit' : 'Select at least one running process scope first'}>
                <Button
                  size="small"
                  icon={<DatabaseOutlined />}
                  disabled={!isRunning || !hasSelectedRunningCgroup}
                  loading={limitDialog.loadingProfile && limitDialog.app?.app_id === record.app_id}
                  onClick={() => handleResourceLimit(record)}
                  style={isRunning ? { borderColor: COLORS.accent, color: COLORS.accent } : {}}
                >
                  Limit
                </Button>
              </Tooltip>
            )}

            {/* Group 2: where the app runs (separate from, and not undone by, Restore) */}
            {isRunning && <Divider type="vertical" />}
            {isRunning && (
              <Tooltip title="Choose which CPU cores (P / E / LPE, or specific cores) this app may run on">
                <Button
                  size="small"
                  icon={<ClusterOutlined />}
                  loading={actionLoading[`affinity-${record.app_id}`]}
                  onClick={() => openAffinityDialog(record)}
                  style={{ borderColor: COLORS.accent, color: COLORS.accent }}
                >
                  CPU Affinity
                </Button>
              </Tooltip>
            )}

            {/* Group 3: app management */}
            <Divider type="vertical" />
            <Tooltip title="Edit application priority and process identities. Add or remove the program names that decide which processes and cgroups this app controls (currently-limited names are locked).">
              <Button
                size="small"
                icon={<EditOutlined />}
                onClick={() => setEditApp(record)}
                style={{ borderColor: COLORS.accent, color: COLORS.accent }}
              >
                Edit
              </Button>
            </Tooltip>

            <Tooltip title={isCritical && isRunning ? 'Keep Alive (OOM protect)' : 'Only available for Critical apps that are Running'}>
              <Button
                size="small"
                icon={<HeartOutlined />}
                disabled={!isCritical || !isRunning}
                loading={actionLoading[`keepalive-${record.app_id}`]}
                onClick={() => handleKeepAlive(record)}
                style={isCritical && isRunning ? { borderColor: COLORS.accent, color: COLORS.accent } : {}}
              >
                Keep Alive
              </Button>
            </Tooltip>

            <Tooltip title="Delete completely (purges config + DB; needs the wizard to re-add)">
              <Button
                size="small"
                danger
                icon={<DeleteFilled />}
                loading={actionLoading[`delete-${record.app_id}`]}
                onClick={() => {
                  Modal.confirm({
                    title: `Delete ${record.app_name} completely?`,
                    content:
                      'This first restores any active manual resource limit, then permanently removes '
                      + 'the entry from config.yaml and the database. If resources cannot be restored, '
                      + 'the app is not deleted. '
                      + 'To control this app again you will need to re-add it through the '
                      + 'wizard.',
                    okText: 'Delete',
                    okType: 'danger',
                    onOk: () => handleDelete(record),
                  })
                }}
              >
                Delete
              </Button>
            </Tooltip>
          </Space>
        )
      },
    },
  ]

  // Mirror the Control Center table's column-width vector so the shared columns
  // (App Name / Priority / Status) line up. Both tables stretch to the same card width, so
  // identical widths + identical leading offset ⇒ identical column positions. The leading
  // 48px spacer stands in for the Control Center expand-icon column; Remark absorbs the
  // Control Center's Remark (220) + Actions (240) widths so it fills the row's remainder
  // instead of leaving a blank trailing column.
  const pendingColumns: ColumnsType<AppInfo> = [
    {
      title: '',
      key: 'expand_spacer',
      width: 48,
      render: () => null,
    },
    {
      title: 'App Name',
      dataIndex: 'app_name',
      key: 'app_name',
      width: 210,
      render: (name: string) => <Text style={{ color: COLORS.text }}>{name}</Text>,
    },
    {
      title: 'Priority',
      dataIndex: 'priority',
      key: 'priority',
      width: 120,
      render: (p: string) => <PriorityTag priority={p} />,
    },
    {
      title: 'Status',
      key: 'status',
      width: 290,
      render: () => <Tag color="processing">Pending</Tag>,
    },
    {
      title: 'Remark',
      dataIndex: 'remark',
      key: 'remark',
      width: 220 + 240,
      render: (v: string) => <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>{v ?? '—'}</Text>,
    },
  ]


  const processStatusColumns: ColumnsType<ProcessStatusRow> = [
    {
      title: 'Process Name',
      dataIndex: 'process_name',
      key: 'process_name',
      width: 200,
      render: (_name: string, row) => (
        <Text style={{ color: COLORS.text }}>
          {deriveDisplayProcessName(row)}
          {row.pid ? <Text style={{ color: COLORS.textMuted }}>{` · PID ${row.pid}`}</Text> : ''}
        </Text>
      ),
    },
    {
      title: 'Command',
      dataIndex: 'cmdline',
      key: 'cmdline',
      width: 280,
      ellipsis: true,
      render: (cmdline: string) => {
        const label = (cmdline || '').trim() || 'Not set'
        return (
          <Tooltip title={label}>
            <Text style={{ color: COLORS.textMuted, fontSize: 12, fontFamily: 'monospace' }} ellipsis>
              {label}
            </Text>
          </Tooltip>
        )
      },
    },
    {
      title: 'Scope (cgroup)',
      dataIndex: 'cgroup',
      key: 'cgroup',
      width: 220,
      ellipsis: true,
      render: (cgroup: string) => {
        const label = (cgroup || '').trim() || '-'
        return (
          <Tooltip title={label}>
            <Text style={{ color: COLORS.textMuted, fontSize: 12, fontFamily: 'monospace' }} ellipsis>
              {label}
            </Text>
          </Tooltip>
        )
      },
    },
    {
      title: 'Status',
      key: 'status',
      width: 190,
      render: (_: unknown, row: ProcessStatusRow) => {
        const limitTag = row.limit_status === 'Limited'
          ? <Tag color="warning" style={{ marginInlineEnd: 0 }}>Limited</Tag>
          : row.limit_status === 'Not Limited'
            ? <Tag color="default" style={{ marginInlineEnd: 0 }}>Not Limited</Tag>
            : <Tag color="default" style={{ marginInlineEnd: 0 }}>N/A</Tag>

        return (
          <Space size={6} wrap>
            {runtimeHintTag(row.runtime_status)}
            {limitTag}
          </Space>
        )
      },
    },
    {
      title: 'Applied At',
      dataIndex: 'applied_at',
      key: 'applied_at',
      width: 170,
      render: (appliedAt: number | null | undefined) => (
        <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>
          {appliedAt ? formatPassiveControlTimestamp(appliedAt) : '-'}
        </Text>
      ),
    },
  ]

  const limitDialogPriority = limitDialog.app
    ? (limitDialog.app.priority ?? 'medium').toLowerCase()
    : 'medium'
  const currentNetworkPriority = normalizeNetworkPriority(
    limitDialog.app?.network_priority ?? limitDialog.app?.priority ?? limitDialogPriority
  )
  const selectedNetworkPriority = normalizeNetworkPriority(limitForm.networkPriority || currentNetworkPriority)
  const currentNetworkRange = networkBandwidthRanges[currentNetworkPriority as NetworkClassKey] ?? DEFAULT_NETWORK_BW_RANGES.low
  const limitDialogPriorityColor = priorityColor(limitDialogPriority)
  const limitDialogTitle = limitDialog.app
    ? (
      <Text strong>
        {`Limit Configuration - ${limitDialog.app.app_name} `}
        <Text strong style={{ color: limitDialogPriorityColor }}>
          ({limitDialogPriority.toUpperCase()})
        </Text>
      </Text>
    )
    : <Text strong>Limit Configuration</Text>

  const inlineProcessNames = useMemo(
    () => limitForm.processNames.map((name) => name.trim()).filter(Boolean),
    [limitForm.processNames]
  )
  const selectedDialogCgroups = useMemo(() => {
    const appId = limitDialog.app?.app_id
    if (!appId) return []

    const preferred = selectedTargetCgroups[appId]
    if (preferred !== undefined) return preferred

    const runningFromRows = Array.from(new Set(
      (limitDialog.app?.process_status_rows ?? [])
        .filter((row) => row.runtime_status === 'Running')
        .map((row) => (row.cgroup || '').trim())
        .filter(Boolean)
    ))
    if (runningFromRows.length > 0) return runningFromRows

    return (limitForm.cgroupIds ?? []).map((x) => String(x).trim()).filter(Boolean)
  }, [limitDialog.app, limitForm.cgroupIds, selectedTargetCgroups])

  const targetRows = useMemo(() => {
    if (!limitDialog.app) return []

    const selectedSet = new Set(selectedDialogCgroups)
    const namesByCgroup = new Map<string, Set<string>>()
    for (const row of (limitDialog.app.process_status_rows ?? [])) {
      const cgroup = (row.cgroup || '').trim()
      if (!cgroup || !selectedSet.has(cgroup)) continue
      const name = deriveDisplayProcessName(row)
      if (!namesByCgroup.has(cgroup)) namesByCgroup.set(cgroup, new Set())
      namesByCgroup.get(cgroup)!.add(name)
    }

    return selectedDialogCgroups.map((cgroupId) => ({
      cgroupId,
      processName: Array.from(namesByCgroup.get(cgroupId) ?? []).join(', ') || inlineProcessNames[0] || '-',
    }))
  }, [inlineProcessNames, limitDialog.app, selectedDialogCgroups])

  // renderLimitSettings accepts a form snapshot and a typed setter so each
  // context (single-cgroup or per-tab) can be fully independent.
  const renderLimitSettings = (
    form: LimitFormValues,
    updateForm: (updater: (prev: LimitFormValues) => LimitFormValues) => void
  ) => (
    <>
      <div>
        <Space size={8} align="center">
          <Button
            size="small"
            type="text"
            onClick={() => setResourceSectionExpanded((prev) => !prev)}
            icon={resourceSectionExpanded
              ? <DownOutlined style={{ color: COLORS.textMuted, fontSize: 12 }} />
              : <RightOutlined style={{ color: COLORS.textMuted, fontSize: 12 }} />}
            aria-label="Toggle resource limit settings"
          />
          <Checkbox
            checked={form.applyResourceLimit}
            onChange={(e) => {
              const checked = e.target.checked
              updateForm((prev) => ({ ...prev, applyResourceLimit: checked }))
              if (checked) setResourceSectionExpanded(true)
            }}
          >
            <Text strong>Apply Resource Limit (CPU/Memory/Disk)</Text>
          </Checkbox>
        </Space>
      </div>

      {resourceSectionExpanded && <div style={{ marginLeft: 14, paddingLeft: 12, borderLeft: `2px solid ${COLORS.border}` }}>
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
          Resource limit settings
        </Text>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Space size={4}>
            <Checkbox
              checked={form.cpuEnabled}
              disabled={!form.applyResourceLimit}
              onChange={(e) => updateForm((prev) => ({ ...prev, cpuEnabled: e.target.checked }))}
            >
              <Text strong>CPU Limit (%)</Text>
            </Checkbox>
            <Tooltip title="Controls how much CPU this app can consume.">
              <Button
                size="small"
                type="text"
                icon={<QuestionCircleOutlined />}
                aria-label="Help: CPU Limit"
                style={{ color: COLORS.textMuted }}
              />
            </Tooltip>
          </Space>
          <InputNumber
            style={{ width: 220, maxWidth: '45%' }}
            disabled={!form.applyResourceLimit || !form.cpuEnabled}
            value={form.cpuPercent}
            controls
            min={form.cpuMin}
            max={form.cpuMax}
            onChange={(v) => updateForm((prev) => ({ ...prev, cpuPercent: Number(v ?? prev.cpuPercent) }))}
          />
        </div>
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
          The percentage is applied to this app's current CPU usage (a share of the whole machine), not to a single core.
        </Text>
        {pinnedCoreCount(limitDialog.app?.cpu_affinity) > 0 && (
          <Alert
            type="info"
            showIcon
            style={{ marginTop: 8 }}
            message={`This app is pinned to ${pinnedCoreCount(limitDialog.app?.cpu_affinity)} core(s) (CPU ${limitDialog.app?.cpu_affinity?.join(' ; ')}) by CPU Affinity.`}
            description="Its CPU use cannot exceed those cores, so a CPU limit that works out to more than that has no effect; the smaller of the two applies."
          />
        )}
      </div>}

      {resourceSectionExpanded && <div style={{ marginLeft: 14, paddingLeft: 12, borderLeft: `2px solid ${COLORS.border}` }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Space size={4}>
            <Checkbox
              checked={form.memEnabled}
              disabled={!form.applyResourceLimit}
              onChange={(e) => updateForm((prev) => ({ ...prev, memEnabled: e.target.checked }))}
            >
              <Text strong>Memory Limit (%)</Text>
            </Checkbox>
            <Tooltip title="Controls the memory pressure boundary for this app.">
              <Button
                size="small"
                type="text"
                icon={<QuestionCircleOutlined />}
                aria-label="Help: Memory Limit"
                style={{ color: COLORS.textMuted }}
              />
            </Tooltip>
          </Space>
          <InputNumber
            style={{ width: 220, maxWidth: '45%' }}
            disabled={!form.applyResourceLimit || !form.memEnabled}
            value={form.memPercent}
            controls
            min={form.memMin}
            max={form.memMax}
            onChange={(v) => updateForm((prev) => ({ ...prev, memPercent: Number(v ?? prev.memPercent) }))}
          />
        </div>
      </div>}

      {resourceSectionExpanded && <div style={{ marginLeft: 14, paddingLeft: 12, borderLeft: `2px solid ${COLORS.border}` }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <Space size={4}>
            <Checkbox
              checked={form.diskEnabled}
              disabled={!form.applyResourceLimit}
              onChange={(e) => updateForm((prev) => ({ ...prev, diskEnabled: e.target.checked }))}
            >
              <Text strong>Disk IO Limit</Text>
            </Checkbox>
            <Tooltip title="Controls disk throughput and IOPS caps for this app.">
              <Button
                size="small"
                type="text"
                icon={<QuestionCircleOutlined />}
                aria-label="Help: Disk IO Limit"
                style={{ color: COLORS.textMuted }}
              />
            </Tooltip>
          </Space>
        </div>
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 6 }}>
          {form.diskDetected
            ? 'This application is experiencing significant disk I/O pressure; applying limits is recommended.'
            : 'This application currently shows low disk I/O pressure, so applying limits is not recommended.'}
        </Text>
        <Row gutter={[8, 8]} style={{ marginTop: 8 }}>
          <Col span={12}>
            <InputNumber
              style={{ width: '100%' }}
              addonBefore="Write"
              addonAfter="MB/s"
              controls
              disabled={!form.applyResourceLimit || !form.diskEnabled}
              min={1}
              value={form.writeMbps}
              onChange={(v) => updateForm((prev) => ({ ...prev, writeMbps: Number(v ?? prev.writeMbps) }))}
            />
          </Col>
          <Col span={12}>
            <InputNumber
              style={{ width: '100%' }}
              addonBefore="Read"
              addonAfter="MB/s"
              controls
              disabled={!form.applyResourceLimit || !form.diskEnabled}
              min={1}
              value={form.readMbps}
              onChange={(v) => updateForm((prev) => ({ ...prev, readMbps: Number(v ?? prev.readMbps) }))}
            />
          </Col>
          <Col span={12}>
            <InputNumber
              style={{ width: '100%' }}
              addonBefore="Write IOPS"
              controls
              disabled={!form.applyResourceLimit || !form.diskEnabled}
              min={1}
              value={form.writeIops}
              onChange={(v) => updateForm((prev) => ({ ...prev, writeIops: Number(v ?? prev.writeIops) }))}
            />
          </Col>
          <Col span={12}>
            <InputNumber
              style={{ width: '100%' }}
              addonBefore="Read IOPS"
              controls
              disabled={!form.applyResourceLimit || !form.diskEnabled}
              min={1}
              value={form.readIops}
              onChange={(v) => updateForm((prev) => ({ ...prev, readIops: Number(v ?? prev.readIops) }))}
            />
          </Col>
        </Row>
      </div>}

      <div>
        <Space size={8} align="center">
          <Button
            size="small"
            type="text"
            onClick={() => setNetworkSectionExpanded((prev) => !prev)}
            icon={networkSectionExpanded
              ? <DownOutlined style={{ color: COLORS.textMuted, fontSize: 12 }} />
              : <RightOutlined style={{ color: COLORS.textMuted, fontSize: 12 }} />}
            aria-label="Toggle network priority settings"
          />
          <Space size={4}>
            <Text strong>Update Network Priority</Text>
            <Tooltip title={networkControlEnabled
              ? 'Expand this section to review or adjust the app network priority.'
              : 'Enable Network control in Settings > Control Policy > Network Control first.'}
            >
              <Button
                size="small"
                type="text"
                icon={<QuestionCircleOutlined />}
                aria-label="Help: Update Network Priority"
                style={{ color: COLORS.textMuted }}
              />
            </Tooltip>
          </Space>
        </Space>
        {networkSectionExpanded && <div style={{
          marginTop: 8,
          marginLeft: 14,
          paddingLeft: 12,
          borderLeft: `2px solid ${COLORS.border}`,
          opacity: networkControlEnabled ? 1 : 0.6,
        }}>
          {!networkControlEnabled && (
            <Text type="warning" style={{ display: 'block', marginBottom: 6 }}>
              Network Control is OFF. Network priority policy is currently not applied.
            </Text>
          )}
          <Text style={{ display: 'block', marginBottom: 10 }}>
            <Text strong>Current Network Priority:</Text>{' '}
            <Text style={{ color: networkPriorityColor(currentNetworkPriority) }}>
              {currentNetworkPriority.toUpperCase()}
            </Text>
            <Text type="secondary"> | </Text>
            <Text type="secondary">
              <Text strong>Bandwidth Range:</Text> {formatPercentNumber(currentNetworkRange.min)}% - {formatPercentNumber(currentNetworkRange.max)}%
            </Text>
          </Text>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 2 }}>
            <Text strong>Adjust Network Priority</Text>
            <Tooltip title={networkControlEnabled
              ? 'Higher priority levels allow higher bandwidth ranges, while lower levels limit speed to save resources.'
              : 'Enable Network control in Settings > Control Policy > Network Control first.'}
            >
              <QuestionCircleOutlined style={{ color: COLORS.textMuted, fontSize: 12 }} />
            </Tooltip>
            <Tooltip title={networkControlEnabled ? 'Select the app network priority to apply.' : 'Enable Network control in Settings > Control Policy > Network Control first.'}>
              <Select
                value={selectedNetworkPriority}
                onChange={(v) => updateForm((prev) => ({ ...prev, networkPriority: v }))}
                style={{ width: 220 }}
                styles={{ popup: { root: { background: COLORS.panelBg } } }}
                disabled={!networkControlEnabled}
              >
                {NETWORK_PRIORITY_OPTIONS.map((opt) => (
                  <Option key={opt.value} value={opt.value}>
                    <span style={{ color: opt.color }}>{opt.label}</span>
                  </Option>
                ))}
              </Select>
            </Tooltip>
          </div>
          <div style={{ marginTop: 10, border: `1px solid ${COLORS.border}`, borderRadius: 6, padding: '8px 10px' }}>
            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
              Global bandwidth ranges (read-only in this dialog)
            </Text>
            <Row gutter={8} style={{ marginBottom: 6 }}>
              <Col flex="150px">
                <Text style={{ fontSize: 12, color: COLORS.textMuted, fontWeight: 600, whiteSpace: 'nowrap' }}>Network Priority</Text>
              </Col>
              <Col flex="auto">
                <Space size={4} align="center">
                  <Text style={{ fontSize: 12, color: COLORS.textMuted, fontWeight: 600 }}>
                    Bandwidth Range (%)
                  </Text>
                  <Tooltip title="Network bandwidth range is calculated as a percentage of each NIC's link speed.">
                    <QuestionCircleOutlined style={{ color: COLORS.textMuted, fontSize: 12 }} />
                  </Tooltip>
                </Space>
              </Col>
            </Row>
            <Space direction="vertical" size={6} style={{ width: '100%' }}>
              {NETWORK_CLASS_ORDER.filter((level) => level !== 'system').map((level) => {
                const range = networkBandwidthRanges[level]
                const isSelected = level === selectedNetworkPriority
                return (
                  <Row key={`network-class-${level}`} gutter={8} align="middle">
                    <Col flex="150px">
                      <Tag color={isSelected ? 'processing' : 'default'} style={{ marginInlineEnd: 0 }}>
                        {level.toUpperCase()}
                      </Tag>
                    </Col>
                    <Col flex="auto">
                      <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>
                        {formatPercentNumber(range.min)} - {formatPercentNumber(range.max)}
                      </Text>
                    </Col>
                  </Row>
                )
              })}
            </Space>
            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 8 }}>
              For advanced rule changes, please go to Settings &gt; Control Policy &gt; Network Control.
            </Text>
          </div>
        </div>}
      </div>
    </>
  )

  // Status quick-filter counts + rows for the unified management table. "Auto Control" holds
  // the apps the pressure engine is currently throttling (control_status === 'AUTO_LIMITED');
  // "Manual Control" holds every other controlled app (normal + manual limit).
  const autoControlledCount = controlledApps.filter((a) => a.control_status === 'AUTO_LIMITED').length
  const manualTabCount = controlledApps.filter((a) => a.control_status !== 'AUTO_LIMITED').length
  // Only hand-restored exemptions belong here; manual-limit exemptions already show up under
  // Manual Control, so listing them again would double-count the same app.
  const userRestoredExclusions = exclusions.filter((e) => e.reason === 'user_restore')
  const excludedTabCount = userRestoredExclusions.length
  // Apps the engine throttled that were never taken under management. Folded into the one
  // table as synthetic rows so "auto-limited" lives in exactly one place, not a second card.
  // A cgroup can contain several processes: when one is a registered app, it already renders
  // the shared Auto limit. Do not add a second "New discovery" row just because a different
  // process in that same cgroup was the sampled IO leader.
  const controlledAppIds = new Set(controlledApps.map((app) => app.app_id))
  const uncontrolledAutoLimited = autoLimitedApps.filter(
    (app) => !app.is_controlled
      && !controlledAppIds.has(app.app_id)
      && !controlledAppIds.has(app.effective_app_id),
  )
  const autoRowFor = (a: AutoLimitedApp): ControlRow => {
    const processRows: ProcessStatusRow[] = a.cgroups.map((cgroup) => {
      const scopeProcesses = a.scope_processes?.[cgroup] ?? []
      return {
        key: `${a.effective_app_id}:${cgroup}`,
        pid: scopeProcesses[0]?.pid ?? null,
        process_name: scopeProcesses[0]?.process_name ?? a.app_name,
        cmdline: scopeProcesses[0]?.cmdline ?? '',
        scope_processes: scopeProcesses,
        cgroup,
        runtime_status: scopeProcesses.length > 0 ? 'Running' : 'Stopped',
        limit_status: 'Limited',
        applied_at: a.limited_at,
        note: scopeProcesses.length > 0 ? 'Applied' : 'No running process found',
      }
    })
    const hasRunningProcess = processRows.some((row) => row.runtime_status === 'Running')
    return {
      app_id: a.app_id,
      app_name: a.app_name,
      cpu_usage: 0,
      memory_mb: 0,
      io_read_rate: 0,
      priority: a.priority,
      status: APP_STATUS.A_LIMITED,
      controlled: false,
      control_status: 'AUTO_LIMITED',
      effective: a.effective,
      auto_detail: a.auto_detail,
      limited_scopes: a.cgroups,
      app_summary_status: hasRunningProcess ? 'Limited' : 'No Running Process',
      runtime_hint: hasRunningProcess ? 'Running' : 'Stopped',
      process_status_rows: processRows,
      __auto: a,
      key: `auto:${a.effective_app_id}`,
    }
  }

  const controlledMatchingTab = controlledApps.filter((a) =>
    controlTab === 'auto'
      ? a.control_status === 'AUTO_LIMITED'
      : a.control_status !== 'AUTO_LIMITED'
  )
  // Unmanaged auto rows only make sense under "Auto Control": they have no DB identity yet.
  const showUncontrolled = controlTab === 'auto'
  const controlRows: ControlRow[] = [
    ...controlledMatchingTab.map((a) => ({ ...a, key: a.app_id } as ControlRow)),
    ...(showUncontrolled ? uncontrolledAutoLimited.map(autoRowFor) : []),
  ]
  const autoTabCount = autoControlledCount + uncontrolledAutoLimited.length

  if (!balancerEnabled) {
    return (
      <div style={{ padding: '16px 0' }}>
        <Alert
          type="info"
          showIcon
          message="Monitor-only mode"
          description="The current server is running in monitor-only mode; balancer control is not available."
        />
      </div>
    )
  }

  return (
    <div style={{ padding: '16px 0' }}>
      {contextHolder}

      {error && (
        <Alert
          message="API Error"
          description={error}
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
        />
      )}

      <Card
        title={
          <Text style={{ color: COLORS.text, fontSize: 13, fontWeight: 600 }}>
            <PlusOutlined style={{ marginRight: 8, color: COLORS.accent }} />
            Add application
          </Text>
        }
        style={{
          background: COLORS.panelBg,
          border: `1px solid ${COLORS.border}`,
          borderRadius: 6,
          marginBottom: 12,
        }}
        headStyle={{ borderBottom: `1px solid ${COLORS.border}`, padding: '8px 16px', minHeight: 40 }}
        bodyStyle={{ padding: '28px 16px' }}
      >
        <Row justify="start" gutter={[12, 12]} wrap>
          <Col flex="auto" style={{ minWidth: 240 }}>
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              <Text type="secondary" style={{ fontSize: 14, display: 'block', textAlign: 'left', width: '100%' }}>
                Scan a running process, choose an application, then set its control policy.
              </Text>
              <div style={{ width: '100%', textAlign: 'center' }}>
                <Button
                  type="primary"
                  icon={<SearchOutlined />}
                  onClick={() => setWizardOpen(true)}
                  style={{ marginTop: 6 }}
                >
                  Scan running applications
                </Button>
              </div>
              <Text style={{ color: COLORS.textMuted, fontSize: 15, display: 'block', textAlign: 'center', width: '100%' }}>
                1. Scan processes &nbsp;→&nbsp; 2. Choose application &nbsp;→&nbsp; 3. Configure limits
              </Text>
            </Space>
          </Col>
        </Row>
      </Card>

      <AddAppWizard
        open={wizardOpen}
        initialKeyword={wizardKeyword ?? registerKeyword ?? undefined}
        onClose={() => {
          setWizardOpen(false)
          setWizardKeyword(null)
          onRegisterConsumed?.()
        }}
        onSuccess={async (result) => {
          if (result?.openLimit) {
            const localTarget = controlledApps.find((a) => a.app_id === result.appId)
              || controlledApps.find((a) => a.app_name === result.appName)

            let serverTarget: AppInfo | undefined
            if (!localTarget) {
              try {
                const latest = await api.getControlledApps()
                serverTarget = latest.find((a) => a.app_id === result.appId)
                  || latest.find((a) => a.app_name === result.appName)
              } catch (e) {
                console.error('[Balance] resolve target for limit dialog failed:', e)
              }
            }

            const fallbackTarget: AppInfo = {
              app_id: result.appId,
              app_name: result.appName,
              cpu_usage: 0,
              memory_mb: 0,
              io_read_rate: 0,
              priority: 'medium',
              status: APP_STATUS.RUNNING,
            }

            await handleResourceLimit(localTarget || serverTarget || fallbackTarget)
          }

          await fetchData()
        }}
      />

      <EditAppProcessesModal
        open={editApp !== null}
        app={editApp}
        onClose={() => setEditApp(null)}
        onSuccess={async () => {
          messageApi.success(`Application updated for ${editApp?.app_name || editApp?.app_id}`)
          await refreshControlled()
        }}
      />

      {/* App Cgroup control center — the merged, entity-centric management table. */}
      <Card
        title={
          // Single-line title, identical to the "Add application" header — the header's own
          // full-width borderBottom draws the divider; the tabs live in the card body below it.
          <Text style={{ color: COLORS.text, fontSize: 13, fontWeight: 600 }}>
            <ControlOutlined style={{ marginRight: 8, color: COLORS.accent }} />
            Application Control Center
          </Text>
        }
        style={{
          background: COLORS.panelBg,
          border: `1px solid ${COLORS.border}`,
          borderRadius: 6,
          marginBottom: 12,
        }}
        headStyle={{ borderBottom: `1px solid ${COLORS.border}`, padding: '8px 16px', minHeight: 40 }}
        bodyStyle={{ padding: '0' }}
      >
        <Tabs
          className="control-center-tabs"
          size="small"
          activeKey={controlTab}
          onChange={(k) => setControlTab(k as 'auto' | 'manual' | 'excluded')}
          tabBarStyle={{ padding: '0 16px', marginBottom: 0 }}
          items={[
            { key: 'manual', label: `🟠 Manual Control (${manualTabCount})` },
            { key: 'auto', label: `🔴 Auto Control (${autoTabCount})` },
            {
              key: 'excluded',
              label: (
                <Tooltip title={EXCLUDED_APPS_TOOLTIP}>
                  <span>{`⛔ Excluded (${excludedTabCount})`}</span>
                </Tooltip>
              ),
            },
          ]}
        />
        {controlTab === 'excluded' ? (
          <ExcludedAppsTable
            rows={userRestoredExclusions}
            loading={loading}
            onRemove={handleRemoveExclusion}
          />
        ) : (
        <Table
          columns={controlledColumns}
          dataSource={controlRows}
          loading={loading}
          size="small"
          pagination={false}
          scroll={{ x: 'max-content' }}
          rowClassName={(record, idx) =>
            [idx % 2 === 1 ? 'table-row-alt' : '', record.app_id === highlightAppId ? 'table-row-highlight' : '']
              .filter(Boolean)
              .join(' ')}
          expandable={{
            // Pin the expand-icon column width so the Pending Queue table can mirror it
            // exactly and keep its data columns aligned with this table.
            columnWidth: 48,
            expandedRowKeys: expandedProcessRows,
            onExpandedRowsChange: (keys) => {
              setExpandedProcessRows([...keys])
              // Per-instance churn of an app that keeps its status emits no SSE event,
              // and this tab does not poll — so pull the rows when the user asks to
              // see them.
              if (keys.length > expandedProcessRows.length) queueRefresh('controlled')
            },
            expandedRowRender: (record) => {
              const rows = record.process_status_rows ?? []
              const selected = selectedTargetCgroups[record.app_id] ?? []
              const selectedSet = new Set(selected)
              const selectedRowKeys = rows
                .filter((row) => selectedSet.has((row.cgroup || '').trim()))
                .map((row) => row.key)

              return (
                <Table
                  columns={processStatusColumns}
                  dataSource={rows}
                  scroll={compactWorkspace ? { x: 'max-content' } : undefined}
                  size="small"
                  pagination={false}
                  rowKey={(row) => row.key}
                  rowSelection={{
                    selectedRowKeys,
                    onChange: (_keys, selectedRows) => {
                      const nextCgroups = Array.from(new Set(
                        selectedRows
                          .filter((row) => row.runtime_status === 'Running')
                          .map((row) => (row.cgroup || '').trim())
                          .filter(Boolean)
                      ))
                      setSelectedTargetCgroups((prev) => ({ ...prev, [record.app_id]: nextCgroups }))
                    },
                    getCheckboxProps: (row) => ({
                      disabled: row.runtime_status !== 'Running' || !(row.cgroup || '').trim(),
                    }),
                  }}
                  locale={{
                    emptyText: (
                      <div style={{ padding: 12, color: COLORS.textMuted, textAlign: 'center', fontSize: 12 }}>
                        No live process details for this app
                      </div>
                    ),
                  }}
                />
              )
            },
            rowExpandable: (record) => (record.process_status_rows?.length ?? 0) > 0,
          }}
          locale={{
            emptyText: (
              <div style={{ padding: 30, color: COLORS.textMuted, textAlign: 'center' }}>
                No applications are being controlled. Use “Find new application” above to start.
              </div>
            ),
          }}
        />
        )}
      </Card>

      {/* Right-side detail drawer: a plain-language breakdown of a row's active / passive /
          effective limits, opened by clicking the row. Read-only; all actions stay in the row. */}
      <Drawer
        title={detailApp ? `${detailApp.app_name} — control detail` : 'Control detail'}
        placement="right"
        width={420}
        open={detailApp !== null}
        onClose={() => setDetailApp(null)}
      >
        {detailApp && (() => {
          const cs = detailApp.control_status ?? 'NORMAL'
          const eff = detailApp.effective
          const auto = detailApp.auto_detail
          const isManagedAuto = cs === 'AUTO_LIMITED' && detailApp.controlled !== false
          const statusLabel =
            cs === 'AUTO_LIMITED' ? '⚠️ Auto circuit-breaking'
              : cs === 'MANUAL_LIMITED' ? (detailApp.frozen ? '🧊 Frozen (limits kept)' : '🟠 Manual limited')
                : '🟢 Normal (not limited)'
          const partial = auto?.partial_parts
          const formatPercent = (rate: number | null | undefined) =>
            rate == null ? '—' : `${Math.round(rate * 100)}%`
          const formatIoLimit = (value: number | null | undefined, unit: string) =>
            value == null ? null : `${value} ${unit}`
          const scopeProcesses = (detailApp.process_status_rows ?? []).reduce<Record<string, ProcessStatusRow[]>>(
            (scopes, process) => {
              const cgroup = (process.cgroup || '').trim()
              if (cgroup) {
                ;(scopes[cgroup] ??= []).push(process)
              }
              return scopes
            },
            {},
          )
          const appScopes = Array.from(new Set([
            ...Object.keys(scopeProcesses),
            ...(detailApp.limited_scopes ?? []),
          ].filter(Boolean)))
          return (
            <Descriptions column={1} size="small" bordered
              labelStyle={{ width: 150 }}>
              <Descriptions.Item label="Status">{statusLabel}</Descriptions.Item>
              <Descriptions.Item label="Priority">
                <PriorityTag priority={detailApp.priority} />
              </Descriptions.Item>
              <Descriptions.Item label="CPU / Memory">
                {eff?.cpu_mem?.limited
                  ? `CPU cap ${formatPercent(eff.cpu_mem.cpu_rate)}; Memory cap ${formatPercent(eff.cpu_mem.mem_rate)}`
                  : 'Not limited'}
              </Descriptions.Item>
              <Descriptions.Item label="Disk I/O">
                {eff?.disk_io?.limited
                  ? (
                    <Space direction="vertical" size={2}>
                      <Text>{`Disks: ${eff.disk_io.disks.length ? eff.disk_io.disks.join(', ') : 'all disks'}`}</Text>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {[
                          formatIoLimit(eff.disk_io.read_mb_s, 'MB/s read'),
                          formatIoLimit(eff.disk_io.write_mb_s, 'MB/s write'),
                          formatIoLimit(eff.disk_io.read_iops, 'read IOPS'),
                          formatIoLimit(eff.disk_io.write_iops, 'write IOPS'),
                        ].filter(Boolean).join('; ') || 'Device scope limited; no rate cap configured'}
                      </Text>
                    </Space>
                  )
                  : 'Not limited'}
              </Descriptions.Item>
              {cs === 'AUTO_LIMITED' && auto && (
                <>
                  <Descriptions.Item label="Limit Reason">
                    {auto.limit_reason === 'disk_pressure' ? 'Disk I/O pressure' : 'System pressure'}
                    {auto.pressure_level ? ` · level ${auto.pressure_level}` : ''}
                  </Descriptions.Item>
                  <Descriptions.Item label="Staged recovery">
                    {partial
                      ? `CPU/Mem ${partial.sys ? 'partially released' : 'limit remains active'}; `
                        + `Disk I/O ${partial.disk_io ? 'partially released' : 'limit remains active'}`
                      : '—'}
                  </Descriptions.Item>
                </>
              )}
              <Descriptions.Item label="Scopes (cgroups)">
                {appScopes.length > 0 ? (
                  <Space direction="vertical" size={6} style={{ width: '100%' }}>
                    {appScopes.map((cgroup) => {
                      const processes = (scopeProcesses[cgroup] ?? []).flatMap((scope) =>
                        scope.scope_processes?.length ? scope.scope_processes.map((process) => ({ ...scope, ...process })) : [scope],
                      )
                      return (
                      <div key={cgroup}>
                        <Text code style={{ fontSize: 12, overflowWrap: 'anywhere' }}>{cgroup}</Text>
                        <div style={{ color: COLORS.textMuted, fontSize: 12, marginTop: 3 }}>
                          {processes.length > 0
                            ? processes.map((process) => `${deriveDisplayProcessName(process)} (PID ${process.pid ?? '—'})`).join(', ')
                            : 'No running process found in this scope'}
                        </div>
                      </div>
                      )
                    })}
                  </Space>
                ) : 'No process scopes found'}
              </Descriptions.Item>
            </Descriptions>
          )
        })()}
      </Drawer>

      {pendingApps.length === 0 ? (
        <Text style={{ color: COLORS.textMuted, fontSize: 12, display: 'block', textAlign: 'right' }}>
          Pending queue: empty
        </Text>
      ) : (
      <Card
        title={
          <Text style={{ color: COLORS.text, fontSize: 13, fontWeight: 600 }}>
            <ThunderboltOutlined style={{ marginRight: 8, color: COLORS.yellow }} />
            Pending Queue
            <Tag color="processing" style={{ marginLeft: 8 }}>
              {pendingApps.length}
            </Tag>
          </Text>
        }
        style={{
          background: COLORS.panelBg,
          border: `1px solid ${COLORS.yellow}44`,
          borderRadius: 6,
        }}
        headStyle={{ borderBottom: `1px solid ${COLORS.border}`, padding: '8px 16px', minHeight: 40 }}
        bodyStyle={{ padding: '0' }}
      >
        <Table
          columns={pendingColumns}
          dataSource={pendingApps.map((a) => ({ ...a, key: a.app_id }))}
          size="small"
          pagination={false}
          scroll={{ x: 'max-content' }}
          rowClassName={(_, idx) => (idx % 2 === 1 ? 'table-row-alt' : '')}
        />
      </Card>
      )}

      <Modal
        title={`CPU affinity - ${affinityDialog.app?.app_name ?? ''}`}
        open={affinityDialog.app !== null}
        width={560}
        okText={affinityDialog.choice === 'restore' ? 'Restore' : 'Apply'}
        okButtonProps={{
          disabled: affinityDialog.status !== 'ready' || !affinityDialog.choice
            || (affinityDialog.choice === 'custom' && affinityDialog.selected.length === 0),
        }}
        onCancel={() => setAffinityDialog(AFFINITY_DIALOG_CLOSED)}
        onOk={submitAffinity}
      >
        {affinityDialog.status === 'loading' && (
          <div style={{ textAlign: 'center', padding: 24 }}><Spin /></div>
        )}
        {affinityDialog.status === 'error' && (
          <Alert
            type="warning"
            showIcon
            message="Cannot read this machine's CPU core layout"
            description="CPU affinity cannot be set right now. Check that the balancer service is running and try again."
          />
        )}
        {affinityDialog.status === 'ready' && (
          <>
            <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
              Choose which CPU cores this app may run on. It takes effect immediately on the running
              app, and is removed automatically when the app exits. It limits <i>where</i> threads run,
              not how many the app creates: an app with 2 busy threads uses at most 2 cores even if
              more are allowed.
            </Typography.Paragraph>
            {affinityDialog.app?.effective?.cpu_mem?.limited && (
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 12 }}
                message="This app also has a CPU limit."
                description="CPU Affinity decides where it runs, the limit decides how much CPU time it gets. The effective cap is the smaller of the two."
              />
            )}
            <AffinityCurrent cgroups={affinityDialog.cgroups} />
            <Radio.Group
              value={affinityDialog.choice}
              onChange={(e) => setAffinityDialog((prev) => ({ ...prev, choice: e.target.value }))}
              style={{ display: 'block' }}
            >
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                {(['p', 'e', 'lpe'] as const)
                  .filter((c) => c in affinityDialog.classes)
                  .map((c) => (
                    <Radio key={c} value={c}>
                      <b>{AFFINITY_CLASS_INFO[c].title}</b> (CPU {affinityDialog.classes[c]})
                      <div style={{ fontSize: 12, color: COLORS.textMuted }}>{AFFINITY_CLASS_INFO[c].hint}</div>
                    </Radio>
                  ))}
                <Radio value="custom">
                  <b>Specific cores</b>
                  <div style={{ fontSize: 12, color: COLORS.textMuted }}>
                    Tick individual cores below, e.g. one P core for one app and another for a second app.
                  </div>
                </Radio>
                <Radio value="restore">
                  <b>Restore original (default)</b>
                  <div style={{ fontSize: 12, color: COLORS.textMuted }}>
                    <RestorePreview cgroups={affinityDialog.cgroups} />
                  </div>
                </Radio>
              </Space>
            </Radio.Group>
            {affinityDialog.choice === 'custom' && (
              <Checkbox.Group
                value={affinityDialog.selected}
                onChange={(vals) => setAffinityDialog((prev) => ({ ...prev, selected: vals as number[] }))}
                style={{ display: 'block', marginTop: 12 }}
              >
                {(['p', 'e', 'lpe'] as const)
                  .filter((c) => c in affinityDialog.classes)
                  .map((c) => (
                    <div key={c} style={{ marginBottom: 6 }}>
                      <Typography.Text strong>{AFFINITY_CLASS_INFO[c].title}: </Typography.Text>
                      {expandCpuList(affinityDialog.classes[c]).map((core) => (
                        <Checkbox key={core} value={core}>CPU {core}</Checkbox>
                      ))}
                    </div>
                  ))}
              </Checkbox.Group>
            )}
          </>
        )}
      </Modal>

      <Modal
        title={(
          <Space size={8}>
            {limitDialogTitle}
            <Tooltip
              title={(
                <div>
                  <div>1) Use switches to enable/disable each resource limit for this apply action.</div>
                  <div>2) Default values are aligned with the balancer's passive control policy.</div>
                  <div>3) Please tune limit values based on the application workload. CPU/Memory and Disk I/O limits can affect GPU utilization, so configure them according to your performance goals.</div>
                </div>
              )}
            >
              <Button
                size="small"
                type="text"
                icon={<QuestionCircleOutlined />}
                aria-label="Help: Configuration Guidelines"
                style={{ color: COLORS.textMuted }}
              />
            </Tooltip>
          </Space>
        )}
        open={limitDialog.open}
        onCancel={() => {
          if (limitDialog.submitting) return
          setLimitDialog({ app: null, open: false, loadingProfile: false, submitting: false })
          setResourceSectionExpanded(false)
          setNetworkSectionExpanded(false)
        }}
        onOk={submitResourceLimit}
        okText="Apply Changes"
        confirmLoading={limitDialog.submitting}
        maskClosable={false}
        // The request installs cgroup limits and then reclaims memory, so it
        // cannot be cancelled. onCancel already refuses while it is in flight;
        // these two stop Cancel and the X from *looking* available meanwhile.
        cancelButtonProps={{ disabled: limitDialog.submitting }}
        closable={!limitDialog.submitting}
        width={760}
        destroyOnClose
      >
        <div style={{ opacity: limitDialog.loadingProfile ? 0.6 : 1, pointerEvents: limitDialog.loadingProfile ? 'none' : 'auto' }}>
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            {limitDialog.submitting && (
              <Alert
                type="info"
                showIcon
                message="Applying resource limits"
                description="Waiting for systemd to update the app's cgroup. This can take several seconds; keep this dialog open."
              />
            )}
            <Text type="secondary" style={{ fontSize: 12 }}>
              The limit is applied to all instances of this app running right now (matched by name).
              Use the process checkboxes in the expanded app row to choose specific scopes.
            </Text>
            {targetRows.length > 0 && (
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>Target</Text>
                <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {targetRows.map((row, idx) => (
                    <Tag
                      key={`target-row-${idx}`}
                      color="blue"
                      style={{ marginBottom: 0, maxWidth: '100%', whiteSpace: 'normal', wordBreak: 'break-all' }}
                    >
                      Process: {row.processName || '-'} | Scope: {row.cgroupId || '-'}
                    </Tag>
                  ))}
                </div>
              </div>
            )}
            {renderLimitSettings(limitForm, setLimitForm)}
          </Space>
        </div>
      </Modal>

      <style>{`
        .table-row-alt td { background: ${COLORS.rowAlt} !important; }
        .table-row-highlight td { background: ${COLORS.yellow}33 !important; transition: background 0.4s ease; }
        .ant-table-tbody > tr.table-row-highlight:hover > td { background: ${COLORS.yellow}44 !important; }
        .ant-table-row { cursor: pointer; }
        .ant-table { background: transparent !important; }
        .ant-table-thead > tr > th {
          background: ${COLORS.headerBg} !important;
          color: ${COLORS.textMuted} !important;
          font-size: 11px !important;
          text-transform: uppercase;
          letter-spacing: 0.5px;
          border-bottom: 1px solid ${COLORS.border} !important;
        }
        .ant-table-tbody > tr > td {
          border-bottom: 1px solid ${COLORS.border}55 !important;
        }
        .ant-table-tbody > tr:hover > td {
          background: ${COLORS.rowAlt} !important;
        }
        .control-center-tabs .ant-tabs-nav::before { border-bottom: none !important; }
      `}</style>
    </div>
  )
}
