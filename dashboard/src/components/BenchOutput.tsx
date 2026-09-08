// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// Live output of the running (or last) benchmark job, and what the machine is
// doing while it runs.
//
// The text arrives over SSE as byte-offset deltas; this component only displays
// what the container has assembled. It stays mounted while the Results tab is on
// screen so that scroll position and the assembled log survive tab switching.
//
// The resource tiles above the log are the same ones the Processes tab shows
// (see ResourceTiles), sampling the machine for as long as the job runs: a log
// line says which case is being measured, and the tiles say whether the device
// it named is the one doing the work.

import React, { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { Button, Empty, Space, Tag, Typography } from 'antd'
import { VerticalAlignBottomOutlined } from '@ant-design/icons'

import type { BenchJob } from '../api/types'
import { COLORS } from '../styles/theme'
import LiveResourceTiles from './ResourceTiles'

const { Text } = Typography

const STATUS_COLOR: Record<string, string> = {
  running: 'processing',
  done: 'success',
  failed: 'error',
  cancelled: 'default',
}

interface Props {
  job: BenchJob | null
  log: string
  // The stream joined a job that had already produced more than the snapshot
  // tail, so the beginning of the log is on disk but not on screen.
  truncated: boolean
  height?: number
}

export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '-'
  const s = Math.floor(seconds % 60)
  const m = Math.floor((seconds / 60) % 60)
  const h = Math.floor(seconds / 3600)
  if (h) return `${h}h ${m}m`
  if (m) return `${m}m ${s}s`
  return `${s}s`
}

export function JobStatusTag({ job }: { job: BenchJob | null }) {
  if (!job) return null
  return <Tag color={STATUS_COLOR[job.status] ?? 'default'}>{job.status}</Tag>
}

// How close to the bottom still counts as following the log. Only has to absorb
// sub-pixel rounding and the odd partial line, because it is measured against
// the scroll the user last made -- not against content appended since.
const STICK_SLACK_PX = 24

export default function BenchOutput({ job, log, truncated, height = 420 }: Props) {
  const boxRef = useRef<HTMLPreElement>(null)
  // Whether to follow the tail. Recorded when the user scrolls rather than
  // derived after a delta lands: by then scrollHeight already counts the new
  // text, so any append taller than the slack reads as "scrolled away" and
  // auto-scroll switches itself off for good. Kept in a ref for the layout
  // effect and mirrored into state only to render the button.
  const stickRef = useRef(true)
  const [pinned, setPinned] = useState(true)

  const stick = useCallback((value: boolean) => {
    stickRef.current = value
    setPinned(value)
  }, [])

  // A new job starts a new log; follow it again however the last one was left.
  useLayoutEffect(() => {
    stick(true)
  }, [job?.id, stick])

  useLayoutEffect(() => {
    const box = boxRef.current
    if (box && stickRef.current) box.scrollTop = box.scrollHeight
  }, [log, pinned])

  const handleScroll = useCallback(() => {
    const box = boxRef.current
    if (!box) return
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < STICK_SLACK_PX
    if (atBottom !== stickRef.current) stick(atBottom)
  }, [stick])

  const running = job?.status === 'running'

  // The tiles come before the early return on purpose: a job that has started
  // but not yet printed a line is exactly when the reader wants to see that
  // something is happening.
  if (!log) {
    return (
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <LiveResourceTiles enabled={running} />
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            running
              ? 'Waiting for the first line of output ...'
              : 'Nothing running. Start a download or a benchmark to see output here.'
          }
        />
      </Space>
    )
  }

  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      <LiveResourceTiles enabled={running} />
      <Space size={8} wrap>
        <JobStatusTag job={job} />
        {job && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {job.kind} · {formatDuration(job.duration)}
          </Text>
        )}
        {job && (
          <Text type="secondary" style={{ fontSize: 12 }} copyable={{ text: job.log_path }}>
            {job.log_path}
          </Text>
        )}
      </Space>
      {truncated && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          Showing the tail of a job that was already running; the full log is in the
          file above.
        </Text>
      )}
      <div style={{ position: 'relative' }}>
        <pre
          ref={boxRef}
          onScroll={handleScroll}
          style={{
            margin: 0,
            height,
            overflow: 'auto',
            background: COLORS.bg,
            border: `1px solid ${COLORS.border}`,
            borderRadius: 4,
            padding: 12,
            fontSize: 12.5,
            whiteSpace: 'pre-wrap',
          }}
        >
          {log}
        </pre>
        {/* Scrolling up is how you read back through a running log, so it has to
            stop the follow -- and then say how to get it back, or the log looks
            stuck at wherever it was left. */}
        {!pinned && (
          <Button
            size="small"
            icon={<VerticalAlignBottomOutlined />}
            onClick={() => stick(true)}
            style={{ position: 'absolute', right: 16, bottom: 12 }}
          >
            Jump to latest
          </Button>
        )}
      </div>
    </Space>
  )
}
