import React, { useCallback, useContext, useEffect, useRef, useState } from 'react'
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
  Tag,
  message,
} from 'antd'
import {
  SettingOutlined,
  SaveOutlined,
  ReloadOutlined,
  MonitorOutlined,
  ControlOutlined,
  DashboardOutlined,
  HddOutlined,
  GlobalOutlined,
  ApiOutlined,
  QuestionCircleOutlined,
  RightOutlined,
  DownOutlined,
  ExperimentOutlined,
} from '@ant-design/icons'
import { api } from '../api/client'
import type {
  MonitoredSectionsData,
  SaveResult,
  LimitPriority,
  LimitPolicyData,
  DiskMedia,
} from '../api/types'
import { COLORS } from '../styles/theme'
import { useGlobalConfigNotices } from '../hooks/useGlobalConfigNotices'

const { Text } = Typography

interface Props {
  visible: boolean
  onClose: () => void
  // In monitor-only deployments (started with `-m`) the balancer is not running,
  // so control settings tabs are hidden — only Monitor applies.
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

const ADVANCED_TIP =
  'Advanced options — changing them is not recommended. The shipped values are calibrated; '
  + 'a guess here degrades detection or throttling instead of tuning it.'

// Warning amber, carried over from the ADV tag this section replaced: it is what tells the
// row apart from an ordinary collapsed group. Not COLORS.orange — that one is the red-ish
// pressure colour, and reusing it here would read as an error rather than as "careful".
const ADVANCED_COLOR = '#faad14'

// Section divider label with a help tooltip. No required marker: every field in
// this dialog already has a value loaded from the server, so a red asterisk marks
// nothing the user has to supply — the field rules still reject empty/out-of-range
// input on save.
function SectionLabel({ text, tip }: { text: string; tip: string }) {
  return (
    <span>
      {text}
      <Tooltip title={tip}>
        <QuestionCircleOutlined style={{ color: COLORS.textMuted, fontSize: 12, marginLeft: 6 }} />
      </Tooltip>
    </span>
  )
}

// Calibrated model coefficients, folded away instead of tagged in place. Marking each one
// ADV still left them sitting between the settings people do come here to change; behind a
// collapsed toggle the card opens on the ordinary knobs, and everything inside is advanced
// by construction, so no per-field marker is needed once it is open.
//
// Fields inside keep their values while collapsed (antd `preserve`), and the save paths read
// the whole form store rather than the mounted fields, so a collapsed block is still written.
function AdvancedSection({ children }: { children: React.ReactNode }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <div style={{ marginTop: 8 }}>
      <Space size={4} align="center">
        <Button
          size="small"
          type="text"
          onClick={() => setExpanded((open) => !open)}
          icon={expanded
            ? <DownOutlined style={{ color: ADVANCED_COLOR, fontSize: 12 }} />
            : <RightOutlined style={{ color: ADVANCED_COLOR, fontSize: 12 }} />}
          aria-label="Toggle advanced settings"
        />
        <ExperimentOutlined style={{ color: ADVANCED_COLOR, fontSize: 12 }} />
        <Text strong style={{ color: ADVANCED_COLOR }}>Advanced</Text>
        <Tooltip title={ADVANCED_TIP}>
          <QuestionCircleOutlined style={{ color: COLORS.textMuted, fontSize: 12 }} />
        </Tooltip>
      </Space>
      {expanded && (
        <div style={{ marginTop: 12, marginLeft: 14, paddingLeft: 12, borderLeft: `2px solid ${COLORS.border}` }}>
          {children}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Saving is driven by the dialog footer: each card registers how to validate
// and persist itself, and the footer buttons drive them.  A Save per card meant
// editing three cards took three clicks, with no way to tell from the dialog
// which ones were already written -- Reset stays per card, since reloading one
// card's server values is genuinely a per-card action.
//
// Each card is now a tab of its own, so a handle also records which tab owns it:
// "Save" writes the tab in view, "Save all" writes every tab the user has
// touched. Panes are kept mounted once visited (see the Tabs below), so an edit
// left behind on another tab survives the switch and is still saved.
// ---------------------------------------------------------------------------
type SaveOutcome = 'ok' | 'conflict' | 'error'

interface SaveHandle {
  // Optional: cards without a form (checkbox lists) have nothing to validate.
  validate?: () => Promise<boolean>
  // Untouched cards are skipped, so saving one tab does not bump the
  // concurrency token of every section on it (which would make another client's
  // in-progress edit conflict for no reason).
  isDirty?: () => boolean
  save: () => Promise<SaveOutcome>
}

interface SaveRegistry {
  register: (id: string, tabKey: string, handle: SaveHandle) => () => void
  // Cards ping this when their edit state may have changed, so the dialog can
  // re-read isDirty() and mark the tab. Cheap: one call per handle, no polling.
  notifyDirty: () => void
}

const SettingsSaveContext = React.createContext<SaveRegistry | null>(null)
// Which tab the surrounding panel belongs to; wrapped around each panel in the items list.
const SettingsTabContext = React.createContext<string>('')

// Registers *handle* for as long as the calling card is mounted. The registry
// stores a stable wrapper that reads the newest closure, so a re-render with
// fresh form state never leaves a stale handler behind.
function useRegisterSave(id: string, handle: SaveHandle) {
  const registry = useContext(SettingsSaveContext)
  const tabKey = useContext(SettingsTabContext)
  const latest = useRef(handle)
  latest.current = handle
  useEffect(
    () => registry?.register(id, tabKey, {
      validate: () => Promise.resolve(latest.current.validate?.() ?? true),
      isDirty: () => latest.current.isDirty?.() ?? true,
      save: () => latest.current.save(),
    }),
    [registry, id, tabKey],
  )
}

// Lets a card report "my edit state changed" without threading props through it.
function useNotifyDirty() {
  const registry = useContext(SettingsSaveContext)
  return useCallback(() => registry?.notifyDirty(), [registry])
}

// ---------------------------------------------------------------------------
// Reusable "load → edit form → save with optimistic-concurrency" card.
// Saving is driven by the dialog footer; a global notice banner is only raised
// on a cross-client conflict.
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
  const notifyDirty = useNotifyDirty()
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [updatedAt, setUpdatedAt] = useState<number | undefined>(undefined)

  // Edited-since-load flag, kept here rather than read from form.isFieldsTouched(): a field
  // that is collapsed again (Advanced settings) unmounts and takes its touched flag with it,
  // which would make an edited card look clean and have Save skip it.
  const dirty = useRef(false)
  const markDirty = useCallback(() => {
    dirty.current = true
    notifyDirty()
  }, [notifyDirty])
  // Nothing is pending once the card holds server values again (saved, or reloaded from
  // the server after a cross-client conflict).
  const clearDirty = useCallback(() => {
    dirty.current = false
    notifyDirty()
  }, [notifyDirty])

  const doLoad = useCallback(async () => {
    setLoading(true)
    try {
      const { values, updatedAt: ts } = await load()
      form.resetFields()
      form.setFieldsValue(values)
      setUpdatedAt(ts)
      dirty.current = false
      notifyDirty()
    } catch (error) {
      message.error(`Failed to load ${title}`)
      console.error(error)
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form, notifyDirty])

  useEffect(() => {
    void doLoad()
  }, [doLoad])

  const onSave = async (): Promise<SaveOutcome> => {
    try {
      await form.validateFields()
    } catch {
      return 'error'
    }
    // Validate the mounted fields, but send the whole store: `validateFields()` resolves
    // with the *mounted* entities only, so a collapsed section (Advanced settings, a
    // switched-off channel) would drop its keys from the PUT and wipe them server-side.
    // Unmounted fields keep their loaded values in the store (antd `preserve`).
    const values = form.getFieldsValue(true) as Record<string, unknown>
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
            clearDirty()
            publishNotice({
              title: `${title} updated`,
              description: `Another client changed ${title} at ${tsLabel}. Your form has been reloaded.`,
              scope,
              updatedAt: current.updated_at as number | undefined,
            })
          },
        })
        return 'conflict'
      }

      const data = result.data
      if (data.success) {
        setUpdatedAt(data.updated_at)
        clearDirty()
        return 'ok'
      }
      message.error(`Failed to update ${title}`)
      return 'error'
    } catch (error) {
      message.error(`Failed to save ${title}`)
      console.error(error)
      return 'error'
    } finally {
      setSaving(false)
    }
  }

  const validate = async () => {
    try {
      await form.validateFields()
      return true
    } catch {
      message.error(`${title}: fix the highlighted fields before saving`)
      return false
    }
  }

  useRegisterSave(scope, { validate, isDirty: () => dirty.current, save: onSave })

  return (
    <Card size="small" title={title} style={{ marginBottom: 16 }}>
      {description && (
        <div style={{ marginBottom: 12 }}>
          <Text type="secondary">{description}</Text>
        </div>
      )}
      <Spin spinning={loading}>
        <Form form={form} layout="vertical" onFieldsChange={markDirty}>
          {children}
        </Form>
        <Text type="secondary" style={{ fontSize: 12 }}>
          Last saved: {formatTimestamp(updatedAt)}
        </Text>
        <div>
          <Button
            icon={<ReloadOutlined />}
            onClick={doLoad}
            disabled={saving}
            style={{ marginTop: 12 }}
          >
            Reset
          </Button>
        </div>
      </Spin>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// System Overview tab: which resource sections the collector samples, which is
// also what the main System Overview tab can show (custom checkbox UI).
// ---------------------------------------------------------------------------
function MonitoredSectionsCard() {
  const { publishNotice } = useGlobalConfigNotices()
  const notifyDirty = useNotifyDirty()
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [allSections, setAllSections] = useState<string[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [updatedAt, setUpdatedAt] = useState<number | undefined>(undefined)
  // No Form here to ask, so track edits by hand (see SaveHandle.isDirty).
  const [dirty, setDirty] = useState(false)

  const applyData = useCallback((data: MonitoredSectionsData) => {
    setAllSections(data.all_sections ?? [])
    setSelected(data.sections ?? [])
    setUpdatedAt(data.updated_at)
    setDirty(false)
    notifyDirty()
  }, [notifyDirty])

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

  const handleSave = async (): Promise<SaveOutcome> => {
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
        return 'conflict'
      }
      const response = result.data
      if (response.success) {
        applyData(response)
        return 'ok'
      }
      message.error('Failed to update monitored sections')
      return 'error'
    } catch (error) {
      message.error('Failed to save monitored sections')
      console.error(error)
      return 'error'
    } finally {
      setSaving(false)
    }
  }

  useRegisterSave('monitored_sections', { isDirty: () => dirty, save: handleSave })

  return (
    <Card size="small" title="System Overview" style={{ marginBottom: 16 }}>
      <div style={{ marginBottom: 12 }}>
        <Text type="secondary">
          Which hardware sections appear on the main <b>System Overview</b> tab. A selected
          section is sampled continuously by the background collector, so it is shown live
          there and recorded to History. Unselected sections are hidden from that tab and are
          only sampled on demand (for example when a per-app view asks for them).
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
          onChange={(vals) => {
            setSelected(vals as string[])
            setDirty(true)
            notifyDirty()
          }}
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
        <Button icon={<ReloadOutlined />} onClick={load} disabled={saving} style={{ marginTop: 12 }}>
          Reset
        </Button>
      </Spin>
    </Card>
  )
}

