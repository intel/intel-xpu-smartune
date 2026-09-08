// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// One benchmark case, in full: its KPIs, what the hardware was doing while it
// ran, and the tail of the log it wrote.
//
// The charts elsewhere in this tab are deliberately narrow -- one metric at a
// time, so configurations can be compared. This is where the rest of what was
// measured lives, one click from any row or any bar.
//
// The hardware readings used to be four tables of medians. A median is one
// number for a whole case, and it cannot tell a run that ramped from 15 W to
// 40 W from one that sat at 28 W throughout -- which is the difference between
// a thermal problem and a steady workload. The samples those medians were taken
// from are on disk (benchmark/service/sampler.py writes them at 2 Hz), so this
// plots them and marks the median on the curve rather than reporting it alone.

import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Descriptions,
  Drawer,
  Empty,
  Select,
  Skeleton,
  Space,
  Statistic,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip as ChartTooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { api } from '../api/client'
import type { BenchMatrixRow, BenchMetricMeta, BenchTimelineData } from '../api/types'
import { COLORS } from '../styles/theme'
import {
  deviceLabel,
  formatMetric,
  formatWithUnit,
  indexMetrics,
  metricLabel,
  metricTitle,
  sentenceCase,
  type MetricIndex,
} from '../utils/benchMetrics'

const { Text } = Typography

// The handful of numbers worth reading before anything else. Shown as tiles in
// the order a person asks about them: how much was asked of it, how fast it was
// overall, then how long the first token took, then the per-token cost, then
// what it drew to do it.
//
// Input length is here rather than in the tables below because throughput and
// both latencies are read against it -- a 40 tok/s figure means one thing for a
// 32-token prompt and another for a 1024-token one. Left where it was, it was a
// single-row "KPI" table under the curve, which is a lot of furniture for one
// number and puts it nowhere near the numbers it qualifies.
const HEADLINE_METRICS = [
  'kpi_input_token_size',
  'kpi_throughput_tokens_s',
  'kpi_first_latency_ms',
  'kpi_second_latency_ms',
  'd_perf_per_watt',
]

// Groups the curve below covers, and so not repeated as tables of medians. The
// derived comparisons go too: a speedup is a statement about the *other* cases
// in the sweep, which is what the Analysis tab is for -- in a drawer about one
// case it is a column of ratios with their denominators somewhere else.
const CHARTED_GROUPS = new Set(['cpu', 'memory', 'gpu', 'npu'])
const HIDDEN_GROUPS = new Set([...CHARTED_GROUPS, 'derived'])

// Temperature is excluded from the curve by request. It is also the one reading
// here that says more about the room and the chassis than about the workload.
const NOT_CHARTED = /(_temp_c_median|_temperature_c_median|_tjmax_c_median)$/

// Which curve to open on: whatever the device that ran the case was doing.
// Falling through to CPU usage, which every platform records.
const PREFERRED_SERIES: Record<string, string[]> = {
  gpu: ['gpu_compute_busy_percent_median', 'gpu_power_w_median'],
  npu: ['npu_utilization_percent_median', 'npu_power_w_median'],
  cpu: ['cpu_usage_percent_median'],
}
const FALLBACK_SERIES = ['cpu_usage_percent_median', 'memory_used_gb_median']

// One series, so one colour. First step of the same validated categorical order
// the comparison charts use (see BenchCompare), at 3.9:1 against this surface.
const CURVE_COLOR = '#3987e5'
const CHART_HEIGHT = 220
const AXIS_TICK = { fill: COLORS.textMuted, fontSize: 11 }
const GRID_STROKE = 'rgba(142, 154, 179, 0.28)'
const AXIS_STROKE = 'rgba(142, 154, 179, 0.45)'

/**
 * A metric's name, with what it measures on hover.
 *
 * The dotted underline is load-bearing: a tooltip nobody knows is there is not a
 * tooltip. It matters most for the derived ratios, where the label ("Vs best
 * precision") does not say what the denominator is and 1.00x can mean either
 * "this one won" or "there was nothing to compare it with".
 */
function MetricName({ label, description }: { label: string; description?: string | null }) {
  if (!description) return <>{label}</>
  return (
    <Tooltip title={description}>
      <span style={{ borderBottom: `1px dotted ${COLORS.textMuted}`, cursor: 'help' }}>
        {label}
      </span>
    </Tooltip>
  )
}

