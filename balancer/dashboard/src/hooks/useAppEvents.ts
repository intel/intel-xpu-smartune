import { useEffect, useRef } from 'react'

export interface AppStatusEvent {
  app_id: string
  app_name: string
  status: string
  purpose: 'app' | 'notify' | string
  message?: string
}

type EventCallback = (event: AppStatusEvent) => void

/**
 * Subscribes to server-sent app status change events from /api/app/events.
 * Replaces polling – the server pushes updates whenever an app status changes.
 */
export function useAppEvents(onEvent: EventCallback, enabled = true) {
  const callbackRef = useRef(onEvent)

  useEffect(() => {
    callbackRef.current = onEvent
  }, [onEvent])

  useEffect(() => {
    if (!enabled) return

    let es: EventSource | null = null
    let retryTimeout: ReturnType<typeof setTimeout> | null = null
    let closed = false

    function connect() {
      if (closed) return
      es = new EventSource('/api/app/events')

      es.onmessage = (e) => {
        try {
          const data: AppStatusEvent = JSON.parse(e.data)
          // Forward any event that has a purpose field (app or notify).
          // Events without purpose (e.g. the initial {type:'connected'} heartbeat) are ignored.
          if (data.purpose) {
            callbackRef.current(data)
          }
        } catch {
          // ignore parse errors (e.g. heartbeat comments)
        }
      }

      es.onerror = () => {
        es?.close()
        es = null
        if (!closed) {
          // Reconnect after 3 s
          retryTimeout = setTimeout(connect, 3000)
        }
      }
    }

    connect()

    return () => {
      closed = true
      if (retryTimeout) clearTimeout(retryTimeout)
      es?.close()
    }
  }, [enabled])
}
