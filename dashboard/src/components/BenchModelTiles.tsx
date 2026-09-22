// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
// Selected benchmark models, their per-model settings, and batch actions.
// Download and Run are separate so measurement starts only with ready artifacts.

import React, { useCallback, useEffect, useRef, useState } from 'react'
import { Badge, Button, Card, Checkbox, Empty, Space, Tag, Tooltip, Typography } from 'antd'
import {
  CheckCircleTwoTone,
  CloseOutlined,
  CloudDownloadOutlined,
  LeftOutlined,
  PlayCircleOutlined,
  RightOutlined,
  SettingOutlined,
} from '@ant-design/icons'

import type { BenchModel, BenchPrecision } from '../api/types'
import { COLORS } from '../styles/theme'
import { applicablePrecisions, missingPrecisions, type ModelParams } from './BenchModelDetail'

const { Text } = Typography

const TILE_WIDTH = 268

interface Props {
  /** Every model with a tile, in list order. */
  ids: string[]
  /** The cached list, for the facts a tile shows. A ticked id it no longer holds gets a bare tile. */
  models: BenchModel[]
  /** Each tile's settings, keyed by model id. */
  params: Record<string, ModelParams>
  /** Which tiles the two buttons act on. */
  checked: string[]
  onCheckedChange: (ids: string[]) => void
  /** Open the drawer on one model. */
  onOpen: (id: string) => void
  /** Remove a tile — the same as unticking the model on the left. */
  onClose: (id: string) => void
  installedOvs: string[]
  ready: boolean
  busy: boolean
  starting: boolean
  onDownload: (ids: string[]) => void
  /** Open the run review -- the memory check and the exact commands, a page per
   *  model -- for the ticked tiles. The run starts from there, not from here. */
  onRun: (ids: string[]) => void
}

/** What a tile is missing before it can be benchmarked, in the order it matters. */
function blockingReason(
  model: BenchModel | null,
  params: ModelParams,
  installedOvs: string[],
): string | null {
  const applicable = model ? applicablePrecisions(model, params.precisions) : params.precisions
  if (!applicable.length) {
    return params.precisions.length
      ? `not published in ${params.precisions.join(' or ')}`
      : 'no precision selected'
  }
  const missing = model ? missingPrecisions([model], applicable) : applicable
  if (missing.length) return `${missing.join(', ')} not downloaded`
  if (!params.devices.length) return 'no device selected'
  if (!params.ov) return 'no OpenVINO version selected'
  if (!installedOvs.includes(params.ov.trim())) return `OpenVINO ${params.ov} not installed`
  return null
}

function PrecisionTags({ model, params }: { model: BenchModel | null; params: ModelParams }) {
  if (!params.precisions.length) {
    return <Text type="secondary" style={{ fontSize: 12 }}>no precision</Text>
  }
  // Ticked precisions, each saying whether this model offers it and whether the
  // weights are here. Three states, because they need three different actions:
  // pick another precision, press Download, or press Run.
  return (
    <Space size={[4, 4]} wrap>
      {params.precisions.map((precision: BenchPrecision) => {
        const offered = !model || !model.variants.length || model.precisions.includes(precision)
        const local = !!model?.local[precision]
        return (
          <Tooltip
            key={precision}
            title={
              !offered
                ? 'This model is not published in this precision'
                : local
                  ? 'Downloaded and ready to benchmark'
                  : 'Not downloaded yet'
            }
          >
            <Tag
              color={!offered ? 'default' : local ? 'success' : 'warning'}
              style={{ marginInlineEnd: 0, opacity: offered ? 1 : 0.5 }}
            >
              {precision}
              {local && ' ✓'}
            </Tag>
          </Tooltip>
        )
      })}
    </Space>
  )
}

