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
  InputNumber,
  Tabs,
} from 'antd'
import {
  PlusOutlined,
  DeleteOutlined,
  ThunderboltOutlined,
  StopOutlined,
  HeartOutlined,
  ArrowUpOutlined,
  DatabaseOutlined,
  ReloadOutlined,
  QuestionCircleOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { COLORS } from '../styles/theme'
import { api } from '../api/client'
import type { AppInfo, ResourceLimitProfileData } from '../api/types'
import { useAppEvents } from '../hooks/useAppEvents'

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
}

interface LimitDialogState {
  app: AppInfo | null
  open: boolean
  submitting: boolean
  loadingProfile: boolean
}

interface LimitFormValues {
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
}

const PRIORITY_OPTIONS = [
  { value: 'low', label: 'Low', color: COLORS.green },
  { value: 'medium', label: 'Medium', color: COLORS.yellow },
  { value: 'high', label: 'High', color: COLORS.orange },
  { value: 'critical', label: 'Critical', color: COLORS.red },
]

function priorityColor(p?: string): string {
  switch (p?.toLowerCase()) {
    case 'low': return COLORS.green
    case 'medium': return COLORS.yellow
    case 'high': return COLORS.orange
    case 'critical': return COLORS.red
    default: return COLORS.textMuted
  }
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

export default function Balance({ active }: Props) {
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

  // Per-row priority edit state
  const [rowPriorities, setRowPriorities] = useState<Record<string, string>>({})
  const [actionLoading, setActionLoading] = useState<Record<string, boolean>>({})
  const [limitDialog, setLimitDialog] = useState<LimitDialogState>({
    app: null,
    open: false,
    submitting: false,
    loadingProfile: false,
  })
  const [limitForm, setLimitForm] = useState<LimitFormValues>({
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
  })
  // Per-cgroup independent form state for multi-cgroup apps.
  // Key = cgroup_id. Only populated when cgroupIds.length > 1.
  const [perTargetForms, setPerTargetForms] = useState<Record<string, LimitFormValues>>({})
  const [activeLimitTarget, setActiveLimitTarget] = useState<string>('')

  const fetchData = useCallback(async () => {
    try {
      const [apps, controlled, pending] = await Promise.allSettled([
        api.getApps(),
        api.getControlledApps(),
        api.getPendingApps(),
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
        // Fire-and-forget: errors here are non-critical; the backend logs them.
        // Log to console in development so frontend engineers can diagnose failures.
        api.checkRunningApps().catch((e: unknown) => {
          console.error('[Balance] startup scan failed:', e)
        })
      }
      fetchData()
    }
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
        //   - limited/a_limited → resource-limit warning
        //   - any other status change → generic status-updated info
        if (event.status === APP_STATUS.LIMITED || event.status === APP_STATUS.A_LIMITED) {
          messageApi.warning(
            `System busy: ${event.app_name} resource usage has been temporarily limited. It will be restored when resources become available.`
          )
        } else {
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

  const handleRemove = (app: AppInfo) =>
    withLoading(`remove-${app.app_id}`, async () => {
      await api.removeFromControl({ app_id: app.app_id, app_name: app.app_name })
      messageApi.success(`Removed ${app.app_name}`)
    })()

  const handleUpdatePriority = (app: AppInfo) =>
    withLoading(`priority-${app.app_id}`, async () => {
      const p = rowPriorities[app.app_id] ?? app.priority ?? 'medium'
      await api.setPriority({ app_id: app.app_id, priority: p })
      messageApi.success(`Priority updated for ${app.app_name}`)
    })()

  function applyLimitProfile(profile: ResourceLimitProfileData) {
    const cpuOptions = normalizePercentOptions(profile.cpu.options, profile.cpu.value)
    const memOptions = normalizePercentOptions(profile.memory.options, profile.memory.value)
    const cgIds = profile.cgroup_ids ?? []

    const baseForm: LimitFormValues = {
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
    }

    setLimitForm(baseForm)

    // For multi-cgroup apps, initialise each tab with the same defaults so
    // they can be edited independently before submitting.
    if (cgIds.length > 1) {
      const initialPerTarget: Record<string, LimitFormValues> = {}
      cgIds.forEach((cg) => { initialPerTarget[cg] = { ...baseForm } })
      setPerTargetForms(initialPerTarget)
      setActiveLimitTarget(cgIds[0])
    } else {
      setPerTargetForms({})
      setActiveLimitTarget(cgIds[0] ?? '')
    }
  }

  const handleResourceLimit = async (app: AppInfo) => {
    setLimitDialog({ app, open: true, loadingProfile: true, submitting: false })
    try {
      const priority = rowPriorities[app.app_id] ?? app.priority ?? 'medium'
      const profile = await api.getResourceLimitProfile({
        app_id: app.app_id,
        app_name: app.app_name,
        priority,
      })
      applyLimitProfile(profile)
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
      const isMultiTarget = limitForm.cgroupIds.length > 1

      if (isMultiTarget && !useInlineProcessHint) {
        const selectedTarget = activeLimitTarget || limitForm.cgroupIds[0]
        const form = perTargetForms[selectedTarget] ?? limitForm
        await api.resourceLimit({
          app_id: limitDialog.app.app_id,
          app_name: limitDialog.app.app_name,
          priority,
          cgroup_id: selectedTarget,
          limit_overrides: {
            cpu: { enabled: form.cpuEnabled, rate: form.cpuPercent / 100 },
            memory: { enabled: form.memEnabled, rate: form.memPercent / 100 },
            disk_io: {
              enabled: form.diskEnabled,
              rate: {
                write: form.writeMbps,
                read: form.readMbps,
                write_iops: form.writeIops,
                read_iops: form.readIops,
              },
            },
          },
        })
      } else {
        await api.resourceLimit({
          app_id: limitDialog.app.app_id,
          app_name: limitDialog.app.app_name,
          priority,
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
      }
      messageApi.success(`Resource limit applied to ${limitDialog.app.app_name}`)
      setActiveLimitTarget('')
      setPerTargetForms({})
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
      width: 150,
      render: (name: string, record) => {
        const displayName = name || record.app_id
        const tooltipContent = record.remark ? `${displayName} — ${record.remark}` : displayName
        return (
          <Tooltip title={tooltipContent}>
            <div style={{ color: COLORS.accent, fontWeight: 500, lineHeight: 1.2 }}>
              <div>{displayName}</div>
            </div>
          </Tooltip>
        )
      },
    },
    {
      title: 'Priority',
      key: 'priority',
      width: 160,
      render: (_: unknown, record: AppInfo) => (
        <Select
          value={rowPriorities[record.app_id] ?? record.priority ?? 'medium'}
          onChange={(v) => setRowPriorities((prev) => ({ ...prev, [record.app_id]: v }))}
          size="small"
          style={{ width: 120 }}
          dropdownStyle={{ background: COLORS.panelBg }}
        >
          {PRIORITY_OPTIONS.map((opt) => (
            <Option key={opt.value} value={opt.value}>
              <span style={{ color: opt.color }}>{opt.label}</span>
            </Option>
          ))}
        </Select>
      ),
    },
    {
      title: 'Status',
      key: 'status',
      width: 100,
      align: 'center',
      render: (_: unknown, record: AppInfo) => {
        const s = record.status ?? APP_STATUS.NA
        if (s === APP_STATUS.RUNNING) return <Tag color="success">Running</Tag>
        if (s === APP_STATUS.STOPPED) return <Tag color="default">Stopped</Tag>
        if (s === APP_STATUS.LIMITED || s === APP_STATUS.A_LIMITED) return <Tag color="warning">Limited</Tag>
        if (s === APP_STATUS.PENDING) return <Tag color="processing">Pending</Tag>
        return <Tag color="default">NA</Tag>
      },
    },
    {
      title: 'Remark',
      dataIndex: 'remark',
      key: 'remark',
      width: 140,
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
        const isPending = record.status === APP_STATUS.PENDING
        const isRunning = record.status === APP_STATUS.RUNNING
        const isCritical = (rowPriorities[record.app_id] ?? record.priority ?? '').toLowerCase() === 'critical'
        const isLimited = record.status === APP_STATUS.LIMITED || record.status === APP_STATUS.A_LIMITED

        return (
          <Space size={4} wrap={false}>
            <Tooltip title="Cancel Relaunch (only for pending)">
              <Button
                size="small"
                icon={<StopOutlined />}
                disabled={!isPending}
                loading={actionLoading[`cancel-${record.app_id}`]}
                onClick={() => handleCancelRelaunch(record)}
              >
                Cancel
              </Button>
            </Tooltip>

            <Tooltip title="Update Priority">
              <Button
                size="small"
                type="primary"
                icon={<ArrowUpOutlined />}
                loading={actionLoading[`priority-${record.app_id}`]}
                onClick={() => handleUpdatePriority(record)}
              >
                Priority
              </Button>
            </Tooltip>

            {isLimited ? (
              <Tooltip title="Restore Resources">
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  loading={actionLoading[`restore-${record.app_id}`]}
                  onClick={() => handleResourceRestore(record)}
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
                style={isCritical && isRunning ? { borderColor: COLORS.red, color: COLORS.red } : {}}
              >
                Keep Alive
              </Button>
            </Tooltip>

            <Tooltip title="Remove from Control">
              <Button
                size="small"
                danger
                icon={<DeleteOutlined />}
                loading={actionLoading[`remove-${record.app_id}`]}
                onClick={() => {
                  Modal.confirm({
                    title: `Remove ${record.app_name}?`,
                    content: 'This will remove the app from resource control.',
                    okText: 'Remove',
                    okType: 'danger',
                    onOk: () => handleRemove(record),
                  })
                }}
              >
                Remove
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

  const uncontrolledApps = allApps.filter(
    (a) => !controlledApps.some((c) => c.app_id === a.app_id)
  )
  const limitDialogPriority = limitDialog.app
    ? (rowPriorities[limitDialog.app.app_id] ?? limitDialog.app.priority ?? 'medium').toLowerCase()
    : 'medium'
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

  // Show multi-tab only when there are genuinely distinct cgroups.
  // Use process names as labels when there is a 1:1 match with cgroup IDs.
  const tabTargets = useMemo(() => {
    const cgIds = limitForm.cgroupIds
    const procNames = limitForm.processNames
    if (cgIds.length <= 1) return []
    return cgIds.map((cg, i) => ({
      key: cg,
      label: procNames.length === cgIds.length ? procNames[i] : cg,
    }))
  }, [limitForm.cgroupIds, limitForm.processNames])
  const inlineProcessNames = useMemo(
    () => limitForm.processNames.map((name) => name.trim()).filter(Boolean),
    [limitForm.processNames]
  )
  const useInlineProcessHint = inlineProcessNames.length > 1

  // renderLimitSettings accepts a form snapshot and a typed setter so each
  // context (single-cgroup or per-tab) can be fully independent.
  const renderLimitSettings = (
    form: LimitFormValues,
    updateForm: (updater: (prev: LimitFormValues) => LimitFormValues) => void
  ) => (
    <>
      <div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <Space size={4}>
            <Text strong>CPU Limit</Text>
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
          <Switch
            checked={form.cpuEnabled}
            onChange={(checked) => updateForm((prev) => ({ ...prev, cpuEnabled: checked }))}
          />
        </div>
        <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', gap: 8 }}>
          <InputNumber
            style={{ width: '100%' }}
            disabled={!form.cpuEnabled}
            value={form.cpuPercent}
            controls
            min={form.cpuMin}
            max={form.cpuMax}
            onChange={(v) => updateForm((prev) => ({ ...prev, cpuPercent: Number(v ?? prev.cpuPercent) }))}
          />
          <Text type="secondary">%</Text>
        </div>
      </div>

      <div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <Space size={4}>
            <Text strong>Memory Limit</Text>
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
          <Switch
            checked={form.memEnabled}
            onChange={(checked) => updateForm((prev) => ({ ...prev, memEnabled: checked }))}
          />
        </div>
        <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', gap: 8 }}>
          <InputNumber
            style={{ width: '100%' }}
            disabled={!form.memEnabled}
            value={form.memPercent}
            controls
            min={form.memMin}
            max={form.memMax}
            onChange={(v) => updateForm((prev) => ({ ...prev, memPercent: Number(v ?? prev.memPercent) }))}
          />
          <Text type="secondary">%</Text>
        </div>
      </div>

      <div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <Space size={4}>
            <Text strong>Disk IO Limit</Text>
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
          <Switch
            checked={form.diskEnabled}
            onChange={(checked) => updateForm((prev) => ({ ...prev, diskEnabled: checked }))}
          />
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
              disabled={!form.diskEnabled}
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
              disabled={!form.diskEnabled}
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
              disabled={!form.diskEnabled}
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
              disabled={!form.diskEnabled}
              min={1}
              value={form.readIops}
              onChange={(v) => updateForm((prev) => ({ ...prev, readIops: Number(v ?? prev.readIops) }))}
            />
          </Col>
        </Row>
      </div>
      <Text type="secondary" style={{ fontSize: 12 }}>
        Note: In rare cases, excessively strict limit settings may prevent certain specialized applications from obtaining even their minimum required resources.
      </Text>
    </>
  )

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

      {/* Add App Section */}
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
        <Row gutter={[12, 12]} align="middle">
          <Col xs={24} sm={10} md={8}>
            <div style={{ marginBottom: 4 }}>
              <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>Application</Text>
            </div>
            <Select
              value={selectedAppId || undefined}
              onChange={setSelectedAppId}
              placeholder="Select application..."
              style={{ width: '100%' }}
              showSearch
              filterOption={(input, option) =>
                String(option?.children ?? '').toLowerCase().includes(input.toLowerCase())
              }
              dropdownStyle={{ background: COLORS.panelBg }}
              notFoundContent={
                <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>No apps found</Text>
              }
            >
              {uncontrolledApps.map((app) => (
                <Option key={app.app_id} value={app.app_id}>
                  {app.app_name}
                </Option>
              ))}
            </Select>
          </Col>

          <Col xs={24} sm={6} md={4}>
            <div style={{ marginBottom: 4 }}>
              <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>Priority</Text>
            </div>
            <Select
              value={addPriority}
              onChange={setAddPriority}
              style={{ width: '100%' }}
              dropdownStyle={{ background: COLORS.panelBg }}
            >
              {PRIORITY_OPTIONS.map((opt) => (
                <Option key={opt.value} value={opt.value}>
                  <span style={{ color: opt.color }}>{opt.label}</span>
                </Option>
              ))}
            </Select>
          </Col>

          <Col xs={24} sm={10} md={8}>
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

          <Col xs={24} sm={24} md={4}>
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
      </Card>

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
          setActiveLimitTarget('')
          setPerTargetForms({})
          setLimitDialog({ app: null, open: false, loadingProfile: false, submitting: false })
        }}
        onOk={submitResourceLimit}
        okText="Apply Limit"
        confirmLoading={limitDialog.submitting}
        maskClosable={false}
        width={760}
        destroyOnClose
      >
        <div style={{ opacity: limitDialog.loadingProfile ? 0.6 : 1, pointerEvents: limitDialog.loadingProfile ? 'none' : 'auto' }}>
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            {tabTargets.length > 1 && !useInlineProcessHint && (
              <Tabs
                activeKey={activeLimitTarget || tabTargets[0].key}
                onChange={setActiveLimitTarget}
                items={tabTargets.map(({ key, label }) => ({
                  key,
                  label,
                  children: (
                    <Space direction="vertical" size={12} style={{ width: '100%' }}>
                      {renderLimitSettings(
                        perTargetForms[key] ?? limitForm,
                        (updater) => setPerTargetForms((prev) => ({
                          ...prev,
                          [key]: updater(prev[key] ?? limitForm),
                        }))
                      )}
                    </Space>
                  ),
                }))}
              />
            )}
            {useInlineProcessHint && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                Target Processes: {inlineProcessNames.join(' | ')}
              </Text>
            )}

            {tabTargets.length === 0 && limitForm.cgroupIds.length === 1 && (
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>Target</Text>
                <div style={{ marginTop: 6 }}>
                  <Tag color="blue" style={{ marginBottom: 6 }}>
                    {limitForm.processNames.length > 0
                      ? limitForm.processNames.join(' | ')
                      : limitForm.cgroupIds[0]}
                  </Tag>
                </div>
              </div>
            )}
            {(tabTargets.length <= 1 || useInlineProcessHint) && renderLimitSettings(limitForm, setLimitForm)}
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
