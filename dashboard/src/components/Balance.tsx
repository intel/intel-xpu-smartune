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
  Switch,
  Checkbox,
  InputNumber,
  Popconfirm,
  Tabs,
  Drawer,
  Descriptions,
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
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { COLORS } from '../styles/theme'
import { api } from '../api/client'
import type {
  AppInfo,
  AutoLimitedApp,
  AutoLimitedAppsData,
  AutoLimitExclusion,
  ControlStatus,
  ResourceLimitProfileData,
  PassiveControlData,
  ProcessStatusRow,
} from '../api/types'
import { useAppEvents } from '../hooks/useAppEvents'
import { useGlobalConfigNotices } from '../hooks/useGlobalConfigNotices'
import { AddAppWizard } from './AddAppWizard'
import { EditAppProcessesModal } from './EditAppProcessesModal'

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

// Shown on the manual buttons while the pressure engine owns a row's cgroup. Tells the
// operator how to take over safely instead of racing the auto-limit writer.
const AUTO_LOCK_TOOLTIP =
  'System pressure is high and automatic circuit-breaking has taken over. To control this ' +
  'app by hand, click "Take Control" on this row to move it to manual control (its limit is ' +
  'kept in place), or turn off the global passive-control switch at the top right.'

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

interface PassiveControlPanelProps {
  active: boolean
}

interface NetworkControlPanelProps {
  active: boolean
  networkControlEnabled: boolean
}

// Compact card with a single Switch that gates the balancer's pressure-driven
// auto-limit/auto-restore loop.  Network shaping and manual per-app limits are
// not affected; flipping this only stops the passive top-consumer hunt.
function PassiveControlPanel({ active }: PassiveControlPanelProps) {
  const { publishNotice } = useGlobalConfigNotices()
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [updatedAt, setUpdatedAt] = useState<number | undefined>(undefined)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await api.getPassiveControl()
      setEnabled(Boolean(data.enabled))
      setUpdatedAt(data.updated_at)
    } catch (e) {
      console.error('[Balance] load passive control failed:', e)
      message.error('Failed to load passive control state')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (active) load()
  }, [active, load])

  const handleToggle = async (checked: boolean) => {
    setSaving(true)
    try {
      const result = await api.updatePassiveControl(checked, updatedAt)
      if (result.status === 'conflict') {
        const current = (result.current ?? {}) as PassiveControlData
        const newTs = current.updated_at
        const tsLabel = formatPassiveControlTimestamp(newTs)
        Modal.confirm({
          title: 'Setting changed by another client',
          content: (
            <div>
              <p>
                Passive resource control was updated to{' '}
                <b>{current.enabled ? 'enabled' : 'disabled'}</b> at <b>{tsLabel}</b>.
                Reload to pick up the latest value before changing it again.
              </p>
            </div>
          ),
          okText: 'Reload latest value',
          cancelText: 'Cancel',
          onOk: () => {
            setEnabled(Boolean(current.enabled))
            setUpdatedAt(newTs)
            publishNotice({
              title: 'Passive resource control updated',
              description: `Another client changed passive control to ${current.enabled ? 'enabled' : 'disabled'} at ${tsLabel}.`,
              scope: 'passive_control',
              updatedAt: newTs,
            })
          },
        })
        return
      }
      const response = result.data
      if (response.success) {
        setEnabled(response.enabled)
        setUpdatedAt(response.updated_at)
        publishNotice({
          title: 'Passive resource control updated',
          description: response.enabled
            ? 'Passive resource control is now enabled.'
            : 'Passive resource control is now disabled.',
          scope: 'passive_control',
          updatedAt: response.updated_at,
        })
        message.success(
          response.enabled
            ? 'Passive resource control enabled'
            : 'Passive resource control disabled'
        )
        if (!response.enabled) {
          // Disabling does NOT slam every cgroup open — that would let the suppressed
          // load stampede back under pressure. Instead every auto-limited app is handed
          // to you as a manual limit with its caps intact; restore each when it's safe.
          message.info({
            content: 'Auto-limited apps are being converted to manual locks (limits kept in '
              + 'place). Restore them from the table when it is safe.',
            duration: 8,
          })
        }
      } else {
        message.error('Failed to update passive control state')
      }
    } catch (e) {
      console.error('[Balance] update passive control failed:', e)
      message.error('Failed to update passive control state')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card
      style={{
        background: COLORS.panelBg,
        border: `1px solid ${COLORS.border}`,
        borderRadius: 6,
        marginBottom: 12,
      }}
      bodyStyle={{ padding: '12px 16px' }}
    >
      <Row gutter={[12, 8]} align="middle" justify="space-between">
        <Col flex="auto">
          <Space size={8} align="center">
            <Text style={{ color: COLORS.text, fontSize: 13, fontWeight: 600 }}>
              Auto System Control
            </Text>
            <Tooltip title="When ON, the balancer monitors system pressure and automatically limits/restores the top resource consumers. When OFF, network shaping and manual per-app limits still work, but the pressure-driven auto-limit loop is paused.">
              <QuestionCircleOutlined style={{ color: COLORS.textMuted }} />
            </Tooltip>
          </Space>
          <div style={{ marginTop: 2 }}>
            <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>
              {updatedAt
                ? `Last changed: ${formatPassiveControlTimestamp(updatedAt)}`
                : 'Never changed via dashboard'}
            </Text>
          </div>
        </Col>
        <Col>
          <Switch
            checked={Boolean(enabled)}
            disabled={loading || saving || enabled === null}
            loading={saving}
            onChange={handleToggle}
            checkedChildren="On"
            unCheckedChildren="Off"
          />
        </Col>
      </Row>
    </Card>
  )
}

