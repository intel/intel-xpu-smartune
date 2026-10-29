import dayjs from 'dayjs'
import type { DiagAlert, DiagEvent } from '../api/types'

// Mirrors ALERT_POLICIES in diagnostics/alerts.py: only these event types
// create alerts. Every policy fires immediately (for_seconds 0), so each event
// of these types is one detection of an alert.
//
// An alert is Open while someone may still need to act and Closed once it has
// ended. What ends it depends on the type: a recovery event, a quiet period,
// or (for one-off faults with no recovery signal) being acknowledged.
// closesWhen describes an open alert, closedBecause a closed one.
export const ALERT_POLICY_SUMMARIES = [
  { eventType: 'PLATFORM_KERNEL_PANIC', severity: 'critical', behavior: 'Kernel panic. A one-off fault with no recovery event: investigate, then acknowledge it to close it.', closesWhen: 'Closes when acknowledged', closedBecause: 'Acknowledged' },
  { eventType: 'RESOURCE_MEMORY_OOM_KILL', severity: 'critical', behavior: 'Kernel OOM kill. A one-off fault with no recovery event: investigate, then acknowledge it to close it.', closesWhen: 'Closes when acknowledged', closedBecause: 'Acknowledged' },
  { eventType: 'DEVICE_GPU_HANG', severity: 'critical', behavior: 'GPU hang. A one-off fault with no recovery event: investigate, then acknowledge it to close it.', closesWhen: 'Closes when acknowledged', closedBecause: 'Acknowledged' },
  { eventType: 'PLATFORM_SERVICE_CRASHED', severity: 'critical', behavior: 'Service crashed. Closes when the same service starts again (PLATFORM_SERVICE_STARTED).', closesWhen: 'Closes when the service starts again', closedBecause: 'Service started again' },
  { eventType: 'PLATFORM_SYSTEMD_RESTART_LOOP', severity: 'error', behavior: 'systemd restart loop. Closes after 120 seconds without a new detection; escalates to critical when detected 3 times in 5 minutes.', closesWhen: 'Closes after 2 min without recurrence', closedBecause: 'No recurrence for 2 min' },
  { eventType: 'CONTROL_CPU_LIMIT_FAILED', severity: 'error', behavior: 'Applying a CPU limit failed. Closes when the CPU limit recovers (CONTROL_CPU_LIMIT_RECOVERED).', closesWhen: 'Closes when the CPU limit recovers', closedBecause: 'CPU limit recovered' },
  { eventType: 'CONTROL_MEMORY_LIMIT_FAILED', severity: 'error', behavior: 'Applying a memory limit failed. Closes when the memory limit recovers (CONTROL_MEMORY_LIMIT_RECOVERED).', closesWhen: 'Closes when the memory limit recovers', closedBecause: 'Memory limit recovered' },
  { eventType: 'CONTROL_DISK_IO_LIMIT_FAILED', severity: 'error', behavior: 'Applying a disk I/O limit failed. Closes when the disk I/O limit recovers (CONTROL_DISK_IO_LIMIT_RECOVERED).', closesWhen: 'Closes when the disk I/O limit recovers', closedBecause: 'Disk I/O limit recovered' },
] as const

const ALERTABLE_EVENT_TYPES = new Set<string>(ALERT_POLICY_SUMMARIES.map((policy) => policy.eventType))

// One line on an alert's lifecycle: what will close it while open, what closed
// it once closed.
export function alertLifecycleText(alert: DiagAlert): string {
  const policy = ALERT_POLICY_SUMMARIES.find((item) => item.eventType === alert.event_type)
  if (alert.status === 'active') return policy?.closesWhen ?? 'Open'
  return policy?.closedBecause ?? 'Closed'
}

export function isAlertableEvent(event: DiagEvent): boolean {
  return ALERTABLE_EVENT_TYPES.has(event.event_type)
}

// Same as dedup_key_for() in diagnostics/alerts.py: event type plus the
// narrowest scope shared by fault and recovery events.
function alertDedupKeyForEvent(event: DiagEvent): string {
  const scope = event.protection_id || event.job_id || event.app_id || event.service || event.source || '-'
  return `${event.event_type}::${scope}`
}

// The alert occurrence an event fired into: same dedup key, and the event falls
// within [first_fired_at, resolved_at] (open-ended while still active).
export function alertOccurrenceForEvent(event: DiagEvent, alerts: DiagAlert[]): DiagAlert | null {
  if (!isAlertableEvent(event)) return null
  const key = alertDedupKeyForEvent(event)
  const timestamp = dayjs(event.ts_utc).valueOf()
  return alerts.find((alert) => (
    alert.dedup_key === key
    && dayjs(alert.first_fired_at).valueOf() <= timestamp
    && (!alert.resolved_at || timestamp <= dayjs(alert.resolved_at).valueOf())
  )) ?? null
}

export function isAlertSilenced(alert: DiagAlert): boolean {
  return !!alert.silenced_until && dayjs(alert.silenced_until).isAfter(dayjs())
}

// How an active alert is being handled. Ack and silence belong to the current
// occurrence (a new fire clears them), so this only describes "now".
export type AlertHandlingMark = 'needs_attention' | 'acknowledged' | 'silenced'

export function alertHandlingMark(alert: DiagAlert): AlertHandlingMark {
  if (isAlertSilenced(alert)) return 'silenced'
  return alert.acknowledged_at ? 'acknowledged' : 'needs_attention'
}
