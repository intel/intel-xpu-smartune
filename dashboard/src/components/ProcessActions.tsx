// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Shared "Operation" menu used by both the Processes tab (single PID per row)
// and the App Resources tab (an app may span several PIDs).  Kill / suspend act
// on the whole `pids` set; copy / properties use the representative PID.

import React, { useCallback, useState } from 'react'
import { Dropdown, Modal, Spin, Descriptions, Typography, message } from 'antd'
import { MoreOutlined } from '@ant-design/icons'
import { COLORS } from '../styles/theme'
import { api } from '../api/client'
import type { ProcessDetailData } from '../api/types'

const { Text } = Typography

export interface ProcessActionTarget {
  // Display name; also used as the keyword when adding to the balancer.
  name: string
  // Every PID the action should signal.  One entry for a plain process row.
  pids: number[]
  // PID used for read-only, single-process actions (Properties / Copy PID).
  representativePid: number
  cmdline: string
  // Representative status; 'stopped' toggles Suspend -> Resume.
  status?: string
  // SmartTune's own processes — kill/suspend disabled.
  isSelf?: boolean
  // False for shells / blacklisted / self — hides "Add to balancer".
  balancerCandidate?: boolean
}

interface MenuProps {
  target: ProcessActionTarget
  balancerEnabled: boolean
  onRegister?: (name: string) => void
  onChanged: () => void
  onShowDetail: (pid: number) => void
  allowSuspend?: boolean
}

async function copyText(text: string, label: string) {
  // navigator.clipboard only exists in a secure context (HTTPS / localhost);
  // over http://<ip> it is undefined, so fall back to the legacy execCommand
  // path via a hidden textarea so remote access keeps working.
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
    } else {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.focus()
      ta.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(ta)
      if (!ok) throw new Error('execCommand copy failed')
    }
    message.success(`${label} copied`)
  } catch {
    message.error('Copy failed')
  }
}

// Signal every PID and report an aggregate result so a partial failure (e.g. one
// child already gone) still surfaces the successes.
async function signalAll(
  pids: number[],
  call: (pid: number) => Promise<unknown>,
  name: string,
  verb: string,
  onChanged: () => void,
) {
  const results = await Promise.allSettled(pids.map((pid) => call(pid)))
  const ok = results.filter((r) => r.status === 'fulfilled').length
  if (ok === pids.length) message.success(`${verb} ${name}`)
  else if (ok > 0) message.warning(`${verb} ${name}: ${ok}/${pids.length} processes`)
  else message.error(`Failed to ${verb.toLowerCase()} ${name}`)
  onChanged()
}

