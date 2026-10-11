import { useEffect } from 'react'
import type { RefObject } from 'react'

const SCROLL_INTENT_MS = 300
const INTENT_EVENTS = ['wheel', 'touchstart', 'touchmove', 'keydown', 'pointerdown', 'click'] as const

let lastIntent = Number.NEGATIVE_INFINITY
let intentSubscriberCount = 0

function markScrollIntent() {
  lastIntent = performance.now()
}

function subscribeToScrollIntent() {
  if (intentSubscriberCount === 0) {
    INTENT_EVENTS.forEach((type) => {
      window.addEventListener(type, markScrollIntent, { capture: true, passive: true })
    })
  }
  intentSubscriberCount += 1

  return () => {
    intentSubscriberCount -= 1
    if (intentSubscriberCount > 0) return
    INTENT_EVENTS.forEach((type) => {
      window.removeEventListener(type, markScrollIntent, { capture: true })
    })
    lastIntent = Number.NEGATIVE_INFINITY
  }
}

function cssTimeMs(element: Element, property: string): number {
  const value = getComputedStyle(element).getPropertyValue(property).trim()
  const number = Number.parseFloat(value)
  if (!Number.isFinite(number)) return 0
  return value.endsWith('ms') ? number : number * 1000
}

/** Reveal a native thin scrollbar only when a scroll follows recent user input. */
export function useScrollbarReveal<T extends HTMLElement>(ref: RefObject<T>): void {
  useEffect(() => {
    const element = ref.current
    if (!element) return

    const unsubscribeFromScrollIntent = subscribeToScrollIntent()
    let timer: ReturnType<typeof setTimeout> | undefined
    let active = false

    const measure = () => {
      element.style.setProperty('--scrollbar-actual', `${element.offsetWidth - element.clientWidth}px`)
    }
    measure()

    const resizeObserver = typeof ResizeObserver === 'undefined'
      ? null
      : new ResizeObserver(measure)
    resizeObserver?.observe(element)

    const onScroll = () => {
      if (element.hasAttribute('data-scrollbar-programmatic')) return
      if (!active && performance.now() - lastIntent > SCROLL_INTENT_MS) return
      active = true
      element.dataset.scrollbar = 'on'
      clearTimeout(timer)
      timer = setTimeout(() => {
        active = false
        element.removeAttribute('data-scrollbar')
      }, cssTimeMs(element, '--scrollbar-hide-ms'))
    }

    element.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      element.removeEventListener('scroll', onScroll)
      clearTimeout(timer)
      resizeObserver?.disconnect()
      unsubscribeFromScrollIntent()
      element.removeAttribute('data-scrollbar')
      element.removeAttribute('data-scrollbar-programmatic')
      element.style.removeProperty('--scrollbar-actual')
    }
  }, [ref])
}