/** The median of the samples actually plotted, when the pipeline reported none. */
function medianOf(values: (number | null)[]): number | undefined {
  const finite = values.filter((v): v is number => v !== null && Number.isFinite(v)).sort((a, b) => a - b)
  if (finite.length === 0) return undefined
  const middle = Math.floor(finite.length / 2)
  return finite.length % 2 ? finite[middle] : (finite[middle - 1] + finite[middle]) / 2
}

/**
 * What one measure did over the case's measurement window.
 *
 * One series at a time, chosen from what was actually sampled: several measures
 * on one plot would need either a shared scale that flattens the smaller one or
 * a second y-axis, and a second y-axis puts the crossing point wherever the
 * scaling happened to fall. The dashed line is the median -- the single number
 * every table and bar in this tab reports for this metric -- so the curve can be
 * read against what is claimed elsewhere.
 */
function CaseTimeline({
  row,
  data,
  loading,
  index,
}: {
  row: BenchMatrixRow
  data: BenchTimelineData | null
  loading: boolean
  index: MetricIndex
}) {
  const [chosen, setChosen] = useState<string | null>(null)

  // Offered in the order the backend put the descriptors in -- CPU, memory,
  // GPU, NPU -- rather than in whatever order the CSV columns happened to be.
  const options = useMemo(() => {
    const present = new Set(Object.keys(data?.series ?? {}))
    const ordered = [...index.values()]
      .filter((meta) => present.has(meta.key) && !NOT_CHARTED.test(meta.key))
      .map((meta) => meta.key)
    // A series the server did not describe still gets offered, just plainly.
    const rest = [...present].filter((key) => !index.has(key) && !NOT_CHARTED.test(key))
    return [...ordered, ...rest]
  }, [data?.series, index])

  const preferred = useMemo(() => {
    const wanted = [...(PREFERRED_SERIES[row.device] ?? []), ...FALLBACK_SERIES]
    return wanted.find((key) => options.includes(key)) ?? options[0] ?? null
  }, [options, row.device])

  // The selection survives switching cases only while it still exists: a GPU
  // case's compute-busy curve has no counterpart on a CPU-only run.
  const active = chosen && options.includes(chosen) ? chosen : preferred

  const meta = active ? index.get(active) : undefined
  const values = active ? data?.series[active] ?? [] : []
  const points = (data?.t ?? []).map((t, position) => ({ t, value: values[position] ?? null }))
  // The pipeline's own median where it has one, so the line and the tables
  // cannot disagree; computed from the plotted samples otherwise.
  const median = active
    ? (Number.isFinite(row.metrics[active]) ? row.metrics[active] : medianOf(values))
    : undefined

  if (loading) return <Skeleton active paragraph={{ rows: 4 }} />

  if (!data || options.length === 0) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={
          row.status !== 'ok'
            ? 'This case produced no measurement, so nothing was sampled for it.'
            : 'No samples for this case — its run either predates in-process sampling or its sampling CSV has been cleaned up. The medians above are all that is left of it.'
        }
      />
    )
  }

  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Space size={8} wrap>
        <Select
          size="small"
          style={{ minWidth: 260 }}
          value={active ?? undefined}
          onChange={setChosen}
          options={options.map((key) => ({
            value: key,
            label: metricTitle(key, index),
            title: index.get(key)?.description ?? metricTitle(key, index),
          }))}
        />
        <Text type="secondary" style={{ fontSize: 12 }}>
          {data.count} sample{data.count === 1 ? '' : 's'} over {formatMetric(data.duration_s)} s
          {median !== undefined && ` · median ${formatWithUnit(median, meta)}`}
        </Text>
      </Space>
      <div style={{ width: '100%', height: CHART_HEIGHT }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={points} margin={{ top: 8, right: 16, left: 0, bottom: 20 }}>
            <CartesianGrid stroke={GRID_STROKE} strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="t"
              type="number"
              domain={[0, 'dataMax']}
              tick={AXIS_TICK}
              axisLine={{ stroke: AXIS_STROKE }}
              tickLine={{ stroke: AXIS_STROKE }}
              tickFormatter={(value: number) => `${Math.round(value)}`}
              label={{
                value: 'seconds into the measurement window',
                position: 'insideBottom',
                offset: -12,
                fill: COLORS.textMuted,
                fontSize: 11,
              }}
            />
            <YAxis
              tick={AXIS_TICK}
              axisLine={{ stroke: AXIS_STROKE }}
              tickLine={{ stroke: AXIS_STROKE }}
              width={64}
              label={{
                value: meta?.unit ?? '',
                angle: -90,
                position: 'insideLeft',
                offset: 14,
                fill: COLORS.textMuted,
                fontSize: 11,
              }}
            />
            <ChartTooltip
              cursor={{ stroke: COLORS.border, strokeDasharray: '3 3' }}
              content={({ active: hovering, payload }) => {
                const point = hovering && payload?.length
                  ? (payload[0].payload as { t: number; value: number | null })
                  : undefined
                if (!point || point.value === null) return null
                return (
                  <div
                    style={{
                      background: COLORS.panelBg,
                      border: `1px solid ${COLORS.border}`,
                      color: COLORS.text,
                      padding: '6px 8px',
                      fontSize: 12,
                    }}
                  >
                    <div>{formatMetric(point.t)} s</div>
                    <div>{formatWithUnit(point.value, meta)}</div>
                  </div>
                )
              }}
            />
            {median !== undefined && (
              <ReferenceLine
                y={median}
                stroke={COLORS.textMuted}
                strokeDasharray="4 4"
                // Inside the plot, not beside it: `right` anchors the text at
                // the line's end and runs it outward, which put "median" past
                // the right edge of the SVG where it was simply cut off.
                label={{
                  value: 'median',
                  position: 'insideTopRight',
                  fill: COLORS.textMuted,
                  fontSize: 10,
                }}
              />
            )}
            <Line
              type="monotone"
              dataKey="value"
              stroke={CURVE_COLOR}
              strokeWidth={2}
              // A sample every 500ms over a minute is more points than dots can
              // be drawn for; the hover dot is how a single reading is picked
              // out. A gap is a collector that missed a tick, so it stays a gap.
              dot={false}
              connectNulls={false}
              activeDot={{ r: 4, stroke: COLORS.panelBg, strokeWidth: 2 }}
              isAnimationActive={false}
              name={meta?.label ?? active ?? ''}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </Space>
  )
}

