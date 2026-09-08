// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// A multi-select filter over one axis of the benchmark results.
//
// Shared by the selectors of the Analysis tab, so that four boxes sitting in one
// row cannot mean opposite things by the same empty state. The Results tab does
// not use it: that tab's boxes are a search, where an empty one asks for
// everything -- see BenchResults.

import { useEffect, useRef, useState } from 'react'

/**
 * A filter that starts out holding what the last run produced.
 *
 * These selectors used to sit empty and mean "all", which put the controls and
 * what they filter at odds: three blank boxes above a chart of every run there
 * is. So the selection is explicit, and an empty one now means empty.
 *
 * What it starts out holding is `initial` -- for these tabs, the models, devices
 * and precisions of the most recent job. Seeding with *everything* was the first
 * attempt and it is wrong for the same reason the blank boxes were: someone who
 * has just pressed Run is looking at that run, and a chart of every measurement
 * the machine has ever taken buries it. Absent an `initial` (a tree whose runs
 * nothing can date) it falls back to everything, since some selection has to be
 * better than none.
 *
 * Values that appear later -- a run finishes on a device nothing had used yet
 * -- are added to the selection; values the user has taken out stay out,
 * because `seen` remembers that they were once offered. Without that
 * distinction every refresh would silently undo every deselection. It is also
 * what brings the *next* run on screen without the user touching a selector.
 */
export function useDimension(
  values: string[] | undefined,
  initial?: string[],
): [string[], (next: string[]) => void] {
  const [selected, setSelected] = useState<string[]>([])
  const seen = useRef(new Set<string>())
  const seeded = useRef(false)
  // Stable dependencies: the dimensions object is rebuilt by every fetch, so
  // the arrays' identities change on each one even when their contents do not.
  const key = (values ?? []).join('\u0000')
  const initialKey = (initial ?? []).join('\u0000')

  useEffect(() => {
    const all = key ? key.split('\u0000') : []
    if (all.length === 0) return

    if (!seeded.current) {
      seeded.current = true
      all.forEach((value) => seen.current.add(value))
      // Intersected rather than trusted: `initial` is derived from the rows and
      // `values` from the axes the backend published, and a value in one but not
      // the other would be a selection the dropdown can neither show nor clear.
      const wanted = initialKey ? initialKey.split('\u0000').filter((v) => all.includes(v)) : []
      setSelected(wanted.length > 0 ? wanted : all)
      return
    }

    const fresh = all.filter((value) => !seen.current.has(value))
    if (fresh.length === 0) return
    fresh.forEach((value) => seen.current.add(value))
    setSelected((prev) => [...prev, ...fresh])
  }, [key, initialKey])

  return [selected, setSelected]
}

/**
 * Whether a row's value passes a filter.
 *
 * A row with no value for the axis at all is not something the selector can
 * offer, so it is never filtered out -- hiding it would make it unreachable.
 */
export function passesDimension(value: string, selected: string[]): boolean {
  return !value || selected.includes(value)
}
