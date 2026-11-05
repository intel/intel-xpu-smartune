// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// The Benchmark tab's server-push channel (benchmark/service/events.py).
//
// One stream for the whole app, requested at the App level rather than inside
// the tab, because a benchmark run outlives the user's attention: a build takes
// an hour, and the point of the connection is that they can go and do something
// else and still be told when it finished. The connection itself is shared by
// every dashboard tab of this origin (events/sseHub.ts).
//
// Two hooks, so exactly one component owns the stream:
//   useBenchStream  – App calls this once; it requests the stream.
//   useBenchEvent   – anything else subscribes to what arrives.
//
// A module-level emitter rather than a context: the value would be a stable
// subscribe function anyway, and every consumer wants a callback, not a render.

import { useEffect, useRef } from 'react'

import type { BenchEvent } from '../api/types'
import { requestBenchStream, subscribe } from '../events/sseHub'

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
    const unsubscribe = subscribe('bench', (raw) => {
      try {
        emit(JSON.parse(raw) as BenchEvent)
      } catch {
        // Heartbeats are comment lines and never reach onmessage; anything
        // else unparseable is a server-side bug we cannot act on here.
      }
    })
    const release = requestBenchStream(withLogs)
    return () => {
      release()
      unsubscribe()
    }
  }, [enabled, withLogs])
}
