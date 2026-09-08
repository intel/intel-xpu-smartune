// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// The benchmark environment: what is installed, where, and the button that
// installs it.
//
// A drawer rather than a panel on the page. Installing is a once-per-machine
// act -- it clones two repositories, builds a venv and downloads tens of GB --
// so after the first hour of its life this content is a status line, and the
// page belongs to the models and their results instead.

import React from 'react'
import { Alert, Button, Descriptions, Divider, Drawer, Select, Space, Tag, Tooltip, Typography } from 'antd'
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
  onSetup: () => void
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
 */
export function EnvStatusTag({ env, installing }: { env: BenchEnvData | null; installing: boolean }) {
  if (installing) return <Tag color="processing">Installing</Tag>
  if (env?.probing) return <Tag color="processing">Checking</Tag>
  if (env?.ready) return <Tag color="success">Ready</Tag>
  if (env?.venv_exists) return <Tag color="warning">Incomplete</Tag>
  return <Tag>Not installed</Tag>
}

/**
 * The built OpenVINO versions and what is actually installed inside each. Pick a
 * version from the dropdown to see the exact package versions in its venv. Empty
 * until a version has been built (OpenVINO columns are built per-run on demand
 * from the Models tab), so a fresh environment shows no packages here.
 */
function OvReference({ detail }: { detail?: BenchEnvData['ov_versions_detail'] }) {
  const [selected, setSelected] = React.useState<string | undefined>(undefined)
  const versions = detail ?? []

  if (!versions.length) {
    return (
      <Field label="OpenVINO versions">
        <Text type="secondary" style={{ fontSize: 12 }}>
          No OpenVINO version built yet. Pick a version on the Models tab and run a
          benchmark; it is built on demand and then appears here.
        </Text>
      </Field>
    )
  }

  const active = versions.find((v) => v.version === selected) ?? versions[0]
  // Only packages that actually resolved a version -- a null means the venv does
  // not carry that package, which is not worth a row.
  const packages = Object.entries(active.packages).filter(([, v]) => v) as [string, string][]

  return (
    <Field label="OpenVINO versions">
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        <Select
          size="small"
          style={{ minWidth: 200 }}
          value={active.version}
          onChange={setSelected}
          options={versions.map((v) => ({ label: v.version, value: v.version }))}
        />
        <div>
          <Text type="secondary" style={{ fontSize: 12 }}>
            Packages of OV {active.version}
          </Text>
          <Space direction="vertical" size={4} style={{ width: '100%', marginTop: 4 }}>
            {packages.length ? (
              packages.map(([name, version]) => (
                <Tag key={name} style={{ marginInlineEnd: 0 }}>
                  {name} {version}
                </Tag>
              ))
            ) : (
              <Text type="secondary" style={{ fontSize: 12 }}>reading the venv ...</Text>
            )}
          </Space>
        </div>
      </Space>
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
  onSetup,
}: Props) {
  const ready = !!env?.ready

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
          also carries the close button, and three controls in that strip left no
          room for the one that costs an hour to press. */}
      <Space style={{ width: '100%', justifyContent: 'flex-end' }} size={8}>
        <Tooltip title="Re-read the environment">
          <Button icon={<ReloadOutlined />} size="small" onClick={onRefresh} />
        </Tooltip>
        <Button
          type={ready ? 'default' : 'primary'}
          size="small"
          icon={<ToolOutlined />}
          // Also while probing: until the venv has been read, this button does
          // not yet know whether it would install or rebuild.
          disabled={busy || !!env?.probing}
          onClick={onSetup}
        >
          {ready ? 'Rebuild' : 'Install'}
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
      {!ready && !busy && !env?.probing && (
        <Alert
          style={{ marginTop: 12 }}
          type="info"
          showIcon
          message="The benchmark environment is not installed yet"
          description={
            'Installing creates a Python virtual environment and clones the OpenVINO ' +
            'notebooks and GenAI repositories. ' +
            'It needs internet access and tens of GB of disk, and takes a while.'
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