export function ProcessActionsMenu({
  target,
  balancerEnabled,
  onRegister,
  onChanged,
  onShowDetail,
  allowSuspend = true,
}: MenuProps) {
  const { name, pids, representativePid, cmdline, status, isSelf, balancerCandidate } = target
  const canRegister = balancerEnabled && !!onRegister && balancerCandidate !== false
  const canSuspend = balancerEnabled && allowSuspend && balancerCandidate !== false
  const stopped = status === 'stopped'
  const multi = pids.length > 1

  const confirmKill = (force: boolean) => {
    Modal.confirm({
      title: force ? `Force kill ${name}?` : `End ${name}?`,
      content: multi
        ? `${pids.length} processes (PIDs ${pids.join(', ')}) will be ${
            force ? 'killed immediately (SIGKILL)' : 'asked to terminate (SIGTERM)'
          }.${force ? ' Unsaved work may be lost.' : ''}`
        : `PID ${pids[0]} will be ${
            force ? 'killed immediately (SIGKILL). Unsaved work may be lost.' : 'asked to terminate (SIGTERM).'
          }`,
      okText: force ? 'Force kill' : 'End process',
      okButtonProps: { danger: true },
      cancelText: 'Cancel',
      onOk: () =>
        signalAll(pids, (pid) => api.killProcess(pid, force), name, force ? 'Killed' : 'Terminated', onChanged),
    })
  }

  const doSuspend = (resume: boolean) =>
    signalAll(
      pids,
      (pid) => api.suspendProcess(pid, resume),
      name,
      resume ? 'Resumed' : 'Suspended',
      onChanged,
    )

  return (
    <Dropdown
      trigger={['click']}
      menu={{
        items: [
          ...(canRegister
            ? [{ key: 'register', label: 'Add to balancer', onClick: () => onRegister!(name) }]
            : []),
          ...(canSuspend
            ? [
                {
                  key: 'suspend',
                  label: stopped ? 'Resume' : 'Suspend',
                  onClick: () => doSuspend(stopped),
                },
              ]
            : []),
          ...(balancerEnabled
            ? [
                {
                  key: 'term',
                  label: <span style={{ fontWeight: 600 }}>End process</span>,
                  disabled: !!isSelf,
                  onClick: () => confirmKill(false),
                },
                {
                  key: 'kill',
                  label: <span style={{ fontWeight: 600 }}>Force kill</span>,
                  disabled: !!isSelf,
                  onClick: () => confirmKill(true),
                },
                { key: 'div1', type: 'divider' as const },
              ]
            : []),
          { key: 'copypid', label: 'Copy PID', onClick: () => copyText(String(representativePid), 'PID') },
          {
            key: 'copycmd',
            label: 'Copy command',
            disabled: !cmdline,
            onClick: () => copyText(cmdline, 'Command'),
          },
          { key: 'div2', type: 'divider' as const },
          { key: 'props', label: 'Properties', onClick: () => onShowDetail(representativePid) },
        ],
      }}
    >
      <MoreOutlined style={{ cursor: 'pointer', color: COLORS.accent, fontSize: 18 }} />
    </Dropdown>
  )
}

// Start time: HH:MM:SS if today, else MM-DD HH:MM.
function formatStartTime(epochSec: number): string {
  const d = new Date(epochSec * 1000)
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  return d.toDateString() === now.toDateString()
    ? `${hm}:${pad(d.getSeconds())}`
    : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`
}

// Shared "Properties" modal + opener, so both tabs render an identical detail view.
export function useProcessDetail() {
  const [detail, setDetail] = useState<ProcessDetailData | null>(null)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)

  const openDetail = useCallback(async (pid: number) => {
    setOpen(true)
    setLoading(true)
    setDetail(null)
    try {
      setDetail(await api.getProcessDetail(pid))
    } catch (e) {
      message.error(e instanceof Error ? e.message : 'Failed to load process detail')
      setOpen(false)
    } finally {
      setLoading(false)
    }
  }, [])

  const detailModal = (
    <Modal
      open={open}
      title={detail ? `${detail.name} (PID ${detail.pid})` : 'Process details'}
      onCancel={() => setOpen(false)}
      footer={null}
      width={640}
    >
      {loading || !detail ? (
        <div style={{ padding: 24, textAlign: 'center' }}>
          <Spin />
        </div>
      ) : (
        <Descriptions column={1} size="small" bordered>
          <Descriptions.Item label="Executable">
            {detail.exe ? <Text copyable>{detail.exe}</Text> : '—'}
          </Descriptions.Item>
          <Descriptions.Item label="Working dir">
            {detail.cwd ? <Text copyable>{detail.cwd}</Text> : '—'}
          </Descriptions.Item>
          <Descriptions.Item label="Command">
            {detail.cmdline ? <Text copyable>{detail.cmdline}</Text> : '—'}
          </Descriptions.Item>
          <Descriptions.Item label="User">{detail.username || '—'}</Descriptions.Item>
          <Descriptions.Item label="Status">{detail.status || '—'}</Descriptions.Item>
          <Descriptions.Item label="Parent PID">{detail.ppid ?? '—'}</Descriptions.Item>
          <Descriptions.Item label="Threads">{detail.num_threads ?? '—'}</Descriptions.Item>
          <Descriptions.Item label="Open FDs">{detail.num_fds ?? '—'}</Descriptions.Item>
          <Descriptions.Item label="Nice">{detail.nice ?? '—'}</Descriptions.Item>
          <Descriptions.Item label="Started">
            {detail.create_time ? formatStartTime(detail.create_time) : '—'}
          </Descriptions.Item>
        </Descriptions>
      )}
    </Modal>
  )

  return { openDetail, detailModal }
}