function ModelTile({
  id,
  model,
  params,
  checked,
  onToggle,
  onOpen,
  onClose,
  installedOvs,
}: {
  id: string
  model: BenchModel | null
  params: ModelParams
  checked: boolean
  onToggle: (next: boolean) => void
  onOpen: () => void
  onClose: () => void
  installedOvs: string[]
}) {
  const blocked = blockingReason(model, params, installedOvs)
  const ovInstalled = !!params.ov && installedOvs.includes(params.ov.trim())
  return (
    <Card
      size="small"
      style={{
        width: TILE_WIDTH,
        flex: `0 0 ${TILE_WIDTH}px`,
        // The ticked tiles are the ones the buttons below act on, so they are
        // the ones drawn as selected rather than merely present.
        borderColor: checked ? COLORS.accent : COLORS.border,
        borderWidth: checked ? 2 : 1,
      }}
      styles={{ body: { padding: 10, cursor: 'pointer' } }}
      onClick={onOpen}
    >
      <Space direction="vertical" size={6} style={{ width: '100%' }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 4 }}>
          {/* The repo's own name; the publisher is on the left and in the
              tooltip, and it costs a line here that the settings need. */}
          <Tooltip title={id}>
            <Text strong ellipsis style={{ flex: 1, minWidth: 0 }}>
              {id.split('/').pop()}
            </Text>
          </Tooltip>
          {model?.downloaded && (
            <Tooltip title="Some precision of this model is on this machine">
              <CheckCircleTwoTone twoToneColor="#52c41a" />
            </Tooltip>
          )}
          <Tooltip title="Remove from the selection">
            <Button
              type="text"
              size="small"
              icon={<CloseOutlined />}
              aria-label={`Remove ${id}`}
              onClick={(event) => {
                event.stopPropagation()
                onClose()
              }}
              style={{ marginTop: -2, marginRight: -6 }}
            />
          </Tooltip>
        </div>

        <PrecisionTags model={model} params={params} />

        <Space size={[4, 4]} wrap>
          {params.devices.length ? (
            params.devices.map((device) => (
              <Tag key={device} color="blue" style={{ marginInlineEnd: 0 }}>
                {device.toUpperCase()}
              </Tag>
            ))
          ) : (
            <Text type="secondary" style={{ fontSize: 12 }}>no device</Text>
          )}
          <Tooltip
            title={
              !params.ov
                ? 'No OpenVINO version selected'
                : ovInstalled
                  ? `Benchmarked against OpenVINO ${params.ov}`
                  : `The OpenVINO ${params.ov} runtime is not installed — open this model to install it`
            }
          >
            <Tag
              color={params.ov && ovInstalled ? 'geekblue' : 'warning'}
              style={{ marginInlineEnd: 0 }}
            >
              OV {params.ov || '—'}
            </Tag>
          </Tooltip>
        </Space>

        {/* Only when there are some: an empty "args: —" on every tile is a line
            of noise on the common case. */}
        {!!params.args.trim() && (
          <Tooltip title={params.args}>
            <Text code ellipsis style={{ fontSize: 11, width: '100%' }}>
              {params.args}
            </Text>
          </Tooltip>
        )}

        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <Tooltip title={blocked ? `Cannot be benchmarked yet: ${blocked}` : 'Ready to benchmark'}>
            <Badge
              status={blocked ? 'warning' : 'success'}
              text={
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {blocked ?? 'ready'}
                </Text>
              }
            />
          </Tooltip>
          <span style={{ flex: 1 }} />
          <Tooltip title="Open this model's settings">
            <Button
              type="text"
              size="small"
              icon={<SettingOutlined />}
              aria-label={`Settings for ${id}`}
              onClick={(event) => {
                event.stopPropagation()
                onOpen()
              }}
            />
          </Tooltip>
          {/* Bottom right, one per tile: which of them Download and Run below
              are about. */}
          <Tooltip title="Include this model when downloading or running">
            <Checkbox
              checked={checked}
              aria-label={`Include ${id}`}
              onClick={(event) => event.stopPropagation()}
              onChange={(event) => onToggle(event.target.checked)}
            />
          </Tooltip>
        </div>
      </Space>
    </Card>
  )
}

