// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// The dialog shown before a measured Run: for every model × precision × device
// the run would sweep, it states how much memory the load needs against how much
// that device has free right now, and how large a context ("window") the free
// memory would allow. It is a warning, not a gate -- the user can Run anyway
// (the numbers are estimates, and a device may swap or spill), so a config that
// does not fit is drawn red rather than disabled.
//
// Free-memory readings come from the same monitor endpoints System Overview uses
// (getDynamicInfo memory/gpu + getStaticInfo for the iGPU/dGPU split), passed in
// by the parent so this component stays a pure render of what it is handed.

import React, { useMemo } from 'react'
import { Alert, Modal, Space, Spin, Table, Tag, Typography } from 'antd'
import { WarningTwoTone } from '@ant-design/icons'

import type {
  BenchDevice,
  BenchModel,
  BenchPrecision,
  DynamicInfoData,
  StaticInfoData,
} from '../api/types'
import { COLORS } from '../styles/theme'
import {
  formatBytesGB,
  formatTokens,
  maxWindowForBudget,
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

interface Props {
  open: boolean
  items: MemoryPreflightItem[]
  dynamicInfo: DynamicInfoData | null
  staticInfo: StaticInfoData | null
  /** True while the memory readings are still being fetched. */
  loading: boolean
  onRun: () => void
  onCancel: () => void
}

// One (precision, device-target) line for a model's table.
interface ConfigRow {
  key: string
  precision: BenchPrecision
  target: string
  minNeeded: number | null
  avail: number | null
  overBudget: boolean
  modelWindow: number | null
  budgetWindow: number | null
  // How many rows this precision spans, set only on the first row of each
  // precision group (0 on the rest) so the precision column merges its cells.
  precisionRowSpan: number
}

function buildRows(
  item: MemoryPreflightItem,
  dyn: DynamicInfoData | null,
  stat: StaticInfoData | null,
): ConfigRow[] {
  const mem = item.model.memory
  const rows: ConfigRow[] = []
  for (const precision of item.precisions) {
    // Rows are emitted grouped by precision, so a group is a contiguous block:
    // the first row carries the span, the rest collapse into it.
    const groupStart = rows.length
    for (const device of item.devices) {
      for (const target of resolveDeviceTargets(device, dyn, stat)) {
        const minNeeded = minNeededBytes(mem, precision)
        const avail = target.availBytes
        // "Over budget" only means something when both numbers are known; an
        // unknown requirement is not an over-budget one.
        const known = minNeeded != null && avail != null
        rows.push({
          key: `${precision}-${device}-${target.label}`,
          precision,
          target: target.label,
          minNeeded,
          avail,
          overBudget: known ? (minNeeded as number) > (avail as number) : false,
          modelWindow: modelMaxWindow(mem),
          budgetWindow: maxWindowForBudget(avail, mem, precision),
          precisionRowSpan: 0,
        })
      }
    }
    if (rows.length > groupStart) rows[groupStart].precisionRowSpan = rows.length - groupStart
  }
  return rows
}

// The numerator (what the model asks for) is coloured by whether it fits: green
// inside the budget, red outside it. Unknown either side stays uncoloured.
function fitColor(need: number | null, budget: number | null): string | undefined {
  if (need == null || budget == null) return undefined
  return need <= budget ? COLORS.green : COLORS.red
}

function MemoryCell({ row }: { row: ConfigRow }) {
  return (
    <Space size={4}>
      <Text style={{ color: fitColor(row.minNeeded, row.avail) }}>
        {formatBytesGB(row.minNeeded)}
      </Text>
      <Text type="secondary">/</Text>
      <Text>{formatBytesGB(row.avail)}</Text>
      {row.overBudget && (
        <Tag color="error" style={{ marginInlineEnd: 0 }}>
          Out of memory
        </Tag>
      )}
    </Space>
  )
}

function WindowCell({ row }: { row: ConfigRow }) {
  // Numerator: the model's own context ceiling; coloured by whether the free
  // memory can actually reach it. Denominator: the largest window that fits now.
  return (
    <Space size={4}>
      <Text style={{ color: fitColor(row.modelWindow, row.budgetWindow) }}>
        {formatTokens(row.modelWindow)}
      </Text>
      <Text type="secondary">/</Text>
      <Text>{formatTokens(row.budgetWindow)}</Text>
    </Space>
  )
}

const COLUMNS = [
  {
    title: 'Precision',
    dataIndex: 'precision',
    key: 'precision',
    // Merge the repeated precision cells into one per group.
    onCell: (row: ConfigRow) => ({ rowSpan: row.precisionRowSpan }),
    render: (p: BenchPrecision) => <Tag>{p}</Tag>,
  },
  { title: 'Device', dataIndex: 'target', key: 'target' },
  {
    title: 'Min. required / free now',
    key: 'memory',
    render: (_: unknown, row: ConfigRow) => <MemoryCell row={row} />,
  },
  {
    title: 'Max window / supported by free memory',
    key: 'window',
    render: (_: unknown, row: ConfigRow) => <WindowCell row={row} />,
  },
]

export default function BenchMemoryPreflightModal({
  open,
  items,
  dynamicInfo,
  staticInfo,
  loading,
  onRun,
  onCancel,
}: Props) {
  const perModel = useMemo(
    () =>
      items.map((item) => ({
        item,
        rows: buildRows(item, dynamicInfo, staticInfo),
      })),
    [items, dynamicInfo, staticInfo],
  )

  const anyOverBudget = perModel.some(({ rows }) => rows.some((r) => r.overBudget))

  return (
    <Modal
      open={open}
      title="Pre-run memory check"
      width={760}
      okText="Run"
      cancelText="Cancel"
      onOk={onRun}
      onCancel={onCancel}
      okButtonProps={{ danger: anyOverBudget }}
      destroyOnClose
    >
      {loading ? (
        <div style={{ textAlign: 'center', padding: '32px 0' }}>
          <Spin tip="Reading current memory…" />
        </div>
      ) : (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            NPU / CPU are measured against free system memory; iGPU shares system
            memory, dGPU is measured against free VRAM. "Min. required" is weights
            plus the output layer (the window = 0 lower bound), excluding runtime
            overhead; the figures are estimates.
          </Text>

          {anyOverBudget && (
            <Alert
              type="warning"
              showIcon
              icon={<WarningTwoTone twoToneColor={COLORS.red} />}
              message="Some configurations need more memory than is free now"
              description="Configurations in red may fail or trigger swapping for lack of memory. You can still choose to run."
            />
          )}

          {perModel.map(({ item, rows }) => (
            <div key={item.model.id}>
              <Text strong>{item.model.id}</Text>
              {item.model.memory ? (
                <Table<ConfigRow>
                  size="small"
                  style={{ marginTop: 6 }}
                  columns={COLUMNS}
                  dataSource={rows}
                  pagination={false}
                />
              ) : (
                <div style={{ marginTop: 6 }}>
                  <Text type="secondary">
                    No memory data yet — refresh the model list to fetch weight / KV-cache info.
                  </Text>
                </div>
              )}
            </div>
          ))}
        </Space>
      )}
    </Modal>
  )
}
