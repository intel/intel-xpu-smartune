// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// The tab side of the shared server-push streams. Hooks subscribe here instead
// of opening their own EventSource; this module turns "who in this tab is
// listening" into a Demand and hands it to the SharedWorker (sseHub.worker.ts),
// which opens each stream once for every tab of the origin.
//
// Without SharedWorker (e.g. Chrome on Android) or if the worker fails to load,
// the same StreamManager runs in this tab instead: one connection per stream per
// tab, which is still better than one per hook.

import { getToken } from '../api/client'
import { type Demand, type StreamName, StreamManager, needsBenchSnapshot } from './streamManager'

export type TabToHub = { type: 'state'; demand: Demand } | { type: 'bye' }
export type HubToTab = { type: 'event'; stream: StreamName; data: string } | { type: 'ping' }

type Listener = (data: string) => void

const listeners: Record<StreamName, Set<Listener>> = { app: new Set(), bench: new Set() }
let benchStream = { enabled: false, withLogs: false }

function currentDemand(): Demand {
  return {
    token: getToken(),
    app: listeners.app.size > 0,
    bench: benchStream.enabled,
    benchLogs: benchStream.enabled && benchStream.withLogs,
  }
}

function deliver(stream: StreamName, data: string): void {
  listeners[stream].forEach((listener) => {
    try {
      listener(data)
    } catch {
      // One bad subscriber must not stop the others from seeing the event.
    }
  })
}

interface Transport {
  send(demand: Demand): void
}

/** Fallback: this tab owns its own streams. */
function localTransport(): Transport {
  const streams = new StreamManager(deliver)
  let prev: Demand | null = null
  return {
    send(demand) {
      streams.apply(demand, prev !== null && needsBenchSnapshot(prev, demand))
      prev = demand
    },
  }
}

function sharedWorkerTransport(onFailure: () => void): Transport | null {
  if (typeof SharedWorker === 'undefined') return null
  let worker: SharedWorker
  try {
    // Literal `new URL(..., import.meta.url)` so Vite bundles the worker.
    worker = new SharedWorker(new URL('./sseHub.worker.ts', import.meta.url), {
      type: 'module',
      name: 'smartune-sse',
    })
  } catch {
    return null
  }
  const port = worker.port
  const post = (message: TabToHub) => port.postMessage(message)

  worker.onerror = () => onFailure()
  port.onmessage = (message: MessageEvent<HubToTab>) => {
    const data = message.data
    if (data.type === 'event') deliver(data.stream, data.data)
    else if (data.type === 'ping') post({ type: 'state', demand: currentDemand() })
  }
  port.start()

  // Leaving (or entering the back/forward cache) releases this tab's share right
  // away; coming back, or resuming from a freeze, registers it again.
  window.addEventListener('pagehide', () => post({ type: 'bye' }))
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) post({ type: 'state', demand: currentDemand() })
  })
  document.addEventListener('resume', () => post({ type: 'state', demand: currentDemand() }))

  return { send: (demand) => post({ type: 'state', demand }) }
}

let transport: Transport | null = null
let lastSent = ''
let syncQueued = false

function getTransport(): Transport {
  if (!transport) {
    transport = sharedWorkerTransport(() => {
      // The worker script failed: take the streams over in this tab.
      transport = localTransport()
      lastSent = ''
      scheduleSync()
    }) ?? localTransport()
  }
  return transport
}

// Coalesce the subscribe/unsubscribe bursts of a render (and StrictMode's double
// mount) into one update.
function scheduleSync(): void {
  if (syncQueued) return
  syncQueued = true
  queueMicrotask(() => {
    syncQueued = false
    const demand = currentDemand()
    const key = JSON.stringify(demand)
    if (key === lastSent) return
    lastSent = key
    getTransport().send(demand)
  })
}

/** Receive every raw `data:` payload of a stream until the returned function is called. */
export function subscribe(stream: StreamName, listener: Listener): () => void {
  listeners[stream].add(listener)
  scheduleSync()
  return () => {
    listeners[stream].delete(listener)
    scheduleSync()
  }
}

/**
 * Whether this tab wants the bench stream open, and with the log channel. Bench
 * listeners alone do not open it: App decides, so a run finishing can still be
 * reported from any tab. Returns a function that withdraws the request.
 */
export function requestBenchStream(withLogs: boolean): () => void {
  benchStream = { enabled: true, withLogs }
  scheduleSync()
  return () => {
    benchStream = { enabled: false, withLogs: false }
    scheduleSync()
  }
}
