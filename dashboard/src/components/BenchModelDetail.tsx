// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
// Model details and per-model benchmark settings for the drawer opened by a tile.

import React from 'react'
import { Alert, Button, Card, Checkbox, Descriptions, Divider, Empty, Input, Select, Space, Tag, Tooltip, Typography } from 'antd'
import { DeleteOutlined, ToolOutlined } from '@ant-design/icons'

import type { BenchDevice, BenchModel, BenchPrecision } from '../api/types'
import { formatBytes } from '../utils/benchMetrics'

const { Text, Link } = Typography

const PRECISIONS: BenchPrecision[] = ['fp16', 'int8', 'int4']

// Why a delete is not on offer. The server refuses one while a job holds the
// slot -- a download or a run is reading exactly these files -- so the control
// says so rather than failing on the click.
const WHILE_BUSY = 'Weights cannot be removed while a benchmark job is running'

// Keep the reported model details and editable settings aligned in one-column rows.
const DESCRIPTION_PROPS = {
  size: 'small' as const,
  column: 1,
  styles: {
    label: { width: 150, whiteSpace: 'nowrap' as const, paddingBottom: 14 },
    content: { paddingBottom: 14 },
  },
}

/** A heading for a group of model facts or settings. */
function SectionHead({ children, first }: { children: React.ReactNode; first?: boolean }) {
  return (
    <Divider orientation="left" plain style={{ margin: first ? '0 0 12px' : '4px 0 12px' }}>
      <Text type="secondary" style={{ fontSize: 12, textTransform: 'uppercase' }}>
        {children}
      </Text>
    </Divider>
  )
}

const DEVICES: { value: BenchDevice; hint: string }[] = [
  { value: 'cpu', hint: 'Also the baseline the report’s speedup columns are measured against' },
  { value: 'gpu', hint: 'Integrated or discrete, whichever OpenVINO resolves GPU to' },
  { value: 'npu', hint: 'Not every quantisation recipe compiles for the NPU' },
]

/** Return selected precisions missing from at least one model. */
export function missingPrecisions(
  models: BenchModel[],
  precisions: BenchPrecision[],
): BenchPrecision[] {
  if (!models.length || !precisions.length) return precisions
  return precisions.filter((p) => !models.every((model) => model.local[p]))
}

/** Return selected precisions supported by a model; unknown variants allow all. */
export function applicablePrecisions(
  model: BenchModel,
  precisions: BenchPrecision[],
): BenchPrecision[] {
  if (!model.variants.length) return precisions
  const offered = new Set(model.precisions)
  return precisions.filter((p) => offered.has(p))
}

/**
 * One model's run settings.
 *
 * All four are per model. `precisions` and `args` reach the pipeline on that
 * model's own entry in the run request; `devices` and `ov` are process-wide in
 * the pipeline, so the server splits a request whose models disagree about them
 * into groups and runs the groups in sequence (benchmark/service/runner.py,
 * run_groups).
 */
export interface ModelParams {
  precisions: BenchPrecision[]
  devices: BenchDevice[]
  // The OpenVINO version a benchmark runs against; only benchmarks use it, a
  // download ignores it.
  ov: string
  // Free-form extra CLI arguments appended to every benchmark run_case for this
  // model; a download ignores them.
  args: string
}

interface Props {
  model: BenchModel | null
  params: ModelParams
  onParamsChange: (next: ModelParams) => void
  // `installedOvs` are the versions already on disk -- the others have to be
  // installed first, which is what onInstallOv starts.
  ovChoices: string[]
  installedOvs: string[]
  onInstallOv: (version: string) => void
  ready: boolean
  // Keeps Install and the weight deletes from being pressed while something else
  // is running; nothing here starts a run any more.
  busy: boolean
  probing: boolean
  onOpenEnv: () => void
  // Offer to free the disk this model's downloaded weights are holding. An empty
  // `precisions` asks about all of them.
  onDeleteLocal: (model: BenchModel, precisions: BenchPrecision[]) => void
}

