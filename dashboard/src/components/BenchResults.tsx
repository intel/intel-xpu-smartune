// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// Benchmark results, by job: what one press of Run produced, folded up.
//
// Every run keeps its own results directory (benchmark/service/runner.py names
// it after the run, and every device it sweeps shares that name), so the tree
// accumulates -- and a flat table of everything ever measured answers "what did
// I just run?" only by making the reader work out which rows are new. Newest job
// open, the rest collapsed.
//
// There was briefly a second view here that grouped by test instead, folding the
// repetitions of one measurement together and marking the best. It is gone: the
// Analysis tab folds and ranks, this one lists, and two arrangements of the same
// rows in one place is one more decision than the tab is worth. Repetitions are
// still all present -- as their own jobs, which is what they are.
//
// The columns are a chosen few. Everything measured -- 28 columns on this
// pipeline -- is one click away in the case drawer, and a table wide enough to
// hold all of it is a table nobody reads across.
//
// Within a job, one table per tool. One press of Run can drive both GenAI and
// benchmark_app, and their KPIs share nothing: TTFT and tok/s against FPS and
// per-request latency. In one table every row was blank under half the KPI
// columns, and only one tool's KPIs could be among the headline ones -- the
// other's were hidden behind "All metrics". Split by tool, each table shows its
// own KPIs by default, under the same columns in every job.
//
// The two selectors above the tables are a search, not a view setting: each one
// narrows the tree by one axis, an empty one says nothing about that axis, and
// together they mean both at once. That is why they do not share the Analysis
// tab's "empty means empty" rule -- a chart has to be told what to plot, while a
// search box that has not been typed into is asking for everything.
//
// Deletion lives here, at both levels the tree has. A job's heading removes
// everything one press of Run produced; the checkboxes in its table remove
// chosen cases. Both are irreversible removals from disk, so both confirm
// first, and both name what is about to go rather than counting it -- a job is
// identified by a timestamp and a uuid fragment, and "delete 6 cases?" is not
// something anyone can check before agreeing to it. Until now the only way to
// discard a measurement was to re-run the same configuration and answer the
// re-run dialog (BenchRerunModal), which meant a misconfigured sweep stayed in
// the tree, competing in the Analysis tab to be the best run of its test.

