// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
// Per-model memory estimate for selected precision and device combinations.
// The estimate warns about over-budget configurations without blocking a run.

import React, { useMemo } from 'react'
import { Alert, Select, Space, Table, Tag, Tooltip, Typography } from 'antd'
import { WarningTwoTone } from '@ant-design/icons'

import type {
  BenchDevice,
  BenchModel,
  BenchPrecision,
  DynamicInfoData,
  StaticInfoData,
} from '../api/types'
import { COLORS } from '../styles/theme'
import type { FitVerdict } from '../utils/benchMemory'
import {
  DEFAULT_STREAMS,
  DEFAULT_WINDOW,
  STREAM_CHOICES,
  WINDOW_CHOICES,
  formatBytesGB,
  formatTokens,
  kvCacheBytes,
  memoryVerdict,
  minNeededBytes,
  modelMaxWindow,
  resolveDeviceTargets,
} from '../utils/benchMemory'

const { Text } = Typography

/**
 * One model, with the precisions and devices the run would actually ask it for.
 *
 * Both travel per model rather than per dialog: run settings are kept per model,
 * so two models in the same benchmark can be headed for different devices.
 */
export interface MemoryPreflightItem {
  model: BenchModel
  precisions: BenchPrecision[]
  devices: BenchDevice[]
}

/** What the two controls above the table are set to, for one model. */
export interface FitChoice {
  window: number
  streams: number
}

export const DEFAULT_FIT_CHOICE: FitChoice = {
  window: DEFAULT_WINDOW,
  streams: DEFAULT_STREAMS,
}

// One (precision, device-target) line of a model's table.
interface ConfigRow {
  key: string
  precision: BenchPrecision
  target: string
  /** Weights plus logits: the part of the bar that neither control moves. */
  minNeeded: number | null
  /** KV cache at the chosen context × streams. */
  kvBytes: number | null
  /** The context this row was costed at. */
  window: number
  /** Sequences at once this row was costed for: the chosen concurrency. */
  streams: number
  avail: number | null
  /** How the load lands against that free memory -- the row's last word. */
  verdict: FitVerdict
  // How many rows this precision spans, set only on the first row of each group
  // (0 on the rest) so the column merges its cells.
  precisionRowSpan: number
}

/**
 * The context lengths this one model can be asked about.
 *
 * Never a length the model could not be run at: the list stops at its own
 * limit. That limit is itself the last step when it is not already one of them
 * -- a model published at 40K would otherwise top out at the 32K step, leaving
 * the length it is actually capable of the one length that could not be asked
 * about. A model whose limit is unknown keeps every choice: there is nothing to
 * cap against.
 */
export function windowChoicesFor(model: BenchModel): number[] {
  const ceiling = modelMaxWindow(model.memory)
  if (ceiling == null) return WINDOW_CHOICES
  const offered = WINDOW_CHOICES.filter((choice) => choice <= ceiling)
  if (!offered.includes(ceiling)) offered.push(ceiling)
  return offered.length ? offered : [ceiling]
}

/**
 * The choice this model is actually costed at.
 *
 * A length carried over from a default (or from a model with a longer ceiling)
 * may not be one this model offers; fall back to the longest that is rather
 * than costing a row at a context it could never be asked for.
 */
export function resolveChoice(model: BenchModel, choice: FitChoice): FitChoice {
  const choices = windowChoicesFor(model)
  return {
    window: choices.includes(choice.window) ? choice.window : choices[choices.length - 1],
    streams: choice.streams,
  }
}

/**
 * Every configuration the run would sweep this model in, as one list.
 *
 * Rows are emitted grouped -- a precision's are contiguous -- so the first row
 * of each group carries its span and the rest collapse into it.
 */
function buildRows(
  item: MemoryPreflightItem,
  dyn: DynamicInfoData | null,
  stat: StaticInfoData | null,
  window: number,
  streams: number,
): ConfigRow[] {
  const rows: ConfigRow[] = []
  const mem = item.model.memory
  for (const precision of item.precisions) {
    const groupStart = rows.length
    for (const device of item.devices) {
      for (const target of resolveDeviceTargets(device, dyn, stat)) {
        const minNeeded = minNeededBytes(mem, precision)
        const kvBytes = kvCacheBytes(mem, window, streams)
        const avail = target.availBytes
        // A verdict needs both numbers: an unknown requirement is not a failing
        // one, and memoryVerdict says so rather than guessing.
        const needed = minNeeded == null ? null : minNeeded + (kvBytes ?? 0)
        rows.push({
          key: `${precision}-${device}-${target.label}`,
          precision,
          target: target.label,
          minNeeded,
          kvBytes,
          window,
          streams,
          avail,
          verdict: memoryVerdict(needed, avail),
          precisionRowSpan: 0,
        })
      }
    }
    if (rows.length > groupStart) rows[groupStart].precisionRowSpan = rows.length - groupStart
  }
  return rows
}

