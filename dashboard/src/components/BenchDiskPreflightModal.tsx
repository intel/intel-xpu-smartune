// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// The dialog shown before a Download: for every model × precision the fetch
// would write, it states how much disk that is against how much is free where
// the weights go.
//
// It is the counterpart of the pre-run memory check (BenchMemoryFitPanel, a
// page of the run review),
// and for the same reason. A download is the one thing this tab puts on disk at
// any scale -- one directory per conversion, hundreds of megabytes to tens of
// gigabytes -- and a volume that runs out partway through does not fail cleanly:
// it leaves a half-fetched directory that reads as downloaded, and takes the
// runtime tree (logs, run outputs, the HF cache) down with it.
//
// Like the memory check it is a warning, not a gate. The sizes are the repo's
// own file sizes when the hub has been asked and a parameter-count estimate
// otherwise, the volume may be freed up from elsewhere while the dialog is open,
// and the operator is the one who knows which -- so a download that does not fit
// is drawn red and still offered.
//
// The free-space reading comes from GET /bench/models/disk, passed in by the
// parent so this component stays a pure render of what it is handed.

import React, { useMemo } from 'react'
import { Alert, Modal, Space, Spin, Table, Tag, Typography } from 'antd'
import { WarningTwoTone } from '@ant-design/icons'

import type { BenchDiskData, BenchPrecision } from '../api/types'
import { COLORS } from '../styles/theme'
import BenchFitTag from './BenchFitTag'
import { formatBytes } from '../utils/benchMetrics'
import {
  buildDiskRows,
  diskTotals,
  diskVerdict,
  type DiskPreflightItem,
  type DiskRow,
} from '../utils/benchDisk'

const { Text } = Typography

interface Props {
  open: boolean
  items: DiskPreflightItem[]
  /** Free space where the weights land, or null when it could not be read. */
  disk: BenchDiskData | null
  /** True while the free-space reading is still in flight. */
  loading: boolean
  /**
   * True while the per-model weight sizes are still being fetched. The dialog is
   * usable without them -- the free space is already known -- so an unpriced row
   * says "reading" rather than "unknown", and the total says it may still grow.
   */
  pricing: boolean
  onDownload: () => void
  onCancel: () => void
}

function columnsFor(pricing: boolean) {
  return [
    {
      title: 'Model',
      dataIndex: 'model',
      key: 'model',
      // Merge the repeated model cells into one per model, so the table reads
      // as a list of models with their precisions under each.
      onCell: (row: DiskRow) => ({ rowSpan: row.modelRowSpan }),
      // A HuggingFace id is long and has no spaces to break at, so it is told
      // where it may wrap rather than being allowed to stretch the table.
      width: 240,
      render: (id: string) => <Text strong style={{ wordBreak: 'break-word' }}>{id}</Text>,
    },
    {
      title: 'Precision',
      dataIndex: 'precision',
      key: 'precision',
      render: (p: BenchPrecision) => <Tag>{p}</Tag>,
    },
    {
      title: 'State',
      key: 'state',
      render: (_: unknown, row: DiskRow) =>
        row.local ? (
          <Space size={4}>
            <Tag color="success" style={{ marginInlineEnd: 0 }}>
              on disk
            </Tag>
            <Text type="secondary">{formatBytes(row.localBytes)}</Text>
          </Space>
        ) : (
          <Tag color="warning" style={{ marginInlineEnd: 0 }}>
            to download
          </Tag>
        ),
    },
    {
      title: 'Disk needed',
      key: 'needed',
      render: (_: unknown, row: DiskRow) =>
        row.local ? (
          // Nothing: `hf download` into a populated directory re-uses what is
          // there, so a precision already fetched costs no further space.
          <Text type="secondary">—</Text>
        ) : row.needBytes == null ? (
          <Text type="secondary">{pricing ? 'reading…' : 'unknown'}</Text>
        ) : (
          <Text>{formatBytes(row.needBytes)}</Text>
        ),
    },
  ]
}

