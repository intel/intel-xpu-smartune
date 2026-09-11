// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// What is known about the selected model, and the actions that apply to it.
//
// Everything shown here comes out of the cached model list, so opening a model
// costs nothing: which precisions exist, how popular the conversion is, and --
// from the runtime IR directory -- which of them are already on this machine.
//
// "Download" is the pipeline's build stage. As wired in
// benchmark/templates/run_template.sh, that stage resolves each requested
// precision to the matching pre-converted OpenVINO repo and fetches it; nothing
// is converted locally. The API still calls it `build`, because the vendored
// pipeline also has real conversion routes behind the same switch.
//
// "Run" is the `benchmark` stage, and only that. The API also offers `all`
// (fetch first, then benchmark) and the page used to pick it automatically when
// something was missing, but a download that lands inside a measured run holds
// quiet mode -- and so suspends SmarTune's monitoring -- for however long tens
// of GB take to arrive, while itself being exactly the kind of load the run is
// supposed to be measured without. So the two are separate jobs and Run stays
// disabled until the weights are there. Everything else on this tab is
// benchmarking, which is why the button does not say so.

import React from 'react'
import { Alert, AutoComplete, Button, Card, Checkbox, Descriptions, Empty, Input, Space, Tag, Tooltip, Typography } from 'antd'
import {
  CloudDownloadOutlined,
  PlayCircleOutlined,
  StopOutlined,
} from '@ant-design/icons'

import type { BenchDevice, BenchModel, BenchPrecision, BenchStage } from '../api/types'

const { Text, Link } = Typography

const PRECISIONS: BenchPrecision[] = ['fp16', 'int8', 'int4']

// Every device the pipeline can sweep, in the order it sweeps them
// (benchmark/service/runner.py VALID_DEVICES). Not probed: what is installed on
// the host is a question the OpenVINO runtime answers inside the benchmark
// venv, and a device that is absent fails its cases visibly rather than
// silently disappearing from the choice.
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

