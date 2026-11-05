// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// One SharedWorker for every dashboard tab and window of this origin: it holds
// the server-push streams once and fans each event out to all tabs, so opening
// more dashboards no longer eats into the browser's 6-connection-per-origin
// budget (see streamManager.ts). It lives as long as any tab is connected.
//
// Protocol (see sseHub.ts for the tab side):
//   tab -> worker  { type: 'state', demand }  register / update; also answers ping
//                  { type: 'bye' }             tab is going away
//   worker -> tab  { type: 'event', stream, data }
//                  { type: 'ping' }

import { type Demand, NO_DEMAND, type StreamName, StreamManager, needsBenchSnapshot, unionDemand } from './streamManager'
import type { HubToTab, TabToHub } from './sseHub'

// A tab that closes without saying 'bye' (crash, killed process) stops answering
// pings and is dropped, so it does not keep the log channel open forever. Message
// delivery is not subject to background-tab timer throttling, so a hidden tab
// still answers promptly; a frozen one re-registers when it resumes.
const PING_INTERVAL_MS = 10_000
const STALE_AFTER_MS = 35_000

interface TabState {
  demand: Demand
  lastSeen: number
}

const tabs = new Map<MessagePort, TabState>()
let token: string | null = null

// Tabs that asked for a bench snapshot the current connection has not sent yet.
// A snapshot re-syncs the receiving Benchmark page (and may switch it to the
// running job's output), so it goes only to the tabs that asked -- one tab
// opening must not move another tab's view. Exception: after a dropped
// connection every tab may have missed events, so every tab gets it.
const awaitingSnapshot = new Set<MessagePort>()

const streams = new StreamManager((stream, data, frame) => {
  if (stream === 'bench' && frame.first && !frame.afterError) {
    awaitingSnapshot.forEach((port) => port.postMessage(eventMessage(stream, data)))
    awaitingSnapshot.clear()
    return
  }
  if (stream === 'bench' && frame.first) awaitingSnapshot.clear()
  broadcast(eventMessage(stream, data))
})

function eventMessage(stream: StreamName, data: string): HubToTab {
  return { type: 'event', stream, data }
}

function broadcast(message: HubToTab): void {
  tabs.forEach((_state, port) => port.postMessage(message))
}

function forget(port: MessagePort): void {
  tabs.delete(port)
  awaitingSnapshot.delete(port)
}

function reconcile(benchSnapshot = false): void {
  const demands = Array.from(tabs.values(), (state) => state.demand)
  streams.apply(unionDemand(demands, token), benchSnapshot)
}

function onTabMessage(port: MessagePort, message: TabToHub): void {
  if (message.type === 'bye') {
    forget(port)
    reconcile()
    return
  }
  const prev = tabs.get(port)?.demand ?? NO_DEMAND
  const next = message.demand
  tabs.set(port, { demand: next, lastSeen: Date.now() })
  // Every tab reads the same localStorage token, so the newest one wins.
  if (next.token) token = next.token
  const snapshot = needsBenchSnapshot(prev, next)
  if (snapshot) awaitingSnapshot.add(port)
  reconcile(snapshot)
}

setInterval(() => {
  const now = Date.now()
  let dropped = false
  tabs.forEach((state, port) => {
    if (now - state.lastSeen > STALE_AFTER_MS) {
      forget(port)
      dropped = true
    }
  })
  if (dropped) reconcile()
  broadcast({ type: 'ping' })
}, PING_INTERVAL_MS)

// The DOM lib types `self` as Window; this file only needs onconnect.
const scope = self as unknown as { onconnect: ((event: MessageEvent) => void) | null }

scope.onconnect = (event) => {
  const port = event.ports[0]
  port.onmessage = (message: MessageEvent<TabToHub>) => onTabMessage(port, message.data)
  port.start()
}
