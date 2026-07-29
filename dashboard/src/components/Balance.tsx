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
} from 'antd'
import {
  PlusOutlined,
  DeleteOutlined,
  DeleteFilled,
  ThunderboltOutlined,
  CloseOutlined,
  HeartOutlined,
  SaveOutlined,
  DatabaseOutlined,
  ReloadOutlined,
  QuestionCircleOutlined,
  SearchOutlined,
  RightOutlined,
  DownOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { COLORS } from '../styles/theme'
import { api } from '../api/client'
import type { AppInfo, ResourceLimitProfileData, PassiveControlData, ProcessStatusRow } from '../api/types'
import { useAppEvents } from '../hooks/useAppEvents'
import { useGlobalConfigNotices } from '../hooks/useGlobalConfigNotices'
import { AddAppWizard } from './AddAppWizard'

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

function deriveDisplayProcessName(row: ProcessStatusRow): string {
  const rawName = (row.process_name || '').trim()
  const cmdline = (row.cmdline || '').trim()
  if (!cmdline) return rawName || '-'

  // Strip common wrappers so we can show the actual target process/script.
  const withoutSudo = cmdline.replace(/^sudo\s+/, '')
  const pythonScriptMatch = withoutSudo.match(/^(?:python\d*(?:\.\d+)?)\s+([^\s]+)/i)
  if (pythonScriptMatch?.[1]) {
    const script = pythonScriptMatch[1].split('/').pop()
    if (script) return script
  }

  if (rawName && !['sudo', 'python', 'python2', 'python3'].includes(rawName.toLowerCase())) {
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
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [messageApi, contextHolder] = message.useMessage()

  // Add app form state
  const [selectedAppId, setSelectedAppId] = useState<string>('')
  const [addPriority, setAddPriority] = useState<string>('medium')
  const [remark, setRemark] = useState('')
  const [adding, setAdding] = useState(false)
  const [wizardOpen, setWizardOpen] = useState(false)
  const [expandedProcessRows, setExpandedProcessRows] = useState<React.Key[]>([])
  const [selectedTargetCgroups, setSelectedTargetCgroups] = useState<Record<string, string[]>>({})

  // Opened from the Processes tab: pop the Add-App wizard pre-filled.
  useEffect(() => {
    if (registerKeyword) setWizardOpen(true)
  }, [registerKeyword])

  // Per-row priority edit state
  const [rowPriorities, setRowPriorities] = useState<Record<string, string>>({})
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

  const fetchData = useCallback(async () => {
    try {
      const [apps, controlled, pending, networkControl] = await Promise.allSettled([
        api.getApps(),
        api.getControlledApps(),
        api.getPendingApps(),
        api.getConfig<{
          enable_network_control: boolean
          config_network_bw?: Record<string, { min?: number; max?: number }>
        }>('network_control'),
      ])

      if (apps.status === 'fulfilled') setAllApps(apps.value ?? [])
      if (controlled.status === 'fulfilled') {
        const ctrl = controlled.value ?? []
        setControlledApps(ctrl)
        const priorities: Record<string, string> = {}
        ctrl.forEach((a: AppInfo) => {
          // Normalise to lowercase so comparisons are case-insensitive.
          // The Python Streamlit side stores "Critical"/"High"/… (title-case);
          // the dashboard stores "critical"/"high"/… (lowercase).
          priorities[a.app_id] = (a.priority ?? 'medium').toLowerCase()
        })
        setRowPriorities((prev) => ({ ...prev, ...priorities }))
      }
      if (pending.status === 'fulfilled') setPendingApps(pending.value ?? [])
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
  }, [])

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
          next[app.app_id] = available
          continue
        }

        const filtered = existing.filter((cg) => available.includes(cg))
        next[app.app_id] = filtered.length > 0 ? filtered : available
      }
      return next
    })
  }, [controlledApps])

  // Keep per-process runtime rows fresh even when the overall app status does
  // not transition (e.g. one instance stops but another is still running).
  // SSE emits app-level changes, but partial per-instance changes may not
  // produce an event, so do a light periodic sync while this tab is active.
  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => {
      fetchData()
    }, 5000)
    return () => window.clearInterval(timer)
  }, [active, fetchData])

  // SSE: push updates from server instead of polling every 5 s
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
        // remove it from the pending queue immediately without waiting for fetchData()
        // to complete.  This prevents the card from showing a stale entry during the
        // async round-trip.
        // Note: the reverse (status === PENDING) is intentionally handled only by the
        // fetchData() call below, because adding to pendingApps requires a full AppInfo
        // object that the SSE payload does not carry.
        if (event.status !== APP_STATUS.PENDING) {
          setPendingApps((prev) => prev.filter((app) => app.app_id !== event.app_id))
        }

        // Full server sync – also re-fetches controlled and pending lists so any
        // remaining pending apps (or a newly pending app) are shown correctly.
        fetchData()
      } else if (event.purpose === 'notify') {
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
    }, [messageApi, fetchData]),
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

  const handleAdd = async () => {
    if (!selectedAppId) {
      messageApi.warning('Please select an application')
      return
    }
    const app = allApps.find((a) => a.app_id === selectedAppId)
    if (!app) return

    setAdding(true)
    try {
      await api.setToControl({
        app_id: app.app_id,
        app_name: app.app_name,
        priority: addPriority,
        network_priority: addPriority,
        controlled: true,
        remark,
        cmdline: app.cmdline ?? '',
        cgroup: 'user',
      })
      messageApi.success(`Added ${app.app_name} to control`)
      setSelectedAppId('')
      setRemark('')
      await fetchData()
    } catch (e: unknown) {
      messageApi.error(e instanceof Error ? e.message : 'Failed to add app')
    } finally {
      setAdding(false)
    }
  }

  const handleUncontrol = (app: AppInfo) =>
    withLoading(`uncontrol-${app.app_id}`, async () => {
      await api.removeFromControl({ app_id: app.app_id, app_name: app.app_name })
      messageApi.success(`Uncontrolled ${app.app_name}`)
    })()

  const handleDelete = (app: AppInfo) =>
    withLoading(`delete-${app.app_id}`, async () => {
      await api.purgeControlledApp(app.app_id)
      messageApi.success(`Deleted ${app.app_name}`)
    })()

  const handleUpdatePriority = (app: AppInfo) =>
    withLoading(`priority-${app.app_id}`, async () => {
      const p = rowPriorities[app.app_id] ?? app.priority ?? 'medium'
      await api.setPriority({ app_id: app.app_id, priority: p })
      messageApi.success(`Priority updated for ${app.app_name}`)
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
      const priority = rowPriorities[app.app_id] ?? app.priority ?? 'medium'
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
      const priority = rowPriorities[limitDialog.app.app_id] ?? limitDialog.app.priority ?? 'medium'
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

  const controlledColumns: ColumnsType<AppInfo> = [
    {
      title: 'App Name',
      dataIndex: 'app_name',
      key: 'app_name',
      width: 240,
      render: (name: string, record) => {
        const displayName = name || record.app_id
        const tooltipContent = record.remark ? `${displayName} — ${record.remark}` : displayName
        return (
          <Space direction="vertical" size={2} style={{ lineHeight: 1.25 }}>
            <Tooltip title={tooltipContent}>
              <div style={{ color: COLORS.accent, fontWeight: 500 }}>{displayName}</div>
            </Tooltip>
          </Space>
        )
      },
    },
    {
      title: 'Priority',
      key: 'priority',
      width: 150,
      render: (_: unknown, record: AppInfo) => (
        <Space size={12} wrap={false}>
          <Select
            value={rowPriorities[record.app_id] ?? record.priority ?? 'medium'}
            onChange={(v) => setRowPriorities((prev) => ({ ...prev, [record.app_id]: v }))}
            size="small"
            style={{ width: 120 }}
            styles={{ popup: { root: { background: COLORS.panelBg } } }}
          >
            {PRIORITY_OPTIONS.map((opt) => (
              <Option key={opt.value} value={opt.value}>
                <span style={{ color: opt.color }}>{opt.label}</span>
              </Option>
            ))}
          </Select>
          <Tooltip title="Save Priority">
            <Button
              size="small"
              icon={<SaveOutlined />}
              onClick={() => handleUpdatePriority(record)}
              style={{ borderColor: COLORS.accent, color: COLORS.accent }}
            />
          </Tooltip>
        </Space>
      ),
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
      title: 'Actions',
      key: 'actions',
      width: 160,
      align: 'center',
      render: (_: unknown, record: AppInfo) => {
        const isRunning = record.status === APP_STATUS.RUNNING
        const isCritical = (rowPriorities[record.app_id] ?? record.priority ?? '').toLowerCase() === 'critical'
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
              <Tooltip title="Apply Resource Limit">
                <Button
                  size="small"
                  icon={<DatabaseOutlined />}
                  disabled={!isRunning}
                  loading={limitDialog.loadingProfile && limitDialog.app?.app_id === record.app_id}
                  onClick={() => handleResourceLimit(record)}
                  style={isRunning ? { borderColor: COLORS.accent, color: COLORS.accent } : {}}
                >
                  Limit
                </Button>
              </Tooltip>
            )}

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

            <Tooltip title="Uncontrol (config kept; re-add from dropdown)">
              <Button
                size="small"
                icon={<DeleteOutlined />}
                loading={actionLoading[`uncontrol-${record.app_id}`]}
                onClick={() => {
                  Modal.confirm({
                    title: `Uncontrol ${record.app_name}?`,
                    content:
                      'This will stop monitoring the app. The configuration is kept, '
                      + 'so you can re-add it later from the Application dropdown above '
                      + 'without going through the wizard again.',
                    okText: 'Uncontrol',
                    onOk: () => handleUncontrol(record),
                  })
                }}
                style={{ borderColor: COLORS.accent, color: COLORS.accent }}
              >
                Uncontrol
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
                      'This permanently removes the entry from config.yaml and the database. '
                      + 'To control this app again you will need to re-add it through the '
                      + 'wizard. Use Uncontrol instead if you want to keep the configuration.',
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

  const uncontrolledApps = allApps.filter(
    (a) => !controlledApps.some((c) => c.app_id === a.app_id)
  )
  const limitDialogPriority = limitDialog.app
    ? (rowPriorities[limitDialog.app.app_id] ?? limitDialog.app.priority ?? 'medium').toLowerCase()
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
    if (preferred && preferred.length > 0) return preferred

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

      {/* Add App Section — two paths:
            (1) Discover new: open the wizard, scan /proc, auto-fill bpf_name
                / process_names / commandline by inspecting running processes.
            (2) Pick configured: choose from controlled_apps already declared
                in config.yaml and just enable monitoring + set priority.
          The wizard is the entry for apps not yet in the dropdown.  */}
      <Card
        title={
          <Text style={{ color: COLORS.text, fontSize: 13, fontWeight: 600 }}>
            <PlusOutlined style={{ marginRight: 8, color: COLORS.accent }} />
            Add Application to Control
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
        {/* Two parallel options shown side-by-side so the user can see both
            paths at once.  Each option has a short heading describing what it
            does, then a bordered box with the actual controls.  Stacks
            vertically on small screens via the responsive Col breakpoints. */}
        <Row gutter={[12, 12]}>
          {/* (1) Discover new — opens the wizard which scans /proc and
                auto-fills bpf_name / process_names by inspecting running
                processes.  Use this when the app isn't already in the
                Pick-from-list dropdown. */}
          <Col xs={24} md={8}>
            <div style={{ marginBottom: 6 }}>
              <Text style={{ color: COLORS.textMuted, fontSize: 11, fontWeight: 600 }}>
                Option 1 — Discover by running process
              </Text>
            </div>
            <div
              style={{
                border: `1px solid ${COLORS.border}`,
                borderRadius: 6,
                padding: '16px',
                height: 'calc(100% - 22px)',
                display: 'flex',
                flexDirection: 'column',
                justifyContent: 'space-between',
                alignItems: 'center',
              }}
            >
              <Text type="secondary" style={{ fontSize: 11, marginBottom: 12, textAlign: 'center' }}>
                Scan running processes for an app that’s not in the list yet
                — the wizard auto-fills the technical fields for you.
              </Text>
              <Button
                type="primary"
                icon={<SearchOutlined />}
                onClick={() => setWizardOpen(true)}
                style={{ width: '50%' }}
              >
                Find new application
              </Button>
            </div>
          </Col>

          {/* (2) Pick configured — choose from controlled_apps already
                declared in config.yaml and just enable monitoring + set
                priority. */}
          <Col xs={24} md={16}>
            <div style={{ marginBottom: 6 }}>
              <Text style={{ color: COLORS.textMuted, fontSize: 11, fontWeight: 600 }}>
                Option 2 — Pick a configured application
              </Text>
            </div>
            <div
              style={{
                border: `1px solid ${COLORS.border}`,
                borderRadius: 6,
                padding: '16px',
              }}
            >
              <Row gutter={[12, 12]} align="middle">
                <Col xs={24} sm={6}>
                  <div style={{ marginBottom: 4 }}>
                    <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>Application</Text>
                  </div>
                  <Select
                    value={selectedAppId || undefined}
                    onChange={setSelectedAppId}
                    placeholder={
                      uncontrolledApps.length === 0
                        ? 'No new apps to add — use the wizard'
                        : 'Select application...'
                    }
                    style={{ width: '100%' }}
                    showSearch
                    filterOption={(input, option) =>
                      String(option?.children ?? '').toLowerCase().includes(input.toLowerCase())
                    }
                    styles={{ popup: { root: { background: COLORS.panelBg } } }}
                    notFoundContent={
                      <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>
                        No new apps to add — register one via &ldquo;Find new application&rdquo;.
                      </Text>
                    }
                  >
                    {uncontrolledApps.map((app) => (
                      <Option key={app.app_id} value={app.app_id}>
                        {app.app_name}
                      </Option>
                    ))}
                  </Select>
                </Col>

                <Col xs={12} sm={4}>
                  <div style={{ marginBottom: 4 }}>
                    <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>Priority</Text>
                  </div>
                  <Select
                    value={addPriority}
                    onChange={setAddPriority}
                    style={{ width: '100%' }}
                    styles={{ popup: { root: { background: COLORS.panelBg } } }}
                  >
                    {PRIORITY_OPTIONS.map((opt) => (
                      <Option key={opt.value} value={opt.value}>
                        <span style={{ color: opt.color }}>{opt.label}</span>
                      </Option>
                    ))}
                  </Select>
                </Col>

                <Col xs={24} sm={9}>
                  <div style={{ marginBottom: 4 }}>
                    <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>Remark</Text>
                  </div>
                  <Input
                    value={remark}
                    onChange={(e) => setRemark(e.target.value)}
                    placeholder="Optional note..."
                    style={{ background: COLORS.panelBg, borderColor: COLORS.border }}
                  />
                </Col>

                <Col xs={12} sm={5}>
                  <div style={{ marginBottom: 4, visibility: 'hidden' }}>
                    <Text style={{ fontSize: 11 }}>action</Text>
                  </div>
                  <Button
                    type="primary"
                    icon={<PlusOutlined />}
                    loading={adding}
                    onClick={handleAdd}
                    block
                  >
                    Add to Control
                  </Button>
                </Col>
              </Row>
            </div>
          </Col>
        </Row>
      </Card>

      <AddAppWizard
        open={wizardOpen}
        initialKeyword={registerKeyword ?? undefined}
        onClose={() => {
          setWizardOpen(false)
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

      {/* Controlled Apps */}
      <Card
        title={
          <Text style={{ color: COLORS.text, fontSize: 13, fontWeight: 600 }}>
            Controlled Applications
            <Tag style={{ marginLeft: 8, fontSize: 11 }}>{controlledApps.length}</Tag>
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
        <Table
          columns={controlledColumns}
          dataSource={controlledApps.map((a) => ({ ...a, key: a.app_id }))}
          loading={loading}
          size="small"
          pagination={false}
          scroll={{ x: 'max-content' }}
          rowClassName={(_, idx) => (idx % 2 === 1 ? 'table-row-alt' : '')}
          expandable={{
            expandedRowKeys: expandedProcessRows,
            onExpandedRowsChange: (keys) => setExpandedProcessRows([...keys]),
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
                No apps under control. Add one above.
              </div>
            ),
          }}
        />
      </Card>

      {/* Pending Queue – always shown so users can see the empty state (mirrors Python's pending_queue_holder) */}
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
        {pendingApps.length === 0 ? (
          <div style={{ padding: 24, textAlign: 'center', color: COLORS.textMuted, fontStyle: 'italic' }}>
            🕊️ Pending queue is empty
          </div>
        ) : (
          <Table
            columns={pendingColumns}
            dataSource={pendingApps.map((a) => ({ ...a, key: a.app_id }))}
            size="small"
            pagination={false}
            rowClassName={(_, idx) => (idx % 2 === 1 ? 'table-row-alt' : '')}
          />
        )}
      </Card>

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
