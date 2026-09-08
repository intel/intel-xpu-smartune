// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// Shared reading of benchmark metrics: what a key is called, how a value is
// written, and which of two values is the better one.
//
// The metric set is not a fixed schema -- it is whatever the pipeline's
// aggregation step found -- so the backend ships a descriptor per key and this
// module is the only place that interprets one. The Results table and the
// Compare views must agree on all three questions; a chart that ranked a metric
// the opposite way from the table beside it would be worse than no chart.

import type { BenchMatrixRow, BenchMetricMeta } from '../api/types'

export type MetricIndex = Map<string, BenchMetricMeta>

/** Descriptors by key, for O(1) lookup while rendering a table. */
export function indexMetrics(metrics: BenchMetricMeta[] | undefined): MetricIndex {
  return new Map((metrics ?? []).map((meta) => [meta.key, meta]))
}

/**
 * A readable name for a metric key.
 *
 * The fallback is for a key the backend somehow did not describe -- an older
 * server against a newer dashboard. It reads plainly rather than being dropped.
 */
export function metricLabel(key: string, index?: MetricIndex): string {
  const meta = index?.get(key)
  if (meta) return meta.label
  return key.replace(/_median$/, '').replace(/^(kpis?|d)_/, '').replace(/_/g, ' ')
}

/**
 * A label with its first letter raised, for somewhere it is read as a heading.
 *
 * The descriptors are written lower case on purpose -- they sit in table headers
 * beside their unit, where Title Case competes with the numbers under it (see
 * results.py's _METRIC_LABELS). A stat tile is the other case: it is a caption
 * over one number, and "input length" there reads as a fragment. Only the first
 * character moves, so TTFT and GPU stay as they are.
 */
export function sentenceCase(label: string): string {
  return label ? label.charAt(0).toUpperCase() + label.slice(1) : label
}

/** Label with its unit, for an axis title or a one-line column header. */
export function metricTitle(key: string, index?: MetricIndex): string {
  const meta = index?.get(key)
  const label = metricLabel(key, index)
  return meta?.unit ? `${label} (${meta.unit})` : label
}

/**
 * Significant digits rather than a fixed precision.
 *
 * These columns sit next to each other and span six orders of magnitude -- a
 * 2313 ms first-token latency beside a 0.0025 s/token -- so a single toFixed
 * would either bury the small values in zeros or pad the large ones with noise.
 */
export function formatMetric(value: number | undefined | null): string {
  if (value === undefined || value === null || !Number.isFinite(value)) return '-'
  const magnitude = Math.abs(value)
  if (magnitude >= 1000) return value.toFixed(0)
  if (magnitude >= 100) return value.toFixed(1)
  if (magnitude >= 1) return value.toFixed(2)
  return value.toFixed(3)
}

/** Value with its unit, for a tooltip or a stat tile. */
export function formatWithUnit(value: number | undefined | null, meta?: BenchMetricMeta): string {
  const text = formatMetric(value)
  if (text === '-' || !meta?.unit) return text
  return `${text} ${meta.unit}`
}

/**
 * The best value among ``values``, or undefined when ranking is meaningless.
 *
 * `higher_is_better` is three-valued: null means the metric has no direction --
 * a clock frequency, an input length -- and the honest answer is that there is
 * no best. Returning undefined leaves the row unmarked instead of crowning
 * whichever end of the scale happened to be larger.
 *
 * All-equal values are the same case in disguise, and they are not rare: the
 * pipeline's "vs best batch size" is 1.00× for every row of a sweep that used
 * one batch size. Marking every cell of such a row as the best one is a claim
 * that nothing was compared, dressed up as a result.
 */
export function bestValue(
  values: (number | undefined)[],
  meta: BenchMetricMeta | undefined,
): number | undefined {
  if (!meta || meta.higher_is_better === null) return undefined
  const finite = values.filter((v): v is number => v !== undefined && Number.isFinite(v))
  if (finite.length < 2) return undefined
  const best = meta.higher_is_better ? Math.max(...finite) : Math.min(...finite)
  return finite.every((value) => value === best) ? undefined : best
}

