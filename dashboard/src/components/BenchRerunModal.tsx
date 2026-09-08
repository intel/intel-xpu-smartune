// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// "This has been benchmarked before" -- and what to do about it.
//
// Every run keeps its own results directory (benchmark/service/runner.py names
// it after the run), so re-running a configuration adds a measurement rather
// than replacing one. That is usually what a person wants: two numbers for the
// same test are a spread, and the Results tab shows them as one. It is not what
// they want when the earlier attempt was wrong -- a machine that was busy, a
// thermal outlier, a device that was not idle -- because the bad number goes on
// competing to be the best run of that test forever.
//
// So this asks, before the job starts, rather than making the user hunt for a
// way to remove results afterwards. Three answers, not two: keeping and
// discarding are both ordinary intentions, and neither is the dangerous one.

import React from 'react'
import { Alert, Button, Modal, Space, Tag, Typography } from 'antd'

import type { BenchMatrixRow } from '../api/types'
import { deviceLabel } from '../utils/benchMetrics'

const { Text } = Typography

/** One configuration that has already been measured, and the cases that hold it. */
export interface RerunConflict {
  /** The model as the browser knows it: a repo id. */
  modelId: string
  precision: string
  device: string
  /** Every existing measurement of this configuration, newest first. */
  rows: BenchMatrixRow[]
}

interface Props {
  open: boolean
  conflicts: RerunConflict[]
  /** True while the previous results are being removed. */
  working: boolean
  onKeep: () => void
  onDiscard: () => void
  onCancel: () => void
}

export default function BenchRerunModal({
  open,
  conflicts,
  working,
  onKeep,
  onDiscard,
  onCancel,
}: Props) {
  // Grouped by model, because that is how the list was chosen. A batch of eight
  // models at three precisions on three devices is 72 lines otherwise.
  const byModel = new Map<string, RerunConflict[]>()
  for (const conflict of conflicts) {
    byModel.set(conflict.modelId, [...(byModel.get(conflict.modelId) ?? []), conflict])
  }
  const cases = conflicts.reduce((total, conflict) => total + conflict.rows.length, 0)

  return (
    <Modal
      open={open}
      onCancel={onCancel}
      title="Some of this has already been benchmarked"
      width={620}
      footer={
        <Space>
          <Button onClick={onCancel} disabled={working}>
            Cancel
          </Button>
          <Button danger onClick={onDiscard} loading={working}>
            Delete the old results, then run
          </Button>
          <Button type="primary" onClick={onKeep} disabled={working}>
            Keep them and run again
          </Button>
        </Space>
      }
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Text type="secondary">
          {conflicts.length} of the configurations you asked for {conflicts.length === 1 ? 'has' : 'have'}{' '}
          been measured before, over {cases} case{cases === 1 ? '' : 's'} on disk.
        </Text>

        <div style={{ maxHeight: 260, overflow: 'auto' }}>
          {[...byModel.entries()].map(([modelId, entries]) => (
            <div key={modelId} style={{ marginBottom: 8 }}>
              <Text strong style={{ fontSize: 13 }}>{modelId}</Text>
              <div style={{ marginTop: 4 }}>
                <Space size={[6, 6]} wrap>
                  {entries.map((entry) => (
                    <Tag key={`${entry.precision}-${entry.device}`} style={{ marginInlineEnd: 0 }}>
                      {entry.precision} · {deviceLabel(entry.device)}
                      {entry.rows.length > 1 && ` · ${entry.rows.length}×`}
                    </Tag>
                  ))}
                </Space>
              </div>
            </div>
          ))}
        </div>

        {/* Said plainly, because one of the two buttons below removes files.
            Which files, and what survives, is exactly what the reader needs to
            choose -- not a warning that something is irreversible. */}
        <Alert
          type="info"
          showIcon
          message="What each choice does"
          description={
            <Space direction="vertical" size={2} style={{ fontSize: 12 }}>
              <span>
                <b>Keep them</b> — the new measurements are added alongside. The Results tab groups
                the repetitions of a test and shows the best of them.
              </span>
              <span>
                <b>Delete the old results</b> — the case directories listed above are removed from
                disk first. Nothing else in those runs is touched, and other devices and precisions
                stay as they are.
              </span>
            </Space>
          }
        />
      </Space>
    </Modal>
  )
}
