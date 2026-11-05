// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef } from 'react'
import { subscribe } from '../events/sseHub'

export interface AppStatusEvent {
  app_id: string
  app_name: string
  status: string
  purpose: 'app' | 'notify' | string
  message?: string
  /** 'pressure_level_changed' only: the levels as of this transition. */
  sys_level?: string
  disk_level?: string
  /** 'auto_limit_changed' only: which staged-restore step fired, for debugging. */
  detail?: string
}

type EventCallback = (event: AppStatusEvent) => void

/**
 * Subscribes to server-sent app status change events from /api/app/events.
 * Replaces polling – the server pushes updates whenever an app status changes.
 * Every subscriber in every tab shares one connection (see events/sseHub.ts).
 */
export function useAppEvents(onEvent: EventCallback, enabled = true) {
  const callbackRef = useRef(onEvent)

  useEffect(() => {
    callbackRef.current = onEvent
  }, [onEvent])

  useEffect(() => {
    if (!enabled) return
    return subscribe('app', (raw) => {
      try {
        const data: AppStatusEvent = JSON.parse(raw)
        // Forward any event that has a purpose field (app or notify).
        // Events without purpose (e.g. the initial {type:'connected'} heartbeat) are ignored.
        if (data.purpose) {
          callbackRef.current(data)
        }
      } catch {
        // ignore parse errors
      }
    })
  }, [enabled])
}
