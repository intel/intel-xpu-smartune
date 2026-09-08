// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// Comparing benchmark results across devices, precisions, models and jobs.
//
// A run directory holds one device, so the Results tab -- one table per job --
// can show what happened but never put two devices side by side. This view takes
// the flattened matrix and answers the comparison questions directly.
//
// Two views, because there are two questions:
//
//   Job       "which device is fastest for this model?"   -- one job, inside it
//   Compare   "is this run better than the last one?"     -- several jobs
//
// Job takes exactly one job and shows what happened inside it; Compare takes
// several and puts them side by side. The single-job form used to accept a set
// of jobs and fold each test down to its best repetition across them, which is a
// third question nobody asked -- and the answer it gave named no job, so a bar
// could come from a run three days older than the one beside it.
//
// Colour means the model, in both views and both chart forms. It used to mean
// the device (Job) or the job (Compare), which put the model name in every tick
// of the category axis: the longest part of the label, repeated, and the one that
// varied least. Now the axis says what is left -- the precision, and which job in
// Compare -- the device names a group of ticks under a rule of its own, and the
// legend carries the models.
//
// One panel component draws both views: whatever makes a mark readable -- the
// colours, the tooltip, the click-through to the case -- is decided once, and the
// two views cannot end up disagreeing about what a colour means.
//
// Up to three metrics can be plotted at a time, one panel each, rather than one
// metric with a second y-axis: TTFT is hundreds of milliseconds and TPOT is
// tens, so a shared linear axis flattens TPOT into the baseline, and a second
// axis for it would put the two series' crossing point wherever the scaling
// happened to fall. Same categories, same colours, one scale each.
//
// Bars or lines, per panel: bars for "how do these compare", lines for the trend
// a model traces across devices and precisions. Same categories, same colours,
// same click-through -- only the mark differs.
//
// Deliberately absent: a pie of throughput by device (three devices running the
// same model are alternatives, not slices of one quantity -- a pie would claim
// they sum to something), a treemap (the data has three orthogonal dimensions
// and no hierarchy to nest), and any second y-axis.

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Divider,
  Empty,
  Select,
  Space,
  Tabs,
  Tag,
  Tooltip as AntTooltip,
  Typography,
  message,
} from 'antd'
import { BarChartOutlined, DownloadOutlined, LineChartOutlined } from '@ant-design/icons'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import type { BenchMatrixData, BenchMatrixRow, BenchMetricMeta } from '../api/types'
import { passesDimension, useDimension } from '../hooks/useDimension'
import { COLORS } from '../styles/theme'
import {
  deviceLabel,
  distinctOf,
  formatMetric,
  groupByTest,
  indexMetrics,
  jobLabels,
  latestJobRows,
  metricTitle,
} from '../utils/benchMetrics'

const { Text } = Typography

// The categorical order the models are coloured from, in fixed slot order.
// Validated against this dashboard's chart surface (#1a1d2e) as a set: all eight
// clear the lightness band, the chroma floor and 3:1 contrast, the worst
// adjacent pair is yellow/aqua at deltaE 8.4 under protanopia, and the worst
// adjacent normal-vision pair is magenta/yellow at 19.3.
//
// The first three slots are the ones this chart has always used, so a screenshot
// of an older comparison still reads the same way. Slots four and five moved
// when the palette grew: the violet and the muted yellow they held could not be
// told apart from the new slots seven and four.
const CATEGORICAL = [
  '#3987e5', // blue
  '#d95926', // orange
  '#199e70', // aqua
  '#c98500', // yellow
  '#d55181', // magenta
  '#008300', // green
  '#9085e9', // violet
  '#e66767', // red
]

// How the devices read, on an axis and in a legend: the one every machine has,
// then the two it might. Alphabetical would put GPU before NPU by accident and
// CPU first by luck; this says it. Anything else follows, alphabetically.
const DEVICE_ORDER = ['cpu', 'gpu', 'npu']

const AXIS_TICK = { fill: COLORS.textMuted, fontSize: 11 }
const AXIS_LABEL = { fill: COLORS.textMuted, fontSize: 11 }
const TOOLTIP_STYLE = {
  background: COLORS.panelBg,
  border: `1px solid ${COLORS.border}`,
  color: COLORS.text,
}
const PANEL_HEIGHT = 340

// The height a group rule under the axis takes: the line, the device's name, and
// air above the panel below it.
const GROUP_BAND_HEIGHT = 22

// The category axis, and the geometry that keeps its ticks off each other and
// off the axis title.
//
// A tick here is "precision · job", and three panels share the window's width.
// Laid out horizontally the ticks overlapped from the fourth category on, so
// they are angled -- and angling makes the three constraints geometric rather
// than a matter of taste:
//
//   * two neighbours collide when the distance between their baselines,
//     band * sin(angle), drops below a line of text. Steepening the angle buys
//     room without narrowing anything.
//   * a tick of n characters reaches n * CHAR_WIDTH * sin(angle) below the axis,
//     so how many characters are shown decides how tall the axis band must be.
//   * the device rule sits under that band. Fixing the band's height and letting
//     the ticks grow into it is what wrote the rule through the middle of the
//     longest tick -- so the height follows the ticks, rather than the ticks
//     being cut to a height chosen in advance.
//
// All three are computed per panel from its measured width -- see `tickPlan` and
// `axisHeight` -- rather than fixed at numbers that happen to work at one
// window size.
//
// The tallest the band is allowed to get. Past this the panel is more axis than
// chart, and the ticks are cut instead.
const MAX_X_AXIS_HEIGHT = 112
// Left below the longest tick, so the group rule under the axis does not start
// against it, and above the ticks for the tick line. What the ticks *are* used
// to be written inside this band; it is under the group rule now (see
// AxisCaption), which is why this is air rather than a line of text.
const TICK_TAIL_ROOM = 6
const TICK_GAP = 10
// At 11px in this stack's UI font, over model names -- which are mixed case with
// digits and underscores, so wider than prose.
const CHAR_WIDTH = 7
const TICK_LINE_HEIGHT = 13
// The gentler angle reads best; the steeper one is what a crowded axis falls
// back to, since it packs more baselines into the same width.
const TICK_ANGLES = [-35, -55] as const
// Never fewer characters than this, whatever the arithmetic says: "Qwen…" names
// nothing. Past 24 the label is longer than any tooltip-free glance needs.
const MIN_TICK_CHARS = 6
const MAX_TICK_CHARS = 24
// What a panel needs per band: one bar, and room for the figure above it --
// four significant digits at 10px. Below this the panels wrap to fewer per row
// rather than the ticks being thinned or the numbers dropped.
const MIN_BAR_SLOT = 26
// The same, for the figure over a line's point. Wider than a bar's band: a bar
// chart puts one number per band, a line chart puts one per model on the same
// band, so they need room to sit beside each other rather than on top.
const MIN_POINT_LABEL_SLOT = 44
// The gutters either side of the plot area: the y-axis and the chart's right
// margin are not plot area. The group rule under the axis is laid out inside
// the same gutters, which is what keeps it under the bars it names.
const Y_AXIS_WIDTH = 68
const PLOT_RIGHT_MARGIN = 16
const PLOT_CHROME = Y_AXIS_WIDTH + PLOT_RIGHT_MARGIN

// A grid has to be visible to do its job -- reading a value off a chart means
// following a line back to the axis. The border token at partial alpha was in
// the DOM and not on the screen: #2d3149 at 60% over the panel came out at
// 1.17:1 against it, which is below what a display resolves. Stepped off the
// muted-text colour instead, the grid lands at 1.59:1 and the axes at 2.09:1 --
// present, and still recessive next to the marks.
const GRID_STROKE = 'rgba(142, 154, 179, 0.28)'
const AXIS_STROKE = 'rgba(142, 154, 179, 0.45)'

