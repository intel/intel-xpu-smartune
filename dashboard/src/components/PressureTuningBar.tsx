import React, { useCallback, useContext, useEffect, useRef, useState } from 'react'
import { Button, Form, Popover, Space, Tooltip, Typography, message } from 'antd'
import { InfoCircleOutlined, MinusOutlined, PlusOutlined } from '@ant-design/icons'
import type { NamePath } from 'antd/es/form/interface'
import { api } from '../api/client'
import type { PressureGroup, PressureTuningResult } from '../api/types'
import { COLORS } from '../styles/theme'
import { HintScopeContext } from './settingsHints'

const { Text } = Typography

// ---------------------------------------------------------------------------
// One-click "sensitive / relaxed" stepping for a pressure card. The step rules live on the
// backend (monitor/pressure_tuning.py); this bar only sends the form's current values,
// fills the proposed ones back in, and leaves saving to the usual Save button.
// ---------------------------------------------------------------------------

// Backend parameters are named by their config path; each card's form names them its own way.
const FIELD_OF: Record<PressureGroup, (path: string) => NamePath> = {
  system: (path) => path,
  disk: (path) => {
    const parts = path.split('.')
    if (parts[0] === 'disk_psi_weights') return ['disk_psi_weights', parts[1]]
    if (parts[0] === 'disk_pressure_model') {
      return parts[1] === 'sub_weights' ? ['sub_weights', parts[2]] : parts[1]
    }
    return path
  },
  network: (path) => `network_pressure_model_${path.split('.')[1]}`,
}

const TITLE: Record<PressureGroup, string> = {
  system: 'system',
  disk: 'disk I/O',
  network: 'network',
}

const RELAX_TONE = '#4096ff'
const SENSITIVE_TONE = '#ff7a45'

const formatOffset = (offset: number) => `${offset > 0 ? '+' : ''}${(offset * 100).toFixed(1)}%`