// The disk channel's config mirrors config.yaml, where the model knobs are nested one
// level down; the form keeps them flat.
interface DiskPressureConfig {
  disk_thresholds: Record<string, number>
  disk_pressure_model: {
    sub_weights: Record<string, number>
    sigmoid_k: number
    max_p_weight: number
  }
  updated_at?: number
}

// Thresholds are ratios in config.yaml and percentages in the form; every pressure card
// converts them the same way, so the helpers live next to the type instead of inside a card.
const toPercentageThresholds = (thresholds: Record<string, number>) =>
  Object.fromEntries(Object.entries(thresholds).map(([level, value]) => [level, Math.round(value * 100)]))
const toRatioThresholds = (thresholds: Record<string, number>) =>
  Object.fromEntries(Object.entries(thresholds).map(([level, value]) => [level, value / 100]))
const diskPressureToValues = (d: DiskPressureConfig) => ({
  disk_thresholds: toPercentageThresholds(d.disk_thresholds ?? {}),
  sub_weights: d.disk_pressure_model?.sub_weights,
  sigmoid_k: d.disk_pressure_model?.sigmoid_k,
  max_p_weight: d.disk_pressure_model?.max_p_weight,
})

// ---------------------------------------------------------------------------
// System pressure tab: how the overall score is computed and graded.
// ---------------------------------------------------------------------------
function SystemPressurePanel() {
  return (
    <>
      <FormCard
        title="System pressure"
        description="How the overall system-pressure score is computed and graded. Weights set each resource's relative importance; thresholds map the score to low / medium / high / critical."
        scope="system_pressure"
        load={async () => {
          const d = await api.getConfig<{
            regular_update_sys_pressure_time: number
            thresholds: Record<string, number>
            weights: Record<string, number>
            mem_gate_steepness: number
            memory_busy_threshold: number
            cpu_busy_threshold: number
            updated_at?: number
          }>('system_pressure')
          return {
            values: {
              regular_update_sys_pressure_time: d.regular_update_sys_pressure_time,
              thresholds: toPercentageThresholds(d.thresholds),
              weights: d.weights,
              mem_gate_steepness: d.mem_gate_steepness,
              memory_busy_threshold: d.memory_busy_threshold,
              cpu_busy_threshold: d.cpu_busy_threshold,
            },
            updatedAt: d.updated_at,
          }
        }}
        save={(values, ts) => api.updateConfig('system_pressure', {
          ...values,
          thresholds: toRatioThresholds(values.thresholds as Record<string, number>),
        }, ts)}
        currentToValues={(c) => ({
          regular_update_sys_pressure_time: c.regular_update_sys_pressure_time,
          thresholds: toPercentageThresholds(c.thresholds as Record<string, number>),
          weights: c.weights,
          mem_gate_steepness: c.mem_gate_steepness,
          memory_busy_threshold: c.memory_busy_threshold,
          cpu_busy_threshold: c.cpu_busy_threshold,
        })}
      >
        <Row gutter={16}>
          <Col span={8}>
            <Form.Item
              label="Update interval (s)"
              name="regular_update_sys_pressure_time"
              tooltip="How often the system-pressure level is recomputed. Lower reacts faster but costs more CPU."
              required={false}
              rules={[{ required: true, type: 'number', min: 1, max: 3600 }]}
            >
              <InputNumber style={{ width: '100%' }} min={1} max={3600} step={1} />
            </Form.Item>
          </Col>
        </Row>

        <Divider orientation="left" orientationMargin={0} plain style={{ margin: '4px 0 12px' }}>
          <SectionLabel
            text="Level thresholds (%, ordered)"
            tip="Maps the system pressure score to a level. Each threshold is the lower bound of that level and they must increase in order (low < medium < high < critical)."
          />
        </Divider>
        <Row gutter={16}>
          {(['low', 'medium', 'high', 'critical'] as const).map((k) => (
            <Col span={6} key={k}>
              <Form.Item
                label={k[0].toUpperCase() + k.slice(1)}
                name={['thresholds', k]}
                required={false}
                rules={[{ required: true, type: 'number', min: 1, max: 100 }]}
              >
                <InputNumber style={{ width: '100%' }} min={1} max={100} step={1} addonAfter="%" />
              </Form.Item>
            </Col>
          ))}
        </Row>

        <AdvancedSection>
          <Divider orientation="left" orientationMargin={0} plain style={{ margin: '0 0 12px' }}>
            <SectionLabel
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
                label="Memory gate steepness"
                name="mem_gate_steepness"
                tooltip="Steepness of the memory-discount sigmoid gate: larger makes the transition around the memory busy point sharper."
                required={false}
                rules={[{ required: true, type: 'number', min: 1, max: 50 }]}
              >
                <InputNumber style={{ width: '100%' }} min={1} max={50} step={0.5} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                label="Memory busy threshold (%)"
                name="memory_busy_threshold"
                tooltip="Memory usage percentage where memory pressure starts to be treated as busy."
                required={false}
                rules={[{ required: true, type: 'number', min: 0, max: 100 }]}
              >
                <InputNumber style={{ width: '100%' }} min={0} max={100} step={1} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                label="CPU busy threshold (%)"
                name="cpu_busy_threshold"
                tooltip="CPU usage percentage where CPU pressure starts to be treated as busy."
                required={false}
                rules={[{ required: true, type: 'number', min: 0, max: 100 }]}
              >
                <InputNumber style={{ width: '100%' }} min={0} max={100} step={1} />
              </Form.Item>
            </Col>
          </Row>
        </AdvancedSection>
      </FormCard>
    </>
  )
}