// Legends sit above the plot, right-aligned. At the bottom, recharts stacks the
// legend under the x-axis label and the two collide as soon as either wraps.
const LEGEND_PROPS = {
  verticalAlign: 'top' as const,
  align: 'right' as const,
  wrapperStyle: { fontSize: 12, paddingBottom: 8 },
}

// How many metrics can be plotted side by side. Three panels still fit across a
// laptop window at a width where each keeps its own readable axis; a fourth
// makes every one of them too narrow to read, which is a worse answer to "show
// me these four" than saying no.
const MAX_METRICS = 3

// How many models get a colour. One per validated hue: a ninth series would have
// to be a generated colour, which is the one thing a validated palette cannot
// absorb. Over the cap the most-measured models are drawn and the rest are
// named, never silently dropped.
const MAX_MODELS = CATEGORICAL.length

// How many jobs the Compare view will draw. Jobs are ticks rather than colours,
// so this is not a palette limit -- it is that five repetitions of one
// configuration, side by side under one label, stop being comparable. Over the
// cap the most recent are drawn and the rest are named.
const MAX_JOBS = 5

type View = 'job' | 'compare'
type ChartKind = 'bar' | 'line'

/**
 * What a category is made of, in the order it sorts and reads.
 *
 * The model is not here: it is the colour. `GROUP_PART` is the one that becomes
 * a rule under the axis instead of tick text, so the ticks say the rest -- the
 * precision, and which job when Compare has more than one.
 */
const CATEGORY_PARTS = ['device', 'precision', 'job'] as const
const GROUP_PART = 'device'
/** The parts that end up as tick text: everything the group rule does not take. */
const TICK_PARTS = CATEGORY_PARTS.filter((part) => part !== GROUP_PART)

function truncate(value: string, max: number): string {
  return value.length > max ? `${value.slice(0, max - 1)}…` : value
}

const sineOf = (degrees: number) => Math.sin((Math.abs(degrees) * Math.PI) / 180)

/**
 * How to draw a category axis of `labels` in `width` pixels.
 *
 * The gentle angle wherever the baselines are far enough apart for it, the
 * steeper one when they are not; a character budget that follows from whichever
 * angle was chosen; and a band tall enough for the longest tick that survives
 * the budget, so what comes below it -- the group rule -- is never written over.
 * Width 0 is a panel that has not been measured yet; the gentle angle is the
 * right guess for the first frame.
 */
function tickPlan(
  width: number,
  labels: string[],
): { angle: number; chars: number; height: number } {
  const band = labels.length > 0 ? Math.max(0, width - PLOT_CHROME) / labels.length : 0
  const angle =
    width === 0 || band * sineOf(TICK_ANGLES[0]) >= TICK_LINE_HEIGHT
      ? TICK_ANGLES[0]
      : TICK_ANGLES[1]
  const fits = Math.floor(
    (MAX_X_AXIS_HEIGHT - TICK_TAIL_ROOM - TICK_GAP) / (sineOf(angle) * CHAR_WIDTH),
  )
  const chars = Math.max(MIN_TICK_CHARS, Math.min(MAX_TICK_CHARS, fits))
  // The longest tick as it will actually be drawn, not as it arrived: a panel of
  // short labels gets a short axis and spends the height on the plot instead.
  const longest = labels.reduce((most, label) => Math.max(most, Math.min(label.length, chars)), 0)
  const height = Math.min(
    MAX_X_AXIS_HEIGHT,
    Math.ceil(longest * CHAR_WIDTH * sineOf(angle)) + TICK_GAP + TICK_TAIL_ROOM,
  )
  return { angle, chars, height }
}

/** An element's width, kept current as the window is resized. */
function useWidth<T extends HTMLElement>(): [React.RefObject<T>, number] {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(0)
  useEffect(() => {
    const node = ref.current
    if (!node) return
    // ResponsiveContainer already re-renders the chart on resize; this is the
    // same measurement taken one level up, where the tick budget is decided.
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width))
    observer.observe(node)
    setWidth(node.getBoundingClientRect().width)
    return () => observer.disconnect()
  }, [])
  return [ref, width]
}

/**
 * One angled tick of the category axis.
 *
 * Rotation is written into the element rather than handed to recharts as a
 * `tick` object: that path runs the props through an SVG attribute filter, so
 * whether the angle survives is a fact about the library's allow-list rather
 * than about this chart. The full name is in the tooltip and in the case drawer
 * a click away, so truncating here loses nothing.
 *
 * The text comes from `labels` by band index rather than from the axis value:
 * one configuration occupies as many bands as it has bars, and it is named once,
 * under the middle of them -- which is what `shifts` moves the text by. Bands
 * with no name of their own render nothing.
 */
function CategoryTick(props: {
  x?: number
  y?: number
  index?: number
  angle: number
  chars: number
  labels: string[]
  shifts: number[]
}) {
  const { x = 0, y = 0, index = 0, angle, chars, labels, shifts } = props
  const text = labels[index] ?? ''
  if (!text) return null
  const at = x + (shifts[index] ?? 0)
  return (
    <text
      x={at}
      y={y}
      dy={4}
      textAnchor="end"
      transform={`rotate(${angle}, ${at}, ${y})`}
      fill={COLORS.textMuted}
      fontSize={11}
    >
      {truncate(text, chars)}
    </text>
  )
}

/** "TTFT (ms)", from a descriptor already in hand rather than by key lookup. */
function titleOf(metric: BenchMetricMeta): string {
  return metric.unit ? `${metric.label} (${metric.unit})` : metric.label
}

/** One series of a chart: a model, in both views and both chart forms. */
interface Series {
  /** How a row is looked up in a category's cells -- the model as recorded. */
  key: string
  /** What the legend and the tooltip call it. */
  label: string
  /** The model in full, for a tooltip that has room for it. */
  title: string
  color: string
}

/** One tick of the category axis, holding the row each series reached there. */
interface Category {
  /** Identity -- every part, always. Used as a key and in the tooltip. */
  name: string
  /** What the tick says, which drops whatever does not vary. */
  label: string
  /** The group this category belongs to: the device, named under a rule. */
  group: string
  cells: Map<string, BenchMatrixRow>
  /**
   * An empty band between two groups rather than a configuration.
   *
   * A bar chart's categories are evenly spaced, so the only way to say "these
   * four belong together and those four are something else" in the axis itself
   * is to leave a band empty. It carries no cells, so no bar and no tick are
   * drawn for it -- see `categorize`.
   */
  spacer?: boolean
}

/**
 * Category labels with the invariant parts dropped, and a name for the axis.
 *
 * A single job's comparison varies in precision alone, so a tick reading
 * "int4 · Job7" spends half its width on the part every tick shares. The axis
 * title then has to say what is left, or the ticks read as unlabelled strings.
 */
function labelParts(
  parts: string[][],
  names: readonly string[],
): { labels: string[]; title: string } {
  const varies = names.map((_, i) => new Set(parts.map((row) => row[i])).size > 1)
  // Everything constant means one category: keep it whole rather than labelling
  // it with the empty string.
  const keep = varies.some(Boolean) ? varies : names.map(() => true)
  return {
    labels: parts.map((row) => row.filter((_, i) => keep[i] && row[i]).join(' · ')),
    title: names.filter((_, i) => keep[i]).join(' · '),
  }
}

interface Props {
  matrix: BenchMatrixData | null
  onOpenCase: (row: BenchMatrixRow) => void
}

