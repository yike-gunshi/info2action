import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useRef } from 'react'
import type { CSSProperties } from 'react'
import { useScrollbarReveal } from '../useScrollbarReveal'

function ScrollHarness({ delay = '1000ms' }: { delay?: string }) {
  const ref = useRef<HTMLDivElement>(null)
  useScrollbarReveal(ref)
  return (
    <div
      ref={ref}
      data-testid="scroller"
      style={{ '--scrollbar-hide-ms': delay } as CSSProperties}
    />
  )
}

let now = 0

describe('useScrollbarReveal', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    now = 10_000
    vi.spyOn(performance, 'now').mockImplementation(() => now)
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it.each(['1000ms', '1s'])('reveals for a user-initiated scroll and hides after the configured pause %s', delay => {
    render(<ScrollHarness delay={delay} />)
    const scroller = screen.getByTestId('scroller')
    expect(scroller).not.toHaveAttribute('data-scrollbar')

    fireEvent.wheel(window)
    fireEvent.scroll(scroller)
    expect(scroller).toHaveAttribute('data-scrollbar', 'on')

    act(() => vi.advanceTimersByTime(999))
    expect(scroller).toHaveAttribute('data-scrollbar', 'on')
    act(() => vi.advanceTimersByTime(1))
    expect(scroller).not.toHaveAttribute('data-scrollbar')
  })

  it('does not reveal for a programmatic scroll without recent user input', () => {
    render(<ScrollHarness />)
    const scroller = screen.getByTestId('scroller')

    fireEvent.scroll(scroller)

    expect(scroller).not.toHaveAttribute('data-scrollbar')
  })

  it('cleans measured width, state, and timers on unmount', () => {
    const { unmount } = render(<ScrollHarness />)
    const scroller = screen.getByTestId('scroller')
    expect(scroller.style.getPropertyValue('--scrollbar-actual')).toBe('0px')

    fireEvent.wheel(window)
    fireEvent.scroll(scroller)
    expect(vi.getTimerCount()).toBe(1)
    expect(scroller).toHaveAttribute('data-scrollbar', 'on')
    scroller.setAttribute('data-scrollbar-programmatic', '')
    unmount()
    expect(vi.getTimerCount()).toBe(0)
    act(() => vi.runAllTimers())

    expect(scroller).not.toHaveAttribute('data-scrollbar')
    expect(scroller).not.toHaveAttribute('data-scrollbar-programmatic')
    expect(scroller.style.getPropertyValue('--scrollbar-actual')).toBe('')
    fireEvent.wheel(window)
    fireEvent.scroll(scroller)
    expect(scroller).not.toHaveAttribute('data-scrollbar')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('restarts the delay on repeated scrolling and reads the current CSS variable', () => {
    render(<ScrollHarness />)
    const scroller = screen.getByTestId('scroller')
    fireEvent.wheel(window)
    fireEvent.scroll(scroller)
    act(() => vi.advanceTimersByTime(600))
    now += 600
    scroller.style.setProperty('--scrollbar-hide-ms', '1500ms')
    // 平滑滚动、拖动滚动条的后续事件沿用本轮意图，超过 300ms 也无需新输入。
    fireEvent.scroll(scroller)
    act(() => vi.advanceTimersByTime(1499))
    expect(scroller).toHaveAttribute('data-scrollbar', 'on')
    act(() => vi.advanceTimersByTime(1))
    expect(scroller).not.toHaveAttribute('data-scrollbar')
  })

  it('ignores programmatic scrolls after user intent expires', () => {
    render(<ScrollHarness />)
    const scroller = screen.getByTestId('scroller')
    fireEvent.wheel(window)
    now += 301
    fireEvent.scroll(scroller)
    expect(scroller).not.toHaveAttribute('data-scrollbar')
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each(['touchStart', 'touchMove', 'keyDown', 'pointerDown', 'click'] as const)('treats %s as user intent', type => {
    render(<ScrollHarness />)
    const scroller = screen.getByTestId('scroller')
    now += 1000
    fireEvent[type](window)
    now += 100
    fireEvent.scroll(scroller)
    expect(scroller).toHaveAttribute('data-scrollbar', 'on')
  })

  it('does not reveal on hover', () => {
    render(<ScrollHarness />)
    const scroller = screen.getByTestId('scroller')
    fireEvent.mouseEnter(scroller)
    fireEvent.mouseOver(scroller)
    expect(scroller).not.toHaveAttribute('data-scrollbar')
  })

  it('a new round after hiding needs fresh input', () => {
    render(<ScrollHarness />)
    const scroller = screen.getByTestId('scroller')
    fireEvent.wheel(window)
    fireEvent.scroll(scroller)
    act(() => vi.advanceTimersByTime(1000))
    now += 1000
    expect(scroller).not.toHaveAttribute('data-scrollbar')
    fireEvent.scroll(scroller)
    expect(scroller).not.toHaveAttribute('data-scrollbar')
  })

  it('ignores explicitly marked programmatic scrolls even after recent user input', () => {
    render(<ScrollHarness />)
    const scroller = screen.getByTestId('scroller')
    fireEvent.click(window)
    scroller.setAttribute('data-scrollbar-programmatic', '')
    fireEvent.scroll(scroller)
    expect(scroller).not.toHaveAttribute('data-scrollbar')
    expect(vi.getTimerCount()).toBe(0)
    scroller.removeAttribute('data-scrollbar-programmatic')
    fireEvent.scroll(scroller)
    expect(scroller).toHaveAttribute('data-scrollbar', 'on')
  })

  it('records the width a classic scrollbar takes so CSS can give it back', () => {
    vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(716)
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(705)
    const { unmount } = render(<ScrollHarness />)
    const scroller = screen.getByTestId('scroller')
    expect(scroller.style.getPropertyValue('--scrollbar-actual')).toBe('11px')
    unmount()
    expect(scroller.style.getPropertyValue('--scrollbar-actual')).toBe('')
  })

  it('records zero for overlay scrollbars and clears the value on unmount', () => {
    vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(716)
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(716)
    const { unmount } = render(<ScrollHarness />)
    const scroller = screen.getByTestId('scroller')
    expect(scroller.style.getPropertyValue('--scrollbar-actual')).toBe('0px')
    unmount()
    expect(scroller.style.getPropertyValue('--scrollbar-actual')).toBe('')
  })
})
