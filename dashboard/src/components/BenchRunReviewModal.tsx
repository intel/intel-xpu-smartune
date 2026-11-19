// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
// Pre-run review for per-model memory estimates and editable planned commands.

import React, { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Checkbox,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Spin,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { WarningTwoTone } from '@ant-design/icons'

import type { BenchPlanCommand, BenchRounds, DynamicInfoData, StaticInfoData } from '../api/types'
import BenchMemoryFitPanel, {
  DEFAULT_FIT_CHOICE,
  hasOverBudget,
  type FitChoice,
  type MemoryPreflightItem,
} from './BenchMemoryFitPanel'
import { COLORS } from '../styles/theme'
import { deviceLabel, matchesModel } from '../utils/benchMetrics'

const { Text } = Typography

// Commands the plan listed for a model that has no page of its own. Nothing
// should land here -- every ticked model gets a tab -- but a command that did
// would otherwise be invisible while still running, so it gets the last tab.
const OTHER_KEY = '__other__'

// Mirrors benchmark/service/runner.py _MAX_ROUNDS / _MAX_CASES.
const MAX_ROUNDS = 10
const MAX_CASES = 1024

// How tall a page may get before it scrolls itself. Bounded by the viewport as
// well as by a figure, so the footer stays reachable on a short screen.
const PAGE_MAX_HEIGHT = 'min(520px, 58vh)'

interface Props {
  open: boolean
  /** One page per model, with the precisions and devices it will be asked for. */
  items: MemoryPreflightItem[]
  dynamicInfo: DynamicInfoData | null
  staticInfo: StaticInfoData | null
  /** The memory readings are still being fetched. */
  memLoading: boolean
  /** The plan pass is still running; the commands are not complete yet. */
  planLoading: boolean
  /** The plan could not be produced, in the words to show instead of commands. */
  planError: string | null
  /** Every case the plan listed. Empty while planning. */
  commands: BenchPlanCommand[]
  /** True while the run is being started. */
  working: boolean
  /** Start the run. `edits` maps case_key -> command, changed cases only;
   *  `skip` lists the case_keys left unticked; `rounds` is how many times each
   *  case is measured and in which rounds the hardware sampler runs in full. */
  onRun: (edits: Record<string, string>, skip: string[], rounds: BenchRounds) => void
  onCancel: () => void
}

/** A model's commands, grouped by precision, in the order the plan listed them. */
function groupByPrecision(cases: BenchPlanCommand[]) {
  const groups: { quant: string; cases: BenchPlanCommand[] }[] = []
  const index = new Map<string, { quant: string; cases: BenchPlanCommand[] }>()
  for (const cmd of cases) {
    let group = index.get(cmd.quant)
    if (!group) {
      group = { quant: cmd.quant, cases: [] }
      index.set(cmd.quant, group)
      groups.push(group)
    }
    group.cases.push(cmd)
  }
  return groups
}

/** The parameters a case's sweep varied, as tags: which combination it is. */
function SweptTags({ cmd }: { cmd: BenchPlanCommand }) {
  const swept = Object.entries(cmd.swept ?? {})
  if (!swept.length) return null
  return (
    <>
      {swept.map(([k, v]) => (
        <Tag key={k} color="geekblue" style={{ marginInlineEnd: 0 }}>
          {k}={String(v)}
        </Tag>
      ))}
    </>
  )
}

/** The commands of one page: one editable box per case, grouped by precision. */
function CommandList({
  cases,
  drafts,
  defaults,
  editing,
  working,
  onEdit,
  excluded,
  onToggle,
}: {
  cases: BenchPlanCommand[]
  drafts: Record<string, string>
  defaults: Record<string, string>
  editing: boolean
  working: boolean
  onEdit: (caseKey: string, value: string) => void
  excluded: Set<string>
  onToggle: (caseKey: string, include: boolean) => void
}) {
  const groups = useMemo(() => groupByPrecision(cases), [cases])
  if (!cases.length) {
    return (
      <Alert
        type="warning"
        showIcon
        message="The plan listed no command for this model"
        description="Nothing was generated for the precisions and devices it is set to."
      />
    )
  }
  return (
    <div>
      {groups.map((group) => (
        <div key={group.quant} style={{ marginBottom: 10 }}>
          <Tag color="blue" style={{ marginBottom: 4 }}>
            {group.quant}
          </Tag>
          {group.cases.map((cmd) => {
            const edited =
              (drafts[cmd.case_key] ?? '').trim() !== (defaults[cmd.case_key] ?? '').trim()
            return (
              <div
                key={cmd.case_key}
                style={{ marginBottom: 8, opacity: excluded.has(cmd.case_key) ? 0.5 : 1 }}
              >
                <Space size={6} style={{ marginBottom: 2 }} wrap>
                  {/* Unticked cases are neither run nor recorded. A case whose
                      configuration is invalid starts unticked. */}
                  <Checkbox
                    checked={!excluded.has(cmd.case_key)}
                    disabled={working}
                    onChange={(e) => onToggle(cmd.case_key, e.target.checked)}
                  />
                  {/* Case identity: where this measurement is filed, regardless
                      of what the command below is edited to. */}
                  <Tag style={{ marginInlineEnd: 0 }}>{deviceLabel(cmd.device)}</Tag>
                  <SweptTags cmd={cmd} />
                  {edited && <Tag color="orange">changed</Tag>}
                </Space>
                {(cmd.errors ?? []).map((e) => (
                  <div key={e}>
                    <Text type="danger" style={{ fontSize: 12 }}>
                      {e}{edited ? '' : ' — not run unless the command is edited'}
                    </Text>
                  </div>
                ))}
                {(cmd.warnings ?? []).map((w) => (
                  <div key={w}>
                    <Text type="warning" style={{ fontSize: 12 }}>{w}</Text>
                  </div>
                ))}
                <Input.TextArea
                  value={drafts[cmd.case_key] ?? ''}
                  onChange={(e) => onEdit(cmd.case_key, e.target.value)}
                  autoSize={{ minRows: 2, maxRows: 8 }}
                  spellCheck={false}
                  readOnly={!editing || working}
                  style={{
                    fontFamily: 'monospace',
                    fontSize: 12,
                    background: editing ? '#fff' : undefined,
                    color: editing ? '#000' : undefined,
                  }}
                />
              </div>
            )
          })}
        </div>
      ))}
    </div>
  )
}

export default function BenchRunReviewModal({
  open,
  items,
  dynamicInfo,
  staticInfo,
  memLoading,
  planLoading,
  planError,
  commands,
  working,
  onRun,
  onCancel,
}: Props) {
  // The editable text of each case, keyed by case_key.
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  // Boxes start locked; the Edit button unlocks them all, Save relocks. One
  // lock for the dialog rather than one per page: it is a mode, and a mode that
  // changed as tabs were switched would be a surprise on the page switched to.
  const [editing, setEditing] = useState(false)
  // What each page's memory estimate is costed at. Held here, not in the panel,
  // so a page keeps its context and streams while another page is on screen.
  const [fit, setFit] = useState<Record<string, FitChoice>>({})
  const [activeKey, setActiveKey] = useState<string>('')
  // Cases the operator unticked. Seeded with the invalid ones as the plan
  // delivers them; a case the operator has ticked or unticked is left alone.
  const [excluded, setExcluded] = useState<Set<string>>(new Set())
  const [touched, setTouched] = useState<Set<string>>(new Set())
  // Repetitions. Round 1 sampled by default, which for one round is exactly
  // what a run always did.
  const [rounds, setRounds] = useState<BenchRounds>({ rounds: 1, sample_rounds: [1], warmup: false })
  const changeRoundCount = (count: number | null) => {
    const n = Math.max(1, Math.min(MAX_ROUNDS, Math.round(count ?? 1)))
    setRounds((prev) => {
      const kept = prev.sample_rounds.filter((r) => r <= n)
      return { ...prev, rounds: n, sample_rounds: kept.length || !prev.sample_rounds.length ? kept : [1] }
    })
  }

  // Seed a box per case as the plan delivers them. An edit already in progress
  // for a case that is still present is kept, so a poll that re-delivers the
  // list mid-review does not wipe the box.
  //
  // Nothing resets any of this state: the parent mounts one of these per press
  // of Run (keyed on the review session), so a fresh dialog is a fresh
  // component -- locked, on the first page, with no edits. What that buys is
  // the other direction: the dialog is hidden, not unmounted, while the rerun
  // gate is up, so answering that gate does not cost the operator their edits.
  useEffect(() => {
    setDrafts((prev) => {
      const next: Record<string, string> = {}
      for (const cmd of commands) next[cmd.case_key] = prev[cmd.case_key] ?? cmd.command
      return next
    })
    setExcluded((prev) => {
      const next = new Set<string>()
      for (const cmd of commands) {
        const keep = touched.has(cmd.case_key) ? prev.has(cmd.case_key) : !!cmd.errors?.length
        if (keep) next.add(cmd.case_key)
      }
      return next
    })
  }, [commands, touched])

  const toggle = (key: string, include: boolean) => {
    setTouched((prev) => new Set(prev).add(key))
    setExcluded((prev) => {
      const next = new Set(prev)
      if (include) next.delete(key)
      else next.add(key)
      return next
    })
  }

  // The commands of each page, and the ones that matched no page. The plan
  // names a model the way the pipeline does (a directory-safe form), not by its
  // HuggingFace id, so the two are matched the way results rows are.
  const { byModel, orphans } = useMemo(() => {
    const map = new Map<string, BenchPlanCommand[]>(items.map((item) => [item.model.id, []]))
    const rest: BenchPlanCommand[] = []
    for (const cmd of commands) {
      const owner = items.find((item) => matchesModel(cmd.model, item.model.id))
      if (owner) map.get(owner.model.id)!.push(cmd)
      else rest.push(cmd)
    }
    return { byModel: map, orphans: rest }
  }, [items, commands])

  // The default command per case, to tell an edited case from an untouched one.
  const defaults = useMemo(() => {
    const map: Record<string, string> = {}
    for (const cmd of commands) map[cmd.case_key] = cmd.command
    return map
  }, [commands])

  const changed = useMemo(
    () =>
      Object.keys(drafts).filter(
        (key) => (drafts[key] ?? '').trim() !== (defaults[key] ?? '').trim(),
      ),
    [drafts, defaults],
  )
  const changedKeys = useMemo(() => new Set(changed), [changed])

  // Which pages are over budget, and which carry an edit. A tab hides its page,
  // so both are marked on the tab itself -- and the first is summed into one
  // line above them, since a run that does not fit should not need the tabs
  // clicked through to be noticed.
  const overBudget = useMemo(() => {
    if (memLoading) return new Set<string>()
    const flagged = new Set<string>()
    for (const item of items) {
      const choice = fit[item.model.id] ?? DEFAULT_FIT_CHOICE
      if (hasOverBudget(item, dynamicInfo, staticInfo, choice)) flagged.add(item.model.id)
    }
    return flagged
  }, [items, dynamicInfo, staticInfo, fit, memLoading])

  const submit = () => {
    const edits: Record<string, string> = {}
    for (const key of changed) if (!excluded.has(key)) edits[key] = drafts[key]
    onRun(edits, commands.map((c) => c.case_key).filter((k) => excluded.has(k)), rounds)
  }
  const included = commands.length - commands.filter((c) => excluded.has(c.case_key)).length
  const totalCases = included * (rounds.rounds + (rounds.warmup ? 1 : 0))
  const invalid = commands.filter((c) => c.errors?.length).length

  const pages = [
    ...items.map((item) => {
      const cases = byModel.get(item.model.id) ?? []
      const short = item.model.id.split('/').pop() || item.model.id
      const edits = cases.filter((cmd) => changedKeys.has(cmd.case_key)).length
      return {
        key: item.model.id,
        label: (
          <Tooltip title={item.model.id} placement="right">
            <Space size={4}>
              {overBudget.has(item.model.id) && (
                <WarningTwoTone twoToneColor={COLORS.red} />
              )}
              <span style={{ maxWidth: 170, display: 'inline-block', overflow: 'hidden',
                             textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                             verticalAlign: 'middle' }}>
                {short}
              </span>
              {edits > 0 && <Tag color="orange" style={{ marginInlineEnd: 0 }}>{edits}</Tag>}
            </Space>
          </Tooltip>
        ),
        children: (
          <Space
            direction="vertical"
            size={14}
            // The page scrolls, not the dialog: the tab column and the footer
            // stay where they are however tall a model's table and command
            // stack turn out to be.
            style={{ width: '100%', maxHeight: PAGE_MAX_HEIGHT, overflowY: 'auto', paddingRight: 8 }}
          >
            {memLoading ? (
              <div style={{ textAlign: 'center', padding: '24px 0' }}>
                <Spin tip="Reading current memory…" />
              </div>
            ) : (
              <BenchMemoryFitPanel
                item={item}
                dynamicInfo={dynamicInfo}
                staticInfo={staticInfo}
                choice={fit[item.model.id] ?? DEFAULT_FIT_CHOICE}
                onChoiceChange={(next) =>
                  setFit((prev) => ({ ...prev, [item.model.id]: next }))
                }
              />
            )}

            <div>
              <Text strong style={{ fontSize: 13 }}>
                Commands
              </Text>
              <div style={{ marginTop: 6 }}>
                {planLoading ? (
                  <div style={{ padding: '20px 0', textAlign: 'center' }}>
                    <Spin />
                    <div style={{ marginTop: 10 }}>
                      <Text type="secondary">
                        Preparing the commands. The environment is being set up
                        exactly as it would for a run — no results are written and
                        nothing is measured yet.
                      </Text>
                    </div>
                  </div>
                ) : planError ? (
                  <Alert type="error" showIcon message="No commands to show" description={planError} />
                ) : (
                  <CommandList
                    cases={cases}
                    drafts={drafts}
                    defaults={defaults}
                    editing={editing}
                    working={working}
                    onEdit={(key, value) => setDrafts((prev) => ({ ...prev, [key]: value }))}
                    excluded={excluded}
                    onToggle={toggle}
                  />
                )}
              </div>
            </div>
          </Space>
        ),
      }
    }),
    // Only when the plan produced something no page claimed.
    ...(orphans.length
      ? [
          {
            key: OTHER_KEY,
            label: <Space size={4}>Other<Tag style={{ marginInlineEnd: 0 }}>{orphans.length}</Tag></Space>,
            children: (
              <Space
                direction="vertical"
                size={10}
                style={{ width: '100%', maxHeight: PAGE_MAX_HEIGHT, overflowY: 'auto', paddingRight: 8 }}
              >
                <Text type="secondary" style={{ fontSize: 12 }}>
                  These cases were planned under a model name that matches none of
                  the pages. They run as part of this job either way.
                </Text>
                <CommandList
                  cases={orphans}
                  drafts={drafts}
                  defaults={defaults}
                  editing={editing}
                  working={working}
                  onEdit={(key, value) => setDrafts((prev) => ({ ...prev, [key]: value }))}
                  excluded={excluded}
                  onToggle={toggle}
                />
              </Space>
            ),
          },
        ]
      : []),
  ]

  // Follow the pages when the set of them changes (a first open, or a model
  // dropping out), without fighting a tab the user has picked.
  const currentKey = pages.some((page) => page.key === activeKey)
    ? activeKey
    : (pages[0]?.key ?? '')

  const noCommands = !planLoading && !planError && (commands.length === 0 || included === 0)

  return (
    <Modal
      open={open}
      onCancel={onCancel}
      title="Review this run"
      width={980}
      footer={
        <Space>
          <Button
            onClick={() => setEditing((v) => !v)}
            disabled={working || planLoading || !!planError || noCommands}
            type="primary"
            style={{ background: '#1677ff', borderColor: '#1677ff' }}
          >
            {editing ? 'Save' : 'Edit'}
          </Button>
          <Button onClick={onCancel} disabled={working}>
            Cancel
          </Button>
          <Tooltip
            title={
              planLoading
                ? 'The commands this run will execute are still being composed'
                : planError || noCommands
                  ? 'There is nothing to run'
                  : undefined
            }
          >
            <Button
              type="primary"
              onClick={submit}
              loading={working}
              disabled={planLoading || !!planError || noCommands || totalCases > MAX_CASES}
              danger={overBudget.size > 0}
            >
              {planLoading
                ? 'Composing commands…'
                : `Run ${totalCases} case${totalCases === 1 ? '' : 's'}` +
                  (changed.length
                    ? `, ${changed.length} edited`
                    : '')}
            </Button>
          </Tooltip>
        </Space>
      }
    >
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        {overBudget.size > 0 && (
          <Alert
            type="warning"
            showIcon
            icon={<WarningTwoTone twoToneColor={COLORS.red} />}
            message={
              `${overBudget.size} model${overBudget.size === 1 ? '' : 's'} ` +
              `${overBudget.size === 1 ? 'has' : 'have'} configurations that need more ` +
              `memory than is free now`
            }
            description="Their tabs are marked. The check is an estimate, not a gate — you can still run."
          />
        )}

        {invalid > 0 && !planLoading && (
          <Alert
            type="warning"
            showIcon
            message={`${invalid} case${invalid === 1 ? ' has' : 's have'} a configuration that cannot run as set`}
            description="They are unticked and say why. Fix the model's parameters and run again, or edit the command and tick the case."
          />
        )}

        {/* Repetitions: the same cases, measured again, each round into its own
            results directory -- the report takes the median across them. The
            sampler's own load can sway a KPI, so it can be left to run in full
            for only some rounds: KPIs then come from the others, hardware
            figures from those. */}
        <Space size={12} wrap align="center">
          <Text strong style={{ fontSize: 13 }}>Rounds</Text>
          <InputNumber
            size="small"
            min={1}
            max={MAX_ROUNDS}
            value={rounds.rounds}
            onChange={changeRoundCount}
            disabled={working}
            style={{ width: 70 }}
          />
          <Tooltip title="Run every case once more before round 1 and leave it out of the medians: the first load of a model reads it from disk, later ones from the page cache.">
            <Checkbox
              checked={rounds.warmup}
              disabled={working}
              onChange={(e) => setRounds((prev) => ({ ...prev, warmup: e.target.checked }))}
            >
              warm-up round
            </Checkbox>
          </Tooltip>
          <Tooltip title="Rounds the hardware sampler runs in full for (power, utilisation, bandwidth). The others record only memory and temperature, and their KPIs are the ones reported.">
            <Space size={6}>
              <Text type="secondary" style={{ fontSize: 12 }}>hardware metrics in</Text>
              <Select
                size="small"
                mode="multiple"
                style={{ minWidth: 160 }}
                placeholder="no round"
                disabled={working}
                value={rounds.sample_rounds}
                onChange={(value: number[]) => setRounds((prev) => ({ ...prev, sample_rounds: [...value].sort((a, b) => a - b) }))}
                options={Array.from({ length: rounds.rounds }, (_, i) => ({ value: i + 1, label: `round ${i + 1}` }))}
              />
            </Space>
          </Tooltip>
          {!planLoading && (
            <Text type={totalCases > MAX_CASES ? 'danger' : 'secondary'} style={{ fontSize: 12 }}>
              {included} case{included === 1 ? '' : 's'}
              {rounds.rounds > 1 || rounds.warmup
                ? ` × ${rounds.rounds + (rounds.warmup ? 1 : 0)} rounds = ${totalCases}`
                : ''}
              {totalCases > MAX_CASES ? ` — over the limit of ${MAX_CASES}` : ''}
            </Text>
          )}
        </Space>
        {rounds.rounds > 1 && rounds.sample_rounds.length === rounds.rounds && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            Every round is sampled, so the KPIs include the sampler's load. Leave a round unsampled to measure without it.
          </Text>
        )}

        <Text type="secondary" style={{ fontSize: 12 }}>
          One page per model: how its configurations fit the memory free right
          now, and the exact command each case will run. A case runs exactly as
          shown unless you change it — click Edit to change any command. The
          environment setup, the OpenVINO column and the results bookkeeping are
          still handled for you.
        </Text>

        {pages.length === 0 ? (
          <Alert
            type="warning"
            showIcon
            message="Nothing to run"
            description="None of the ticked models offer the precisions they are set to."
          />
        ) : (
          <Tabs
            tabPosition="left"
            activeKey={currentKey}
            onChange={setActiveKey}
            items={pages}
            style={{ minHeight: 360 }}
            // The pages are tall -- a table and a stack of command boxes -- so
            // the dialog scrolls the page rather than the whole body, keeping
            // the tab column and the footer where they are.
            tabBarStyle={{ maxHeight: PAGE_MAX_HEIGHT, overflowY: 'auto' }}
            // Keep a visited page mounted: its drafts live above, but its
            // scroll position and the table's layout are cheaper kept than
            // rebuilt every time a tab is clicked back to.
            destroyInactiveTabPane={false}
          />
        )}
      </Space>
    </Modal>
  )
}
