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
// The two selectors above the tables are a search, not a view setting: each one
// narrows the tree by one axis, an empty one says nothing about that axis, and
// together they mean both at once. That is why they do not share the Analysis
// tab's "empty means empty" rule -- a chart has to be told what to plot, while a
// search box that has not been typed into is asking for everything.

import React, { useMemo, useState } from 'react'
import {
  Button,
  Checkbox,
  Collapse,
  Empty,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'

import type { BenchMatrixData, BenchMatrixRow } from '../api/types'
import { COLORS } from '../styles/theme'
import {
  deviceLabel,
  formatMetric,
  indexMetrics,
  jobLabels,
  metricTitle,
  type JobLabel,
  type MetricIndex,
} from '../utils/benchMetrics'

const { Text } = Typography

// What a benchmark run is usually being read for: how much was asked of it, how
// long the first and each subsequent token took, what came out per second, and
// how hard the part of the machine that ran it was working.
//
// The accelerator columns are occupancy, not clocks: a GPU clock of 2500 MHz is
// the same number whether the device is saturated or idle at its boost point,
// so it answers nothing about the run, while "GPU compute busy 99%" says the
// model had the device. CPU package power left with them -- it is the
// denominator of performance-per-watt, which is worth checking but not worth a
// column in the table everyone reads first; it is one click away in the case
// drawer, and under "All metrics" here.
const KEY_METRICS = [
  'kpi_input_token_size',
  'kpi_first_latency_ms',
  'kpi_second_latency_ms',
  'kpi_throughput_tokens_s',
  'memory_used_gb_median',
  'memory_bandwidth_gb_s_median',
  'cpu_usage_percent_median',
  'gpu_compute_busy_percent_median',
  'npu_utilization_percent_median',
]

/**
 * A column title, "TTFT (ms)", with its definition on hover.
 *
 * One line, unit in parentheses. The unit used to sit under the name on a
 * second line to keep columns narrow; the names are short enough now (and lower
 * case, so they do not compete with the numbers) that the saving was not worth
 * a header shaped unlike every other header in the product.
 */
function MetricHead({ metricKey, index }: { metricKey: string; index: MetricIndex }) {
  const head = (
    <div style={{ textAlign: 'center', lineHeight: 1.25 }}>{metricTitle(metricKey, index)}</div>
  )
  const description = index.get(metricKey)?.description
  return description ? <Tooltip title={description}>{head}</Tooltip> : head
}

/**
 * The metric columns.
 *
 * Headers are centred over the column, values right-aligned within it: the
 * header names the column, but the numbers are read down it and digits have to
 * line up for that to work.
 */
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

function StatusTag({ row }: { row: BenchMatrixRow }) {
  const tag = (
    <Tag color={row.status === 'ok' ? 'success' : 'error'} style={{ marginInlineEnd: 0 }}>
      {row.status || 'unknown'}
    </Tag>
  )
  // A failed case says why on hover. "failed" alone leaves the reader to open a
  // log to find out whether the device was busy or the model was never
  // supported there.
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
  devices: string[]
  ok: number
  failed: number
}

function jobGroupsOf(rows: BenchMatrixRow[], labels: Map<string, JobLabel>): JobGroup[] {
  const byJob = new Map<string, BenchMatrixRow[]>()
  for (const row of rows) {
    const job = row.job || row.run
    byJob.set(job, [...(byJob.get(job) ?? []), row])
  }
  const groups = [...byJob.entries()].map(([job, jobRows]) => ({
    job,
    // Numbered over every job on disk, not over the filtered ones: a job's
    // number must not change when the selection above does.
    name: labels.get(job)?.short ?? job,
    // A job's own timestamp when it has one. The fallback is the newest of its
    // run directories, not the oldest: a job is placed by when it was last
    // heard from, which is the only thing an unnamed tree records.
    at: jobRows[0].job_started_at ?? Math.max(...jobRows.map((row) => row.updated_at)),
    exact: jobRows[0].job_started_at != null,
    rows: [...jobRows].sort(
      (a, b) =>
        a.model.localeCompare(b.model) ||
        a.precision.localeCompare(b.precision) ||
        a.device.localeCompare(b.device),
    ),
    devices: [...new Set(jobRows.map((row) => row.device).filter(Boolean))].sort(),
    ok: jobRows.filter((row) => row.status === 'ok').length,
    failed: jobRows.filter((row) => row.status !== 'ok').length,
  }))
  // Newest first: the job a reader is looking for is almost always the last one
  // they started.
  return groups.sort((a, b) => b.at - a.at)
}

function JobHeader({ group }: { group: JobGroup }) {
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
      <Text type="secondary" style={{ fontSize: 12 }}>
        {group.rows.length} case{group.rows.length === 1 ? '' : 's'} · {group.ok} ok
        {group.failed > 0 && ` · ${group.failed} failed`}
      </Text>
    </Space>
  )
}

/** The cases one job produced, in one table. */
function JobCases({
  group,
  keys,
  index,
  onOpenCase,
}: {
  group: JobGroup
  keys: string[]
  index: MetricIndex
  onOpenCase: (row: BenchMatrixRow) => void
}) {
  const columns: ColumnsType<BenchMatrixRow> = [
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
    {
      title: 'Status',
      key: 'status',
      width: 110,
      render: (_: unknown, row: BenchMatrixRow) => <StatusTag row={row} />,
    },
    ...metricColumns(keys, index),
  ]
  return (
    <Table
      size="small"
      rowKey={(row) => row.case_dir || `${row.run}-${row.model}-${row.quant}`}
      columns={columns}
      dataSource={group.rows}
      pagination={false}
      scroll={{ x: 'max-content' }}
      onRow={(row) => ({
        onClick: () => onOpenCase(row),
        style: { cursor: 'pointer' },
      })}
    />
  )
}

interface Props {
  matrix: BenchMatrixData | null
  onRefresh: () => void
  onOpenCase: (row: BenchMatrixRow) => void
}

/** One term of the search: nothing chosen on an axis constrains nothing. */
function matches(selected: string[], value: string): boolean {
  return selected.length === 0 || selected.includes(value)
}

export default function BenchResults({ matrix, onRefresh, onOpenCase }: Props) {
  const [showAll, setShowAll] = useState(false)
  // null until the reader opens or closes something: up to then the newest job
  // is open, which is what they came to see. Their choice sticks afterwards.
  const [openJobs, setOpenJobs] = useState<string[] | null>(null)

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

  // Only columns something actually measured. A run with no GPU sampling should
  // not get an empty GPU column just because the schema has one.
  const keys = useMemo(() => {
    const present = new Set(rows.flatMap((row) => Object.keys(row.metrics)))
    const wanted = showAll ? (matrix?.metrics ?? []).map((m) => m.key) : KEY_METRICS
    return wanted.filter((key) => present.has(key))
  }, [rows, showAll, matrix?.metrics])

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
            label: <JobHeader group={group} />,
            children: <JobCases group={group} keys={keys} index={index} onOpenCase={onOpenCase} />,
          }))}
        />
      )}
    </Space>
  )
}