export default function BenchCompare({ matrix, onOpenCase }: Props) {
  const [view, setView] = useState<View>('job')
  const [chart, setChart] = useState<ChartKind>('bar')
  const dims = matrix?.dimensions
  // The last press of Run, until the reader widens it -- and whatever a later
  // run adds. Everything-by-default puts a fresh three-bar comparison in among
  // however many the machine has accumulated; see useDimension.
  const latest = useMemo(() => latestJobRows(matrix?.rows ?? []), [matrix?.rows])
  const [models, setModels] = useDimension(dims?.models, distinctOf(latest, 'model'))
  const [devices, setDevices] = useDimension(dims?.devices, distinctOf(latest, 'device'))
  const [precisions, setPrecisions] = useDimension(dims?.precisions, distinctOf(latest, 'precision'))
  const [metricKeys, setMetricKeys] = useState<string[] | null>(null)
  const metricIndex = useMemo(() => indexMetrics(matrix?.metrics), [matrix?.metrics])
  const labels = useMemo(() => jobLabels(matrix?.rows ?? []), [matrix?.rows])

  // Jobs, newest first -- the axis the backend deliberately does not sort
  // alphabetically (see results.py). Both job controls read from it.
  const jobOptions = useMemo(
    () => (dims?.jobs ?? []).map((job) => ({
      value: job,
      label: labels.get(job)?.short ?? job,
      title: labels.get(job)?.long ?? job,
    })),
    [dims?.jobs, labels],
  )

  // Which single job the Job view is showing: the reader's, while it still
  // exists on disk, else the last press of Run.
  const [pickedJob, setPickedJob] = useState<string | null>(null)
  const soloJob = useMemo(() => {
    const all = dims?.jobs ?? []
    if (pickedJob && all.includes(pickedJob)) return pickedJob
    return (latest[0]?.job || latest[0]?.run) ?? all[0] ?? null
  }, [pickedJob, dims?.jobs, latest])

  // Which runs Compare is comparing. This used to be implicit -- the charts read
  // every job on disk and folded each test down to its best repetition, which
  // no control above them said and no reader would guess.
  //
  // Seeded with the two most recent rather than only the newest: one job in a
  // view whose question is "is this run better than the last one?" can only
  // answer it after the reader has added the other half.
  const recentJobs = useMemo(() => (dims?.jobs ?? []).slice(0, 2), [dims?.jobs])
  const [jobs, setJobs] = useDimension(dims?.jobs, recentJobs)

  // Throughput is the question people arrive with; anything else only if this
  // pipeline did not measure it.
  const metrics: BenchMetricMeta[] = useMemo(() => {
    const list = matrix?.metrics ?? []
    if (metricKeys) {
      const chosen = metricKeys.map((key) => list.find((m) => m.key === key)).filter(Boolean)
      if (chosen.length) return chosen as BenchMetricMeta[]
    }
    const preferred = list.find((m) => m.key === 'kpi_throughput_tokens_s') ?? list[0]
    return preferred ? [preferred] : []
  }, [matrix?.metrics, metricKeys])

  const pickMetrics = useCallback((next: string[]) => {
    if (next.length > MAX_METRICS) {
      message.warning(
        `Three metrics at a time — a fourth panel is too narrow to read. ` +
        `Remove one first.`,
      )
      return
    }
    setMetricKeys(next)
  }, [])

  // Empty selections are honoured rather than read as "all": the charts have to
  // agree with the controls above them, in both directions. The job axis is a
  // different control in each view, so it is asked about separately.
  const nothingSelected =
    models.length === 0 ||
    devices.length === 0 ||
    precisions.length === 0 ||
    (view === 'job' ? soloJob === null : jobs.length === 0)

  const rows = useMemo(() => {
    const all = matrix?.rows ?? []
    return all.filter(
      (row) =>
        passesDimension(row.model, models) &&
        passesDimension(row.device, devices) &&
        passesDimension(row.precision, precisions) &&
        (view === 'job'
          ? (row.job || row.run) === soloJob
          : passesDimension(row.job || row.run, jobs)),
    )
  }, [matrix?.rows, models, devices, precisions, jobs, soloJob, view])

  // One case per test: the best of however many times it was run.
  //
  // Within one job a repeated test is rare but possible -- a sweep can measure
  // the same configuration twice -- and a chart that plotted both would put two
  // bars for one configuration under the same tick and invite the reader to
  // compare a test against itself. Which *run* was better is Compare's question,
  // and it answers it by keying on the job rather than by folding.
  const cases = useMemo(
    () => groupByTest(rows, matrix?.primary_metric, metricIndex).map((group) => group.best),
    [rows, matrix?.primary_metric, metricIndex],
  )

  // Which jobs Compare is comparing, oldest first, so a tick further right is a
  // later run.
  const comparedJobs = useMemo(() => {
    const ordered = jobs
      .filter((job) => labels.has(job))
      .sort((a, b) => (labels.get(a)!.index - labels.get(b)!.index))
    return ordered.slice(Math.max(0, ordered.length - MAX_JOBS))
  }, [jobs, labels])

  // Folded per job, not across them: two runs of one configuration are the two
  // ticks this view exists to put side by side.
  const jobCases = useMemo(
    () => comparedJobs.flatMap((job) => {
      const ofJob = rows.filter((row) => (row.job || row.run) === job)
      return groupByTest(ofJob, matrix?.primary_metric, metricIndex).map((group) => group.best)
    }),
    [comparedJobs, rows, matrix?.primary_metric, metricIndex],
  )

  // The cases the current view is actually drawing, one row each. Compare keeps
  // a job's repetitions apart, so its set is not the other view's: exporting or
  // listing failures from `cases` while Compare was on screen reported figures
  // folded across jobs -- numbers that are on no chart in this tab.
  const shownRows = useMemo(
    () => (view === 'job' ? cases : jobCases),
    [view, cases, jobCases],
  )

  // Colour is the model, so which colour a model gets has to survive the reader
  // taking a different model out of the selection: the slot follows the model's
  // place on the axis the backend published, not its place in the selection.
  const modelSlot = useMemo(
    () => new Map((dims?.models ?? []).map((model, position) => [model, position])),
    [dims?.models],
  )

  // The models with a colour, and how many did not fit. Over the cap the
  // most-measured are kept -- a model with one bar contributes least to a
  // comparison -- and the count is reported above the charts.
  const drawn = useMemo(() => rankModels(shownRows, modelSlot), [shownRows, modelSlot])

  const modelSeries: Series[] = useMemo(() => {
    const colors = colorsFor(drawn.models, modelSlot)
    return drawn.models.map((model) => ({
      key: model,
      label: modelLabel(model),
      title: model,
      color: colors.get(model) ?? CATEGORICAL[0],
    }))
  }, [drawn.models, modelSlot])

  // One tick per device+precision(+job), one mark per model under it.
  const shown = useMemo(() => {
    const keep = new Set(drawn.models)
    const jobText = (row: BenchMatrixRow) => {
      const job = row.job || row.run
      const label = labels.get(job)
      return {
        text: label?.short ?? job,
        // Job10 sorts after Job9 by number, which it does not by name.
        sort: String(label?.index ?? 999).padStart(4, '0'),
      }
    }
    return categorize(shownRows.filter((row) => keep.has(row.model)), jobText)
  }, [shownRows, drawn.models, labels])

  // A test counts as failed only when its best run failed, which for a repeated
  // test means every run did. One bad run out of three is not a failure of the
  // test; it is visible as such in the Results tab.
  const failures = useMemo(() => shownRows.filter((row) => row.status !== 'ok'), [shownRows])

  const exportCsv = useCallback(() => {
    const keys = (matrix?.metrics ?? []).map((m) => m.key)
    // Units belong in the header: a bare "48.31" in a spreadsheet cell is not a
    // measurement, and the descriptor is the only place the unit exists.
    const header = [
      'backend', 'job', 'run', 'model', 'precision', 'quant', 'device', 'mode',
      'batch_size', 'status', 'failure_reason',
      ...keys.map((key) => {
        const unit = metricIndex.get(key)?.unit
        return unit ? `${key} (${unit})` : key
      }),
    ]
    const escape = (value: unknown) => {
      const text = value === undefined || value === null ? '' : String(value)
      return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
    }
    // What the charts show, not every row on disk: a CSV that disagrees with the
    // chart it was exported beside is worse than no CSV. The job and run columns
    // say which repetition each figure came from.
    const lines = [header.join(',')]
    for (const row of shownRows) {
      const job = row.job || row.run
      lines.push([
        row.backend, labels.get(job)?.short ?? job, row.run, row.model, row.precision,
        row.quant, row.device, row.mode, row.batch_size, row.status, row.failure_reason ?? '',
        ...keys.map((key) => row.metrics[key] ?? ''),
      ].map(escape).join(','))
    }
    const url = URL.createObjectURL(new Blob([lines.join('\n')], { type: 'text/csv' }))
    const link = document.createElement('a')
    link.href = url
    link.download = 'benchmark-comparison.csv'
    link.click()
    URL.revokeObjectURL(url)
  }, [matrix?.metrics, metricIndex, shownRows, labels])

  if (!matrix || matrix.rows.length === 0) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="Nothing to compare yet — run a benchmark on more than one device."
      />
    )
  }
  if (!matrix.metrics_available) {
    return (
      <Alert
        type="info"
        showIcon
        message="No measurements to compare"
        description="The runs on disk have no hardware metrics: their aggregation step never produced windowed_metric_medians.csv. The Results tab still lists which cases ran."
      />
    )
  }

  const selector = (
    label: string,
    value: string[],
    options: string[],
    onChange: (next: string[]) => void,
    render: (option: string) => string = (o) => o,
    title: (option: string) => string = (o) => o,
    // Model names are two or three times the length of a device or a precision,
    // so the box that holds them is wider than the boxes that do not need it.
    width: { minWidth: number; maxWidth: number } = { minWidth: 270, maxWidth: 480 },
  ) => (
    <Select
      mode="multiple"
      allowClear
      size="small"
      style={width}
      // Only reached when the reader has cleared the box, which is why it says
      // what that means rather than naming the dimension.
      placeholder={`No ${label} selected`}
      value={value}
      maxTagCount="responsive"
      onChange={onChange}
      options={options.map((option) => ({
        value: option,
        label: render(option),
        title: title(option),
      }))}
    />
  )

  const droppedJobs = view === 'compare' ? jobs.length - comparedJobs.length : 0

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      {/* The view is a tab strip, above everything it governs. It used to be a
          Segmented among the filters, where it read as one of six settings of
          equal weight -- but it is not one of them: the filters change which
          marks there are, while this changes what the whole panel is about, and
          which controls below it even exist. A tab is the shape that says
          "everything under here belongs to this one". */}
      <div>
        <Tabs
          size="small"
          activeKey={view}
          onChange={(key) => setView(key as View)}
          // The panes are the rest of this component, not children of the strip:
          // a tab pane would nest a scroll area inside the one the Analysis tab
          // already is.
          items={[
            { key: 'job', label: 'One job' },
            { key: 'compare', label: 'Compare' },
          ]}
          style={{ marginBottom: -4 }}
        />
        <Text type="secondary" style={{ fontSize: 12 }}>
          {view === 'job' ? (
            <>
              {labels.get(soloJob ?? '')?.short ?? soloJob ?? 'No job'} · {cases.length} test
              {cases.length === 1 ? '' : 's'} · {shown.count}{' '}
              configuration{shown.count === 1 ? '' : 's'}
              {/* A job can measure one configuration more than once. */}
              {cases.length < rows.length && ` · best of ${rows.length} runs`}
            </>
          ) : (
            <>
              {comparedJobs.length} job{comparedJobs.length === 1 ? '' : 's'} ·{' '}
              {shown.count} configuration{shown.count === 1 ? '' : 's'}
              {/* Never a silent cap. */}
              {droppedJobs > 0 &&
                ` · showing the ${MAX_JOBS} most recent of ${jobs.length} selected`}
            </>
          )}
          {/* The other cap: colour is the model, and there are only so many
              colours. Said here rather than left for the reader to count. */}
          {drawn.dropped > 0 &&
            ` · showing the ${MAX_MODELS} most-measured of ${drawn.models.length + drawn.dropped} models`}
          {chart === 'bar' ? ' · click a bar for the full case' : ' · click a point for the full case'}
        </Text>
        <Divider style={{ margin: '10px 0 12px' }} />
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          {/* Which runs... */}
          <Space size={8} wrap>
            {view === 'job' ? (
              <AntTooltip title="Which run to look inside. One job is one press of Run, across every device it swept; Job1 is the oldest.">
                <Select
                  size="small"
                  style={{ minWidth: 270, maxWidth: 480 }}
                  placeholder="No job"
                  value={soloJob ?? undefined}
                  onChange={setPickedJob}
                  showSearch
                  optionFilterProp="label"
                  options={jobOptions}
                />
              </AntTooltip>
            ) : (
              <AntTooltip title="Which runs to compare. One job is one press of Run, across every device it swept; Job1 is the oldest.">
                {selector(
                  'jobs', jobs, dims?.jobs ?? [], setJobs,
                  (job) => labels.get(job)?.short ?? job,
                  (job) => labels.get(job)?.long ?? job,
                )}
              </AntTooltip>
            )}
          </Space>
          {/* ...of what... */}
          <Space size={8} wrap>
            {selector('models', models, dims?.models ?? [], setModels, undefined, undefined, {
              minWidth: 405,
              maxWidth: 720,
            })}
            {selector('devices', devices, dims?.devices ?? [], setDevices, deviceLabel)}
            {selector('precisions', precisions, dims?.precisions ?? [], setPrecisions)}
          </Space>
          {/* ...measuring what, drawn how. */}
          <Space size={8} wrap>
            <AntTooltip title={`Up to ${MAX_METRICS} metrics, one panel each.`}>
              <Select
                mode="multiple"
                size="small"
                style={{ minWidth: 330, maxWidth: 520 }}
                placeholder="Pick a metric"
                maxTagCount="responsive"
                value={metrics.map((m) => m.key)}
                onChange={pickMetrics}
                options={[...new Map(
                  (matrix.metrics ?? []).map((m) => [m.group_label, m.group_label]),
                ).keys()].map((group) => ({
                  label: group,
                  options: (matrix.metrics ?? [])
                    .filter((m) => m.group_label === group)
                    .map((m) => ({
                      value: m.key,
                      label: metricTitle(m.key, metricIndex),
                      title: m.description ?? metricTitle(m.key, metricIndex),
                    })),
                }))}
              />
            </AntTooltip>
            {/* Bars answer "which of these is bigger", lines answer "how does
                this model move across the devices" -- the same numbers, and the
                reader knows which question they arrived with. */}
            <AntTooltip title="Bars compare configurations; lines trace each model across them.">
              <Select
                size="small"
                style={{ width: 128 }}
                value={chart}
                onChange={(value) => setChart(value)}
                options={[
                  { value: 'bar', label: <Space size={6}><BarChartOutlined />Bars</Space> },
                  { value: 'line', label: <Space size={6}><LineChartOutlined />Lines</Space> },
                ]}
              />
            </AntTooltip>
            <Button size="small" icon={<DownloadOutlined />} onClick={exportCsv}>
              CSV
            </Button>
          </Space>
        </Space>
      </div>

      {view === 'compare' && comparedJobs.length < 2 && !nothingSelected && (
        <Alert
          type="info"
          showIcon
          message="Select more than one job above to compare runs against each other."
        />
      )}

      {nothingSelected ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            view === 'job'
              ? 'Nothing selected — pick a job, and at least one model, device and precision above.'
              : 'Nothing selected — pick at least one model, device, precision and job above.'
          }
        />
      ) : (
        <MetricPanels
          categories={shown.categories}
          series={modelSeries}
          metrics={metrics}
          xTitle={shown.title}
          chart={chart}
          onOpenCase={onOpenCase}
        />
      )}

      {/* Failed cases never reach a chart -- they have no numbers to plot. Listed
          here so an empty slot reads as "this was attempted and did not work"
          rather than "this was never tried". */}
      {failures.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message={`${failures.length} case${failures.length === 1 ? '' : 's'} produced no measurement`}
          description={
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              {failures.map((row) => (
                <Text
                  key={row.case_dir || `${row.run}-${row.model}`}
                  style={{ fontSize: 12, cursor: 'pointer' }}
                  onClick={() => onOpenCase(row)}
                >
                  <Tag color="error">{deviceLabel(row.device)}</Tag>
                  {row.precision ? `${row.model} · ${row.precision}` : row.model} —{' '}
                  {truncate(row.failure_reason ?? row.status, 180)}
                </Text>
              ))}
            </Space>
          }
        />
      )}
    </Space>
  )
}
// --- shaping ---------------------------------------------------------------