/**
 * Whether any configuration of this model is over budget at the given choice.
 *
 * Exported because the pages are tabs now: the model whose numbers are red may
 * not be the page on screen, so the modal marks its tab with this and sums it
 * into one line above the tabs.
 */
export function hasOverBudget(
  item: MemoryPreflightItem,
  dyn: DynamicInfoData | null,
  stat: StaticInfoData | null,
  choice: FitChoice,
): boolean {
  const { window, streams } = resolveChoice(item.model, choice)
  return buildRows(item, dyn, stat, window, streams).some((row) => row.verdict === 'full')
}

const BAR_HEIGHT = 8

/** The track every bar is drawn in: full width, the unfilled part of it. */
function Bar({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        display: 'flex',
        width: '100%',
        height: BAR_HEIGHT,
        borderRadius: BAR_HEIGHT / 2,
        overflow: 'hidden',
        background: COLORS.border,
      }}
    >
      {children}
    </div>
  )
}

function Segment({ pct, color }: { pct: number; color: string }) {
  return <div style={{ width: `${pct}%`, background: color }} />
}

/** A bar colour, inline in the sentence that explains what it means. */
function Swatch({ color }: { color: string }) {
  return (
    <span
      style={{
        display: 'inline-block',
        width: 10,
        height: 10,
        borderRadius: 2,
        background: color,
        verticalAlign: 'middle',
      }}
    />
  )
}

/**
 * What the load costs, drawn against what the device has free.
 *
 * The one bar in the table, because there is one question: does this fit. It is
 * the free memory, filled with the weights (which neither control moves) and
 * the KV cache, which is the context times the concurrency -- so the two
 * controls above the table each move one visible part of it, and what a longer
 * context or a second stream actually costs is something to look at rather than
 * to work out. Over budget the bar is full and red; how far past the end it
 * would have gone is in the figures, which is where a number that has no room
 * to be drawn belongs.
 */
function MemoryCell({ row }: { row: ConfigRow }) {
  const { minNeeded, kvBytes, avail } = row
  const total = minNeeded == null ? null : minNeeded + (kvBytes ?? 0)
  const weightPct = minNeeded != null && avail ? (minNeeded / avail) * 100 : 0
  const kvPct = kvBytes != null && avail ? (kvBytes / avail) * 100 : 0

  return (
    <Tooltip
      title={
        `weights + output layer ${formatBytesGB(minNeeded)} · ` +
        `KV cache for ${formatTokens(row.window)} tokens` +
        (row.streams > 1 ? ` × ${row.streams} streams` : '') +
        ` ${formatBytesGB(kvBytes)} · free now ${formatBytesGB(avail)}`
      }
    >
      <Space direction="vertical" size={2} style={{ width: '100%' }}>
        {avail != null && total != null && (
          <Bar>
            {row.verdict === 'full' ? (
              // Past the end of the track there is nowhere left to draw, so an
              // over-budget row fills it: the figures and the verdict carry how
              // far past it went.
              <Segment pct={100} color={COLORS.red} />
            ) : (
              <>
                <Segment pct={weightPct} color={COLORS.accent} />
                <Segment pct={kvPct} color={`${COLORS.accent}66`} />
              </>
            )}
          </Bar>
        )}
        <Space size={4}>
          <Text style={{ color: row.verdict === 'full' ? COLORS.red : undefined }}>
            {formatBytesGB(total)}
          </Text>
          <Text type="secondary">/ {formatBytesGB(avail)}</Text>
        </Space>
      </Space>
    </Tooltip>
  )
}