// ---------------------------------------------------------------------------
// Disk I/O pressure tab: the disk channel's own scoring model and bands.
// ---------------------------------------------------------------------------
function DiskPressurePanel() {
  return (
    <>
      <FormCard
        title="Disk I/O pressure"
        description="A channel of its own: each disk is scored from its latency, queue depth and utilisation (normalised against what its media class can sustain), the disks are combined, and the I/O PSI decides how much of that saturation counts. Critical on this channel is what authorises an io.max cap — it never feeds the system pressure score above."
        scope="disk_pressure"
        load={async () => {
          const d = await api.getConfig<DiskPressureConfig>('disk_pressure')
          return { values: diskPressureToValues(d), updatedAt: d.updated_at }
        }}
        save={(values, ts) =>
          api.updateConfig('disk_pressure', {
            disk_thresholds: toRatioThresholds(values.disk_thresholds as Record<string, number>),
            disk_pressure_model: {
              sub_weights: values.sub_weights,
              sigmoid_k: Number(values.sigmoid_k),
              max_p_weight: Number(values.max_p_weight),
            },
          }, ts)
        }
        currentToValues={(c) => diskPressureToValues(c as unknown as DiskPressureConfig)}
      >
        <Divider orientation="left" orientationMargin={0} plain style={{ margin: '4px 0 12px' }}>
          <SectionLabel
            text="Level thresholds (%, ordered)"
            tip="Bands of the disk-IO score, separate from the system ones. Low/high are also the ends of the ramp that decides how much of an I/O stall is blamed on the local disk; medium marks a disk busy in the UI; critical triggers throttling."
          />
        </Divider>
        <Row gutter={16}>
          {(['low', 'medium', 'high', 'critical'] as const).map((k) => (
            <Col span={6} key={k}>
              <Form.Item
                label={k[0].toUpperCase() + k.slice(1)}
                name={['disk_thresholds', k]}
                required={false}
                rules={[{ required: true, type: 'number', min: 1, max: 100 }]}
              >
                <InputNumber style={{ width: '100%' }} min={1} max={100} step={1} addonAfter="%" />
              </Form.Item>
            </Col>
          ))}
        </Row>

        <AdvancedSection>
          <Divider orientation="left" orientationMargin={0} plain style={{ margin: '0 0 12px' }}>
            <SectionLabel
              text="Per-disk sub-signal weights (sum ≤ 1)"
              tip="How much each USE sub-signal contributes to a single disk's pressure. Latency dominates because it is what users feel; utilisation is only a tie-breaker, since a parallel device sits at 100% with headroom to spare."
            />
          </Divider>
          <Row gutter={16}>
            {([
              ['latency', 'Latency (await)'],
              ['queue', 'Queue depth'],
              ['util', 'Utilisation'],
            ] as const).map(([key, label]) => (
              <Col span={8} key={key}>
                <Form.Item
                  label={label}
                  name={['sub_weights', key]}
                  required={false}
                  rules={[{ required: true, type: 'number', min: 0, max: 1 }]}
                >
                  <InputNumber style={{ width: '100%' }} min={0} max={1} step={0.05} />
                </Form.Item>
              </Col>
            ))}
          </Row>

          <Row gutter={16}>
            <Col span={8}>
              <Form.Item
                label="Sigmoid steepness"
                name="sigmoid_k"
                tooltip="Steepness of the curve that squashes each sub-signal around its media-specific half-point: larger is more switch-like, smaller is a gentler ramp."
                required={false}
                rules={[{ required: true, type: 'number', min: 1, max: 50 }]}
              >
                <InputNumber style={{ width: '100%' }} min={1} max={50} step={0.5} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                label="Worst-disk weight"
                name="max_p_weight"
                tooltip="How strongly the single busiest disk carries the aggregate: 0 is a plain mean (one hammered disk among many is averaged away), 1 lets the worst disk decide on its own."
                required={false}
                rules={[{ required: true, type: 'number', min: 0, max: 1 }]}
              >
                <InputNumber style={{ width: '100%' }} min={0} max={1} step={0.05} />
              </Form.Item>
            </Col>
          </Row>
        </AdvancedSection>
      </FormCard>
    </>
  )
}

// ---------------------------------------------------------------------------
// Network I/O pressure tab: utilisation bands for the network channel.
// ---------------------------------------------------------------------------
function NetworkPressurePanel() {
  return (
    <>
      <FormCard
        title="Network I/O pressure"
        description="Bands for the network channel: the utilisation of the monitored interfaces is graded against these thresholds, and the level drives the pressure-based bandwidth shaping in Network Control."
        scope="network_pressure"
        load={async () => {
          const d = await api.getConfig<{
            network_thresholds: Record<string, number>
            network_pressure_model?: { rx_drop_weight?: number }
            updated_at?: number
          }>('network_pressure')
          return {
            values: {
              network_thresholds: toPercentageThresholds(d.network_thresholds),
              network_pressure_model_rx_drop_weight: Number(d.network_pressure_model?.rx_drop_weight ?? 0.5),
            },
            updatedAt: d.updated_at,
          }
        }}
        save={(values, ts) => api.updateConfig('network_pressure', {
          network_thresholds: toRatioThresholds(values.network_thresholds as Record<string, number>),
          network_pressure_model: {
            rx_drop_weight: Number(values.network_pressure_model_rx_drop_weight),
          },
        }, ts)}
        currentToValues={(c) => ({
          network_thresholds: toPercentageThresholds(c.network_thresholds as Record<string, number>),
          network_pressure_model_rx_drop_weight: Number(
            ((c.network_pressure_model as { rx_drop_weight?: number } | undefined)?.rx_drop_weight) ?? 0.5,
          ),
        })}
      >
        <Divider orientation="left" orientationMargin={0} plain style={{ margin: '4px 0 12px' }}>
          <SectionLabel
            text="Network pressure thresholds (%, ordered)"
            tip="Maps the fused network pressure score to low, medium, high, and critical. The score combines near-saturation utilisation with congestion distress such as drops, FIFO overflow, and softnet pressure. Thresholds must increase in that order."
          />
        </Divider>
        <Row gutter={16}>
          {(['low', 'medium', 'high', 'critical'] as const).map((level) => (
            <Col span={6} key={level}>
              <Form.Item
                label={level[0].toUpperCase() + level.slice(1)}
                name={['network_thresholds', level]}
                required={false}
                rules={[{ required: true, type: 'number', min: 1, max: 100 }]}
              >
                <InputNumber style={{ width: '100%' }} min={1} max={100} step={1} addonAfter="%" />
              </Form.Item>
            </Col>
          ))}
        </Row>

        <AdvancedSection>
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item
                label="RX drop weight"
                name="network_pressure_model_rx_drop_weight"
                tooltip="Weight of generic rx_dropped distress in RX pressure scoring. Lower values make RX drops contribute less to pressure; valid range is 0 to 1."
                required={false}
                rules={[{ required: true, type: 'number', min: 0, max: 1 }]}
              >
                <InputNumber style={{ width: '100%' }} min={0} max={1} step={0.05} />
              </Form.Item>
            </Col>
          </Row>
        </AdvancedSection>
      </FormCard>
    </>
  )
}

// ---------------------------------------------------------------------------
// Control tab content: Smartune control + network control.
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
          <Form.Item label={p.label} name={[resource, 'rate', p.key]} rules={[{ type: 'number', min: 1, max: 100 }]}>
            <InputNumber style={{ width: '100%' }} min={1} max={100} step={1} addonAfter="%" disabled={ratesDisabled} />
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