/** How a model reads in a legend: its own name, without the publisher. */
function modelLabel(model: string): string {
  return truncate(model.split('/').pop() ?? model, 28)
}

/**
 * Which models get a colour, and how many did not fit.
 *
 * The palette holds MAX_MODELS validated hues and a ninth would have to be
 * generated, so a wider selection is cut -- by how much each model was actually
 * measured, since a model with one bar contributes least to a comparison. The
 * survivors are put back into the model axis's order, which is what decides
 * their colour: see colorsFor.
 */
function rankModels(
  rows: BenchMatrixRow[],
  slot: Map<string, number>,
): { models: string[]; dropped: number } {
  const measured = new Map<string, number>()
  for (const row of rows) {
    if (!row.model) continue
    const has = Object.keys(row.metrics).length > 0 ? 1 : 0
    measured.set(row.model, (measured.get(row.model) ?? 0) + has)
  }
  const all = [...measured.keys()]
  const kept = [...all]
    .sort((a, b) => (measured.get(b) ?? 0) - (measured.get(a) ?? 0) || a.localeCompare(b))
    .slice(0, MAX_MODELS)
  const order = (model: string) => slot.get(model) ?? Number.MAX_SAFE_INTEGER
  kept.sort((a, b) => order(a) - order(b) || a.localeCompare(b))
  return { models: kept, dropped: all.length - kept.length }
}