interface Props {
  model: BenchModel | null
  precisions: BenchPrecision[]
  onPrecisionsChange: (value: BenchPrecision[]) => void
  devices: BenchDevice[]
  onDevicesChange: (value: BenchDevice[]) => void
  // The OpenVINO version a benchmark builds/runs against. Free text: a version
  // not in `ovOptions` is built on demand. Only benchmarks use it; a download
  // ignores it. `installedOvs` are the ones already built on disk, tagged in the
  // dropdown so the user can tell a quick pick from a build-on-demand.
  ov: string
  onOvChange: (value: string) => void
  ovOptions: string[]
  installedOvs: string[]
  // Free-form extra CLI arguments appended to every benchmark run_case. Applies
  // to the whole run, alongside the OpenVINO version; a download ignores it.
  args: string
  onArgsChange: (value: string) => void
  ready: boolean
  busy: boolean
  probing: boolean
  starting: boolean
  onRun: (stage: BenchStage, models: string[]) => void
  onCancel: () => void
  onOpenEnv: () => void
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
  precisions,
  onPrecisionsChange,
  devices,
  onDevicesChange,
  ov,
  onOvChange,
  ovOptions,
  installedOvs,
  args,
  onArgsChange,
  ready,
  busy,
  probing,
  starting,
  onRun,
  onCancel,
  onOpenEnv,
}: Props) {
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
  const knowsVariants = model.variants.length > 0
  // Everything below is about what this model can be asked for, which is the
  // ticked precisions narrowed to the ones it offers -- not the raw tick list.
  const applicable = applicablePrecisions(model, precisions)
  // A download needs precisions; a benchmark needs somewhere to run and an
  // OpenVINO version to run it (validated the same way the server does).
  const ovValid = /^\d+\.\d+\.\d+$/.test(ov.trim())
  const canDownload = ready && !busy && applicable.length > 0
  const missing = missingPrecisions([model], applicable)
  const canRun = canDownload && devices.length > 0 && ovValid && missing.length === 0
  const deviceSummary = devices.map((device) => device.toUpperCase()).join(', ')

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
      <Descriptions size="small" column={{ xs: 1, sm: 2 }} styles={{ label: { whiteSpace: 'nowrap' } }}>
        <Descriptions.Item label="OpenVINO variants">
          <VariantTags model={model} />
        </Descriptions.Item>
        <Descriptions.Item label="On this machine">
          {model.downloaded ? (
            <Space size={[4, 4]} wrap>
              {Object.entries(model.local)
                .filter(([, present]) => present)
                .map(([precision]) => (
                  <Tag key={precision} color="success" style={{ marginInlineEnd: 0 }}>
                    {precision}
                  </Tag>
                ))}
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

      {/* Precision and device are the two halves of "what to run": one picks
          the weights, the other picks what runs them. Side by side because the
          sweep is their product -- three precisions on two devices is six
          cases, and that is easier to see when both are in one line of sight. */}
      <div style={{ marginTop: 12, display: 'flex', gap: 32, flexWrap: 'wrap' }}>
        <div>
          <Text type="secondary">Precision</Text>
          <div style={{ marginTop: 6 }}>
            <Checkbox.Group
              // What this model can offer, not the raw selection: a precision it
              // has no conversion for is drawn disabled, and disabled *and*
              // ticked is a box the user can neither act on nor clear.
              value={applicable}
              onChange={(value) =>
                // The selection is shared by every model, so the ticks this one
                // cannot honour are carried through rather than dropped on the
                // way past -- the next model may well offer them. Rebuilt in
                // canonical order so the state does not depend on click order.
                onPrecisionsChange(
                  PRECISIONS.filter(
                    (p) =>
                      (value as BenchPrecision[]).includes(p) ||
                      (knowsVariants && !offered.has(p) && precisions.includes(p)),
                  ),
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
          </div>
        </div>

        <div>
          <Text type="secondary">Device</Text>
          <div style={{ marginTop: 6 }}>
            <Checkbox.Group
              value={devices}
              onChange={(value) => onDevicesChange(value as BenchDevice[])}
              options={DEVICES.map(({ value, hint }) => ({
                label: <Tooltip title={hint}>{value.toUpperCase()}</Tooltip>,
                value,
              }))}
            />
          </div>
        </div>

        {/* OpenVINO version to benchmark against. Free text: a version that is
            not already built is built on demand when the run starts. Only the
            benchmark uses it -- a download fetches weights and never touches
            OpenVINO. */}
        <div>
          <Tooltip title="The build/download stage never uses OpenVINO; only the benchmark does.">
            <Text type="secondary">OpenVINO</Text>
          </Tooltip>
          <div style={{ marginTop: 6 }}>
            <AutoComplete
              size="small"
              style={{ width: 180 }}
              value={ov}
              onChange={onOvChange}
              // Show every option regardless of the box's current value: it is
              // seeded with a version, and the default filter would then hide all
              // the other installed versions the user wants to pick from.
              filterOption={false}
              options={ovOptions.map((v) => ({
                value: v,
                label: (
                  <Space size={6}>
                    {v}
                    {installedOvs.includes(v) && (
                      <Tag color="success" style={{ marginInlineEnd: 0 }}>installed</Tag>
                    )}
                  </Space>
                ),
              }))}
              placeholder="e.g. 2026.2.0"
              status={ov && !ovValid ? 'error' : undefined}
            />
          </div>
        </div>

        {/* Free-form extra arguments, forwarded to the benchmark command at the
            end of each run_case. A way to pass flags the page does not model on
            its own (e.g. --num-warmup 2); only the benchmark uses them. */}
        <div>
          <Tooltip title="Appended to the benchmark command for every case; the download stage ignores them.">
            <Text type="secondary">Extra args</Text>
          </Tooltip>
          <div style={{ marginTop: 6 }}>
            <Input
              size="small"
              style={{ width: 220 }}
              value={args}
              onChange={(e) => onArgsChange(e.target.value)}
              placeholder="e.g. --num-warmup 2"
              allowClear
            />
          </div>
        </div>
      </div>

      {/* Download first, then Run: whichever is the next thing to do is the one
          drawn as primary, so the pair reads as a sequence rather than a
          choice. */}
      <Space style={{ marginTop: 16 }} wrap>
        <Tooltip
          title={
            !applicable.length
              ? precisions.length
                ? `This model is not published in ${precisions.join(' or ')} — tick a precision it offers`
                : 'Tick a precision to download'
              : missing.length
                ? `Fetch ${missing.join(', ')} — Run unlocks once they are on disk`
                : 'The ticked precisions are already here; this fetches them again'
          }
        >
          <Button
            type={missing.length ? 'primary' : 'default'}
            icon={<CloudDownloadOutlined />}
            // A download is about weights, not about where they will run: the
            // device selection does not gate it.
            disabled={!canDownload}
            loading={starting}
            onClick={() => onRun('build', [model.id])}
          >
            Download
          </Button>
        </Tooltip>
        <Tooltip
          title={
            !applicable.length
              ? precisions.length
                ? `This model is not published in ${precisions.join(' or ')} — tick a precision it offers`
                : 'Tick a precision to benchmark'
              : missing.length
                ? `Download ${missing.join(', ')} first`
                : devices.length === 0
                  ? 'Pick at least one device to benchmark on'
                  : !ovValid
                    ? 'Enter an OpenVINO version (e.g. 2026.2.0) to benchmark against'
                    : `Benchmark ${applicable.join(', ')} on ${deviceSummary} with OpenVINO ${ov}`
          }
        >
          <Button
            type="primary"
            icon={<PlayCircleOutlined />}
            disabled={!canRun}
            loading={starting}
            // Never `all`: fetching inside a measured run is what the split above
            // exists to prevent.
            onClick={() => onRun('benchmark', [model.id])}
          >
            Run
          </Button>
        </Tooltip>
        {busy && (
          <Button danger icon={<StopOutlined />} onClick={onCancel}>
            Cancel
          </Button>
        )}
      </Space>

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
