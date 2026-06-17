import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Modal,
  Steps,
  Button,
  Input,
  Select,
  Table,
  Typography,
  Space,
  Tag,
  Alert,
} from 'antd'
import type { ColumnsType, TableRowSelection } from 'antd/es/table/interface'
import { COLORS } from '../styles/theme'
import { api } from '../api/client'
import type {
  DiscoverCandidate,
  DiscoverExtractData,
  WizardCommitPayload,
} from '../api/types'

const { Text, Paragraph } = Typography

interface Props {
  open: boolean
  onClose: () => void
  onSuccess: () => void
}

const PRIORITY_OPTIONS = [
  { value: 'low', label: 'Low', color: COLORS.green },
  { value: 'medium', label: 'Medium', color: COLORS.yellow },
  { value: 'high', label: 'High', color: COLORS.orange },
  { value: 'critical', label: 'Critical', color: COLORS.red },
]

// Step 1 collapses the old keyword-input + result-table flow into a single
// type-to-filter view, so the wizard now has three steps instead of four.
const STEP_PICK = 0
const STEP_CONFIRM = 1
const STEP_DONE = 2

const SEARCH_DEBOUNCE_MS = 300

export function AddAppWizard({ open, onClose, onSuccess }: Props) {
  const [step, setStep] = useState(STEP_PICK)

  // Step 1 — app name + live process search/multi-select
  const [appName, setAppName] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [candidates, setCandidates] = useState<DiscoverCandidate[]>([])
  const [selectedPids, setSelectedPids] = useState<number[]>([])
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const searchSeq = useRef(0) // protects against out-of-order debounced responses

  // Step 2 — extracted fields the user can still edit before commit
  const [appId, setAppId] = useState('')
  const [priority, setPriority] = useState<string>('low')
  const [remark, setRemark] = useState('')
  const [bpfNames, setBpfNames] = useState<string[]>([])
  const [processNames, setProcessNames] = useState<string[]>([])
  // commandline is stored / saved as a single string.  The wizard
  // additionally remembers the *other* argv[0] values surfaced by
  // discover_extract so the user can see them as suggestions, but only the
  // value in `commandline` ever reaches the backend.
  const [commandline, setCommandline] = useState<string>('')
  const [commandlineSuggestions, setCommandlineSuggestions] = useState<string[]>([])
  const [extracting, setExtracting] = useState(false)
  const [extractError, setExtractError] = useState<string | null>(null)

  // Step 3 — commit
  const [committing, setCommitting] = useState(false)
  const [commitError, setCommitError] = useState<string | null>(null)
  const [committed, setCommitted] = useState(false)
  // Conflict state — set when the backend rejects with retcode CONFLICT.
  // Holds the existing app's id so we can offer a "purge & re-add" button.
  const [conflict, setConflict] = useState<{
    kind: 'id' | 'name' | 'processes'
    withName: string
    withId: string
    message: string
  } | null>(null)
  const [purging, setPurging] = useState(false)

  const reset = useCallback(() => {
    setStep(STEP_PICK)
    setAppName('')
    setSearchInput('')
    setCandidates([])
    setSelectedPids([])
    setSearching(false)
    setSearchError(null)
    setAppId('')
    setPriority('low')
    setRemark('')
    setBpfNames([])
    setProcessNames([])
    setCommandline('')
    setCommandlineSuggestions([])
    setExtracting(false)
    setExtractError(null)
    setCommitting(false)
    setCommitError(null)
    setCommitted(false)
    setConflict(null)
    setPurging(false)
    if (searchTimer.current) {
      clearTimeout(searchTimer.current)
      searchTimer.current = null
    }
  }, [])

  const handleClose = useCallback(() => {
    // No toast on close — the Done step inside the modal already confirmed
    // success, and the user can see the new row appear in the table behind
    // the dialog.  Just refresh the parent and dismiss.
    if (committed) onSuccess()
    reset()
    onClose()
  }, [committed, onClose, onSuccess, reset])

  // ---------- live search ----------
  // Debounce the searchInput → discoverSearch call so that typing doesn't
  // hammer the backend.  Each request increments searchSeq so a slow earlier
  // response can't overwrite a faster later one.
  const runSearch = useCallback(async (raw: string) => {
    // Lower-case before sending — the backend already does case-insensitive
    // matching, but mobile/IME inputs sometimes auto-capitalize the first
    // letter and we want "Calculator" and "calculator" to behave the same.
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
      const stillVisible = new Set(res.candidates.map((c) => c.pid))
      setSelectedPids((prev) => prev.filter((pid) => stillVisible.has(pid)))
    } catch (e) {
      if (mySeq !== searchSeq.current) return
      setSearchError(e instanceof Error ? e.message : 'Search failed')
      setCandidates([])
    } finally {
      if (mySeq === searchSeq.current) setSearching(false)
    }
  }, [])

  useEffect(() => {
    if (!open || step !== STEP_PICK) return
    if (searchTimer.current) clearTimeout(searchTimer.current)
    searchTimer.current = setTimeout(() => runSearch(searchInput), SEARCH_DEBOUNCE_MS)
    return () => {
      if (searchTimer.current) clearTimeout(searchTimer.current)
    }
  }, [searchInput, open, step, runSearch])

  // ---------- step transitions ----------
  const goToConfirm = useCallback(async () => {
    setExtracting(true)
    setExtractError(null)
    try {
      const res: DiscoverExtractData = await api.discoverExtract(selectedPids, appName.trim())
      setBpfNames(res.bpf_name ?? [])
      setProcessNames(res.process_names ?? [])
      const cmds = res.commandline ?? []
      setCommandline(cmds[0] ?? '')
      setCommandlineSuggestions(cmds.slice(1))
      // Backend returns id_suggestion as either the shared systemd unit or
      // <slug-of-name>.id as a fallback.  Only auto-fill if the user hasn't
      // already typed an id manually.
      if (!appId && res.id_suggestion) setAppId(res.id_suggestion)
      setStep(STEP_CONFIRM)
    } catch (e) {
      setExtractError(e instanceof Error ? e.message : 'Extract failed')
    } finally {
      setExtracting(false)
    }
  }, [selectedPids, appName, appId])

  const commit = useCallback(async () => {
    setCommitting(true)
    setCommitError(null)
    setConflict(null)
    try {
      const payload: WizardCommitPayload = {
        name: appName.trim(),
        id: appId.trim(),
        priority,
        remark: remark.trim(),
        commandline: commandline.trim(),
        bpf_name: bpfNames,
        process_names: processNames,
      }
      const res = await api.newControlledApp(payload)
      if (res.status === 'ok') {
        setCommitted(true)
        setStep(STEP_DONE)
      } else if (res.status === 'conflict') {
        setConflict({
          kind: res.conflict,
          withName: res.withName,
          withId: res.withId,
          message: res.message,
        })
      } else {
        setCommitError(res.message || 'Commit failed')
      }
    } catch (e) {
      setCommitError(e instanceof Error ? e.message : 'Commit failed')
    } finally {
      setCommitting(false)
    }
  }, [appName, appId, priority, remark, commandline, bpfNames, processNames])

  // Conflict resolution path: purge the existing entry the backend pointed
  // us at, then re-run the original commit.  Triggered by the "Purge & re-add"
  // button surfaced inside the conflict alert.
  const purgeAndRetry = useCallback(async () => {
    if (!conflict?.withId) return
    setPurging(true)
    setCommitError(null)
    try {
      await api.purgeControlledApp(conflict.withId)
      setConflict(null)
      // Trigger a refresh of the parent dashboard so the now-deleted app
      // disappears from the controlled-apps table even if the user backs
      // out without finishing the wizard.
      onSuccess()
      await commit()
    } catch (e) {
      setCommitError(e instanceof Error ? e.message : 'Purge failed')
    } finally {
      setPurging(false)
    }
  }, [conflict, commit, onSuccess])

  // ---------- per-step validation ----------
  const step1Valid =
    appName.trim().length > 0 && selectedPids.length > 0 && !searching
  const step2Valid =
    appName.trim().length > 0 &&
    appId.trim().length > 0 &&
    bpfNames.length > 0

  // ---------- step 1 candidate table ----------
  const candidateColumns: ColumnsType<DiscoverCandidate> = useMemo(
    () => [
      { title: 'PID', dataIndex: 'pid', key: 'pid', width: 80 },
      {
        title: 'comm',
        dataIndex: 'comm',
        key: 'comm',
        width: 160,
        render: (v: string) => <Text code>{v}</Text>,
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
      {
        title: 'cgroup unit',
        dataIndex: 'cgroup_unit',
        key: 'cgroup_unit',
        width: 220,
        ellipsis: true,
        render: (v: string) =>
          v ? <Tag color="blue">{v}</Tag> : <Text type="secondary">-</Text>,
      },
    ],
    [],
  )

  const rowSelection: TableRowSelection<DiscoverCandidate> = {
    selectedRowKeys: selectedPids,
    onChange: (keys) => setSelectedPids(keys.map((k) => Number(k))),
  }

  // ---------- footer buttons (one set per step) ----------
  const footer = useMemo(() => {
    if (step === STEP_PICK) {
      return [
        <Button key="cancel" onClick={handleClose}>Cancel</Button>,
        <Button
          key="next"
          type="primary"
          loading={extracting}
          disabled={!step1Valid}
          onClick={goToConfirm}
        >
          Next
        </Button>,
      ]
    }
    if (step === STEP_CONFIRM) {
      return [
        <Button key="back" onClick={() => setStep(STEP_PICK)}>Back</Button>,
        <Button
          key="finish"
          type="primary"
          loading={committing}
          disabled={!step2Valid}
          onClick={commit}
        >
          Finish
        </Button>,
      ]
    }
    return [
      <Button key="close" type="primary" onClick={handleClose}>Close</Button>,
    ]
  }, [
    step, step1Valid, step2Valid,
    extracting, committing,
    goToConfirm, commit, handleClose,
  ])

  const emptyHint = useMemo(() => {
    if (searching) return 'Scanning /proc...'
    if (!searchInput.trim()) return 'Type part of the app name above to start matching running processes.'
    return `No processes matched. Make sure the application is running, then refine the keyword.`
  }, [searching, searchInput])

  return (
    <Modal
      title="Add Application Wizard"
      open={open}
      onCancel={handleClose}
      width={900}
      footer={footer}
      destroyOnClose
      maskClosable={false}
    >
      <Steps
        current={step}
        size="small"
        items={[
          { title: 'Pick processes' },
          { title: 'Confirm' },
          { title: 'Done' },
        ]}
        style={{ marginBottom: 24 }}
      />

      {step === STEP_PICK && (
        <div>
          <Paragraph>
            Make sure the application is <b>currently running</b>, give it an
            App name, then type part of its process name in the search box —
            matching processes appear below.  Multi-select all processes that
            belong to this application (multiple cgroups are fine).
          </Paragraph>

          <div style={{ marginBottom: 12 }}>
            <Text>App name</Text>
            <Input
              value={appName}
              onChange={(e) => setAppName(e.target.value)}
              placeholder="The label shown in the controlled-apps table"
              style={{ marginTop: 4 }}
            />
          </div>

          <div style={{ marginBottom: 12 }}>
            <Text>Search processes</Text>
            <Input.Search
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder="Type part of the comm/exe/cmdline; separate multiple keywords with space"
              loading={searching}
              allowClear
              style={{ marginTop: 4 }}
            />
          </div>

          {searchError && (
            <Alert type="error" message={searchError} style={{ marginBottom: 12 }} />
          )}
          <Table
            rowKey="pid"
            size="small"
            loading={searching}
            dataSource={candidates}
            columns={candidateColumns}
            rowSelection={rowSelection}
            pagination={{ pageSize: 8, hideOnSinglePage: true }}
            locale={{ emptyText: emptyHint }}
          />
        </div>
      )}

      {step === STEP_CONFIRM && (
        <div>
          {extractError && (
            <Alert type="error" message={extractError} style={{ marginBottom: 12 }} />
          )}
          <Paragraph>
            These fields were extracted from the selected processes. You can
            still edit them before saving.
          </Paragraph>

          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <div>
              <Text>App name</Text>
              <Input
                value={appName}
                onChange={(e) => setAppName(e.target.value)}
                style={{ marginTop: 4 }}
              />
            </div>

            <div>
              <Text>
                Unique id <Text type="danger">*</Text>{' '}
                <Text type="secondary" style={{ fontSize: 12 }}>
                  (DB primary key; used for systemd unit matching when limits apply)
                </Text>
              </Text>
              <Input
                value={appId}
                onChange={(e) => setAppId(e.target.value)}
                style={{ marginTop: 4 }}
              />
            </div>

            <div>
              <Text>Priority</Text>
              <Select
                value={priority}
                onChange={setPriority}
                style={{ width: '100%', marginTop: 4 }}
              >
                {PRIORITY_OPTIONS.map((p) => (
                  <Select.Option key={p.value} value={p.value}>
                    <span style={{ color: p.color }}>{p.label}</span>
                  </Select.Option>
                ))}
              </Select>
            </div>

            <div>
              <Text>
                Remark{' '}
                <Text type="secondary" style={{ fontSize: 12 }}>
                  (optional note shown in the controlled-apps table)
                </Text>
              </Text>
              <Input
                value={remark}
                onChange={(e) => setRemark(e.target.value)}
                placeholder="e.g. dev workstation only"
                style={{ marginTop: 4 }}
              />
            </div>

            <div>
              <Text>
                bpf_name <Text type="danger">*</Text>{' '}
                <Text type="secondary" style={{ fontSize: 12 }}>
                  (BPF watches these comm names — note 15-byte truncation)
                </Text>
              </Text>
              <Select
                mode="tags"
                value={bpfNames}
                onChange={setBpfNames}
                style={{ width: '100%', marginTop: 4 }}
                placeholder="comm names"
              />
            </div>

            <div>
              <Text>
                process_names{' '}
                <Text type="secondary" style={{ fontSize: 12 }}>
                  (executable basenames; required only for multi-cgroup apps)
                </Text>
              </Text>
              <Select
                mode="tags"
                value={processNames}
                onChange={setProcessNames}
                style={{ width: '100%', marginTop: 4 }}
                placeholder="exe basenames"
              />
            </div>

            <div>
              <Text>
                commandline{' '}
                <Text type="secondary" style={{ fontSize: 12 }}>
                  (used by pgrep when adjusting OOM score)
                </Text>
              </Text>
              <Input
                value={commandline}
                onChange={(e) => setCommandline(e.target.value)}
                placeholder="argv[0] of the main process"
                style={{ marginTop: 4 }}
              />
              {commandlineSuggestions.length > 0 && (
                <Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 4 }}>
                  Other detected: {commandlineSuggestions.map((s, i) => (
                    <span key={s}>
                      {i > 0 && ', '}
                      <a onClick={() => setCommandline(s)}>{s}</a>
                    </span>
                  ))}
                </Text>
              )}
            </div>
          </Space>

          {conflict && (
            <Alert
              type="warning"
              showIcon
              style={{ marginTop: 12 }}
              message="This app may already be configured"
              description={
                <Space direction="vertical" size="small" style={{ width: '100%' }}>
                  <Text>{conflict.message}</Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    Tip: if you only want to re-enable monitoring of an existing
                    app, close this dialog and pick it from the Application
                    dropdown above instead. Use Purge &amp; re-add only when you
                    want to reconfigure it from scratch.
                  </Text>
                  <Space>
                    <Button
                      size="small"
                      danger
                      loading={purging}
                      onClick={purgeAndRetry}
                    >
                      Purge &amp; re-add
                    </Button>
                    <Button size="small" onClick={() => setConflict(null)}>
                      Dismiss
                    </Button>
                  </Space>
                </Space>
              }
            />
          )}
          {commitError && (
            <Alert type="error" message={commitError} style={{ marginTop: 12 }} />
          )}
        </div>
      )}

      {step === STEP_DONE && (
        <div style={{ padding: 24, textAlign: 'center' }}>
          <Paragraph>
            <Text strong>Application "{appName}" added.</Text>
          </Paragraph>
          <Paragraph type="secondary">
            The new entry has been written to <Text code>config.yaml</Text>{' '}
            and the BPF match cache was refreshed. Closing this dialog will
            refresh the controlled-apps list.
          </Paragraph>
        </div>
      )}
    </Modal>
  )
}