/**
 * A palette slot per model, taken from its place on the model axis.
 *
 * Colour follows the model, not its rank in the current selection: taking one
 * model out of the picture must not repaint the ones that are left. A machine
 * with more models on disk than the palette has hues wraps, so two of the models
 * being drawn can want the same slot -- the later one steps to the next free
 * hue rather than taking a second helping of the same one. There is always a
 * free slot, because at most MAX_MODELS models are drawn.
 */
function colorsFor(models: string[], slot: Map<string, number>): Map<string, string> {
  const taken = new Set<number>()
  const chosen = new Map<string, string>()
  models.forEach((model, index) => {
    let at = (slot.get(model) ?? index) % CATEGORICAL.length
    for (let step = 0; taken.has(at) && step < CATEGORICAL.length; step += 1) {
      at = (at + 1) % CATEGORICAL.length
    }
    taken.add(at)
    chosen.set(model, CATEGORICAL[at])
  })
  return chosen
}

/** One part of a category: what it says, and what it sorts by. */
interface Part {
  text: string
  sort: string
}

/**
 * Rows into axis categories: one tick per distinct combination of the parts.
 *
 * The order is the point. Sorted by name the axis ran model-first, which put one
 * model's CPU int4 next to its GPU fp16 and the *other* model's CPU int4 four
 * bars away -- so the comparison a reader actually makes, "how do these models do
 * on this device at this precision", was the one arrangement the chart did not
 * offer. Now the model is the colour and the axis runs device, then precision,
 * then job: every mark under one tick is the same configuration measured by
 * different models, and a device's ticks are adjacent.
 *
 * A device is not tick text -- it names a run of ticks, under a rule of its own
 * (see GroupBand). The groups are separated by an empty band so the eye does not
 * have to find the boundary in the labels.
 */
function categorize(
  rows: BenchMatrixRow[],
  /** How a row's job reads and sorts. Job10 is after Job9 by number, not name. */
  jobPart: (row: BenchMatrixRow) => Part,
): { categories: Category[]; title: string; count: number } {
  const partsOf = (row: BenchMatrixRow): Part[] =>
    CATEGORY_PARTS.map((part) => {
      if (part === 'device') {
        const known = DEVICE_ORDER.indexOf((row.device ?? '').toLowerCase())
        return {
          text: deviceLabel(row.device),
          // An unknown device sorts after the three named ones rather than
          // before them, which is where a reader looks for a surprise.
          sort: String(known < 0 ? 99 : known).padStart(2, '0'),
        }
      }
      if (part === 'precision') {
        const text = row.precision || row.quant
        return { text, sort: text }
      }
      return jobPart(row)
    })

  const byName = new Map<string, { parts: Part[]; cells: Map<string, BenchMatrixRow> }>()
  for (const row of rows) {
    const parts = partsOf(row)
    const name = parts.map((part) => part.text).filter(Boolean).join(' · ')
    const entry = byName.get(name) ?? { parts, cells: new Map() }
    entry.cells.set(row.model, row)
    byName.set(name, entry)
  }

  const sortKey = (parts: Part[]) => parts.map((part) => part.sort).join(' ')
  const entries = [...byName.entries()].sort((a, b) =>
    sortKey(a[1].parts).localeCompare(sortKey(b[1].parts)),
  )

  const groupAt = CATEGORY_PARTS.indexOf(GROUP_PART)
  const tickAt = CATEGORY_PARTS.map((_, index) => index).filter((index) => index !== groupAt)
  const { labels, title } = labelParts(
    entries.map(([, entry]) => tickAt.map((index) => entry.parts[index].text)),
    TICK_PARTS,
  )

  const categories: Category[] = []
  let previous: string | null = null
  entries.forEach(([name, entry], position) => {
    const group = entry.parts[groupAt].text
    if (previous !== null && group !== previous) {
      categories.push({
        name: ` gap-${position}`,
        label: '',
        group: '',
        cells: new Map(),
        spacer: true,
      })
    }
    previous = group
    categories.push({ name, label: labels[position], group, cells: entry.cells })
  })

  return { categories, title, count: entries.length }
}

/**
 * The categories worth drawing, with the spacers tidied up.
 *
 * A case that measured nothing gets no mark at any width, so a category none of
 * the series measured is dropped rather than held open as a hole -- they are
 * listed under the charts with their reason instead. That can leave a group with
 * nothing in it and its gaps behind, so the spacers are re-normalised after:
 * never two in a row, never one at either end.
 */
function prune(categories: Category[], series: Series[]): Category[] {
  const kept = categories.filter(
    (category) =>
      category.spacer ||
      series.some((s) => {
        const row = category.cells.get(s.key)
        return row && Object.keys(row.metrics).length > 0
      }),
  )
  const out: Category[] = []
  for (const category of kept) {
    if (category.spacer && (out.length === 0 || out[out.length - 1].spacer)) continue
    out.push(category)
  }
  while (out.length && out[out.length - 1].spacer) out.pop()
  return out
}

/** How many bands one label of the group rule covers. */
interface GroupCell {
  label: string
  span: number
}

