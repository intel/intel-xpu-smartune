// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
// Pre-run review for per-model memory estimates and editable planned commands.

import React, { useEffect, useMemo, useState } from 'react'
import { Alert, Button, Input, Modal, Space, Spin, Tabs, Tag, Tooltip, Typography } from 'antd'
import { WarningTwoTone } from '@ant-design/icons'

import type { BenchPlanCommand, DynamicInfoData, StaticInfoData } from '../api/types'
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
  /** Start the run. `edits` maps case_key -> command, changed cases only. */
  onRun: (edits: Record<string, string>) => void
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

/** The commands of one page: one editable box per case, grouped by precision. */
function CommandList({
  cases,
  drafts,
  defaults,
  editing,
  working,
  onEdit,
}: {
  cases: BenchPlanCommand[]
  drafts: Record<string, string>
  defaults: Record<string, string>
  editing: boolean
  working: boolean
  onEdit: (caseKey: string, value: string) => void
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
              <div key={cmd.case_key} style={{ marginBottom: 8 }}>
                <Space size={6} style={{ marginBottom: 2 }}>
                  {/* Case identity: where this measurement is filed, regardless
                      of what the command below is edited to. */}
                  <Tag style={{ marginInlineEnd: 0 }}>{deviceLabel(cmd.device)}</Tag>
                  {edited && <Tag color="orange">changed</Tag>}
                </Space>
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
  }, [commands])

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
    for (const key of changed) edits[key] = drafts[key]
    onRun(edits)
  }

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

  const noCommands = !planLoading && !planError && commands.length === 0

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
              disabled={planLoading || !!planError || noCommands}
              danger={overBudget.size > 0}
            >
              {planLoading
                ? 'Composing commands…'
                : changed.length
                  ? `Run with ${changed.length} edited command${changed.length === 1 ? '' : 's'}`
                  : 'Run'}
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
