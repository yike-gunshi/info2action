import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, renderHook } from '@testing-library/react'
import { useBodyScrollLock } from '../useBodyScrollLock'

const html = document.documentElement

beforeEach(() => {
  vi.stubGlobal('innerWidth', 1440)
  vi.spyOn(html, 'clientWidth', 'get').mockReturnValue(1425)
})

afterEach(() => {
  cleanup()
  html.removeAttribute('style')
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('useBodyScrollLock', () => {
  it('leaves every inline style untouched while inactive', () => {
    html.style.cssText = 'overflow: scroll; padding-right: 7px; scrollbar-gutter: stable;'
    const original = html.style.cssText
    const { unmount } = renderHook(() => useBodyScrollLock(false, { compensateGutter: true }))
    expect(html.style.cssText).toBe(original)
    unmount()
    expect(html.style.cssText).toBe(original)
  })

  it('only sets overflow by default and removes it when the snapshot was empty', () => {
    const { unmount } = renderHook(() => useBodyScrollLock(true))
    expect(html.style.overflow).toBe('hidden')
    expect(html.style.paddingRight).toBe('')
    expect(html.style.scrollbarGutter).toBe('')
    unmount()
    expect(html.style.cssText).toBe('')
  })

  it.each([false, true])('restores the original values and priorities (compensateGutter=%s)', (compensateGutter) => {
    html.style.setProperty('overflow', 'scroll', 'important')
    html.style.setProperty('padding-right', '7px', 'important')
    html.style.setProperty('scrollbar-gutter', 'stable both-edges', 'important')
    const { unmount } = renderHook(() => useBodyScrollLock(true, { compensateGutter }))
    expect(html.style.overflow).toBe('hidden')
    expect(html.style.paddingRight).toBe(compensateGutter ? '15px' : '7px')
    expect(html.style.scrollbarGutter).toBe(compensateGutter ? 'auto' : 'stable both-edges')
    unmount()
    expect(html.style.overflow).toBe('scroll')
    expect(html.style.paddingRight).toBe('7px')
    expect(html.style.scrollbarGutter).toBe('stable both-edges')
    for (const property of ['overflow', 'padding-right', 'scrollbar-gutter']) {
      expect(html.style.getPropertyPriority(property)).toBe('important')
    }
  })

  it.each([0, -5])('uses zero compensation when the measured gutter is %spx', (gutter) => {
    vi.stubGlobal('innerWidth', 1425 + gutter)
    const { unmount } = renderHook(() => useBodyScrollLock(true, { compensateGutter: true }))
    expect(html.style.overflow).toBe('hidden')
    expect(html.style.paddingRight).toBe('0px')
    expect(html.style.scrollbarGutter).toBe('auto')
    unmount()
    expect(html.style.cssText).toBe('')
  })

  it.each([false, true])('keeps the first snapshot and lock until both layers close (compensateGutter=%s)', (compensateGutter) => {
    html.style.overflow = 'auto'
    html.style.paddingRight = '7px'
    html.style.scrollbarGutter = 'stable'
    const first = renderHook(() => useBodyScrollLock(true, { compensateGutter }))
    // A second lock must not take another snapshot or remeasure the gutter.
    vi.stubGlobal('innerWidth', 1500)
    const second = renderHook(() => useBodyScrollLock(true, { compensateGutter }))
    first.unmount()
    expect(html.style.overflow).toBe('hidden')
    expect(html.style.paddingRight).toBe(compensateGutter ? '15px' : '7px')
    expect(html.style.scrollbarGutter).toBe(compensateGutter ? 'auto' : 'stable')
    second.unmount()
    expect(html.style.overflow).toBe('auto')
    expect(html.style.paddingRight).toBe('7px')
    expect(html.style.scrollbarGutter).toBe('stable')
  })

  it.each([false, true])('does not add compensation to an existing plain lock (close first lock=%s)', (closeFirst) => {
    html.style.scrollbarGutter = 'stable'
    const plain = renderHook(() => useBodyScrollLock(true))
    const modal = renderHook(() => useBodyScrollLock(true, { compensateGutter: true }))
    expect(html.style.paddingRight).toBe('')
    expect(html.style.scrollbarGutter).toBe('stable')
    const firstToClose = closeFirst ? plain : modal
    const lastToClose = closeFirst ? modal : plain
    firstToClose.unmount()
    expect(html.style.overflow).toBe('hidden')
    expect(html.style.paddingRight).toBe('')
    expect(html.style.scrollbarGutter).toBe('stable')
    lastToClose.unmount()
    expect(html.style.overflow).toBe('')
    expect(html.style.paddingRight).toBe('')
    expect(html.style.scrollbarGutter).toBe('stable')

    const nextModal = renderHook(() => useBodyScrollLock(true, { compensateGutter: true }))
    expect(html.style.paddingRight).toBe('15px')
    expect(html.style.scrollbarGutter).toBe('auto')
    nextModal.unmount()
    expect(html.style.paddingRight).toBe('')
    expect(html.style.scrollbarGutter).toBe('stable')
  })

  it('keeps compensation when a plain lock outlives the first compensating lock', () => {
    html.style.scrollbarGutter = 'stable'
    const modal = renderHook(() => useBodyScrollLock(true, { compensateGutter: true }))
    const plain = renderHook(() => useBodyScrollLock(true))
    modal.unmount()
    expect(html.style.overflow).toBe('hidden')
    expect(html.style.paddingRight).toBe('15px')
    expect(html.style.scrollbarGutter).toBe('auto')
    plain.unmount()
    expect(html.style.overflow).toBe('')
    expect(html.style.paddingRight).toBe('')
    expect(html.style.scrollbarGutter).toBe('stable')
  })

  it('does not release an active lock when another caller becomes inactive', () => {
    const first = renderHook(({ active }) => useBodyScrollLock(active), { initialProps: { active: true } })
    const second = renderHook(() => useBodyScrollLock(true))
    first.rerender({ active: false })
    expect(html.style.overflow).toBe('hidden')
    second.unmount()
    expect(html.style.overflow).toBe('')
    first.unmount()
    expect(html.style.overflow).toBe('')
  })

  it('balances setup and cleanup during StrictMode effect replay', () => {
    html.style.scrollbarGutter = 'stable'
    const { unmount } = renderHook(() => useBodyScrollLock(true, { compensateGutter: true }), { wrapper: StrictMode })
    expect(html.style.overflow).toBe('hidden')
    expect(html.style.paddingRight).toBe('15px')
    expect(html.style.scrollbarGutter).toBe('auto')
    unmount()
    expect(html.style.overflow).toBe('')
    expect(html.style.paddingRight).toBe('')
    expect(html.style.scrollbarGutter).toBe('stable')
  })
})
