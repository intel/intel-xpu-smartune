// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// The live resource tiles: one number per subsystem, as it is right now.
//
// These started out inside the Processes tab, above its table, where they answer
// "is this machine busy, and with what" before the reader looks at any one
// process. A benchmark run asks exactly the same question -- a run whose GPU sits
// at 4% is measuring something other than what the reader thinks -- so the tiles
// are here rather than there, and both tabs draw the same ones.
//
// <LiveResourceTiles> adds the polling: the Processes tab already has a fetch of
// its own to hang them off, while the Logs pane of the Benchmark tab has nothing
// but an SSE log stream, so it gets a self-contained panel that samples
// /monitor/dynamic_info for as long as a job is running and stops when it ends.

import React, { useCallback, useState } from 'react'
import { Badge, Progress, Space, Typography } from 'antd'
import { DownOutlined, ReloadOutlined, RightOutlined } from '@ant-design/icons'

import { api } from '../api/client'
import type { DynamicInfoData } from '../api/types'
import { useDocumentVisible } from '../hooks/useDocumentVisible'
import { usePolling } from '../hooks/usePolling'
import { COLORS } from '../styles/theme'
import { buildGpuLabelMap } from '../utils/gpu'

const { Text } = Typography

export function usageColor(pct: number): string {
  return pct > 80 ? COLORS.red : pct > 50 ? COLORS.orange : COLORS.green
}

export function formatRate(bytesPerSec: number): string {
  if (bytesPerSec < 1024) return `${bytesPerSec.toFixed(0)} B/s`
  if (bytesPerSec < 1024 * 1024) return `${(bytesPerSec / 1024).toFixed(1)} KB/s`
  if (bytesPerSec < 1024 * 1024 * 1024) return `${(bytesPerSec / 1024 / 1024).toFixed(1)} MB/s`
  return `${(bytesPerSec / 1024 / 1024 / 1024).toFixed(2)} GB/s`
}

export function formatGB(gb: number): string {
  return gb >= 100 ? `${gb.toFixed(0)} GB` : `${gb.toFixed(1)} GB`
}

// A single GPU device's utilisation: prefer the device-level value, falling
// back to the busiest engine when it is absent.  The display label comes from
// the shared label map so igpu/dgpu (and multiple same-type GPUs) can be told
// apart at a glance.
export interface GpuDevStat {
  name: string
  util: number | null
}

export function gpuDeviceStats(
  dyn: DynamicInfoData | null,
  labels: Map<string, string>,
): GpuDevStat[] {
  const devices = dyn?.gpu?.gpu_usage?.parsed?.devices
  if (!devices || devices.length === 0) return []
  return devices.map((d, i) => {
    let util = typeof d.utilization === 'number' ? d.utilization : null
    if (util === null) {
      const vals = Object.values(d.engine_util || {}).filter(
        (v): v is number => typeof v === 'number',
      )
      if (vals.length) util = Math.max(...vals)
    }
    const key = d.pci_dev || `GPU ${i}`
    return { name: labels.get(key) ?? key, util }
  })
}

// Busiest GPU across integrated + discrete devices — what the headline tile shows.
export function busiestGpu(devs: GpuDevStat[]): GpuDevStat | null {
  let best: GpuDevStat | null = null
  for (const d of devs) {
    if (d.util === null) continue
    if (best === null || (best.util ?? -1) < d.util) best = d
  }
  return best
}

export interface DiskDevStat {
  name: string
  utilization: number
  readBytes: number
  writeBytes: number
}

// Per-disk stats plus fleet totals.  read/write are reported in KB/s by the
// backend; convert to bytes/s so formatRate() can render them like the network tile.
export function diskStats(dyn: DynamicInfoData | null): {
  devices: DiskDevStat[]
  totalBytes: number
  busiest: DiskDevStat | null
} {
  const io = dyn?.disk?.disk_io
  const devices: DiskDevStat[] = []
  let totalBytes = 0
  let busiest: DiskDevStat | null = null
  if (io) {
    for (const [name, d] of Object.entries(io)) {
      const readBytes = (d.read_kb_per_sec || 0) * 1024
      const writeBytes = (d.write_kb_per_sec || 0) * 1024
      const dev: DiskDevStat = {
        name,
        utilization: d.utilization || 0,
        readBytes,
        writeBytes,
      }
      devices.push(dev)
      totalBytes += readBytes + writeBytes
      if (busiest === null || busiest.utilization < dev.utilization) busiest = dev
    }
  }
  return { devices, totalBytes, busiest }
}

export interface StatTileProps {
  label: string
  value: string
  color?: string
  percent?: number | null
  sub?: string
  // When provided, the tile becomes collapsible and renders these rows below
  // the headline value on expand (per-disk / per-NIC / per-GPU breakdown).
  details?: React.ReactNode
}