export default function BenchModelTiles({
  ids,
  models,
  params,
  checked,
  onCheckedChange,
  onOpen,
  onClose,
  installedOvs,
  ready,
  busy,
  starting,
  onDownload,
  onRun,
}: Props) {
  const strip = useRef<HTMLDivElement>(null)
  // Whether there is anything to scroll to on each side. Kept in state rather
  // than read during render: it depends on the laid-out width, which is not
  // known until after the browser has one.
  const [overflow, setOverflow] = useState({ left: false, right: false })

  const measure = useCallback(() => {
    const node = strip.current
    if (!node) return
    const max = node.scrollWidth - node.clientWidth
    setOverflow({
      left: node.scrollLeft > 1,
      // A pixel of slack: a fractional layout width otherwise leaves the arrow
      // live at the very end of the strip with nothing left to show.
      right: node.scrollLeft < max - 1,
    })
  }, [])

  useEffect(() => {
    measure()
    const node = strip.current
    if (!node || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(node)
    return () => observer.disconnect()
  }, [measure, ids.length])

  const scroll = (direction: -1 | 1) => {
    // By whole tiles, so a press lands on a tile boundary rather than halfway
    // through one.
    strip.current?.scrollBy({ left: direction * TILE_WIDTH * 2, behavior: 'smooth' })
  }

  const byId = new Map(models.map((model) => [model.id, model]))
  // What Download and Run will actually be sent. A ticked tile whose model
  // offers none of its precisions contributes no cases, so a batch of nothing
  // but those has no job in it.
  const runnable = checked.filter((id) => {
    const model = byId.get(id) ?? null
    const own = params[id]
    if (!own) return false
    return (model ? applicablePrecisions(model, own.precisions) : own.precisions).length > 0
  })
  const blockers = runnable
    .map((id) => ({ id, reason: blockingReason(byId.get(id) ?? null, params[id], installedOvs) }))
    .filter((entry) => entry.reason)
  // A download only needs precisions the model offers; everything else in
  // `blockers` is about measuring.
  const canDownload = ready && !busy && runnable.length > 0
  const canRun = canDownload && blockers.length === 0
  const missing = runnable.flatMap((id) => {
    const model = byId.get(id) ?? null
    const own = params[id]
    const applicable = model ? applicablePrecisions(model, own.precisions) : own.precisions
    return model ? missingPrecisions([model], applicable) : applicable
  })

  if (!ids.length) {
    return (
      <Card size="small">
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="Pick models on the left. Each one gets a tile here, with its own precision, device and OpenVINO version."
        />
      </Card>
    )
  }

  return (
    <Card size="small" styles={{ body: { padding: 8 } }}>
      <div style={{ display: 'flex', alignItems: 'stretch', gap: 4 }}>
        <Button
          type="text"
          icon={<LeftOutlined />}
          aria-label="Scroll the selection left"
          disabled={!overflow.left}
          onClick={() => scroll(-1)}
          style={{ height: 'auto', alignSelf: 'stretch' }}
        />
        <div
          ref={strip}
          onScroll={measure}
          style={{
            display: 'flex',
            gap: 8,
            overflowX: 'auto',
            // The strip is driven by the arrows; a scrollbar under the tiles
            // would be a second control for the same thing, on a row that is
            // only one tile tall.
            scrollbarWidth: 'none',
            flex: 1,
            minWidth: 0,
            padding: 2,
          }}
        >
          {ids.map((id) => (
            <ModelTile
              key={id}
              id={id}
              model={byId.get(id) ?? null}
              params={params[id]}
              checked={checked.includes(id)}
              onToggle={(next) =>
                onCheckedChange(
                  next
                    ? // Rebuilt in tile order, so the run request follows what
                      // is on screen rather than the order boxes were ticked.
                      ids.filter((other) => other === id || checked.includes(other))
                    : checked.filter((other) => other !== id),
                )
              }
              onOpen={() => onOpen(id)}
              onClose={() => onClose(id)}
              installedOvs={installedOvs}
            />
          ))}
        </div>
        <Button
          type="text"
          icon={<RightOutlined />}
          aria-label="Scroll the selection right"
          disabled={!overflow.right}
          onClick={() => scroll(1)}
          style={{ height: 'auto', alignSelf: 'stretch' }}
        />
      </div>

      {/* The two buttons, bottom right of the strip they act on -- the only
          place either job is started from. Neither runs anything directly:
          Download opens the disk check, Run opens the run review (the memory
          check and the exact commands, a page per model) and the run starts
          from there. */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          marginTop: 8,
          justifyContent: 'flex-end',
        }}
      >
        <Text type="secondary" style={{ fontSize: 12, marginRight: 'auto' }}>
          {checked.length === 0
            ? 'Tick a tile to download or benchmark it'
            : `${checked.length} of ${ids.length} ticked`}
        </Text>
        <Tooltip
          title={
            !ready
              ? 'The benchmark environment is not installed'
              : runnable.length === 0
                ? 'No ticked model is published in the precisions it is set to'
                : missing.length
                  ? `Fetch ${[...new Set(missing)].join(', ')} for the ticked models, as one job`
                  : 'Every ticked model already has its precisions; this fetches them again'
          }
        >
          <Button
            type={missing.length ? 'primary' : 'default'}
            icon={<CloudDownloadOutlined />}
            disabled={!canDownload}
            loading={starting}
            onClick={() => onDownload(runnable)}
          >
            Download
          </Button>
        </Tooltip>
        <Tooltip
          title={
            !ready
              ? 'The benchmark environment is not installed'
              : runnable.length === 0
                ? 'No ticked model is published in the precisions it is set to'
                : blockers.length
                  ? blockers
                      .map(({ id, reason }) => `${id.split('/').pop()}: ${reason}`)
                      .join('; ')
                  : `Review and benchmark ${runnable.length} model${runnable.length === 1 ? '' : 's'}: ` +
                    'the memory check and the exact commands, a page per model'
          }
        >
          <Button
            type="primary"
            icon={<PlayCircleOutlined />}
            disabled={!canRun}
            loading={starting}
            // Never the pipeline's `all` stage: see this file's header for why
            // a fetch must not happen inside a measured run.
            onClick={() => onRun(runnable)}
          >
            Run
          </Button>
        </Tooltip>
      </div>
    </Card>
  )
}
