// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// The benchmark environment: what is installed, where, and the button that
// installs it.
//
// A drawer rather than a panel on the page. Installing is a once-per-machine
// act -- it clones a repository and builds the base venv plus one OpenVINO
// runtime -- so after the first few minutes of its life this content is a status
// line, and the page belongs to the models and their results instead. Not once
// ever, though: a further OpenVINO version is installed from the same button,
// which is why the version is picked here rather than implied.

import React from 'react'
import { Alert, Button, Descriptions, Divider, Drawer, Select, Space, Table, Tag, Tooltip, Typography } from 'antd'
import { ReloadOutlined, ToolOutlined } from '@ant-design/icons'

import type { BenchEnvData } from '../api/types'

const { Text, Paragraph } = Typography

interface Props {
  open: boolean
  onClose: () => void
  env: BenchEnvData | null
  busy: boolean
  installing: boolean
  onRefresh: () => void
  // The version the Models tab is set to, and everything installable. Opening
  // on the former installs the runtime the user is about to benchmark against.
  ov: string
  ovChoices: string[]
  onSetup: (version: string) => void
}

export function versionSummary(versions: Record<string, string | null>): string {
  const parts = Object.entries(versions)
    .filter(([, v]) => v)
    .map(([k, v]) => `${k} ${v}`)
  return parts.length ? parts.join(' · ') : 'unknown'
}

/**
 * A field whose value gets its own line.
 *
 * Absolute paths and version lists do not fit beside a label in a drawer, and
 * squeezing them there is what made this read as cramped: the label took a third
 * of the width and the path ellipsised away the part that identifies it.
 */
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ marginBottom: 4 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {label}
        </Text>
      </div>
      {children}
    </div>
  )
}

/** An absolute path: one line, ellipsised, full value on hover and on copy. */
function PathValue({ value }: { value?: string }) {
  if (!value) return <Text type="secondary">-</Text>
  return (
    <Text code copyable={{ text: value }} ellipsis={{ tooltip: value }} style={{ maxWidth: '100%' }}>
      {value}
    </Text>
  )
}

/**
 * The environment's state as a single tag.
 *
 * "Can a run start" and "what is the job doing" are different questions that
 * looked contradictory while a setup ran (Not installed / running). Installing
 * and Checking say so explicitly instead.
 *
 * "No runtime" is the state between the two halves of the installation: models
 * can be browsed and downloaded, but nothing can be benchmarked yet. Reading
 * Ready there would be a ready environment with every Run button greyed out.
 */
export function EnvStatusTag({ env, installing }: { env: BenchEnvData | null; installing: boolean }) {
  if (installing) return <Tag color="processing">Installing</Tag>
  if (env?.probing) return <Tag color="processing">Checking</Tag>
  if (env?.ready) {
    return env.ov_versions.length
      ? <Tag color="success">Ready</Tag>
      : <Tag color="warning">No runtime</Tag>
  }
  if (env?.venv_exists) return <Tag color="warning">Incomplete</Tag>
  return <Tag>Not installed</Tag>
}

/**
 * The installed OpenVINO versions and the exact openvino package inside each.
 * One row per built runtime: the version on the left, the resolved openvino
 * package version on the right. Empty until a runtime has been installed (the
 * action at the top of this drawer), so a fresh environment shows no rows here.
 */
function OvReference({ detail }: { detail?: BenchEnvData['ov_versions_detail'] }) {
  const versions = detail ?? []

  if (!versions.length) {
    return (
      <Field label="OpenVINO versions">
        <Text type="secondary" style={{ fontSize: 12 }}>
          No OpenVINO runtime installed yet. Pick a version above and install it;
          the packages it brought in then appear here.
        </Text>
      </Field>
    )
  }

  const rows = versions.map((v) => ({
    key: v.version,
    version: v.version,
    openvino: v.packages.openvino ?? null,
  }))

  return (
    <Field label="OpenVINO versions">
      <Table
        size="small"
        pagination={false}
        dataSource={rows}
        columns={[
          {
            title: 'Version',
            dataIndex: 'version',
            key: 'version',
          },
          {
            title: 'openvino',
            dataIndex: 'openvino',
            key: 'openvino',
            render: (v: string | null) =>
              v ? <Text>{v}</Text> : <Text type="secondary">-</Text>,
          },
        ]}
      />
    </Field>
  )
}

