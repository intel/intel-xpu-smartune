// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// What is known about one model, and what it will be run as.
//
// The body of the drawer a tile opens (BenchModelDrawer). Two blocks, and the
// split is the point: above, facts the page can only report -- which precisions
// exist, how popular the conversion is, and, from the runtime IR directory,
// which of them are already on this machine; below, the choices the operator
// makes about this one model. All of it comes out of the cached model list, so
// opening a model costs nothing.
//
// Every control here edits ONE model's settings. They used to be one global set
// shared by whatever was selected, which could not express the thing the page is
// for -- this model as int4 on the NPU, that one as fp16 on the CPU -- so they
// are a per-model ModelParams now.
//
// Nothing here starts anything. Download and Run are the tile strip's, and act
// on whichever tiles are ticked (BenchModelTiles); this pane used to carry its
// own pair for the single model it showed, which behind a drawer would be a
// second set of buttons doing the same thing for a set of one. What the two
// stages mean is documented where those buttons are.
//
// One exception, and it is not a run: the OpenVINO box offers Install when the
// runtime it names is not on disk. That used to be built by the run that needed
// it, inside the measured window -- a multi-GB pip install with the sampler
// already recording -- so it is a job of its own now, offered next to the box
// that says it is missing rather than in a drawer the user has to be told to
// open.

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

// Every device the pipeline can sweep, in the order it sweeps them
// (benchmark/service/runner.py VALID_DEVICES). Not probed: what is installed on
// the host is a question the OpenVINO runtime answers inside the benchmark
// venv, and a device that is absent fails its cases visibly rather than
// silently disappearing from the choice.
// How every row in this pane is laid out: one item per row, label and value on
// one line with a colon between them. The label column is pinned rather than
// sized to its content so that the two blocks below -- what the model is, and
// how it will be run -- line up as one list, and the rows are given room to
// breathe: eight items packed at the default spacing filled the top third of
// the drawer and left the rest of it empty.
const DESCRIPTION_PROPS = {
  size: 'small' as const,
  column: 1,
  styles: {
    label: { width: 150, whiteSpace: 'nowrap' as const, paddingBottom: 14 },
    content: { paddingBottom: 14 },
  },
}

/**
 * A heading over one kind of row.
 *
 * The two kinds are worth separating: everything above the second one is a fact
 * about the model that the page can only report, and everything below it is a
 * choice the operator makes. Without the split they were eight rows of the same
 * weight, and which of them could be changed was something to find out by
 * clicking.
 */
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

/**
 * Which of the ticked `precisions` are not on disk yet for `models`.
 *
 * Run used to start the `all` stage and fetch whatever was missing on the way
 * in. It no longer does: a download of tens of GB would then sit inside quiet
 * mode, with SmarTune's monitoring suspended for the whole of it, and a heavy
 * fetch is the opposite of the quiet machine the run is being given. So a
 * download is its own job now, and Run is only unlocked once its weights are
 * there.
 *
 * `local` carries only the precisions the cached list knows about, so an unknown
 * one reads as missing -- the safe direction, since the worst case is a download
 * that turns out to be a no-op.
 */
export function missingPrecisions(
  models: BenchModel[],
  precisions: BenchPrecision[],
): BenchPrecision[] {
  if (!models.length || !precisions.length) return precisions
  return precisions.filter((p) => !models.every((model) => model.local[p]))
}

/**
 * Which of the ticked `precisions` this model can actually be asked for.
 *
 * The tick list is one global selection that follows the user from model to
 * model; what a model offers is not. A precision it has no conversion for is
 * drawn disabled -- but the tick itself survived, so the default int4 landed on
 * a model published only as fp16/int8 as a box that was checked, greyed, and
 * therefore impossible to clear. It counted as missing, Run stayed disabled,
 * and the only thing that would have unlocked it was a download that could
 * never succeed.
 *
 * Judging a model on the intersection is what that box was already saying.
 * `variants` empty means the cached list knows no conversion details at all (a
 * v1 cache, or repo names carrying no format suffix): there is nothing to
 * intersect against and the pipeline resolves the repo itself, so the selection
 * passes through -- the same condition under which nothing is disabled either.
 */
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
  // What this model can actually be asked for: the ticked precisions narrowed
  // to the ones it offers, not the raw tick list.
  const applicable = applicablePrecisions(model, precisions)
  // A runtime that is not on disk cannot be benchmarked against, so the box
  // that names it offers Install instead -- rather than the version being built
  // inside the measured run, which is what used to happen.
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
      {/* One item per row, label and value on the same line, separated by a
          colon. Two columns is what this used to be, and in a drawer's width
          that left every value -- each of them a list of tags -- wrapping in
          half the space it needed. */}
      <SectionHead first>What this model is</SectionHead>
      <Descriptions {...DESCRIPTION_PROPS}>
        <Descriptions.Item label="OpenVINO variants">
          <VariantTags model={model} />
        </Descriptions.Item>
        {/* What is on disk, what it costs, and the one control that gives it
            back. A conversion is hundreds of megabytes to a couple of gigabytes
            and nothing else on this tab removes one, so the tag that reports it
            is also where it is deleted -- with the precision it names already
            ticked in the dialog. */}
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
                      // The tag is a fact about the machine, not a filter chip:
                      // it goes away when the weights do, and not before.
                      e.preventDefault()
                      onDeleteLocal(model, [precision])
                    }}
                  >
                    {precision} · {formatBytes(model.local_bytes[precision])}
                  </Tag>
                </Tooltip>
              ))}
              {/* Only worth offering when there is more than one to take at
                  once; with a single conversion the tag above is the same act. */}
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

      {/* The settings, in the same two-column shape as the facts above: these
          four used to sit side by side across the full width of the page, which
          a drawer does not have. */}
      <SectionHead>How it will be run</SectionHead>
      <Descriptions {...DESCRIPTION_PROPS}>
        <Descriptions.Item label="Precision">
          <Checkbox.Group
              // What this model can offer, not the raw selection: a precision it
              // has no conversion for is drawn disabled, and disabled *and*
              // ticked is a box the user can neither act on nor clear. The
              // selection defaults to the last one made on another model, which
              // is how a tick that this model cannot honour gets here at all.
              value={applicable}
              onChange={(value) =>
                // Rebuilt in canonical order, so the state does not depend on
                // click order. Anything this model does not offer is dropped
                // rather than carried: the settings are its own now, and a
                // precision it is not published in is not a thing to remember
                // about it.
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
                // Only when the list actually knows the variants. A v1 cache (or
                // a model whose repo names carry no format suffix) knows none,
                // and disabling everything would make the model unusable rather
                // than honest -- the pipeline resolves the repo itself either way.
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

        {/* OpenVINO version to benchmark against, tagged with whether its
            runtime is installed. Only the benchmark uses it. */}
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
