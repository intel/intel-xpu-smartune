import type { DiagEvent, DiagSeverity } from '../api/types'

const LIVE_QUERY_LEAD_SECONDS = 5 * 60

export interface DiagnosticDisplayEvent {
  event: DiagEvent
  resources: string[]
  // Every event type merged into this row (one per resource of a limit batch).
  eventTypes: string[]
}

export function createLiveDiagnosticWindow(from: number, to: number) {
  return { from, to, queryTo: to + LIVE_QUERY_LEAD_SECONDS }
}

/** Milliseconds since the epoch for an event time; 0 when unparsable. Order by
 *  this, not by the string: rows recorded before the server stored UTC carry a
 *  local offset, and "+08:00" text sorts eight hours late against "+00:00". */
export function eventTimeMs(ts: string | null | undefined): number {
  const ms = ts ? Date.parse(ts) : NaN
  return Number.isFinite(ms) ? ms : 0
}

export type ProtectionAction = 'APPLIED' | 'RECOVERED' | 'FAILED'

const PROTECTION_ACTIONS: Partial<Record<string, ProtectionAction>> = {
  applied: 'APPLIED',
  recovered: 'RECOVERED',
  failed: 'FAILED',
}

/** The resource-limit action an event records, from the lifecycle_action the
 *  server derives from its event catalog -- never from the event name, which a
 *  memory reclaim or a freeze failure can share. Null for lifecycle markers and
 *  every other event. */
export function protectionAction(event: DiagEvent): ProtectionAction | null {
  return (event.lifecycle_action && PROTECTION_ACTIONS[event.lifecycle_action]) || null
}

function displayEvent(event: DiagEvent): DiagnosticDisplayEvent {
  return { event, resources: event.resource_type ? [event.resource_type] : [], eventTypes: [event.event_type] }
}

export function groupDiagnosticEvents(events: DiagEvent[]): DiagnosticDisplayEvent[] {
  const grouped = new Map<string, DiagnosticDisplayEvent>()
  const ungrouped: DiagnosticDisplayEvent[] = []
  for (const event of events) {
    const action = protectionAction(event)
    if (!action) {
      ungrouped.push(displayEvent(event))
      continue
    }
    const key = `${event.protection_id}:${action}:${event.ts_utc.slice(0, 19)}`
    const existing = grouped.get(key)
    if (existing) {
      if (event.resource_type && !existing.resources.includes(event.resource_type)) existing.resources.push(event.resource_type)
      if (!existing.eventTypes.includes(event.event_type)) existing.eventTypes.push(event.event_type)
    } else {
      grouped.set(key, displayEvent(event))
    }
  }
  return [...ungrouped, ...grouped.values()].sort((left, right) => eventTimeMs(right.event.ts_utc) - eventTimeMs(left.event.ts_utc))
}

export function summarizeVisibleDiagnosticEvents(
  events: DiagEvent[],
  from: number,
  to: number,
): Record<DiagSeverity, number> {
  const counts: Record<DiagSeverity, number> = { info: 0, warning: 0, error: 0, critical: 0 }
  for (const item of groupDiagnosticEvents(events)) {
    const timestamp = Date.parse(item.event.ts_utc) / 1000
    if (!Number.isFinite(timestamp) || timestamp < from || timestamp > to) continue
    const severity = (item.event.severity || 'info').toLowerCase()
    if (severity.includes('critical') || severity.includes('fatal')) counts.critical += 1
    else if (severity.includes('error')) counts.error += 1
    else if (severity.includes('warn')) counts.warning += 1
    else counts.info += 1
  }
  return counts
}