import React, { useCallback, useMemo, useState } from 'react'
import {
  Button,
  Checkbox,
  Collapse,
  Empty,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { DeleteOutlined, ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'

import type { BenchMatrixData, BenchMatrixRow, BenchMetricMeta } from '../api/types'
import {
  deviceLabel,
  formatMetric,
  indexMetrics,
  foldRounds,
  hasRounds,
  jobLabels,
  paramsLabel,
  type RoundGroup,
  metricTitle,
  type JobLabel,
  type MetricIndex,
} from '../utils/benchMetrics'

const { Text } = Typography

// What a benchmark run is usually being read for: the tool's own KPIs, then how
// hard the part of the machine that ran it was working.
//
// The KPIs are per tool, keyed by the results directory the tool writes under
// (benchmark/service/results.py BACKENDS). GenAI: how much was asked of it, how
// long the first and each subsequent token took, and what came out per second.
// benchmark_app: inferences per second and how long one request took. A tool
// not listed here shows every KPI it recorded, rather than none.
const KEY_KPIS: Record<string, string[]> = {
  genai: [
    'kpi_input_token_size',
    'kpi_first_latency_ms',
    'kpi_second_latency_ms',
    'kpi_throughput_tokens_s',
  ],
  app: ['kpi_throughput_fps', 'kpi_latency_median_ms', 'kpi_latency_average_ms'],
}

// In BACKENDS order, so GenAI comes first in a job that ran both.
const TOOLS: { backend: string; label: string }[] = [
  { backend: 'genai', label: 'GenAI (llm_bench)' },
  { backend: 'ovms', label: 'OVMS' },
  { backend: 'app', label: 'benchmark_app' },
]

function toolLabel(backend: string): string {
  return TOOLS.find((tool) => tool.backend === backend)?.label ?? backend
}

// The machine, whichever tool was driving it.
//
// The accelerator columns are occupancy, not clocks: a GPU clock of 2500 MHz is
// the same number whether the device is saturated or idle at its boost point,
// so it answers nothing about the run, while "GPU compute busy 99%" says the
// model had the device. CPU package power left with them -- it is the
// denominator of performance-per-watt, which is worth checking but not worth a
// column in the table everyone reads first; it is one click away in the case
// drawer, and under "All metrics" here.
const KEY_SYSTEM_METRICS = [
  'memory_used_gb_median',
  'memory_bandwidth_gb_s_median',
  'cpu_usage_percent_median',
  'gpu_compute_busy_percent_median',
  'npu_utilization_percent_median',
]

/** Render a metric title with its definition on hover. */
function MetricHead({ metricKey, index }: { metricKey: string; index: MetricIndex }) {
  const head = (
    <div style={{ textAlign: 'center', lineHeight: 1.25 }}>{metricTitle(metricKey, index)}</div>
  )
  const description = index.get(metricKey)?.description
  return description ? <Tooltip title={description}>{head}</Tooltip> : head
}

/** Create right-aligned metric columns. */
function metricColumns(
  keys: string[],
  index: MetricIndex,
): ColumnsType<BenchMatrixRow> {
  return keys.map((key) => ({
    title: <MetricHead metricKey={key} index={index} />,
    key,
    width: 132,
    align: 'right' as const,
    render: (_: unknown, row: BenchMatrixRow) => formatMetric(row.metrics?.[key]),
  }))
}

/**
 * The metric columns of one tool's table.
 *
 * Only columns something in that table actually measured -- worked out from its
 * own rows, not the whole job's, or benchmark_app's table would inherit GenAI's
 * empty TTFT column. A run with no GPU sampling likewise gets no GPU column just
 * because the schema has one.
 */
function metricKeysOf(
  backend: string,
  rows: BenchMatrixRow[],
  metrics: BenchMetricMeta[],
  showAll: boolean,
): string[] {
  const present = new Set(rows.flatMap((row) => Object.keys(row.metrics)))
  const kpis =
    KEY_KPIS[backend] ?? metrics.filter((m) => m.group === 'kpi').map((m) => m.key)
  const wanted = showAll ? metrics.map((m) => m.key) : [...kpis, ...KEY_SYSTEM_METRICS]
  return wanted.filter((key) => present.has(key))
}

function StatusTag({ row }: { row: BenchMatrixRow }) {
  const tag = (
    <Tag
      color={row.status === 'ok' ? 'success' : row.status === 'invalid' ? 'warning' : 'error'}
      style={{ marginInlineEnd: 0 }}
    >
      {row.status || 'unknown'}
    </Tag>
  )
  // A failed case says why on hover. "failed" alone leaves the reader to open a
  // log to find out whether the device was busy or the model was never
  // supported there. "invalid" is a sweep combination that was never run because
  // its configuration contradicted itself; the reason says which part.
  return row.failure_reason ? <Tooltip title={row.failure_reason}>{tag}</Tooltip> : tag
}

interface JobGroup {
  job: string
  /** "Job3", numbered across every job on disk -- see jobLabels. */
  name: string
  /** When it started, or -- for a tree not named by runner.py -- last wrote. */
  at: number
  /** Whether `at` is the start or that fallback, which changes how it reads. */
  exact: boolean
  rows: BenchMatrixRow[]
  /** The same rows, one entry per tool that produced any -- one table each. */
  tools: { backend: string; rows: BenchMatrixRow[] }[]
  devices: string[]
  /** The OpenVINO versions its cases recorded; empty for a run that recorded none. */
  ovs: string[]
  ok: number
  failed: number
  /** Multi-round runs: how many rounds, with a warm-up or not, and which ran
   *  with full hardware sampling. null for a single-round job. */
  rounds: { count: number; warmup: boolean; sampled: number[] } | null
}

function jobGroupsOf(rows: BenchMatrixRow[], labels: Map<string, JobLabel>): JobGroup[] {
  const byJob = new Map<string, BenchMatrixRow[]>()
  for (const row of rows) {
    const job = row.job || row.run
    byJob.set(job, [...(byJob.get(job) ?? []), row])
  }
  const groups = [...byJob.entries()].map(([job, jobRows]) => {
    const rows = [...jobRows].sort(
      (a, b) =>
        a.model.localeCompare(b.model) ||
        a.precision.localeCompare(b.precision) ||
        a.device.localeCompare(b.device),
    )
    // Known tools in a fixed order, so GenAI's table is first in every job that
    // has one; anything else after them, by name.
    const rank = (backend: string) => {
      const at = TOOLS.findIndex((tool) => tool.backend === backend)
      return at < 0 ? TOOLS.length : at
    }
    const backends = [...new Set(rows.map((row) => row.backend))].sort(
      (a, b) => rank(a) - rank(b) || a.localeCompare(b),
    )
    return {
      job,
      // Numbered over every job on disk, not over the filtered ones: a job's
      // number must not change when the selection above does.
      name: labels.get(job)?.short ?? job,
      // A job's own timestamp when it has one. The fallback is the newest of its
      // run directories, not the oldest: a job is placed by when it was last
      // heard from, which is the only thing an unnamed tree records.
      at: jobRows[0].job_started_at ?? Math.max(...jobRows.map((row) => row.updated_at)),
      exact: jobRows[0].job_started_at != null,
      rows,
      tools: backends.map((backend) => ({
        backend,
        rows: rows.filter((row) => row.backend === backend),
      })),
      devices: [...new Set(jobRows.map((row) => row.device).filter(Boolean))].sort(),
      // Normally one: a job used to be one OpenVINO version by construction, and
      // still is unless a request asked for several -- in which case each group
      // is its own job. Read as a set anyway, so a tree assembled by hand cannot
      // make this claim something it is not.
      ovs: [...new Set(jobRows.map((row) => row.ov).filter(Boolean) as string[])].sort(),
      ok: jobRows.filter((row) => row.status === 'ok').length,
      failed: jobRows.filter((row) => row.status !== 'ok').length,
      rounds: hasRounds(jobRows)
        ? {
            count: Math.max(...jobRows.map((row) => row.rounds ?? row.round ?? 0)),
            warmup: jobRows.some((row) => row.warmup),
            sampled: [...new Set(jobRows
              .filter((row) => !row.warmup && row.sampled !== false && row.round != null)
              .map((row) => row.round as number))].sort((a, b) => a - b),
          }
        : null,
    }
  })
  // Newest first: the job a reader is looking for is almost always the last one
  // they started.
  return groups.sort((a, b) => b.at - a.at)
}

function JobHeader({ group, onDelete }: { group: JobGroup; onDelete: () => void }) {
  return (
    <Space size={8} wrap>
      {/* The number, then when it ran. Seconds included: two runs of the same
          sweep minutes apart are exactly the case this has to tell apart. */}
      <Text strong>{group.name}</Text>
      <Text type="secondary">{new Date(group.at * 1000).toLocaleString()}</Text>
      {!group.exact && (
        <Tooltip title="This directory was not named by a run, so this is when it last changed rather than when it started">
          <Tag style={{ marginInlineEnd: 0 }}>{group.job}</Tag>
        </Tooltip>
      )}
      <Space size={4}>
        {group.devices.map((device) => (
          <Tag key={device} color="blue" style={{ marginInlineEnd: 0 }}>
            {deviceLabel(device)}
          </Tag>
        ))}
      </Space>
      {/* Which tools ran: a collapsed job otherwise does not say whether it
          has one table inside or two. */}
      <Space size={4}>
        {group.tools.map((tool) => (
          <Tag key={tool.backend} style={{ marginInlineEnd: 0 }}>
            {toolLabel(tool.backend)}
          </Tag>
        ))}
      </Space>
      {/* Which runtime produced these numbers. One press of Run can now sweep
          several OpenVINO versions -- one per model -- so this is no longer
          implied by "the version the tab was set to", and two jobs are not
          comparable without it. */}
      {group.ovs.length > 0 && (
        <Tooltip title="The OpenVINO runtime this job was benchmarked against">
          <Tag color="geekblue" style={{ marginInlineEnd: 0 }}>
            OV {group.ovs.join(', ')}
          </Tag>
        </Tooltip>
      )}
      {group.rounds && (
        <Tooltip title="Every case was measured this many times; the table shows the median per configuration and expands to the rounds">
          <Tag color="purple" style={{ marginInlineEnd: 0 }}>
            {group.rounds.count} round{group.rounds.count === 1 ? '' : 's'}
            {group.rounds.warmup ? ' + warm-up' : ''}
            {' · metrics in '}
            {group.rounds.sampled.length ? `r${group.rounds.sampled.join(', r')}` : 'none'}
          </Tag>
        </Tooltip>
      )}
      <Text type="secondary" style={{ fontSize: 12 }}>
        {group.rows.length} case{group.rows.length === 1 ? '' : 's'} · {group.ok} ok
        {group.failed > 0 && ` · ${group.failed} failed`}
      </Text>
      <Tooltip title="Delete this job and every result it produced">
        <Button
          type="text"
          size="small"
          danger
          icon={<DeleteOutlined />}
          aria-label={`Delete ${group.name}`}
          // The heading is a Collapse label, so a bare click here would also
          // fold the panel the button is in.
          onClick={(event) => {
            event.stopPropagation()
            onDelete()
          }}
        />
      </Tooltip>
    </Space>
  )
}

/** How a row is keyed, in the table and in the selection above it. */
function caseKey(row: BenchMatrixRow): string {
  return row.case_dir || `${row.run}-${row.model}-${row.quant}`
}

/** Which case a row is: model, precision, device, and -- for a sweep -- its config. */
function identityColumns(rows: BenchMatrixRow[]): ColumnsType<BenchMatrixRow> {
  return [
    {
      title: 'Model',
      dataIndex: 'model',
      key: 'model',
      width: 230,
      ellipsis: true,
      fixed: 'left' as const,
    },
    {
      title: 'Precision',
      key: 'precision',
      width: 100,
      render: (_: unknown, row: BenchMatrixRow) => row.quant || row.precision,
    },
    {
      title: 'Device',
      key: 'device',
      width: 90,
      render: (_: unknown, row: BenchMatrixRow) => deviceLabel(row.device),
    },
    // Only for a tool that swept parameters: which combination each row is.
    ...(rows.some((row) => row.swept?.length)
      ? [
          {
            title: 'Config',
            key: 'config',
            width: 220,
            render: (_: unknown, row: BenchMatrixRow) => {
              const label = paramsLabel(row)
              return label ? (
                <Tooltip
                  title={Object.entries(row.params ?? {})
                    .map(([k, v]) => `${k}=${v}`)
                    .join('\n')}
                  overlayStyle={{ whiteSpace: 'pre-line' }}
                >
                  <Text style={{ fontSize: 12 }}>{label}</Text>
                </Tooltip>
              ) : (
                <Text type="secondary">-</Text>
              )
            },
          },
        ]
      : []),
  ]
}

/** The cases one tool produced within a job, in one table. */
function ToolCases({
  rows,
  keys,
  index,
  primaryMetric,
  onOpenCase,
  selected,
  onSelectedChange,
}: {
  rows: BenchMatrixRow[]
  keys: string[]
  index: MetricIndex
  primaryMetric: string | null
  onOpenCase: (row: BenchMatrixRow) => void
  selected: string[]
  onSelectedChange: (keys: string[]) => void
}) {
  if (hasRounds(rows)) {
    return (
      <RoundCases
        rows={rows}
        keys={keys}
        index={index}
        primaryMetric={primaryMetric}
        onOpenCase={onOpenCase}
        selected={selected}
        onSelectedChange={onSelectedChange}
      />
    )
  }
  const columns: ColumnsType<BenchMatrixRow> = [
    ...identityColumns(rows),    {
      title: 'Status',
      key: 'status',
      width: 110,
      render: (_: unknown, row: BenchMatrixRow) => <StatusTag row={row} />,
    },
    ...metricColumns(keys, index),
  ]
  // The selection is the job's, shared by its tool tables: this table reports
  // only its own rows, so the other tables' ticks are carried through.
  const own = new Set(rows.map(caseKey))
  return (
    <Table
      size="small"
      rowKey={caseKey}
      columns={columns}
      dataSource={rows}
      pagination={false}
      scroll={{ x: 'max-content' }}
      rowSelection={{
        selectedRowKeys: selected.filter((key) => own.has(key)),
        onChange: (keys) =>
          onSelectedChange([...selected.filter((key) => !own.has(key)), ...keys.map(String)]),
        // A row with no case directory is a summary row whose case never
        // wrote a log: there is nothing on disk to name, so it cannot be
        // deleted on its own. Deleting the whole job does take it.
        getCheckboxProps: (row) => ({ disabled: !row.case_dir }),
      }}
      onRow={(row) => ({
        onClick: (event) => {
          // Ticking a box is not a request to read the case. Without this the
          // checkbox column's clicks bubble out to the row and open the
          // drawer on top of the selection the user was making.
          if ((event.target as HTMLElement).closest('.ant-table-selection-column')) return
          onOpenCase(row)
        },
        style: { cursor: 'pointer' },
      })}
    />
  )
}

/** A round's place in its run: its number, and how it was measured. */
function RoundTag({ row }: { row: BenchMatrixRow }) {
  if (row.warmup) {
    return (
      <Tooltip title="Warm-up round: recorded, but left out of every median">
        <Tag style={{ marginInlineEnd: 0 }}>warm-up</Tag>
      </Tooltip>
    )
  }
  return (
    <Space size={4}>
      <Text>{row.round}</Text>
      {row.sampled !== false && (
        <Tooltip title="The hardware sampler ran in full for this round: its hardware columns come from here, its KPIs only if no round ran without it">
          <Tag color="purple" style={{ marginInlineEnd: 0 }}>metrics</Tag>
        </Tooltip>
      )}
    </Space>
  )
}

/**
 * A multi-round job's cases: one row per configuration, its numbers folded over
 * the rounds (see foldRounds), expanding to the rounds themselves. Ticking a
 * configuration ticks every round of it.
 */
function RoundCases({
  rows,
  keys,
  index,
  primaryMetric,
  onOpenCase,
  selected,
  onSelectedChange,
}: {
  rows: BenchMatrixRow[]
  keys: string[]
  index: MetricIndex
  primaryMetric: string | null
  onOpenCase: (row: BenchMatrixRow) => void
  selected: string[]
  onSelectedChange: (keys: string[]) => void
}) {
  const primary = primaryMetric && keys.includes(primaryMetric)
    ? primaryMetric
    : keys.find((k) => k.startsWith('kpi_throughput')) ?? null
  const groups = useMemo(() => foldRounds(rows, primary), [rows, primary])
  const byKey = useMemo(() => new Map(groups.map((g) => [g.key, g])), [groups])
  const own = new Set(rows.map(caseKey))
  const picked = new Set(selected)

  const columns: ColumnsType<RoundGroup> = [
    // The same identity columns as a single-round table, reading the
    // configuration's representative row.
    ...identityColumns(rows).map((col) => {
      const render = 'render' in col ? col.render : undefined
      const { dataIndex: _drop, ...rest } = col as typeof col & { dataIndex?: unknown }
      return {
        ...rest,
        render: (_: unknown, g: RoundGroup, i: number) =>
          render ? render(undefined, g.row, i) : g.row.model,
      }
    }) as ColumnsType<RoundGroup>,
    {
      title: (
        <Tooltip title="Repetitions the KPI median was taken over, their spread (coefficient of variation) and range. The warm-up round is not counted.">
          Rounds
        </Tooltip>
      ),
      key: 'rounds',
      width: 150,
      render: (_: unknown, g: RoundGroup) =>
        g.primary ? (
          <Tooltip title={`${metricTitle(primary!, index)}: ${formatMetric(g.primary.min)} – ${formatMetric(g.primary.max)}`}>
            <Text style={{ fontSize: 12 }}>
              n={g.primary.n}
              {g.primary.cv !== undefined && (
                <Text type={g.primary.cv > 5 ? 'warning' : 'secondary'} style={{ fontSize: 12 }}>
                  {' '}· CV {g.primary.cv.toFixed(1)}%
                </Text>
              )}
            </Text>
          </Tooltip>
        ) : (
          <Text type="secondary">n={g.measured.length}</Text>
        ),
    },
    {
      title: 'Status',
      key: 'status',
      width: 110,
      render: (_: unknown, g: RoundGroup) => <StatusTag row={g.row} />,
    },
    ...metricColumns(keys, index).map((col) => ({
      ...col,
      render: (_: unknown, g: RoundGroup) => formatMetric(g.row.metrics?.[String(col.key)]),
    })) as ColumnsType<RoundGroup>,
  ]

  const roundColumns: ColumnsType<BenchMatrixRow> = [
    { title: 'Round', key: 'round', width: 130, render: (_: unknown, row) => <RoundTag row={row} /> },
    { title: 'Status', key: 'status', width: 110, render: (_: unknown, row) => <StatusTag row={row} /> },
    ...metricColumns(keys, index),
  ]

  return (
    <Table<RoundGroup>
      size="small"
      rowKey="key"
      columns={columns}
      dataSource={groups}
      pagination={false}
      scroll={{ x: 'max-content' }}
      rowSelection={{
        selectedRowKeys: groups
          .filter((g) => g.rounds.every((row) => !row.case_dir || picked.has(caseKey(row))))
          .filter((g) => g.rounds.some((row) => row.case_dir))
          .map((g) => g.key),
        onChange: (chosen) => {
          const cases = chosen.flatMap((key) =>
            (byKey.get(String(key))?.rounds ?? []).filter((row) => row.case_dir).map(caseKey))
          onSelectedChange([...selected.filter((key) => !own.has(key)), ...cases])
        },
        getCheckboxProps: (g) => ({ disabled: !g.rounds.some((row) => row.case_dir) }),
      }}
      expandable={{
        expandedRowRender: (g) => (
          <Table
            size="small"
            rowKey={caseKey}
            columns={roundColumns}
            dataSource={g.rounds}
            pagination={false}
            onRow={(row) => ({ onClick: () => onOpenCase(row), style: { cursor: 'pointer' } })}
          />
        ),
      }}
      onRow={(g) => ({
        onClick: (event) => {
          const target = event.target as HTMLElement
          if (target.closest('.ant-table-selection-column') || target.closest('.ant-table-row-expand-icon-cell')) return
          onOpenCase(g.row)
        },
        style: { cursor: 'pointer' },
      })}
    />
  )
}

/** The cases one job produced: one table per tool that ran in it. */
function JobCases({
  group,
  metrics,
  showAll,
  index,
  primaryMetric,
  onOpenCase,
  selected,
  onSelectedChange,
  onDeleteSelected,
}: {
  group: JobGroup
  metrics: BenchMetricMeta[]
  showAll: boolean
  index: MetricIndex
  primaryMetric: string | null
  onOpenCase: (row: BenchMatrixRow) => void
  selected: string[]
  onSelectedChange: (keys: string[]) => void
  onDeleteSelected: () => void
}) {
  // A heading per table only when there is more than one: a job that ran one
  // tool reads as it always did, and says which in its own heading.
  const titled = group.tools.length > 1
  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      {/* Only once something is ticked. A disabled button above every table is
          a permanent reminder of an operation nobody has asked for. Once per
          job, not per tool: ticks in either table are one selection. */}
      {selected.length > 0 && (
        <Space size={8}>
          <Button size="small" danger icon={<DeleteOutlined />} onClick={onDeleteSelected}>
            Delete {selected.length} case{selected.length === 1 ? '' : 's'}
          </Button>
          <Button size="small" type="text" onClick={() => onSelectedChange([])}>
            Clear
          </Button>
        </Space>
      )}
      {group.tools.map((tool) => (
        <Space key={tool.backend} direction="vertical" size={4} style={{ width: '100%' }}>
          {titled && (
            <Space size={8}>
              <Text strong>{toolLabel(tool.backend)}</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {tool.rows.length} case{tool.rows.length === 1 ? '' : 's'}
              </Text>
            </Space>
          )}
          <ToolCases
            rows={tool.rows}
            keys={metricKeysOf(tool.backend, tool.rows, metrics, showAll)}
            index={index}
            primaryMetric={primaryMetric}
            onOpenCase={onOpenCase}
            selected={selected}
            onSelectedChange={onSelectedChange}
          />
        </Space>
      ))}
    </Space>
  )
}

interface Props {
  matrix: BenchMatrixData | null
  onRefresh: () => void
  onOpenCase: (row: BenchMatrixRow) => void
  /** Remove one job's run directories. Resolves once the tree has been re-read. */
  onDeleteJob: (job: string) => Promise<void>
  /** Remove the named case directories. Resolves once the tree has been re-read. */
  onDeleteCases: (cases: string[]) => Promise<void>
}

/** One term of the search: nothing chosen on an axis constrains nothing. */
function matches(selected: string[], value: string): boolean {
  return selected.length === 0 || selected.includes(value)
}

export default function BenchResults({
  matrix,
  onRefresh,
  onOpenCase,
  onDeleteJob,
  onDeleteCases,
}: Props) {
  const [showAll, setShowAll] = useState(false)
  // null until the reader opens or closes something: up to then the newest job
  // is open, which is what they came to see. Their choice sticks afterwards.
  const [openJobs, setOpenJobs] = useState<string[] | null>(null)
  // Ticked cases, per job. Per job rather than one flat set because the tables
  // are independent: a selection made in one job and then deleted from another
  // would be a delete button acting on rows that are not on screen.
  const [ticked, setTicked] = useState<Record<string, string[]>>({})

  const dims = matrix?.dimensions
  // Both start empty, which is every case on disk -- the newest job is the one
  // expanded below, so "what did I just run?" is answered without a filter
  // saying so. This used to be paired with an "Only the selected model"
  // checkbox driven by the browser on the left; two controls deciding one thing
  // is one too many, and the model selector here is the one that can also say
  // "these three".
  const [models, setModels] = useState<string[]>([])
  const [jobs, setJobs] = useState<string[]>([])

  const index = useMemo(() => indexMetrics(matrix?.metrics), [matrix?.metrics])

  const rows = useMemo(
    () =>
      (matrix?.rows ?? []).filter(
        (row) => matches(models, row.model) && matches(jobs, row.job || row.run),
      ),
    [matrix?.rows, models, jobs],
  )

  // Built from every row, never from the filtered ones -- see jobLabels.
  const labels = useMemo(() => jobLabels(matrix?.rows ?? []), [matrix?.rows])

  const jobGroups = useMemo(() => jobGroupsOf(rows, labels), [rows, labels])

  // What the reader opened, of what the search left standing. Their choice is
  // remembered, but a job they opened before narrowing the search is not among
  // the tables any more -- and an activeKey naming it would leave every visible
  // table shut, which reads as "these jobs are empty".
  const activeJobs = useMemo(() => {
    const visible = jobGroups.map((group) => group.job)
    // Closed everything on purpose: that is a choice, and it survives.
    if (openJobs?.length === 0) return []
    const kept = (openJobs ?? []).filter((job) => visible.includes(job))
    // Nothing of theirs survived, or they have not chosen yet: the newest.
    return kept.length ? kept : visible.slice(0, 1)
  }, [openJobs, jobGroups])

  // Both deletions confirm first and both say what goes: these remove
  // directories, and there is nothing to undo them with.
  const confirmDeleteJob = useCallback(
    (group: JobGroup) => {
      Modal.confirm({
        title: `Delete ${group.name}?`,
        width: 520,
        okText: 'Delete',
        okButtonProps: { danger: true },
        content: (
          <Space direction="vertical" size={4} style={{ marginTop: 8 }}>
            <Text>
              {group.rows.length} case{group.rows.length === 1 ? '' : 's'} measured{' '}
              {new Date(group.at * 1000).toLocaleString()}
              {group.devices.length > 0 &&
                ` on ${group.devices.map(deviceLabel).join(', ')}`}
              .
            </Text>
            <Text type="secondary">
              Its result directories are removed from disk. This cannot be undone.
            </Text>
          </Space>
        ),
        onOk: () => onDeleteJob(group.job),
      })
    },
    [onDeleteJob],
  )

  const confirmDeleteCases = useCallback(
    (group: JobGroup) => {
      const keys = new Set(ticked[group.job] ?? [])
      const rows = group.rows.filter((row) => keys.has(caseKey(row)) && row.case_dir)
      if (!rows.length) return
      Modal.confirm({
        title: `Delete ${rows.length} case${rows.length === 1 ? '' : 's'} from ${group.name}?`,
        width: 520,
        okText: 'Delete',
        okButtonProps: { danger: true },
        content: (
          <Space direction="vertical" size={4} style={{ marginTop: 8 }}>
            {/* Named, up to a point: the question is whether these are the rows
                the user meant, and a count alone cannot answer it. */}
            {rows.slice(0, 8).map((row) => (
              <Text key={caseKey(row)} style={{ fontSize: 12 }}>
                {row.model} · {row.quant || row.precision} · {deviceLabel(row.device)}
              </Text>
            ))}
            {rows.length > 8 && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                and {rows.length - 8} more
              </Text>
            )}
            <Text type="secondary">
              Their case directories are removed from disk. This cannot be undone.
            </Text>
          </Space>
        ),
        onOk: async () => {
          await onDeleteCases(rows.map((row) => row.case_dir))
          // Only after the removal succeeded: a failed delete leaves the rows on
          // screen, and they should still be ticked so it can be retried.
          setTicked((prev) => ({ ...prev, [group.job]: [] }))
        },
      })
    },
    [onDeleteCases, ticked],
  )

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space size={12} wrap>
        <Button size="small" icon={<ReloadOutlined />} onClick={onRefresh}>
          Refresh
        </Button>
        <Tooltip title="Search by model, across every job. Empty lists them all.">
          <Select
            mode="multiple"
            allowClear
            size="small"
            style={{ minWidth: 300, maxWidth: 450 }}
            placeholder="All models"
            value={models}
            maxTagCount="responsive"
            onChange={setModels}
            // Model names run past forty characters and differ near their end,
            // so the list is filtered by what is typed rather than scrolled.
            optionFilterProp="label"
            options={(dims?.models ?? []).map((model) => ({ value: model, label: model }))}
          />
        </Tooltip>
        <Tooltip title="Which runs to list; empty lists them all. One job is one press of Run, across every device it swept. Job1 is the oldest.">
          <Select
            mode="multiple"
            allowClear
            size="small"
            style={{ minWidth: 300, maxWidth: 450 }}
            placeholder="All jobs"
            value={jobs}
            maxTagCount="responsive"
            onChange={setJobs}
            optionFilterProp="label"
            // "Job3" alone, both in the list and as a tag: several selected jobs
            // have to fit across one row, and when each one ran is in the
            // heading of its own table below. The full name is on hover, for
            // the times it has to be matched against a directory.
            options={(dims?.jobs ?? []).map((job) => ({
              value: job,
              label: labels.get(job)?.short ?? job,
              title: labels.get(job)?.long ?? job,
            }))}
          />
        </Tooltip>
        <Tooltip title="Every metric the pipeline recorded, rather than the headline ones">
          <Checkbox checked={showAll} onChange={(e) => setShowAll(e.target.checked)}>
            All metrics
          </Checkbox>
        </Tooltip>
        {matrix && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {jobGroups.length} job{jobGroups.length === 1 ? '' : 's'} · {rows.length} case
            {rows.length === 1 ? '' : 's'}
            {!matrix.metrics_available && ' · no hardware metrics recorded'}
            {matrix.metrics_available && ' · click a row for the full case'}
          </Text>
        )}
      </Space>

      {rows.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            (matrix?.rows.length ?? 0) > 0
              ? 'No cases match this search — widen the model or job selection above.'
              : 'No benchmark results yet.'
          }
        />
      ) : (
        <Collapse
          activeKey={activeJobs}
          onChange={(keys) => setOpenJobs(Array.isArray(keys) ? keys : [keys])}
          items={jobGroups.map((group) => ({
            key: group.job,
            label: <JobHeader group={group} onDelete={() => confirmDeleteJob(group)} />,
            children: (
              <JobCases
                group={group}
                metrics={matrix?.metrics ?? []}
                showAll={showAll}
                index={index}
                primaryMetric={matrix?.primary_metric ?? null}
                onOpenCase={onOpenCase}
                selected={ticked[group.job] ?? []}
                onSelectedChange={(next) =>
                  setTicked((prev) => ({ ...prev, [group.job]: next }))
                }
                onDeleteSelected={() => confirmDeleteCases(group)}
              />
            ),
          }))}
        />
      )}
    </Space>
  )
}
