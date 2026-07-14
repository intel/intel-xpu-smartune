// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from 'react'

/**
 * Tracks whether the page is currently visible to the user via the Page
 * Visibility API. Returns false when the tab is backgrounded or the window is
 * minimized. Note: occlusion by another app window is NOT reported as hidden by
 * browsers, so a covered-but-not-minimized window still reads as visible.
 */
export function useDocumentVisible(): boolean {
  const [visible, setVisible] = useState(
    () => typeof document === 'undefined' || document.visibilityState === 'visible',
  )

  useEffect(() => {
    const onVisibility = () => setVisible(document.visibilityState === 'visible')
    document.addEventListener('visibilitychange', onVisibility)
    // Sync once on mount in case visibility changed before the listener attached.
    onVisibility()
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [])

  return visible
}
