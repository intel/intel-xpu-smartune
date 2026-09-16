// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// Giving back the disk a model's downloaded weights are holding.
//
// Weights are the one thing this tab puts on disk at any scale: one directory
// per model per precision, hundreds of megabytes to a couple of gigabytes each,
// and until now nothing ever removed them -- a machine that had swept a handful
// of models was tens of gigabytes down with no way back short of an ssh session.
//
// A choice of precisions rather than a yes/no on the model, because that is how
// they arrived: three independent downloads of very different sizes, and "keep
// int4, drop fp16" is the ordinary request. Each one carries what it is holding,
// so the decision can be made here rather than by adding up `du` output in
// another window.
//
// One dialog for both entry points -- the row in the model list and the tags in
// the detail pane -- rather than a Modal.confirm at each. The two differ only in
// what they arrive preselected with.

import React, { useEffect, useMemo, useState } from 'react'
import { Alert, Checkbox, Modal, Space, Typography } from 'antd'

import type { BenchModel, BenchPrecision } from '../api/types'
import { formatBytes } from '../utils/benchMetrics'

const { Text } = Typography

// Display order, as everywhere else on this tab (largest first, which here is
// also most-worth-deleting first).
const PRECISIONS: BenchPrecision[] = ['fp16', 'int8', 'int4']

/** The precisions of `model` that are actually on disk, in display order. */
export function downloadedPrecisions(model: BenchModel | null): BenchPrecision[] {
  if (!model) return []
  return PRECISIONS.filter((p) => model.local[p])
}

interface Props {
  /** The model to clean up, or null when the dialog is closed. */
  model: BenchModel | null
  /**
   * What to tick on open. Empty means everything on disk -- the list row asks
   * about the model as a whole, while a tag in the detail pane asks about the
   * one precision it names.
   */
  preselect?: BenchPrecision[]
  /** True while a job holds the slot: the server refuses a delete then, so so does this. */
  busy: boolean
  onCancel: () => void
  onConfirm: (model: BenchModel, precisions: BenchPrecision[]) => Promise<void>
}

export default function BenchModelDeleteModal({
  model,
  preselect,
  busy,
  onCancel,
  onConfirm,
}: Props) {
  const [chosen, setChosen] = useState<BenchPrecision[]>([])
  const [working, setWorking] = useState(false)

  const available = useMemo(() => downloadedPrecisions(model), [model])

  // Reset on every open, not on mount: the dialog outlives one model, and a
  // selection carried over from the last one would be a different question
  // answered in advance.
  useEffect(() => {
    if (!model) return
    const wanted = (preselect ?? []).filter((p) => available.includes(p))
    setChosen(wanted.length ? wanted : available)
  }, [model, preselect, available])

  const total = chosen.reduce((sum, p) => sum + (model?.local_bytes[p] ?? 0), 0)
  // Every precision on disk, i.e. the model is about to leave the machine
  // entirely. Worth saying, because "delete int4" and "delete this model" look
  // identical up to which boxes are ticked.
  const whole = chosen.length > 0 && chosen.length === available.length

  const confirm = async () => {
    if (!model || !chosen.length) return
    setWorking(true)
    try {
      await onConfirm(model, chosen)
    } finally {
      // The parent closes the dialog on success; on failure it stays open with
      // the selection intact, so the same delete can be retried.
      setWorking(false)
    }
  }

  return (
    <Modal
      open={!!model}
      onCancel={onCancel}
      title={model ? `Delete downloaded weights — ${model.id}` : 'Delete downloaded weights'}
      width={520}
      okText={total > 0 ? `Delete · frees ${formatBytes(total)}` : 'Delete'}
      okButtonProps={{ danger: true, disabled: !chosen.length || busy, loading: working }}
      cancelButtonProps={{ disabled: working }}
      onOk={() => void confirm()}
    >
      <Space direction="vertical" size={12} style={{ width: '100%', marginTop: 8 }}>
        <Text type="secondary">
          Which conversions to remove from this machine. The model can be
          downloaded again at any time.
        </Text>

        <Checkbox.Group
          value={chosen}
          onChange={(value) =>
            // Rebuilt in canonical order so the list does not depend on click
            // order -- it is read back in the button label and the message.
            setChosen(PRECISIONS.filter((p) => (value as BenchPrecision[]).includes(p)))
          }
          style={{ width: '100%' }}
        >
          <Space direction="vertical" size={6} style={{ width: '100%' }}>
            {available.map((precision) => (
              <Checkbox key={precision} value={precision} disabled={busy}>
                <Space size={8}>
                  <Text>{precision}</Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {formatBytes(model?.local_bytes[precision])}
                  </Text>
                </Space>
              </Checkbox>
            ))}
          </Space>
        </Checkbox.Group>

        {whole && available.length > 1 && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            That is everything this model has on disk — it will go back to being
            a model that has to be downloaded before it can be run.
          </Text>
        )}

        {busy && (
          <Alert
            type="warning"
            showIcon
            message="A benchmark job is running"
            description="Weights cannot be removed while a download or a run may be reading them. Wait for it to finish, or cancel it first."
          />
        )}
      </Space>
    </Modal>
  )
}