function VariantTags({ model }: { model: BenchModel }) {
  if (!model.variants.length) {
    return (
      <Text type="secondary">
        unknown — refresh the model list to pick up conversion details
      </Text>
    )
  }
  return (
    <Space size={[4, 4]} wrap>
      {model.variants.map((variant) => (
        <Tooltip key={variant.repo} title={variant.repo}>
          <Tag style={{ marginInlineEnd: 0 }}>
            {variant.precision ?? variant.repo.split('/').pop()}
          </Tag>
        </Tooltip>
      ))}
    </Space>
  )
}

export default function BenchModelDetail({
  model,
  params,
  onParamsChange,
  ovChoices,
  installedOvs,
  onInstallOv,
  ready,
  busy,
  probing,
  onOpenEnv,
  onDeleteLocal,
}: Props) {
  const { precisions, devices, ov, args } = params
  const onPrecisionsChange = (value: BenchPrecision[]) =>
    onParamsChange({ ...params, precisions: value })
  const onDevicesChange = (value: BenchDevice[]) =>
    onParamsChange({ ...params, devices: value })
  const onOvChange = (value: string) => onParamsChange({ ...params, ov: value })
  const onArgsChange = (value: string) => onParamsChange({ ...params, args: value })

  if (!model) {
    return (
      <Card size="small">
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="Pick a model on the left to see what it offers and download it."
        />
      </Card>
    )
  }

  const offered = new Set(model.precisions)
  const applicable = applicablePrecisions(model, precisions)
  const ovInstalled = installedOvs.includes(ov.trim())

  return (
    <Card
      size="small"
      title={
        <Space size={8} wrap>
          <Text strong>{model.id}</Text>
          {model.task && <Tag color="blue">{model.task}</Tag>}
          {model.downloaded && <Tag color="success">downloaded</Tag>}
        </Space>
      }
      extra={
        <Link href={`https://huggingface.co/${model.id}`} target="_blank" rel="noreferrer">
          HuggingFace
        </Link>
      }
    >
      <SectionHead first>What this model is</SectionHead>
      <Descriptions {...DESCRIPTION_PROPS}>
        <Descriptions.Item label="OpenVINO variants">
          <VariantTags model={model} />
        </Descriptions.Item>
        <Descriptions.Item label="On this machine">
          {model.downloaded ? (
            <Space size={[4, 4]} wrap>
              {PRECISIONS.filter((precision) => model.local[precision]).map((precision) => (
                <Tooltip key={precision} title={busy ? WHILE_BUSY : 'Delete these weights'}>
                  <Tag
                    color="success"
                    style={{ marginInlineEnd: 0 }}
                    closable={!busy}
                    closeIcon={<DeleteOutlined />}
                    onClose={(e: React.MouseEvent<HTMLElement>) => {
                      e.preventDefault()
                      onDeleteLocal(model, [precision])
                    }}
                  >
                    {precision} · {formatBytes(model.local_bytes[precision])}
                  </Tag>
                </Tooltip>
              ))}
              {Object.keys(model.local_bytes).length > 1 && (
                <Button
                  type="link"
                  size="small"
                  danger
                  disabled={busy}
                  style={{ padding: 0, height: 'auto', fontSize: 12 }}
                  onClick={() => onDeleteLocal(model, [])}
                >
                  delete all
                </Button>
              )}
            </Space>
          ) : (
            <Text type="secondary">nothing downloaded yet</Text>
          )}
        </Descriptions.Item>
        <Descriptions.Item label="Popularity">
          <Text type="secondary">
            {model.downloads.toLocaleString()} downloads · {model.likes} likes
          </Text>
        </Descriptions.Item>
        <Descriptions.Item label="Updated">
          <Text type="secondary">{model.last_modified?.slice(0, 10) ?? '-'}</Text>
        </Descriptions.Item>
      </Descriptions>

      <SectionHead>How it will be run</SectionHead>
      <Descriptions {...DESCRIPTION_PROPS}>
        <Descriptions.Item label="Precision">
          <Checkbox.Group
              value={applicable}
              onChange={(value) =>
                onPrecisionsChange(
                  PRECISIONS.filter((p) => (value as BenchPrecision[]).includes(p)),
                )
              }
              options={PRECISIONS.map((precision) => ({
                label: (
                  <Space size={4}>
                    {precision}
                    {model.local[precision] && <Tag color="success" style={{ marginInlineEnd: 0 }}>ready</Tag>}
                  </Space>
                ),
                value: precision,
                disabled: model.variants.length > 0 && !offered.has(precision),
              }))}
            />
        </Descriptions.Item>

        <Descriptions.Item label="Device">
          <Checkbox.Group
            value={devices}
            onChange={(value) => onDevicesChange(value as BenchDevice[])}
            options={DEVICES.map(({ value, hint }) => ({
              label: <Tooltip title={hint}>{value.toUpperCase()}</Tooltip>,
              value,
            }))}
          />
        </Descriptions.Item>

        <Descriptions.Item
          label={
            <Tooltip title="The build/download stage never uses OpenVINO; only the benchmark does.">
              OpenVINO
            </Tooltip>
          }
        >
          <Space size={6}>
            <Select
              size="small"
              style={{ width: 180 }}
              value={ov || undefined}
              onChange={onOvChange}
              showSearch
              placeholder="pick a version"
              options={ovChoices.map((v) => ({
                value: v,
                label: (
                  <Space size={6}>
                    {v}
                    {installedOvs.includes(v) ? (
                      <Tag color="success" style={{ marginInlineEnd: 0 }}>installed</Tag>
                    ) : (
                      <Tag style={{ marginInlineEnd: 0 }}>not installed</Tag>
                    )}
                  </Space>
                ),
              }))}
            />
            {/* Next to the box that says the runtime is missing, rather than in
                a drawer the user has to be told to open. */}
            {!!ov && !ovInstalled && (
              <Tooltip
                title={`Install the OpenVINO ${ov} runtime, so this version can be benchmarked`}
              >
                <Button
                  // Primary: this is the next step, and Run stays greyed out
                  // until it is done.
                  type="primary"
                  size="small"
                  icon={<ToolOutlined />}
                  disabled={busy}
                  onClick={() => onInstallOv(ov.trim())}
                >
                  Install
                </Button>
              </Tooltip>
            )}
          </Space>
        </Descriptions.Item>

        {/* Free-form extra arguments, forwarded to the benchmark command at the
            end of each run_case. A way to pass flags the page does not model on
            its own (e.g. --num-warmup 2); only the benchmark uses them. */}
        <Descriptions.Item
          label={
            <Tooltip title="Appended to the benchmark command for every case; the download stage ignores them.">
              Extra args
            </Tooltip>
          }
        >
          <Input
            size="small"
            style={{ width: 240 }}
            value={args}
            onChange={(e) => onArgsChange(e.target.value)}
            placeholder="e.g. --num-warmup 2"
            allowClear
          />
        </Descriptions.Item>
      </Descriptions>

      {/* No Download or Run here.
          This pane used to be the only place either could be pressed, and it
          carried its own pair for the one model it showed. The tile strip's pair
          now acts on whichever tiles are ticked -- which is the same act, for
          one model or six -- and a second pair behind a drawer would be two
          controls doing one thing, differing only in how many models they
          happened to include. What is left here is what the numbers will be
          measured with; the buttons are downstairs. Cancel went to the toolbar
          for the same reason: stopping the machine is not about a model. */}

      {!ready && !probing && (
        <Alert
          style={{ marginTop: 12 }}
          type="warning"
          showIcon
          message="The benchmark environment is not installed"
          description={<a onClick={onOpenEnv}>Install it before downloading or benchmarking.</a>}
        />
      )}
    </Card>
  )
}