// Rendered only while disk I/O control is on (the parent collapses this section with the
// switch), so `disabled` here carries just the master system-control gate.
function DiskRateMatrix({ disabled }: { disabled?: boolean }) {
  const ratesDisabled = disabled
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

const DISK_MEDIA: Array<{ key: DiskMedia; label: string }> = [
  { key: 'nvme', label: 'NVMe' },
  { key: 'sata_ssd', label: 'SATA SSD' },
  { key: 'mmc', label: 'eMMC / SD' },
  { key: 'hdd', label: 'HDD' },
  { key: 'usb', label: 'USB' },
  { key: 'unknown', label: 'Unknown' },
]

// The rate matrix above is one row of numbers for the whole machine, but 30 MB/s is idle on an
// NVMe and more than a thumb drive can deliver.  This scales the written cap per media class so
// a priority means the same fraction of what the device can actually do.
function DiskScaleMatrix({ disabled }: { disabled?: boolean }) {
  return (
    <Row gutter={[8, 8]}>
      {DISK_MEDIA.map((m) => (
        <Col span={8} key={m.key}>
          <Form.Item
            label={<Text type="secondary" style={{ fontSize: 12 }}>{m.label}</Text>}
            name={['disk_io', 'media_scale', m.key]}
            rules={[{ type: 'number', min: 0.01, max: 1 }]}
            style={{ marginBottom: 0 }}
          >
            <InputNumber style={{ width: '100%' }} min={0.01} max={1} step={0.05}
                         addonAfter="x" disabled={disabled} />
          </Form.Item>
        </Col>
      ))}
    </Row>
  )
}

// A different question from the scale above, and the reason the two are no longer one table:
// this is not how hard to cap an app, it is how much I/O an app must already be doing on a
// disk before capping it is worth doing at all.  Below the floor a cap cannot relieve the
// device, so the app is left alone and the next candidate is considered.
function DiskFloorMatrix({ disabled }: { disabled?: boolean }) {
  return (
    <>
      <Row gutter={8}>
        <Col span={6} />
        <Col span={9}>
          <Text type="secondary" style={{ fontSize: 12 }}>Bandwidth (MB/s)</Text>
        </Col>
        <Col span={9}>
          <Text type="secondary" style={{ fontSize: 12 }}>IOPS</Text>
        </Col>
      </Row>
      {DISK_MEDIA.map((m) => (
        <Row gutter={8} align="middle" key={m.key} style={{ marginBottom: 8 }}>
          <Col span={6}>
            <Text type={disabled ? 'secondary' : undefined}>{m.label}</Text>
          </Col>
          <Col span={9}>
            <Form.Item name={['disk_io', 'candidate_floor', m.key, 'mb_s']} noStyle rules={[{ type: 'number', min: 0.1 }]}>
              <InputNumber style={{ width: '100%' }} min={0.1} step={1} disabled={disabled} />
            </Form.Item>
          </Col>
          <Col span={9}>
            <Form.Item name={['disk_io', 'candidate_floor', m.key, 'iops']} noStyle rules={[{ type: 'number', min: 1 }]}>
              <InputNumber style={{ width: '100%' }} min={1} step={50} disabled={disabled} />
            </Form.Item>
          </Col>
        </Row>
      ))}
    </>
  )
}

// Smartune Control holds System Control and Disk I/O Control as two nested cards, but they
// share ONE form and one save: both live in the same `limit_policy` config section, so two
// independent saves would race on its optimistic-concurrency token and the second would
// always conflict. The master "auto control" switch and the policy mode sit on the outer
// card because they gate and shape both channels, not just CPU/memory.
function AutoControlPanel() {
  const [form] = Form.useForm()
  const { publishNotice } = useGlobalConfigNotices()
  const notifyDirty = useNotifyDirty()
  const [policyExpanded, setPolicyExpanded] = useState(false)
  const [diskExpanded, setDiskExpanded] = useState(false)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [enabledTs, setEnabledTs] = useState<number | undefined>(undefined)
  const [limitTs, setLimitTs] = useState<number | undefined>(undefined)
  const autoEnabled = (Form.useWatch('enabled', form) ?? false) as boolean
  const diskEnabled = (Form.useWatch(['disk_io', 'enabled'], form) ?? false) as boolean
  const toPercentageRates = (resource: LimitPolicyData['cpu']) => ({
    ...resource,
    rate: Object.fromEntries(Object.entries(resource?.rate ?? {}).map(([priority, rate]) => [priority, Math.round(Number(rate) * 100)])),
  })
  const toRatioRates = (resource: LimitPolicyData['cpu']) => ({
    ...resource,
    rate: Object.fromEntries(Object.entries(resource?.rate ?? {}).map(([priority, rate]) => [priority, Number(rate) / 100])),
  })

  // Same flag and the same reason as FormCard: the disk knobs inside Advanced settings
  // unmount when collapsed, so a touched-based check would lose the edit.
  const dirty = useRef(false)
  const markDirty = useCallback(() => {
    dirty.current = true
    notifyDirty()
  }, [notifyDirty])
  const clearDirty = useCallback(() => {
    dirty.current = false
    notifyDirty()
  }, [notifyDirty])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [pc, lp] = await Promise.all([
        api.getPassiveControl(),
        api.getConfig<LimitPolicyData>('limit_policy'),
      ])
      form.resetFields()
      form.setFieldsValue({
        enabled: pc.enabled,
        policy: lp.policy,
        cpu: toPercentageRates(lp.cpu),
        memory: toPercentageRates(lp.memory),
        disk_io: lp.disk_io,
      })
      setPolicyExpanded(Boolean(pc.enabled))
      setDiskExpanded(Boolean(lp.disk_io?.enabled))
      setEnabledTs(pc.updated_at)
      setLimitTs(lp.updated_at)
      dirty.current = false
      notifyDirty()
    } catch (error) {
      message.error('Failed to load Smartune control settings')
      console.error(error)
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form, notifyDirty])

  useEffect(() => {
    void load()
  }, [load])

  const onSave = async (): Promise<SaveOutcome> => {
    try {
      await form.validateFields()
    } catch {
      return 'error'
    }
    // Whole store, not just the mounted fields: with auto control off (or the disk section
    // collapsed) `validateFields()` returns only `enabled`, which used to leave cpu/memory
    // undefined here. See the same note in FormCard.onSave.
    const values = form.getFieldsValue(true) as Record<string, unknown>
    setSaving(true)
    try {
      const policyValues = values as unknown as LimitPolicyData
      const [r1, r2] = await Promise.all([
        api.updatePassiveControl(Boolean(values.enabled), enabledTs),
        api.updateConfig<LimitPolicyData>(
          'limit_policy',
          {
            policy: policyValues.policy,
            cpu: toRatioRates(policyValues.cpu),
            memory: toRatioRates(policyValues.memory),
            disk_io: policyValues.disk_io,
          },
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
            <p>Smartune control settings were changed elsewhere while you were editing. Reload the latest values?</p>
          ),
          okText: 'Reload latest values',
          cancelText: 'Cancel',
          onOk: () => {
            void load()
            publishNotice({
              title: 'Smartune control updated',
              description: 'Another client changed Smartune control settings. Your form has been reloaded.',
              scope: 'auto_control',
            })
          },
        })
        return 'conflict'
      }

      if (r1.data.success && r2.data.success) {
        clearDirty()
        return 'ok'
      }
      message.error('Failed to update Smartune control settings')
      return 'error'
    } catch (error) {
      message.error('Failed to save Smartune control settings')
      console.error(error)
      return 'error'
    } finally {
      setSaving(false)
    }
  }

  const validate = async () => {
    try {
      await form.validateFields()
      return true
    } catch {
      message.error('Smartune control: fix the highlighted fields before saving')
      return false
    }
  }

  useRegisterSave('auto_control', { validate, isDirty: () => dirty.current, save: onSave })

  const resetButton = (
    <Button icon={<ReloadOutlined />} onClick={load} disabled={saving} style={{ marginTop: 12 }}>
      Reset
    </Button>
  )

  return (
    <Spin spinning={loading}>
      {/* One Form spanning the whole block: the master switch, the policy mode and both
          per-channel sections are one config section server-side, so they are saved and
          reset together. The switch and the policy mode sit on the outer card because
          they govern both channels, not just CPU/memory. */}
      <Form form={form} layout="vertical" onFieldsChange={markDirty}>
        <Card size="small" title="Smartune Control" style={{ marginBottom: 16 }}>
          <Form.Item
            label={
              <Space size={4}>
                <Button
                  size="small"
                  type="text"
                  onClick={() => setPolicyExpanded((expanded) => !expanded)}
                  disabled={!autoEnabled}
                  icon={policyExpanded
                    ? <DownOutlined style={{ color: COLORS.textMuted, fontSize: 12 }} />
                    : <RightOutlined style={{ color: COLORS.textMuted, fontSize: 12 }} />}
                  aria-label="Toggle Smartune control details"
                />
                <span>Auto control</span>
              </Space>
            }
            name="enabled"
            valuePropName="checked"
            tooltip="When enabled, the balancer automatically throttles top apps under critical pressure. When disabled, only manual per-app limits active in Balancer tab. Gates both the system and the disk I/O control below."
            style={{ marginBottom: 4 }}
          >
            <Switch checkedChildren="On" unCheckedChildren="Off" onChange={setPolicyExpanded} />
          </Form.Item>
          {policyExpanded && (
            <>
              <Text type="secondary">
                The policy below only applies while auto control is enabled.
              </Text>

              <Divider orientation="left" orientationMargin={0} plain style={{ margin: '12px 0 8px' }}>
                <SectionLabel
                  text="Policy mode"
                  tip="How the disk-IO channel relates to the CPU/memory one. Separated: disk I/O is judged on its own thresholds and its own top-consumer list, so a saturated disk is throttled even while CPU and memory are calm, and each channel recovers on its own timer. Combined: there is only the system pressure score (disk I/O enters it as the I/O weight), so caps are applied only when that score is critical, and CPU/memory + disk caps are applied and lifted together."
                />
              </Divider>
              <Form.Item name="policy" rules={[{ required: true }]} style={{ marginBottom: 4 }}>
                <Radio.Group disabled={!autoEnabled}>
                  <Radio.Button value="combined">Combined</Radio.Button>
                  <Radio.Button value="separated">Separated</Radio.Button>
                </Radio.Group>
              </Form.Item>
              <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 12 }}>
                Separated is recommended when disk I/O matters on its own: under Combined, disk
                pressure only contributes the I/O weight to the system score, so a disk-only
                storm rarely reaches critical and may never be throttled.
              </Text>

              <Card size="small" type="inner" title="System Control" style={{ marginBottom: 12 }}>
                <Divider orientation="left" orientationMargin={0} plain style={{ margin: '0 0 8px' }}>
                  <SectionLabel
                    text="System rate (%)"
                    tip="Per-priority CPU and memory cap as a percentage of total system capacity. A throttled app in that priority is held at or below this share. Toggle a resource off to leave it uncapped."
                  />
                </Divider>
                <LimitRateRow resource="cpu" disabled={!autoEnabled} />
                <LimitRateRow resource="memory" disabled={!autoEnabled} />
              </Card>

              <Card size="small" type="inner" title="Disk I/O Control" style={{ marginBottom: 4 }}>
                <Space size={4} align="center">
                  <Button
                    size="small"
                    type="text"
                    onClick={() => setDiskExpanded((expanded) => !expanded)}
                    disabled={!autoEnabled || !diskEnabled}
                    icon={diskExpanded && diskEnabled
                      ? <DownOutlined style={{ color: COLORS.textMuted, fontSize: 12 }} />
                      : <RightOutlined style={{ color: COLORS.textMuted, fontSize: 12 }} />}
                    aria-label="Toggle disk I/O policy details"
                  />
                  <Text strong>Disk I/O rate</Text>
                  <Tooltip title="Per-priority absolute disk caps (MB/s and IOPS, read and write) written as io.max. Off leaves disk I/O uncapped, and the details below do not apply.">
                    <QuestionCircleOutlined style={{ color: COLORS.textMuted, fontSize: 12 }} />
                  </Tooltip>
                  <Form.Item name={['disk_io', 'enabled']} valuePropName="checked" noStyle>
                    <Switch
                      size="small"
                      checkedChildren="On"
                      unCheckedChildren="Off"
                      disabled={!autoEnabled}
                      onChange={setDiskExpanded}
                    />
                  </Form.Item>
                </Space>

                {/* Collapsed while disk I/O control is off: the rates, the per-disk scale and the
                    floors are all meaningless without a cap to apply them to. */}
                {diskEnabled && diskExpanded && (
                  <div style={{ marginTop: 12 }}>
                    <DiskRateMatrix disabled={!autoEnabled} />

                    <AdvancedSection>
                      <Divider orientation="left" orientationMargin={0} plain style={{ margin: '0 0 8px' }}>
                        <SectionLabel
                          text="Per-disk adjustment"
                          tip="The rates above are calibrated for NVMe. The cap written to a disk is rate x this factor, so a priority means the same fraction of what that device can actually do instead of the same absolute MB/s."
                        />
                      </Divider>
                      <DiskScaleMatrix disabled={!autoEnabled} />

                      <Divider orientation="left" orientationMargin={0} plain style={{ margin: '16px 0 8px' }}>
                        <SectionLabel
                          text="Minimum app I/O worth throttling"
                          tip="Not how hard to cap, but whether capping helps at all: an app must already be doing at least this much on a disk of that media before it is considered a throttle candidate. Below it, capping cannot relieve the device, so the app is skipped and the next-heaviest consumer is considered. Either bandwidth or IOPS clearing the bar qualifies, because a small-block random workload moves few MB but still saturates the device."
                        />
                      </Divider>
                      <DiskFloorMatrix disabled={!autoEnabled} />
                    </AdvancedSection>
                  </div>
                )}
              </Card>
            </>
          )}
          <div style={{ marginTop: 12 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              Last saved: {formatTimestamp(limitTs ?? enabledTs)}
            </Text>
          </div>
          {/* One Reset for the whole block, matching the single Save in the footer. */}
          <div>{resetButton}</div>
        </Card>
      </Form>
    </Spin>
  )
}