export default function BenchDiskPreflightModal({
  open,
  items,
  disk,
  loading,
  pricing,
  onDownload,
  onCancel,
}: Props) {
  const rows = useMemo(() => buildDiskRows(items), [items])
  const columns = useMemo(() => columnsFor(pricing), [pricing])

  const totals = useMemo(() => diskTotals(rows), [rows])

  const free = disk?.free_bytes ?? null
  const verdict = diskVerdict(totals.required, free)
  // What is left once the fetch has landed. Negative is possible and is the
  // point of the "full" verdict, so it is shown as 0 rather than as a minus.
  const after = free == null ? null : Math.max(0, free - totals.required)

  return (
    <Modal
      open={open}
      title="Pre-download disk check"
      width={800}
      okText="Download"
      cancelText="Cancel"
      onOk={onDownload}
      onCancel={onCancel}
      okButtonProps={{ danger: verdict === 'full' }}
      destroyOnClose
    >
      {loading ? (
        <div style={{ textAlign: 'center', padding: '32px 0' }}>
          <Spin tip="Reading free disk space…" />
        </div>
      ) : (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            A download writes the weight files of each requested precision into{' '}
            <Text code style={{ fontSize: 11 }}>{disk?.path ?? 'the models directory'}</Text>. The
            sizes are the OpenVINO repository's own file sizes where they are known and a
            parameter-count estimate otherwise; tokenizers and configs add a few megabytes on top.
          </Text>

          {verdict === 'full' && (
            <Alert
              type="error"
              showIcon
              icon={<WarningTwoTone twoToneColor={COLORS.red} />}
              message="Not enough free disk space for this download"
              description={
                `The download needs ${formatBytes(totals.required)} and only ` +
                `${formatBytes(free)} is free. It will likely fail partway through, ` +
                'leaving incomplete weights behind. Delete some downloaded models first, ' +
                'or download fewer precisions. You can still choose to continue.'
              }
            />
          )}

          {/* Only when something is actually being fetched: a re-download of
              precisions already on disk costs nothing, and a low-disk warning
              about it would be about the machine, not about this click. */}
          {verdict === 'tight' && totals.fetching > 0 && (
            <Alert
              type="warning"
              showIcon
              message="This download leaves very little free disk space"
              description={
                `${formatBytes(after)} would be left afterwards. Benchmark runs write logs, ` +
                'results and a HuggingFace cache onto this same volume.'
              }
            />
          )}

          {verdict === 'unknown' && (
            <Alert
              type="info"
              showIcon
              message="Free disk space could not be read"
              description="The download is not held up by that — only the space it needs is shown below."
            />
          )}

          {totals.unknown > 0 && !pricing && (
            <Alert
              type="info"
              showIcon
              message={`${totals.unknown} precision${totals.unknown === 1 ? '' : 's'} of unknown size`}
              description="The total below counts only what could be sized, so the real download is larger. This machine could not read those sizes from HuggingFace — refresh the model list to try again."
            />
          )}

          <Table<DiskRow>
            size="small"
            columns={columns}
            dataSource={rows}
            pagination={false}
          />


          {/* The one line the decision is actually made on, so it sits under the
              tables rather than being assembled from them by eye. */}
          <Space size={6} wrap>
            <Text strong>
              {totals.fetching === 0
                ? 'Nothing left to fetch — every requested precision is already on disk'
                : `Download needs ${formatBytes(totals.required)}${totals.unknown ? '+' : ''}`}
            </Text>
            <Text type="secondary">·</Text>
            <Text>
              free now{' '}
              <Text strong style={{ color: verdict === 'full' ? COLORS.red : undefined }}>
                {formatBytes(free)}
              </Text>
            </Text>
            {totals.fetching > 0 && after != null && (
              <>
                <Text type="secondary">·</Text>
                <Text type="secondary">{formatBytes(after)} left afterwards</Text>
              </>
            )}
            {/* One tag, not one per row: every row is being written to the same
                volume, so there is one verdict to give. Same words and colours
                as the memory check's per-row column. */}
            {totals.fetching > 0 && <BenchFitTag verdict={verdict} />}
            {pricing && (
              <Space size={4}>
                <Spin size="small" />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  still reading model sizes — the total may grow
                </Text>
              </Space>
            )}
          </Space>
        </Space>
      )}
    </Modal>
  )
}
