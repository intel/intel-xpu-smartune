// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// Owns the dashboard's two server-push streams (/app/events and /bench/events)
// for one "client": the shared worker serving every tab (sseHub.worker.ts), or a
// single tab when SharedWorker is unavailable (sseHub.ts falls back).
//
// Why this exists: HTTP/1.1 allows only 6 connections per origin, shared by every
// tab. Each EventSource holds one forever, so opening them per tab (or per hook)
// starved ordinary requests once two dashboards were open. Whatever runs this
// opens each stream at most once.
//
// No DOM dependencies beyond EventSource and timers, so it runs in a worker.

export type StreamName = 'app' | 'bench'

/** What one client wants open. The bench log channel only matters while `bench`. */
export interface Demand {
  token: string | null
  app: boolean
  bench: boolean
  benchLogs: boolean
}

export const NO_DEMAND: Demand = { token: null, app: false, bench: false, benchLogs: false }

// Long enough not to hammer a server that is restarting, short enough that the
// dashboard feels live again once it is back.
const RECONNECT_DELAY_MS = 3000

// EventSource cannot set headers, so the server accepts the token as ?token=.
export function appEventsUrl(token: string | null): string {
  return token ? `/api/app/events?token=${encodeURIComponent(token)}` : '/api/app/events'
}

/**
 * `withLogs` asks the server to also stream the running job's output -- off unless
 * someone is looking at the Benchmark tab, since a build writes megabytes of log.
 * `clientId` lets the server retire the stream a reconnect replaces instead of
 * counting both against its client limit (benchmark/service/events.py).
 */
export function benchEventsUrl(token: string | null, withLogs: boolean, clientId: string): string {
  const params = new URLSearchParams()
  if (token) params.set('token', token)
  if (withLogs) params.set('logs', '1')
  params.set('client', clientId)
  return `/api/bench/events?${params.toString()}`
}

export function newClientId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

/** Merge several clients' demands: a stream is open if anyone wants it. */
export function unionDemand(demands: Iterable<Demand>, token: string | null): Demand {
  const merged: Demand = { ...NO_DEMAND, token }
  for (const d of demands) {
    merged.app ||= d.app
    merged.bench ||= d.bench
    merged.benchLogs ||= d.bench && d.benchLogs
  }
  return merged
}

/**
 * The server answers every new bench connection with a full snapshot, which is how
 * a client syncs its Benchmark state and log tail. A client that has just started
 * wanting the stream, or its logs, has not seen one yet -- so the shared stream
 * must reconnect even when it is already open with the right URL.
 */
export function needsBenchSnapshot(prev: Demand, next: Demand): boolean {
  if (!next.bench) return false
  return !prev.bench || (next.benchLogs && !prev.benchLogs)
}

/**
 * Where a message sits in its connection. The server opens every bench connection
 * with a snapshot, so `first` marks it; `afterError` says this is the first message
 * since a connection dropped, when every listener may have missed events.
 */
export interface FrameInfo {
  first: boolean
  afterError: boolean
}

type Deliver = (stream: StreamName, data: string, frame: FrameInfo) => void

class Stream {
  private source: EventSource | null = null
  private retry: ReturnType<typeof setTimeout> | null = null
  private url: string | null = null
  // A connection dropped and no message has arrived since: listeners may be stale.
  private recovering = false

  constructor(private readonly onMessage: (data: string, frame: FrameInfo) => void) {}

  /** Open `url` (null closes). Same URL keeps the connection unless `force`. */
  set(url: string | null, force = false): void {
    if (url === this.url && !force) return
    this.url = url
    this.disconnect()
    if (url) this.connect()
    else this.recovering = false
  }

  private connect(): void {
    if (!this.url) return
    const source = new EventSource(this.url)
    let first = true
    source.onmessage = (message) => {
      const frame = { first, afterError: this.recovering }
      first = false
      this.recovering = false
      this.onMessage(message.data, frame)
    }
    source.onerror = () => {
      // EventSource would retry on its own, but not after an HTTP error such as a
      // 401 -- closing and retrying ourselves covers both the same way.
      source.close()
      if (this.source !== source) return
      this.source = null
      this.recovering = true
      this.retry = setTimeout(() => {
        this.retry = null
        this.connect()
      }, RECONNECT_DELAY_MS)
    }
    this.source = source
  }

  private disconnect(): void {
    if (this.retry) clearTimeout(this.retry)
    this.retry = null
    this.source?.close()
    this.source = null
  }
}

export class StreamManager {
  private readonly app: Stream
  private readonly bench: Stream
  private readonly clientId = newClientId()

  constructor(deliver: Deliver) {
    this.app = new Stream((data, frame) => deliver('app', data, frame))
    this.bench = new Stream((data, frame) => deliver('bench', data, frame))
  }

  /** Bring the open streams in line with `demand`; `benchSnapshot` forces a bench reconnect. */
  apply(demand: Demand, benchSnapshot = false): void {
    this.app.set(demand.app ? appEventsUrl(demand.token) : null)
    this.bench.set(
      demand.bench ? benchEventsUrl(demand.token, demand.benchLogs, this.clientId) : null,
      benchSnapshot,
    )
  }

  close(): void {
    this.apply(NO_DEMAND)
  }
}
