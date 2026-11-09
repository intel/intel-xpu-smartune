// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// The server-push hub, run by whichever dashboard tab currently leads (see
// sseHub.ts for the election). It merges every tab's Demand, holds each stream
// once for the whole origin, and fans the events back out: to itself directly,
// to the other tabs over a BroadcastChannel.
//
// Why a leading tab and not a SharedWorker: a self-signed certificate that the
// user accepted on Chrome's warning page is honoured for the page but not for
// requests made from a SharedWorker, so on the packaged HTTPS origin a worker
// could never connect. A tab is a page. Running the same hub in dev and in the
// package also means what is tested in one is what ships in the other.

import { type Demand, NO_DEMAND, type StreamName, StreamManager, needsBenchSnapshot, unionDemand } from './streamManager'

/** Follower -> leader. */
export type ToLeader = { type: 'state'; tab: string; demand: Demand } | { type: 'bye'; tab: string }

/** Leader -> followers. `to` limits an event to some tabs; absent means all. */
export type FromLeader =
  | { type: 'event'; stream: StreamName; data: string; to?: string[] }
  | { type: 'ping' }

// The leader pings every PING_INTERVAL_MS; a tab that has not answered the
// previous ping by the next one is dropped, so a crashed tab does not hold the
// log channel open. Measured against the ping, not wall time, so a leader whose
// timers are throttled in a background tab does not drop live followers.
const PING_INTERVAL_MS = 10_000

// Tabs (re)registering in a burst -- typically all of them right after a
// leadership change -- share one snapshot reconnect instead of one each.
const SNAPSHOT_COALESCE_MS = 50

interface TabState {
  demand: Demand
  lastSeen: number
}

export class LeaderHub {
  private readonly tabs = new Map<string, TabState>()
  // Tabs that asked for a bench snapshot the current connection has not sent
  // yet. A snapshot re-syncs the receiving Benchmark page, so it goes only to
  // the tabs that asked -- one tab opening must not move another tab's view.
  // Exception: after a dropped connection every tab may have missed events.
  private readonly awaitingSnapshot = new Set<string>()
  private readonly streams: StreamManager
  private token: string | null = null
  private lastPingAt = 0
  private snapshotDue = false
  private snapshotTimer: ReturnType<typeof setTimeout> | null = null
  private readonly pingTimer: ReturnType<typeof setInterval>

  constructor(
    private readonly selfId: string,
    private readonly deliverSelf: (stream: StreamName, data: string) => void,
    private readonly post: (message: FromLeader) => void,
  ) {
    this.streams = new StreamManager((stream, data, frame) => {
      if (stream === 'bench' && frame.first && !frame.afterError) {
        this.sendTo([...this.awaitingSnapshot], stream, data)
        this.awaitingSnapshot.clear()
        return
      }
      if (stream === 'bench' && frame.first) this.awaitingSnapshot.clear()
      this.deliverSelf(stream, data)
      this.post({ type: 'event', stream, data })
    })
    this.pingTimer = setInterval(() => this.tick(), PING_INTERVAL_MS)
  }

  /** A tab (this one included) registered or changed what it wants. */
  onState(tab: string, demand: Demand): void {
    const prev = this.tabs.get(tab)?.demand ?? NO_DEMAND
    this.tabs.set(tab, { demand, lastSeen: Date.now() })
    // Every tab reads the same localStorage token, so the newest one wins.
    if (demand.token) this.token = demand.token
    if (needsBenchSnapshot(prev, demand)) {
      this.awaitingSnapshot.add(tab)
      this.scheduleSnapshot()
    } else if (!this.snapshotTimer) {
      this.reconcile()
    }
  }

  /** A tab is going away. */
  onBye(tab: string): void {
    this.forget(tab)
    this.reconcile()
  }

  /** Ask every follower to (re)register now -- used on taking the lead. */
  ping(): void {
    this.lastPingAt = Date.now()
    this.post({ type: 'ping' })
  }

  close(): void {
    clearInterval(this.pingTimer)
    if (this.snapshotTimer) clearTimeout(this.snapshotTimer)
    this.streams.close()
  }

  private sendTo(tabs: string[], stream: StreamName, data: string): void {
    if (tabs.includes(this.selfId)) this.deliverSelf(stream, data)
    const others = tabs.filter((id) => id !== this.selfId)
    if (others.length) this.post({ type: 'event', stream, data, to: others })
  }

  private forget(tab: string): void {
    this.tabs.delete(tab)
    this.awaitingSnapshot.delete(tab)
  }

  private scheduleSnapshot(): void {
    this.snapshotDue = true
    if (this.snapshotTimer) return
    this.snapshotTimer = setTimeout(() => {
      this.snapshotTimer = null
      const snapshot = this.snapshotDue
      this.snapshotDue = false
      this.reconcile(snapshot)
    }, SNAPSHOT_COALESCE_MS)
  }

  private reconcile(benchSnapshot = false): void {
    const demands = Array.from(this.tabs.values(), (state) => state.demand)
    this.streams.apply(unionDemand(demands, this.token), benchSnapshot)
  }

  private tick(): void {
    let dropped = false
    this.tabs.forEach((state, tab) => {
      if (tab !== this.selfId && state.lastSeen < this.lastPingAt) {
        this.forget(tab)
        dropped = true
      }
    })
    if (dropped) this.reconcile()
    this.ping()
  }
}