/** Consecutive bands of one group into a single cell of the rule. */
function groupCells(bands: string[]): GroupCell[] {
  const cells: GroupCell[] = []
  for (const label of bands) {
    const last = cells[cells.length - 1]
    if (last && last.label === label) last.span += 1
    else cells.push({ label, span: 1 })
  }
  return cells
}

/** One band of the axis: a measurement's bar, or the space between two groups. */
interface BarPoint {
  /** Identity of the band. */
  name: string
  /** The configuration this bar is of, in full -- the tooltip's heading. */
  category: string
  /** Which device group it belongs to. */
  group: string
  /** Which series: the model. */
  seriesLabel: string
  color: string
  row: BenchMatrixRow | null
  spacer?: boolean
}

/**
 * Categories into bands: one band per measurement, plus the gaps between groups.
 *
 * Recharts groups bars by giving every category a slot for every series, drawn
 * or not. That is the right shape when each configuration was measured by each
 * series -- and the wrong one here, because a job need not have measured every
 * model at every precision. Every slot a model did not fill was still reserved,
 * so a group of four bars was as wide as a group of eight, and the empty band
 * between groups came out barely wider than the holes inside them: the grouping
 * was in the data and not on the screen.
 *
 * So the bars are laid out flat, one band each, and the grouping is spacing:
 * bars of one configuration are adjacent, configurations of one device are
 * adjacent, and devices are one empty band apart. A configuration is named once,
 * under the middle of its bars.
 */
function flatten(categories: Category[], series: Series[]): {
  bars: BarPoint[]
  labels: string[]
  spans: number[]
  groups: GroupCell[]
} {
  const bars: BarPoint[] = []
  const labels: string[] = []
  const spans: number[] = []
  const bands: string[] = []

  for (const category of categories) {
    if (category.spacer) {
      bars.push({ ...EMPTY_BAR, name: category.name })
      labels.push('')
      spans.push(0)
      bands.push('')
      continue
    }
    const measured = series
      .map((s) => ({ s, row: category.cells.get(s.key) }))
      .filter((cell) => cell.row && Object.keys(cell.row.metrics).length > 0)
    measured.forEach(({ s, row }, position) => {
      bars.push({
        name: `${category.name} ${s.key}`,
        category: category.name,
        group: category.group,
        seriesLabel: s.label,
        color: s.color,
        row: row ?? null,
      })
      // Named once per configuration, centred over its bars by `spans`.
      labels.push(position === 0 ? category.label : '')
      spans.push(position === 0 ? measured.length : 0)
      bands.push(category.group)
    })
  }

  return { bars, labels, spans, groups: groupCells(bands) }
}

const EMPTY_BAR: BarPoint = {
  name: '',
  category: '',
  group: '',
  seriesLabel: '',
  color: 'transparent',
  row: null,
  spacer: true,
}

/** One point of a line chart: every model's figure at one configuration. */
interface LinePoint {
  name: string
  /** The configuration in full -- the tooltip's heading. */
  category: string
  group: string
  /** The case each model measured here, for the click-through. */
  rows: Record<string, BenchMatrixRow>
  spacer?: boolean
}

/**
 * Categories into line points: one point per configuration, all models on it.
 *
 * The bar layout gives every mark a band of its own; a line needs the opposite
 * -- one x position per configuration, with each model's value read off it -- so
 * the same categories are pivoted rather than flattened. The gaps between device
 * groups are kept as valueless points so the grouping is in the same place in
 * both forms; `connectNulls` carries each model's line across them.
 */
function pivot(categories: Category[], series: Series[]): {
  points: LinePoint[]
  labels: string[]
  groups: GroupCell[]
} {
  const points: LinePoint[] = []
  const labels: string[] = []
  const bands: string[] = []

  for (const category of categories) {
    const rows: Record<string, BenchMatrixRow> = {}
    if (!category.spacer) {
      for (const s of series) {
        const row = category.cells.get(s.key)
        if (row && Object.keys(row.metrics).length > 0) rows[s.key] = row
      }
    }
    points.push({
      name: category.name,
      category: category.name,
      group: category.group,
      rows,
      spacer: category.spacer,
    })
    labels.push(category.label)
    bands.push(category.spacer ? '' : category.group)
  }

  return { points, labels, groups: groupCells(bands) }
}

// --- views ----------------------------------------------------------------

/**
 * The rule under the axis that says which device a run of ticks belongs to.
 *
 * The device used to be part of every tick, which spent the axis width on the
 * one component a run of neighbouring ticks all shared. As a rule under them it
 * is said once, and "these four bars are the CPU's" is a shape rather than four
 * strings to compare.
 *
 * Laid out in percentages of the plot area rather than from a measured width:
 * every band is the same fraction of it, so a cell of n bands is n/total of the
 * row and the rule lines up with the bars above it at any panel width. The
 * gutters are the y-axis and the chart's right margin -- the same numbers the
 * chart is given.
 */
function GroupBand({ cells, bands }: { cells: GroupCell[]; bands: number }) {
  if (bands === 0 || !cells.some((cell) => cell.label)) return null
  return (
    <div
      style={{
        display: 'flex',
        marginLeft: Y_AXIS_WIDTH,
        marginRight: PLOT_RIGHT_MARGIN,
        height: GROUP_BAND_HEIGHT,
      }}
    >
      {cells.map((cell, index) => (
        <div
          key={`${cell.label}-${index}`}
          style={{ flex: `0 0 ${(cell.span / bands) * 100}%`, minWidth: 0, padding: '0 3px' }}
        >
          {cell.label ? (
            <div
              style={{
                borderTop: `1px solid ${AXIS_STROKE}`,
                paddingTop: 2,
                textAlign: 'center',
                color: COLORS.textMuted,
                fontSize: 11,
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
              title={cell.label}
            >
              {cell.label}
            </div>
          ) : null}
        </div>
      ))}
    </div>
  )
}

/** What the ticks and the rule under them are, said once below both. */
function AxisCaption({ text }: { text: string }) {
  if (!text) return null
  return (
    <div style={{ textAlign: 'center', color: COLORS.textMuted, fontSize: 11 }}>{text}</div>
  )
}

/**
 * One panel per metric, side by side.
 *
 * Wrapping rather than scrolling: on a narrow window the panels stack, which
 * keeps each one wide enough to read its own axis. Every panel shares the
 * colours and the category order, so a mark in one is the same case as the mark
 * above it in the next.
 *
 * How wide "wide enough" is depends on the data, not on the layout: every mark
 * needs a band wide enough for the figure over it and its share of the angled
 * tick below. Three metrics of six configurations sit in one row; three of
 * twenty wrap to one panel per row, which is the honest trade -- the alternative
 * is three panels whose ticks are on top of each other.
 *
 * The bands are worked out once, here, and every panel is drawn from the same
 * list: a mark in one panel is the mark above it in the next, which is what
 * makes reading two metrics of one case off two charts possible at all.
 */
function MetricPanels({
  categories,
  series,
  metrics,
  xTitle,
  chart,
  onOpenCase,
}: {
  categories: Category[]
  series: Series[]
  metrics: BenchMetricMeta[]
  xTitle: string
  chart: ChartKind
  onOpenCase: (row: BenchMatrixRow) => void
}) {
  // Only the panels something was measured for. An empty axis next to two full
  // ones reads as "this device drew no memory", not as "nothing sampled it".
  const measured = metrics.filter((metric) =>
    categories.some((category) =>
      series.some((s) => category.cells.get(s.key)?.metrics[metric.key] !== undefined),
    ),
  )

  const drawable = prune(categories, series)
  const bars = flatten(drawable, series)
  const lines = pivot(drawable, series)
  const bands = chart === 'bar' ? bars.bars.length : lines.points.length

  if (bands === 0 || measured.length === 0) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={
          metrics.length === 0
            ? 'No metric selected — pick one above.'
            : 'This selection measured none of the chosen metrics.'
        }
      />
    )
  }

  // Wide enough for every band to hold its mark with the figure on top -- which
  // is a wider band in the line form, where one band carries a figure per model
  // rather than one in total. A panel narrower than this wraps to a row of its
  // own rather than shrinking: min() keeps that from overflowing a container
  // narrower than one panel.
  const slot = chart === 'bar' ? MIN_BAR_SLOT : MIN_POINT_LABEL_SLOT
  const minWidth = Math.max(300, bands * slot + PLOT_CHROME)
  // Both dimensions the axis is now made of: the ticks say one, the rule under
  // them says the other, and neither names itself.
  const caption = xTitle ? `${xTitle} · device` : 'device'

  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16 }}>
      {measured.map((metric) => (
        <div
          key={metric.key}
          style={{ flex: `1 1 ${minWidth}px`, minWidth: `min(${minWidth}px, 100%)` }}
        >
          <Space direction="vertical" size={2} style={{ width: '100%', marginBottom: 4 }}>
            <Text strong style={{ fontSize: 13 }}>
              {titleOf(metric)}
              {metric.higher_is_better !== null && (
                <Text type="secondary" style={{ fontSize: 11, marginLeft: 6 }}>
                  {metric.higher_is_better ? 'higher is better' : 'lower is better'}
                </Text>
              )}
            </Text>
            {/* What the metric means, in the panel rather than a tooltip: TTFT
                and TPOT are the two things a reader most often has backwards,
                and a chart of the wrong one still looks right. */}
            {metric.description && (
              <Text type="secondary" style={{ fontSize: 11 }}>
                {metric.description}
              </Text>
            )}
          </Space>
          {chart === 'bar' ? (
            <GroupedBars
              bars={bars.bars}
              tickLabels={bars.labels}
              spans={bars.spans}
              groups={bars.groups}
              series={series}
              metric={metric}
              caption={caption}
              height={PANEL_HEIGHT}
              onOpenCase={onOpenCase}
            />
          ) : (
            <MetricLines
              points={lines.points}
              tickLabels={lines.labels}
              groups={lines.groups}
              series={series}
              metric={metric}
              caption={caption}
              height={PANEL_HEIGHT}
              onOpenCase={onOpenCase}
            />
          )}
        </div>
      ))}
    </div>
  )
}

