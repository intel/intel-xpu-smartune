import React, { createContext, useCallback, useContext, useMemo, useState } from 'react'

export interface GlobalConfigNotice {
  id: string
  title: string
  description: string
  scope: string
  updatedAt?: number
}

interface PublishNoticeInput {
  title: string
  description: string
  scope: string
  updatedAt?: number
}

interface GlobalConfigNoticesContextValue {
  notices: GlobalConfigNotice[]
  publishNotice: (notice: PublishNoticeInput) => void
  dismissNotice: (id: string) => void
}

const GlobalConfigNoticesContext = createContext<GlobalConfigNoticesContextValue | null>(null)

function makeNoticeId(notice: PublishNoticeInput): string {
  const tsPart = notice.updatedAt ? `${notice.updatedAt}` : 'none'
  return `${notice.scope}:${tsPart}:${notice.description}`
}

export function GlobalConfigNoticesProvider({ children }: { children: React.ReactNode }) {
  const [notices, setNotices] = useState<GlobalConfigNotice[]>([])

  const publishNotice = useCallback((notice: PublishNoticeInput) => {
    const id = makeNoticeId(notice)
    setNotices((prev) => {
      if (prev.some((item) => item.id === id)) return prev
      // Supersede any older notice from the same scope so repeated changes to
      // one setting (e.g. toggling passive control) show a single, current
      // banner instead of stacking one per change.
      return [{ id, ...notice }, ...prev.filter((item) => item.scope !== notice.scope)]
    })
  }, [])

  const dismissNotice = useCallback((id: string) => {
    setNotices((prev) => prev.filter((item) => item.id !== id))
  }, [])

  const value = useMemo(
    () => ({ notices, publishNotice, dismissNotice }),
    [notices, publishNotice, dismissNotice],
  )

  return (
    <GlobalConfigNoticesContext.Provider value={value}>
      {children}
    </GlobalConfigNoticesContext.Provider>
  )
}

export function useGlobalConfigNotices() {
  const context = useContext(GlobalConfigNoticesContext)
  if (!context) {
    throw new Error('useGlobalConfigNotices must be used within GlobalConfigNoticesProvider')
  }
  return context
}
