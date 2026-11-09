import React, { createContext, useContext, useEffect, useState } from 'react'
import { Form, InputNumber, Select, Space, Typography } from 'antd'
import { api } from '../api/client'
import type { ConfigFieldHint, ConfigHintsData } from '../api/types'

const { Text } = Typography

// ---------------------------------------------------------------------------
// Settings field guidance. The hints (range, step, presets, what up/down does) and the
// startup defaults both come from the backend, so every client shows the same thing.
// Everything here degrades to the plain inputs when the hints cannot be fetched.
// ---------------------------------------------------------------------------

// Fetched once per page load: the hints are static and the startup defaults do not move
// while the backend runs.  A failed fetch is not cached, so reopening Settings retries.
let hintsPromise: Promise<ConfigHintsData | null> | null = null

function loadConfigHints(): Promise<ConfigHintsData | null> {
  if (!hintsPromise) {
    hintsPromise = api.getConfigHints().catch((error) => {
      console.error('Failed to load config hints', error)
      hintsPromise = null
      return null
    })
  }
  return hintsPromise
}

export function useConfigHints(): ConfigHintsData | null {
  const [hints, setHints] = useState<ConfigHintsData | null>(null)
  useEffect(() => {
    let alive = true
    loadConfigHints().then((data) => {
      if (alive) setHints(data)
    })
    return () => {
      alive = false
    }
  }, [])
  return hints
}

export interface HintScope {
  section: string
  hints: ConfigHintsData | null
  // Startup defaults already converted to the shape of this section's form.
  defaults: Record<string, unknown> | null
  // setFieldsValue does not raise onFieldsChange, so programmatic edits report themselves.
  markDirty: () => void
}

export const HintScopeContext = createContext<HintScope | null>(null)

// Exact key first, then a key with "*" standing for one path segment.
export function findHint(
  hints: ConfigHintsData | null | undefined,
  section: string,
  path: string[],
): ConfigFieldHint | undefined {
  if (!hints) return undefined
  const parts = [section, ...path]
  const exact = hints.fields[parts.join('.')]
  if (exact) return exact
  for (const [key, hint] of Object.entries(hints.fields)) {
    const keyParts = key.split('.')
    if (keyParts.length === parts.length && keyParts.every((seg, i) => seg === '*' || seg === parts[i])) {
      return hint
    }
  }
  return undefined
}

function getPath(obj: unknown, path: string[]): unknown {
  return path.reduce<unknown>(
    (cur, key) => (cur && typeof cur === 'object' ? (cur as Record<string, unknown>)[key] : undefined),
    obj,
  )
}

function withUnit(value: unknown, unit?: string): string {
  if (value === undefined || value === null) return ''
  const text = typeof value === 'number' ? String(Number(value.toFixed(4))) : String(value)
  if (!unit) return text
  return unit === '%' || unit === 'x' ? `${text}${unit}` : `${text} ${unit}`
}

// What moving the value does, shown under the field rather than hidden in a tooltip so it
// is read at the moment the number is being chosen.
function HintLines({ info, hint }: { info: string; hint?: ConfigFieldHint }) {
  return (
    <div style={{ fontSize: 12, lineHeight: 1.5 }}>
      {info && <div>{info}</div>}
      {hint && (
        <>
          <div>↑ {hint.up}</div>
          <div>↓ {hint.down}</div>
          {hint.note && <div>{hint.note}</div>}
          {hint.apply && <div>Takes effect: {hint.apply}</div>}
        </>
      )}
    </div>
  )
}

const CUSTOM = '__custom__'

interface PresetControlProps {
  hint: ConfigFieldHint
  value?: number
  onChange?: (value: number | null) => void
  disabled?: boolean
  min?: number
  max?: number
  step?: number
}

// Named levels for a value people cannot reason about in raw numbers, plus a way out to
// an arbitrary number.  Which level shows is derived from the value, so a stored number
// that matches no level reads as Custom instead of being silently snapped to one.
function PresetControl({ hint, value, onChange, disabled, min, max, step }: PresetControlProps) {
  const presets = hint.presets ?? []
  const matched = presets.find((p) => p.value === value)
  const [forcedCustom, setForcedCustom] = useState(false)
  const custom = hint.custom !== false && (forcedCustom || (value !== undefined && value !== null && !matched))
  const options = [
    ...presets.map((p) => ({
      value: p.value as number,
      label: p.label.includes(String(p.value)) ? p.label : `${p.label} · ${withUnit(p.value, hint.unit)}`,
    })),
    ...(hint.custom !== false ? [{ value: CUSTOM as unknown as number, label: 'Custom…' }] : []),
  ]
  return (
    <>
      <Space.Compact style={{ width: '100%' }}>
        <Select
          style={{ width: custom ? '55%' : '100%' }}
          value={(custom ? CUSTOM : matched?.value) as number | undefined}
          options={options}
          disabled={disabled}
          onChange={(next) => {
            if ((next as unknown) === CUSTOM) {
              setForcedCustom(true)
              return
            }
            setForcedCustom(false)
            onChange?.(next)
          }}
        />
        {custom && (
          <InputNumber
            style={{ width: '45%' }}
            value={value}
            onChange={onChange}
            min={min ?? hint.min}
            max={max ?? hint.max}
            step={step ?? hint.step}
            disabled={disabled}
          />
        )}
      </Space.Compact>
      {matched && !custom && (
        <Text type="secondary" style={{ fontSize: 12 }}>{matched.desc}</Text>
      )}
    </>
  )
}

