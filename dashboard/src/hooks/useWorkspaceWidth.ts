import { createContext, useContext } from 'react'

/**
 * Width of the page workspace (the content area beside or below the
 * navigation), measured once in App.tsx and shared here; null until measured.
 * The same box the CSS `@container workspace` rules query.
 */
export const WorkspaceWidthContext = createContext<number | null>(null)

/**
 * Below this workspace width page content switches to its compact form. It
 * matches the widest phone viewport (767px less the 24px workspace padding), so
 * a phone behaves as before, and a tablet with the sider open, whose workspace
 * can be narrower than a phone's, gets the same treatment.
 */
export const COMPACT_WORKSPACE_WIDTH = 744

/** True while the page workspace is too narrow for the full desktop layout. */
export function useIsCompactWorkspace(): boolean {
  const width = useContext(WorkspaceWidthContext)
  return width !== null && width < COMPACT_WORKSPACE_WIDTH
}
