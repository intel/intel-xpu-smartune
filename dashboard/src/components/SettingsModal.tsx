import React, { useCallback, useEffect, useState } from 'react'
import {
  Modal,
  Tabs,
  Checkbox,
  Button,
  Space,
  Typography,
  Alert,
  Spin,
  Card,
  Form,
  InputNumber,
  Radio,
  Switch,
  Row,
  Col,
  Divider,
  Tooltip,
  message,
} from 'antd'
import {
  SettingOutlined,
  SaveOutlined,
  ReloadOutlined,
  MonitorOutlined,
  ControlOutlined,
  GlobalOutlined,
  QuestionCircleOutlined,
} from '@ant-design/icons'
import { api } from '../api/client'
import type {
  MonitoredSectionsData,
  SaveResult,
  LimitPriority,
  LimitPolicyData,
} from '../api/types'
import { COLORS } from '../styles/theme'
import { useGlobalConfigNotices } from '../hooks/useGlobalConfigNotices'

const { Text } = Typography

interface Props {
  visible: boolean
  onClose: () => void
  // In monitor-only deployments (started with `-m`) the balancer is not running,
  // so the Auto Control and Network tabs are hidden — only Monitor applies.
  balancerEnabled: boolean
}

const SECTION_LABELS: Record<string, string> = {
  cpu: 'CPU',
  memory: 'Memory',
  pressure: 'Pressure',
  network: 'Network',
  disk: 'Disk I/O',
  gpu: 'GPU',
  npu: 'NPU',
}

const PRIORITIES: Array<{ key: LimitPriority; label: string }> = [
  { key: 'high', label: 'High' },
  { key: 'medium', label: 'Medium' },
  { key: 'low', label: 'Low' },
  { key: 'undefined', label: 'Undefined' },
]

function formatTimestamp(ts: number | undefined | null): string {
  if (!ts) return 'Not yet saved'
  return new Date(ts * 1000).toLocaleString()
}

// Section divider label with an optional required marker and a help tooltip.
function SectionLabel({ text, tip, required }: { text: string; tip: string; required?: boolean }) {
  return (
    <span>
      {required && <span style={{ color: '#ff4d4f', marginRight: 4 }}>*</span>}
      {text}
      <Tooltip title={tip}>
        <QuestionCircleOutlined style={{ color: COLORS.textMuted, fontSize: 12, marginLeft: 6 }} />
      </Tooltip>
    </span>
  )
}

// ---------------------------------------------------------------------------
// Reusable "load → edit form → save with optimistic-concurrency" card.
// On success it relies on the antd `message` toast (auto-dismissing); a global
// notice banner is only raised on a cross-client conflict.
// ---------------------------------------------------------------------------
interface FormCardProps {
  title: string
  description?: React.ReactNode
  scope: string
  load: () => Promise<{ values: Record<string, unknown>; updatedAt?: number }>
  save: (
    values: Record<string, unknown>,
    expectedUpdatedAt?: number,
  ) => Promise<SaveResult<{ success: boolean; updated_at: number }>>
  currentToValues: (current: Record<string, unknown>) => Record<string, unknown>
  children: React.ReactNode
}

