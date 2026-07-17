import React, { useState, useCallback } from 'react'
import {
  Table,
  Tag,
  Typography,
  Alert,
  Spin,
  Badge,
  Progress,
  Space,
  Tooltip,
  Input,
  message,
  Checkbox,
  Button,
} from 'antd'
import {
  ReloadOutlined,
  SearchOutlined,
  DownOutlined,
  RightOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { COLORS } from '../styles/theme'
import { api } from '../api/client'
import type { ProcessEntry, DynamicInfoData } from '../api/types'
import { usePolling } from '../hooks/usePolling'
import { ProcessActionsMenu, useProcessDetail } from './ProcessActions'
import { buildGpuLabelMap } from '../utils/gpu'

const { Text, Title } = Typography

interface Props {
  active: boolean
  balancerEnabled: boolean
  // Jump to the balancer tab and open the Add-App wizard pre-filled with this name.
  onRegister?: (name: string) => void
}

function usageColor(pct: number): string {
  return pct > 80 ? COLORS.red : pct > 50 ? COLORS.orange : COLORS.green
}

function formatRate(bytesPerSec: number): string {
  if (bytesPerSec < 1024) return `${bytesPerSec.toFixed(0)} B/s`
  if (bytesPerSec < 1024 * 1024) return `${(bytesPerSec / 1024).toFixed(1)} KB/s`
  if (bytesPerSec < 1024 * 1024 * 1024) return `${(bytesPerSec / 1024 / 1024).toFixed(1)} MB/s`
  return `${(bytesPerSec / 1024 / 1024 / 1024).toFixed(2)} GB/s`
}

function formatGB(gb: number): string {
  return gb >= 100 ? `${gb.toFixed(0)} GB` : `${gb.toFixed(1)} GB`
}

// A single GPU device's utilisation: prefer the device-level value, falling
// back to the busiest engine when it is absent.  The display label comes from
// the shared label map so igpu/dgpu (and multiple same-type GPUs) can be told
// apart at a glance.
interface GpuDevStat {
  name: string
  util: number | null
}

function gpuDeviceStats(dyn: DynamicInfoData | null, labels: Map<string, string>): GpuDevStat[] {
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
function busiestGpu(devs: GpuDevStat[]): GpuDevStat | null {
  let best: GpuDevStat | null = null
  for (const d of devs) {
    if (d.util === null) continue
    if (best === null || (best.util ?? -1) < d.util) best = d
  }
  return best
}

interface DiskDevStat {
  name: string
  utilization: number
  readBytes: number
  writeBytes: number
}

// Per-disk stats plus fleet totals.  read/write are reported in KB/s by the
// backend; convert to bytes/s so formatRate() can render them like the network tile.
function diskStats(dyn: DynamicInfoData | null): {
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

interface StatTileProps {
  label: string
  value: string
  color?: string
  percent?: number | null
  sub?: string
  // When provided, the tile becomes collapsible and renders these rows below
  // the headline value on expand (per-disk / per-NIC / per-GPU breakdown).
  details?: React.ReactNode
}

function StatTile({ label, value, color, percent, sub, details }: StatTileProps) {
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
function DetailRow({ name, value, color }: { name: string; value: string; color?: string }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
      <Text style={{ color: COLORS.textMuted, fontSize: 11, fontFamily: 'monospace' }}>{name}</Text>
      <Text style={{ color: color ?? COLORS.text, fontSize: 11, whiteSpace: 'nowrap' }}>{value}</Text>
    </div>
  )
}

function formatMemory(kb: number): string {
  if (kb < 1024) return `${kb.toFixed(0)} KB`
  if (kb < 1024 * 1024) return `${(kb / 1024).toFixed(1)} MB`
  return `${(kb / 1024 / 1024).toFixed(2)} GB`
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

// System processes use uid < 1000 by Linux convention (root = 0).
function isSystemProcess(p: ProcessEntry): boolean {
  return p.uid !== null && p.uid < 1000
}

// A table row is either a process or an aggregated app/cgroup group (with children).
type Row = ProcessEntry & {
  key?: string
  isGroup?: boolean
  groupName?: string
  groupRawName?: string
  childCount?: number
  children?: Row[]
}

// Last cgroup path segment (systemd unit/scope).
function cgroupLeaf(cgroup: string): string {
  const segs = cgroup.split('/').filter(Boolean)
  return segs.length ? segs[segs.length - 1] : '(root)'
}

function friendlyGroupLabel(cgroup: string): string {
  const raw = cgroupLeaf(cgroup)
  if (raw === '(root)') return raw

  let label = raw.replace(/\.scope$/i, '')
  label = label.replace(/-[0-9]+@[A-Za-z0-9._-]+$/, '')
  label = label.replace(/-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i, '')
  label = label.replace(/-[0-9]+$/, '')
  if (label.startsWith('app-')) label = label.slice(4)
  label = label.replace(/^org\.gnome\./i, '')
  label = label.replace(/^org\.kde\./i, '')

  return label || raw
}

// Generic systemd containers whose leaf name says nothing about what runs
// inside (a terminal scope, a login session, a slice…).  For these we label the
// group after its dominant child instead — e.g. "node" rather than "vte-spawn".
function isGenericScope(label: string): boolean {
  const l = label.toLowerCase()
  return (
    l === '(root)' ||
    l === 'vte-spawn' ||
    l === 'init' ||
    l === 'app' ||
    l === 'systemd' ||
    l.startsWith('session') ||
    l.startsWith('user') ||
    l.startsWith('snap') ||
    l.endsWith('.slice')
  )
}

// The child that best represents a group: the heaviest (by RSS) real app,
// preferring balancer-worthy processes so shells never win over the app they host.
function representativeName(members: ProcessEntry[]): string {
  const heaviest = (arr: ProcessEntry[]) =>
    arr.reduce<ProcessEntry | null>(
      (best, m) => (best === null || m.mem_rss_kb > best.mem_rss_kb ? m : best),
      null,
    )
  const apps = members.filter((m) => m.balancer_candidate !== false)
  return (heaviest(apps.length ? apps : members)?.name || '').trim()
}

function aggregateGpuDevices(rows: ProcessEntry[]): ProcessEntry['gpu_devices'] | undefined {
  const merged: NonNullable<ProcessEntry['gpu_devices']> = {}
  let hasAny = false

  for (const row of rows) {
    if (!row.gpu_devices) continue
    hasAny = true
    for (const [pdev, dev] of Object.entries(row.gpu_devices)) {
      const prev = merged[pdev] ?? { gpu_util: 0, gpu_mem_mb: 0 }
      merged[pdev] = {
        gpu_util: Math.min(100, prev.gpu_util + dev.gpu_util),
        gpu_mem_mb: Math.round((prev.gpu_mem_mb + dev.gpu_mem_mb) * 10) / 10,
      }
    }
  }

  return hasAny ? merged : undefined
}

// Group processes by cgroup label into expandable parent rows with summed metrics.
function buildGroups(rows: ProcessEntry[]): Row[] {
  const map = new Map<string, ProcessEntry[]>()
  for (const p of rows) {
    const key = p.cgroup || '(root)'
    const arr = map.get(key)
    if (arr) arr.push(p)
    else map.set(key, [p])
  }
  const groups: Row[] = []
  for (const [groupKey, members] of map) {
    const sum = (f: (p: ProcessEntry) => number) => members.reduce((s, p) => s + (f(p) || 0), 0)
    const rawLabel = friendlyGroupLabel(groupKey)
    const friendlyName = isGenericScope(rawLabel)
      ? representativeName(members) || rawLabel
      : rawLabel
    groups.push({
      pid: -1,
      name: friendlyName,
      username: '',
      uid: null,
      cpu_percent: Math.round(sum((p) => p.cpu_percent) * 10) / 10,
      memory_percent: Math.round(sum((p) => p.memory_percent) * 100) / 100,
      mem_rss_kb: sum((p) => p.mem_rss_kb),
      mem_shared_kb: sum((p) => p.mem_shared_kb),
      status: '',
      create_time: members.reduce((earliest, p) => {
        if (!p.create_time) return earliest
        if (!earliest) return p.create_time
        return Math.min(earliest, p.create_time)
      }, null as number | null),
      cgroup: groupKey,
      cmdline: '',
      io_read_rate: sum((p) => p.io_read_rate || 0),
      io_write_rate: sum((p) => p.io_write_rate || 0),
      gpu_devices: aggregateGpuDevices(members),
      isGroup: true,
      groupName: friendlyName,
      groupRawName: cgroupLeaf(groupKey),
      childCount: members.length,
      key: `group:${groupKey}`,
      children: members.map((c) => ({ ...c, key: String(c.pid) })),
    })
  }
  return groups.sort((a, b) => b.cpu_percent - a.cpu_percent)
}

export default function Processes({ active, balancerEnabled, onRegister }: Props) {
  const [allRows, setAllRows] = useState<ProcessEntry[]>([])
  const [dyn, setDyn] = useState<DynamicInfoData | null>(null)
  const [filter, setFilter] = useState('')
  const [hideSystem, setHideSystem] = useState(false)
  const [groupByApp, setGroupByApp] = useState(false)
  const { openDetail, detailModal } = useProcessDetail()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [totalCount, setTotalCount] = useState(0)

  const fetchData = useCallback(async () => {
    try {
      // Fetch the process list and the system-wide dynamic snapshot together so
      // the summary bar stays in sync with the table.  A failure of the summary
      // snapshot must not blank out the table, so it is tolerated separately.
      const [data, dynData] = await Promise.all([
        api.getProcesses(true, true),
        api.getDynamicInfo().catch(() => null),
      ])
      setAllRows(data.processes)
      setTotalCount(data.count)
      setDyn(dynData)
      setError(null)
      setLastUpdated(new Date())
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to fetch data')
    } finally {
      setLoading(false)
    }
  }, [])

  usePolling(fetchData, 5000, active)

  // The row menu lives in the shared <ProcessActionsMenu>; this is only the
  // one-click Resume used by the "suspended" banner below.
  const resumeProcess = useCallback(
    async (p: ProcessEntry) => {
      try {
        await api.suspendProcess(p.pid, true)
        message.success(`Resumed ${p.name} (${p.pid})`)
        fetchData()
      } catch (e) {
        message.error(e instanceof Error ? e.message : 'Failed to signal process')
      }
    },
    [fetchData],
  )

  const filterLower = filter.toLowerCase()
  const textFiltered = filterLower
    ? allRows.filter(
        (p) =>
          p.name.toLowerCase().includes(filterLower) ||
          p.cmdline.toLowerCase().includes(filterLower) ||
          String(p.pid).includes(filterLower) ||
          (p.username || '').toLowerCase().includes(filterLower) ||
          (p.cgroup || '').toLowerCase().includes(filterLower),
      )
    : allRows
  const rows = hideSystem ? textFiltered.filter((p) => !isSystemProcess(p)) : textFiltered
  const tableRows: Row[] = groupByApp
    ? buildGroups(rows)
    : rows.map((p) => ({ ...p, key: String(p.pid) }))

  const hasGpu = allRows.some((p) => p.gpu_devices !== undefined)
  const hasIo = allRows.some((p) => p.io_read_rate !== undefined || p.io_write_rate !== undefined)

  const ioColumns: ColumnsType<Row> = [
    {
      title: 'Disk I/O',
      key: 'disk_io',
      width: 100,
      sorter: (a, b) =>
        (a.io_read_rate || 0) + (a.io_write_rate || 0) - (b.io_read_rate || 0) - (b.io_write_rate || 0),
      render: (_: unknown, p: ProcessEntry) => {
        if (p.io_read_rate == null && p.io_write_rate == null)
          return <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>—</Text>
        return (
          <Space direction="vertical" size={0} style={{ width: '100%' }}>
            <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>↓{formatRate(p.io_read_rate || 0)}</Text>
            <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>↑{formatRate(p.io_write_rate || 0)}</Text>
          </Space>
        )
      },
    },
  ]

  // Map a PCI address (drm-pdev) to an iGPU/dGPU label via the dynamic_info device
  // list; multiple same-type GPUs are disambiguated by their PCI address.
  const gpuLabelMap = buildGpuLabelMap(dyn?.gpu?.gpu_usage?.parsed?.devices)
  const gpuLabel = (pdev: string): string => gpuLabelMap.get(pdev) ?? pdev

  const peakUtil = (p: ProcessEntry): number =>
    p.gpu_devices ? Math.max(0, ...Object.values(p.gpu_devices).map((d) => d.gpu_util)) : 0
  const totalGpuMem = (p: ProcessEntry): number =>
    p.gpu_devices ? Object.values(p.gpu_devices).reduce((s, d) => s + d.gpu_mem_mb, 0) : 0

  const gpuColumns: ColumnsType<Row> = [
    {
      title: (
        <Tooltip title="Per-GPU utilisation.">
          GPU %
        </Tooltip>
      ),
      key: 'gpu_util',
      width: 90,
      sorter: (a, b) => peakUtil(a) - peakUtil(b),
      render: (_: unknown, p: ProcessEntry) => {
        const devs = p.gpu_devices
        if (!devs) return <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>—</Text>
        return (
          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            {Object.entries(devs).map(([pdev, s]) => {
              const color = usageColor(s.gpu_util)
              return (
                <div key={pdev} style={{ display: 'flex', justifyContent: 'space-between', gap: 6 }}>
                  <Text style={{ color: COLORS.textMuted, fontSize: 10 }}>{gpuLabel(pdev)}</Text>
                  <Text style={{ color, fontSize: 11 }}>{s.gpu_util.toFixed(1)}%</Text>
                </div>
              )
            })}
          </Space>
        )
      },
    },
    {
      title: 'GPU Mem',
      key: 'gpu_mem_mb',
      width: 100,
      sorter: (a, b) => totalGpuMem(a) - totalGpuMem(b),
      render: (_: unknown, p: ProcessEntry) => {
        const devs = p.gpu_devices
        if (!devs) return <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>—</Text>
        return (
          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            {Object.entries(devs).map(([pdev, s]) => (
              <div key={pdev} style={{ display: 'flex', justifyContent: 'space-between', gap: 6 }}>
                <Text style={{ color: COLORS.textMuted, fontSize: 10 }}>{gpuLabel(pdev)}</Text>
                <Text style={{ color: COLORS.text, fontSize: 11 }}>{formatMemory(s.gpu_mem_mb * 1024)}</Text>
              </div>
            ))}
          </Space>
        )
      },
    },
  ]

  const columns: ColumnsType<Row> = [
    {
      title: 'PID',
      dataIndex: 'pid',
      key: 'pid',
      width: 70,
      sorter: (a, b) => a.pid - b.pid,
      render: (v: number, p: Row) =>
        p.isGroup ? (
          <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>{p.childCount} procs</Text>
        ) : (
          <Text style={{ color: COLORS.textMuted, fontFamily: 'monospace', fontSize: 11 }}>{v}</Text>
        ),
    },
    {
      title: 'Name',
      dataIndex: 'name',
      key: 'name',
      width: 180,
      sorter: (a, b) => a.name.localeCompare(b.name),
      render: (v: string, p: Row) => (
        <Tooltip title={p.isGroup ? p.cgroup : undefined}>
          <Space size={4}>
            <Text
              style={{
                color: groupByApp && !p.isGroup ? COLORS.text : COLORS.accent,
                fontWeight: p.isGroup ? 700 : 500,
                fontSize: 12,
              }}
            >
              {v}
            </Text>
            {!p.isGroup && p.status === 'stopped' && (
              <Tag
                color="warning"
                style={{ fontSize: 10, lineHeight: '16px', margin: 0, padding: '0 4px' }}
              >
                Suspended
              </Tag>
            )}
          </Space>
        </Tooltip>
      ),
    },
    {
      title: 'User',
      dataIndex: 'username',
      key: 'username',
      width: 90,
      sorter: (a, b) => (a.username || '').localeCompare(b.username || ''),
      render: (v: string) => (
        <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>{v || '—'}</Text>
      ),
    },
    {
      title: 'CPU %',
      dataIndex: 'cpu_percent',
      key: 'cpu_percent',
      width: 110,
      defaultSortOrder: 'descend',
      sorter: (a, b) => a.cpu_percent - b.cpu_percent,
      render: (v: number) => {
        const color = v > 80 ? COLORS.red : v > 50 ? COLORS.orange : COLORS.green
        return (
          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            <Text style={{ color, fontSize: 11 }}>{v.toFixed(1)}%</Text>
            <Progress
              percent={Math.min(v, 100)}
              showInfo={false}
              strokeColor={color}
              trailColor={COLORS.border}
              size="small"
            />
          </Space>
        )
      },
    },
    {
      title: 'Mem %',
      dataIndex: 'memory_percent',
      key: 'memory_percent',
      width: 90,
      sorter: (a, b) => a.memory_percent - b.memory_percent,
      render: (v: number) => {
        const color = v > 10 ? COLORS.orange : COLORS.text
        return <Text style={{ color, fontSize: 12 }}>{v.toFixed(1)}%</Text>
      },
    },
    {
      title: 'RSS',
      dataIndex: 'mem_rss_kb',
      key: 'mem_rss_kb',
      width: 90,
      sorter: (a, b) => a.mem_rss_kb - b.mem_rss_kb,
      render: (v: number) => (
        <Text style={{ color: COLORS.text, fontSize: 12 }}>{formatMemory(v)}</Text>
      ),
    },
    {
      title: (
        <Tooltip title="Shared memory (shared libraries, mappings shared with other processes).">
          SHR
        </Tooltip>
      ),
      dataIndex: 'mem_shared_kb',
      key: 'mem_shared_kb',
      width: 90,
      sorter: (a, b) => a.mem_shared_kb - b.mem_shared_kb,
      render: (v: number) => (
        <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>{formatMemory(v)}</Text>
      ),
    },
    ...(hasIo ? ioColumns : []),
    ...(hasGpu ? gpuColumns : []),
    {
      title: 'Started',
      dataIndex: 'create_time',
      key: 'create_time',
      width: 90,
      sorter: (a, b) => (a.create_time || 0) - (b.create_time || 0),
      render: (v: number | null) => (
        <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>
          {v ? formatStartTime(v) : '—'}
        </Text>
      ),
    },
    {
      title: 'Command',
      dataIndex: 'cmdline',
      key: 'cmdline',
      width: 260,
      ellipsis: true,
      render: (v: string) => (
        <Tooltip title={v} overlayStyle={{ maxWidth: 500 }}>
          <Text
            style={{
              color: COLORS.textMuted,
              fontFamily: 'monospace',
              fontSize: 11,
              display: 'inline-block',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              maxWidth: 300,
            }}
          >
            {v || '—'}
          </Text>
        </Tooltip>
      ),
    },
    {
      title: 'Operation',
      key: 'actions',
      width: 90,
      align: 'center' as const,
      fixed: 'right' as const,
      render: (_: unknown, p: Row) =>
        p.isGroup ? null : (
          <ProcessActionsMenu
            target={{
              name: p.name,
              pids: [p.pid],
              representativePid: p.pid,
              cmdline: p.cmdline,
              status: p.status,
              isSelf: p.is_self,
              balancerCandidate: p.balancer_candidate,
            }}
            balancerEnabled={balancerEnabled}
            onRegister={onRegister}
            onChanged={fetchData}
            onShowDetail={openDetail}
          />
        ),
    },
  ]

  return (
    <div style={{ padding: '16px 0' }}>
      <div
        style={{
          background: COLORS.panelBg,
          border: `1px solid ${COLORS.border}`,
          borderRadius: 6,
          padding: 16,
        }}
      >
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            marginBottom: 12,
          }}
        >
          <Space>
            <Title level={5} style={{ color: COLORS.text, margin: 0 }}>
              All Processes
            </Title>
            {totalCount > 0 && (
              <Tag style={{ color: COLORS.textMuted, borderColor: COLORS.border, background: 'transparent' }}>
                {rows.length}/{totalCount}
              </Tag>
            )}
            <Text style={{ color: COLORS.border }}>|</Text>
            <Checkbox
              checked={hideSystem}
              onChange={(e) => setHideSystem(e.target.checked)}
              style={{ color: COLORS.textMuted, fontSize: 12 }}
            >
              Hide system
            </Checkbox>
            <Checkbox
              checked={groupByApp}
              onChange={(e) => setGroupByApp(e.target.checked)}
              style={{ color: COLORS.textMuted, fontSize: 12 }}
            >
              Group by app
            </Checkbox>
          </Space>
          <Space>
            <Input
              size="small"
              placeholder="Filter by name / PID / user / cmd"
              prefix={<SearchOutlined style={{ color: COLORS.textMuted }} />}
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              allowClear
              style={{ width: 220, background: COLORS.headerBg, borderColor: COLORS.border, color: COLORS.text }}
            />
            {lastUpdated && (
              <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>
                <ReloadOutlined style={{ marginRight: 4 }} />
                {lastUpdated.toLocaleTimeString()}
              </Text>
            )}
            <Badge
              status="processing"
              color={COLORS.green}
              text={<Text style={{ color: COLORS.textMuted, fontSize: 11 }}>Auto-refresh 5s</Text>}
            />
          </Space>
        </div>

        {(() => {
          const cpu = dyn?.cpu?.usage_total
          const mem = dyn?.memory?.usage_percent
          const memTotal = dyn?.memory?.total_gb ?? null
          const memAvail = dyn?.memory?.available_gb ?? null
          const memUsed =
            memTotal !== null && memAvail !== null ? Math.max(memTotal - memAvail, 0) : null
          const swapUsed = dyn?.memory?.swap_used_gb ?? null
          const swapTotal = dyn?.memory?.swap_total_gb ?? null

          const netRx = dyn?.network?.total?.rx_bytes_per_sec ?? 0
          const netTx = dyn?.network?.total?.tx_bytes_per_sec ?? 0
          const netTotal = netRx + netTx
          const nics = Object.entries(dyn?.network?.interfaces ?? {})

          const disk = diskStats(dyn)
          const gpuDevs = gpuDeviceStats(dyn, gpuLabelMap)
          const gpu = busiestGpu(gpuDevs)

          return (
            <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 12 }}>
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
                    <DetailRow
                      name="swap"
                      value={`${formatGB(swapUsed ?? 0)} / ${formatGB(swapTotal)}`}
                    />
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
              <StatTile
                label="Network"
                value={formatRate(netTotal)}
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
              <StatTile
                label="GPU"
                value={gpu?.util != null ? `${gpu.util.toFixed(1)}%` : '—'}
                color={gpu?.util != null ? usageColor(gpu.util) : undefined}
                percent={gpu?.util != null ? gpu.util : null}
                sub={gpuDevs.length > 1 ? `${gpuDevs.length} GPUs` : undefined}
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
            </div>
          )
        })()}

        {error && (
          <Alert
            message="API Error"
            description={error}
            type="error"
            showIcon
            style={{ marginBottom: 12 }}
          />
        )}

        {(() => {
          // Suspended (SIGSTOP'd) processes stay in the list as status 'stopped'
          // but sink to the bottom of the CPU-sorted table, so surface them here
          // with a one-click Resume regardless of the current sort/filter/page.
          const suspended = allRows.filter((p) => p.status === 'stopped')
          if (!suspended.length) return null
          return (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message={`${suspended.length} suspended process${suspended.length > 1 ? 'es' : ''}`}
              description={
                <Space size={[8, 8]} wrap>
                  {suspended.map((p) => (
                    <Tag key={p.pid} style={{ margin: 0, paddingRight: 4 }}>
                      {p.name} ({p.pid})
                      <Button
                        type="link"
                        size="small"
                        style={{ padding: '0 4px', height: 'auto' }}
                        onClick={() => resumeProcess(p)}
                      >
                        Resume
                      </Button>
                    </Tag>
                  ))}
                </Space>
              }
            />
          )
        })()}

        <Table
          columns={columns}
          dataSource={tableRows}
          loading={loading}
          size="small"
          scroll={{ x: 1180 + (hasIo ? 140 : 0) + (hasGpu ? 260 : 0) }}
          pagination={{ pageSize: 30, showSizeChanger: true, pageSizeOptions: ['20', '30', '50', '100'] }}
          expandable={
            groupByApp
              ? {
                  rowExpandable: (record) => !!record.isGroup,
                }
              : undefined
          }
          rowClassName={(record, idx) =>
            groupByApp && !record.isGroup
              ? 'table-row-child'
              : idx % 2 === 1
                ? 'table-row-alt'
                : ''
          }
          locale={{
            emptyText: (
              <div style={{ padding: 40, color: COLORS.textMuted, textAlign: 'center' }}>
                {loading ? <Spin /> : 'No process data available'}
              </div>
            ),
          }}
        />

        <style>{`
          .table-row-alt td { background: ${COLORS.rowAlt} !important; }
          .table-row-child > td { background: ${COLORS.accent}14 !important; }
          .table-row-child:hover > td { background: ${COLORS.accent}22 !important; }
          .ant-table { background: transparent !important; }
          .ant-table-thead > tr > th {
            background: ${COLORS.headerBg} !important;
            color: ${COLORS.textMuted} !important;
            font-size: 11px !important;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 1px solid ${COLORS.border} !important;
          }
          .ant-table-tbody > tr > td {
            border-bottom: 1px solid ${COLORS.border}55 !important;
          }
          .ant-table-tbody > tr:hover > td {
            background: ${COLORS.rowAlt} !important;
          }
        `}</style>
      </div>

      {detailModal}
    </div>
  )
}
