// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import { useEffect } from 'react'
import { sendHeartbeat, sendUiRelease } from '../api/client'

// How often each open tab renews its lease. Kept well below the server's
// heartbeat grace (default 90s, see utils/ui_lease.py) so a couple of missed
// beats — including background-tab timer throttling — never lapse the lease.
const HEARTBEAT_INTERVAL_MS = 15_000

const SESSION_ID_KEY = 'smartune_ui_session_id'

// One stable id per tab. sessionStorage is per-tab and survives reloads (so a
// refresh keeps the same lease), while a brand-new tab gets its own id — which
// is what lets the server distinguish "last tab closed" from "one of several".
function getSessionId(): string {
  let id = sessionStorage.getItem(SESSION_ID_KEY)
  if (!id) {
    id =
      typeof crypto !== 'undefined' && 'randomUUID' in crypto
        ? crypto.randomUUID()
        : `s-${Date.now()}-${Math.random().toString(36).slice(2)}`
    sessionStorage.setItem(SESSION_ID_KEY, id)
  }
  return id
}

/**
 * Hold an open-UI lease while `enabled` (i.e. while logged in): heartbeat on an
 * interval and release on tab close / logout. The packaged monitor uses these
 * leases to stop itself once the last dashboard UI is gone. No-op when disabled.
 */
export function useUiLease(enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return
    const sessionId = getSessionId()

    sendHeartbeat(sessionId)
    const timer = window.setInterval(() => sendHeartbeat(sessionId), HEARTBEAT_INTERVAL_MS)
    // pagehide (not beforeunload/unload) is the reliable "leaving" signal and is
    // bfcache-compatible; the server's short release grace covers a bfcache
    // restore, whose heartbeat resumes and re-takes the lease.
    const onPageHide = () => sendUiRelease(sessionId)
    window.addEventListener('pagehide', onPageHide)

    return () => {
      window.clearInterval(timer)
      window.removeEventListener('pagehide', onPageHide)
      // Logout (enabled -> false) or unmount: release promptly.
      sendUiRelease(sessionId)
    }
  }, [enabled])
}