export default function PressureTuningBar({ group }: { group: PressureGroup }) {
  const form = Form.useFormInstance()
  const markDirty = useContext(HintScopeContext)?.markDirty
  // Re-render on every edit so the position shown follows unsaved changes too.
  const watched = Form.useWatch([], form)
  const [paths, setPaths] = useState<string[]>([])
  const [info, setInfo] = useState<PressureTuningResult | null>(null)
  const [busy, setBusy] = useState(false)
  const alive = useRef(true)
  // Only the newest request may update the counter; a slower, older answer must not overwrite it.
  const latest = useRef(0)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  // The controlled parameters, learned once from the backend's own table.
  useEffect(() => {
    api.previewPressureTuning(group, {})
      .then((res) => alive.current && setPaths(res.controls.map((c) => c.path)))
      .catch((error) => console.error('Could not load pressure tuning parameters', error))
  }, [group])

  const gather = useCallback(() => {
    const current: Record<string, number> = {}
    for (const path of paths) {
      const value = form.getFieldValue(FIELD_OF[group](path))
      if (typeof value === 'number' && Number.isFinite(value)) current[path] = value
    }
    return current
  }, [form, group, paths])

  // Where the form's values sit now. Debounced: it follows typing, and a request per
  // keystroke would be wasteful.
  useEffect(() => {
    if (paths.length === 0) return undefined
    const timer = window.setTimeout(() => {
      const seq = ++latest.current
      api.previewPressureTuning(group, gather())
        .then((res) => alive.current && seq === latest.current && setInfo(res))
        .catch((error) => console.error('Could not read pressure tuning position', error))
    }, 300)
    return () => window.clearTimeout(timer)
  }, [watched, group, paths, gather])

  const step = async (direction: 'relax' | 'tighten') => {
    setBusy(true)
    const seq = ++latest.current
    try {
      const res = await api.previewPressureTuning(group, gather(), direction)
      const values = res.values ?? {}
      for (const [path, value] of Object.entries(values)) {
        form.setFieldValue(FIELD_OF[group](path), value)
      }
      if (Object.keys(values).length > 0) markDirty?.()
      if (alive.current && seq === latest.current) setInfo(res)
      const notes = res.notes ?? []
      if (notes.length > 0) {
        message.warning({
          duration: 8,
          content: (
            <div style={{ textAlign: 'left' }}>
              <div>
                {Object.keys(values).length === 0
                  ? 'Nothing could be changed any further:'
                  : 'Some parameters stopped at their limit:'}
              </div>
              {notes.map((note) => <div key={note}>• {note}</div>)}
            </div>
          ),
        })
      }
    } catch (error) {
      message.error(error instanceof Error ? error.message : 'Could not compute the next step')
    } finally {
      setBusy(false)
    }
  }

  const state = info?.state
  const reference = info?.reference_source === 'startup' ? 'startup values' : 'factory'
  // The counter: +1 per Relaxed click, -1 per Sensitive click, 0 at the reference.
  const n = state?.n ?? 0
  const counter = !state ? '…' : n > 0 ? `+${n}` : n < 0 ? `\u2212${Math.abs(n)}` : '0'
  const tone = n > 0 ? RELAX_TONE : n < 0 ? SENSITIVE_TONE : undefined

  const details = (
    <div style={{ maxWidth: 320 }}>
      <Text strong>Compared with {reference}</Text>
      <div style={{ marginTop: 6 }}>
        {(info?.details ?? []).length === 0
          ? <Text type="secondary">No controlled parameter has been changed.</Text>
          : info?.details.map((d) => (
            <div key={d.path} style={{ display: 'flex', justifyContent: 'space-between', gap: 16 }}>
              <span>{d.label}</span>
              <Text type="secondary">{formatOffset(d.offset)}</Text>
            </div>
          ))}
      </div>
      <div style={{ marginTop: 10 }}>
        <Text strong>Parameters this stepper changes</Text>
        <div>
          <Text type="secondary">{(info?.controls ?? []).map((c) => c.label).join(', ')}</Text>
        </div>
      </div>
    </div>
  )

  const tip = `Each click is one small step on the ${TITLE[group]} pressure parameters below. + makes the system less likely to report pressure: the same load reads lower, so alerts and limits start later, and pressure may never reach critical. \u2212 does the opposite: pressure is reported sooner. Thresholds and resource weights are not changed. Press Save to keep the result.`

  return (
    <div style={{ margin: '0 0 16px', display: 'flex', justifyContent: 'flex-end' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <Text style={{ color: SENSITIVE_TONE, fontWeight: 500 }}>Sensitive</Text>
        <Space.Compact>
          <Tooltip title="Sensitive step: counter −1">
            <Button
              icon={<MinusOutlined />}
              loading={busy}
              disabled={busy || !state || !state.can_tighten}
              onClick={() => step('tighten')}
              aria-label="Sensitive step"
            />
          </Tooltip>
          <Popover content={details} title={null} trigger="click">
            <div
              style={{
                minWidth: 64,
                padding: '0 12px',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                cursor: 'pointer',
                fontSize: 16,
                fontWeight: 600,
                color: tone,
                border: `1px solid ${COLORS.border}`,
                borderLeft: 'none',
                borderRight: 'none',
              }}
            >
              {counter}
            </div>
          </Popover>
          <Tooltip title="Relaxed step: counter +1">
            <Button
              icon={<PlusOutlined />}
              loading={busy}
              disabled={busy || !state || !state.can_relax}
              onClick={() => step('relax')}
              aria-label="Relaxed step"
            />
          </Tooltip>
        </Space.Compact>
        <Text style={{ color: RELAX_TONE, fontWeight: 500 }}>Relaxed</Text>
        <Tooltip title={tip} placement="topRight">
          <InfoCircleOutlined style={{ color: COLORS.textMuted }} />
        </Tooltip>
      </div>
    </div>
  )
}