/**
 * The data point a bar was drawn from, out of whatever recharts hands an event.
 *
 * Bar events are given the rectangle's props, which carry the point both spread
 * onto them and under `payload` depending on the version and the event. Reading
 * both is what makes "which bar is this" a fact rather than a guess.
 */
function pointOf(entry: unknown): BarPoint | undefined {
  const bar = entry as { payload?: BarPoint; name?: string } | undefined
  return bar?.payload ?? (bar as BarPoint | undefined)
}

/** The tooltip body: a heading, then one line per series being reported. */
function TooltipCard({ title, lines }: { title: string; lines: { label: string; value: string }[] }) {
  return (
    <div style={{ ...TOOLTIP_STYLE, padding: '8px 10px', fontSize: 12 }}>
      <div style={{ marginBottom: 4 }}>{title}</div>
      {lines.map((line) => (
        <div key={line.label}>
          {line.label}: {line.value}
        </div>
      ))}
    </div>
  )
}

/** The y-axis, the grid and the legend -- identical in both chart forms. */
function metricAxis(metric: BenchMetricMeta) {
  return (
    <YAxis
      tick={AXIS_TICK}
      axisLine={{ stroke: AXIS_STROKE }}
      tickLine={{ stroke: AXIS_STROKE }}
      width={Y_AXIS_WIDTH}
      // The measure and its unit, not the unit alone: "ms" down the side of a
      // chart says how the numbers are counted and not what they are.
      label={{
        value: titleOf(metric),
        angle: -90,
        position: 'insideLeft',
        offset: 14,
        style: { textAnchor: 'middle' },
        ...AXIS_LABEL,
      }}
    />
  )
}

/** The legend payload: the models, spelled out because the marks are Cells. */
function seriesLegend(series: Series[]) {
  return (
    <Legend
      {...LEGEND_PROPS}
      payload={series.map((s) => ({
        value: s.label,
        id: s.key,
        type: 'square' as const,
        color: s.color,
      }))}
    />
  )
}

