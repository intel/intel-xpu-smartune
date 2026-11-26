import dayjs from 'dayjs'
import { useEffect, useSyncExternalStore } from 'react'
import { api } from '../api/client'
import type { DiagAlert, DiagAlertPoliciesData, DiagAlertPolicy, DiagEvent } from '../api/types'

// Which event types create alerts, and what closes each one, come from the
// server (GET /diag/alert-policies: the built-in policies of
// diagnostics/alerts.py plus the custom rules whose alert is on). They are
// loaded once and shared; a component that renders alert text calls
// useAlertPolicies() so it re-renders when they arrive.
//
// An alert is Open while someone may still need to act and Closed once it has
// ended. What ends it depends on the type: a recovery event, a quiet period,
// or (for one-off faults with no recovery signal) being acknowledged.

export interface AlertPolicyState {
  policies: DiagAlertPolicy[]
  cooldownSeconds: number | null
  loaded: boolean
}

let policyState: AlertPolicyState = { policies: [], cooldownSeconds: null, loaded: false }
let policiesByType = new Map<string, DiagAlertPolicy>()
let loadingPolicies: Promise<void> | null = null
const policyListeners = new Set<() => void>()

function setAlertPolicies(data: DiagAlertPoliciesData) {
  policyState = { policies: data.policies ?? [], cooldownSeconds: data.cooldown_seconds ?? null, loaded: true }
  policiesByType = new Map(policyState.policies.map((policy) => [policy.event_type, policy]))
  policyListeners.forEach((listener) => listener())
}

/** Load the policies once; again after a failed load, or with `refresh` (a
 *  custom rule's alert setting changed). */
export function loadAlertPolicies(refresh = false): Promise<void> {
  if (loadingPolicies) return loadingPolicies
  if (policyState.loaded && !refresh) return Promise.resolve()
  loadingPolicies = api.getDiagAlertPolicies()
    .then(setAlertPolicies)
    .catch((error) => { console.error('Failed to load alert policies', error) })
    .finally(() => { loadingPolicies = null })
  return loadingPolicies
}

function subscribeAlertPolicies(listener: () => void) {
  policyListeners.add(listener)
  return () => { policyListeners.delete(listener) }
}

/** The alert policies, loaded on first use. */
export function useAlertPolicies(): AlertPolicyState {
  const state = useSyncExternalStore(subscribeAlertPolicies, () => policyState)
  useEffect(() => { void loadAlertPolicies() }, [])
  return state
}

const CUSTOM_RULE_PREFIX = 'CUSTOM_RULE::'

// Lifecycle text for alert types without a current policy (a custom rule
// deleted or with its alert turned off since), from the close_mode the backend
// reports for the alert.
const CLOSE_MODE_TEXT = {
  ack: { closesWhen: 'Closes when acknowledged', closedBecause: 'Acknowledged' },
  quiet: { closesWhen: 'Closes when the rule stops matching', closedBecause: 'Rule stopped matching' },
  recovery: { closesWhen: 'Closes on recovery', closedBecause: 'Recovered' },
} as const

// Closes that are not the policy's own close, recorded by the server.
const RESOLVED_REASON_TEXT: Partial<Record<NonNullable<DiagAlert['resolved_reason']>, string>> = {
  operator_closed: 'Closed manually',
  cleared_by_reboot: 'Cleared by reboot',
}

// The policy close a recorded reason stands for, for a custom rule's alert: the
// rule may have been edited or deleted since it closed.
const RESOLVED_REASON_CLOSE_MODE = {
  acknowledged: 'ack',
  expired: 'quiet',
  recovered: 'recovery',
} as const

// One line on an alert's lifecycle: what will close it while open, what closed
// it once closed.
export function alertLifecycleText(alert: DiagAlert): string {
  const policy = policiesByType.get(alert.event_type)
  if (alert.status === 'active') {
    if (policy) return policy.closes_when
    return alert.close_mode ? CLOSE_MODE_TEXT[alert.close_mode].closesWhen : 'Open'
  }
  const reason = alert.resolved_reason
  if (reason && RESOLVED_REASON_TEXT[reason]) return RESOLVED_REASON_TEXT[reason] as string
  if (policy?.source === 'builtin') return policy.closed_because
  const mode = (reason && reason in RESOLVED_REASON_CLOSE_MODE)
    ? RESOLVED_REASON_CLOSE_MODE[reason as keyof typeof RESOLVED_REASON_CLOSE_MODE]
    : alert.close_mode
  return mode ? CLOSE_MODE_TEXT[mode].closedBecause : 'Closed'
}

// Built-in types alert when they have a policy. A custom rule's event records
// the rule's alert mode at the time it matched, which is what counts for it.
export function isAlertableEvent(event: DiagEvent): boolean {
  if (event.event_type.startsWith(CUSTOM_RULE_PREFIX)) {
    const mode = event.attributes?.alert_mode
    return typeof mode === 'string' && mode !== 'off'
  }
  return policiesByType.has(event.event_type)
}

// Same as scope_for() / dedup_key_for() in diagnostics/alerts.py: event type
// plus the narrowest scope shared by fault and recovery events. attributes.subject
// is what a host-level fault is about (killed process, GPU, stuck task, device).
function alertDedupKeyForEvent(event: DiagEvent): string {
  const subject = event.attributes && typeof event.attributes.subject === 'string' && event.attributes.subject
    ? event.attributes.subject
    : null
  const scope = event.protection_id || event.job_id || event.app_id || event.service || subject || event.source || '-'
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