function NetworkPanel() {
  const [policyExpanded, setPolicyExpanded] = useState(false)
  const toPercentage = (ratio: number) => Math.round(ratio * 100)
  const toPercentages = (bandwidth: Record<string, { min: number; max: number }> | undefined) =>
    Object.fromEntries(Object.entries(bandwidth ?? {}).map(([priority, bounds]) => [
      priority,
      { min: toPercentage(bounds.min), max: toPercentage(bounds.max) },
    ]))

  return (
    <FormCard
      title="Network I/O Control"
      scope="network_control"
      load={async () => {
        const d = await api.getConfig<{
          enable_network_control: boolean
          enable_network_pressure_shaping: boolean
          enable_app_pps_police: boolean
          network_pps_policy: { app_pps_high: number; pps_cap_rate: number; pps_cap_burst: number }
          available_network_interfaces: Array<{ name: string; speed_mbps: number }>
          selected_network_interfaces: string[]
          config_network_bw: Record<string, { min: number; max: number }>
          network_system_ports?: number[]
          updated_at?: number
        }>('network_control')
        setPolicyExpanded(Boolean(d.enable_network_control))
        return {
          values: {
            enable_network_control: Boolean(d.enable_network_control),
            enable_network_pressure_shaping: Boolean(d.enable_network_pressure_shaping ?? true),
            enable_app_pps_police: Boolean(d.enable_app_pps_police),
            network_pps_policy: d.network_pps_policy,
            available_network_interfaces: d.available_network_interfaces ?? [],
            selected_network_interfaces: d.selected_network_interfaces ?? [],
            config_network_bw: toPercentages(d.config_network_bw),
            network_system_ports: Array.isArray(d.network_system_ports) ? d.network_system_ports : [],
          },
          updatedAt: d.updated_at,
        }
      }}
      save={(values, ts) =>
        api.updateConfig('network_control', {
          enable_network_control: Boolean(values.enable_network_control),
          enable_network_pressure_shaping: Boolean(values.enable_network_pressure_shaping),
          enable_app_pps_police: Boolean(values.enable_app_pps_police),
          network_pps_policy: values.network_pps_policy,
          ...(Boolean(values.enable_network_control)
            ? { selected_network_interfaces: values.selected_network_interfaces }
            : {}),
          config_network_bw: Object.fromEntries(
            ['critical', 'high', 'low'].map((priority) => [
              priority,
              Object.fromEntries(Object.entries(
                (values.config_network_bw as Record<string, Record<string, number>> | undefined)?.[priority] ?? {},
              ).map(([bound, value]) => [bound, value / 100])),
            ]),
          ),
        }, ts)
      }
      currentToValues={(c) => ({
        enable_network_control: Boolean(c.enable_network_control),
        enable_network_pressure_shaping: Boolean(c.enable_network_pressure_shaping ?? true),
        enable_app_pps_police: Boolean(c.enable_app_pps_police),
        network_pps_policy: c.network_pps_policy,
        available_network_interfaces: c.available_network_interfaces,
        selected_network_interfaces: c.selected_network_interfaces,
        config_network_bw: toPercentages(c.config_network_bw as Record<string, { min: number; max: number }> | undefined),
        network_system_ports: c.network_system_ports,
      })}
    >
      <Form.Item noStyle shouldUpdate>
        {({ getFieldValue, setFieldsValue }) => {
          const networkControlEnabled = Boolean(getFieldValue('enable_network_control'))
          const pressureShapingEnabled = Boolean(getFieldValue('enable_network_pressure_shaping'))
          const packetFloodProtectionEnabled = Boolean(getFieldValue('enable_app_pps_police'))
          const availableNetworkInterfaces = (
            getFieldValue('available_network_interfaces') as Array<{ name: string; speed_mbps: number }> | undefined
          ) ?? []
          const systemPorts = (getFieldValue('network_system_ports') as number[] | undefined) ?? []
          const reservedBandwidth = (
            getFieldValue('config_network_bw') as Record<string, { min?: number; max?: number }> | undefined
          )?.system
          const bandwidth = getFieldValue('config_network_bw') as Record<string, { min?: number }> | undefined
          const reservedMinimumRatio = Math.round(Number(reservedBandwidth?.min ?? 0))
          const applicationMinimumRatioTotal = ['critical', 'high', 'low'].reduce(
            (sum, priority) => sum + Number(
              bandwidth?.[priority]?.min ?? 0,
            ),
            0,
          )
          const displayedApplicationMinimumRatioTotal = Math.round(applicationMinimumRatioTotal)
          const applicationMinimumRatioLimit = 100 - reservedMinimumRatio
          const minimumRatioExceeded = displayedApplicationMinimumRatioTotal > applicationMinimumRatioLimit
          return (
            <>
              <Space size={8} align="center" style={{ marginTop: 6 }}>
                <Button
                  size="small"
                  type="text"
                  onClick={() => setPolicyExpanded((expanded) => !expanded)}
                  disabled={!networkControlEnabled}
                  icon={policyExpanded
                    ? <DownOutlined style={{ color: COLORS.textMuted, fontSize: 12 }} />
                    : <RightOutlined style={{ color: COLORS.textMuted, fontSize: 12 }} />}
                  aria-label="Toggle network policy details"
                />
                <Text strong>Network control</Text>
                <Tooltip title="When enabled, network shaping is applied to controlled apps. When disabled, all network shaping is paused.">
                  <QuestionCircleOutlined style={{ color: COLORS.textMuted, fontSize: 12 }} />
                </Tooltip>
                <Form.Item name="enable_network_control" valuePropName="checked" noStyle>
                  <Switch
                    checkedChildren="On"
                    unCheckedChildren="Off"
                    onChange={(checked) => {
                      setPolicyExpanded(checked)
                      if (!checked) {
                        setFieldsValue({ selected_network_interfaces: [] })
                        return
                      }
                      const currentSelected = (
                        getFieldValue('selected_network_interfaces') as string[] | undefined
                      ) ?? []
                      if (currentSelected.length === 0) {
                        setFieldsValue({
                          selected_network_interfaces: availableNetworkInterfaces.map((nic) => nic.name),
                        })
                      }
                    }}
                  />
                </Form.Item>
              </Space>

              {policyExpanded && (
                <div style={{ marginTop: 8, marginLeft: 14, paddingLeft: 12, borderLeft: `2px solid ${COLORS.border}` }}>
                  <Text type="secondary" style={{ display: 'block', marginBottom: 10 }}>
                    The network policy below only applies while network control is enabled.
                  </Text>
                  <Row gutter={16}>
                    <Col span={10}>
                      <Form.Item
                        label="Auto network control"
                        name="enable_network_pressure_shaping"
                        valuePropName="checked"
                        tooltip="When enabled, bandwidth ceilings are adjusted automatically based on network pressure. When disabled, only the static class policy is applied."
                      >
                        <Switch
                          checkedChildren="On"
                          unCheckedChildren="Off"
                          disabled={!networkControlEnabled}
                        />
                      </Form.Item>
                    </Col>
                    <Col span={14}>
                      <Form.Item
                        label="Per-app packet flood protection"
                        name="enable_app_pps_police"
                        valuePropName="checked"
                        tooltip="Caps lower-priority outbound applications only when fused TX pressure indicates a small-packet flood. Requires automatic network control."
                      >
                        <Switch
                          checkedChildren="On"
                          unCheckedChildren="Off"
                          disabled={!networkControlEnabled || !pressureShapingEnabled}
                        />
                      </Form.Item>
                    </Col>
                  </Row>

                  <AdvancedSection>
                    <Divider orientation="left" orientationMargin={0} plain style={{ margin: '0 0 12px' }}>
                      <SectionLabel
                        text="Packet flood policy"
                        tip="Tune the outbound small-packet flood candidate threshold and the per-app packet-rate cap. Changes apply when packet flood protection is enabled."
                      />
                    </Divider>
                    <Row gutter={12}>
                      <Col span={8}>
                        <Form.Item
                          label="Candidate threshold (PPS)"
                          name={['network_pps_policy', 'app_pps_high']}
                          tooltip="A lower-priority app must exceed this outbound packet rate, while causing network harm, before packet flood protection can select it for limiting. Use it as the upper reference when setting the per-app cap."
                          rules={[{ required: true, type: 'integer', min: 1 }]}
                        >
                          <InputNumber style={{ width: '100%' }} min={1} step={1000} disabled={!networkControlEnabled || !pressureShapingEnabled || !packetFloodProtectionEnabled} />
                        </Form.Item>
                      </Col>
                      <Col span={8}>
                        <Form.Item
                          label="Per-app cap (PPS)"
                          name={['network_pps_policy', 'pps_cap_rate']}
                          tooltip="The sustained packet-rate limit applied after an app exceeds the candidate threshold and is selected for protection. Packets above this rate are dropped after its burst allowance is used."
                          dependencies={[['network_pps_policy', 'app_pps_high']]}
                          rules={[
                            { required: true, type: 'integer', min: 1 },
                            ({ getFieldValue }) => ({
                              validator(_, value) {
                                const threshold = getFieldValue(['network_pps_policy', 'app_pps_high'])
                                return value <= threshold
                                  ? Promise.resolve()
                                  : Promise.reject(new Error('Cap cannot exceed candidate threshold'))
                              },
                            }),
                          ]}
                        >
                          <InputNumber style={{ width: '100%' }} min={1} step={1000} disabled={!networkControlEnabled || !pressureShapingEnabled || !packetFloodProtectionEnabled} />
                        </Form.Item>
                      </Col>
                      <Col span={8}>
                        <Form.Item
                          label="Burst (packets)"
                          name={['network_pps_policy', 'pps_cap_burst']}
                          rules={[{ required: true, type: 'integer', min: 1 }]}
                        >
                          <InputNumber style={{ width: '100%' }} min={1} step={100} disabled={!networkControlEnabled || !pressureShapingEnabled || !packetFloodProtectionEnabled} />
                        </Form.Item>
                      </Col>
                    </Row>
                  </AdvancedSection>

                  <Divider orientation="left" orientationMargin={0} plain style={{ margin: '4px 0 12px' }}>
                    <SectionLabel
                      text="Controlled network interfaces"
                      tip="Select the automatically detected physical interfaces to control. Link bandwidth is detected by the operating system."
                    />
                  </Divider>
                  <Form.Item
                    name="selected_network_interfaces"
                    rules={[{
                      validator: (_, value: string[]) => !networkControlEnabled || value?.length > 0
                        ? Promise.resolve()
                        : Promise.reject(new Error('Select at least one network interface')),
                    }]}
                  >
                    <Checkbox.Group disabled={!networkControlEnabled}>
                      <Space direction="vertical" size={6}>
                        {availableNetworkInterfaces.map((nic) => (
                          <Checkbox key={nic.name} value={nic.name}>
                            {nic.name} ({nic.speed_mbps.toLocaleString()} Mbps)
                          </Checkbox>
                        ))}
                      </Space>
                    </Checkbox.Group>
                  </Form.Item>
                  {availableNetworkInterfaces.length === 0 && (
                    <Alert type="warning" showIcon message="No controllable physical network interfaces detected" />
                  )}

                  <Divider orientation="left" orientationMargin={0} plain style={{ margin: '4px 0 12px' }}>
                    <SectionLabel
                      text="Reserved network bandwidth range"
                      tip="Reserved capacity and its controlled ports are managed by the system and cannot be changed here."
                    />
                  </Divider>

                  <Text style={{ display: 'block', marginBottom: 10 }}>
                    Reserved network bandwidth for system ports:{' '}
                    {reservedBandwidth?.min != null && reservedBandwidth?.max != null
                      ? `${Math.round(reservedBandwidth.min)}% - ${Math.round(reservedBandwidth.max)}%`
                      : 'Not configured'}
                  </Text>

                  <div style={{ marginTop: 10 }}>
                    <Text type="secondary" style={{ display: 'block', marginBottom: 4 }}>
                      Controlled system ports
                    </Text>
                    {systemPorts.length > 0 ? (
                      <Space size={[4, 4]} wrap>
                        {systemPorts.map((port) => (
                          <Tag key={port}>{({ 22: '22 - SSH', 53: '53 - DNS', 80: '80 - HTTP', 123: '123 - NTP', 443: '443 - HTTPS' }[port] ?? port)}</Tag>
                        ))}
                      </Space>
                    ) : (
                      <Text>None</Text>
                    )}
                  </div>

                  <Divider orientation="left" orientationMargin={0} plain style={{ margin: '16px 0 12px' }}>
                    <SectionLabel
                      text="Network bandwidth ranges"
                      tip="Set the minimum and maximum ratio of NIC link bandwidth for each application priority. The application minimum total cannot exceed the capacity remaining after the system reservation."
                    />
                  </Divider>

                  <Row gutter={12} align="middle" style={{ marginBottom: 6 }}>
                    <Col span={6}><Text type="secondary" style={{ whiteSpace: 'nowrap' }}>Network priority</Text></Col>
                    <Col span={5}><Text type="secondary">Min (%)</Text></Col>
                    <Col span={5}><Text type="secondary">Max (%)</Text></Col>
                  </Row>

                  {(['critical', 'high', 'low'] as const).map((pri) => (
                    <Row gutter={12} align="middle" key={pri} style={{ marginBottom: 8 }}>
                      <Col span={6}>
                        <Text>{pri[0].toUpperCase() + pri.slice(1)}</Text>
                      </Col>
                      <Col span={5}>
                        <Form.Item
                          name={['config_network_bw', pri, 'min']}
                          rules={[
                            { required: true, type: 'number', min: 0, max: 100 },
                            ({ getFieldValue }) => ({
                              validator() {
                                const bandwidth = getFieldValue('config_network_bw') as Record<string, { min?: number }> | undefined
                                const total = ['system', 'critical', 'high', 'low'].reduce(
                                  (sum, priority) => sum + Number(bandwidth?.[priority]?.min ?? 0),
                                  0,
                                )
                                if (total > 100) {
                                  const reserved = Math.round(Number(bandwidth?.system?.min ?? 0))
                                  return Promise.reject(new Error(`Application minimum ratios cannot exceed ${100 - reserved}% (${reserved}% is reserved for system ports)`))
                                }
                                return Promise.resolve()
                              },
                            }),
                          ]}
                          noStyle
                        >
                          <InputNumber
                            style={{ width: '100%' }}
                            min={0}
                            max={100}
                            step={1}
                            addonAfter="%"
                            disabled={!networkControlEnabled}
                          />
                        </Form.Item>
                      </Col>
                      <Col span={5}>
                        <Form.Item
                          name={['config_network_bw', pri, 'max']}
                          rules={[{ required: true, type: 'number', min: 0, max: 100 }]}
                          noStyle
                        >
                          <InputNumber
                            style={{ width: '100%' }}
                            min={0}
                            max={100}
                            step={1}
                            addonAfter="%"
                            disabled={!networkControlEnabled}
                          />
                        </Form.Item>
                      </Col>
                    </Row>
                  ))}

                  <Text type={minimumRatioExceeded ? 'danger' : 'secondary'} style={{ display: 'block', marginTop: 4 }}>
                    Application minimum allocation: {displayedApplicationMinimumRatioTotal}% / {applicationMinimumRatioLimit}% ({reservedMinimumRatio}% reserved for system ports)
                  </Text>
                  {!minimumRatioExceeded && displayedApplicationMinimumRatioTotal < applicationMinimumRatioLimit && (
                    <Text type="secondary" style={{ display: 'block', marginTop: 2 }}>
                      Remaining application minimum capacity: {applicationMinimumRatioLimit - displayedApplicationMinimumRatioTotal}%
                    </Text>
                  )}
                  {minimumRatioExceeded && (
                    <Alert
                      type="error"
                      showIcon
                      message={`Application minimum allocation exceeds ${applicationMinimumRatioLimit}%`}
                      description={`${reservedMinimumRatio}% is reserved for system ports. Reduce the Critical, High, or Low minimum ratio before saving.`}
                      style={{ marginTop: 8 }}
                    />
                  )}

                </div>
              )}
            </>
          )
        }}
      </Form.Item>
    </FormCard>
  )
}

