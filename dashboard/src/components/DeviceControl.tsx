// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Card, ConfigProvider, Popconfirm, Slider, Space, Tag, Tooltip, Typography, message } from 'antd'
import { DownOutlined, RightOutlined, RollbackOutlined } from '@ant-design/icons'
import { api } from '../api/client'
import type { FreqStatus, FreqUnit } from '../api/types'
import { COLORS } from '../styles/theme'

const { Text } = Typography

// The default Slider rail and track are both pale grey here; give them the accent colour.
// Built per render: COLORS is updated in place when the colour mode changes.
const sliderTheme = () => ({
  components: {
    Slider: {
      railBg: COLORS.borderStrong,
      railHoverBg: COLORS.borderStrong,
      trackBg: COLORS.accent,
      trackHoverBg: COLORS.accent,
      handleColor: COLORS.accent,
      handleActiveColor: COLORS.accent,
      handleColorDisabled: COLORS.textTertiary,
    },
  },
})

const POLL_MS = 3000
const SLIDER_STEP_MHZ = 50

const CORE_CLASS_LABEL: Record<string, string> = {
  p: 'P-cores',
  e: 'E-cores',
  lpe: 'LP E-cores',
  mixed: 'Mixed cores',
  '': 'Cores',
}
const CORE_CLASS_ORDER = ['p', 'e', 'lpe', 'mixed', '']

// What one row controls: the units to write, and the range they currently share.
interface Row {
  key: string
  label: string
  detail?: string
  tooltip?: string
  units: FreqUnit[]
}

type Range = [number, number]

function formatMhz(mhz: number): string {
  return mhz >= 1000 ? `${(mhz / 1000).toFixed(mhz % 100 === 0 ? 1 : 2)} GHz` : `${mhz} MHz`
}

function rowCurrent(row: Row): Range {
  return [Math.min(...row.units.map((u) => u.min_mhz)), Math.max(...row.units.map((u) => u.max_mhz))]
}

function rowHardware(row: Row): Range {
  return [Math.min(...row.units.map((u) => u.hw_min_mhz)), Math.max(...row.units.map((u) => u.hw_max_mhz))]
}

// Units of a row can sit on different values (set one by one, or hardware tops differ): the
// slider then shows the widest span and the row says so.
function rowUniform(row: Row): boolean {
  const [first, ...rest] = row.units
  return rest.every((u) => u.min_mhz === first.min_mhz && u.max_mhz === first.max_mhz)
}

function cpuRows(units: FreqUnit[]): Row[] {
  const byClass = new Map<string, FreqUnit[]>()
  for (const unit of units) {
    const key = unit.core_class ?? ''
    byClass.set(key, [...(byClass.get(key) ?? []), unit])
  }
  return CORE_CLASS_ORDER.filter((k) => byClass.has(k)).map((k) => {
    const members = byClass.get(k) as FreqUnit[]
    const cpus = members.flatMap((u) => u.cpus ?? []).sort((a, b) => a - b)
    return {
      key: `cpu:${k}`,
      label: CORE_CLASS_LABEL[k] ?? k,
      detail: `CPU ${cpus.join(', ')}`,
      units: members,
    }
  })
}

function unitRow(unit: FreqUnit, label: string, detail?: string, tooltip?: string): Row {
  return { key: unit.id, label, detail, tooltip, units: [unit] }
}

