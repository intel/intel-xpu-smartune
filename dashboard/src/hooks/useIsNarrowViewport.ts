import { Grid } from 'antd'

/**
 * True while the browser viewport is narrower than 768 CSS px (below the AntD
 * `md` breakpoint). Updates live on resize and rotation. It measures the
 * viewport, not a device type: a narrowed desktop window counts too.
 *
 * Use it only for what the viewport itself decides: the app shell (sider or
 * drawer navigation) and content in modals and drawers, which are sized by the
 * viewport. Content inside the page should follow the room it actually has,
 * via `useIsCompactWorkspace()`, since the sider takes a varying share.
 *
 * The CSS side of the same switch is `@media (max-width: 767px)`.
 *
 * `md` is undefined before the first measurement; only an explicit `false`
 * counts as narrow, so a desktop never flashes the narrow layout.
 */
export function useIsNarrowViewport(): boolean {
  return Grid.useBreakpoint().md === false
}
