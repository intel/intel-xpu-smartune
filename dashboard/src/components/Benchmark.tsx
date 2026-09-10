// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// Benchmark tab: browse the models that have an OpenVINO conversion, download
// one, benchmark it, read the numbers.
//
// The page is a model browser (left) plus the selected model and its results
// (right). The backend runs at most one job at a time -- setup and runs share a
// single slot -- so there is no job list: there is what is running now, and
// there are the results of everything that ran before.
//
// Nothing here polls. State arrives on the SSE stream App holds open
// (hooks/useBenchEvents, benchmark/service/events.py): a snapshot on connect,
// then job/log/env/models/results events as they happen. REST is used for the
// three things a push cannot sensibly carry -- the model list, the results
// tables, and log spans the stream could not deliver contiguously -- and for
// starting and cancelling jobs.

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Card, Modal, Space, Tabs, Tag, Tooltip, Typography, message } from 'antd'
import {
  CheckCircleTwoTone,
  CloudDownloadOutlined,
  LeftOutlined,
  PlayCircleOutlined,
  RightOutlined,
  ToolOutlined,
} from '@ant-design/icons'

import { api } from '../api/client'
import type {
  BenchDevice,
  BenchEnvData,
  BenchJob,
  BenchLogDelta,
  BenchMatrixData,
  BenchMatrixRow,
  BenchModel,
  BenchModelsState,
  BenchPrecision,
  BenchPreflightBlocker,
  BenchPreflightData,
  BenchStage,
} from '../api/types'
import { useBenchEvent } from '../hooks/useBenchEvents'
import BenchCaseDrawer from './BenchCaseDrawer'
import BenchCompare from './BenchCompare'
import BenchEnvDrawer, { EnvStatusTag, versionSummary } from './BenchEnvDrawer'
import BenchModelDetail, { missingPrecisions } from './BenchModelDetail'
import BenchModelList, { type ModelFilter } from './BenchModelList'
import BenchOutput, { JobStatusTag } from './BenchOutput'
import BenchResults from './BenchResults'
import BenchRerunModal, { type RerunConflict } from './BenchRerunModal'
import { COLORS } from '../styles/theme'
import { matchesModel } from '../utils/benchMetrics'

const { Text, Paragraph } = Typography

// Keep the on-screen log bounded — a full build writes megabytes and the browser
// slows to a crawl long before a user scrolls that far back. The whole log
// remains on disk at job.log_path.
const MAX_LOG_CHARS = 400_000

const SIDER_WIDTH = 320

// Benchmark Results is what ran, Analysis is how the runs stack up, Logs is the
// live output. The keys keep the old short names: they are state, not labels.
type MainTab = 'results' | 'compare' | 'output'

/** A run held back until the user says what to do with the results it repeats. */
interface PendingRun {
  stage: BenchStage
  ids: string[]
  conflicts: RerunConflict[]
}

interface BenchmarkProps {
  /** Switch the app to the Balancer tab. Absent in monitor-only mode. */
  onOpenBalance?: () => void
}

/**
 * Which of the requested (model, precision, device) triples have been measured.
 *
 * Results name a model by the directory-safe form the pipeline gave it, so the
 * comparison goes through matchesModel rather than string equality. Failed
 * cases count: a case that ran and produced nothing is still a case on disk,
 * and it is the one most likely to be worth deleting before running again.
 */
function findConflicts(
  rows: BenchMatrixRow[],
  ids: string[],
  precisions: BenchPrecision[],
  devices: BenchDevice[],
): RerunConflict[] {
  const conflicts: RerunConflict[] = []
  for (const modelId of ids) {
    const forModel = rows.filter((row) => matchesModel(row.model, modelId))
    if (forModel.length === 0) continue
    for (const precision of precisions) {
      for (const device of devices) {
        const matching = forModel.filter(
          (row) => row.precision === precision && row.device === device,
        )
        if (matching.length) {
          conflicts.push({
            modelId,
            precision,
            device,
            rows: [...matching].sort((a, b) => b.updated_at - a.updated_at),
          })
        }
      }
    }
  }
  return conflicts
}

/**
 * One line naming the apps a preflight blocker is about, or its bare reason.
 *
 * Names, not counts: "resolve two apps" sends the reader hunting through the
 * Balancer page, and the whole point of surfacing this before the click is that
 * they know what to go and do.
 */
function blockerLine(blocker: BenchPreflightBlocker): string {
  const apps = (blocker.apps ?? [])
    .map((app) => app.app_name || app.app_id)
    .filter(Boolean) as string[]
  const reason = blocker.reason || blocker.name
  return apps.length ? `${reason} (${apps.join(', ')})` : reason
}