/** One bar per measurement, in the bands `flatten` laid out. */
function GroupedBars({
  bars,
  tickLabels,
  spans,
  groups,
  series,
  metric,
  caption,
  height,
  onOpenCase,
}: {
  bars: BarPoint[]
  /** What each band's tick says; '' for the bands another band speaks for. */
  tickLabels: string[]
  /** How many bands the tick at this one covers, so it can be centred. */
  spans: number[]
  /** The device rule under the axis. */
  groups: GroupCell[]
  series: Series[]
  metric: BenchMetricMeta
  caption: string
  height: number
  onOpenCase: (row: BenchMatrixRow) => void
}) {
  // How much room this panel actually got, which is what the ticks are cut to.
  // Its own width, not the window's: the panels wrap, so two of them side by
  // side and one below can be different widths in the same layout.
  const [panel, width] = useWidth<HTMLDivElement>()
  const { angle, chars, height: axisHeight } = tickPlan(width, tickLabels)
  // One band per bar, so the band is the bar's own width -- and the width the
  // figure over it has to fit in.
  const bandWidth = bars.length > 0 ? Math.max(0, width - PLOT_CHROME) / bars.length : 0
  // The figure over each bar, while there is room for it. Below that the numbers
  // would run into each other, and a row of half-overlapping digits is harder to
  // read than no digits at all -- the tooltip and the case drawer still have
  // them.
  const showValues = width > 0 && bandWidth >= MIN_BAR_SLOT
  // A tick names a configuration, and a configuration can be several bars wide;
  // its text belongs over the middle of them rather than over the first.
  const shifts = spans.map((span) => (span > 1 ? ((span - 1) / 2) * bandWidth : 0))

  const data = bars.map((bar) => ({ ...bar, value: bar.row?.metrics[metric.key] }))

  return (
    <div ref={panel}>
      <div style={{ width: '100%', height }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={data}
            // Room above the tallest bar for its figure, when they are being
            // drawn: without it the top one is clipped by the plot's edge.
            margin={{ top: showValues ? 20 : 8, right: PLOT_RIGHT_MARGIN, left: 0, bottom: 0 }}
            // Bars nearly fill their band: the spacing that means something here
            // is the empty band between groups, and a wide gap between every bar
            // competes with it. See `flatten`.
            barCategoryGap={2}
          >
            <CartesianGrid stroke={GRID_STROKE} strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="name"
              // Angled, always, at whatever angle this panel's width allows --
              // see tickPlan. Laid out horizontally the ticks overlapped each
              // other from the fourth category on, and at the narrowest panel
              // width from the second. Angling costs height once; overlapping
              // costs the labels entirely.
              tick={
                <CategoryTick angle={angle} chars={chars} labels={tickLabels} shifts={shifts} />
              }
              axisLine={{ stroke: AXIS_STROKE }}
              tickLine={false}
              // Every configuration labelled, truncated rather than dropped: a
              // chart with some of its bars unnamed is worse than one with
              // elided names. The tooltip carries the untruncated one.
              interval={0}
              // Tall enough for the longest tick it is actually drawing. What the
              // ticks *are* is said under the group rule instead of here, so the
              // axis band holds nothing but the ticks.
              height={axisHeight}
            />
            {metricAxis(metric)}
            <Tooltip
              cursor={{ fill: `${COLORS.rowAlt}80` }}
              content={({ active, payload }) => {
                if (!active || !payload?.length) return null
                // One bar per band, so the tooltip is about that bar. The axis
                // tick is abbreviated and shared by a configuration's bars; this
                // is where there is room to say which one the pointer is on.
                const point = payload[0]?.payload as (BarPoint & { value?: number }) | undefined
                if (!point || point.spacer || !Number.isFinite(Number(point.value))) return null
                return (
                  <TooltipCard
                    title={point.category}
                    lines={[{
                      label: point.seriesLabel,
                      value: `${formatMetric(Number(point.value))}${metric.unit ? ` ${metric.unit}` : ''}`,
                    }]}
                  />
                )
              }}
            />
            {/* On every panel, not just the first: the panels are read one at a
                time, and a colour whose meaning is stated over a chart two
                columns to the left is a colour the reader has to go and look
                up. */}
            {seriesLegend(series)}
            <Bar
              dataKey="value"
              radius={[4, 4, 0, 0]}
              maxBarSize={56}
              cursor="pointer"
              isAnimationActive={false}
              onClick={(entry: unknown) => {
                const row = pointOf(entry)?.row
                if (row) onOpenCase(row)
              }}
            >
              {data.map((point) => (
                <Cell key={point.name} fill={point.color} />
              ))}
              {/* The figure on the bar, not just in the tooltip: a reader
                  comparing two bars of similar height should not have to hover
                  each of them in turn to find out by how much. Same rounding as
                  every table in the tab, so the two cannot disagree. */}
              {showValues && (
                <LabelList
                  dataKey="value"
                  position="top"
                  offset={4}
                  fill={COLORS.textMuted}
                  fontSize={10}
                  formatter={(value: number | string) =>
                    (Number.isFinite(Number(value)) ? formatMetric(Number(value)) : '')
                  }
                />
              )}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <GroupBand cells={groups} bands={bars.length} />
      <AxisCaption text={caption} />
    </div>
  )
}

/**
 * One dot of a line, with a hit target bigger than the mark.
 *
 * A 4px radius dot is the right size to read and the wrong size to click, so the
 * visible circle sits inside a transparent one the pointer can actually find.
 * The ring in the panel's own colour keeps two models' dots apart where their
 * lines cross.
 */
function LineDot(props: {
  cx?: number
  cy?: number
  payload?: LinePoint
  color: string
  seriesKey: string
  onOpenCase: (row: BenchMatrixRow) => void
}) {
  const { cx, cy, payload, color, seriesKey, onOpenCase } = props
  if (cx == null || cy == null) return null
  const row = payload?.rows?.[seriesKey]
  return (
    <g cursor={row ? 'pointer' : 'default'} onClick={() => row && onOpenCase(row)}>
      <circle cx={cx} cy={cy} r={9} fill="transparent" />
      <circle cx={cx} cy={cy} r={4} fill={color} stroke={COLORS.panelBg} strokeWidth={1.5} />
    </g>
  )
}

/** One line per model, across the configurations `pivot` laid out. */
function MetricLines({
  points,
  tickLabels,
  groups,
  series,
  metric,
  caption,
  height,
  onOpenCase,
}: {
  points: LinePoint[]
  tickLabels: string[]
  groups: GroupCell[]
  series: Series[]
  metric: BenchMetricMeta
  caption: string
  height: number
  onOpenCase: (row: BenchMatrixRow) => void
}) {
  const [panel, width] = useWidth<HTMLDivElement>()
  const { angle, chars, height: axisHeight } = tickPlan(width, tickLabels)
  // One point per configuration, so a tick sits under its own point and has
  // nothing to be centred over -- unlike the bar form, where a configuration is
  // as many bands as it has bars.
  const noShift = useMemo(() => tickLabels.map(() => 0), [tickLabels])
  // The figure over every point, as long as two neighbouring ones do not have to
  // share the room: four significant digits at 10px need about as much width as
  // a bar's band does, and half-overlapping digits are worse than none. The
  // tooltip carries them all either way.
  const bandWidth = points.length > 0 ? Math.max(0, width - PLOT_CHROME) / points.length : 0
  const showValues = width > 0 && bandWidth >= MIN_POINT_LABEL_SLOT

  const data = points.map((point) => ({
    ...point,
    values: Object.fromEntries(
      series.map((s) => [s.key, point.rows[s.key]?.metrics[metric.key]]),
    ) as Record<string, number | undefined>,
  }))

  return (
    <div ref={panel}>
      <div style={{ width: '100%', height }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart
            data={data}
            // Room above the highest point for its figure, when they are being
            // drawn: without it the top one is clipped by the plot's edge.
            margin={{ top: showValues ? 22 : 12, right: PLOT_RIGHT_MARGIN, left: 0, bottom: 0 }}
          >
            <CartesianGrid stroke={GRID_STROKE} strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="name"
              tick={<CategoryTick angle={angle} chars={chars} labels={tickLabels} shifts={noShift} />}
              axisLine={{ stroke: AXIS_STROKE }}
              tickLine={false}
              interval={0}
              height={axisHeight}
            />
            {metricAxis(metric)}
            <Tooltip
              cursor={{ stroke: AXIS_STROKE }}
              content={({ active, payload }) => {
                if (!active || !payload?.length) return null
                // Every model at this configuration, best first: a line chart is
                // read across the x axis, so the question at a point is "who is
                // where" rather than "what is this one mark".
                const point = payload[0]?.payload as
                  | (LinePoint & { values?: Record<string, number | undefined> })
                  | undefined
                if (!point || point.spacer) return null
                const lines = series
                  .map((s) => ({ label: s.label, value: point.values?.[s.key] }))
                  .filter((line) => Number.isFinite(Number(line.value)))
                  .sort((a, b) => Number(b.value) - Number(a.value))
                  .map((line) => ({
                    label: line.label,
                    value: `${formatMetric(Number(line.value))}${metric.unit ? ` ${metric.unit}` : ''}`,
                  }))
                if (lines.length === 0) return null
                return <TooltipCard title={point.category} lines={lines} />
              }}
            />
            {seriesLegend(series)}
            {series.map((s) => (
              <Line
                key={s.key}
                name={s.label}
                // Straight segments: the x axis is a list of configurations, and
                // a curve through them would draw values between two things that
                // have nothing in between.
                type="linear"
                // A function rather than a key: a model's name carries dots and
                // slashes, which recharts would read as a path into the point.
                dataKey={(point: { values?: Record<string, number | undefined> }) =>
                  point.values?.[s.key] ?? null
                }
                stroke={s.color}
                strokeWidth={2}
                // A model measured on the CPU but not the GPU still reads as one
                // line rather than two fragments; the gap band between devices
                // is crossed for the same reason.
                connectNulls
                isAnimationActive={false}
                dot={
                  <LineDot color={s.color} seriesKey={s.key} onOpenCase={onOpenCase} />
                }
                activeDot={{ r: 6, fill: s.color, stroke: COLORS.panelBg, strokeWidth: 2 }}
              >
                {/* The figure at every point, like the bar form's. Above the
                    point rather than beside it, and in the series' own colour so
                    that two lines crossing do not swap which number belongs to
                    which model -- the one place in this tab where a colour is on
                    text, because proximity alone cannot say it here. */}
                {showValues && (
                  <LabelList
                    dataKey={(point: unknown) =>
                      (point as { values?: Record<string, number | undefined> }).values?.[s.key] ??
                      null
                    }
                    position="top"
                    offset={8}
                    fill={s.color}
                    fontSize={10}
                    formatter={(value: number | string | null) =>
                      (Number.isFinite(Number(value)) && value !== null
                        ? formatMetric(Number(value))
                        : '')
                    }
                  />
                )}
              </Line>
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <GroupBand cells={groups} bands={points.length} />
      <AxisCaption text={caption} />
    </div>
  )
}