function FreqRow({
  row,
  indent = false,
  draft,
  busy,
  onDraft,
  onApply,
  onRestore,
}: {
  row: Row
  indent?: boolean
  draft: Range | undefined
  busy: boolean
  onDraft: (key: string, value: Range | undefined) => void
  onApply: (row: Row, value: Range) => void
  onRestore: (row: Row) => void
}) {
  const current = rowCurrent(row)
  const [hwMin, hwMax] = rowHardware(row)
  const value = draft ?? current
  const dirty = draft !== undefined && (draft[0] !== current[0] || draft[1] !== current[1])
  const limited = row.units.some((u) => u.limited)
  const uniform = rowUniform(row)
  const live = row.units.map((u) => u.cur_mhz).filter((v): v is number => v != null)
  const original = row.units.find((u) => u.limited)

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(150px, 220px) minmax(180px, 1fr) auto',
        gap: 16,
        alignItems: 'center',
        padding: '10px 0',
        paddingLeft: indent ? 24 : 0,
        borderTop: `1px solid ${COLORS.border}`,
      }}
    >
      <div>
        <Space size={6} wrap>
          <Tooltip title={row.tooltip}>
            <Text style={{ color: COLORS.text, fontWeight: 600 }}>{row.label}</Text>
          </Tooltip>
          {limited && (
            <Tooltip
              title={original
                ? `Limited by SmarTune. Restore puts back ${formatMhz(original.original_min_mhz ?? 0)} – ${formatMhz(original.original_max_mhz ?? 0)}.`
                : 'Limited by SmarTune.'}
            >
              <Tag color="orange" style={{ marginInlineEnd: 0 }}>Limited</Tag>
            </Tooltip>
          )}
          {!uniform && (
            <Tooltip title="The cores of this row are not on the same range; applying sets them all to one.">
              <Tag style={{ marginInlineEnd: 0 }}>Mixed</Tag>
            </Tooltip>
          )}
        </Space>
        <div>
          <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>
            {row.detail ? `${row.detail} · ` : ''}
            now {live.length ? formatMhz(Math.max(...live)) : '–'}
          </Text>
        </div>
      </div>
      <div>
        <Slider
          range
          min={hwMin}
          max={hwMax}
          step={SLIDER_STEP_MHZ}
          value={value}
          disabled={busy}
          tooltip={{ formatter: (v) => (v === undefined ? '' : formatMhz(v)) }}
          onChange={(next) => onDraft(row.key, next as Range)}
          style={{ margin: '4px 6px' }}
        />
        <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>
          {formatMhz(value[0])} – {formatMhz(value[1])}
          <span style={{ color: COLORS.textTertiary }}> (hardware {formatMhz(hwMin)} – {formatMhz(hwMax)})</span>
        </Text>
      </div>
      <Space size={8}>
        <Button type="primary" size="small" disabled={!dirty || busy} onClick={() => onApply(row, value)}>
          Apply
        </Button>
        <Button
          size="small"
          icon={<RollbackOutlined />}
          disabled={!limited || busy}
          onClick={() => onRestore(row)}
        >
          Restore
        </Button>
      </Space>
    </div>
  )
}

