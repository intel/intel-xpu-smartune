import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Modal,
  Button,
  Input,
  Table,
  Typography,
  Space,
  Select,
  Alert,
  Tag,
  Tooltip,
  Divider,
} from 'antd'
import { LockOutlined } from '@ant-design/icons'
import type { CustomTagProps } from 'rc-select/lib/BaseSelect'
import type { ColumnsType, TableRowSelection } from 'antd/es/table/interface'
import { COLORS } from '../styles/theme'
import { api } from '../api/client'
import type { AppInfo, DiscoverCandidate } from '../api/types'

const { Text, Paragraph } = Typography

interface Props {
  open: boolean
  // The Manual Control row being edited. null while the modal is closed.
  app: AppInfo | null
  onClose: () => void
  // Fired after a successful save so the parent can refresh the controlled list.
  onSuccess: () => void
}

const SEARCH_DEBOUNCE_MS = 300

const PRIORITY_OPTIONS = [
  { value: 'low', label: 'Low', color: COLORS.green },
  { value: 'medium', label: 'Medium', color: COLORS.yellow },
  { value: 'high', label: 'High', color: COLORS.orange },
  { value: 'critical', label: 'Critical', color: COLORS.red },
]

// Full editor for a controlled app's name-based identity. The app is controlled BY NAME:
// every running process whose program name matches belongs to it, across all the cgroups
// those processes live in. This dialog opens on the app's current names (add + remove),
// with one hard rule: a program name that currently owns a LIVE limit is locked and cannot
// be removed here — dropping it would strand the running throttle behind an entry that no
// longer references it, so it could never be restored. To prune such a name, restore its
// limit first. Saving commits through /app/set_controlled_app_processes, which rewrites
// config.yaml, mirrors the DB metadata, and rebuilds the BPF cache.
export function EditAppProcessesModal({ open, app, onClose, onSuccess }: Props) {
  // Program names that currently have at least one Limited instance — locked from removal.
  // Sourced from the same per-instance rows the controlled table renders, so what is locked
  // here matches exactly what shows as "Limited" in the UI.
  const lockedNames = useMemo(() => {
    const set = new Set<string>()
    for (const row of app?.process_status_rows ?? []) {
      if (row.limit_status === 'Limited') {
        const name = (row.process_name || '').trim()
        if (name) set.add(name)
      }
    }
    return set
  }, [app])
  const isLocked = useCallback(
    (name: string) => lockedNames.has(name.trim()),
    [lockedNames],
  )

  // Configured names with no running process right now. These are NOT garbage: control is
  // by-name and meant to survive restarts, so a stopped app's names are kept by default and
  // match again when it relaunches. We only flag them (dashed) so the user can consciously
  // prune a name whose program is gone for good. Newly-typed/added names are never flagged.
  const staleNames = useMemo(() => {
    const original = new Set((app?.process_names ?? []).map((n) => n.trim().toLowerCase()))
    const running = new Set<string>()
    for (const row of app?.process_status_rows ?? []) {
      if (row.runtime_status === 'Running') {
        const name = (row.process_name || '').trim().toLowerCase()
        if (name) running.add(name)
      }
    }
    const lockedLower = new Set(Array.from(lockedNames, (n) => n.toLowerCase()))
    const stale = new Set<string>()
    for (const name of original) {
      if (!running.has(name) && !lockedLower.has(name)) stale.add(name)
    }
    return stale
  }, [app, lockedNames])
  const isStale = useCallback(
    (name: string) => staleNames.has(name.trim().toLowerCase()),
    [staleNames],
  )

  // The full desired identity lists (add + remove), seeded from the app on open.
  const [processNames, setProcessNames] = useState<string[]>([])
  const [bpfNames, setBpfNames] = useState<string[]>([])
  const [priority, setPriority] = useState('medium')

  // ---------- live /proc discovery (same flow as the Add-App wizard) ----------
  const [searchInput, setSearchInput] = useState('')
  const [candidates, setCandidates] = useState<DiscoverCandidate[]>([])
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const searchSeq = useRef(0)

  const [extracting, setExtracting] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Reset to the app's current identities each time the dialog opens, so a previous edit's
  // staging never bleeds into the next app.
  useEffect(() => {
    if (!open) return
    setProcessNames(app?.process_names ?? [])
    setBpfNames(app?.bpf_name ?? [])
    setPriority((app?.priority ?? 'medium').toLowerCase())
    setSearchInput('')
    setCandidates([])
    setSearching(false)
    setSearchError(null)
    setExtracting(false)
    setSubmitting(false)
    setError(null)
    if (searchTimer.current) {
      clearTimeout(searchTimer.current)
      searchTimer.current = null
    }
  }, [open, app])

  const runSearch = useCallback(async (raw: string) => {
    const keywords = raw.toLowerCase().split(/[\s,]+/).map((k) => k.trim()).filter(Boolean)
    if (keywords.length === 0) {
      setCandidates([])
      setSearchError(null)
      return
    }
    const mySeq = ++searchSeq.current
    setSearching(true)
    setSearchError(null)
    try {
      const res = await api.discoverSearch(keywords)
      if (mySeq !== searchSeq.current) return
      setCandidates(res.candidates ?? [])
    } catch (e) {
      if (mySeq !== searchSeq.current) return
      setSearchError(e instanceof Error ? e.message : 'Search failed')
      setCandidates([])
    } finally {
      if (mySeq === searchSeq.current) setSearching(false)
    }
  }, [])

  useEffect(() => {
    if (!open) return
    if (searchTimer.current) clearTimeout(searchTimer.current)
    searchTimer.current = setTimeout(() => runSearch(searchInput), SEARCH_DEBOUNCE_MS)
    return () => {
      if (searchTimer.current) clearTimeout(searchTimer.current)
    }
  }, [searchInput, open, runSearch])

  // Removal guard: locked (currently-limited) names are force-kept even if the Select's
  // change event tries to drop them (e.g. Backspace). Their close icon is also hidden by
  // the custom tag renderer below, so this is a belt-and-braces invariant.
  const handleProcessNamesChange = useCallback((next: string[]) => {
    const cleaned = next.map((n) => n.trim()).filter(Boolean)
    const kept = new Set(cleaned.map((n) => n.toLowerCase()))
    const forced = [...cleaned]
    for (const locked of lockedNames) {
      if (!kept.has(locked.toLowerCase())) forced.unshift(locked)
    }
    setProcessNames(Array.from(new Set(forced)))
  }, [lockedNames])

  const renderProcessTag = useCallback((props: CustomTagProps) => {
    const { label, value, onClose } = props
    const name = String(value)
    if (isLocked(name)) {
      return (
        <Tooltip title="This process is currently limited. Restore its limit first to remove it.">
          <Tag icon={<LockOutlined />} color="warning" style={{ marginInlineEnd: 4 }}>
            {label}
          </Tag>
        </Tooltip>
      )
    }
    if (isStale(name)) {
      return (
        <Tooltip title="No running process matches this name right now. It is kept as the app's identity and will match again if the program restarts — remove it only if this program is gone for good.">
          <Tag
            closable
            onClose={onClose}
            style={{ marginInlineEnd: 4, borderStyle: 'dashed', color: COLORS.textMuted }}
          >
            {label}
          </Tag>
        </Tooltip>
      )
    }
    return (
      <Tag closable onClose={onClose} style={{ marginInlineEnd: 4 }}>
        {label}
      </Tag>
    )
  }, [isLocked, isStale])

  // A discovery-table selection immediately becomes an identity. Removal remains explicit
  // through the Program names tag's close button, so unchecking a row cannot drop an identity.
  const extractCandidates = useCallback(async (rows: DiscoverCandidate[]) => {
    const pids = rows.map((row) => row.pid)
    if (pids.length === 0) return
    setExtracting(true)
    setError(null)
    try {
      const res = await api.discoverExtract(pids, app?.app_name?.trim() || '')
      const foldIn = (prev: string[], incoming: string[] | undefined) => {
        const seen = new Set(prev.map((n) => n.toLowerCase()))
        const merged = [...prev]
        for (const raw of incoming ?? []) {
          const name = raw.trim()
          const key = name.toLowerCase()
          if (name && !seen.has(key)) {
            seen.add(key)
            merged.push(name)
          }
        }
        return merged
      }
      setProcessNames((prev) => foldIn(prev, res.process_names))
      setBpfNames((prev) => foldIn(prev, res.bpf_name))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to read process identities')
    } finally {
      setExtracting(false)
    }
  }, [app])

  const rowSelection: TableRowSelection<DiscoverCandidate> = {
    onSelect: (record, checked) => {
      if (checked) void extractCandidates([record])
    },
    onSelectAll: (checked, _rows, changeRows) => {
      if (checked) void extractCandidates(changeRows)
    },
    getCheckboxProps: () => ({ disabled: extracting }),
  }

  // What actually changed vs. the app's stored identities — drives the Save button and the
  // no-op guard. Order-insensitive, case-insensitive.
  const identityDirty = useMemo(() => {
    const norm = (xs: string[]) => Array.from(new Set(xs.map((x) => x.trim().toLowerCase()))).sort()
    const eq = (a: string[], b: string[]) => {
      const na = norm(a), nb = norm(b)
      return na.length === nb.length && na.every((v, i) => v === nb[i])
    }
    return !eq(processNames, app?.process_names ?? []) || !eq(bpfNames, app?.bpf_name ?? [])
  }, [processNames, bpfNames, app])

  const priorityDirty = priority !== (app?.priority ?? 'medium').toLowerCase()
  const canSave = (identityDirty && processNames.length > 0) || priorityDirty

  const submit = useCallback(async () => {
    if (!app || !canSave) return
    setSubmitting(true)
    setError(null)
    try {
      if (identityDirty) {
        await api.setControlledAppProcesses({
          id: app.app_id,
          process_names: processNames,
          bpf_name: bpfNames,
        })
      }
      if (priorityDirty) {
        await api.setPriority({ app_id: app.app_id, priority })
      }
      onSuccess()
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save process identities')
    } finally {
      setSubmitting(false)
    }
  }, [app, canSave, identityDirty, processNames, bpfNames, priorityDirty, priority, onSuccess, onClose])

  const candidateColumns: ColumnsType<DiscoverCandidate> = useMemo(
    () => [
      { title: 'PID', dataIndex: 'pid', key: 'pid', width: 80 },
      {
        title: 'Program Name',
        dataIndex: 'comm',
        key: 'comm',
        width: 160,
        render: (_v: string, record) => {
          const label = (record.process_name || '').trim() || (record.comm || '').trim() || '-'
          const raw = (record.comm || '').trim()
          return <Text code title={raw ? `comm: ${raw}` : undefined}>{label}</Text>
        },
      },
      {
        title: 'Executable',
        dataIndex: 'exe',
        key: 'exe',
        ellipsis: true,
        render: (v: string) => <Text style={{ fontSize: 12 }}>{v || '-'}</Text>,
      },
      {
        title: 'Cmdline',
        dataIndex: 'cmdline',
        key: 'cmdline',
        ellipsis: true,
        render: (v: string) => <Text style={{ fontSize: 12 }}>{v || '-'}</Text>,
      },
    ],
    [],
  )

  const emptyHint = useMemo(() => {
    if (searching) return 'Scanning /proc...'
    if (!searchInput.trim()) return 'Type part of a program name above to find running processes to add.'
    return 'No processes matched. Make sure the application is running, then refine the keyword.'
  }, [searching, searchInput])

  const footer = [
    <Button key="cancel" onClick={onClose}>Cancel</Button>,
    <Button
      key="save"
      type="primary"
      loading={submitting}
      disabled={!canSave}
      onClick={submit}
    >
      Save
    </Button>,
  ]

  return (
    <Modal
      title={app ? `Edit application — ${app.app_name || app.app_id}` : 'Edit application'}
      open={open}
      onCancel={onClose}
      width={900}
      footer={footer}
      destroyOnClose
      maskClosable={false}
    >
      <Paragraph type="secondary" style={{ marginTop: 0 }}>
        This app is controlled <b>by name</b>: every running process whose program name
        matches belongs to it, across all the cgroups they live in. Add or remove names to
        change what this app controls. A <LockOutlined /> name currently owns a{' '}
        <Text strong>live limit</Text> — restore it first to remove. A{' '}
        <Text style={{ borderBottom: `1px dashed ${COLORS.textMuted}` }}>dashed</Text> name has
        no running process right now; it is kept as identity and matches again on restart —
        remove it only if the program is gone for good.
      </Paragraph>

      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <div>
          <Text>Priority</Text>
          <Select
            value={priority}
            onChange={setPriority}
            style={{ width: '100%', marginTop: 4 }}
          >
            {PRIORITY_OPTIONS.map((option) => (
              <Select.Option key={option.value} value={option.value}>
                <span style={{ color: option.color }}>{option.label}</span>
              </Select.Option>
            ))}
          </Select>
        </div>

        <div>
          <Text>
            Program names <Text type="danger">*</Text>{' '}
            <Text type="secondary" style={{ fontSize: 12 }}>
              (this app's identity; script basename for python/bash; used by pgrep matching)
            </Text>
          </Text>
          <Select
            mode="multiple"
            value={processNames}
            onChange={handleProcessNamesChange}
            tagRender={renderProcessTag}
            open={false}
            showArrow={false}
            showSearch={false}
            style={{ width: '100%', marginTop: 4 }}
            placeholder="Select running processes below to add program names"
          />
        </div>
      </Space>

      <Divider style={{ margin: '16px 0' }}>Discover running processes</Divider>

      <div style={{ marginBottom: 12 }}>
        <Input.Search
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          placeholder="Type part of the program name/exe/cmdline; separate multiple keywords with space"
          loading={searching}
          allowClear
        />
      </div>

      {searchError && <Alert type="error" message={searchError} style={{ marginBottom: 12 }} />}

      <Table
        rowKey="pid"
        size="small"
        loading={searching}
        dataSource={candidates}
        columns={candidateColumns}
        rowSelection={rowSelection}
        pagination={{ pageSize: 6, hideOnSinglePage: true }}
        locale={{ emptyText: emptyHint }}
      />

      {error && <Alert type="error" message={error} style={{ marginTop: 12 }} />}
    </Modal>
  )
}
