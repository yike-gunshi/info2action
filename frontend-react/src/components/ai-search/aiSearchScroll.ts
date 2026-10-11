import { type FocusEvent } from 'react'

// The breakpoint is the site's lg boundary; lengths and animation timings come
// from the generated tokens or the browser's measured layout.
export const AI_SEARCH_DESKTOP_QUERY = '(min-width: 1024px)'

export function isDesktopAiSearch(): boolean {
  return window.matchMedia(AI_SEARCH_DESKTOP_QUERY).matches
}

export function cssTimeMs(element: Element, property: string): number {
  const value = getComputedStyle(element).getPropertyValue(property).trim()
  const number = Number.parseFloat(value)
  if (!Number.isFinite(number)) return 0
  return value.endsWith('ms') ? number : number * 1000
}

export function keepFocusVisible(event: FocusEvent<HTMLElement>): void {
  if (!isDesktopAiSearch() || !(event.target instanceof HTMLElement)) return
  const container = event.currentTarget
  const target = event.target
  const bounds = container.getBoundingClientRect()
  const rect = target.getBoundingClientRect()
  const style = getComputedStyle(target)
  const marginTop = Number.parseFloat(style.scrollMarginTop) || 0
  const marginBottom = Number.parseFloat(style.scrollMarginBottom) || 0
  const top = bounds.top + container.clientTop
  const bottom = top + container.clientHeight
  // A pointer click on an already-visible citation must not move the article.
  if (rect.top >= top && rect.bottom <= bottom) return
  if (rect.top < top) {
    container.scrollTop += rect.top - marginTop - top
  } else if (rect.bottom > bottom) {
    container.scrollTop += rect.bottom + marginBottom - bottom
  }
}

/** Run `done` once the given scroller has settled.
 *
 * Smooth scrolling is asynchronous, so firing the highlight on click flashed a
 * card that was still off-screen — by the time it arrived the pulse was over
 * (user feedback 2026-09-22). `scrollend` is the precise signal; the timeout is
 * the fallback for browsers without it and for the case where the scroll never
 * starts because the position is already correct.
 */
function afterScrollSettles(scroller: HTMLElement | Window, done: () => void): void {
  let finished = false
  const finish = () => {
    if (finished) return
    finished = true
    scroller.removeEventListener('scrollend', finish)
    clearTimeout(timer)
    done()
  }
  // 800ms comfortably covers a smooth scroll across this column; past that the
  // user has moved on and a late pulse would be noise rather than a signal.
  const timer = setTimeout(finish, 800)
  scroller.addEventListener('scrollend', finish, { once: true })
}

export function revealCitation(card: HTMLElement, onSettled?: () => void): void {
  const container = card.closest<HTMLElement>('[data-ai-scroll="cards"]')
  if (!container) { onSettled?.(); return }
  const rect = card.getBoundingClientRect()
  const behavior = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
  if (isDesktopAiSearch()) {
    const bounds = container.getBoundingClientRect()
    const top = bounds.top + container.clientTop
    // Always centre the card, even when it is already fully visible. Skipping the
    // scroll made the click look like nothing happened, so readers could not tell
    // the citation had been located (user feedback 2026-09-22).
    const target = container.scrollTop + rect.top - top - (container.clientHeight - rect.height) / 2
    container.scrollTo({ top: Math.max(0, Math.min(target, container.scrollHeight - container.clientHeight)), behavior })
    if (onSettled) afterScrollSettles(container, onSettled)
  } else {
    // Narrow screens deliberately use document scrolling, with room for the
    // site's sticky TopBar. Never scroll any ancestor via scrollIntoView.
    const topbar = document.querySelector<HTMLElement>('[data-testid="topbar"]')
    const top = topbar?.getBoundingClientRect().bottom ?? 0
    if (rect.top >= top && rect.bottom <= window.innerHeight) { onSettled?.(); return }
    window.scrollTo({ top: Math.max(0, window.scrollY + rect.top - top - (window.innerHeight - top - rect.height) / 2), behavior })
    if (onSettled) afterScrollSettles(window, onSettled)
  }
}