// --- repeated tests ------------------------------------------------------
//
// The same test can be run any number of times. Every run gets its own results
// directory (benchmark/service/runner.py names it after the run), so the backend
// returns one row per measurement and nothing is overwritten or dropped.
//
// That leaves the dashboard with the question the files cannot answer: of the
// four times this model was benchmarked at int4 on the GPU, which number is
// "the" number? Both answers are wanted, so both are given -- the repetitions
// are grouped under the test they belong to, and the best of them represents the
// group wherever one row is all there is room for.

/** What makes two measurements the same test rather than two tests. */
export function testKey(row: BenchMatrixRow): string {
  return [row.backend, row.model, row.precision || row.quant, row.device, row.batch_size, row.mode]
    .join('\u0000')
}

export interface TestGroup {
  key: string
  model: string
  precision: string
  quant: string
  device: string
  task: string
  /** Every measurement of this test, newest run first. */
  runs: BenchMatrixRow[]
  /**
   * The one that represents the group: the best primary-metric result among the
   * runs that produced one, else the newest.
   */
  best: BenchMatrixRow
  /** How many runs measured something. The rest failed. */
  measured: number
}

/**
 * Group measurements by the test they are repetitions of.
 *
 * `primaryMetric` decides which repetition is the best one -- throughput, for a
 * text-generation profile. It is applied to the whole group rather than
 * per-metric on purpose: picking, for every column, whichever run happened to
 * excel at it would compose a row out of several different runs and report a
 * machine that never existed. So one repetition wins and every number shown for
 * the group is that repetition's.
 *
 * Repetitions are keyed by case directory, not by row: two summary lines
 * pointing into the same directory are one measurement recorded twice (the
 * older runs of a shared directory were overwritten in place, before the
 * pipeline gave each run its own), and listing it twice would invent a
 * repetition whose numbers are on disk exactly once.
 */
export function groupByTest(
  rows: BenchMatrixRow[],
  primaryMetric: string | null | undefined,
  index?: MetricIndex,
): TestGroup[] {
  const seenCase = new Map<string, Set<string>>()
  const byTest = new Map<string, BenchMatrixRow[]>()
  for (const row of rows) {
    const key = testKey(row)
    const cases = seenCase.get(key) ?? new Set<string>()
    const caseId = row.case_dir || row.log_file
    if (caseId && cases.has(caseId)) continue
    if (caseId) cases.add(caseId)
    seenCase.set(key, cases)
    byTest.set(key, [...(byTest.get(key) ?? []), row])
  }

  const meta = primaryMetric ? index?.get(primaryMetric) : undefined
  // Absent a descriptor, a primary metric named by the pipeline is still assumed
  // to be one where more is better -- it is the throughput column.
  const higherIsBetter = meta?.higher_is_better ?? true

  const groups: TestGroup[] = []
  for (const [key, unsorted] of byTest) {
    const runs = [...unsorted].sort((a, b) => b.updated_at - a.updated_at)
    const scored = primaryMetric
      ? runs.filter((row) => Number.isFinite(row.metrics[primaryMetric]))
      : []
    const best = scored.length
      ? scored.reduce((winner, row) =>
          (higherIsBetter
            ? row.metrics[primaryMetric!] > winner.metrics[primaryMetric!]
            : row.metrics[primaryMetric!] < winner.metrics[primaryMetric!])
            ? row
            : winner,
        )
      : runs[0]
    groups.push({
      key,
      model: best.model,
      precision: best.precision,
      quant: best.quant,
      device: best.device,
      task: best.task,
      runs,
      best,
      measured: runs.filter((row) => Object.keys(row.metrics).length > 0).length,
    })
  }
  groups.sort(
    (a, b) =>
      a.model.localeCompare(b.model) ||
      a.precision.localeCompare(b.precision) ||
      a.device.localeCompare(b.device),
  )
  return groups
}

// --- jobs ----------------------------------------------------------------
//
// A job is one press of Run: the backend names a run directory per device and
// puts the shared part of the name back together (benchmark/service/results.py's
// _job_name), so every case a single request produced carries the same `job`.

/** When a job ran: its own start time, or when its files last changed. */
export function jobTime(row: BenchMatrixRow): number {
  return row.job_started_at ?? row.updated_at
}