export default function DeviceControl({ active }: { active: boolean }) {
  const [status, setStatus] = useState<FreqStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [drafts, setDrafts] = useState<Record<string, Range>>({})
  const [busy, setBusy] = useState(false)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const inFlight = useRef(false)

  const load = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    try {
      setStatus(await api.getFreqStatus())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to read device frequencies')
    } finally {
      inFlight.current = false
    }
  }, [])

  // Live clocks move on their own and another client may change a range, so poll while visible.
  useEffect(() => {
    if (!active) return
    load()
    const timer = window.setInterval(load, POLL_MS)
    return () => window.clearInterval(timer)
  }, [active, load])

  const setDraft = useCallback((key: string, value: Range | undefined) => {
    setDrafts((prev) => {
      const next = { ...prev }
      if (value) next[key] = value
      else delete next[key]
      return next
    })
  }, [])

  const run = useCallback(async (action: () => Promise<void>, done: string, rowKeys: string[]) => {
    setBusy(true)
    try {
      await action()
      setDrafts((prev) => {
        const next = { ...prev }
        rowKeys.forEach((k) => delete next[k])
        return next
      })
      message.success(done)
    } catch (e) {
      message.error(e instanceof Error ? e.message : 'Frequency request failed')
    } finally {
      setBusy(false)
      await load()
    }
  }, [load])

  const apply = useCallback((row: Row, value: Range) => {
    // Rows that are a group also hold their per-policy children's drafts: drop those too.
    const keys = [row.key, ...row.units.map((u) => u.id)]
    run(() => api.setFreq({ units: row.units.map((u) => u.id), min_mhz: value[0], max_mhz: value[1] }),
      `${row.label}: limited to ${formatMhz(value[0])} – ${formatMhz(value[1])}`, keys)
  }, [run])

  const restore = useCallback((row: Row) => {
    const keys = [row.key, ...row.units.map((u) => u.id)]
    run(() => api.restoreFreq(row.units.filter((u) => u.limited).map((u) => u.id)),
      `${row.label}: restored`, keys)
  }, [run])

  const units = status?.units ?? []
  const cpuUnits = useMemo(() => units.filter((u) => u.kind === 'cpu'), [units])
  const gpuByCard = useMemo(() => {
    const cards = new Map<string, FreqUnit[]>()
    units.filter((u) => u.kind === 'gpu').forEach((u) => cards.set(u.group, [...(cards.get(u.group) ?? []), u]))
    return Array.from(cards.entries())
  }, [units])
  const anyLimited = units.some((u) => u.limited)

  const rowProps = { busy, onDraft: setDraft, onApply: apply, onRestore: restore }

  return (
    <ConfigProvider theme={sliderTheme()}>
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <Popconfirm
          title="Restore every limited device to its original range?"
          okText="Restore all"
          disabled={!anyLimited || busy}
          onConfirm={() => run(() => api.restoreFreq([]), 'All frequencies restored', Object.keys(drafts))}
        >
          <Button size="small" icon={<RollbackOutlined />} disabled={!anyLimited || busy}>
            Restore all
          </Button>
        </Popconfirm>
      </div>
      {error && <Alert type="error" showIcon message={error} />}

      <Card
        size="small"
        title="CPU"
        loading={status === null && !error}
        style={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}` }}
        extra={cpuUnits[0]?.driver ? <Text type="secondary">{cpuUnits[0].driver}</Text> : null}
      >
        {status !== null && cpuUnits.length === 0 && (
          <Text type="secondary">No cpufreq interface found on this machine.</Text>
        )}
        {cpuRows(cpuUnits).map((row) => {
          const open = expanded[row.key] ?? false
          return (
            <div key={row.key}>
              <FreqRow row={row} draft={drafts[row.key]} {...rowProps} />
              {row.units.length > 1 && (
                <div style={{ paddingBottom: 6 }}>
                  <Button
                    type="link"
                    size="small"
                    icon={open ? <DownOutlined /> : <RightOutlined />}
                    onClick={() => setExpanded((prev) => ({ ...prev, [row.key]: !open }))}
                  >
                    {open ? 'Hide' : 'Set'} per policy ({row.units.length})
                  </Button>
                </div>
              )}
              {open && row.units.map((unit) => (
                <FreqRow
                  key={unit.id}
                  indent
                  row={unitRow(unit, unit.label, `CPU ${(unit.cpus ?? []).join(', ')}`)}
                  draft={drafts[unit.id]}
                  {...rowProps}
                />
              ))}
            </div>
          )
        })}
      </Card>

      <Card
        size="small"
        title="GPU"
        loading={status === null && !error}
        style={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}` }}
      >
        {status !== null && gpuByCard.length === 0 && (
          <Text type="secondary">No GPU frequency interface found on this machine.</Text>
        )}
        {gpuByCard.map(([card, members]) => (
          <div key={card}>
            {gpuByCard.length > 1 && (
              <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>{card}</Text>
            )}
            {members.map((unit) => (
              <FreqRow
                key={unit.id}
                row={unitRow(
                  unit,
                  unit.label.toUpperCase().replace('GT', 'GT '),
                  unit.driver ?? undefined,
                  'On Xe, GT 0 is normally the render/compute GT and GT 1 the media GT.',
                )}
                draft={drafts[unit.id]}
                {...rowProps}
              />
            ))}
          </div>
        ))}
      </Card>
    </div>
    </ConfigProvider>
  )
}
