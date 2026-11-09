// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// The tab side of the shared server-push streams. Hooks subscribe here instead
// of opening their own EventSource; this module turns "who in this tab is
// listening" into a Demand and hands it to the hub that holds the streams.
//
// Why share at all: HTTP/1.1 allows only 6 connections per origin, shared by
// every tab. Each stream holds one forever, so with per-tab streams a third
// dashboard tab starves every ordinary request.
//
// Who holds them: one tab, elected with the Web Locks API -- whichever holds the
// `smartune-sse-leader` lock runs the LeaderHub (leaderHub.ts) and the other
// tabs talk to it over a BroadcastChannel. When the leader goes away its lock is
// released and the next tab in line takes over. The hub runs in a page, so a
// self-signed certificate the user accepted on the browser's warning page
// applies to its connections (it would not inside a SharedWorker).
//
// Without Web Locks or BroadcastChannel (very old browsers, or a non-secure
// origin such as plain http on a LAN address) the same StreamManager runs in
// this tab instead: one connection per stream per tab.

import { getToken } from '../api/client'
import { type FromLeader, LeaderHub, type ToLeader } from './leaderHub'
import { type Demand, type StreamName, StreamManager, needsBenchSnapshot, newClientId } from './streamManager'

type Listener = (data: string) => void

const LEADER_LOCK = 'smartune-sse-leader'
const CHANNEL_NAME = 'smartune-sse'

const listeners: Record<StreamName, Set<Listener>> = { app: new Set(), bench: new Set() }
let benchStream = { enabled: false, withLogs: false }
// Set once the service is stopped: no stream is wanted any more, whatever is
// still subscribed, so nothing keeps reconnecting to a server that is gone.
let suspended = false

function currentDemand(): Demand {
  if (suspended) return { token: getToken(), app: false, bench: false, benchLogs: false }
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

/** Share the streams with every tab of this origin through an elected leader. */
function leaderTransport(): Transport | null {
  if (typeof BroadcastChannel === 'undefined' || !navigator.locks) return null
  const tabId = newClientId()
  const channel = new BroadcastChannel(CHANNEL_NAME)
  let hub: LeaderHub | null = null
  let demand: Demand | null = null

  const post = (message: ToLeader) => channel.postMessage(message)
  // Register (or re-register) this tab's demand with whoever leads.
  const sendState = () => {
    if (!demand) return
    if (hub) hub.onState(tabId, demand)
    else post({ type: 'state', tab: tabId, demand })
  }

  channel.onmessage = (message: MessageEvent<ToLeader | FromLeader>) => {
    const data = message.data
    if (hub) {
      // Leading: the only messages that concern us are the followers'.
      if (data.type === 'state') hub.onState(data.tab, data.demand)
      else if (data.type === 'bye') hub.onBye(data.tab)
      return
    }
    // Following: events from the leader (other followers' messages are ignored).
    if (data.type === 'event') {
      if (!data.to || data.to.includes(tabId)) deliver(data.stream, data.data)
    } else if (data.type === 'ping') {
      sendState()
    }
  }

  // Queue for the lock. The callback runs once this tab becomes the leader and
  // never resolves, so the lock is held until the page goes away -- at which
  // point the browser releases it and the next queued tab takes over.
  navigator.locks
    .request(LEADER_LOCK, () => {
      hub = new LeaderHub(tabId, deliver, (message) => channel.postMessage(message))
      if (demand) hub.onState(tabId, demand)
      // Every follower re-registers at once and gets a fresh snapshot, so a
      // leadership change leaves no tab behind on events the old leader had.
      hub.ping()
      return new Promise<never>(() => {})
    })
    .catch(() => {})

  // A follower leaving (or entering the back/forward cache) releases its share
  // right away; coming back, or resuming from a freeze, registers it again. The
  // leader needs nothing here: its lock is released with the page.
  window.addEventListener('pagehide', () => {
    if (!hub) post({ type: 'bye', tab: tabId })
  })
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) sendState()
  })
  document.addEventListener('resume', sendState)

  return {
    send(next) {
      demand = next
      sendState()
    },
  }
}

let transport: Transport | null = null
let lastSent = ''
let syncQueued = false

function getTransport(): Transport {
  if (!transport) transport = leaderTransport() ?? localTransport()
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

/** Close this tab's share of every stream for good (the service was stopped). */
export function suspendStreams(): void {
  suspended = true
  scheduleSync()
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