/**
 * The rows of the most recent job -- what the last press of Run produced.
 *
 * This is what the filters on both tabs start out showing. The alternative,
 * everything ever measured, is not a neutral default: it is a claim that the
 * hundred cases from last week are as relevant as the six from a minute ago,
 * and it hides the six among them.
 *
 * A tree with no dateable jobs (the legacy TEST_CPU directories, or a tree
 * assembled by hand) returns nothing, and the caller falls back to everything --
 * there is no "last" run to prefer.
 */
export function latestJobRows(rows: BenchMatrixRow[]): BenchMatrixRow[] {
  let latest: string | null = null
  let at = -Infinity
  for (const row of rows) {
    const job = row.job || row.run
    if (!job) continue
    const when = jobTime(row)
    if (when > at) {
      at = when
      latest = job
    }
  }
  return latest === null ? [] : rows.filter((row) => (row.job || row.run) === latest)
}

export interface JobLabel {
  /** 1 for the oldest job on disk. */
  index: number
  /** When it ran -- see jobTime. */
  at: number
  /** Whether `at` is the job's own start or a directory's mtime. */
  exact: boolean
  /** "Job3", for a dropdown option or an axis tick. */
  short: string
  /** "Job3 · 04/09/2026, 18:12:45", for a heading with room for it. */
  long: string
}

/**
 * A readable name per job: Job1 is the oldest, counting forward.
 *
 * The directory name a job carries ("20260904_181245_ab12cd") is a timestamp
 * and a uuid fragment, which identifies a job perfectly and names it not at all
 * -- in a dropdown it is unreadable, and two of them differ in the middle.
 *
 * Numbered oldest-first rather than newest-first so that a number means
 * something across a conversation: pressing Run appends JobN+1 and leaves every
 * existing number where it was. Newest-first would renumber the whole history
 * on every run, so "look at Job2" would mean a different job by the time it was
 * read.
 *
 * Undateable trees (the legacy TEST_* directories) sort by mtime through
 * jobTime, so they are numbered too rather than left out.
 */
export function jobLabels(rows: BenchMatrixRow[]): Map<string, JobLabel> {
  const byJob = new Map<string, { at: number; exact: boolean }>()
  for (const row of rows) {
    const job = row.job || row.run
    if (!job) continue
    const at = jobTime(row)
    const seen = byJob.get(job)
    // The earliest sighting: a job's runs finish at different times, and it is
    // one moment, not several.
    if (!seen || at < seen.at) byJob.set(job, { at, exact: row.job_started_at != null })
  }
  const ordered = [...byJob.entries()].sort((a, b) => a[1].at - b[1].at)
  return new Map(
    ordered.map(([job, { at, exact }], position) => {
      const index = position + 1
      const short = `Job${index}`
      return [job, {
        index,
        at,
        exact,
        short,
        long: `${short} · ${new Date(at * 1000).toLocaleString()}`,
      }]
    }),
  )
}

/** The distinct values one field takes across a set of rows, in sorted order. */
export function distinctOf(rows: BenchMatrixRow[], field: 'model' | 'device' | 'precision' | 'job'): string[] {
  return [...new Set(rows.map((row) => row[field]).filter(Boolean))].sort()
}

/** How a case is named on an axis: the model, then whatever else varies. */
export function caseLabel(row: BenchMatrixRow, showPrecision = true): string {
  return showPrecision && row.precision ? `${row.model} · ${row.precision}` : row.model
}

/** Devices read as acronyms; the data carries them lowercased for grouping. */
export function deviceLabel(device: string): string {
  return device.toUpperCase()
}

/**
 * Whether a result row is about ``modelId``.
 *
 * The pipeline records a model under the name it gave the directory
 * ("Qwen_Qwen3-8B"), while the browser knows it as a repo id ("Qwen/Qwen3-8B"),
 * and the flattening is not reversible -- both "/" and "." became "_". Comparing
 * the separator-insensitive forms bridges the two without teaching the UI the
 * pipeline's naming rules.
 */
export function matchesModel(name: string, modelId: string): boolean {
  const normalize = (value: string) => value.toLowerCase().replace(/[/_.-]/g, '')
  return normalize(name ?? '').includes(normalize(modelId.split('/').pop() ?? modelId))
}
