// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// The model browser: everything the OpenVINO org has a conversion for.
//
// The whole list is fetched once and filtered here, in the browser. It runs to a
// few hundred entries -- tens of kilobytes -- and the alternative was a request
// per keystroke against a cache the server had to re-read and re-filter each
// time. A few hundred rows is also well short of needing virtualisation.
//
// Two selections coexist on purpose: the checkboxes pick a batch to run in one
// job (the backend caps a request at 32 models), while clicking a row picks the
// one model whose details are shown to the right.
//
// The rows are grouped by publisher -- the part of a repo id before the slash.
// Flat, the list was a few hundred entries in popularity order, which put the
// thirty-six Qwen models in six places; a publisher is also the closest thing
// the hub gives to a model family, and it needs no name-guessing to derive.

import React, { useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Badge, Button, Empty, Input, Segmented, Space, Spin, Tag, Tooltip, Typography } from 'antd'
import { CheckCircleTwoTone, DownOutlined, ReloadOutlined, RightOutlined } from '@ant-design/icons'

import type { BenchModel } from '../api/types'
import { COLORS } from '../styles/theme'

const { Text } = Typography

export type ModelFilter = 'all' | 'downloaded'

// Two states of the same question -- is this model here yet. Which precisions a
// model offers is a property of the model, not a mode for the list to be in, so
// it is shown per row instead (the precision tag on the right of each entry).
const FILTERS: { label: string; value: ModelFilter; hint: string }[] = [
  { label: 'All', value: 'all', hint: 'Every model with an OpenVINO conversion' },
  { label: 'Downloaded', value: 'downloaded', hint: 'Already fetched onto this machine' },
]

interface Props {
  models: BenchModel[]
  loading: boolean
  refreshing: boolean
  updatedAt: string | null
  lastError: string | null
  search: string
  onSearchChange: (value: string) => void
  filter: ModelFilter
  onFilterChange: (value: ModelFilter) => void
  selectedId: string | null
  onSelect: (id: string) => void
  checkedIds: string[]
  onCheckedChange: (ids: string[]) => void
  onRefreshList: () => void
}

function matchesFilter(model: BenchModel, filter: ModelFilter): boolean {
  return filter === 'downloaded' ? model.downloaded : true
}

/** Who published a repo: "Qwen", from "Qwen/Qwen3-4B". */
function publisherOf(model: BenchModel): string {
  const [org] = model.id.split('/')
  return org || model.id
}

interface Group {
  publisher: string
  models: BenchModel[]
  downloaded: number
}

/**
 * The visible models by publisher, publishers in first-appearance order.
 *
 * Not alphabetical: the cached list arrives in popularity order and the flat
 * list relied on it, so the publisher of the most-downloaded model stays at the
 * top and the rows inside a group keep the order they came in.
 */
function groupModels(models: BenchModel[]): Group[] {
  const groups: Group[] = []
  const index = new Map<string, Group>()
  for (const model of models) {
    const publisher = publisherOf(model)
    let group = index.get(publisher)
    if (!group) {
      group = { publisher, models: [], downloaded: 0 }
      index.set(publisher, group)
      groups.push(group)
    }
    group.models.push(model)
    if (model.downloaded) group.downloaded += 1
  }
  return groups
}

function formatCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`
  return String(value)
}

/**
 * What to say when there is nothing to list.
 *
 * Nothing here depends on the benchmark environment. It used to: the list was
 * only buildable with the venv's `hf` CLI, so an un-installed machine got an
 * "install the environment first" dead end. The search now falls back to the
 * hub's HTTP API (benchmark/service/search_models.py), which is the right way
 * round -- browsing models is what someone does *before* installing anything.
 */
function EmptyState({
  busy,
  noneCached,
}: {
  busy: boolean
  noneCached: boolean
}) {
  return (
    <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      style={{ marginTop: 32 }}
      description={
        busy
          ? 'Building the model list ...'
          : noneCached
            ? 'No cached models yet. Refresh the list to fetch it.'
            : 'No models match'
      }
    />
  )
}

export default function BenchModelList({
  models,
  loading,
  refreshing,
  updatedAt,
  lastError,
  search,
  onSearchChange,
  filter,
  onFilterChange,
  selectedId,
  onSelect,
  checkedIds,
  onCheckedChange,
  onRefreshList,
}: Props) {
  const checked = useMemo(() => new Set(checkedIds), [checkedIds])
  // Which publishers are open, and the only answer to that question: every
  // header toggles, all of them, always. Collapsed is the default -- a few
  // hundred models are forty-odd publishers, and that fits on one screen where
  // the models never did.
  //
  // A group is never *held* open by something else being true. It used to be:
  // the group of the highlighted model, and every group while a search was
  // running, were drawn open whatever this set said -- so their headers did
  // nothing when clicked. What opens a group by itself now writes it into this
  // set instead (see below), which leaves it collapsible.
  const [opened, setOpened] = useState<Set<string>>(new Set())
  // What was open before a search widened it, so clearing the box puts the list
  // back rather than leaving forty groups open.
  const beforeSearch = useRef<Set<string> | null>(null)

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase()
    return models.filter(
      (model) =>
        matchesFilter(model, filter) &&
        (!needle ||
          model.id.toLowerCase().includes(needle) ||
          (model.task ?? '').toLowerCase().includes(needle)),
    )
  }, [models, search, filter])

  const groups = useMemo(() => groupModels(visible), [visible])

  const searching = search.trim().length > 0

  // A search is a request to see the matches, so the groups holding them open
  // -- as an ordinary opening, which a click can still close. Restored when the
  // box is cleared, so a filter does not permanently rearrange the list.
  useEffect(() => {
    if (searching) {
      if (beforeSearch.current === null) beforeSearch.current = opened
      setOpened(new Set(groups.map((group) => group.publisher)))
    } else if (beforeSearch.current !== null) {
      setOpened(beforeSearch.current)
      beforeSearch.current = null
    }
    // `opened` is read but deliberately not depended on: this reacts to the
    // search changing, not to the reader opening a group while one is running.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searching, groups])

  // The group of the model whose details are on screen, opened once per
  // selection -- the pane on the right and the list on the left should not
  // disagree about which model is being read. Written into the set, so it is no
  // less collapsible than any other group.
  useEffect(() => {
    if (!selectedId) return
    const publisher = selectedId.split('/')[0] || selectedId
    setOpened((prev) => (prev.has(publisher) ? prev : new Set(prev).add(publisher)))
  }, [selectedId])

  const isOpen = (group: Group): boolean => opened.has(group.publisher)

  const toggleGroup = (publisher: string) => {
    setOpened((prev) => {
      const next = new Set(prev)
      if (next.has(publisher)) next.delete(publisher)
      else next.add(publisher)
      return next
    })
  }

  const allOpen = groups.length > 0 && groups.every((group) => isOpen(group))

  const toggleAll = () => {
    setOpened(allOpen ? new Set() : new Set(groups.map((group) => group.publisher)))
  }

  const toggleChecked = (id: string) => {
    const next = new Set(checked)
    if (next.has(id)) {
      next.delete(id)
    } else {
      next.add(id)
      // Ticking a model is also an expression of interest in it, so bring its
      // details up. Without this, a first click on the checkbox left the pane on
      // the right empty and the model had to be clicked a second time.
      onSelect(id)
    }
    onCheckedChange([...next])
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <Space direction="vertical" size={8} style={{ padding: 12, paddingBottom: 8 }}>
        <Input.Search
          placeholder="Filter models"
          value={search}
          allowClear
          onChange={(e) => onSearchChange(e.target.value)}
        />
        <Segmented
          size="small"
          block
          options={FILTERS.map(({ label, value, hint }) => ({
            value,
            label: <Tooltip title={hint}>{label}</Tooltip>,
          }))}
          value={filter}
          onChange={(value) => onFilterChange(value as ModelFilter)}
        />
        {/* How much of the list is on screen, and the one control that opens or
            closes all of it at once. Any mixture in between is a matter of
            clicking the headers -- nothing here or elsewhere pins a group
            open. */}
        {groups.length > 0 && (
          <Space size={8} style={{ justifyContent: 'space-between', width: '100%' }}>
            <Text type="secondary" style={{ fontSize: 11 }}>
              {groups.length} publisher{groups.length === 1 ? '' : 's'} · {visible.length} model
              {visible.length === 1 ? '' : 's'}
            </Text>
            <Button
              size="small"
              type="link"
              style={{ padding: 0, height: 'auto', fontSize: 11 }}
              onClick={toggleAll}
            >
              {allOpen ? 'Collapse all' : 'Expand all'}
            </Button>
          </Space>
        )}
      </Space>

      {/* A search takes minutes, and the one started for the user -- at startup,
          or as soon as an environment setup finishes -- is otherwise invisible:
          the list sits empty (or stale) with a Refresh button next to it, as if
          nothing were happening. Said above the list so it is seen whether or
          not there is already an old list underneath. */}
      {refreshing && (
        <div style={{ padding: '0 12px 8px' }}>
          <Alert
            type="info"
            icon={<Spin size="small" />}
            showIcon
            message={
              <span style={{ fontSize: 11 }}>
                Syncing the model list from HuggingFace — this takes a few minutes.
              </span>
            }
            style={{ padding: '4px 8px' }}
          />
        </div>
      )}

      <div style={{ flex: 1, overflow: 'auto', padding: '0 4px' }}>
        {visible.length === 0 ? (
          <EmptyState busy={loading || refreshing} noneCached={models.length === 0} />
        ) : (
          groups.map((group) => {
            const open = isOpen(group)
            return (
              <div key={group.publisher}>
                <div
                  onClick={() => toggleGroup(group.publisher)}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 6,
                    padding: '5px 8px',
                    marginTop: 2,
                    borderRadius: 4,
                    cursor: 'pointer',
                    background: COLORS.headerBg,
                  }}
                >
                  {open ? (
                    <DownOutlined style={{ color: COLORS.textMuted, fontSize: 9 }} />
                  ) : (
                    <RightOutlined style={{ color: COLORS.textMuted, fontSize: 9 }} />
                  )}
                  <Text style={{ fontSize: 12, flex: 1, minWidth: 0 }} ellipsis title={group.publisher}>
                    {group.publisher}
                  </Text>
                  {/* How many of this publisher's models are already on the
                      machine: the one fact that decides whether opening the
                      group is worth it. */}
                  {group.downloaded > 0 && (
                    <Tooltip title={`${group.downloaded} already downloaded`}>
                      <Space size={2} style={{ fontSize: 11 }}>
                        <CheckCircleTwoTone twoToneColor="#52c41a" />
                        <Text type="secondary" style={{ fontSize: 11 }}>
                          {group.downloaded}
                        </Text>
                      </Space>
                    </Tooltip>
                  )}
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    {group.models.length}
                  </Text>
                </div>

                {open &&
                  group.models.map((model) => {
                    const isSelected = model.id === selectedId
                    return (
                      <div
                        key={model.id}
                        onClick={() => onSelect(model.id)}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: 8,
                          padding: '6px 8px',
                          paddingLeft: 18,
                          borderRadius: 4,
                          cursor: 'pointer',
                          background: isSelected ? COLORS.headerBg : undefined,
                          borderLeft: `2px solid ${isSelected ? COLORS.accent : 'transparent'}`,
                        }}
                      >
                        <input
                          type="checkbox"
                          checked={checked.has(model.id)}
                          onClick={(e) => e.stopPropagation()}
                          onChange={() => toggleChecked(model.id)}
                          aria-label={`Select ${model.id}`}
                        />
                        <div style={{ minWidth: 0, flex: 1 }}>
                          <div
                            style={{
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                              fontSize: 13,
                            }}
                            title={model.id}
                          >
                            {/* The publisher is the group's heading, so a row
                                says the model. The full id is in the tooltip
                                and is what the checkbox reports. */}
                            {model.id.slice(group.publisher.length + 1) || model.id}
                          </div>
                          <Space size={4} style={{ fontSize: 11 }}>
                            {model.task && <Text type="secondary">{model.task}</Text>}
                            {model.downloads > 0 && (
                              <Text type="secondary">· {formatCount(model.downloads)} ⬇</Text>
                            )}
                          </Space>
                        </div>
                        {model.downloaded && (
                          <Tooltip title="Already downloaded">
                            <CheckCircleTwoTone twoToneColor="#52c41a" />
                          </Tooltip>
                        )}
                        {model.precisions.length > 0 && (
                          <Tag style={{ marginInlineEnd: 0, fontSize: 11 }}>
                            {model.precisions.join(' ')}
                          </Tag>
                        )}
                      </div>
                    )
                  })}
              </div>
            )
          })
        )}
      </div>

      <div style={{ borderTop: `1px solid ${COLORS.border}`, padding: '8px 12px' }}>
        <Space direction="vertical" size={6} style={{ width: '100%' }}>
          <Space size={8} wrap>
            <Tooltip title={refreshing ? 'A search is already running' : 'Search HuggingFace again'}>
              {/* A span so the tooltip still fires while the button is disabled --
                  a disabled button emits no pointer events, and the one moment
                  the reason is worth reading is when it cannot be pressed. */}
              <span>
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  loading={refreshing}
                  // Refusing a second search is the server's answer anyway; the
                  // button used to accept the click and report it as an error.
                  disabled={refreshing}
                  onClick={onRefreshList}
                >
                  Refresh list
                </Button>
              </span>
            </Tooltip>
            <Text type="secondary" style={{ fontSize: 11 }}>
              {refreshing
                ? 'searching HuggingFace ...'
                : updatedAt
                  ? `${models.length} models · ${updatedAt.slice(0, 10)}`
                  : 'no cached list yet'}
            </Text>
          </Space>
          {/* The whole message, not "Last refresh failed" with the substance in a
              tooltip: a failed search is the one thing here a user has to read to
              act on -- a proxy, a token, a name resolution failure. */}
          {lastError && (
            <Alert
              type="error"
              showIcon
              message={<span style={{ fontSize: 11 }}>{lastError}</span>}
              style={{ padding: '4px 8px' }}
            />
          )}
          {checkedIds.length > 0 && (
            <Badge
              count={checkedIds.length}
              size="small"
              offset={[6, 0]}
              style={{ backgroundColor: COLORS.accent }}
            >
              <Text type="secondary" style={{ fontSize: 11 }}>
                selected for a batch run
              </Text>
            </Badge>
          )}
        </Space>
      </div>
    </div>
  )
}
