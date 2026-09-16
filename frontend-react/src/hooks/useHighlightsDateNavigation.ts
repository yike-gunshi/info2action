import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { useEventsStore } from '../store/eventsStore'
import { useDailyDigestStore } from '../store/dailyDigestStore'
import { useUIStore } from '../store/uiStore'

const DATE_POSITION_TOLERANCE_PX = 2

function readingLine(root: HTMLElement) {
  const tabs = root.querySelector<HTMLElement>('[data-testid="highlights-filter-tabs"]')
  if (!tabs) return 8
  const rect = tabs.getBoundingClientRect()
  return Math.max(rect.bottom, (parseFloat(getComputedStyle(tabs).top) || 0) + rect.height) + 8
}

function visible(root: HTMLElement) {
  return document.visibilityState !== 'hidden' && root.getClientRects().length > 0
}

/** One page-level observer owns reading indication and pending date positioning. */
export function useHighlightsDateNavigation(root: RefObject<HTMLElement | null>) {
  const navigation = useEventsStore((s) => s.navigation)
  const contentKey = useEventsStore((s) => s.events.map((event) => event.id).join(','))
  const searchBlocked = useEventsStore((s) => Boolean(s.searchQuery.trim() || s.searching))
  const highlightsVisible = useUIStore((s) => s.l1 === 'highlights')
  const [activeDate, setActiveDate] = useState<string | null>(null)
  const focusTarget = useRef(false)

  const updateActiveDate = useCallback(() => {
    const container = root.current
    const state = useEventsStore.getState()
    if (!container || !visible(container) || useUIStore.getState().l1 !== 'highlights') return
    if (state.searchQuery.trim() || state.searching) { setActiveDate(null); return }
    if (state.navigation && state.navigation.status !== 'error') return
    const groups = [...container.querySelectorAll<HTMLElement>('[data-highlight-date]')]
    const line = readingLine(container) + DATE_POSITION_TOLERANCE_PX
    const positions = groups.map((group) => ({ date: group.dataset.highlightDate!, rect: group.getBoundingClientRect() }))
    let current = positions.find((item) => item.rect.bottom > line && item.rect.top < window.innerHeight)
    for (const item of positions) {
      if (item.rect.top <= line) current = item
      else break
    }
    const atBottom = window.scrollY > 0 && window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2
    if (atBottom) {
      const inView = positions.filter((item) => item.rect.bottom > line && item.rect.top < window.innerHeight)
      current = inView[inView.length - 1] ?? current
    }
    setActiveDate(current?.date ?? null)
  }, [root])

  useEffect(() => {
    let frame = 0
    const schedule = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(updateActiveDate)
    }
    const cancelNavigation = () => {
      const pending = useEventsStore.getState().navigation
      if (pending) useEventsStore.getState().finishNavigation(pending.requestId)
    }
    const onUserIntent = (event: Event) => {
      if (event.target instanceof Element && event.target.closest('[data-date-navigation]')) return
      if (event instanceof KeyboardEvent && !['ArrowDown', 'ArrowUp', 'PageDown', 'PageUp', 'Home', 'End', ' '].includes(event.key)) return
      cancelNavigation()
    }
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') cancelNavigation()
      else schedule()
    }
    if (!highlightsVisible || searchBlocked) cancelNavigation()
    schedule()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule)
    if (root.current) observer?.observe(root.current)
    window.addEventListener('scroll', schedule, { passive: true })
    window.addEventListener('resize', schedule)
    window.addEventListener('wheel', onUserIntent, { passive: true })
    window.addEventListener('touchmove', onUserIntent, { passive: true })
    window.addEventListener('pointerdown', onUserIntent)
    window.addEventListener('keydown', onUserIntent)
    window.addEventListener('pagehide', cancelNavigation)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      cancelAnimationFrame(frame)
      cancelNavigation()
      observer?.disconnect()
      window.removeEventListener('scroll', schedule)
      window.removeEventListener('resize', schedule)
      window.removeEventListener('wheel', onUserIntent)
      window.removeEventListener('touchmove', onUserIntent)
      window.removeEventListener('pointerdown', onUserIntent)
      window.removeEventListener('keydown', onUserIntent)
      window.removeEventListener('pagehide', cancelNavigation)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [root, highlightsVisible, searchBlocked, updateActiveDate])

  useEffect(() => { updateActiveDate() }, [contentKey, navigation, updateActiveDate])

  useLayoutEffect(() => {
    if (!navigation || navigation.status !== 'ready') return
    const container = root.current
    const { requestId } = navigation
    const finish = () => useEventsStore.getState().finishNavigation(requestId)
    if (!container || !highlightsVisible || searchBlocked || !visible(container)) { finish(); return }
    let frame = 0
    let lastTop: number | null = null
    let stableFrames = 0
    let stopped = false
    let started = false
    let animatedTop: number | null = null
    let animationDeadline = 0
    const schedule = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(position)
    }
    const position = () => {
      if (stopped || useEventsStore.getState().navigation?.requestId !== requestId) return
      if (!visible(container) || useUIStore.getState().l1 !== 'highlights') { finish(); return }
      const group = navigation.kind === 'latest'
        ? container.querySelector<HTMLElement>('[data-highlight-date]')
        : container.querySelector<HTMLElement>(`[data-highlight-date="${navigation.date}"]`)
      // A DOM date alone is insufficient: the ready contract must name a real day-head card.
      if (!group || (navigation.kind === 'date' && !group.querySelector(`[data-cluster-id="${navigation.anchorId}"]`))) { finish(); return }
      const desired = navigation.kind === 'latest' ? 0 : group.getBoundingClientRect().top + window.scrollY - readingLine(container)
      const top = Math.max(0, Math.min(desired, document.documentElement.scrollHeight - window.innerHeight))
      const stableTarget = lastTop !== null && Math.abs(top - lastTop) <= 1
      lastTop = top
      if (!started) {
        if (useDailyDigestStore.getState().loading) return
        if (!stableTarget) { schedule(); return }
        started = true
        if (Math.abs(window.scrollY - top) > DATE_POSITION_TOLERANCE_PX && !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
          animatedTop = top
          animationDeadline = performance.now() + 2000
          window.scrollTo({ top, behavior: 'smooth' })
          schedule()
          return
        }
      }
      if (animatedTop !== null) {
        // Observe one native animation; a shortened document may clamp its original target.
        if (Math.abs(window.scrollY - animatedTop) > DATE_POSITION_TOLERANCE_PX && performance.now() < animationDeadline) { schedule(); return }
        animatedTop = null
      }
      const settled = stableTarget && Math.abs(window.scrollY - top) <= DATE_POSITION_TOLERANCE_PX
      stableFrames = settled ? stableFrames + 1 : 0
      if (Math.abs(window.scrollY - top) > DATE_POSITION_TOLERANCE_PX) window.scrollTo({ top, behavior: 'instant' })
      if (stableFrames < 2) { schedule(); return }
      // Digest insertion can move a loaded older day. Resume on its actual result / resize.
      if (useDailyDigestStore.getState().loading) return
      if (focusTarget.current) group.querySelector<HTMLElement>('[data-testid="event-date-heading"]')?.focus({ preventScroll: true })
      focusTarget.current = false
      finish()
      updateActiveDate()
    }
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule)
    observer?.observe(container)
    const unsubscribe = useDailyDigestStore.subscribe(schedule)
    schedule()
    return () => {
      stopped = true
      cancelAnimationFrame(frame)
      observer?.disconnect()
      unsubscribe()
      if (animatedTop !== null) window.scrollTo({ top: window.scrollY, behavior: 'instant' })
    }
  }, [navigation, root, highlightsVisible, searchBlocked, updateActiveDate])

  const selectDate = useCallback((date: string, keyboard = false) => {
    focusTarget.current = keyboard
    void useEventsStore.getState().seekToDate(date)
  }, [])
  const backToLatest = useCallback((keyboard = false) => {
    focusTarget.current = keyboard
    void useEventsStore.getState().backToLatest()
  }, [])

  return { activeDate, selectDate, backToLatest }
}