function FormCard({ title, description, scope, load, save, currentToValues, children }: FormCardProps) {
  const [form] = Form.useForm()
  const { publishNotice } = useGlobalConfigNotices()
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [updatedAt, setUpdatedAt] = useState<number | undefined>(undefined)

  const doLoad = useCallback(async () => {
    setLoading(true)
    try {
      const { values, updatedAt: ts } = await load()
      form.setFieldsValue(values)
      setUpdatedAt(ts)
    } catch (error) {
      message.error(`Failed to load ${title}`)
      console.error(error)
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form])

  useEffect(() => {
    void doLoad()
  }, [doLoad])

  const onSave = async () => {
    let values: Record<string, unknown>
    try {
      values = await form.validateFields()
    } catch {
      return
    }
    setSaving(true)
    try {
      const result = await save(values, updatedAt)
      if (result.status === 'conflict') {
        const current = (result.current ?? {}) as Record<string, unknown>
        const tsLabel = formatTimestamp(current.updated_at as number | undefined)
        Modal.confirm({
          title: 'Settings changed by another client',
          content: (
            <p>
              {title} was updated at <b>{tsLabel}</b> while you were editing.
              Reloading will replace your values with the latest from the server.
            </p>
          ),
          okText: 'Reload latest values',
          cancelText: 'Cancel',
          onOk: () => {
            form.setFieldsValue(currentToValues(current))
            setUpdatedAt(current.updated_at as number | undefined)
            publishNotice({
              title: `${title} updated`,
              description: `Another client changed ${title} at ${tsLabel}. Your form has been reloaded.`,
              scope,
              updatedAt: current.updated_at as number | undefined,
            })
          },
        })
        return
      }

      const data = result.data
      if (data.success) {
        setUpdatedAt(data.updated_at)
        message.success(`${title} saved`)
      } else {
        message.error(`Failed to update ${title}`)
      }
    } catch (error) {
      message.error(`Failed to save ${title}`)
      console.error(error)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card size="small" title={title} style={{ marginBottom: 16 }}>
      {description && (
        <div style={{ marginBottom: 12 }}>
          <Text type="secondary">{description}</Text>
        </div>
      )}
      <Spin spinning={loading}>
        <Form form={form} layout="vertical">
          {children}
        </Form>
        <Text type="secondary" style={{ fontSize: 12 }}>
          Last saved: {formatTimestamp(updatedAt)}
        </Text>
        <div>
          <Space style={{ marginTop: 12 }}>
            <Button icon={<ReloadOutlined />} onClick={doLoad} disabled={saving}>
              Reset
            </Button>
            <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={onSave}>
              Save
            </Button>
          </Space>
        </div>
      </Spin>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Monitored dynamic sections (custom checkbox UI).
// ---------------------------------------------------------------------------
function MonitoredSectionsCard() {
  const { publishNotice } = useGlobalConfigNotices()
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [allSections, setAllSections] = useState<string[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [updatedAt, setUpdatedAt] = useState<number | undefined>(undefined)

  const applyData = useCallback((data: MonitoredSectionsData) => {
    setAllSections(data.all_sections ?? [])
    setSelected(data.sections ?? [])
    setUpdatedAt(data.updated_at)
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      applyData(await api.getMonitoredSections())
    } catch (error) {
      message.error('Failed to load monitored sections')
      console.error(error)
    } finally {
      setLoading(false)
    }
  }, [applyData])

  useEffect(() => {
    void load()
  }, [load])

  const handleSave = async () => {
    setSaving(true)
    try {
      const result = await api.updateMonitoredSections(selected, updatedAt)
      if (result.status === 'conflict') {
        const current = (result.current ?? {}) as MonitoredSectionsData
        const tsLabel = formatTimestamp(current.updated_at)
        Modal.confirm({
          title: 'Settings changed by another client',
          content: (
            <p>
              Monitored sections were updated at <b>{tsLabel}</b> while you were editing.
              Reloading will replace your selection with the latest server values.
            </p>
          ),
          okText: 'Reload latest values',
          cancelText: 'Cancel',
          onOk: () => {
            applyData(current)
            publishNotice({
              title: 'Monitored sections updated',
              description: `Another client changed monitored sections at ${tsLabel}. Your selection has been reloaded.`,
              scope: 'monitored_sections',
              updatedAt: current.updated_at,
            })
          },
        })
        return
      }
      const response = result.data
      if (response.success) {
        applyData(response)
        message.success('Monitored sections saved')
      } else {
        message.error('Failed to update monitored sections')
      }
    } catch (error) {
      message.error('Failed to save monitored sections')
      console.error(error)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card size="small" title="Monitored sections" style={{ marginBottom: 16 }}>
      <div style={{ marginBottom: 12 }}>
        <Text type="secondary">
          Which hardware sections the background collector continuously monitors. Monitored
          sections feed the live dashboard and history; unselected sections are only collected
          on demand.
        </Text>
      </div>
      <Spin spinning={loading}>
        {selected.length === 0 && !loading && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="Nothing selected — pure on-demand mode"
            description="No background collector will run and history will not be recorded until at least one section is enabled."
          />
        )}
        <Checkbox.Group
          value={selected}
          onChange={(vals) => setSelected(vals as string[])}
          style={{ display: 'flex', flexWrap: 'wrap', gap: '8px 24px' }}
        >
          {allSections.map((s) => (
            <Checkbox key={s} value={s} disabled={saving}>
              {SECTION_LABELS[s] ?? s}
            </Checkbox>
          ))}
        </Checkbox.Group>
        <div style={{ marginTop: 12 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            Last saved: {formatTimestamp(updatedAt)}
          </Text>
        </div>
        <Space style={{ marginTop: 12 }}>
          <Button icon={<ReloadOutlined />} onClick={load} disabled={saving}>
            Reset
          </Button>
          <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={handleSave}>
            Save
          </Button>
        </Space>
      </Spin>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Monitor tab: sections + collection cadence + pressure detection params.
// ---------------------------------------------------------------------------
function MonitorPanel() {
  return (
    <>
      <MonitoredSectionsCard />

      <FormCard
        title="System pressure"
        description="How the overall system-pressure score is computed and graded. Weights set each resource's relative importance; thresholds map the score to low / medium / high / critical."
        scope="system_pressure"
        load={async () => {
          const d = await api.getConfig<{
            regular_update_sys_pressure_time: number
            thresholds: Record<string, number>
            weights: Record<string, number>
            dominant_app_reduce_factor: number
            updated_at?: number
          }>('system_pressure')
          return {
            values: {
              regular_update_sys_pressure_time: d.regular_update_sys_pressure_time,
              thresholds: d.thresholds,
              weights: d.weights,
              dominant_app_reduce_factor: d.dominant_app_reduce_factor,
            },
            updatedAt: d.updated_at,
          }
        }}
        save={(values, ts) => api.updateConfig('system_pressure', values, ts)}
        currentToValues={(c) => ({
          regular_update_sys_pressure_time: c.regular_update_sys_pressure_time,
          thresholds: c.thresholds,
          weights: c.weights,
          dominant_app_reduce_factor: c.dominant_app_reduce_factor,
        })}
      >
        <Row gutter={16}>
          <Col span={8}>
            <Form.Item
              label="Update interval (s)"
              name="regular_update_sys_pressure_time"
              tooltip="How often the system-pressure level is recomputed. Lower reacts faster but costs more CPU."
              rules={[{ required: true, type: 'number', min: 1, max: 3600 }]}
            >
              <InputNumber style={{ width: '100%' }} min={1} max={3600} step={1} />
            </Form.Item>
          </Col>
        </Row>

        <Divider orientation="left" orientationMargin={0} plain style={{ margin: '4px 0 12px' }}>
          <SectionLabel
            required
            text="Level thresholds (0–1, ordered)"
            tip="Maps the 0–1 pressure score to a level. Each threshold is the lower bound of that level and they must increase in order (low < medium < high < critical)."
          />
        </Divider>
        <Row gutter={16}>
          {(['low', 'medium', 'high', 'critical'] as const).map((k) => (
            <Col span={6} key={k}>
              <Form.Item
                label={k[0].toUpperCase() + k.slice(1)}
                name={['thresholds', k]}
                required={false}
                rules={[{ required: true, type: 'number', min: 0.01, max: 1 }]}
              >
                <InputNumber style={{ width: '100%' }} min={0.01} max={1} step={0.05} />
              </Form.Item>
            </Col>
          ))}
        </Row>

        <Divider orientation="left" orientationMargin={0} plain style={{ margin: '4px 0 12px' }}>
          <SectionLabel
            required
            text="Resource weights"
            tip="Relative importance of each resource when combining them into the overall pressure score. Larger weight means that resource contributes more; the values are normalised against their sum."
          />
        </Divider>
        <Row gutter={16}>
          {(['cpu', 'memory', 'io'] as const).map((k) => (
            <Col span={8} key={k}>
              <Form.Item
                label={k === 'io' ? 'I/O' : k[0].toUpperCase() + k.slice(1)}
                name={['weights', k]}
                required={false}
                rules={[{ required: true, type: 'integer', min: 0 }]}
              >
                <InputNumber style={{ width: '100%' }} min={0} step={1} />
              </Form.Item>
            </Col>
          ))}
        </Row>

        <Row gutter={16}>
          <Col span={8}>
            <Form.Item
              label="Dominant-app factor"
              name="dominant_app_reduce_factor"
              tooltip="Damps the score when a throttled app dominates while the system is otherwise idle."
              rules={[{ required: true, type: 'number', min: 1, max: 100 }]}
            >
              <InputNumber style={{ width: '100%' }} min={1} max={100} step={0.5} />
            </Form.Item>
          </Col>
        </Row>
      </FormCard>

      <FormCard
        title="Disk I/O pressure"
        description="Each disk is judged on its own: a disk is marked busy when its own utilisation exceeds this percent (together with a throughput check). One busy disk does not mark the others busy."
        scope="disk_pressure"
        load={async () => {
          const d = await api.getConfig<{ disk_utilization_threshold: number; updated_at?: number }>('disk_pressure')
          return { values: { disk_utilization_threshold: d.disk_utilization_threshold }, updatedAt: d.updated_at }
        }}
        save={(values, ts) =>
          api.updateConfig('disk_pressure', { disk_utilization_threshold: Number(values.disk_utilization_threshold) }, ts)
        }
        currentToValues={(c) => ({ disk_utilization_threshold: c.disk_utilization_threshold })}
      >
        <Row gutter={16}>
          <Col span={8}>
            <Form.Item
              label="Disk utilisation threshold (%)"
              name="disk_utilization_threshold"
              rules={[{ required: true, type: 'number', min: 0, max: 100 }]}
            >
              <InputNumber style={{ width: '100%' }} min={0} max={100} step={1} />
            </Form.Item>
          </Col>
        </Row>
      </FormCard>

      <Alert
        type="info"
        showIcon
        message="Network I/O pressure — coming soon"
        description="Network pressure thresholds will be configurable here alongside the network control settings."
      />
    </>
  )
}

// ---------------------------------------------------------------------------
// Auto Control tab: auto resource control + full limit policy.
// ---------------------------------------------------------------------------
function LimitRateRow({ resource, disabled }: { resource: 'cpu' | 'memory'; disabled?: boolean }) {
  const form = Form.useFormInstance()
  // Per-resource off greys out its own rates; the master switch greys out everything.
  const enabled = (Form.useWatch([resource, 'enabled'], form) ?? true) as boolean
  const ratesDisabled = disabled || !enabled
  return (
    <Row gutter={12} align="bottom">
      <Col span={4}>
        <Form.Item label={resource === 'cpu' ? 'CPU' : 'Memory'} name={[resource, 'enabled']} valuePropName="checked">
          <Switch checkedChildren="On" unCheckedChildren="Off" disabled={disabled} />
        </Form.Item>
      </Col>
      {PRIORITIES.map((p) => (
        <Col span={5} key={p.key}>
          <Form.Item label={p.label} name={[resource, 'rate', p.key]} rules={[{ type: 'number', min: 0.01, max: 1 }]}>
            <InputNumber style={{ width: '100%' }} min={0.01} max={1} step={0.05} disabled={ratesDisabled} />
          </Form.Item>
        </Col>
      ))}
    </Row>
  )
}

const DISK_FIELDS: Array<{ key: 'write' | 'read' | 'write_iops' | 'read_iops'; label: string }> = [
  { key: 'write', label: 'Write MB/s' },
  { key: 'read', label: 'Read MB/s' },
  { key: 'write_iops', label: 'Write IOPS' },
  { key: 'read_iops', label: 'Read IOPS' },
]

function DiskRateMatrix({ disabled }: { disabled?: boolean }) {
  const form = Form.useFormInstance()
  // Disk I/O off greys out the whole matrix; the master switch greys out everything.
  const enabled = (Form.useWatch(['disk_io', 'enabled'], form) ?? true) as boolean
  const ratesDisabled = disabled || !enabled
  return (
    <>
      <Row gutter={8}>
        <Col span={4} />
        {DISK_FIELDS.map((f) => (
          <Col span={5} key={f.key}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {f.label}
            </Text>
          </Col>
        ))}
      </Row>
      {PRIORITIES.map((p) => (
        <Row gutter={8} align="middle" key={p.key} style={{ marginBottom: 8 }}>
          <Col span={4}>
            <Text type={ratesDisabled ? 'secondary' : undefined}>{p.label}</Text>
          </Col>
          {DISK_FIELDS.map((f) => (
            <Col span={5} key={f.key}>
              <Form.Item name={['disk_io', 'rate', p.key, f.key]} noStyle rules={[{ type: 'number', min: 1 }]}>
                <InputNumber style={{ width: '100%' }} min={1} step={f.key.endsWith('iops') ? 100 : 1} disabled={ratesDisabled} />
              </Form.Item>
            </Col>
          ))}
        </Row>
      ))}
    </>
  )
}

// Auto Control is a single card with one Save: the master "Auto resource
// control" switch gates (grays out) the limit-policy settings below, which are
// meaningless while auto control is off.  One save persists both the switch
// (passive_control section) and the limit policy (limit_policy section).
function AutoControlPanel() {
  const [form] = Form.useForm()
  const { publishNotice } = useGlobalConfigNotices()
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [enabledTs, setEnabledTs] = useState<number | undefined>(undefined)
  const [limitTs, setLimitTs] = useState<number | undefined>(undefined)
  const autoEnabled = (Form.useWatch('enabled', form) ?? true) as boolean

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [pc, lp] = await Promise.all([
        api.getPassiveControl(),
        api.getConfig<LimitPolicyData>('limit_policy'),
      ])
      form.setFieldsValue({
        enabled: pc.enabled,
        policy: lp.policy,
        cpu: lp.cpu,
        memory: lp.memory,
        disk_io: lp.disk_io,
      })
      setEnabledTs(pc.updated_at)
      setLimitTs(lp.updated_at)
    } catch (error) {
      message.error('Failed to load auto control settings')
      console.error(error)
    } finally {
      setLoading(false)
    }
  }, [form])

  useEffect(() => {
    void load()
  }, [load])

  const onSave = async () => {
    let values: Record<string, unknown>
    try {
      values = await form.validateFields()
    } catch {
      return
    }
    setSaving(true)
    try {
      const [r1, r2] = await Promise.all([
        api.updatePassiveControl(Boolean(values.enabled), enabledTs),
        api.updateConfig<LimitPolicyData>(
          'limit_policy',
          { policy: values.policy, cpu: values.cpu, memory: values.memory, disk_io: values.disk_io },
          limitTs,
        ),
      ])

      // Keep tokens for whichever call succeeded so a retry after a conflict
      // on the other one doesn't spuriously re-conflict.
      if (r1.status === 'ok') setEnabledTs(r1.data.updated_at)
      if (r2.status === 'ok') setLimitTs(r2.data.updated_at)

      if (r1.status === 'conflict' || r2.status === 'conflict') {
        Modal.confirm({
          title: 'Settings changed by another client',
          content: (
            <p>Auto control settings were changed elsewhere while you were editing. Reload the latest values?</p>
          ),
          okText: 'Reload latest values',
          cancelText: 'Cancel',
          onOk: () => {
            void load()
            publishNotice({
              title: 'Auto control updated',
              description: 'Another client changed auto control settings. Your form has been reloaded.',
              scope: 'auto_control',
            })
          },
        })
        return
      }

      if (r1.data.success && r2.data.success) {
        message.success('Auto control settings saved')
      } else {
        message.error('Failed to update auto control settings')
      }
    } catch (error) {
      message.error('Failed to save auto control settings')
      console.error(error)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card size="small" title="Auto Control" style={{ marginBottom: 16 }}>
      <Spin spinning={loading}>
        <Form form={form} layout="vertical">
          <Form.Item
            label="Auto resource control"
            name="enabled"
            valuePropName="checked"
            tooltip="When enabled, the balancer automatically throttles top apps under critical system pressure. When disabled, only manual per-app limits active in Balancer tab."
            style={{ marginBottom: 4 }}
          >
            <Switch />
          </Form.Item>
          <Text type="secondary">
            The limit policy below only applies while auto resource control is enabled.
          </Text>

          <Divider orientation="left" orientationMargin={0} plain style={{ margin: '12px 0 8px' }}>
            <SectionLabel
              text="Policy mode"
              tip="Combined applies one shared limit across all matched processes of an app; Separated applies the limit to each process individually."
            />
          </Divider>
          <Form.Item name="policy" rules={[{ required: true }]}>
            <Radio.Group disabled={!autoEnabled}>
              <Radio.Button value="combined">Combined</Radio.Button>
              <Radio.Button value="separated">Separated</Radio.Button>
            </Radio.Group>
          </Form.Item>

          <Divider orientation="left" orientationMargin={0} plain style={{ margin: '4px 0 8px' }}>
            <SectionLabel
              text="System rate (fraction of total)"
              tip="Per-priority cap on CPU / memory, as a fraction of total system capacity (0–1). A throttled app in that priority is held at or below this share. Toggle a resource off to leave it uncapped."
            />
          </Divider>
          <LimitRateRow resource="cpu" disabled={!autoEnabled} />
          <LimitRateRow resource="memory" disabled={!autoEnabled} />

          <Divider orientation="left" orientationMargin={0} plain style={{ margin: '4px 0 8px' }}>
            <SectionLabel
              text="Disk I/O rate"
              tip="Per-priority absolute disk limits (MB/s and IOPS, read and write). Toggle off to leave disk I/O uncapped."
            />
            &nbsp;
            <Form.Item name={['disk_io', 'enabled']} valuePropName="checked" noStyle>
              <Switch size="small" checkedChildren="On" unCheckedChildren="Off" disabled={!autoEnabled} />
            </Form.Item>
          </Divider>
          <DiskRateMatrix disabled={!autoEnabled} />
        </Form>

        <Text type="secondary" style={{ fontSize: 12 }}>
          Last saved: {formatTimestamp(limitTs ?? enabledTs)}
        </Text>
        <div>
          <Space style={{ marginTop: 12 }}>
            <Button icon={<ReloadOutlined />} onClick={load} disabled={saving}>
              Reset
            </Button>
            <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={onSave}>
              Save
            </Button>
          </Space>
        </div>
      </Spin>
    </Card>
  )
}

function ReservedPanel({ title, description }: { title: string; description: string }) {
  return <Alert type="info" showIcon message={title} description={description} style={{ marginTop: 8 }} />
}

export default function SettingsModal({ visible, onClose, balancerEnabled }: Props) {
  const items = [
    {
      key: 'monitor',
      label: (
        <Space>
          <MonitorOutlined />
          Monitor
        </Space>
      ),
      children: <MonitorPanel />,
    },
    // Auto Control and Network configure the balancer; in monitor-only mode the
    // balancer is not running, so these tabs are omitted entirely (matching how
    // the main Balancer tab is hidden in App.tsx).
    ...(balancerEnabled
      ? [
          {
            key: 'autocontrol',
            label: (
              <Space>
                <ControlOutlined />
                Auto Control
              </Space>
            ),
            children: <AutoControlPanel />,
          },
          {
            key: 'network',
            label: (
              <Space>
                <GlobalOutlined />
                Network
              </Space>
            ),
            children: (
              <ReservedPanel
                title="Network control settings — coming soon"
                description="Network traffic control (interface, bandwidth, per-priority limits) will be configurable here."
              />
            ),
          },
        ]
      : []),
  ]

  return (
    <Modal
      title={
        <Space>
          <SettingOutlined style={{ color: COLORS.accent }} />
          <span>Settings</span>
        </Space>
      }
      open={visible}
      onCancel={onClose}
      footer={[
        <Button key="close" onClick={onClose}>
          Close
        </Button>,
      ]}
      width={1040}
      styles={{ body: { maxHeight: '72vh', overflowY: 'auto' } }}
    >
      <Tabs tabPosition="left" items={items} style={{ minHeight: 440 }} destroyOnHidden />
    </Modal>
  )
}