// ---------------------------------------------------------------------------
// One config section per tab. Each tab used to hold four cards, so opening the
// dialog fired four requests at once and the page had to be scrolled to find
// anything; a tab per section loads exactly what is being looked at.
//
// `group` only draws a heading in the left rail (a disabled, unselectable item).
// ---------------------------------------------------------------------------
interface SettingsTabDef {
  key: string
  title: string
  icon: React.ReactNode
  panel: React.ReactNode
  // Balancer-only sections: in monitor-only mode (`-m`) the balancer is not
  // running, so they are omitted entirely, matching the hidden Balancer tab in App.tsx.
  balancerOnly?: boolean
  group?: string
}

const SETTINGS_TABS: SettingsTabDef[] = [
  {
    key: 'overview',
    title: 'System Overview',
    icon: <DashboardOutlined />,
    panel: <MonitoredSectionsCard />,
    group: 'Monitor',
  },
  { key: 'system_pressure', title: 'System Pressure', icon: <MonitorOutlined />, panel: <SystemPressurePanel /> },
  { key: 'disk_pressure', title: 'Disk I/O Pressure', icon: <HddOutlined />, panel: <DiskPressurePanel /> },
  { key: 'network_pressure', title: 'Network I/O Pressure', icon: <GlobalOutlined />, panel: <NetworkPressurePanel /> },
  {
    key: 'auto_control',
    title: 'Auto Control',
    icon: <ControlOutlined />,
    panel: <AutoControlPanel />,
    balancerOnly: true,
    group: 'Control',
  },
  {
    key: 'network_control',
    title: 'Network Control',
    icon: <ApiOutlined />,
    panel: <NetworkPanel />,
    balancerOnly: true,
  },
]