export function StatTile({ label, value, color, percent, sub, details }: StatTileProps) {
  const [open, setOpen] = useState(false)
  const expandable = details != null
  return (
    <div
      style={{
        flex: '1 1 0',
        minWidth: 130,
        background: COLORS.headerBg,
        border: `1px solid ${COLORS.border}`,
        borderRadius: 6,
        padding: '10px 12px',
        alignSelf: 'flex-start',
      }}
    >
      <div
        onClick={expandable ? () => setOpen((o) => !o) : undefined}
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          cursor: expandable ? 'pointer' : 'default',
        }}
      >
        <Text
          style={{
            color: COLORS.textMuted,
            fontSize: 10,
            textTransform: 'uppercase',
            letterSpacing: 0.5,
          }}
        >
          {label}
        </Text>
        {expandable &&
          (open ? (
            <DownOutlined style={{ color: COLORS.textMuted, fontSize: 9 }} />
          ) : (
            <RightOutlined style={{ color: COLORS.textMuted, fontSize: 9 }} />
          ))}
      </div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6, marginTop: 2 }}>
        <Text style={{ color: color ?? COLORS.text, fontSize: 20, fontWeight: 600 }}>{value}</Text>
        {sub && <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>{sub}</Text>}
      </div>
      {typeof percent === 'number' && (
        <Progress
          percent={Math.min(Math.max(percent, 0), 100)}
          showInfo={false}
          strokeColor={color ?? COLORS.accent}
          trailColor={COLORS.border}
          size="small"
          style={{ marginTop: 4, marginBottom: 0 }}
        />
      )}
      {expandable && open && (
        <div
          style={{
            marginTop: 8,
            paddingTop: 8,
            borderTop: `1px solid ${COLORS.border}`,
            display: 'flex',
            flexDirection: 'column',
            gap: 4,
          }}
        >
          {details}
        </div>
      )}
    </div>
  )
}

// One line inside an expanded tile: a name on the left, a value on the right.
export function DetailRow({
  name,
  value,
  color,
}: {
  name: string
  value: string
  color?: string
}) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
      <Text style={{ color: COLORS.textMuted, fontSize: 11, fontFamily: 'monospace' }}>{name}</Text>
      <Text style={{ color: color ?? COLORS.text, fontSize: 11, whiteSpace: 'nowrap' }}>
        {value}
      </Text>
    </div>
  )
}

/**
 * The four tiles a snapshot can fill, drawn from one already-fetched snapshot.
 *
 * Kept separate from the polling below so the Processes tab -- which fetches the
 * snapshot alongside its process list, and must keep the two in step -- draws
 * the same tiles without a second request of its own.
 */
