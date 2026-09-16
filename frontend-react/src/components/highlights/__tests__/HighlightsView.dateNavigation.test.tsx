import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { HighlightsView } from '../HighlightsView'
import { useEventsStore } from '../../../store/eventsStore'
import { useDailyDigestStore } from '../../../store/dailyDigestStore'
import { useUIStore } from '../../../store/uiStore'
import type { ClusterEvent } from '../../../lib/types'
import { readFileSync } from 'node:fs'

vi.mock('../../events/LatestEvents', () => ({
  LatestEvents: () => {
    const events = useEventsStore((s) => s.events)
    return <div data-testid="latest-events-page">{events.map((event) => (
      <section key={event.id} data-testid="event-date-group" data-highlight-date={event.first_doc_at!.slice(0, 10)}>
        <div data-testid="event-date-heading" tabIndex={-1}><span data-testid="event-date-label">{event.first_doc_at!.slice(0, 10)}</span></div>
        <article data-cluster-id={event.id}>{event.ai_title}</article>
      </section>
    ))}</div>
  },
}))

const initial = useEventsStore.getState()
const styles = readFileSync('src/globals.css', 'utf8')
const dates = ['2026-09-05', '2026-09-04', '2026-09-03']
const events = dates.map((date, index) => ({
  id: index + 1, ai_title: `新闻 ${date}`, first_doc_at: `${date}T06:00:00Z`,
}) as ClusterEvent)
let desktop = true
let reducedMotion = false
let deferSmoothScroll = false
let pageTops: Record<string, number>

function flushLayout() {
  act(() => { vi.advanceTimersByTime(120) })
}

