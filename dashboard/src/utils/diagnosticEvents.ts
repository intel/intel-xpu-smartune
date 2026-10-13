import type { DiagEvent, DiagSeverity } from '../api/types'

const LIVE_QUERY_LEAD_SECONDS = 5 * 60

export interface DiagnosticDisplayEvent {
  event: DiagEvent
  resources: string[]
}

export function createLiveDiagnosticWindow(from: number, to: number) {
  return { from, to, queryTo: to + LIVE_QUERY_LEAD_SECONDS }
}

function protectionAction(event: DiagEvent): string | null {
  if (event.category !== 'platform.control' || !event.protection_id) return null
  const match = event.event_type.match(/_(APPLIED|RECOVERED|FAILED)$/)
  return match?.[1] || null
}

export function groupDiagnosticEvents(events: DiagEvent[]): DiagnosticDisplayEvent[] {
  const grouped = new Map<string, DiagnosticDisplayEvent>()
  const ungrouped: DiagnosticDisplayEvent[] = []
  for (const event of events) {
    const action = protectionAction(event)
    if (!action) {
      ungrouped.push({ event, resources: event.resource_type ? [event.resource_type] : [] })
      continue
    }
    const key = `${event.protection_id}:${action}:${event.ts_utc.slice(0, 19)}`
    const existing = grouped.get(key)
    if (existing) {
      if (event.resource_type && !existing.resources.includes(event.resource_type)) existing.resources.push(event.resource_type)
    } else {
      grouped.set(key, { event, resources: event.resource_type ? [event.resource_type] : [] })
    }
  }
  return [...ungrouped, ...grouped.values()].sort((left, right) => right.event.ts_utc.localeCompare(left.event.ts_utc))
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