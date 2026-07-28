// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef } from 'react'

import { isBackendUnreachable } from '../api/client'

// Once the backend is unreachable (see client.ts), stop hammering it at the
// normal cadence and probe at this slow interval instead. A single successful
// request resets the reachability flag, so polling returns to intervalMs on the
// very next tick after the server comes back.
const RECONNECT_INTERVAL_MS = 30000

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
        const delay = isBackendUnreachable() ? RECONNECT_INTERVAL_MS : intervalMs
        timeoutId = setTimeout(run, delay)
      }
    }

    run()

    return () => {
      cancelled = true
      clearTimeout(timeoutId)
    }
  }, [intervalMs, enabled])
}