describe('Highlights date navigation', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    desktop = true
    reducedMotion = false
    deferSmoothScroll = false
    pageTops = { '2026-09-05': 140, '2026-09-04': 1400, '2026-09-03': 2200 }
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({ matches: query.includes('prefers-reduced-motion') ? reducedMotion : desktop, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
    vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
    vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue([{}] as unknown as DOMRectList)
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      const top = this.dataset.highlightDate ? pageTops[this.dataset.highlightDate] - window.scrollY : 0
      const height = this.dataset.testid === 'highlights-filter-tabs' ? 92 : 44
      return { top, bottom: top + height, height, left: 0, right: 1040, width: 1040, x: 0, y: top, toJSON() {} }
    })
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 0, writable: true })
    Object.defineProperty(document.documentElement, 'scrollHeight', { configurable: true, value: 5000 })
    vi.spyOn(window, 'scrollTo').mockImplementation(((options: ScrollToOptions | number) => {
      if (typeof options === 'object' && !(deferSmoothScroll && options.behavior === 'smooth')) Object.defineProperty(window, 'scrollY', { configurable: true, value: options.top ?? 0, writable: true })
    }) as typeof window.scrollTo)
    useUIStore.setState({ l1: 'highlights' })
    useDailyDigestStore.setState({ loading: false })
    useEventsStore.setState({
      ...initial, enabled: true, events, dateCounts: Object.fromEntries(dates.map((date) => [date, 48])),
      dateAnchors: { '2026-09-05': 1, '2026-09-04': 2 },
      init: vi.fn().mockResolvedValue(undefined),
      seekToDate: vi.fn().mockResolvedValue(undefined),
      backToLatest: vi.fn().mockResolvedValue(undefined),
    })
  })

  afterEach(() => {
    cleanup()
    useEventsStore.setState(initial)
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('shows every browseable date without counts, including unloaded dates', () => {
    const allDates = Array.from({ length: 35 }, (_, i) => new Date(Date.UTC(2026, 8, 5 - i)).toISOString().slice(0, 10))
    useEventsStore.setState({ dateCounts: Object.fromEntries(allDates.map((date) => [date, 123])) })
    render(<HighlightsView />)
    const directory = screen.getByRole('navigation', { name: '精选日期导航' })
    expect(within(directory).getAllByRole('button')).toHaveLength(35)
    expect(within(directory).getByRole('button', { name: allDates[34] })).toBeInTheDocument()
    expect(directory).not.toHaveTextContent('123')
    expect(within(directory).getByRole('button', { name: '2026-09-05' }).querySelector('.highlights-date-text')).toHaveAttribute('aria-hidden', 'true')
    expect(screen.getByTestId('highlights-view-shell')).toHaveClass('xl:max-w-[1136px]')
  })

  it('hides only the highlights page scrollbar and restores it on another view or unmount', () => {
    const previousInlineStyle = document.documentElement.style.cssText
    const view = render(<HighlightsView />)
    expect(document.documentElement).toHaveClass('highlights-no-scrollbar')
    expect(document.documentElement.style.cssText).toBe(previousInlineStyle)
    act(() => useUIStore.setState({ l1: 'info' }))
    expect(document.documentElement).not.toHaveClass('highlights-no-scrollbar')
    act(() => useUIStore.setState({ l1: 'highlights' }))
    expect(document.documentElement).toHaveClass('highlights-no-scrollbar')
    view.unmount()
    expect(document.documentElement).not.toHaveClass('highlights-no-scrollbar')
  })

  it('lists only real positive-count dates from the archive start, without inventing missing days', () => {
    useEventsStore.setState({ dateCounts: {
      '2026-09-05': 3, '2026-09-04': 0, '2026-09-03': -1,
      '2026-09-02': NaN, '2026-09-01': Infinity, '2026-07-03': 2,
      '2026-07-02': 5, '2025-12-31': 1, '2026-07-32': 1, 'invalid': 1,
    } })
    render(<HighlightsView />)
    const buttons = within(screen.getByRole('navigation', { name: '精选日期导航' })).getAllByRole('button')
    expect(buttons.map((button) => button.getAttribute('aria-label'))).toEqual(['2026-09-05', '2026-07-03'])
  })

  it('keeps the year outside the scrollable date list even for a single year', () => {
    render(<HighlightsView />)
    const year = document.querySelector('.highlights-date-year')!
    expect(year).toHaveTextContent('2026')
    expect(year.closest('.highlights-date-list')).toBeNull()
    expect(styles).toMatch(/\.highlights-date-year\s*\{[^}]*font-size:\s*18px/)
    expect(styles).toMatch(/\.highlights-date-list\s*\{[^}]*scrollbar-width:\s*none/)
    expect(styles).toMatch(/\.highlights-date-list::-webkit-scrollbar\s*\{[^}]*display:\s*none/)
  })

  it('updates the fixed year when the date list crosses a year, including the compact menu', () => {
    desktop = false
    useEventsStore.setState({ dateCounts: { '2027-01-01': 1, '2026-12-31': 2 } })
    render(<HighlightsView />)
    fireEvent.click(screen.getByRole('button', { name: '选择日期' }))
    const list = screen.getByRole('navigation', { name: '精选日期导航' })
    const year = document.querySelector('.highlights-date-year')!
    expect(year).toHaveTextContent('2027')
    Object.defineProperty(list.firstElementChild, 'offsetHeight', { configurable: true, value: 44 })
    list.scrollTop = 48
    fireEvent.scroll(list)
    expect(year).toHaveTextContent('2026')
    list.scrollTop = 0
    fireEvent.scroll(list)
    expect(year).toHaveTextContent('2027')
    expect(year.closest('.highlights-date-list')).toBeNull()
  })

  it('extends the decorative timeline from the fixed year top to the date list without moving its layout', () => {
    const yearLine = styles.match(/\.highlights-date-year::before\s*\{([^}]+)\}/)?.[1] ?? ''
    expect(yearLine).toContain("content: ''")
    expect(yearLine).toContain('top: 0')
    expect(yearLine).toContain('left: 9.5px')
    expect(yearLine).toContain('bottom: calc(-4px - var(--date-list-offset, 0px))')
    expect(yearLine).toContain('width: 1px')
    expect(yearLine).toContain('background: var(--border)')
    expect(yearLine).toContain('pointer-events: none')
    expect(styles).toMatch(/\.highlights-date-year\s*\{[^}]*position:\s*relative/)
    expect(styles).toMatch(/\.highlights-date-rail \.highlights-date-list\s*\{[^}]*margin-top:\s*var\(--date-list-offset\)/)
  })

  it('does not alter the scrollbar while the mounted highlights view is hidden', () => {
    useUIStore.setState({ l1: 'actions' })
    render(<HighlightsView />)
    expect(document.documentElement).not.toHaveClass('highlights-no-scrollbar')
  })

  it('defines scrollbar appearance without disabling scrolling and sizes desktop dates to fourteen rows', () => {
    const scrollbarRules = styles.match(/html\.highlights-no-scrollbar[^}]+}/g)?.join('') ?? ''
    expect(scrollbarRules).toContain('scrollbar-width: none')
    expect(scrollbarRules).toContain('scrollbar-gutter: auto')
    expect(scrollbarRules).not.toMatch(/overflow(?:-y)?:\s*hidden/)
    expect(styles).toMatch(/\.highlights-date-rail \.highlights-date-row\s*\{[^}]*height:\s*var\(--date-row-height\)[^}]*min-height:\s*44px/)
  })

  it('keeps the rail anchor in normal flow when the date heading sticks and the reading prelude changes', () => {
    let groupTop = 124
    vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(function (this: HTMLElement) {
      const headingTop = Math.max(92, groupTop - window.scrollY)
      const top = this.dataset.testid === 'event-date-heading' ? headingTop
        : this.dataset.testid === 'event-date-label' ? headingTop + 4
          : this.dataset.testid === 'event-date-group' ? groupTop - window.scrollY : 52 - window.scrollY
      const height = this.dataset.testid === 'event-date-label' ? 22 : 44
      return { top, bottom: top + height, height, left: 0, right: 1040, width: 1040, x: 0, y: top, toJSON() {} }
    })
    render(<HighlightsView />)
    const shell = screen.getByTestId('highlights-view-shell')
    expect(shell.style.getPropertyValue('--highlights-first-group-top')).toBe('72px')
    expect(shell.style.getPropertyValue('--highlights-label-center')).toBe('15px')
    expect(shell.style.getPropertyValue('--highlights-first-label-y')).toBe('139px')
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 200, writable: true })
    fireEvent.resize(window)
    flushLayout()
    expect(shell.style.getPropertyValue('--highlights-first-label-y')).toBe('139px')
    groupTop = 92
    fireEvent.resize(window)
    flushLayout()
    expect(shell.style.getPropertyValue('--highlights-first-group-top')).toBe('40px')
    expect(shell.style.getPropertyValue('--highlights-first-label-y')).toBe('107px')
  })

  it('always delegates the date to the store, even when a partial date is already in the DOM', () => {
    render(<HighlightsView />)
    fireEvent.click(screen.getByRole('button', { name: '2026-09-03' }))
    expect(useEventsStore.getState().seekToDate).toHaveBeenCalledWith('2026-09-03')
    expect(window.scrollTo).not.toHaveBeenCalled()
  })

  it('disables cached-category navigation during filtering but allows ordinary pagination to be interrupted', () => {
    useEventsStore.setState({ filtering: true, loading: true })
    render(<HighlightsView />)
    const date = screen.getByRole('button', { name: '2026-09-04' })
    expect(date).toBeDisabled()
    fireEvent.click(date)
    expect(useEventsStore.getState().seekToDate).not.toHaveBeenCalled()
    act(() => useEventsStore.setState({ filtering: false }))
    expect(date).toBeEnabled()
    fireEvent.click(date)
    expect(useEventsStore.getState().seekToDate).toHaveBeenCalledWith('2026-09-04')
  })

  it('uses normal group positions to maintain exactly one current date and freezes it during loading', () => {
    render(<HighlightsView />)
    flushLayout()
    expect(screen.getByRole('button', { name: '2026-09-05' })).toHaveAttribute('aria-current', 'date')
    act(() => {
      Object.defineProperty(window, 'scrollY', { configurable: true, value: 1350, writable: true })
      fireEvent.scroll(window)
    })
    flushLayout()
    expect(screen.getByRole('button', { name: '2026-09-04' })).toHaveAttribute('aria-current', 'date')
    expect(document.querySelectorAll('[aria-current="date"]')).toHaveLength(1)
    act(() => useEventsStore.setState({ navigation: { requestId: 10, kind: 'date', date: '2026-09-03', status: 'loading', anchorId: null, error: null } }))
    flushLayout()
    expect(screen.getByRole('button', { name: '2026-09-04' })).toHaveAttribute('aria-current', 'date')
    expect(screen.getByRole('button', { name: '2026-09-03' })).toBeDisabled()
  })

  it('positions the ready day head below the sticky row before releasing navigation', () => {
    render(<HighlightsView />)
    act(() => useEventsStore.setState({ navigation: { requestId: 12, kind: 'date', date: '2026-09-04', status: 'ready', anchorId: 2, error: null } }))
    expect(useEventsStore.getState().navigation?.status).toBe('ready')
    flushLayout()
    expect(window.scrollTo).toHaveBeenCalledWith({ top: 1300, behavior: 'smooth' })
    expect(useEventsStore.getState().navigation).toBeNull()
    expect(screen.getByRole('button', { name: '2026-09-04' })).toHaveAttribute('aria-current', 'date')
  })

  it.each([0.1015625, 2])('marks the selected day active when scrolling settles %spx below the reading line', (offset) => {
    vi.mocked(window.scrollTo).mockImplementation(((options: ScrollToOptions | number) => {
      if (typeof options === 'object') Object.defineProperty(window, 'scrollY', { configurable: true, value: (options.top ?? 0) - offset, writable: true })
    }) as typeof window.scrollTo)
    render(<HighlightsView />)
    act(() => useEventsStore.setState({ navigation: { requestId: 39, kind: 'date', date: '2026-09-03', status: 'ready', anchorId: 3, error: null } }))
    flushLayout()
    expect(useEventsStore.getState().navigation).toBeNull()
    expect(window.scrollTo).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: '2026-09-03' })).toHaveAttribute('aria-current', 'date')
    expect(document.querySelectorAll('[aria-current="date"]')).toHaveLength(1)
  })

  it('starts one smooth scroll and keeps navigation pending until the page reaches the anchor', () => {
    deferSmoothScroll = true
    render(<HighlightsView />)
    act(() => useEventsStore.setState({ navigation: { requestId: 30, kind: 'date', date: '2026-09-04', status: 'ready', anchorId: 2, error: null } }))
    flushLayout()
    expect(window.scrollTo).toHaveBeenCalledTimes(1)
    expect(window.scrollTo).toHaveBeenCalledWith({ top: 1300, behavior: 'smooth' })
    expect(useEventsStore.getState().navigation?.status).toBe('ready')
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 500, writable: true })
    flushLayout()
    expect(window.scrollTo).toHaveBeenCalledTimes(1)
    expect(useEventsStore.getState().navigation?.status).toBe('ready')
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 1300, writable: true })
    flushLayout()
    expect(useEventsStore.getState().navigation).toBeNull()
    expect(window.scrollTo).toHaveBeenCalledTimes(1)
  })

  it.each(['wheel', 'touchmove', 'keydown', 'pointerdown', 'pagehide'])('stops an in-flight animation at the current page position on %s', (type) => {
    deferSmoothScroll = true
    render(<HighlightsView />)
    act(() => useEventsStore.setState({ navigation: { requestId: 31, kind: 'date', date: '2026-09-04', status: 'ready', anchorId: 2, error: null } }))
    flushLayout()
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 500, writable: true })
    fireEvent(window, type === 'keydown' ? new KeyboardEvent(type, { key: 'PageDown' }) : new Event(type))
    expect(useEventsStore.getState().navigation).toBeNull()
    expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 500, behavior: 'instant' })
    const calls = vi.mocked(window.scrollTo).mock.calls.length
    flushLayout()
    expect(window.scrollTo).toHaveBeenCalledTimes(calls)
  })

  it.each(['search', 'hidden'])('stops an in-flight animation when %s replaces the active reading context', (context) => {
    deferSmoothScroll = true
    render(<HighlightsView />)
    act(() => useEventsStore.setState({ navigation: { requestId: 32, kind: 'date', date: '2026-09-04', status: 'ready', anchorId: 2, error: null } }))
    flushLayout()
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 500, writable: true })
    act(() => {
      if (context === 'search') useEventsStore.setState({ searchQuery: '模型', searching: true })
      else useUIStore.setState({ l1: 'info' })
    })
    expect(useEventsStore.getState().navigation).toBeNull()
    expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 500, behavior: 'instant' })
  })

  it('cancels the previous animation before starting a newer date selection', () => {
    deferSmoothScroll = true
    render(<HighlightsView />)
    act(() => useEventsStore.setState({ navigation: { requestId: 33, kind: 'date', date: '2026-09-04', status: 'ready', anchorId: 2, error: null } }))
    flushLayout()
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 500, writable: true })
    act(() => useEventsStore.setState({ navigation: { requestId: 34, kind: 'date', date: '2026-09-03', status: 'ready', anchorId: 3, error: null } }))
    expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 500, behavior: 'instant' })
    flushLayout()
    expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 2100, behavior: 'smooth' })
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 2100, writable: true })
    flushLayout()
    expect(useEventsStore.getState().navigation).toBeNull()
    expect(window.scrollTo).toHaveBeenCalledTimes(3)
  })

  it('respects reduced motion and does not invent movement when already at the anchor', () => {
    reducedMotion = true
    render(<HighlightsView />)
    act(() => useEventsStore.setState({ navigation: { requestId: 35, kind: 'date', date: '2026-09-04', status: 'ready', anchorId: 2, error: null } }))
    flushLayout()
    expect(window.scrollTo).toHaveBeenCalledTimes(1)
    expect(window.scrollTo).toHaveBeenCalledWith({ top: 1300, behavior: 'instant' })
    vi.mocked(window.scrollTo).mockClear()
    reducedMotion = false
    act(() => useEventsStore.setState({ navigation: { requestId: 36, kind: 'date', date: '2026-09-04', status: 'ready', anchorId: 2, error: null } }))
    flushLayout()
    expect(window.scrollTo).not.toHaveBeenCalled()
    expect(useEventsStore.getState().navigation).toBeNull()
  })

  it('calibrates a shifted anchor after the single animation completes', () => {
    deferSmoothScroll = true
    render(<HighlightsView />)
    act(() => useEventsStore.setState({ navigation: { requestId: 37, kind: 'date', date: '2026-09-04', status: 'ready', anchorId: 2, error: null } }))
    flushLayout()
    pageTops['2026-09-04'] = 1600
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 1300, writable: true })
    flushLayout()
    expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 1500, behavior: 'instant' })
    expect(window.scrollTo).toHaveBeenCalledTimes(2)
    expect(useEventsStore.getState().navigation).toBeNull()
  })

  it('releases navigation when a shrinking document clamps the original animation destination', () => {
    vi.spyOn(performance, 'now').mockImplementation(() => Date.now())
    deferSmoothScroll = true
    render(<HighlightsView />)
    act(() => useEventsStore.setState({ navigation: { requestId: 38, kind: 'date', date: '2026-09-04', status: 'ready', anchorId: 2, error: null } }))
    flushLayout()
    Object.defineProperty(document.documentElement, 'scrollHeight', { configurable: true, value: window.innerHeight + 500 })
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 500, writable: true })
    act(() => { vi.advanceTimersByTime(2200) })
    expect(useEventsStore.getState().navigation).toBeNull()
    expect(window.scrollTo).toHaveBeenCalledTimes(1)
  })

  it('a newer ready request wins before an older positioning frame can run', () => {
    render(<HighlightsView />)
    act(() => useEventsStore.setState({ navigation: { requestId: 20, kind: 'date', date: '2026-09-04', status: 'ready', anchorId: 2, error: null } }))
    act(() => useEventsStore.setState({ navigation: { requestId: 21, kind: 'date', date: '2026-09-03', status: 'ready', anchorId: 3, error: null } }))
    flushLayout()
    expect(window.scrollTo).not.toHaveBeenCalledWith({ top: 1300, behavior: 'smooth' })
    expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 2100, behavior: 'smooth' })
    expect(useEventsStore.getState().navigation).toBeNull()
  })

  it('corrects a digest-induced layout shift before finishing, without continued snapping afterward', () => {
    render(<HighlightsView />)
    act(() => {
      useDailyDigestStore.setState({ loading: true })
      useEventsStore.setState({ navigation: { requestId: 22, kind: 'date', date: '2026-09-04', status: 'ready', anchorId: 2, error: null } })
    })
    flushLayout()
    expect(window.scrollTo).not.toHaveBeenCalled()
    pageTops['2026-09-04'] = 1600
    act(() => useDailyDigestStore.setState({ loading: false }))
    flushLayout()
    expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 1500, behavior: 'smooth' })
    expect(useEventsStore.getState().navigation).toBeNull()
    vi.mocked(window.scrollTo).mockClear()
    pageTops['2026-09-04'] = 1800
    fireEvent.resize(window)
    flushLayout()
    expect(window.scrollTo).not.toHaveBeenCalled()
  })

  it('keyboard selection moves focus to the real date heading only after positioning', () => {
    useEventsStore.setState({ seekToDate: vi.fn(async (date) => {
      useEventsStore.setState({ navigation: { requestId: 23, kind: 'date', date, status: 'ready', anchorId: 2, error: null } })
    }) })
    render(<HighlightsView />)
    fireEvent.click(screen.getByRole('button', { name: '2026-09-04' }), { detail: 0 })
    flushLayout()
    expect(document.querySelector('[data-highlight-date="2026-09-04"] [data-testid="event-date-heading"]')).toHaveFocus()
  })

  it('back-to-latest ready restores the top before releasing loading', () => {
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 1350, writable: true })
    render(<HighlightsView />)
    act(() => useEventsStore.setState({ navigation: { requestId: 24, kind: 'latest', date: null, status: 'ready', anchorId: null, error: null } }))
    flushLayout()
    expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 0, behavior: 'smooth' })
    expect(useEventsStore.getState().navigation).toBeNull()
    expect(screen.getByRole('button', { name: '2026-09-05' })).toHaveAttribute('aria-current', 'date')
  })

  it('waits for pending digest layout, and cancels on user scrolling without later snapping back', () => {
    render(<HighlightsView />)
    act(() => {
      useDailyDigestStore.setState({ loading: true })
      useEventsStore.setState({ navigation: { requestId: 13, kind: 'date', date: '2026-09-04', status: 'ready', anchorId: 2, error: null } })
    })
    flushLayout()
    expect(useEventsStore.getState().navigation?.status).toBe('ready')
    fireEvent.wheel(window, { deltaY: 100 })
    expect(useEventsStore.getState().navigation).toBeNull()
    vi.mocked(window.scrollTo).mockClear()
    act(() => useDailyDigestStore.setState({ loading: false }))
    flushLayout()
    expect(window.scrollTo).not.toHaveBeenCalled()
  })

  it('search and hidden highlights cancel pending navigation and cannot scroll a different page', () => {
    render(<HighlightsView />)
    act(() => {
      useEventsStore.setState({ navigation: { requestId: 14, kind: 'date', date: '2026-09-04', status: 'loading', anchorId: null, error: null } })
      useUIStore.setState({ l1: 'info' })
    })
    flushLayout()
    expect(useEventsStore.getState().navigation).toBeNull()
    expect(window.scrollTo).not.toHaveBeenCalled()
    act(() => {
      useUIStore.setState({ l1: 'highlights' })
      useEventsStore.setState({ searchQuery: '模型', searching: true })
    })
    expect(screen.getByRole('button', { name: '2026-09-04' })).toBeDisabled()
    expect(screen.getByText('清除搜索后按日期浏览')).toBeInTheDocument()
  })

  it('unmount cancels pending navigation so a late response cannot position a later visit', () => {
    const view = render(<HighlightsView />)
    act(() => useEventsStore.setState({ navigation: { requestId: 25, kind: 'date', date: '2026-09-04', status: 'loading', anchorId: null, error: null } }))
    view.unmount()
    expect(useEventsStore.getState().navigation).toBeNull()
    flushLayout()
    expect(window.scrollTo).not.toHaveBeenCalled()
  })

  it('pagehide releases pending navigation before a page is cached or closed', () => {
    render(<HighlightsView />)
    act(() => useEventsStore.setState({ navigation: { requestId: 26, kind: 'date', date: '2026-09-04', status: 'loading', anchorId: null, error: null } }))
    fireEvent(window, new Event('pagehide'))
    expect(useEventsStore.getState().navigation).toBeNull()
  })

  it('directory wheel gestures cannot reach the page refresh or scroll handler', () => {
    render(<HighlightsView />)
    const pageWheel = vi.fn()
    window.addEventListener('wheel', pageWheel)
    const event = new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: -100 })
    screen.getByRole('navigation', { name: '精选日期导航' }).dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)
    expect(pageWheel).not.toHaveBeenCalled()
    expect(window.scrollY).toBe(0)
    window.removeEventListener('wheel', pageWheel)
  })

  it('empty dates and date metadata failure have distinct recoverable states', () => {
    useEventsStore.setState({ dateCounts: {}, events: [] })
    render(<HighlightsView />)
    expect(screen.getByText('暂无日期')).toBeInTheDocument()
    act(() => useEventsStore.setState({ error: '精选加载失败' }))
    expect(screen.queryByText('暂无日期')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(useEventsStore.getState().init).toHaveBeenCalled()
  })

  it('keeps news readable after failure, supports retry, and provides one real back-to-latest action', () => {
    useEventsStore.setState({ timelineStartDate: '2026-09-04', navigation: { requestId: 15, kind: 'date', date: '2026-09-03', status: 'error', anchorId: null, error: '该日期暂无可浏览内容' } })
    render(<HighlightsView />)
    expect(screen.getByText('新闻 2026-09-05')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(useEventsStore.getState().seekToDate).toHaveBeenCalledWith('2026-09-03')
    fireEvent.click(screen.getByRole('button', { name: '回到最新' }))
    expect(useEventsStore.getState().backToLatest).toHaveBeenCalledTimes(1)
  })

  it('opens a narrow-screen date list, closes with Escape, and returns focus to its trigger', () => {
    desktop = false
    render(<HighlightsView />)
    expect(screen.queryByRole('navigation', { name: '精选日期导航' })).toBeNull()
    const trigger = screen.getByRole('button', { name: '选择日期' })
    fireEvent.click(trigger)
    expect(screen.getByRole('navigation', { name: '精选日期导航' })).toBeInTheDocument()
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    fireEvent.keyDown(screen.getByRole('button', { name: '2026-09-04' }), { key: 'Escape' })
    flushLayout()
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(trigger).toHaveFocus()
  })
})