// Quick runtime toggle for pressure-driven auto network shaping.
function NetworkControlPanel({ active, networkControlEnabled }: NetworkControlPanelProps) {
  const { publishNotice } = useGlobalConfigNotices()
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [updatedAt, setUpdatedAt] = useState<number | undefined>(undefined)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const disabledByMaster = !networkControlEnabled

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await api.getConfig<{
        enable_network_pressure_shaping: boolean
        updated_at?: number
      }>('network_control')
      const nextEnabled = Boolean(data.enable_network_pressure_shaping)
      setEnabled(nextEnabled)
      setUpdatedAt(data.updated_at)
    } catch (e) {
      console.error('[Balance] load auto network control failed:', e)
      message.error('Failed to load auto network control state')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (active) load()
  }, [active, load])

  const handleToggle = async (checked: boolean) => {
    if (disabledByMaster) {
      message.warning('Network control is disabled. Enable it in Settings / Control Policy first.')
      return
    }

    setSaving(true)
    try {
      const result = await api.updateConfig<{
        enable_network_pressure_shaping?: boolean
        updated_at: number
      }>(
        'network_control',
        { enable_network_pressure_shaping: checked },
        updatedAt,
      )

      if (result.status === 'conflict') {
        const current = (result.current ?? {}) as {
          enable_network_pressure_shaping?: boolean
          updated_at?: number
        }
        const latestEnabled = Boolean(current.enable_network_pressure_shaping)
        const latestTs = current.updated_at
        const tsLabel = formatPassiveControlTimestamp(latestTs)
        Modal.confirm({
          title: 'Setting changed by another client',
          content: (
            <div>
              <p>
                Auto network control was updated to{' '}
                <b>{latestEnabled ? 'enabled' : 'disabled'}</b> at <b>{tsLabel}</b>.
                Reload to pick up the latest value before changing it again.
              </p>
            </div>
          ),
          okText: 'Reload latest value',
          cancelText: 'Cancel',
          onOk: () => {
            setEnabled(latestEnabled)
            setUpdatedAt(latestTs)
            publishNotice({
              title: 'Auto network control updated',
              description: `Another client changed auto network control to ${latestEnabled ? 'enabled' : 'disabled'} at ${tsLabel}.`,
              scope: 'network_control',
              updatedAt: latestTs,
            })
          },
        })
        return
      }

      const response = result.data
      if (response.success) {
        const nextEnabled = Boolean(response.enable_network_pressure_shaping ?? checked)
        setEnabled(nextEnabled)
        setUpdatedAt(response.updated_at)
        publishNotice({
          title: 'Auto network control updated',
          description: nextEnabled
            ? 'Auto network control is now enabled.'
            : 'Auto network control is now disabled.',
          scope: 'network_control',
          updatedAt: response.updated_at,
        })
        message.success(nextEnabled ? 'Auto network control enabled' : 'Auto network control disabled')
      } else {
        message.error('Failed to update auto network control state')
      }
    } catch (e) {
      console.error('[Balance] update auto network control failed:', e)
      message.error('Failed to update auto network control state')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card
      style={{
        background: COLORS.panelBg,
        border: `1px solid ${COLORS.border}`,
        borderRadius: 6,
        marginBottom: 12,
      }}
      bodyStyle={{ padding: '12px 16px' }}
    >
      <Row gutter={[12, 8]} align="middle" justify="space-between">
        <Col flex="auto">
          <Space size={8} align="center">
            <Text style={{ color: COLORS.text, fontSize: 13, fontWeight: 600 }}>
              Auto Network Control
            </Text>
            <Tooltip title="Pressure-driven automatic network shaping. Network control master switch remains in Settings / Control Policy.">
              <QuestionCircleOutlined style={{ color: COLORS.textMuted }} />
            </Tooltip>
          </Space>
          <div style={{ marginTop: 2 }}>
            <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>
              {updatedAt
                ? `Last changed: ${formatPassiveControlTimestamp(updatedAt)}`
                : 'Never changed via dashboard'}
            </Text>
          </div>
        </Col>
        <Col>
          <Tooltip title={disabledByMaster ? 'Enable Network control in Settings / Control Policy first' : ''}>
            <Switch
              checked={Boolean(enabled)}
              disabled={disabledByMaster || loading || saving || enabled === null}
              loading={saving}
              onChange={handleToggle}
              checkedChildren="On"
              unCheckedChildren="Off"
            />
          </Tooltip>
        </Col>
      </Row>
    </Card>
  )
}


