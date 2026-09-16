// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// Benchmark tab: browse the models that have an OpenVINO conversion, download
// one, benchmark it, read the numbers.
//
// The page is a model browser (left) plus the models in play and their results
// (right). The backend runs at most one job at a time -- setup and runs share a
// single slot -- so there is no job list: there is what is running now, and
// there are the results of everything that ran before.
//
// The models in play are a strip of tiles (BenchModelTiles), one per ticked
// model, each carrying its own precisions, devices, OpenVINO version and extra
// args; this component owns those settings and the drawer that edits them. They
// used to be one global set, which could not express the thing the page exists
// for -- this model as int4 on the NPU, that one as fp16 on the CPU. The server
// takes them per model and groups the models that agree about the two the
// pipeline cannot vary within one process (benchmark/service/runner.py,
// run_groups), so a request that disagrees is still one press of Run.
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
  LeftOutlined,
  RightOutlined,
  StopOutlined,
  ToolOutlined,
} from '@ant-design/icons'

import { api } from '../api/client'
import type {
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
import { applicablePrecisions, type ModelParams } from './BenchModelDetail'
import BenchModelDeleteModal from './BenchModelDeleteModal'
import BenchModelDrawer from './BenchModelDrawer'
import BenchModelList, { type ModelFilter } from './BenchModelList'
import BenchModelTiles from './BenchModelTiles'
import BenchOutput, { JobStatusTag } from './BenchOutput'
import BenchResults from './BenchResults'
import BenchRerunModal, { type RerunConflict } from './BenchRerunModal'
import { COLORS } from '../styles/theme'
import { formatBytes, matchesModel } from '../utils/benchMetrics'

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
 *
 * Each model is judged against its own precisions and devices: the settings are
 * per model now, so a global pair would report conflicts for combinations the
 * run is not going to measure.
 */
function findConflicts(
  rows: BenchMatrixRow[],
  ids: string[],
  params: Record<string, ModelParams>,
): RerunConflict[] {
  const conflicts: RerunConflict[] = []
  for (const modelId of ids) {
    const own = params[modelId]
    if (!own) continue
    const forModel = rows.filter((row) => matchesModel(row.model, modelId))
    if (forModel.length === 0) continue
    for (const precision of own.precisions) {
      for (const device of own.devices) {
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
  // Which tiles the Download and Run buttons act on. A subset of checkedIds:
  // ticking a model on the left brings it into play, ticking its tile says it
  // is part of the next job -- which is what lets six models be lined up and
  // two of them measured.
  const [runIds, setRunIds] = useState<string[]>([])
  // The model whose drawer is open, or null.
  const [openModelId, setOpenModelId] = useState<string | null>(null)
  // The model whose downloaded weights are being deleted, with the precisions
  // the dialog opens ticked. Held here rather than in either of the two places
  // that can ask for it -- the list row and the detail pane's tags -- so both
  // open the same dialog.
  const [deleting, setDeleting] = useState<
    { id: string; precisions: BenchPrecision[] } | null
  >(null)

  // One set of run settings per model, keyed by id.
  //
  // These were four pieces of global state, shared by everything selected --
  // which could not say "this model as int4 on the NPU, that one as fp16 on the
  // CPU", the thing a page that runs several models at once is for. The
  // precisions and extra args reach the pipeline on each model's own entry in
  // the run request; the devices and the OpenVINO version are process-wide down
  // there, so the server groups the models that agree about them and runs the
  // groups in sequence (benchmark/service/runner.py, run_groups).
  const [params, setParams] = useState<Record<string, ModelParams>>({})
  // What a model gets when it is first ticked: the last settings the user chose,
  // so a batch does not have to be configured one tile at a time. All three
  // devices to begin with, which is what the pipeline swept before the request
  // could name devices at all; the OpenVINO version is seeded once the
  // environment says which ones are installed.
  const [defaults, setDefaults] = useState<ModelParams>({
    precisions: ['int4'],
    devices: ['cpu', 'gpu', 'npu'],
    ov: '',
    args: '',
  })

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

  // Which models get a tile: everything ticked, in list order, plus the
  // highlighted row if it is not ticked. Clicking a row while a batch is ticked
  // must not hide the batch, and ticking must not lose the row being read.
  const tileIds = useMemo(() => {
    const checked = new Set(checkedIds)
    const ids = models.filter((m) => checked.has(m.id)).map((m) => m.id)
    // A ticked model that is no longer in the list (a refresh dropped it) still
    // deserves its tile, so anything unaccounted for is appended.
    checkedIds.forEach((id) => {
      if (!ids.includes(id)) ids.push(id)
    })
    if (selectedId && !ids.includes(selectedId)) ids.push(selectedId)
    return ids
  }, [models, checkedIds, selectedId])

  // The versions the dropdown offers, decided by the server, newest first;
  // ov_versions is the fallback for a service too old to send ov_choices.
  const ovOptions = useMemo(
    () => (env?.ov_choices?.length ? env.ov_choices : (env?.ov_versions ?? [])),
    [env?.ov_choices, env?.ov_versions],
  )

  // Seed the default OpenVINO version once, while the user has not chosen:
  // newest *installed* first, so a machine with a runtime lands on a selection
  // Run can actually use. Only the default -- a tile that already carries a
  // version was set deliberately and is left alone.
  useEffect(() => {
    if (defaults.ov) return
    const installed = env?.ov_versions ?? []
    const seed = installed[0] ?? ovOptions[0]
    if (seed) setDefaults((prev) => (prev.ov ? prev : { ...prev, ov: seed }))
  }, [defaults.ov, ovOptions, env?.ov_versions])

  // Give every tile settings, and forget the ones whose tile has gone.
  //
  // Derived from tileIds rather than set when a checkbox is clicked: a tile can
  // also appear because a row was selected, or because a model was ticked
  // before the list arrived, and a params entry missing for any of those is a
  // tile with nothing to render.
  useEffect(() => {
    setParams((prev) => {
      const next: Record<string, ModelParams> = {}
      let changed = tileIds.length !== Object.keys(prev).length
      tileIds.forEach((id) => {
        next[id] = prev[id] ?? defaults
        if (!prev[id]) changed = true
      })
      return changed ? next : prev
    })
  }, [tileIds, defaults])

  // Settings for every tile, gaps filled. The effect above cannot be relied on
  // for the render that first shows a tile -- effects run after it, so the tile
  // would be asked to draw settings that do not exist yet.
  const tileParams = useMemo(() => {
    const filled: Record<string, ModelParams> = {}
    tileIds.forEach((id) => {
      filled[id] = params[id] ?? defaults
    })
    return filled
  }, [tileIds, params, defaults])

  // A new tile is ticked for the run: putting a model in play and then having to
  // tick it again to act on it is two clicks for one intent. Unticking is
  // remembered -- only ids that have never been seen are added.
  const seenTiles = useRef<Set<string>>(new Set())
  useEffect(() => {
    const fresh = tileIds.filter((id) => !seenTiles.current.has(id))
    seenTiles.current = new Set(tileIds)
    setRunIds((prev) => {
      const kept = prev.filter((id) => tileIds.includes(id))
      const added = fresh.filter((id) => !kept.includes(id))
      if (!added.length && kept.length === prev.length) return prev
      // In tile order, so the run request follows what is on screen.
      return tileIds.filter((id) => kept.includes(id) || added.includes(id))
    })
  }, [tileIds])

  /** Change one model's settings, and make them the default for the next tile. */
  const changeParams = useCallback((id: string, next: ModelParams) => {
    setParams((prev) => ({ ...prev, [id]: next }))
    setDefaults(next)
  }, [])

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

  /**
   * Install the environment, optionally including one OpenVINO runtime.
   *
   * `ovVersion` is what turns this from half an installation into all of it:
   * that runtime is what a benchmark runs inside, and it used to be built by
   * the first run that needed it, inside a measured window.
   */
  const startSetup = useCallback(
    async (force: boolean, ovVersion?: string) => {
      try {
        const res = await api.setupBenchEnv(force, ovVersion)
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
                  <Text>
                    <Text type="secondary">OpenVINO runtimes: </Text>
                    {existing.ov_versions.length ? existing.ov_versions.join(', ') : 'none'}
                  </Text>
                  <Paragraph type="warning" style={{ marginTop: 8, marginBottom: 0 }}>
                    Rebuilding re-downloads several GB of wheels. The existing
                    environment is moved aside, not deleted.
                  </Paragraph>
                </Space>
              ),
              okText: 'Rebuild anyway',
              okButtonProps: { danger: true },
              cancelText: 'Keep it',
              onOk: () => startSetup(true, ovVersion),
            })
          } else {
            message.warning(res.message)
          }
          return
        }
        message.success(
          ovVersion
            ? `Installing the environment with OpenVINO ${ovVersion}`
            : 'Environment setup started',
        )
        setEnvOpen(false)
      } catch (e) {
        message.error(e instanceof Error ? e.message : String(e))
      }
    },
    [],
  )

  // Install one runtime from the version box on a model's pane. No
  // confirmation: uv hardlink-shares wheels with the versions already here, so
  // a second one is a few hundred MB, and the job is cancellable anyway.
  const installOv = useCallback(
    (version: string) => void startSetup(false, version),
    [startSetup],
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
        const byId = new Map(models.map((m) => [m.id, m]))
        // Each model carries its own settings, and is asked only for the
        // precisions it actually offers: sending a tick verbatim asked a model
        // published only as fp16/int8 for an int4 repo that does not exist -- a
        // case that fails deep in the pipeline instead of never being
        // generated. A model left with nothing to ask for drops out of the
        // request; one the list no longer holds keeps its raw selection, there
        // being nothing to narrow it against.
        //
        // Devices and the runtime go per model too. The server groups the models
        // that agree about them and runs the groups in sequence, which is what
        // makes "this one on the NPU against 2025.3, that one on the CPU against
        // 2025.2" one press of Run. Neither means anything to a build: it
        // fetches weights, which are the same file whatever runs them.
        const payload = ids
          .map((id) => {
            const model = byId.get(id)
            const own = params[id] ?? defaults
            const extra = own.args.trim()
            return {
              id,
              build: model ? applicablePrecisions(model, own.precisions) : own.precisions,
              ...(stage === 'build'
                ? {}
                : {
                    devices: own.devices,
                    ov: own.ov,
                    ...(extra ? { args: extra } : {}),
                  }),
            }
          })
          .filter((entry) => entry.build.length > 0)
        if (!payload.length) {
          message.warning('None of the selected models offer the precisions they are set to')
          return
        }
        // The request-level pair is what an entry that named neither falls back
        // to, server-side. Every entry here names both for a benchmark, so this
        // only decides what a service too old to read them uses.
        const first = params[payload[0].id] ?? defaults
        const res = await api.startBenchRun(
          payload,
          stage,
          stage === 'build' ? undefined : first.devices,
          stage === 'build' ? undefined : first.ov,
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
    [defaults, models, params, showBlockedDialog],
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
        stage === 'build' ? [] : findConflicts(matrix?.rows ?? [], ids, params)
      if (conflicts.length) {
        setRerun({ stage, ids, conflicts })
        return
      }
      void launchRun(stage, ids)
    },
    [launchRun, matrix?.rows, params],
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
   * Unticking a model removes its tile, including when it is the one selected.
   *
   * `tileIds` appends the highlighted row to the ticked set, so without this
   * unticking the model whose row is selected left its tile on screen -- the
   * checkbox appeared not to work. The selection moves to whatever is left
   * rather than to nothing, so the strip does not empty out mid-batch.
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

  /**
   * Empty the selection: every tile, and the list's ticks with them.
   *
   * The highlighted row goes too. It is what puts a tile on screen for a model
   * that was clicked rather than ticked, so leaving it would clear five tiles
   * of six and look like the button had missed one. Each model's settings are
   * dropped with its tile -- the last of them stays on as the default, so
   * starting again does not start from scratch.
   */
  const clearSelection = useCallback(() => {
    setCheckedIds([])
    setRunIds([])
    setSelectedId(null)
    setOpenModelId(null)
  }, [])

  /** The X on a tile: the same as unticking the model on the left. */
  const closeTile = useCallback(
    (id: string) => {
      const next = checkedIds.filter((checked) => checked !== id)
      setCheckedIds(next)
      if (selectedId === id) setSelectedId(next.length ? next[next.length - 1] : null)
      // The tile is gone, so anything about it that is still on screen has to
      // go with it.
      setOpenModelId((open) => (open === id ? null : open))
    },
    [checkedIds, selectedId],
  )

  /**
   * Remove one job's results, then re-read the tree.
   *
   * The server also announces the change on the stream, which is what updates
   * every *other* open tab; this awaits its own read so the dialog's spinner
   * lasts until the table it is over has actually changed.
   */
  const deleteJob = useCallback(
    async (job: string) => {
      try {
        const { removed_cases, removed_runs, skipped } = await api.deleteBenchJob(job)
        if (skipped.length) {
          message.warning(
            `Removed ${removed_cases} case(s); ${skipped.length} run directory(ies) could not be removed`,
          )
        } else {
          message.success(
            `Removed ${removed_cases} case${removed_cases === 1 ? '' : 's'} in ` +
              `${removed_runs} run director${removed_runs === 1 ? 'y' : 'ies'}`,
          )
        }
        await loadResults()
      } catch (e) {
        message.error(e instanceof Error ? e.message : String(e))
        // Rethrown so the confirmation dialog stays open on a failure: the rows
        // are still there, and closing it would look like the delete worked.
        throw e
      }
    },
    [loadResults],
  )

  const deleteCases = useCallback(
    async (cases: string[]) => {
      try {
        const { removed, skipped } = await api.deleteBenchCases(cases)
        if (skipped.length) {
          message.warning(
            `Removed ${removed} case(s); ${skipped.length} could not be removed`,
          )
        } else {
          message.success(`Removed ${removed} case${removed === 1 ? '' : 's'}`)
        }
        await loadResults()
      } catch (e) {
        message.error(e instanceof Error ? e.message : String(e))
        throw e
      }
    },
    [loadResults],
  )

  /**
   * Give back the disk one model's downloaded weights are holding.
   *
   * The server announces the change on the stream, which is what updates every
   * other open tab; this awaits its own read so the dialog closes onto a list
   * that has already lost the weights it just deleted. A failure leaves the
   * dialog open with its selection, so the same delete can be retried -- 409 is
   * the one that matters, and it means "a job started while you were reading
   * this".
   */
  const deleteLocalWeights = useCallback(
    async (model: BenchModel, precisions: BenchPrecision[]) => {
      try {
        const { removed, freed_bytes, skipped } = await api.deleteBenchModelLocal(
          model.id,
          precisions,
        )
        if (skipped.length) {
          message.warning(
            `Removed ${removed.join(', ') || 'nothing'}; ${skipped.join(', ')} could not be removed`,
          )
        } else {
          message.success(
            `Removed ${removed.join(', ')} weights for ${model.id} · freed ${formatBytes(freed_bytes)}`,
          )
        }
        await loadModels()
        setDeleting(null)
      } catch (e) {
        message.error(e instanceof Error ? e.message : String(e))
        throw e
      }
    },
    [loadModels],
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
            // From a row, the question is about the model: every precision it
            // has on disk arrives ticked, and the dialog is where it narrows.
            onDeleteLocal={(model) => setDeleting({ id: model.id, precisions: [] })}
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
            {tileIds.length > 0 ? (
              <>
                <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                  Selected {tileIds.length}
                </Tag>
                {/* Empties the strip and the list's ticks. Nothing on disk is
                    touched -- which is why it sits here, next to the count it
                    resets, and not near the two buttons that do run things. */}
                <Tooltip title="Clear the selection — nothing is downloaded, run or deleted">
                  <Button size="small" type="text" onClick={clearSelection}>
                    Clear
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
                {/* Next to what it stops. A model's own pane has the same
                    button, but that one is now behind a tile's drawer -- and
                    stopping the machine doing something should not require
                    finding which model it is doing it for. */}
                <Button
                  size="small"
                  danger
                  icon={<StopOutlined />}
                  onClick={() => void cancelJob()}
                >
                  Cancel
                </Button>
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

          {/* One tile per model in play, each carrying its own settings: what a
              model offers, what is already downloaded, and what it is set to
              run as all differ across a batch, and those are exactly the facts
              needed before starting one. The full detail is in the drawer a
              tile opens. */}
          <BenchModelTiles
            ids={tileIds}
            models={models}
            params={tileParams}
            checked={runIds}
            onCheckedChange={setRunIds}
            onOpen={setOpenModelId}
            onClose={closeTile}
            installedOvs={env?.ov_versions ?? []}
            ready={!!env?.ready}
            busy={busy}
            starting={starting}
            onDownload={(ids) => requestRun('build', ids)}
            onRun={(ids) => requestRun('benchmark', ids)}
          />

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
                      onDeleteJob={deleteJob}
                      onDeleteCases={deleteCases}
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

      {/* One model's detail and settings. Rendered whenever a tile has been
          opened at least once, so the drawer animates shut rather than
          disappearing -- and keyed on nothing, since the params it edits are
          held here. */}
      <BenchModelDrawer
        open={!!openModelId}
        model={models.find((m) => m.id === openModelId) ?? null}
        params={(openModelId ? tileParams[openModelId] : undefined) ?? defaults}
        onParamsChange={(next) => openModelId && changeParams(openModelId, next)}
        onClose={() => setOpenModelId(null)}
        ovChoices={ovOptions}
        installedOvs={env?.ov_versions ?? []}
        onInstallOv={installOv}
        ready={!!env?.ready}
        busy={busy}
        probing={!!env?.probing}
        onOpenEnv={() => setEnvOpen(true)}
        // From a tag, the question is about that one conversion, so it is the
        // only thing ticked; "delete all" in the same row passes none, which
        // means everything.
        onDeleteLocal={(model, precisions) => setDeleting({ id: model.id, precisions })}
      />

      {/* One dialog for both entry points. Keyed off the id rather than the
          model object, so it follows the list through a reload instead of
          holding the copy it was opened with. */}
      <BenchModelDeleteModal
        model={deleting ? models.find((m) => m.id === deleting.id) ?? null : null}
        preselect={deleting?.precisions}
        busy={busy}
        onCancel={() => setDeleting(null)}
        onConfirm={deleteLocalWeights}
      />

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
        // The drawer picks which runtime to install, opening on the version the
        // next tile would be set to -- there is no one version the tab is on any
        // more, and the last one chosen is the best guess at the one being
        // worked with.
        ov={defaults.ov}
        ovChoices={ovOptions}
        onSetup={(version) => void startSetup(false, version)}
      />
    </div>
  )
}