// No `active` prop: the stream App holds keeps this component current whether or
// not the tab is on screen, which is the whole point -- coming back to a
// finished run is instant, with nothing to re-fetch.
export default function Benchmark({ onOpenBalance }: BenchmarkProps) {
  const [env, setEnv] = useState<BenchEnvData | null>(null)
  const [envError, setEnvError] = useState<string | null>(null)
  const [envOpen, setEnvOpen] = useState(false)

  const [models, setModels] = useState<BenchModel[]>([])
  const [modelsState, setModelsState] = useState<BenchModelsState | null>(null)
  const [modelsLoading, setModelsLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<ModelFilter>('all')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [checkedIds, setCheckedIds] = useState<string[]>([])
  const [precisions, setPrecisions] = useState<BenchPrecision[]>(['int4'])
  // All three by default, which is what the pipeline swept before the request
  // could name devices at all.
  const [devices, setDevices] = useState<BenchDevice[]>(['cpu', 'gpu', 'npu'])
  // The OpenVINO version a benchmark builds/runs against. Free text (a version
  // not yet built is built on demand); seeded from the reference list once the
  // environment arrives, and only if the user has not typed one.
  const [ov, setOv] = useState<string>('')
  // Free-form extra CLI arguments appended to every benchmark run_case. Applies
  // to the whole run, like `ov` and the device selection; blank by default and
  // ignored by a pure build/download.
  const [args, setArgs] = useState<string>('')

  const [job, setJob] = useState<BenchJob | null>(null)
  // Mirrors `job` for the log handler, which needs to know which job's log it is
  // fetching a missing span of without that knowledge being a render input.
  const jobRef = useRef<BenchJob | null>(null)
  const [log, setLog] = useState('')
  const [logTruncated, setLogTruncated] = useState(false)
  // Byte offset of the end of what `log` holds, and which job it belongs to.
  // Refs, not state: every log delta consults them, and re-rendering to store a
  // number the render does not display would be a render per line of output.
  const logOffset = useRef(0)
  const logJobId = useRef<string | null>(null)
  const catchingUp = useRef(false)

  const [matrix, setMatrix] = useState<BenchMatrixData | null>(null)
  // Whether the model pane has been pointed at the newest run yet. A ref, and
  // never reset: it guards a one-time courtesy, and a second attempt after the
  // user has picked a model would be a UI moving on its own.
  const seededSelection = useRef(false)
  const [caseRow, setCaseRow] = useState<BenchMatrixRow | null>(null)
  const [caseOpen, setCaseOpen] = useState(false)
  // A run the user asked for that would repeat work already on disk, parked
  // here while they answer what should happen to the previous results.
  const [rerun, setRerun] = useState<PendingRun | null>(null)
  const [discarding, setDiscarding] = useState(false)
  const [mainTab, setMainTab] = useState<MainTab>('results')
  const [starting, setStarting] = useState(false)
  const [siderOpen, setSiderOpen] = useState(true)
  // Whether a measured run could start right now. A measured run takes the
  // machine quiet for its duration, and cannot do that while the balancer is
  // holding automatic limits on an app -- so the answer is shown here, before
  // the click, rather than being an error the Run button returns.
  const [preflight, setPreflight] = useState<BenchPreflightData | null>(null)

  const busy = job?.status === 'running' || !!env?.busy
  const installing = (env?.busy?.kind ?? (job?.status === 'running' ? job.kind : null)) === 'setup'
  const selected = models.find((m) => m.id === selectedId) ?? null

  // Which models get a detail pane: everything ticked, in list order, plus the
  // highlighted row if it is not ticked. Clicking a row while a batch is ticked
  // must not hide the batch, and ticking must not lose the row being read.
  const detailIds = useMemo(() => {
    const checked = new Set(checkedIds)
    const ids = models.filter((m) => checked.has(m.id)).map((m) => m.id)
    // A ticked model that is no longer in the list (a refresh dropped it) still
    // deserves its pane, so anything unaccounted for is appended.
    checkedIds.forEach((id) => {
      if (!ids.includes(id)) ids.push(id)
    })
    if (selectedId && !ids.includes(selectedId)) ids.push(selectedId)
    return ids
  }, [models, checkedIds, selectedId])

  const activeDetailId =
    selectedId && detailIds.includes(selectedId) ? selectedId : (detailIds[0] ?? null)

  // "Download all" / "Run all" mean the same thing as a model's own two buttons,
  // decided over the whole batch: the batch can be benchmarked only when every
  // ticked model already has every ticked precision. A model that is checked but
  // no longer in the list counts as missing, which is why this compares lengths
  // rather than just mapping.
  const batchMissing = useMemo<BenchPrecision[]>(() => {
    const checked = models.filter((m) => checkedIds.includes(m.id))
    if (checked.length !== checkedIds.length) return precisions
    return missingPrecisions(checked, precisions)
  }, [models, checkedIds, precisions])

  // OpenVINO versions to suggest, and whether the one in the box is runnable.
  // Already-built columns come first (quick pick of what is on disk), then the
  // static reference versions; a version outside both is allowed (built on
  // demand) as long as it is a bare X.Y.Z.
  const ovOptions = useMemo(() => {
    const built = env?.ov_versions ?? []
    const reference = (env?.ov_reference ?? []).map((v) => v.version)
    return [...new Set([...built, ...reference])]
  }, [env?.ov_versions, env?.ov_reference])
  const ovValid = /^\d+\.\d+\.\d+$/.test(ov.trim())

  // Seed the version box from the newest reference entry once, and only while the
  // user has not typed one -- so it starts populated but never fights the user.
  useEffect(() => {
    if (!ov && ovOptions.length) setOv(ovOptions[0])
  }, [ov, ovOptions])

  // --- REST loads ---------------------------------------------------------

  const loadModels = useCallback(async () => {
    setModelsLoading(true)
    try {
      const data = await api.getBenchModels()
      setModels(data.models)
      setModelsState(data)
    } catch {
      // The model list is advisory: the tab still works off whatever it had, and
      // the refresh button is right there.
    } finally {
      setModelsLoading(false)
    }
  }, [])

  // One reading of the results tree, shared by both views below.
  //
  // The table used to have its own, grouped by run directory, which was the
  // right shape only while a run directory meant a device. Now that every run
  // keeps its own results, the question the table answers -- how did this test
  // do, across the times it was run -- is answered off the flat matrix like
  // everything else, and both views cannot disagree about what is on disk
  // because there is one fetch. /bench/results is still served; nothing reads it.
  const loadResults = useCallback(async () => {
    try {
      setMatrix(await api.getBenchMatrix())
    } catch {
      /* results are best-effort */
    }
  }, [])

  const openCase = useCallback((row: BenchMatrixRow) => {
    setCaseRow(row)
    setCaseOpen(true)
  }, [])

  const loadEnv = useCallback(async () => {
    try {
      setEnv(await api.getBenchEnv())
      setEnvError(null)
    } catch (e) {
      setEnvError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  /**
   * Re-read whether a measured run can start.
   *
   * Not polled. What it reports is decided on the Balancer page, and polling for
   * it would put a periodic request behind a feature whose entire purpose is to
   * keep background activity off the machine during a run. Read on mount, when a
   * job reaches a terminal state, and on the banner's own Re-check -- and the
   * server checks again for real when Run is pressed, so a stale banner can
   * only ever be a stale *hint*.
   */
  const loadPreflight = useCallback(async () => {
    try {
      setPreflight(await api.getBenchPreflight())
    } catch {
      // Advisory: a server that cannot answer must not stop anyone running
      // anything. The block, if there is one, still comes back from Run.
    }
  }, [])

  // One load per mount. The tab is rendered lazily and then kept mounted, so
  // this is also once per session; live updates after it arrive on the stream.
  //
  // env is seeded over REST too, not just from the SSE `snapshot`: the snapshot
  // fires once when App opens the stream, which can be before this component has
  // mounted and subscribed -- in that case the one-shot is missed and env would
  // stay null ("environment not installed") with no way to recover. The REST
  // read is the authoritative fallback; the stream still pushes later changes.
  useEffect(() => {
    void loadEnv()
    void loadModels()
    void loadResults()
    void loadPreflight()
  }, [loadEnv, loadModels, loadResults, loadPreflight])

  // Open on the model the results below are about.
  //
  // Nothing is remembered between restarts, so the tab used to come up with the
  // upper half empty -- a model pane with no model, above a table full of
  // results for one. The most recently benchmarked model is both the best guess
  // at what the reader was last doing and the thing they are looking at.
  //
  // Runs once. It fires when the matrix and the model list have both arrived
  // (either order), and is marked done even when no list entry matches the run's
  // model, so a later list refresh cannot move a selection the user has made.
  useEffect(() => {
    if (seededSelection.current || selectedId || models.length === 0) return
    const rows = matrix?.rows ?? []
    if (rows.length === 0) return
    const newest = rows.reduce((latest, row) => (row.updated_at > latest.updated_at ? row : latest))
    seededSelection.current = true
    const match = models.find((model) => matchesModel(newest.model, model.id))
    if (match) setSelectedId(match.id)
  }, [matrix, models, selectedId])

  // --- log assembly -------------------------------------------------------

  const appendChunk = useCallback((chunk: string, end: number) => {
    logOffset.current = end
    if (!chunk) return
    setLog((prev) => {
      const next = prev + chunk
      return next.length > MAX_LOG_CHARS ? next.slice(next.length - MAX_LOG_CHARS) : next
    })
  }, [])

  /**
   * Pull the span the stream could not deliver contiguously.
   *
   * Deltas are byte ranges, and a browser string is not indexed in bytes, so a
   * partially-overlapping delta cannot be trimmed here without guessing at the
   * encoding. Both that case and an outright gap are rare (a reconnect, a
   * dropped event) and both are answered exactly by re-reading from where we
   * are over REST.
   */
  const catchUp = useCallback(
    async (target: BenchJob) => {
      if (catchingUp.current) return
      catchingUp.current = true
      try {
        const from = logOffset.current
        const next =
          target.kind === 'setup'
            ? await api.getBenchSetupLog(from)
            : await api.getBenchRun(target.id, from)
        // Another delta may have landed while the request was in flight; only
        // apply this one if it still starts where we are.
        if (logOffset.current === from && next.chunk !== undefined && next.offset !== undefined) {
          appendChunk(next.chunk, next.offset)
        }
      } catch {
        // The next delta will try again; a missing span is not worth a toast.
      } finally {
        catchingUp.current = false
      }
    },
    [appendChunk],
  )

  const trackJob = useCallback((next: BenchJob | null) => {
    jobRef.current = next
    setJob(next)
  }, [])

  const applyDelta = useCallback(
    (delta: BenchLogDelta) => {
      if (logJobId.current !== delta.job_id) {
        // A different job's output: start a fresh pane rather than splicing two
        // logs together.
        logJobId.current = delta.job_id
        logOffset.current = delta.start
        setLogTruncated(!!delta.truncated)
        setLog('')
      }
      if (delta.end <= logOffset.current) return // already have it
      if (delta.start === logOffset.current) {
        appendChunk(delta.chunk, delta.end)
        return
      }
      // Gap, or an overlap we cannot trim by bytes. Ask for the exact span.
      const target = jobRef.current
      if (target && target.id === delta.job_id) void catchUp(target)
    },
    [appendChunk, catchUp],
  )

  // --- server push --------------------------------------------------------

  useBenchEvent(
    useCallback(
      (event) => {
        switch (event.type) {
          case 'snapshot': {
            setEnv(event.env)
            setEnvError(null)
            trackJob(event.job)
            if (event.models) setModelsState(event.models)
            if (event.log) {
              // A snapshot re-states the whole tail, so it replaces rather than
              // appends -- this is also how a reconnect resyncs a log it may
              // have missed the middle of.
              logJobId.current = event.log.job_id
              logOffset.current = event.log.end
              setLogTruncated(!!event.log.truncated)
              setLog(event.log.chunk.slice(-MAX_LOG_CHARS))
            }
            if (event.job?.status === 'running') setMainTab('output')
            break
          }
          case 'job': {
            trackJob(event.job)
            if (event.job.status === 'running') {
              setMainTab('output')
            } else {
              // Finished. Only a measured run has numbers to show, so only a
              // measured run takes the reader away from the log: a download or
              // an environment install would land them on an unchanged Results
              // table, while what they were actually watching -- how the fetch
              // went -- is the thing they just got moved away from.
              const measured =
                event.job.kind === 'run' && event.job.meta?.stage !== 'build'
              if (measured) setMainTab('results')
              // A finished run releases quiet mode, and the balancer resumes --
              // which is exactly when what preflight reports can change. This
              // is also the refresh that clears a block the user has just gone
              // and resolved before running again. Also worth doing after a
              // setup or a download: the balancer moved on meanwhile.
              void loadPreflight()
            }
            break
          }
          case 'log':
            applyDelta(event)
            break
          case 'env':
            setEnv(event.env)
            setEnvError(null)
            break
          case 'models':
            setModelsState(event.models)
            // The list itself is too big to push, so the event only says it
            // changed and the list is fetched here. Unconditionally: the server
            // also sends this when a job finishes, and a run that has just
            // downloaded weights changes the list whether or not a search
            // happens to be in flight at the same moment.
            void loadModels()
            break
          case 'results':
            void loadResults()
            break
        }
      },
      [applyDelta, loadModels, loadPreflight, loadResults, trackJob],
    ),
  )

  // --- actions ------------------------------------------------------------

  const startSetup = useCallback(
    async (force: boolean) => {
      try {
        const res = await api.setupBenchEnv(force)
        if (res.status === 'conflict') {
          const existing = (res.data as { status?: BenchEnvData })?.status
          if (existing) {
            Modal.confirm({
              title: 'A benchmark environment already exists',
              width: 560,
              content: (
                <Space direction="vertical" size={4} style={{ marginTop: 8 }}>
                  <Text>
                    <Text type="secondary">Location: </Text>
                    <Text code>{existing.venv_dir}</Text>
                  </Text>
                  <Text>
                    <Text type="secondary">Installed: </Text>
                    {versionSummary(existing.versions)}
                  </Text>
                  <Paragraph type="warning" style={{ marginTop: 8, marginBottom: 0 }}>
                    Rebuilding downloads several GB and can take an hour. The existing
                    environment is moved aside, not deleted.
                  </Paragraph>
                </Space>
              ),
              okText: 'Rebuild anyway',
              okButtonProps: { danger: true },
              cancelText: 'Keep it',
              onOk: () => startSetup(true),
            })
          } else {
            message.warning(res.message)
          }
          return
        }
        message.success('Environment setup started')
        setEnvOpen(false)
      } catch (e) {
        message.error(e instanceof Error ? e.message : String(e))
      }
    },
    [],
  )

  /**
   * Explain a refused run, and offer the one place it can be resolved.
   *
   * Deliberately a dialog and not a toast: the user has to do something
   * elsewhere before the run can happen, and "Open the Balancer" being the
   * primary button is the difference between a message and an instruction.
   */
  const showBlockedDialog = useCallback(
    (blockers: BenchPreflightBlocker[]) => {
      const actions = [...new Set(blockers.map((b) => b.action).filter(Boolean))] as string[]
      Modal.confirm({
        title: 'This run cannot be measured yet',
        width: 560,
        okText: onOpenBalance ? 'Open the Balancer' : 'OK',
        cancelText: 'Close',
        okCancel: !!onOpenBalance,
        onOk: onOpenBalance,
        content: (
          <Space direction="vertical" size={8} style={{ marginTop: 8 }}>
            <Paragraph style={{ marginBottom: 0 }}>
              A benchmark run takes the machine quiet for its duration — background
              collection stops and the balancer's automatic control is suspended — so
              its numbers can be compared with other runs. That cannot be done while
              limits are already being applied:
            </Paragraph>
            {blockers.map((blocker) => (
              <Text key={blocker.name}>• {blockerLine(blocker)}</Text>
            ))}
            {actions.map((action) => (
              <Text key={action} type="secondary">
                {action}
              </Text>
            ))}
          </Space>
        ),
      })
    },
    [onOpenBalance],
  )

  // Send the run. Nothing is asked here -- whether the user has already answered
  // for the results this repeats is decided by requestRun, above it.
  const launchRun = useCallback(
    async (stage: BenchStage, ids: string[]) => {
      if (!ids.length) return
      setStarting(true)
      try {
        // Extra args only mean anything to a benchmark case; a build never runs
        // one, so leave them off that request entirely.
        const extra = args.trim()
        const payload = ids.map((id) => ({
          id,
          build: precisions,
          ...(stage !== 'build' && extra ? { args: extra } : {}),
        }))
        // A build fetches weights, which are the same file whatever runs them;
        // the device and OpenVINO selections are only about the benchmark stage.
        const res = await api.startBenchRun(
          payload,
          stage,
          stage === 'build' ? undefined : devices,
          stage === 'build' ? undefined : ov,
        )
        if (res.status === 'conflict') {
          // Two different conflicts arrive on this path. A busy execution slot
          // is a "try again in a minute" and a toast says it fine. A quiet-mode
          // block is a job for the user on another page, named app by app, and
          // a toast that disappears in three seconds is the wrong shape for
          // that -- so it gets a dialog with the way there.
          const blocked = (res.data as { blockers?: BenchPreflightBlocker[] })?.blockers
          if (blocked?.length) {
            setPreflight({
              blocked: true,
              blockers: blocked,
              // Nothing holds quiet mode: the run that would have is the one
              // just refused. Kept in shape so the banner reads one field.
              quiet_mode: { active: false, held: false, owner: null, since: null, user_exited: false },
            })
            showBlockedDialog(blocked)
            return
          }
          message.warning(res.message)
          return
        }
        message.success(
          stage === 'build'
            ? `Downloading ${ids.length === 1 ? ids[0] : `${ids.length} models`}`
            : 'Benchmark run started',
        )
      } catch (e) {
        message.error(e instanceof Error ? e.message : String(e))
      } finally {
        setStarting(false)
      }
    },
    [args, devices, ov, precisions, showBlockedDialog],
  )

  /**
   * What the Run buttons call.
   *
   * A benchmark that would repeat measurements already on disk stops here and
   * asks first -- see BenchRerunModal for why the answer is not obvious. A
   * download has nothing to repeat: the weights are either present or not, and
   * the pipeline decides that per precision.
   */
  const requestRun = useCallback(
    (stage: BenchStage, ids: string[]) => {
      if (!ids.length) return
      const conflicts =
        stage === 'build'
          ? []
          : findConflicts(matrix?.rows ?? [], ids, precisions, devices)
      if (conflicts.length) {
        setRerun({ stage, ids, conflicts })
        return
      }
      void launchRun(stage, ids)
    },
    [devices, launchRun, matrix?.rows, precisions],
  )

  const keepAndRun = useCallback(() => {
    const pending = rerun
    setRerun(null)
    if (pending) void launchRun(pending.stage, pending.ids)
  }, [launchRun, rerun])

  const discardAndRun = useCallback(async () => {
    const pending = rerun
    if (!pending) return
    // Only the case directories the summary actually names: a row that never
    // wrote one has nothing on disk to remove.
    const cases = [
      ...new Set(
        pending.conflicts.flatMap((conflict) =>
          conflict.rows.map((row) => row.case_dir).filter(Boolean),
        ),
      ),
    ]
    setDiscarding(true)
    try {
      const { removed, skipped } = await api.deleteBenchCases(cases)
      if (skipped.length) {
        // Partial removal is still a usable state -- what is left is fewer old
        // measurements than before -- so the run goes ahead and says so.
        message.warning(`Removed ${removed} case(s); ${skipped.length} could not be removed`)
      } else {
        message.success(`Removed ${removed} previous case${removed === 1 ? '' : 's'}`)
      }
      setRerun(null)
      await loadResults()
      void launchRun(pending.stage, pending.ids)
    } catch (e) {
      // The old results are still there, so the run is not started: the user
      // asked for a clean slate and did not get one.
      message.error(e instanceof Error ? e.message : String(e))
    } finally {
      setDiscarding(false)
    }
  }, [launchRun, loadResults, rerun])

  const cancelJob = useCallback(async () => {
    const target = env?.busy ?? job
    if (!target) return
    try {
      await api.cancelBenchRun(target.id)
      message.success('Cancellation requested')
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e))
    }
  }, [env?.busy, job])

  /**
   * Unticking a model closes its pane, including when it is the one being read.
   *
   * `detailIds` appends the highlighted row to the ticked set, so without this
   * unticking the model whose row is selected left its pane on screen -- the
   * checkbox appeared not to work. The selection moves to whatever is left
   * rather than to nothing, so the pane area does not empty out mid-batch.
   */
  const changeChecked = useCallback(
    (next: string[]) => {
      setCheckedIds(next)
      if (selectedId && checkedIds.includes(selectedId) && !next.includes(selectedId)) {
        setSelectedId(next.length ? next[next.length - 1] : null)
      }
    },
    [checkedIds, selectedId],
  )

  /** The X on a model's tab: the same as unticking it on the left. */
  const closeDetail = useCallback(
    (id: string) => {
      const next = checkedIds.filter((checked) => checked !== id)
      setCheckedIds(next)
      if (selectedId === id) setSelectedId(next.length ? next[next.length - 1] : null)
    },
    [checkedIds, selectedId],
  )

  const refreshModelList = useCallback(async () => {
    try {
      const { started, reason } = await api.refreshBenchModels()
      if (!started) {
        // The server says why -- a search already in flight, or no `hf` CLI to
        // search with. Either is a fact about the machine, not a failure.
        message.info(reason ?? 'The model list could not be refreshed')
        return
      }
      message.success('Searching HuggingFace for benchmarkable models; this takes a few minutes.')
      // The server announces completion over the stream, so there is nothing to
      // wait on here -- only the spinner to turn on.
      setModelsState((prev) => (prev ? { ...prev, refreshing: true } : prev))
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e))
    }
  }, [])

  // --- render -------------------------------------------------------------

  // Only take the page over when there is nothing to show. A failed request must
  // not tear down a tab that is following a running job; the banner reports it
  // against the last known state instead.
  if (envError && !env) {
    return <Alert type="error" showIcon message="Benchmark unavailable" description={envError} />
  }

  const paneHeight = 'calc(100vh - var(--app-sticky-top, 120px) - 24px)'

  return (
    <div style={{ display: 'flex', gap: 12, marginTop: 12, height: paneHeight }}>
      {siderOpen ? (
        <div
          style={{
            width: SIDER_WIDTH,
            flex: `0 0 ${SIDER_WIDTH}px`,
            border: `1px solid ${COLORS.border}`,
            borderRadius: 6,
            background: COLORS.panelBg,
            overflow: 'hidden',
          }}
        >
          <BenchModelList
            models={models}
            loading={modelsLoading}
            refreshing={!!modelsState?.refreshing}
            updatedAt={modelsState?.updated_at ?? null}
            lastError={modelsState?.last_error ?? null}
            search={search}
            onSearchChange={setSearch}
            filter={filter}
            onFilterChange={setFilter}
            selectedId={selectedId}
            onSelect={setSelectedId}
            checkedIds={checkedIds}
            onCheckedChange={changeChecked}
            onRefreshList={() => void refreshModelList()}
          />
        </div>
      ) : null}

      {/* The collapse handle lives on the edge it moves, and stays in place when
          the panel is gone -- so re-opening it is the same click in the same
          spot, rather than a button somewhere in a toolbar. */}
      <div style={{ display: 'flex', alignItems: 'center', flex: '0 0 auto' }}>
        <Tooltip
          title={siderOpen ? 'Collapse the model list' : 'Show the model list'}
          placement="right"
        >
          <Button
            type="text"
            size="small"
            aria-label={siderOpen ? 'Collapse the model list' : 'Show the model list'}
            icon={siderOpen ? <LeftOutlined /> : <RightOutlined />}
            onClick={() => setSiderOpen((open) => !open)}
            style={{
              height: 48,
              width: 16,
              minWidth: 16,
              padding: 0,
              border: `1px solid ${COLORS.border}`,
              borderRadius: 4,
              fontSize: 10,
            }}
          />
        </Tooltip>
      </div>

      <div style={{ flex: 1, minWidth: 0, overflow: 'auto' }}>
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {envError && (
            <Alert
              type="warning"
              showIcon
              closable
              message="Could not refresh the environment status"
              description={`${envError} — showing the last known state.`}
            />
          )}

          {/* Shown before anything is clicked, because the fix is on another
              page: a measured run needs the machine quiet, and it cannot go
              quiet while the balancer is holding automatic limits. Not
              closable -- it is a precondition of the button below it, not
              news. The Run buttons stay live: the server decides, and this
              banner is only as fresh as its last read. */}
          {preflight?.blocked && (
            <Alert
              type="warning"
              showIcon
              message="A benchmark run cannot be measured right now"
              description={
                <Space direction="vertical" size={4} style={{ width: '100%' }}>
                  {preflight.blockers.map((blocker) => (
                    <Text key={blocker.name}>{blockerLine(blocker)}</Text>
                  ))}
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    Restore them or lock them to manual, then re-check. Starting a run
                    anyway will be refused.
                  </Text>
                </Space>
              }
              action={
                <Space direction="vertical" size={4}>
                  {onOpenBalance && (
                    <Button size="small" type="primary" onClick={onOpenBalance}>
                      Open the Balancer
                    </Button>
                  )}
                  <Button size="small" onClick={() => void loadPreflight()}>
                    Re-check
                  </Button>
                </Space>
              }
            />
          )}

          {/* Flex rather than <Space> so the environment control can be pushed
              to the far edge: it is the one thing here that is not about the
              models, and it belongs out of the way of the ones that are. */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            {/* The count is a fact, not an action. It used to be the label of the
                button that acted on it, which made one control answer two
                questions -- how many are picked, and what pressing it does. */}
            {checkedIds.length > 0 ? (
              <>
                <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                  Selected {checkedIds.length}
                </Tag>
                <Tooltip
                  title={
                    batchMissing.length
                      ? `Fetch ${batchMissing.join(', ')} for every selected model, as one job`
                      : 'Every selected model already has the ticked precisions; this fetches them again'
                  }
                >
                  <Button
                    size="small"
                    type={batchMissing.length ? 'primary' : 'default'}
                    icon={<CloudDownloadOutlined />}
                    disabled={!env?.ready || busy || precisions.length === 0}
                    loading={starting}
                    onClick={() => requestRun('build', checkedIds)}
                  >
                    Download all
                  </Button>
                </Tooltip>
                <Tooltip
                  title={
                    batchMissing.length
                      ? `Download ${batchMissing.join(', ')} first`
                      : devices.length === 0
                        ? 'Pick at least one device to benchmark on'
                        : !ovValid
                          ? 'Enter an OpenVINO version (e.g. 2026.2.0) to benchmark against'
                          : 'Benchmark every selected model, as one job'
                  }
                >
                  <Button
                    size="small"
                    type="primary"
                    icon={<PlayCircleOutlined />}
                    disabled={
                      !env?.ready ||
                      busy ||
                      precisions.length === 0 ||
                      devices.length === 0 ||
                      !ovValid ||
                      batchMissing.length > 0
                    }
                    loading={starting}
                    // Never `all`: see BenchModelDetail's header for why a fetch
                    // must not happen inside a measured run.
                    onClick={() => requestRun('benchmark', checkedIds)}
                  >
                    Run all
                  </Button>
                </Tooltip>
              </>
            ) : (
              <Text type="secondary" style={{ fontSize: 12 }}>
                Tick models on the left to work on several at once.
              </Text>
            )}
            {busy && (
              <Space size={6}>
                <JobStatusTag job={env?.busy ?? job} />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {installing ? 'installing the environment' : 'job in progress'}
                </Text>
              </Space>
            )}
            {/* Its own status is the label, so a glance at the corner answers
                "can I run anything" without opening the drawer. */}
            <Button
              size="small"
              icon={<ToolOutlined />}
              onClick={() => setEnvOpen(true)}
              style={{ marginLeft: 'auto' }}
            >
              <Space size={6}>
                Environment
                <EnvStatusTag env={env} installing={installing} />
              </Space>
            </Button>
          </div>

          {/* With several models in play, each one keeps its own pane: the
              precisions it offers and what is already downloaded differ per
              model, and those are exactly the facts needed before running a
              batch. One model needs no tab strip. */}
          {detailIds.length > 1 ? (
            <Tabs
              // Editable only for the X: closing a model's pane is the same act
              // as unticking it, and the tab strip is where the batch is in
              // front of the user. There is nothing to add, so no add button.
              type="editable-card"
              hideAdd
              size="small"
              activeKey={activeDetailId ?? undefined}
              onChange={setSelectedId}
              onEdit={(key, action) => {
                if (action === 'remove') closeDetail(String(key))
              }}
              items={detailIds.map((id) => {
                const model = models.find((m) => m.id === id) ?? null
                return {
                  key: id,
                  label: (
                    <Space size={4}>
                      {id.split('/').pop()}
                      {model?.downloaded && <CheckCircleTwoTone twoToneColor="#52c41a" />}
                    </Space>
                  ),
                  children: (
                    <BenchModelDetail
                      model={model}
                      precisions={precisions}
                      onPrecisionsChange={setPrecisions}
                      devices={devices}
                      onDevicesChange={setDevices}
                      ov={ov}
                      onOvChange={setOv}
                      ovOptions={ovOptions}
                      installedOvs={env?.ov_versions ?? []}
                      args={args}
                      onArgsChange={setArgs}
                      ready={!!env?.ready}
                      busy={busy}
                      probing={!!env?.probing}
                      starting={starting}
                      onRun={requestRun}
                      onCancel={() => void cancelJob()}
                      onOpenEnv={() => setEnvOpen(true)}
                    />
                  ),
                }
              })}
            />
          ) : (
            <BenchModelDetail
              model={selected}
              precisions={precisions}
              onPrecisionsChange={setPrecisions}
              devices={devices}
              onDevicesChange={setDevices}
              ov={ov}
              onOvChange={setOv}
              ovOptions={ovOptions}
              installedOvs={env?.ov_versions ?? []}
              args={args}
              onArgsChange={setArgs}
              ready={!!env?.ready}
              busy={busy}
              probing={!!env?.probing}
              starting={starting}
              onRun={requestRun}
              onCancel={() => void cancelJob()}
              onOpenEnv={() => setEnvOpen(true)}
            />
          )}

          <Card size="small" styles={{ body: { paddingTop: 8 } }}>
            <Tabs
              activeKey={mainTab}
              onChange={(key) => setMainTab(key as MainTab)}
              size="small"
              items={[
                {
                  key: 'results',
                  label: 'Benchmark Results',
                  children: (
                    <BenchResults
                      matrix={matrix}
                      onRefresh={() => void loadResults()}
                      onOpenCase={openCase}
                    />
                  ),
                },
                {
                  key: 'compare',
                  label: 'Analysis',
                  children: <BenchCompare matrix={matrix} onOpenCase={openCase} />,
                },
                {
                  key: 'output',
                  label: (
                    <Space size={6}>
                      Logs
                      {job?.status === 'running' && <Tag color="processing">live</Tag>}
                    </Space>
                  ),
                  children: <BenchOutput job={job} log={log} truncated={logTruncated} />,
                },
              ]}
            />
          </Card>
        </Space>
      </div>

      <BenchCaseDrawer
        open={caseOpen}
        row={caseRow}
        metrics={matrix?.metrics ?? []}
        onClose={() => setCaseOpen(false)}
      />

      <BenchRerunModal
        open={!!rerun}
        conflicts={rerun?.conflicts ?? []}
        working={discarding}
        onKeep={keepAndRun}
        onDiscard={() => void discardAndRun()}
        onCancel={() => setRerun(null)}
      />

      <BenchEnvDrawer
        open={envOpen}
        onClose={() => setEnvOpen(false)}
        env={env}
        busy={busy}
        installing={installing}
        onRefresh={() => void loadEnv()}
        onSetup={() => void startSetup(false)}
      />
    </div>
  )
}
