import React, { useState, useCallback, useEffect } from 'react'
import {
  Table,
  Tag,
  Typography,
  Alert,
  Spin,
  Badge,
  Progress,
  Space,
  Row,
  Col,
  Button,
  Modal,
  Form,
  InputNumber,
  Tooltip,
  message,
} from 'antd'
import { ReloadOutlined, ThunderboltOutlined, SettingOutlined, SaveOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { COLORS } from '../styles/theme'
import { api } from '../api/client'
import type { AppResourceEntry, AppDiskIoEntry, ProcessEntry, WeightsTopData, DynamicInfoData } from '../api/types'
import { useGlobalConfigNotices } from '../hooks/useGlobalConfigNotices'
import { ProcessActionsMenu, useProcessDetail } from './ProcessActions'
import { buildGpuLabelMap } from '../utils/gpu'

const { Text, Title } = Typography

// Fields shared by both tables that drive the row's Operation menu and its
// expandable child-process list.  pids spans every process the app owns.
interface AppControl {
  pid: number
  pids: number[]
  cmdline: string
  status?: string
  is_self?: boolean
  balancer_candidate?: boolean
}

interface AppRow extends AppControl {
  key: string
  app_id: string
  app_name: string
  cpu_usage: number
  memory_mb: number
  io_read_rate: number
  io_write_rate: number
  score: number
  gpu_util: number
  gpu_mem_mb: number
}

interface DiskIoRow extends AppControl {
  key: string
  name: string
  app_name: string
  io_read_rate: number
  io_write_rate: number
  io_read_iops: number
  io_write_iops: number
  score: number
}

interface Props {
  active: boolean
  balancerEnabled: boolean
  // Jump to the Balancer tab's Add-App wizard pre-filled with this name.
  onRegister?: (name: string) => void
}

function formatBytes(mb: number): string {
  if (mb < 1) return `${(mb * 1024).toFixed(0)} KB/s`
  return `${mb.toFixed(1)} MB/s`
}

function formatMemory(kb: number): string {
  if (kb < 1024) return `${kb.toFixed(0)} KB`
  if (kb < 1024 * 1024) return `${(kb / 1024).toFixed(1)} MB`
  return `${(kb / 1024 / 1024).toFixed(2)} GB`
}

function formatRate(bytesPerSec: number): string {
  if (bytesPerSec < 1024) return `${bytesPerSec.toFixed(0)} B/s`
  if (bytesPerSec < 1024 * 1024) return `${(bytesPerSec / 1024).toFixed(1)} KB/s`
  if (bytesPerSec < 1024 * 1024 * 1024) return `${(bytesPerSec / 1024 / 1024).toFixed(1)} MB/s`
  return `${(bytesPerSec / 1024 / 1024 / 1024).toFixed(2)} GB/s`
}

// ========== Settings Modal Component ==========

interface WeightsConfig {
  cpu: number
  memory: number
  gpu: number
}

interface SettingsModalProps {
  visible: boolean
  onClose: () => void
}

function formatRelativeAge(ts: number | undefined | null): string {
  if (!ts) return 'never'
  const ageSec = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (ageSec < 5) return 'just now'
  if (ageSec < 60) return `${ageSec}s ago`
  if (ageSec < 3600) return `${Math.floor(ageSec / 60)}m ago`
  if (ageSec < 86400) return `${Math.floor(ageSec / 3600)}h ago`
  return `${Math.floor(ageSec / 86400)}d ago`
}

function formatTimestamp(ts: number | undefined | null): string {
  if (!ts) return 'Not yet saved'
  return new Date(ts * 1000).toLocaleString()
}

function SettingsModal({ visible, onClose }: SettingsModalProps) {
  const [form] = Form.useForm()
  const { publishNotice } = useGlobalConfigNotices()
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [initialValues, setInitialValues] = useState<WeightsConfig | null>(null)
  const [updatedAt, setUpdatedAt] = useState<number | undefined>(undefined)

  const loadWeights = useCallback(async () => {
    setLoading(true)
    try {
      const weights = await api.getWeightsTop()
      const { updated_at, ...rest } = weights
      const config: WeightsConfig = {
        cpu: rest.cpu ?? 0,
        memory: rest.memory ?? 0,
        gpu: rest.gpu ?? 0,
      }
      setInitialValues(config)
      setUpdatedAt(updated_at)
      form.setFieldsValue(config)
    } catch (error) {
      message.error('Failed to load weights configuration')
      console.error(error)
    } finally {
      setLoading(false)
    }
  }, [form])

  useEffect(() => {
    if (visible) {
      loadWeights()
    }
  }, [visible, loadWeights])

  const handleSave = async () => {
    try {
      const values = await form.validateFields()
      setSaving(true)

      const result = await api.updateWeightsTop(values, updatedAt)

      if (result.status === 'conflict') {
        const current = (result.current ?? {}) as WeightsTopData
        const newTs = current.updated_at
        const tsLabel = newTs ? formatTimestamp(newTs) : 'unknown time'
        Modal.confirm({
          title: 'Settings changed by another client',
          content: (
            <div>
              <p>
                These weights were updated at <b>{tsLabel}</b> while you were editing.
                Reloading will replace the form with the latest server values.
              </p>
            </div>
          ),
          okText: 'Reload latest values',
          cancelText: 'Cancel',
          onOk: async () => {
            const next: WeightsConfig = {
              cpu: current.cpu ?? 0,
              memory: current.memory ?? 0,
              gpu: current.gpu ?? 0,
            }
            setInitialValues(next)
            setUpdatedAt(newTs)
            form.setFieldsValue(next)
            publishNotice({
              title: 'Weights configuration updated',
              description: `Another client changed score weights at ${tsLabel}. The form has been reloaded.`,
              scope: 'weights_top',
              updatedAt: newTs,
            })
          },
        })
        return
      }

      const response = result.data
      if (response.success) {
        message.success('Weights configuration updated successfully')
        const w = response.updated_weights
        setInitialValues({ cpu: w.cpu, memory: w.memory, gpu: w.gpu })
        setUpdatedAt(response.updated_at)
        publishNotice({
          title: 'Weights configuration updated',
          description: `Score weights are now CPU ${w.cpu}, Memory ${w.memory}, GPU ${w.gpu}.`,
          scope: 'weights_top',
          updatedAt: response.updated_at,
        })
        onClose()
      } else {
        message.error('Failed to update weights configuration')
      }
    } catch (error) {
      message.error('Failed to save weights configuration')
      console.error(error)
    } finally {
      setSaving(false)
    }
  }

  // Re-fetch the latest server values.  Useful both for "discard my edits"
  // and "pick up another client's changes without closing the modal".
  const handleReset = () => {
    loadWeights()
  }

  return (
    <Modal
      title={
        <Space>
          <SettingOutlined style={{ color: COLORS.accent }} />
          <span>Score Weights Configuration</span>
        </Space>
      }
      open={visible}
      onCancel={onClose}
      footer={[
        <Button key="reset" icon={<ReloadOutlined />} onClick={handleReset}>
          Reset
        </Button>,
        <Button key="cancel" onClick={onClose}>
          Cancel
        </Button>,
        <Button
          key="save"
          type="primary"
          icon={<SaveOutlined />}
          loading={saving}
          onClick={handleSave}
        >
          Save
        </Button>,
      ]}
      width={600}
    >
      <div style={{ marginBottom: 16 }}>
        <Text type="secondary">
          Configure the weight of each resource type in the Top Resource Consumers score calculation.
          Higher weights give more importance to that resource when ranking applications by combined CPU, Memory, and GPU usage.
        </Text>
        <Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
          Note: Disk I/O is ranked separately in the Top Disk I/O Consumer section and does not use these weights.
        </Text>
        <Text type="secondary" style={{ display: 'block', marginTop: 8, fontSize: 12 }}>
          Last updated: {formatTimestamp(updatedAt)}{updatedAt ? ` (${formatRelativeAge(updatedAt)})` : ''}
        </Text>
      </div>

      <Form
        form={form}
        layout="vertical"
        initialValues={initialValues || undefined}
      >
        <Form.Item
          label="CPU Weight"
          name="cpu"
          rules={[
            { required: true, message: 'CPU weight is required' },
            { type: 'integer', min: 0, message: 'Must be a non-negative integer' },
          ]}
        >
          <InputNumber
            style={{ width: '100%' }}
            min={0}
            placeholder="Enter CPU weight"
            disabled={loading}
          />
        </Form.Item>

        <Form.Item
          label="Memory Weight"
          name="memory"
          rules={[
            { required: true, message: 'Memory weight is required' },
            { type: 'integer', min: 0, message: 'Must be a non-negative integer' },
          ]}
        >
          <InputNumber
            style={{ width: '100%' }}
            min={0}
            placeholder="Enter memory weight"
            disabled={loading}
          />
        </Form.Item>

<Form.Item
          label="GPU Weight"
          name="gpu"
          rules={[
            { required: true, message: 'GPU weight is required' },
            { type: 'integer', min: 0, message: 'Must be a non-negative integer' },
          ]}
          extra="Added to the default score as a secondary factor after GPU data is collected"
        >
          <InputNumber
            style={{ width: '100%' }}
            min={0}
            placeholder="Enter GPU weight"
            disabled={loading}
          />
        </Form.Item>
      </Form>
    </Modal>
  )
}


// ========== Main Component ==========

export default function AppResources({ active, balancerEnabled, onRegister }: Props) {
  const [rows, setRows] = useState<AppRow[]>([])
  const [diskRows, setDiskRows] = useState<DiskIoRow[]>([])
  // pid -> full process detail, used to render each app's expandable child list.
  const [procMap, setProcMap] = useState<Map<number, ProcessEntry>>(new Map())
  // System dynamic snapshot — only its GPU device list is needed here, to map
  // each child process's PCI address to an iGPU/dGPU label.
  const [dyn, setDyn] = useState<DynamicInfoData | null>(null)
  const [loading, setLoading] = useState(true)
  // Drives the Refresh button's spinner; `loading` only covers the first fetch.
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [settingsVisible, setSettingsVisible] = useState(false)
  const { openDetail, detailModal } = useProcessDetail()

  const fetchData = useCallback(async () => {
    setRefreshing(true)
    try {
      // The process list backs the expandable child rows (joined by PID); its
      // failure must not blank the app tables, so it is tolerated separately.
      const [resourceData, diskData, procData, dynData] = await Promise.all([
        api.getAppResourceStats(3),
        api.getAppDiskIoStats(3),
        api.getProcesses(true, true).catch(() => null),
        api.getDynamicInfo(['gpu']).catch(() => null),
      ])

      const appRows: AppRow[] = resourceData.apps.map((entry: AppResourceEntry) => ({
        key: entry.app_id,
        app_id: entry.app_id,
        app_name: entry.app_name,
        pid: entry.pid ?? 0,
        pids: entry.pids ?? (entry.pid ? [entry.pid] : []),
        cmdline: entry.cmdline ?? '',
        status: entry.status,
        is_self: entry.is_self,
        balancer_candidate: entry.balancer_candidate,
        cpu_usage: entry.cpu_usage,
        memory_mb: entry.memory_mb,
        io_read_rate: entry.io_read_rate,
        io_write_rate: entry.io_write_rate,
        score: entry.score,
        gpu_util: entry.gpu_util ?? 0,
        gpu_mem_mb: entry.gpu_mem_mb ?? 0,
      }))
      setRows(appRows)

      const dRows: DiskIoRow[] = diskData.apps.map((entry: AppDiskIoEntry, idx: number) => ({
        key: `${entry.pid ?? idx}`,
        pid: entry.pid ?? 0,
        pids: entry.pids ?? (entry.pid ? [entry.pid] : []),
        name: entry.name ?? 'Unknown',
        app_name: entry.app_name ?? '',
        cmdline: entry.cmdline ?? '',
        status: entry.status,
        is_self: entry.is_self,
        balancer_candidate: entry.balancer_candidate,
        io_read_rate: entry.io_read_rate ?? 0,
        io_write_rate: entry.io_write_rate ?? 0,
        io_read_iops: entry.io_read_iops ?? 0,
        io_write_iops: entry.io_write_iops ?? 0,
        score: entry.score ?? 0,
      }))
      setDiskRows(dRows)

      if (procData) {
        setProcMap(new Map(procData.processes.map((p) => [p.pid, p])))
      }
      setDyn(dynData)

      setError(null)
      setLastUpdated(new Date())
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to fetch data')
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [])

  // No periodic polling here: this tab refreshes on demand only.  Entering the
  // tab pulls once so the tables are never blank / badly stale; after that the
  // user drives updates with the Refresh button (or an Operation-menu action).
  useEffect(() => {
    if (active) fetchData()
  }, [active, fetchData])

  // Suspended (SIGSTOP'd) processes sink to the bottom of the score-sorted tables
  // and may not surface at all in the current snapshot, so — like the Processes
  // tab — give them a one-click Resume regardless of the current sort/filter.
  const resumeProcess = useCallback(
    async (p: ProcessEntry) => {
      try {
        await api.suspendProcess(p.pid, true)
        message.success(`Resumed ${p.name} (${p.pid})`)
        fetchData()
      } catch (e) {
        message.error(e instanceof Error ? e.message : 'Failed to signal process')
      }
    },
    [fetchData],
  )

  // Map each child process's PCI address (drm-pdev) to an iGPU/dGPU label, with
  // multiple same-type GPUs disambiguated by PCI address.  Mirrors the Processes
  // tab so an expanded app row shows the same per-device breakdown.
  const gpuLabelMap = buildGpuLabelMap(dyn?.gpu?.gpu_usage?.parsed?.devices)
  const gpuLabel = (pdev: string): string => gpuLabelMap.get(pdev) ?? pdev

  // Shared bits for both tables: the app-level Operation menu and the expandable
  // child-process list (joined from the full process snapshot by PID).
  const appNameCell = (label: string, pids: number[]) => (
    <Space size={6}>
      <Tag
        style={{
          margin: 0,
          padding: '0 6px',
          fontSize: 11,
          color: COLORS.textMuted,
          borderColor: COLORS.border,
          background: 'transparent',
        }}
      >
        {pids.length}
      </Tag>
      <Text style={{ color: COLORS.accent, fontWeight: 500 }} ellipsis>
        {label}
      </Text>
    </Space>
  )

  const actionsColumn = <RowT extends AppControl & { app_name?: string; name?: string }>(): ColumnsType<RowT>[number] => ({
    title: 'Operation',
    key: 'actions',
    width: 90,
    align: 'center' as const,
    render: (_: unknown, row: RowT) => (
      <ProcessActionsMenu
        target={{
          name: row.app_name || row.name || row.cmdline || String(row.pid),
          pids: row.pids.length ? row.pids : [row.pid],
          representativePid: row.pid,
          cmdline: row.cmdline,
          status: row.status,
          isSelf: row.is_self,
          balancerCandidate: row.balancer_candidate,
        }}
        balancerEnabled={balancerEnabled}
        onRegister={onRegister}
        onChanged={fetchData}
        onShowDetail={openDetail}
        allowSuspend={false}
      />
    ),
  })

  // Child-process columns for an expanded app row.  Widths are percentages so
  // the nested table lines up with the parent above; GPU columns are dropped
  // for the Disk I/O table.
  const buildChildColumns = (showGpu: boolean): ColumnsType<ProcessEntry> => [
    {
      title: 'PID',
      dataIndex: 'pid',
      key: 'pid',
      width: '7%',
      render: (v: number) => (
        <Text style={{ color: COLORS.textMuted, fontFamily: 'monospace', fontSize: 11 }}>{v}</Text>
      ),
    },
    {
      title: 'Name',
      dataIndex: 'name',
      key: 'name',
      width: '21%',
      ellipsis: true,
      render: (v: string, p: ProcessEntry) => (
        <Space size={4}>
          <Text style={{ color: COLORS.text, fontWeight: 500, fontSize: 12 }}>{v}</Text>
          {p.status === 'stopped' && (
            <Tag color="warning" style={{ fontSize: 10, lineHeight: '16px', margin: 0, padding: '0 4px' }}>
              Suspended
            </Tag>
          )}
        </Space>
      ),
    },
    {
      title: 'CPU %',
      dataIndex: 'cpu_percent',
      key: 'cpu_percent',
      width: showGpu ? '10%' : '11%',
      render: (v: number) => {
        const color = v > 80 ? COLORS.red : v > 50 ? COLORS.orange : COLORS.green
        return (
          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            <Text style={{ color, fontSize: 11 }}>{v.toFixed(1)}%</Text>
            <Progress
              percent={Math.min(v, 100)}
              showInfo={false}
              strokeColor={color}
              trailColor={COLORS.border}
              size="small"
            />
          </Space>
        )
      },
    },
    {
      title: 'Mem %',
      dataIndex: 'memory_percent',
      key: 'memory_percent',
      width: showGpu ? '8%' : '9%',
      render: (v: number) => {
        const color = v > 10 ? COLORS.orange : COLORS.text
        return <Text style={{ color, fontSize: 12 }}>{v.toFixed(1)}%</Text>
      },
    },
    {
      title: 'RSS',
      dataIndex: 'mem_rss_kb',
      key: 'mem_rss_kb',
      width: showGpu ? '9%' : '10%',
      render: (v: number) => <Text style={{ color: COLORS.text, fontSize: 12 }}>{formatMemory(v)}</Text>,
    },
    {
      title: 'Disk I/O',
      key: 'disk_io',
      width: showGpu ? '11%' : '12%',
      render: (_: unknown, p: ProcessEntry) => {
        if (p.io_read_rate == null && p.io_write_rate == null)
          return <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>—</Text>
        return (
          <Space direction="vertical" size={0} style={{ width: '100%' }}>
            <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>↓{formatRate(p.io_read_rate || 0)}</Text>
            <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>↑{formatRate(p.io_write_rate || 0)}</Text>
          </Space>
        )
      },
    },
    ...(showGpu
      ? [
          {
            title: 'GPU %',
            key: 'gpu_util',
            width: '9%',
            render: (_: unknown, p: ProcessEntry) => {
              const devs = p.gpu_devices
              if (!devs) return <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>—</Text>
              return (
                <Space direction="vertical" size={2} style={{ width: '100%' }}>
                  {Object.entries(devs).map(([pdev, s]) => {
                    const color =
                      s.gpu_util > 80 ? COLORS.red : s.gpu_util > 50 ? COLORS.orange : s.gpu_util > 0 ? COLORS.green : COLORS.textMuted
                    return (
                      <div key={pdev} style={{ display: 'flex', justifyContent: 'space-between', gap: 6 }}>
                        <Text style={{ color: COLORS.textMuted, fontSize: 10 }}>{gpuLabel(pdev)}</Text>
                        <Text style={{ color, fontSize: 11 }}>{s.gpu_util.toFixed(1)}%</Text>
                      </div>
                    )
                  })}
                </Space>
              )
            },
          },
          {
            title: 'GPU Mem',
            key: 'gpu_mem',
            width: '10%',
            render: (_: unknown, p: ProcessEntry) => {
              const devs = p.gpu_devices
              if (!devs) return <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>—</Text>
              return (
                <Space direction="vertical" size={2} style={{ width: '100%' }}>
                  {Object.entries(devs).map(([pdev, s]) => (
                    <div key={pdev} style={{ display: 'flex', justifyContent: 'space-between', gap: 6 }}>
                      <Text style={{ color: COLORS.textMuted, fontSize: 10 }}>{gpuLabel(pdev)}</Text>
                      <Text style={{ color: COLORS.text, fontSize: 11 }}>{formatMemory(s.gpu_mem_mb * 1024)}</Text>
                    </div>
                  ))}
                </Space>
              )
            },
          },
        ]
      : []),
    {
      title: 'Command',
      dataIndex: 'cmdline',
      key: 'cmdline',
      width: showGpu ? '8%' : '22%',
      ellipsis: true,
      render: (v: string) => (
        <Tooltip title={v} overlayStyle={{ maxWidth: 500 }}>
          <Text style={{ color: COLORS.textMuted, fontFamily: 'monospace', fontSize: 11 }}>{v || '—'}</Text>
        </Tooltip>
      ),
    },
    {
      title: 'Operation',
      key: 'actions',
      width: showGpu ? '7%' : '8%',
      align: 'center' as const,
      render: (_: unknown, p: ProcessEntry) => (
        <ProcessActionsMenu
          target={{
            name: p.name,
            pids: [p.pid],
            representativePid: p.pid,
            cmdline: p.cmdline,
            status: p.status,
            isSelf: p.is_self,
            balancerCandidate: p.balancer_candidate,
          }}
          balancerEnabled={balancerEnabled}
          onRegister={onRegister}
          onChanged={fetchData}
          onShowDetail={openDetail}
        />
      ),
    },
  ]

  const expandableFor = <RowT extends AppControl>(showGpu: boolean) => ({
    rowExpandable: (row: RowT) => (row.pids?.length ?? 0) > 0,
    expandedRowRender: (row: RowT) => {
      const children = (row.pids || [])
        .map((pid) => procMap.get(pid))
        .filter((p): p is ProcessEntry => !!p)
      return (
        <Table
          columns={buildChildColumns(showGpu)}
          dataSource={children.map((p) => ({ ...p, key: String(p.pid) }))}
          size="small"
          pagination={false}
          tableLayout="fixed"
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
  })

  const appColumns: ColumnsType<AppRow> = [
    {
      title: 'App Name',
      dataIndex: 'app_name',
      key: 'app_name',
      width: '28%',
      ellipsis: true,
      render: (name: string, row: AppRow) => appNameCell(name, row.pids),
      sorter: (a, b) => a.app_name.localeCompare(b.app_name),
    },
    {
      title: 'CPU %',
      dataIndex: 'cpu_usage',
      key: 'cpu_usage',
      sorter: (a, b) => a.cpu_usage - b.cpu_usage,
      render: (v: number) => {
        const pct = v * 100
        const color = pct > 80 ? COLORS.red : pct > 50 ? COLORS.orange : COLORS.green
        return (
          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            <Text style={{ color, fontSize: 12 }}>{pct.toFixed(1)}%</Text>
            <Progress
              percent={Math.min(pct, 100)}
              showInfo={false}
              strokeColor={color}
              trailColor={COLORS.border}
              size="small"
            />
          </Space>
        )
      },
    },
    {
      title: 'Memory (MB)',
      dataIndex: 'memory_mb',
      key: 'memory_mb',
      sorter: (a, b) => a.memory_mb - b.memory_mb,
      render: (v: number) => (
        <Text style={{ color: COLORS.text }}>{v.toFixed(1)}</Text>
      ),
    },
    {
      title: 'GPU Util %',
      dataIndex: 'gpu_util',
      key: 'gpu_util',
      sorter: (a, b) => a.gpu_util - b.gpu_util,
      render: (v: number) => {
        const color = v > 80 ? COLORS.red : v > 50 ? COLORS.orange : v > 0 ? COLORS.green : COLORS.textMuted
        return (
          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            <Text style={{ color, fontSize: 12 }}>{v.toFixed(1)}%</Text>
            <Progress
              percent={Math.min(v, 100)}
              showInfo={false}
              strokeColor={color}
              trailColor={COLORS.border}
              size="small"
            />
          </Space>
        )
      },
    },
    {
      title: 'GPU Mem (MB)',
      dataIndex: 'gpu_mem_mb',
      key: 'gpu_mem_mb',
      sorter: (a, b) => a.gpu_mem_mb - b.gpu_mem_mb,
      render: (v: number) => (
        <Text style={{ color: v > 0 ? COLORS.text : COLORS.textMuted }}>{v.toFixed(1)}</Text>
      ),
    },
    {
      title: 'IO Read Rate',
      dataIndex: 'io_read_rate',
      key: 'io_read_rate',
      sorter: (a, b) => a.io_read_rate - b.io_read_rate,
      render: (v: number) => (
        <Text style={{ color: COLORS.textMuted }}>{formatBytes(v)}</Text>
      ),
    },
    {
      title: 'IO Write Rate',
      dataIndex: 'io_write_rate',
      key: 'io_write_rate',
      sorter: (a, b) => a.io_write_rate - b.io_write_rate,
      render: (v: number) => (
        <Text style={{ color: COLORS.textMuted }}>{formatBytes(v)}</Text>
      ),
    },
    {
      title: 'Score',
      dataIndex: 'score',
      key: 'score',
      defaultSortOrder: 'descend',
      sorter: (a, b) => a.score - b.score,
      render: (v: number) => {
        const color = v > 80 ? COLORS.red : v > 50 ? COLORS.orange : v > 20 ? COLORS.yellow : COLORS.green
        return (
          <Tag style={{ color, borderColor: color, background: `${color}15`, fontSize: 12, fontWeight: 600 }}>
            {v.toFixed(1)}
          </Tag>
        )
      },
    },
    actionsColumn<AppRow>(),
  ]

  const diskColumns: ColumnsType<DiskIoRow> = [
    {
      title: 'App Name',
      dataIndex: 'app_name',
      key: 'app_name',
      width: '28%',
      ellipsis: true,
      render: (v: string, row: DiskIoRow) => appNameCell(v || row.name, row.pids),
    },
    {
      title: 'IO Read',
      dataIndex: 'io_read_rate',
      key: 'io_read_rate',
      sorter: (a, b) => a.io_read_rate - b.io_read_rate,
      render: (v: number) => <Text style={{ color: COLORS.text }}>{formatBytes(v)}</Text>,
    },
    {
      title: 'Read IOPS',
      dataIndex: 'io_read_iops',
      key: 'io_read_iops',
      sorter: (a, b) => a.io_read_iops - b.io_read_iops,
      render: (v: number) => (
        <Text style={{ color: COLORS.textMuted }}>{v.toFixed(0)}</Text>
      ),
    },
    {
      title: 'IO Write',
      dataIndex: 'io_write_rate',
      key: 'io_write_rate',
      sorter: (a, b) => a.io_write_rate - b.io_write_rate,
      render: (v: number) => <Text style={{ color: COLORS.textMuted }}>{formatBytes(v)}</Text>,
    },
    {
      title: 'Write IOPS',
      dataIndex: 'io_write_iops',
      key: 'io_write_iops',
      sorter: (a, b) => a.io_write_iops - b.io_write_iops,
      render: (v: number) => (
        <Text style={{ color: COLORS.textMuted }}>{v.toFixed(0)}</Text>
      ),
    },
    {
      title: 'Total I/O',
      key: 'total_io',
      defaultSortOrder: 'descend',
      sorter: (a, b) => (a.io_read_rate + a.io_write_rate) - (b.io_read_rate + b.io_write_rate),
      render: (_: unknown, row: DiskIoRow) => {
        const total = row.io_read_rate + row.io_write_rate
        const color = total > 100 ? COLORS.red : total > 50 ? COLORS.orange : COLORS.green
        return <Text style={{ color, fontWeight: 600 }}>{formatBytes(total)}</Text>
      },
    },
    actionsColumn<DiskIoRow>(),
  ]

  const tableStyle = `
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
  `

  return (
    <div style={{ padding: '16px 0' }}>
      {error && (
        <Alert
          message="API Error"
          description={error}
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
        />
      )}

      <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', marginBottom: 8, gap: 12 }}>
        <Tooltip title="Refresh now">
          <Button
            size="small"
            icon={<ReloadOutlined />}
            loading={refreshing}
            onClick={() => fetchData()}
            aria-label="Manual refresh"
          >
          </Button>
        </Tooltip>
        {lastUpdated && (
          <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>
            Updated: {lastUpdated.toLocaleTimeString()}
          </Text>
        )}
        <Button
          type="text"
          icon={<SettingOutlined style={{ fontSize: 16, color: COLORS.accent }} />}
          onClick={() => setSettingsVisible(true)}
          style={{ padding: '4px 8px' }}
          title="Configure Score Weights"
        />
      </div>

      {(() => {
        const suspended = Array.from(procMap.values()).filter((p) => p.status === 'stopped')
        if (!suspended.length) return null
        return (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={`${suspended.length} suspended process${suspended.length > 1 ? 'es' : ''}`}
            description={
              <Space size={[8, 8]} wrap>
                {suspended.map((p) => (
                  <Tag key={p.pid} style={{ margin: 0, paddingRight: 4 }}>
                    {p.name} ({p.pid})
                    <Button
                      type="link"
                      size="small"
                      style={{ padding: '0 4px', height: 'auto' }}
                      onClick={() => resumeProcess(p)}
                    >
                      Resume
                    </Button>
                  </Tag>
                ))}
              </Space>
            }
          />
        )
      })()}

      <Row gutter={[16, 16]}>
        {/* Top Resource Consumer */}
        <Col span={24}>
          <div
            style={{
              background: COLORS.panelBg,
              border: `1px solid ${COLORS.border}`,
              borderRadius: 6,
              padding: 16,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
              <Title level={5} style={{ color: COLORS.text, margin: 0 }}>
                Top Resource Consumers
              </Title>
            </div>
            <Table
              columns={appColumns}
              dataSource={rows}
              loading={loading}
              size="small"
              pagination={false}
              tableLayout="fixed"
              expandable={expandableFor<AppRow>(true)}
              rowClassName={(_, idx) => (idx % 2 === 1 ? 'table-row-alt' : '')}
              style={{ color: COLORS.text }}
              locale={{
                emptyText: (
                  <div style={{ padding: 30, color: COLORS.textMuted, textAlign: 'center' }}>
                    {loading ? <Spin /> : 'No app consumers data available'}
                  </div>
                ),
              }}
            />
            <style>{tableStyle}</style>
          </div>
        </Col>

        {/* Top Disk IO Consumer */}
        <Col span={24}>
          <div
            style={{
              background: COLORS.panelBg,
              border: `1px solid ${COLORS.border}`,
              borderRadius: 6,
              padding: 16,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
              <ThunderboltOutlined style={{ color: COLORS.orange }} />
              <Title level={5} style={{ color: COLORS.text, margin: 0 }}>
                Top Disk I/O Consumer
              </Title>
            </div>
            <Table
              columns={diskColumns}
              dataSource={diskRows}
              loading={loading}
              size="small"
              pagination={false}
              tableLayout="fixed"
              expandable={expandableFor<DiskIoRow>(false)}
              rowClassName={(_, idx) => (idx % 2 === 1 ? 'table-row-alt' : '')}
              style={{ color: COLORS.text }}
              locale={{
                emptyText: (
                  <div style={{ padding: 30, color: COLORS.textMuted, textAlign: 'center' }}>
                    {loading ? <Spin /> : 'No disk I/O consumer data available'}
                  </div>
                ),
              }}
            />
          </div>
        </Col>
      </Row>

      <SettingsModal
        visible={settingsVisible}
        onClose={() => setSettingsVisible(false)}
      />
      {detailModal}
    </div>
  )
}
