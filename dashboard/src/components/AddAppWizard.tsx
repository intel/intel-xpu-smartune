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
  onSuccess: (result?: { appId: string; appName: string; openLimit?: boolean }) => void
  // Pre-fill the process search (and app name) when opened from the Processes tab.
  initialKeyword?: string
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

export function AddAppWizard({ open, onClose, onSuccess, initialKeyword }: Props) {
  const [step, setStep] = useState(STEP_PICK)

  // Step 1 — app name + live process search/multi-select
  const [appName, setAppName] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [candidates, setCandidates] = useState<DiscoverCandidate[]>([])
  // Selected processes form a "basket" that persists across multiple keyword
  // searches, keyed by pid so a pick stays visible/removable even after the
  // search that surfaced it has been replaced by a different query.  Only the
  // pids reach the backend; we keep the full candidate so the basket can show
  // comm/pid and so we don't need the process to still be in the results.
  const [selected, setSelected] = useState<Record<number, DiscoverCandidate>>({})
  const selectedPids = useMemo(
    () => Object.keys(selected).map(Number),
    [selected],
  )
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
  const [extracting, setExtracting] = useState(false)
  const [extractError, setExtractError] = useState<string | null>(null)

  // Step 3 — commit
  const [committing, setCommitting] = useState(false)
  const [commitError, setCommitError] = useState<string | null>(null)
  const [committed, setCommitted] = useState(false)
  const [openLimitAfterAdd, setOpenLimitAfterAdd] = useState(false)
  const [committedApp, setCommittedApp] = useState<{ appId: string; appName: string } | null>(null)
  // Conflict state — set when the backend rejects with retcode CONFLICT.
  const [conflict, setConflict] = useState<{
    kind: 'id' | 'name' | 'processes'
    withName: string
    withId: string
    message: string
  } | null>(null)
  const [merging, setMerging] = useState(false)
  const [purging, setPurging] = useState(false)

  const reset = useCallback(() => {
    setStep(STEP_PICK)
    setAppName('')
    setSearchInput('')
    setCandidates([])
    setSelected({})
    setSearching(false)
    setSearchError(null)
    setAppId('')
    setPriority('low')
    setRemark('')
    setBpfNames([])
    setProcessNames([])
    setCommandline('')
    setExtracting(false)
    setExtractError(null)
    setCommitting(false)
    setCommitError(null)
    setCommitted(false)
    setOpenLimitAfterAdd(false)
    setCommittedApp(null)
    setConflict(null)
    setMerging(false)
    setPurging(false)
    if (searchTimer.current) {
      clearTimeout(searchTimer.current)
      searchTimer.current = null
    }
  }, [])

  const handleClose = useCallback((openLimitNow = false) => {
    // No toast on close — the Done step inside the modal already confirmed
    // success, and the user can see the new row appear in the table behind
    // the dialog.  Just refresh the parent and dismiss.
    if (committed && committedApp) {
      onSuccess({
        appId: committedApp.appId,
        appName: committedApp.appName,
        openLimit: openLimitNow || openLimitAfterAdd,
      })
    }
    reset()
    onClose()
  }, [committed, committedApp, openLimitAfterAdd, onClose, onSuccess, reset])

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
      // Replace only the browse list; the selection basket persists across
      // searches so the user can accumulate processes from several keywords.
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
    if (!open || step !== STEP_PICK) return
    if (searchTimer.current) clearTimeout(searchTimer.current)
    searchTimer.current = setTimeout(() => runSearch(searchInput), SEARCH_DEBOUNCE_MS)
    return () => {
      if (searchTimer.current) clearTimeout(searchTimer.current)
    }
  }, [searchInput, open, step, runSearch])

  // Pre-fill the search + app name when opened with a keyword (from Processes tab).
  useEffect(() => {
    if (open && initialKeyword) {
      setSearchInput(initialKeyword)
      setAppName(initialKeyword)
    }
  }, [open, initialKeyword])

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
        setCommittedApp({ appId: res.data.id, appName: res.data.name })
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

  const mergeProcesses = useCallback(async () => {
    if (!conflict?.withId) return
    setMerging(true)
    setCommitError(null)
    try {
      const result = await api.mergeControlledAppProcesses({
        id: conflict.withId,
        process_names: processNames,
        bpf_name: bpfNames,
      })
      setConflict(null)
      setCommittedApp({ appId: result.id, appName: result.name || conflict.withName })
      setCommitted(true)
      setStep(STEP_DONE)
    } catch (e) {
      setCommitError(e instanceof Error ? e.message : 'Could not merge process identities')
    } finally {
      setMerging(false)
    }
  }, [bpfNames, conflict, processNames])

  // ---------- per-step validation ----------
  const step1Valid =
    appName.trim().length > 0 && selectedPids.length > 0 && !searching
  const step2Valid =
    appName.trim().length > 0 &&
    appId.trim().length > 0 &&
    processNames.length > 0

  // ---------- step 1 candidate table ----------
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
          return (
            <Text code title={raw ? `comm: ${raw}` : undefined}>{label}</Text>
          )
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

  const addToBasket = useCallback((rows: DiscoverCandidate[]) => {
    setSelected((prev) => {
      const next = { ...prev }
      for (const r of rows) next[r.pid] = r
      return next
    })
  }, [])

  const removeFromBasket = useCallback((pids: number[]) => {
    setSelected((prev) => {
      const next = { ...prev }
      for (const pid of pids) delete next[pid]
      return next
    })
  }, [])

  // Per-row toggles (onSelect/onSelectAll) rather than onChange: the basket
  // spans multiple searches, so we must add/remove individual rows instead of
  // replacing the whole selection with only the currently-visible keys.
  const rowSelection: TableRowSelection<DiscoverCandidate> = {
    selectedRowKeys: selectedPids,
    onSelect: (record, checked) =>
      checked ? addToBasket([record]) : removeFromBasket([record.pid]),
    onSelectAll: (checked, _rows, changeRows) =>
      checked
        ? addToBasket(changeRows)
        : removeFromBasket(changeRows.map((r) => r.pid)),
  }

  // ---------- footer buttons (one set per step) ----------
  const footer = useMemo(() => {
    if (step === STEP_PICK) {
      return [
        <Button key="cancel" onClick={() => handleClose()}>Cancel</Button>,
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
      <Button key="close" onClick={() => handleClose()}>Close</Button>,
      <Button
        key="limit-now"
        type="primary"
        onClick={() => {
          handleClose(true)
        }}
      >
        Set Limit Now
      </Button>,
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
      onCancel={() => handleClose()}
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
            App name, then type part of its program name in the search box.
            Tick the matching rows — these are just <b>samples</b> used to read
            off the <b>program name(s)</b> this app runs as. The app is then
            controlled <b>by name</b>: every process with a matching name — in
            any terminal, now or after a restart — belongs to this app. If your
            app runs several differently-named processes (e.g. a service plus
            workers), search different keywords and keep ticking; the picks
            accumulate below.
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
              placeholder="Type part of the program name/exe/cmdline; separate multiple keywords with space"
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
                Program names <Text type="danger">*</Text>{' '}
                <Text type="secondary" style={{ fontSize: 12 }}>
                  (this app's identity — every running process with one of these
                  names belongs to it; script basename for python/bash)
                </Text>
              </Text>
              <Select
                mode="tags"
                value={processNames}
                onChange={setProcessNames}
                style={{ width: '100%', marginTop: 4 }}
                placeholder="program / exe basenames"
              />
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
                    Tip: Merge processes keeps the existing app's settings and
                    current limits. Delete and recreate is only for a complete
                    replacement.
                  </Text>
                  <Space>
                    {conflict.kind !== 'name' && (
                      <Button
                        size="small"
                        type="primary"
                        loading={merging}
                        onClick={mergeProcesses}
                      >
                        Merge processes
                      </Button>
                    )}
                    <Button
                      size="small"
                      danger
                      loading={purging}
                      onClick={purgeAndRetry}
                    >
                      Delete and recreate
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
          <Paragraph type="secondary" style={{ marginBottom: 0 }}>
            You can set a unified resource limit now, or close and configure it later.
          </Paragraph>
        </div>
      )}
    </Modal>
  )
}