export default function Balance({
  active,
  balancerEnabled = true,
  registerKeyword,
  onRegisterConsumed,
}: Props) {
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

  // Add app form state
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

  // Narrow refreshes used by the SSE handler. This tab has no polling fallback, so an
  // event pulls exactly the slice it can have changed and nothing else.
  const refreshAutoLimited = useCallback(async () => {
    try {
      applyAutoLimited(await api.getAutoLimitedApps())
    } catch (e: unknown) {
      console.error('[Balance] auto-limited refresh failed:', e)
    }
  }, [applyAutoLimited])

  const refreshControlled = useCallback(async () => {
    try {
      applyControlled((await api.getControlledApps()) ?? [])
    } catch (e: unknown) {
      console.error('[Balance] controlled refresh failed:', e)
    }
  }, [applyControlled])

  const refreshPending = useCallback(async () => {
    try {
      setPendingApps((await api.getPendingApps()) ?? [])
    } catch (e: unknown) {
      console.error('[Balance] pending refresh failed:', e)
    }
  }, [])

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
    const timer = window.setInterval(() => {
      refreshControlled()
    }, 3000)
    return () => window.clearInterval(timer)
  }, [active, refreshControlled])

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
        // Note: the reverse (status === PENDING) needs refreshPending(), because adding
        // to pendingApps requires a full AppInfo object the SSE payload does not carry.
        if (event.status !== APP_STATUS.PENDING) {
          setPendingApps((prev) => prev.filter((app) => app.app_id !== event.app_id))
        } else {
          refreshPending()
        }

        // Refresh only what this event can have changed. The controlled list carries the
        // per-process rows and limit flags, so it is refreshed for every status change;
        // the auto-limited list only moves when a limit is applied or lifted.
        refreshControlled()
        if (
          event.status === APP_STATUS.LIMITED ||
          event.status === APP_STATUS.A_LIMITED ||
          event.status === APP_STATUS.RUNNING ||
          event.status === APP_STATUS.STOPPED
        ) {
          refreshAutoLimited()
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
          refreshAutoLimited()
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
    }, [messageApi, refreshAutoLimited, refreshControlled, refreshPending]),
    active
  )

  function withLoading(key: string, fn: () => Promise<void>) {
    return async () => {
      setActionLoading((prev) => ({ ...prev, [key]: true }))
      try {
        await fn()
        await fetchData()
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

  const submitResourceLimit = async () => {
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
  // the table entirely, so refresh both sources after the restore.
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
      messageApi.warning({ content: AUTO_LIMIT_EXCLUSION_HINT, duration: 8 })
      await Promise.allSettled([refreshAutoLimited(), refreshControlled()])
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
      await Promise.allSettled([refreshAutoLimited(), refreshControlled()])
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
        await Promise.allSettled([refreshAutoLimited(), refreshControlled()])
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
      await Promise.allSettled([refreshAutoLimited(), refreshControlled()])
    })()

  const controlledColumns: ColumnsType<ControlRow> = [
    {
      title: 'App Name',
      dataIndex: 'app_name',
      key: 'app_name',
      width: 240,
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
      width: 150,
      render: (_: unknown, record: ControlRow) => <PriorityTag priority={record.priority} />,
    },
    {
      title: 'Status',
      key: 'status',
      width: 230,
      render: (_: unknown, record: AppInfo) => {
        const combined = deriveCombinedStatus(record)

        return (
          <Space size={6} wrap>
            {runtimeHintTag(combined.runtime)}
            {limitSummaryTag(combined.limitSummary)}
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
      title: 'Details',
      key: 'details',
      width: 100,
      align: 'center',
      render: (_: unknown, record: ControlRow) => (
        <Tooltip title="View current limits, trigger, and recovery state">
          <Button
            size="small"
            icon={<InfoCircleOutlined />}
            aria-label={`View control details for ${record.app_name || record.app_id}`}
            onClick={() => setDetailApp(record)}
          >
            Details
          </Button>
        </Tooltip>
      ),
    },
    {
      title: 'Actions',
      key: 'actions',
      width: 160,
      align: 'center',
      render: (_: unknown, record: ControlRow) => {
        // Unmanaged auto-limited row: it has no DB entry, so the managed actions (Edit /
        // Delete / Keep-Alive) don't apply yet. Offer exactly
        // two paths — take it under management (its live limit follows it, then the full
        // managed toolset unlocks), or release the limit now.
        if (record.controlled === false && record.__auto) {
          const auto = record.__auto
          return (
            <Space size={4} wrap={false}>
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

        const isAuto = record.control_status === 'AUTO_LIMITED'
        if (isAuto) {
          return (
            <Space size={4} wrap={false}>
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
        const isLimited = record.status === APP_STATUS.LIMITED || record.status === APP_STATUS.A_LIMITED

        return (
          <Space size={4} wrap={false}>
            {isLimited ? (
              <Tooltip title="Restore Resources">
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  loading={actionLoading[`restore-${record.app_id}`]}
                  onClick={() => handleResourceRestore(record)}
                  style={{ borderColor: COLORS.accent, color: COLORS.accent }}
                >
                  Restore
                </Button>
              </Tooltip>
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

            <Tooltip title={isAuto ? AUTO_LOCK_TOOLTIP : 'Delete completely (purges config + DB; needs the wizard to re-add)'}>
              <Button
                size="small"
                danger
                icon={isAuto ? <LockOutlined /> : <DeleteFilled />}
                disabled={isAuto}
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

  const pendingColumns: ColumnsType<AppInfo> = [
    {
      title: 'App Name',
      dataIndex: 'app_name',
      key: 'app_name',
      render: (name: string) => <Text style={{ color: COLORS.text }}>{name}</Text>,
    },
    {
      title: 'Priority',
      dataIndex: 'priority',
      key: 'priority',
      render: (p: string) => <PriorityTag priority={p} />,
    },
    {
      title: 'Status',
      key: 'status',
      render: () => <Tag color="processing">Pending</Tag>,
    },
    {
      title: 'Remark',
      dataIndex: 'remark',
      key: 'remark',
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
  const autoRowFor = (a: AutoLimitedApp): ControlRow => ({
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
    __auto: a,
    key: `auto:${a.effective_app_id}`,
  })

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
        bodyStyle={{ padding: '16px' }}
      >
        <Row gutter={[12, 12]} wrap>
          <Col flex="auto" style={{ minWidth: 240 }}>
            <Space direction="vertical" size={4}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                Scan a running process, choose an application, then set its control policy.
              </Text>
              <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>
                1. Scan processes &nbsp;→&nbsp; 2. Choose application &nbsp;→&nbsp; 3. Configure limits
              </Text>
              <Button
                type="primary"
                icon={<SearchOutlined />}
                onClick={() => setWizardOpen(true)}
                style={{ marginTop: 4, alignSelf: 'flex-start' }}
              >
                Scan running applications
              </Button>
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
          <Space size={8} wrap>
            <Text style={{ color: COLORS.text, fontSize: 13, fontWeight: 600 }}>
              Application Control Center
            </Text>
            <Tabs
              size="small"
              activeKey={controlTab}
              onChange={(k) => setControlTab(k as 'auto' | 'manual' | 'excluded')}
              style={{ marginBottom: -12 }}
              items={[
                { key: 'auto', label: `🔴 Auto Control (${autoTabCount})` },
                { key: 'manual', label: `🟠 Manual Control (${manualTabCount})` },
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
          </Space>
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
            expandedRowKeys: expandedProcessRows,
            onExpandedRowsChange: (keys) => {
              setExpandedProcessRows([...keys])
              // Per-instance churn of an app that keeps its status emits no SSE event,
              // and this tab does not poll — so pull the rows when the user asks to
              // see them.
              if (keys.length > expandedProcessRows.length) refreshControlled()
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
              : cs === 'MANUAL_LIMITED' ? '🟠 Manual limited'
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
          rowClassName={(_, idx) => (idx % 2 === 1 ? 'table-row-alt' : '')}
        />
      </Card>
      )}

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
          setLimitDialog({ app: null, open: false, loadingProfile: false, submitting: false })
          setResourceSectionExpanded(false)
          setNetworkSectionExpanded(false)
        }}
        onOk={submitResourceLimit}
        okText="Apply Changes"
        confirmLoading={limitDialog.submitting}
        maskClosable={false}
        width={760}
        destroyOnClose
      >
        <div style={{ opacity: limitDialog.loadingProfile ? 0.6 : 1, pointerEvents: limitDialog.loadingProfile ? 'none' : 'auto' }}>
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
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
      `}</style>
    </div>
  )
}
