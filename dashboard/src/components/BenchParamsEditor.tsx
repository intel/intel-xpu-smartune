// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
// Per-model benchmark parameters: one form per tool, rendered from the server's schema.

import React, { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  InputNumber,
  Input,
  Select,
  Space,
  Spin,
  Switch,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { CloseOutlined, LinkOutlined, QuestionCircleOutlined, UndoOutlined } from '@ant-design/icons'

import { api } from '../api/client'
import { COLORS } from '../styles/theme'
import type {
  BenchConfig,
  BenchParamSchema,
  BenchParamSpec,
  BenchParamValue,
  BenchTool,
  BenchToolConfig,
  BenchToolPrediction,
} from '../api/types'

const { Text } = Typography

const TOOLS: { key: BenchTool; label: string }[] = [
  { key: 'genai', label: 'GenAI (llm_bench)' },
  { key: 'benchmark_app', label: 'benchmark_app' },
]

// One schema per (tool, OpenVINO version) for the page's lifetime: it only
// changes when the tool on disk does, and the first fetch may wait on a probe.
const schemaCache = new Map<string, Promise<BenchParamSchema>>()
const predictionCache = new Map<string, Promise<BenchToolPrediction>>()

function loadSchema(tool: BenchTool, ov: string): Promise<BenchParamSchema> {
  const key = `${tool}@${ov}`
  let hit = schemaCache.get(key)
  if (!hit) {
    hit = api.getBenchParamSchema(tool, ov)
    hit.catch(() => schemaCache.delete(key))
    schemaCache.set(key, hit)
  }
  return hit
}

function loadPrediction(model: string, ov: string): Promise<BenchToolPrediction> {
  const key = `${model}@${ov}`
  let hit = predictionCache.get(key)
  if (!hit) {
    hit = api.predictBenchTool(model, ov)
    // Only a routed model is remembered: a failure, or a model not downloaded
    // yet, is asked again on the next open -- the download may have happened.
    hit.catch(() => predictionCache.delete(key))
    hit.then((p) => { if (p.source !== 'built') predictionCache.delete(key) })
    predictionCache.set(key, hit)
  }
  return hit
}

const asList = (v: BenchParamValue | BenchParamValue[] | undefined): BenchParamValue[] =>
  v === undefined ? [] : Array.isArray(v) ? v : [v]

/** How many cases a tool configuration expands to, the way bench_params.expand counts. */
export function countVariants(cfg: BenchToolConfig | undefined): number {
  if (!cfg) return 1
  const swept = Object.entries(cfg.params).filter(([, v]) => asList(v).length > 1)
  const zipped = new Set((cfg.zip ?? []).flat())
  let n = 1
  for (const group of cfg.zip ?? []) {
    const lens = group.map((name) => asList(cfg.params[name]).length).filter((l) => l > 1)
    if (lens.length) n *= lens[0]
  }
  for (const [name, v] of swept) if (!zipped.has(name)) n *= asList(v).length
  return n
}

/** A config with empty parameters dropped, or undefined when nothing is left. */
export function cleanToolConfig(cfg: BenchToolConfig | undefined): BenchToolConfig | undefined {
  if (!cfg) return undefined
  const params: BenchToolConfig['params'] = {}
  for (const [k, v] of Object.entries(cfg.params)) {
    const list = asList(v).filter((x) => x !== '' && x !== null && x !== undefined)
    if (list.length) params[k] = list.length === 1 ? list[0] : list
  }
  if (!Object.keys(params).length) return undefined
  const zip = (cfg.zip ?? [])
    .map((g) => g.filter((name) => asList(params[name]).length > 1))
    .filter((g) => g.length > 1)
  return zip.length ? { params, zip } : { params }
}

export function cleanBenchConfig(bench: BenchConfig | undefined): BenchConfig | undefined {
  if (!bench) return undefined
  const out: BenchConfig = {}
  for (const tool of Object.keys(bench) as BenchTool[]) {
    const cleaned = cleanToolConfig(bench[tool])
    if (cleaned) out[tool] = cleaned
  }
  return Object.keys(out).length ? out : undefined
}

/** "bs×2 · shape×2" -- the swept parameters, for a tile. */
export function sweepSummary(cfg: BenchToolConfig | undefined): string {
  if (!cfg) return ''
  return Object.entries(cfg.params)
    .filter(([, v]) => asList(v).length > 1)
    .map(([k, v]) => `${k}×${asList(v).length}`)
    .join(' · ')
}

function coerce(spec: BenchParamSpec, raw: string): BenchParamValue {
  if (spec.type === 'int' || spec.type === 'float') {
    const n = Number(raw)
    return Number.isFinite(n) ? n : raw
  }
  if (spec.type === 'bool') return raw === 'true'
  return raw
}

function applies(spec: BenchParamSpec, task: string | null): boolean {
  return !task || !spec.tasks || spec.tasks.includes(task)
}

/**
 * The input for one parameter: several values where it may be swept.
 *
 * It shows what the case will run with. A value the user set, else the default
 * the generator puts on the command line (`fallback`), else nothing -- an empty
 * field is a flag that is not passed. No placeholder text: a field reads as
 * filled only when it is.
 */
function ParamInput({
  spec,
  value,
  onChange,
  schema,
  task,
  fallback,
}: {
  spec: BenchParamSpec
  value: BenchParamValue | BenchParamValue[] | undefined
  onChange: (next: BenchParamValue | BenchParamValue[] | undefined) => void
  schema: BenchParamSchema
  task: string | null
  /** What the case runs with when this is left empty, as the generator resolves it. */
  fallback: BenchParamValue | undefined
}) {
  const own = asList(value)
  const isSet = own.length > 0
  // Clearing a value hands the field back to its default, so it is only
  // offered on a value the user set.
  const list = isSet ? own : fallback !== undefined && fallback !== null ? [fallback] : []

  if (spec.type === 'file') {
    const files = schema.prompt_files
    const fits = files.filter((f) => !task || !f.tasks || f.tasks.includes(task))
    const others = files.filter((f) => !fits.includes(f))
    return (
      <Select
        size="small"
        style={{ width: 300 }}
        mode={spec.sweep ? 'multiple' : undefined}
        allowClear={isSet}
        value={spec.sweep ? list.map(String) : (list[0] as string | undefined)}
        onChange={(v: string | string[] | undefined) =>
          onChange(Array.isArray(v) ? (v.length ? v : undefined) : v)
        }
        options={[
          { label: task ? `For ${task}` : 'Prompt files', options: fits.map((f) => ({ value: f.name, label: f.name })) },
          ...(others.length
            ? [{ label: 'Other tasks', options: others.map((f) => ({ value: f.name, label: f.name })) }]
            : []),
        ]}
      />
    )
  }

  if (spec.type === 'enum' || spec.type === 'bool') {
    const choices = spec.type === 'bool' ? ['true', 'false'] : spec.choices ?? []
    if (spec.type === 'bool' && !spec.sweep) {
      return <Switch size="small" checked={list[0] === true} onChange={(on) => onChange(on || undefined)} />
    }
    return (
      <Select
        size="small"
        style={{ width: 240 }}
        mode={spec.sweep ? 'multiple' : undefined}
        allowClear={isSet}
        value={spec.sweep ? list.map(String) : (list[0] !== undefined ? String(list[0]) : undefined)}
        onChange={(v: string | string[] | undefined) => {
          if (Array.isArray(v)) onChange(v.length ? v.map((x) => coerce(spec, x)) : undefined)
          else onChange(v === undefined ? undefined : coerce(spec, v))
        }}
        options={choices.map((c) => ({ value: c, label: c }))}
      />
    )
  }

  if (spec.sweep) {
    // Typed values become chips. Enter or ";" ends one -- not ",", which a shape
    // like [1,3,224,224] contains. The "?" next to the label says so.
    return (
      <Select
        size="small"
        style={{ width: 300 }}
        mode="tags"
        open={false}
        suffixIcon={null}
        tokenSeparators={[';']}
        value={list.map(String)}
        onChange={(v: string[]) => {
          const values = v.map((x) => x.trim()).filter(Boolean).map((x) => coerce(spec, x))
          onChange(values.length ? values : undefined)
        }}
      />
    )
  }

  if (spec.type === 'int' || spec.type === 'float') {
    return (
      <InputNumber
        size="small"
        style={{ width: 160 }}
        min={spec.min}
        max={spec.max}
        step={spec.type === 'int' ? 1 : 0.1}
        precision={spec.type === 'int' ? 0 : undefined}
        value={typeof list[0] === 'number' ? list[0] : undefined}
        onChange={(v) => onChange(v === null ? undefined : v)}
      />
    )
  }

  return (
    <Input
      size="small"
      style={{ width: 300 }}
      allowClear={isSet}
      value={list[0] !== undefined ? String(list[0]) : ''}
      onChange={(e) => onChange(e.target.value || undefined)}
    />
  )
}

const show = (v: BenchParamValue | undefined) =>
  v === undefined || v === null ? '' : typeof v === 'boolean' ? (v ? 'on' : 'off') : String(v)

/** What the "?" after a parameter explains: what it is, and where its value comes from. */
function paramHelp(
  spec: BenchParamSpec,
  task: string | null,
  fallback: BenchParamValue | undefined,
  fromRouter: boolean,
): React.ReactNode {
  const lines: React.ReactNode[] = []
  lines.push(<div key="flags" style={{ fontFamily: 'monospace' }}>{spec.flags.join(', ')}</div>)
  if (spec.help) lines.push(<div key="help" style={{ marginTop: 4 }}>{spec.help}</div>)
  if (spec.type === 'shape') {
    lines.push(<div key="shape" style={{ marginTop: 4 }}>e.g. [1,3,224,224] or input[1,3,224,224]</div>)
  }
  lines.push(
    <div key="default" style={{ marginTop: 4 }}>
      {fallback !== undefined && fallback !== null
        ? `Default ${show(fallback)}, ${fromRouter ? `from the ${task ?? 'model'} task defaults` : 'from the benchmark settings'} -- passed unless you change it.`
        : spec.tool_default !== undefined
          ? `Empty: not passed, so the tool uses its own default (${show(spec.tool_default)}).`
          : 'Empty: not passed, so the tool decides.'}
    </div>,
  )
  if (spec.sweep) {
    lines.push(
      <div key="multi" style={{ marginTop: 4 }}>
        multi: give several values to run one case each
        {spec.type !== 'enum' && spec.type !== 'file' && spec.type !== 'bool'
          ? ' -- press Enter or type ; between them.' : '.'}
      </div>,
    )
  }
  if (spec.tasks && task && !spec.tasks.includes(task)) {
    lines.push(<div key="task" style={{ marginTop: 4 }}>Does not apply to {task}.</div>)
  } else if (spec.tasks) {
    lines.push(<div key="tasks" style={{ marginTop: 4 }}>For tasks: {spec.tasks.join(', ')}.</div>)
  }
  return <div style={{ maxWidth: 360 }}>{lines}</div>
}

function ParamLabel({
  spec,
  task,
  fallback,
  fromRouter,
}: {
  spec: BenchParamSpec
  task: string | null
  fallback: BenchParamValue | undefined
  fromRouter: boolean
}) {
  // The same help on the name and on the "?": the "?" says there is help to
  // read, and the name is where a hover has always found it.
  const help = paramHelp(spec, task, fallback, fromRouter)
  return (
    <Space size={4} style={{ flexWrap: 'nowrap' }}>
      <Tooltip title={help}>
        <Text style={{ fontSize: 12, cursor: 'help' }} type={applies(spec, task) ? undefined : 'warning'}>
          {spec.label || spec.name}
        </Text>
      </Tooltip>
      {spec.sweep && <Tag style={{ marginInlineEnd: 0, fontSize: 10, lineHeight: '14px', padding: '0 4px' }}>multi</Tag>}
      <Tooltip title={help}>
        {/* The accent blue: the same in light and dark mode, and it reads as
            "there is something to look at here", which a muted grey did not. */}
        <QuestionCircleOutlined style={{ fontSize: 12, color: COLORS.accent, cursor: 'help' }} />
      </Tooltip>
    </Space>
  )
}

// genai: a prompt and a prompt file replace each other. A user choice of one
// drops the other's default (bench_params._EXCLUSIVE), so the form must not
// show that default either.
const EXCLUSIVE: Record<string, string> = { prompt: 'prompt_file', prompt_file: 'prompt' }

/** One tool's form. */
function ToolForm({
  tool,
  ov,
  task,
  defaults,
  value,
  onChange,
}: {
  tool: BenchTool
  ov: string
  task: string | null
  /** The router's defaults for this tool, or undefined when not known. */
  defaults: Record<string, BenchParamValue> | undefined
  value: BenchToolConfig | undefined
  onChange: (next: BenchToolConfig | undefined) => void
}) {
  const [schema, setSchema] = useState<BenchParamSchema | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [check, setCheck] = useState<{ ok: boolean; error: string | null } | null>(null)
  const cfg: BenchToolConfig = value ?? { params: {} }

  useEffect(() => {
    let live = true
    setSchema(null)
    setError(null)
    loadSchema(tool, ov)
      .then((s) => live && setSchema(s))
      .catch((e) => live && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      live = false
    }
  }, [tool, ov])

  // The server's own check, debounced: what the run would refuse, said now.
  const cleaned = useMemo(() => cleanToolConfig(value), [value])
  useEffect(() => {
    if (!cleaned) {
      setCheck(null)
      return
    }
    let live = true
    const timer = window.setTimeout(() => {
      api
        .validateBenchParams(tool, ov, cleaned)
        .then((r) => live && setCheck({ ok: r.ok, error: r.error }))
        .catch(() => live && setCheck(null))
    }, 500)
    return () => {
      live = false
      window.clearTimeout(timer)
    }
  }, [tool, ov, cleaned])

  if (error) return <Alert type="error" showIcon message="Could not load the parameters" description={error} />
  if (!schema) {
    return (
      <div style={{ padding: '12px 0' }}>
        <Spin size="small" />{' '}
        <Text type="secondary" style={{ fontSize: 12 }}>
          Reading the parameters this OpenVINO version's tool accepts…
        </Text>
      </div>
    )
  }

  // Alphabetical by the name shown, so a parameter is found where it is
  // expected rather than wherever the schema happened to list it.
  const shown = (p: BenchParamSpec) => (p.label || p.name).toLowerCase()
  const byName = (a: BenchParamSpec, b: BenchParamSpec) => shown(a).localeCompare(shown(b))
  const specs = schema.params.filter((p) => p.available !== false).sort(byName)
  // The basic form: what this task takes, plus anything the router gives a
  // default for -- a default the user cannot see is one they cannot change.
  const basic = specs.filter(
    (p) => (!p.advanced && applies(p, task)) || defaults?.[p.name] !== undefined,
  )
  const setNames = Object.keys(cfg.params)
  // Advanced parameters, and basic ones for another task, appear once set.
  const extra = specs.filter(
    (p) => setNames.includes(p.name) && !basic.includes(p),
  )
  const addable = specs.filter((p) => !basic.includes(p) && !setNames.includes(p.name))
  const fallback = (p: BenchParamSpec) => {
    const other = EXCLUSIVE[p.name]
    if (tool === 'genai' && other && asList(cfg.params[other]).length) return undefined
    return defaults?.[p.name] ?? p.default ?? undefined
  }

  const setParam = (name: string, v: BenchParamValue | BenchParamValue[] | undefined) => {
    const params = { ...cfg.params }
    if (v === undefined || (Array.isArray(v) && !v.length)) delete params[name]
    else params[name] = v
    onChange({ ...cfg, params })
  }
  const removeParam = (name: string) => setParam(name, undefined)

  const swept = Object.entries(cfg.params)
    .filter(([, v]) => asList(v).length > 1)
    .map(([k]) => k)
  const zipGroup = (cfg.zip?.[0] ?? []).filter((n) => swept.includes(n))
  const zipLens = zipGroup.map((n) => asList(cfg.params[n]).length)
  const zipMismatch = zipLens.length > 1 && new Set(zipLens).size > 1
  const variants = countVariants(cfg)

  const row = (spec: BenchParamSpec, removable: boolean) => {
    const def = fallback(spec)
    const isSet = asList(cfg.params[spec.name]).length > 0
    return (
      <div key={spec.name} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <div style={{ width: 200, flexShrink: 0 }}>
          <ParamLabel
            spec={spec}
            task={task}
            fallback={def}
            fromRouter={defaults?.[spec.name] !== undefined && defaults?.[spec.name] !== spec.default}
          />
        </div>
        <ParamInput
          spec={spec}
          value={cfg.params[spec.name]}
          onChange={(v) => setParam(spec.name, v)}
          schema={schema}
          task={task}
          fallback={def}
        />
        {/* Back to the default: clearing the field would do the same, but a
            field with a default never looks empty, so it gets a button. */}
        {isSet && def !== undefined && !removable && (
          <Tooltip title={`Back to the default (${show(def)})`}>
            <Button size="small" type="text" icon={<UndoOutlined />} onClick={() => removeParam(spec.name)} />
          </Tooltip>
        )}
        {removable && (
          <Button size="small" type="text" icon={<CloseOutlined />} onClick={() => removeParam(spec.name)} />
        )}
      </div>
    )
  }

  return (
    <div>
      {!schema.probed && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 8 }}
          message={`Not checked against the installed tool${schema.probe_error ? `: ${schema.probe_error}` : ''}`}
          description="Install this OpenVINO version to verify the flags; the run still validates them."
        />
      )}
      {basic.map((spec) => row(spec, false))}
      {extra.map((spec) => row(spec, true))}

      <Space size={8} style={{ marginTop: 4 }} wrap>
        <Select
          size="small"
          style={{ width: 300 }}
          showSearch
          value={null as unknown as string}
          placeholder={`+ parameter (${addable.length} more the tool accepts)`}
          onChange={(name: string) => {
            const spec = specs.find((p) => p.name === name)
            setParam(name, spec?.type === 'bool' ? true : '')
          }}
          options={addable.map((p) => ({
            value: p.name,
            label: (
              <Space size={6}>
                <span>{p.label && p.label !== p.name ? `${p.label} (${p.flags[0]})` : p.flags[0]}</span>
                {!applies(p, task) && <Tag color="warning" style={{ marginInlineEnd: 0 }}>other task</Tag>}
              </Space>
            ),
          }))}
          filterOption={(input, option) =>
            String(option?.value ?? '').toLowerCase().includes(input.toLowerCase())
          }
        />
        {swept.length >= 2 && (
          <Tooltip title="Linked parameters advance together -- the first value of each, then the second -- instead of every combination.">
            <Select
              size="small"
              mode="multiple"
              style={{ minWidth: 220 }}
              suffixIcon={<LinkOutlined />}
              placeholder="link swept parameters"
              value={zipGroup}
              onChange={(names: string[]) =>
                onChange({ ...cfg, zip: names.length ? [names] : undefined })
              }
              options={swept.map((n) => ({ value: n, label: n }))}
            />
          </Tooltip>
        )}
      </Space>

      <div style={{ marginTop: 8 }}>
        <Text type={variants > schema.limits.max_variants ? 'danger' : 'secondary'} style={{ fontSize: 12 }}>
          {variants === 1
            ? 'One case per precision and device.'
            : `${variants} cases per precision and device` +
              (variants > schema.limits.max_variants ? ` — over the limit of ${schema.limits.max_variants}` : '.')}
        </Text>
        {zipMismatch && (
          <div>
            <Text type="danger" style={{ fontSize: 12 }}>
              Linked parameters need the same number of values ({zipGroup.map((n, i) => `${n}: ${zipLens[i]}`).join(', ')}).
            </Text>
          </div>
        )}
        {check && !check.ok && check.error && (
          <div>
            <Text type="danger" style={{ fontSize: 12 }}>{check.error}</Text>
          </div>
        )}
      </div>
    </div>
  )
}