const COLUMNS = [
  {
    title: 'Precision',
    dataIndex: 'precision',
    key: 'precision',
    // Merge the repeated precision cells into one per group.
    onCell: (row: ConfigRow) => ({ rowSpan: row.precisionRowSpan }),
    width: 110,
    render: (p: BenchPrecision) => <Tag>{p}</Tag>,
  },
  { title: 'Device', dataIndex: 'target', key: 'target' },
  {
    title: 'Needs / Available',
    key: 'memory',
    width: 260,
    render: (_: unknown, row: ConfigRow) => <MemoryCell row={row} />,
  },
  {
    // The row's answer in one word, which is what most of this table is read
    // for; everything to its left is why. Green when it fits, red when it does
    // not -- colour on the text alone, no filled tag.
    title: 'status',
    key: 'verdict',
    width: 96,
    render: (_: unknown, row: ConfigRow) => {
      const ok = row.verdict !== 'full'
      return (
        <Text strong style={{ color: ok ? COLORS.green : COLORS.red }}>
          {ok ? 'ok' : 'not ok'}
        </Text>
      )
    },
  },
]

interface Props {
  item: MemoryPreflightItem
  dynamicInfo: DynamicInfoData | null
  staticInfo: StaticInfoData | null
  /** The context × streams this page is costed at, held by the parent so
   *  switching tabs does not lose what was chosen here. */
  choice: FitChoice
  onChoiceChange: (next: FitChoice) => void
}

export default function BenchMemoryFitPanel({
  item,
  dynamicInfo,
  staticInfo,
  choice,
  onChoiceChange,
}: Props) {
  const model = item.model
  const windowChoices = useMemo(() => windowChoicesFor(model), [model])
  const { window: contextTokens, streams } = resolveChoice(model, choice)

  const rows = useMemo(
    () => buildRows(item, dynamicInfo, staticInfo, contextTokens, streams),
    [item, dynamicInfo, staticInfo, contextTokens, streams],
  )

  const anyOverBudget = rows.some((row) => row.verdict === 'full')
  const modelWindow = modelMaxWindow(model.memory)

  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      {/* The inputs the estimate has, above the table because they change every
          row of it: context window, times how many streams at once. The model's
          own ceiling sits with them -- it is what bounds the first control, so
          it belongs where that control is rather than in a column repeating it
          down every row. */}
      <Space size={8} wrap>
        <Text style={{ fontSize: 12 }}>Context window</Text>
        <Select<number>
          size="small"
          value={contextTokens}
          onChange={(value) => onChoiceChange({ window: value, streams })}
          style={{ width: 100 }}
          options={windowChoices.map((value) => ({ label: formatTokens(value), value }))}
        />
        {/* Highlighted so it reads as "times", not as a stray letter. */}
        <Text strong style={{ fontSize: 16, color: COLORS.accent }}>
          ×
        </Text>
        <Text style={{ fontSize: 12 }}>Concurrent streams</Text>
        <Select<number>
          size="small"
          value={streams}
          onChange={(value) => onChoiceChange({ window: contextTokens, streams: value })}
          style={{ width: 72 }}
          options={STREAM_CHOICES.map((value) => ({ label: `${value}`, value }))}
        />
        {modelWindow != null && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            this model goes up to {formatTokens(modelWindow)}
          </Text>
        )}
      </Space>

      <Space direction="vertical" size={0}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          <Swatch color={COLORS.accent} /> filled with the weights, one copy serves every stream,
        </Text>
        <Text type="secondary" style={{ fontSize: 12 }}>
          <Swatch color={`${COLORS.accent}66`} /> is the KV cache, which costs that per token per stream.
        </Text>
      </Space>

      {anyOverBudget && (
        <Alert
          type="warning"
          showIcon
          icon={<WarningTwoTone twoToneColor={COLORS.red} />}
          message={
            `Some configurations need more memory than is free now at ` +
            `${formatTokens(contextTokens)} tokens` +
            (streams > 1 ? ` × ${streams} streams` : '')
          }
          description="Configurations in red may fail or trigger swapping for lack of memory. A shorter context or fewer streams makes the KV cache part smaller; the weights do not move. You can still choose to run."
        />
      )}

      <Table<ConfigRow> size="small" columns={COLUMNS} dataSource={rows} pagination={false} />

      {/* Every figure that depends on the model reads N/A without a footprint,
          which on its own looks like a bug rather than like missing data. The
          rows are still worth showing: the free memory on each device is. */}
      {!model.memory && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          No weight / KV-cache data for {model.id} yet — it is being fetched;
          until it lands, everything above that depends on the model reads N/A.
        </Text>
      )}
    </Space>
  )
}