interface HintedItemProps {
  name: string | string[]
  label?: React.ReactNode
  // The field's own description (what it is); what up/down does comes from the hint.
  tooltip?: string
  rules?: React.ComponentProps<typeof Form.Item>['rules']
  // Used when the hints are unavailable, and for the range checks the form rules also enforce.
  min?: number
  max?: number
  step?: number
  addonAfter?: React.ReactNode
  disabled?: boolean
  dependencies?: React.ComponentProps<typeof Form.Item>['dependencies']
  // For a row of sibling fields that share one guidance block (see HintNote): show only
  // the default and step under each box.
  compact?: boolean
}

// A numeric field with its guidance attached: under the box, the startup default and the
// suggested step, then what raising or lowering the value does.
// Fields whose hint is a preset render as a level dropdown instead of a bare number.
export function HintedItem({
  name, label, tooltip, rules, min, max, step, addonAfter, disabled, dependencies, compact,
}: HintedItemProps) {
  const scope = useContext(HintScopeContext)
  const path = Array.isArray(name) ? name : [name]
  const hint = findHint(scope?.hints, scope?.section ?? '', path)
  const fallbackDefault = scope?.defaults ? getPath(scope.defaults, path) : undefined
  const effectiveStep = hint?.step ?? step
  const unit = hint?.unit ?? (typeof addonAfter === 'string' ? addonAfter : undefined)

  const infoParts: string[] = []
  if (fallbackDefault !== undefined && fallbackDefault !== null) {
    infoParts.push(`Default ${withUnit(fallbackDefault, unit)}`)
  }
  if (effectiveStep !== undefined && hint?.kind !== 'preset') {
    infoParts.push(`step ${effectiveStep}`)
  }

  const control = hint?.kind === 'preset'
    ? <PresetControl hint={hint} disabled={disabled} min={min} max={max} step={effectiveStep} />
    : (
      <InputNumber
        style={{ width: '100%' }}
        min={min ?? hint?.min}
        max={max ?? hint?.max}
        step={effectiveStep}
        addonAfter={addonAfter}
        disabled={disabled}
      />
    )

  return (
    <Form.Item
      label={label}
      name={name}
      tooltip={tooltip}
      extra={infoParts.length > 0 || (hint && !compact) ? <HintLines info={infoParts.join(' · ')} hint={compact ? undefined : hint} /> : undefined}
      required={false}
      rules={rules}
      dependencies={dependencies}
    >
      {control}
    </Form.Item>
  )
}

// A bare input for a cell in a table of identical fields (no label, no per-cell error line),
// with just the startup default under it.  The table's guidance is stated once, in a HintNote.
export function HintedCell({
  name, rules, min, max, step, addonAfter, disabled,
}: Omit<HintedItemProps, 'label' | 'tooltip' | 'dependencies' | 'compact'>) {
  const scope = useContext(HintScopeContext)
  const path = Array.isArray(name) ? name : [name]
  const hint = findHint(scope?.hints, scope?.section ?? '', path)
  const fallbackDefault = scope?.defaults ? getPath(scope.defaults, path) : undefined
  const unit = hint?.unit ?? (typeof addonAfter === 'string' ? addonAfter : undefined)
  return (
    <div>
      <Form.Item name={name} noStyle rules={rules}>
        <InputNumber
          style={{ width: '100%' }}
          min={min ?? hint?.min}
          max={max ?? hint?.max}
          step={hint?.step ?? step}
          addonAfter={addonAfter}
          disabled={disabled}
        />
      </Form.Item>
      {fallbackDefault !== undefined && fallbackDefault !== null && (
        <Text type="secondary" style={{ fontSize: 11 }}>default {withUnit(fallbackDefault, unit)}</Text>
      )}
    </div>
  )
}

// What raising or lowering the values of a table does, stated once under it.  ``name`` is
// the form path of any one cell; the hint is shared by the whole row or column it stands for.
export function HintNote({ name, title }: { name: string[]; title?: string }) {
  const scope = useContext(HintScopeContext)
  const hint = findHint(scope?.hints, scope?.section ?? '', name)
  if (!hint) return null
  return (
    <Text type="secondary" style={{ display: 'block', marginTop: 6 }}>
      <HintLines info={title ?? ''} hint={hint} />
    </Text>
  )
}