/** Shown instead of the form until the model has IR on disk to route. */
export function NotDownloaded() {
  return (
    <Alert
      type="info"
      showIcon
      message="Download the model first"
      description={
        'Which benchmark tool runs a model, and so which parameters it takes, is ' +
        'decided from its downloaded weights. Download at least one precision, ' +
        'then come back to set its parameters.'
      }
    />
  )
}

interface Props {
  modelId: string
  ov: string
  value: BenchConfig | undefined
  onChange: (next: BenchConfig) => void
}

/**
 * A model's benchmark parameters, one tab per tool.
 *
 * Which tool a model gets is the router's decision, made per run -- so both tabs
 * are offered, the router's predicted pick opens first, and only the section for
 * the tool actually chosen is used (gen_wrapper.py). Values persist with the
 * model's other settings, so every press of Run uses them without touching the
 * plan's commands.
 */
export default function BenchParamsEditor({ modelId, ov, value, onChange }: Props) {
  const [prediction, setPrediction] = useState<BenchToolPrediction | null>(null)
  const [predicting, setPredicting] = useState(false)
  const [active, setActive] = useState<BenchTool | null>(null)

  useEffect(() => {
    if (!ov) return
    let live = true
    setPredicting(true)
    setPrediction(null)
    loadPrediction(modelId, ov)
      .then((p) => live && setPrediction(p))
      .catch(() => live && setPrediction(null))
      .finally(() => live && setPredicting(false))
    return () => {
      live = false
    }
  }, [modelId, ov])

  const predicted = (prediction?.tool as BenchTool | null) ?? null
  const current: BenchTool =
    active ?? (predicted && TOOLS.some((t) => t.key === predicted) ? predicted : 'genai')
  const task = current === 'genai' ? prediction?.task ?? null : null

  if (!ov) {
    return <Text type="secondary">Pick an OpenVINO version first: the parameters are read from its tools.</Text>
  }

  if (prediction?.source === 'not_built') {
    return <NotDownloaded />
  }

  const routeNote = predicting ? (
    <Text type="secondary" style={{ fontSize: 12 }}><Spin size="small" /> asking the router…</Text>
  ) : prediction?.tool ? (
    <Text type="secondary" style={{ fontSize: 12 }}>
      Routed to <b>{prediction.tool}</b>
      {prediction.task ? ` (task ${prediction.task})` : ''}
    </Text>
  ) : prediction && prediction.tools.length > 1 ? (
    <Text type="secondary" style={{ fontSize: 12 }}>
      Its precisions route to different tools: {prediction.tools.join(', ')}. Fill in both.
    </Text>
  ) : (
    <Text type="secondary" style={{ fontSize: 12 }}>
      The router could not be asked; the tool is decided when the run is planned.
    </Text>
  )

  return (
    <div>
      {routeNote}
      <Tabs
        size="small"
        activeKey={current}
        onChange={(k) => setActive(k as BenchTool)}
        items={TOOLS.map(({ key, label }) => {
          const cfg = value?.[key]
          const n = Object.keys(cfg?.params ?? {}).length
          // Which built precisions this tool runs -- shown only when they split.
          const runs = Object.entries(prediction?.precisions ?? {})
            .filter(([, v]) => v.tool === key)
            .map(([precision]) => precision.replace(/_ov$/, ''))
          return {
            key,
            label: (
              <Space size={4}>
                {label}
                {(predicted === key || prediction?.tools.includes(key)) && (
                  <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                    {(prediction?.tools.length ?? 0) > 1 && runs.length ? `used · ${runs.join(', ')}` : 'used'}
                  </Tag>
                )}
                {n > 0 && <Tag style={{ marginInlineEnd: 0 }}>{n}</Tag>}
              </Space>
            ),
            children: (
              <ToolForm
                tool={key}
                ov={ov}
                task={key === 'genai' ? prediction?.task ?? null : null}
                defaults={prediction?.defaults?.[key]}
                value={cfg}
                onChange={(next) => onChange({ ...(value ?? {}), [key]: next })}
              />
            ),
          }
        })}
      />
      {task === null && current === 'genai' && prediction && (
        <Text type="secondary" style={{ fontSize: 11 }}>
          Task unknown, so every parameter is offered; the run review flags any the task does not take.
        </Text>
      )}
    </div>
  )
}
