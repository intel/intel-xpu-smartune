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
  message,
} from 'antd'
import { ReloadOutlined, ThunderboltOutlined, SettingOutlined, SaveOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { COLORS } from '../styles/theme'
import { api } from '../api/client'
import type { AppResourceEntry, AppDiskIoEntry, WeightsTopData } from '../api/types'
import { usePolling } from '../hooks/usePolling'

const { Text, Title } = Typography

interface AppRow {
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

interface DiskIoRow {
  key: string
  pid: number
  name: string
  app_name: string
  cmdline: string
  io_read_rate: number
  io_write_rate: number
  io_read_iops: number
  io_write_iops: number
  score: number
}

interface Props {
  active: boolean
}

function formatBytes(mb: number): string {
  if (mb < 1) return `${(mb * 1024).toFixed(0)} KB/s`
  return `${mb.toFixed(1)} MB/s`
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

export default function AppResources({ active }: Props) {
  const [rows, setRows] = useState<AppRow[]>([])
  const [diskRows, setDiskRows] = useState<DiskIoRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [settingsVisible, setSettingsVisible] = useState(false)

  const fetchData = useCallback(async () => {
    try {
      const [resourceData, diskData] = await Promise.all([
        api.getAppResourceStats(3),
        api.getAppDiskIoStats(3),
      ])

      const appRows: AppRow[] = resourceData.apps.map((entry: AppResourceEntry) => ({
        key: entry.app_id,
        app_id: entry.app_id,
        app_name: entry.app_name,
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
        name: entry.name ?? 'Unknown',
        app_name: entry.app_name ?? '',
        cmdline: entry.cmdline ?? '',
        io_read_rate: entry.io_read_rate ?? 0,
        io_write_rate: entry.io_write_rate ?? 0,
        io_read_iops: entry.io_read_iops ?? 0,
        io_write_iops: entry.io_write_iops ?? 0,
        score: entry.score ?? 0,
      }))
      setDiskRows(dRows)

      setError(null)
      setLastUpdated(new Date())
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to fetch data')
    } finally {
      setLoading(false)
    }
  }, [])

  usePolling(fetchData, 5000, active)

  const appColumns: ColumnsType<AppRow> = [
    {
      title: 'App Name',
      dataIndex: 'app_name',
      key: 'app_name',
      width: '28%',
      ellipsis: true,
      render: (name: string) => (
        <Text style={{ color: COLORS.accent, fontWeight: 500 }}>{name}</Text>
      ),
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
  ]

  const diskColumns: ColumnsType<DiskIoRow> = [
    {
      title: 'App Name',
      dataIndex: 'app_name',
      key: 'app_name',
      width: '28%',
      ellipsis: true,
      render: (v: string, row: DiskIoRow) => (
        <Text style={{ color: COLORS.accent, fontWeight: 500 }}>{v ?? row.name}</Text>
      ),
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
        <Button
          type="text"
          icon={<SettingOutlined style={{ fontSize: 16, color: COLORS.accent }} />}
          onClick={() => setSettingsVisible(true)}
          style={{ padding: '4px 8px' }}
          title="Configure Score Weights"
        />
        {lastUpdated && (
          <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>
            <ReloadOutlined style={{ marginRight: 4 }} />
            Updated: {lastUpdated.toLocaleTimeString()}
          </Text>
        )}
        <Badge
          status="processing"
          color={COLORS.green}
          text={<Text style={{ color: COLORS.textMuted, fontSize: 11 }}>Auto-refresh 5s</Text>}
        />
      </div>

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
    </div>
  )
}