export default function BenchEnvDrawer({
  open,
  onClose,
  env,
  busy,
  installing,
  onRefresh,
  ov,
  ovChoices,
  onSetup,
}: Props) {
  const ready = !!env?.ready
  const installedOvs = env?.ov_versions ?? []
  // Which runtime to install: follows the Models tab until picked here, then
  // stays put rather than being moved by a change on the tab.
  const [target, setTarget] = React.useState<string | undefined>(undefined)
  const version = target ?? ov ?? ''
  const versionInstalled = installedOvs.includes(version)

  return (
    <Drawer
      title={
        <Space>
          <ToolOutlined />
          Benchmark environment
        </Space>
      }
      open={open}
      onClose={onClose}
      width={600}
    >
      {/* The actions sit in the body rather than the drawer header: the header
          also carries the close button, and four controls in that strip left no
          room for the one that starts an installation. */}
      <Space style={{ width: '100%', justifyContent: 'flex-end' }} size={8} wrap>
        <Tooltip title="Re-read the environment">
          <Button icon={<ReloadOutlined />} size="small" onClick={onRefresh} />
        </Tooltip>
        <Tooltip title="The OpenVINO runtime to install alongside the base environment">
          <Select
            size="small"
            style={{ minWidth: 170 }}
            value={version || undefined}
            onChange={setTarget}
            showSearch
            placeholder="OpenVINO version"
            disabled={busy}
            options={ovChoices.map((v) => ({
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
          />
        </Tooltip>
        <Button
          type={ready && versionInstalled ? 'default' : 'primary'}
          size="small"
          icon={<ToolOutlined />}
          // Also while probing: until the venv has been read, this button does
          // not yet know whether it would install or rebuild.
          disabled={busy || !!env?.probing || !version}
          // Never force from here: nothing left to install comes back as a
          // conflict, which the page turns into "rebuild anyway?". Moving a
          // working environment aside should cost that confirmation.
          onClick={() => onSetup(version)}
        >
          {ready && versionInstalled ? `Rebuild OV ${version}` : `Install OV ${version}`}
        </Button>
      </Space>
      <Divider style={{ margin: '12px 0' }} />

      <Descriptions size="small" column={1} styles={{ label: { whiteSpace: 'nowrap' } }}>
        <Descriptions.Item label="Status">
          <EnvStatusTag env={env} installing={installing} />
        </Descriptions.Item>
        <Descriptions.Item label="Downloaded models">{env?.model_count ?? 0}</Descriptions.Item>
      </Descriptions>

      <div style={{ marginTop: 12 }}>
        <OvReference detail={env?.ov_versions_detail} />
        <Field label="Runtime root">
          <PathValue value={env?.env_root} />
        </Field>
        <Field label="Models directory">
          <PathValue value={env?.models_dir} />
        </Field>
      </div>

      {/* Not while probing: until the server has read the venv, "not installed"
          is a guess, and flashing this banner on every page load is noise. */}
      {!busy && !env?.probing && (!ready || (!!version && !versionInstalled)) && (
        <Alert
          style={{ marginTop: 12 }}
          type="info"
          showIcon
          message={
            ready
              ? `The OpenVINO ${version} runtime is not installed`
              : 'The benchmark environment is not installed yet'
          }
          description={
            'Installing clones the OpenVINO GenAI repository, builds the base ' +
            'environment that lists and downloads models, and then the chosen ' +
            'OpenVINO runtime with each transformers release the pipeline picks ' +
            'between. It needs internet access and a few GB of disk; a further ' +
            'version costs only the wheels that differ from the ones already here. ' +
            'It happens here rather than inside a measured run.'
          }
        />
      )}
      <Paragraph type="secondary" style={{ marginTop: 16, marginBottom: 0, fontSize: 12 }}>
        Setup and benchmark runs share a single execution slot, so only one of them
        can be in progress at a time.
      </Paragraph>
    </Drawer>
  )
}
