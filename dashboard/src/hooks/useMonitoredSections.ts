import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api/client'

interface SectionsChange {
  from: string[]
  to: string[]
  added: string[]
  removed: string[]
  updatedAt: number
}

function normalizeSections(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((name) => `${name ?? ''}`.trim()).filter(Boolean)
}

export function useMonitoredSections(
  active: boolean,
  fallbackSections: string[],
  sourceUpdatedAt?: number | null,
) {
  const [sections, setSections] = useState<string[]>(fallbackSections)
  const [updatedAt, setUpdatedAt] = useState<number>(0)
  const [lastChange, setLastChange] = useState<SectionsChange | null>(null)
  const [hasInitialLoad, setHasInitialLoad] = useState(false)

  // Refs let refresh() stay stable (empty deps) while always diffing against
  // the current committed values — avoids the stale-closure trap where a
  // focus/visibility refetch would keep seeing the mount-time hasInitialLoad.
  const sectionsRef = useRef(sections)
  const loadedRef = useRef(false)

  const refresh = useCallback(async () => {
    let data
    try {
      data = await api.getMonitoredSections()
    } catch {
      return // Keep last known sections on transient network/backend errors.
    }

    const next = normalizeSections(data.sections)
    const nextUpdatedAt = Number.isFinite(data.updated_at) ? Number(data.updated_at) : 0
    const prev = sectionsRef.current
    const added = next.filter((name) => !prev.includes(name))
    const removed = prev.filter((name) => !next.includes(name))

    // Only surface a change once the first load has established a baseline, so
    // the initial population never reads as a "change".
    if (loadedRef.current && (added.length > 0 || removed.length > 0)) {
      setLastChange({ from: prev, to: next, added, removed, updatedAt: nextUpdatedAt })
    }

    sectionsRef.current = next
    setSections(next)
    setUpdatedAt(nextUpdatedAt)
    if (!loadedRef.current) {
      loadedRef.current = true
      setHasInitialLoad(true)
    }
  }, [])

  // Initial load + refetch whenever the tab regains focus / visibility.
  useEffect(() => {
    if (!active) return
    void refresh()

    const onFocus = () => { void refresh() }
    const onVisibility = () => {
      if (document.visibilityState === 'visible') void refresh()
    }
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [active, refresh])

  // Server-signalled change: dynamic_info carries monitored_sections_updated_at,
  // which advances only when the effective sections actually change.  Refetch
  // when it moves past what we last saw.
  useEffect(() => {
    if (!active) return
    if (!sourceUpdatedAt || !Number.isFinite(sourceUpdatedAt)) return
    if (sourceUpdatedAt <= updatedAt) return
    void refresh()
  }, [active, sourceUpdatedAt, updatedAt, refresh])

  const sectionSet = useMemo(() => new Set<string>(sections), [sections])
  const clearLastChange = useCallback(() => setLastChange(null), [])

  return { sections, sectionSet, updatedAt, lastChange, clearLastChange, hasInitialLoad }
}
