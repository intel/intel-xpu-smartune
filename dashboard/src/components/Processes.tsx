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
} from 'antd'
import { ReloadOutlined, SearchOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { COLORS } from '../styles/theme'
import { api } from '../api/client'
import type { ProcessEntry } from '../api/types'
import { usePolling } from '../hooks/usePolling'

const { Text, Title } = Typography

interface Props {
  active: boolean
}

const STATUS_COLOR: Record<string, string> = {
  running: COLORS.green,
  sleeping: COLORS.textMuted,
  disk_sleep: COLORS.orange,
  stopped: COLORS.red,
  zombie: COLORS.red,
  idle: COLORS.textMuted,
}

function formatMemory(kb: number): string {
  if (kb < 1024) return `${kb.toFixed(0)} KB`
  if (kb < 1024 * 1024) return `${(kb / 1024).toFixed(1)} MB`
  return `${(kb / 1024 / 1024).toFixed(2)} GB`
}

export default function Processes({ active }: Props) {
  const [allRows, setAllRows] = useState<ProcessEntry[]>([])
  const [filter, setFilter] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [totalCount, setTotalCount] = useState(0)

  const fetchData = useCallback(async () => {
    try {
      const data = await api.getProcesses()
      setAllRows(data.processes)
      setTotalCount(data.count)
      setError(null)
      setLastUpdated(new Date())
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to fetch data')
    } finally {
      setLoading(false)
    }
  }, [])

  usePolling(fetchData, 5000, active)

  const filterLower = filter.toLowerCase()
  const rows = filterLower
    ? allRows.filter(
        (p) =>
          p.name.toLowerCase().includes(filterLower) ||
          p.cmdline.toLowerCase().includes(filterLower) ||
          String(p.pid).includes(filterLower) ||
          (p.username || '').toLowerCase().includes(filterLower),
      )
    : allRows

  const columns: ColumnsType<ProcessEntry> = [
    {
      title: 'PID',
      dataIndex: 'pid',
      key: 'pid',
      width: 70,
      sorter: (a, b) => a.pid - b.pid,
      render: (v: number) => (
        <Text style={{ color: COLORS.textMuted, fontFamily: 'monospace', fontSize: 11 }}>{v}</Text>
      ),
    },
    {
      title: 'Name',
      dataIndex: 'name',
      key: 'name',
      width: 150,
      sorter: (a, b) => a.name.localeCompare(b.name),
      render: (v: string) => (
        <Text style={{ color: COLORS.accent, fontWeight: 500, fontSize: 12 }}>{v}</Text>
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
      width: 100,
      sorter: (a, b) => a.mem_rss_kb - b.mem_rss_kb,
      render: (v: number) => (
        <Text style={{ color: COLORS.text, fontSize: 12 }}>{formatMemory(v)}</Text>
      ),
    },
    {
      title: 'Status',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      sorter: (a, b) => a.status.localeCompare(b.status),
      render: (v: string) => (
        <Tag
          style={{
            color: STATUS_COLOR[v] ?? COLORS.textMuted,
            borderColor: STATUS_COLOR[v] ?? COLORS.border,
            background: `${STATUS_COLOR[v] ?? COLORS.textMuted}15`,
            fontSize: 11,
          }}
        >
          {v}
        </Tag>
      ),
    },
    {
      title: 'Command',
      dataIndex: 'cmdline',
      key: 'cmdline',
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
              maxWidth: 260,
            }}
          >
            {v || '—'}
          </Text>
        </Tooltip>
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

        {error && (
          <Alert
            message="API Error"
            description={error}
            type="error"
            showIcon
            style={{ marginBottom: 12 }}
          />
        )}

        <Table
          columns={columns}
          dataSource={rows.map((p) => ({ ...p, key: String(p.pid) }))}
          loading={loading}
          size="small"
          scroll={{ x: 900 }}
          pagination={{ pageSize: 30, showSizeChanger: true, pageSizeOptions: ['20', '30', '50', '100'] }}
          rowClassName={(_, idx) => (idx % 2 === 1 ? 'table-row-alt' : '')}
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
    </div>
  )
}
