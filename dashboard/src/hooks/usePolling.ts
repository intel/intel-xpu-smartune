// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef } from 'react'

export function usePolling(callback: () => void | Promise<void>, intervalMs: number, enabled = true) {
  const savedCallback = useRef(callback)

  useEffect(() => {
    savedCallback.current = callback
  }, [callback])

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    let timeoutId: ReturnType<typeof setTimeout> | undefined

    const run = async () => {
      if (cancelled) return
      try {
        await savedCallback.current()
      } catch {
        // Errors are expected to be handled by the callback itself
      }
      if (!cancelled) {
        timeoutId = setTimeout(run, intervalMs)
      }
    }

    run()

    return () => {
      cancelled = true
      clearTimeout(timeoutId)
    }
  }, [intervalMs, enabled])
}