// Heading of a group in the left rail. Larger and brighter than the tabs under it (which
// are muted until selected) plus a gap above the second group: as small uppercase muted
// text it read as *less* important than its own children.
function TabGroupLabel({ text, first }: { text: string; first?: boolean }) {
  return (
    <span
      style={{
        display: 'block',
        fontSize: 13,
        fontWeight: 600,
        color: COLORS.text,
        marginTop: first ? 0 : 12,
      }}
    >
      {text}
    </span>
  )
}

// A tab whose card holds edits that have not been written yet.
function UnsavedDot() {
  return (
    <Tooltip title="Unsaved changes on this page">
      <span
        style={{
          display: 'inline-block',
          width: 6,
          height: 6,
          borderRadius: 3,
          background: COLORS.accent,
        }}
      />
    </Tooltip>
  )
}

export default function SettingsModal({ visible, onClose, balancerEnabled }: Props) {
  const tabs = SETTINGS_TABS.filter((tab) => balancerEnabled || !tab.balancerOnly)
  const firstTabKey = tabs[0].key
  // Cards register here while mounted. Panes stay mounted once visited, so this
  // holds every section the user has opened -- which is what lets Save all write
  // edits left behind on another tab.
  const handles = useRef(new Map<string, { tabKey: string; handle: SaveHandle }>())
  const [saving, setSaving] = useState(false)
  const [activeTab, setActiveTab] = useState(firstTabKey)
  const [dirtyTabs, setDirtyTabs] = useState<string[]>([])

  const refreshDirty = useCallback(() => {
    const next = [...new Set(
      [...handles.current.values()]
        .filter(({ handle }) => handle.isDirty?.() ?? true)
        .map(({ tabKey }) => tabKey),
    )].sort()
    // Same set -> same array, so a keystroke in an already-dirty card does not
    // re-render the whole dialog.
    setDirtyTabs((prev) => (
      prev.length === next.length && prev.every((key, i) => key === next[i]) ? prev : next
    ))
  }, [])

  const registry = React.useMemo<SaveRegistry>(() => ({
    register: (id, tabKey, handle) => {
      handles.current.set(id, { tabKey, handle })
      return () => {
        handles.current.delete(id)
      }
    },
    notifyDirty: refreshDirty,
  }), [refreshDirty])

  // Reopening starts from the first tab with no stale markers: the panes were
  // destroyed on close (destroyOnClose), so their handles are gone too.
  useEffect(() => {
    if (!visible) return
    setActiveTab(firstTabKey)
    setDirtyTabs([])
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible])

  const dirtyHandles = () => [...handles.current.values()].filter(({ handle }) => handle.isDirty?.() ?? true)

  const runSave = async (pending: Array<{ tabKey: string; handle: SaveHandle }>, closeOnSuccess: boolean) => {
    setSaving(true)
    try {
      // Validate everything first: a half-written save (one card written, the
      // next rejected for a bad number) is worse than saving nothing.
      for (const { handle } of pending) {
        if (!(await handle.validate?.() ?? true)) return
      }
      const outcomes: SaveOutcome[] = []
      for (const { handle } of pending) {
        outcomes.push(await handle.save())
      }
      // Each card reports its own failure; a conflict opens its own reload
      // dialog. Either way the dialog stays open so the user can act on it.
      if (outcomes.every((outcome) => outcome === 'ok')) {
        message.success('Settings saved')
        if (closeOnSuccess) onClose()
      }
    } finally {
      setSaving(false)
      refreshDirty()
    }
  }

  // Save writes the page in view and stays open, so a multi-tab edit can be
  // committed one page at a time; Save all writes every touched page at once
  // and closes, which is the "edit a few tabs, then leave" path.
  const savePage = () => {
    const pending = dirtyHandles().filter(({ tabKey }) => tabKey === activeTab)
    if (pending.length === 0) {
      message.info('No unsaved changes on this page')
      return
    }
    void runSave(pending, false)
  }

  const saveAll = () => {
    const pending = dirtyHandles()
    if (pending.length === 0) {
      onClose()
      return
    }
    void runSave(pending, true)
  }

  const titleFor = (key: string) => tabs.find((tab) => tab.key === key)?.title ?? key

  const requestClose = () => {
    if (dirtyTabs.length === 0) {
      onClose()
      return
    }
    Modal.confirm({
      title: 'Discard unsaved changes?',
      content: (
        <p>
          Unsaved changes on: <b>{dirtyTabs.map(titleFor).join(', ')}</b>. Closing now throws
          them away — use <b>Save all</b> to write every page first.
        </p>
      ),
      okText: 'Discard and close',
      okButtonProps: { danger: true },
      cancelText: 'Keep editing',
      onOk: onClose,
    })
  }

  // Only the second group onwards gets a gap above it; the first sits right under the header.
  const firstGroupKey = tabs.find((tab) => tab.group)?.key

  const items = tabs.flatMap((tab) => [
    // Heading rows are items so they sit in the rail's flow; disabled makes them
    // unselectable rather than a tab that looks broken when clicked.
    ...(tab.group
      ? [{
        key: `group:${tab.group}`,
        label: <TabGroupLabel text={tab.group} first={tab.key === firstGroupKey} />,
        disabled: true,
        children: null,
      }]
      : []),
    {
      key: tab.key,
      // Indented under its group heading, so the rail reads as heading > pages.
      label: (
        <Space size={6} style={{ paddingLeft: 10 }}>
          {tab.icon}
          <span>{tab.title}</span>
          {dirtyTabs.includes(tab.key) && <UnsavedDot />}
        </Space>
      ),
      children: <SettingsTabContext.Provider value={tab.key}>{tab.panel}</SettingsTabContext.Provider>,
    },
  ])

  return (
    <Modal
      title={
        <Space>
          <SettingOutlined style={{ color: COLORS.accent }} />
          <span>Settings</span>
        </Space>
      }
      open={visible}
      onCancel={requestClose}
      destroyOnClose
      // Two save buttons rather than one, because a per-section dialog makes the
      // two intents different: commit every page that was touched and leave (the
      // wider action, so it leads), or commit the page in view and carry on.
      // Primary is given to whichever is currently actionable rather than fixed to
      // one of them, so the blue button always marks a save that would do something.
      // The tooltips sit on a span because a disabled antd button swallows mouse
      // events, and Space keeps the gaps that the footer's default button-sibling
      // margin would otherwise lose.
      footer={
        <Space>
          <Tooltip
            title={dirtyTabs.length === 0
              ? 'Nothing to save'
              : `Save ${dirtyTabs.map(titleFor).join(', ')} and close`}
          >
            <span>
              <Button
                type={dirtyTabs.length > 0 ? 'primary' : 'default'}
                icon={<SaveOutlined />}
                loading={saving}
                disabled={dirtyTabs.length === 0}
                onClick={saveAll}
              >
                {dirtyTabs.length > 1 ? `Save all (${dirtyTabs.length} pages)` : 'Save all'}
              </Button>
            </span>
          </Tooltip>
          <Tooltip title={dirtyTabs.includes(activeTab) ? 'Save this page and keep editing' : 'No unsaved changes on this page'}>
            <span>
              <Button
                type={dirtyTabs.includes(activeTab) ? 'primary' : 'default'}
                icon={<SaveOutlined />}
                loading={saving}
                disabled={!dirtyTabs.includes(activeTab)}
                onClick={savePage}
              >
                Save page
              </Button>
            </span>
          </Tooltip>
          <Button onClick={requestClose} disabled={saving}>
            Close
          </Button>
        </Space>
      }
      width={1140}
      // The default header margin leaves the title almost touching the first tab label.
      styles={{ header: { marginBottom: 20 }, body: { maxHeight: '72vh', overflowY: 'auto' } }}
    >
      <SettingsSaveContext.Provider value={registry}>
        {/* No destroyOnHidden: a visited pane stays mounted, so switching tabs keeps
            both the edits and the save handle of the page left behind. Panes are still
            created lazily, so only the tab in view has loaded its config. */}
        <Tabs
          tabPosition="left"
          items={items}
          activeKey={activeTab}
          onChange={(key) => {
            setActiveTab(key)
            refreshDirty()
          }}
          style={{ minHeight: 440 }}
        />
      </SettingsSaveContext.Provider>
    </Modal>
  )
}