interface Props {
  open: boolean
  row: BenchMatrixRow | null
  metrics: BenchMetricMeta[]
  onClose: () => void
}

export default function BenchCaseDrawer({ open, row, metrics, onClose }: Props) {
  const [log, setLog] = useState<string | null>(null)
  const [logError, setLogError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [timeline, setTimeline] = useState<BenchTimelineData | null>(null)
  const [timelineLoading, setTimelineLoading] = useState(false)

  const index = React.useMemo(() => indexMetrics(metrics), [metrics])
  const logPath = row?.log_file ?? ''
  const caseDir = row?.case_dir ?? ''

  const loadTimeline = useCallback(async () => {
    if (!caseDir) {
      setTimeline(null)
      return
    }
    setTimelineLoading(true)
    try {
      setTimeline(await api.getBenchCaseTimeline(caseDir))
    } catch {
      // A case with no samples answers 404, which is a normal state of an older
      // tree rather than a fault -- CaseTimeline says so where the chart would
      // have been, so there is nothing to report here.
      setTimeline(null)
    } finally {
      setTimelineLoading(false)
    }
  }, [caseDir])

  const loadLog = useCallback(async () => {
    if (!logPath) {
      setLog(null)
      setLogError(null)
      return
    }
    setLoading(true)
    setLogError(null)
    try {
      const data = await api.getBenchCaseLog(logPath)
      setLog(data?.content ?? '')
    } catch (error) {
      setLog(null)
      setLogError(error instanceof Error ? error.message : 'Could not read the log')
    } finally {
      setLoading(false)
    }
  }, [logPath])

  useEffect(() => {
    // Fetched on open rather than on selection: the log is up to 256 kB, the
    // samples are thousands of numbers, and the drawer is closed most of the
    // time.
    if (open) {
      void loadLog()
      void loadTimeline()
    }
  }, [open, loadLog, loadTimeline])

  const failed = !!row && row.status !== 'ok'

  // Everything measured that is neither a headline tile nor on the curve below.
  // In practice that is the KPIs the tiles did not take, plus anything the
  // server did not describe -- the hardware readings are all in the chart.
  //
  // Driven by the descriptor list rather than by the row, because the backend
  // has already put the descriptors in reading order -- the row's own key order
  // is the order the CSV columns happened to be written in.
  const grouped = new Map<string, { meta: BenchMetricMeta; value: number }[]>()
  for (const meta of metrics) {
    if (HEADLINE_METRICS.includes(meta.key) || HIDDEN_GROUPS.has(meta.group)) continue
    const value = row?.metrics[meta.key]
    if (value === undefined) continue
    const bucket = grouped.get(meta.group_label) ?? []
    bucket.push({ meta, value })
    grouped.set(meta.group_label, bucket)
  }
  // Anything the server did not describe still gets shown, just plainly.
  for (const [key, value] of Object.entries(row?.metrics ?? {})) {
    if (HEADLINE_METRICS.includes(key) || index.has(key)) continue
    const bucket = grouped.get('Other') ?? []
    bucket.push({
      meta: {
        key,
        label: metricLabel(key),
        unit: null,
        higher_is_better: null,
        group: 'other',
        group_label: 'Other',
      },
      value,
    })
    grouped.set('Other', bucket)
  }

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={720}
      title={
        row ? (
          <Space size={8} wrap>
            <Text strong>{row.model}</Text>
            <Tag>{row.quant || row.precision}</Tag>
            <Tag color="blue">{deviceLabel(row.device)}</Tag>
            <Tag color={failed ? 'error' : 'success'}>{row.status || 'unknown'}</Tag>
          </Space>
        ) : (
          'Case detail'
        )
      }
    >
      {!row ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No case selected." />
      ) : (
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          {failed && (
            <Alert
              type="error"
              showIcon
              message="This case did not produce a measurement"
              description={
                row.failure_reason ?? 'No reason was recorded; the log below is what there is.'
              }
            />
          )}

          {Object.keys(row.metrics).length > 0 && (
            <Space size={24} wrap>
              {HEADLINE_METRICS.filter((key) => row.metrics[key] !== undefined).map((key) => (
                <Statistic
                  key={key}
                  title={
                    <MetricName
                      label={sentenceCase(metricLabel(key, index))}
                      description={index.get(key)?.description}
                    />
                  }
                  value={formatMetric(row.metrics[key])}
                  suffix={
                    <span style={{ fontSize: 13, color: COLORS.textMuted }}>
                      {index.get(key)?.unit ?? ''}
                    </span>
                  }
                  valueStyle={{ fontSize: 22 }}
                />
              ))}
            </Space>
          )}

          <Descriptions size="small" column={2} bordered items={[
            { key: 'backend', label: 'Backend', children: row.backend },
            { key: 'run', label: 'Run', children: row.run },
            { key: 'task', label: 'Task', children: row.task || '-' },
            { key: 'precision', label: 'Precision', children: row.precision || '-' },
            { key: 'batch', label: 'Batch size', children: row.batch_size || '-' },
            { key: 'mode', label: 'Mode', children: row.mode || '-' },
            {
              key: 'model_dir',
              label: 'Model directory',
              span: 2,
              children: <Text copyable style={{ fontSize: 12 }}>{row.model_dir || '-'}</Text>,
            },
            {
              key: 'log_file',
              label: 'Log file',
              span: 2,
              children: <Text copyable style={{ fontSize: 12 }}>{row.log_file || '-'}</Text>,
            },
          ]} />

          <div>
            <Text strong style={{ fontSize: 13 }}>
              While it ran
            </Text>
            <div style={{ marginTop: 8 }}>
              <CaseTimeline
                row={row}
                data={timeline}
                loading={timelineLoading}
                index={index}
              />
            </div>
          </div>

          {[...grouped.entries()].map(([group, entries]) => (
            <div key={group}>
              <Text strong style={{ fontSize: 13 }}>{group}</Text>
              <Descriptions
                size="small"
                column={2}
                bordered
                style={{ marginTop: 8 }}
                items={entries.map(({ meta, value }) => ({
                  key: meta.key,
                  label: <MetricName label={meta.label} description={meta.description} />,
                  children: formatWithUnit(value, meta),
                }))}
              />
            </div>
          ))}

          <div>
            <Text strong style={{ fontSize: 13 }}>
              Log{logPath ? ` · ${logPath.split('/').slice(-2).join('/')}` : ''}
            </Text>
            {loading ? (
              <Skeleton active paragraph={{ rows: 4 }} style={{ marginTop: 8 }} />
            ) : logError ? (
              <Alert type="warning" showIcon style={{ marginTop: 8 }} message={logError} />
            ) : (
              <pre
                style={{
                  marginTop: 8,
                  maxHeight: 320,
                  overflow: 'auto',
                  background: COLORS.bg,
                  border: `1px solid ${COLORS.border}`,
                  borderRadius: 4,
                  padding: 12,
                  fontSize: 12,
                  lineHeight: 1.6,
                  color: COLORS.text,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {log || 'This case wrote no log.'}
              </pre>
            )}
          </div>
        </Space>
      )}
    </Drawer>
  )
}
