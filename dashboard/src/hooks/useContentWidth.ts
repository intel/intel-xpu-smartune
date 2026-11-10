import { useEffect, useState } from 'react'

/**
 * Content-box width of the element given the returned callback ref, kept
 * current by a ResizeObserver; null until first measured. For layout that
 * depends on the room a component actually has, which the viewport does not
 * tell once the sider (open, collapsed, or hidden) takes its share.
 *
 * A callback ref, so an element that mounts after the first render (behind a
 * loading or error branch) is still observed.
 */
export function useContentWidth<T extends HTMLElement>(): [(element: T | null) => void, number | null] {
  const [element, setElement] = useState<T | null>(null)
  const [width, setWidth] = useState<number | null>(null)
  useEffect(() => {
    if (!element) return
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width))
    observer.observe(element)
    return () => observer.disconnect()
  }, [element])
  return [setElement, width]
}
