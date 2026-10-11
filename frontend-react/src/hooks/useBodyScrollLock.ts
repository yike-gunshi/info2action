import { useEffect } from 'react'

interface ScrollLockSnapshot {
  property: string
  value: string
  priority: string
}

let scrollLockCount = 0
let scrollLockSnapshot: ScrollLockSnapshot[] | null = null

export function useBodyScrollLock(active: boolean, options?: { compensateGutter?: boolean; scrollbarGutter?: 'auto' }) {
  const compensateGutter = options?.compensateGutter ?? false
  const scrollbarGutter = options?.scrollbarGutter

  useEffect(() => {
    if (!active) return
    const html = document.documentElement
    if (scrollLockCount === 0) {
      scrollLockSnapshot = ['overflow', 'padding-right', 'scrollbar-gutter'].map((property) => ({
        property,
        value: html.style.getPropertyValue(property),
        priority: html.style.getPropertyPriority(property),
      }))
      const gutter = compensateGutter ? Math.max(0, window.innerWidth - html.clientWidth) : 0
      html.style.overflow = 'hidden'
      if (compensateGutter || scrollbarGutter === 'auto') {
        html.style.scrollbarGutter = 'auto'
      }
      if (compensateGutter) {
        html.style.paddingRight = `${gutter}px`
      }
    }
    // The first lock owns the styles until every lock closes. If AI search
    // already holds a non-compensating lock, a later modal must not add gutter
    // compensation and shift its fixed, full-width layout (the pre-merge behavior).
    scrollLockCount += 1
    return () => {
      scrollLockCount = Math.max(0, scrollLockCount - 1)
      if (scrollLockCount !== 0 || !scrollLockSnapshot) return
      for (const { property, value, priority } of scrollLockSnapshot) {
        if (value) html.style.setProperty(property, value, priority)
        else html.style.removeProperty(property)
      }
      scrollLockSnapshot = null
    }
  }, [active, compensateGutter, scrollbarGutter])
}
