// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// The Benchmark tab's server-push channel (benchmark/service/events.py).
//
// One EventSource for the whole app, opened at the App level rather than inside
// the tab, because a benchmark run outlives the user's attention: a build takes
// an hour, and the point of the connection is that they can go and do something
// else and still be told when it finished.
//
// Two hooks, so exactly one component owns the socket:
//   useBenchStream  – App calls this once; it holds the connection.
//   useBenchEvent   – anything else subscribes to what arrives.
//
// A module-level emitter rather than a context: the value would be a stable
// subscribe function anyway, and every consumer wants a callback, not a render.

import { useEffect, useRef } from 'react'

import { benchEventsUrl } from '../api/client'
import type { BenchEvent } from '../api/types'

// Mirrors useAppEvents: long enough not to hammer a server that is restarting,
// short enough that the tab feels live again once it is back.
const RECONNECT_DELAY_MS = 3000

type Listener = (event: BenchEvent) => void

const listeners = new Set<Listener>()

function emit(event: BenchEvent) {
  listeners.forEach((listener) => {
    try {
      listener(event)
    } catch {
      // One bad subscriber must not stop the others from seeing the event.
    }
  })
}

/**
 * Subscribe to benchmark events for as long as the component is mounted.
 *
 * The handler is held in a ref, so passing an inline arrow function does not
 * resubscribe on every render.
 */
export function useBenchEvent(handler: Listener, enabled = true): void {
  const handlerRef = useRef(handler)

  useEffect(() => {
    handlerRef.current = handler
  }, [handler])

  useEffect(() => {
    if (!enabled) return
    const listener: Listener = (event) => handlerRef.current(event)
    listeners.add(listener)
    return () => {
      listeners.delete(listener)
    }
  }, [enabled])
}

/**
 * Own the single benchmark event stream. Call this once, from App.
 *
 * `withLogs` toggles the log-delta channel. Changing it reconnects on purpose:
 * the server answers every new connection with a full snapshot, so switching to
 * the Benchmark tab re-syncs its state and log tail in one message, with no gap
 * to paper over.
 */
export function useBenchStream(enabled: boolean, withLogs: boolean): void {
  useEffect(() => {
    if (!enabled) return

    let source: EventSource | null = null
    let retry: ReturnType<typeof setTimeout> | null = null
    let closed = false

    function connect() {
      if (closed) return
      source = new EventSource(benchEventsUrl(withLogs))

      source.onmessage = (message) => {
        try {
          emit(JSON.parse(message.data) as BenchEvent)
        } catch {
          // Heartbeats are comment lines and never reach onmessage; anything
          // else unparseable is a server-side bug we cannot act on here.
        }
      }

      source.onerror = () => {
        source?.close()
        source = null
        if (!closed) retry = setTimeout(connect, RECONNECT_DELAY_MS)
      }
    }

    connect()

    return () => {
      closed = true
      if (retry) clearTimeout(retry)
      source?.close()
    }
  }, [enabled, withLogs])
}