export function ResourceTiles({
  dyn,
  gpuLabels,
}: {
  dyn: DynamicInfoData | null
  /** PCI address -> iGPU/dGPU label. Built by the caller when it has one already. */
  gpuLabels?: Map<string, string>
}) {
  const labels = gpuLabels ?? buildGpuLabelMap(dyn?.gpu?.gpu_usage?.parsed?.devices)
  const cpu = dyn?.cpu?.usage_total
  const mem = dyn?.memory?.usage_percent
  const memTotal = dyn?.memory?.total_gb ?? null
  const memAvail = dyn?.memory?.available_gb ?? null
  const memUsed = memTotal !== null && memAvail !== null ? Math.max(memTotal - memAvail, 0) : null
  const swapUsed = dyn?.memory?.swap_used_gb ?? null
  const swapTotal = dyn?.memory?.swap_total_gb ?? null

  const netRx = dyn?.network?.total?.rx_bytes_per_sec ?? 0
  const netTx = dyn?.network?.total?.tx_bytes_per_sec ?? 0
  const nics = Object.entries(dyn?.network?.interfaces ?? {})

  const disk = diskStats(dyn)
  const gpuDevs = gpuDeviceStats(dyn, labels)
  const gpu = busiestGpu(gpuDevs)

  return (
    <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
      <StatTile
        label="CPU"
        value={typeof cpu === 'number' ? `${cpu.toFixed(1)}%` : '—'}
        color={typeof cpu === 'number' ? usageColor(cpu) : undefined}
        percent={typeof cpu === 'number' ? cpu : null}
      />
      <StatTile
        label="Memory"
        value={typeof mem === 'number' ? `${mem.toFixed(1)}%` : '—'}
        color={typeof mem === 'number' ? usageColor(mem) : undefined}
        percent={typeof mem === 'number' ? mem : null}
        sub={
          memUsed !== null && memTotal !== null
            ? `${formatGB(memUsed)} / ${formatGB(memTotal)}`
            : undefined
        }
        details={
          swapTotal !== null && swapTotal > 0 ? (
            <DetailRow name="swap" value={`${formatGB(swapUsed ?? 0)} / ${formatGB(swapTotal)}`} />
          ) : undefined
        }
      />
      <StatTile
        label="GPU"
        value={gpu?.util != null ? `${gpu.util.toFixed(1)}%` : '—'}
        color={gpu?.util != null ? usageColor(gpu.util) : undefined}
        percent={gpu?.util != null ? gpu.util : null}
        sub={gpuDevs.length > 1 ? `busiest of ${gpuDevs.length}` : undefined}
        details={
          gpuDevs.length ? (
            gpuDevs.map((d, i) => (
              <DetailRow
                key={`${d.name}-${i}`}
                name={d.name}
                color={d.util != null ? usageColor(d.util) : COLORS.textMuted}
                value={d.util != null ? `${d.util.toFixed(1)}%` : '—'}
              />
            ))
          ) : undefined
        }
      />
      <StatTile
        label="Disk"
        value={formatRate(disk.totalBytes)}
        color={COLORS.accent}
        sub={
          disk.busiest
            ? `busiest ${disk.busiest.name} ${disk.busiest.utilization.toFixed(0)}%`
            : undefined
        }
        percent={disk.busiest ? disk.busiest.utilization : null}
        details={
          disk.devices.length ? (
            disk.devices.map((d) => (
              <DetailRow
                key={d.name}
                name={d.name}
                color={usageColor(d.utilization)}
                value={`${d.utilization.toFixed(0)}%  ↓${formatRate(d.readBytes)} ↑${formatRate(d.writeBytes)}`}
              />
            ))
          ) : undefined
        }
      />
      {/* Here because a benchmark's first stage is a download: a run that looks
          stuck with the GPU idle is usually still pulling weights. */}
      <StatTile
        label="Network"
        value={formatRate(netRx + netTx)}
        color={COLORS.accent}
        sub={`↓${formatRate(netRx)} ↑${formatRate(netTx)}`}
        details={
          nics.length ? (
            nics.map(([name, n]) => (
              <DetailRow
                key={name}
                name={name}
                value={`↓${formatRate(n.rx_bytes_per_sec)} ↑${formatRate(n.tx_bytes_per_sec)}`}
              />
            ))
          ) : undefined
        }
      />
    </div>
  )
}

// How often the self-polling panel samples the machine while a job runs. Faster
// than the Processes tab's five seconds: this is watched during a run, where a
// stage change (weights loading, then generating) is the thing being waited for.
const LIVE_INTERVAL_MS = 3000

// Only what the tiles read. The full snapshot walks every subsystem the monitor
// knows, which is a lot of work to do three times a minute for four numbers.
const LIVE_SECTIONS = ['cpu', 'memory', 'gpu', 'disk', 'network']

/**
 * The tiles, sampling the machine themselves.
 *
 * `enabled` is the run: polling starts when a job does and stops when it ends,
 * so an idle dashboard makes no requests at all. A backgrounded browser tab
 * stops too -- nobody is reading it, and usePolling's own backoff only covers a
 * server that has gone away.
 *
 * A failed sample leaves the last one on screen rather than blanking the panel:
 * the numbers going stale for three seconds is a smaller lie than four dashes
 * where a busy machine was.
 */
export default function LiveResourceTiles({
  enabled,
  title = 'Machine while this job runs',
}: {
  enabled: boolean
  title?: string
}) {
  const [dyn, setDyn] = useState<DynamicInfoData | null>(null)
  const [at, setAt] = useState<Date | null>(null)
  const visible = useDocumentVisible()

  const sample = useCallback(async () => {
    try {
      const data = await api.getDynamicInfo(LIVE_SECTIONS)
      setDyn(data)
      setAt(new Date())
    } catch {
      // Keep the previous sample; the next tick will try again.
    }
  }, [])

  usePolling(sample, LIVE_INTERVAL_MS, enabled && visible)

  if (!enabled) return null

  return (
    <Space direction="vertical" size={6} style={{ width: '100%' }}>
      <Space size={10} wrap>
        <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>{title}</Text>
        <Badge
          status="processing"
          color={COLORS.green}
          text={
            <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>
              Auto-refresh {LIVE_INTERVAL_MS / 1000}s
            </Text>
          }
        />
        {at && (
          <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>
            <ReloadOutlined style={{ marginRight: 4 }} />
            {at.toLocaleTimeString()}
          </Text>
        )}
      </Space>
      <ResourceTiles dyn={dyn} />
    </Space>
  )
}
