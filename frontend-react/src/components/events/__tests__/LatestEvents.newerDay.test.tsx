import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { LatestEvents } from '../LatestEvents'
import { useEventsStore } from '../../../store/eventsStore'
import { useAuthStore } from '../../../store/authStore'
import { useUIStore } from '../../../store/uiStore'
import { useDailyDigestStore } from '../../../store/dailyDigestStore'
import { fetchEvents, fetchDailyDigests } from '../../../lib/api'
import type { ClusterEvent, FeedEventsResponse } from '../../../lib/types'

vi.mock('../../../lib/api', () => ({
  fetchEvents: vi.fn(), searchRecommend: vi.fn(), fetchClusterStatuses: vi.fn(),
  fetchDailyDigests: vi.fn(),
}))
vi.mock('sonner', () => ({ toast: { info: vi.fn() } }))
vi.mock('../EventCard', () => ({ EventCard: ({ cluster }: { cluster: ClusterEvent }) => (
  <div data-testid="event-card" data-cluster-id={cluster.id}>{cluster.ai_title}</div>
) }))

function event(id: number, day = '10'): ClusterEvent {
  return { id, ai_title: `新闻 ${id}`, doc_count: 1, unique_source_count: 1,
    first_doc_at: `2026-09-${day}T08:00:00Z`, last_doc_at: null, platforms: [],
    cover_url: null, has_update: false, live_version: 1 }
}

function newerResponse(): FeedEventsResponse {
  return { enabled: true, events: [event(200, '11'), event(199, '11'), event(100)],
    next_cursor: null, date_counts: { '2026-09-15': 4, '2026-09-11': 2, '2026-09-10': 48 },
    date_seek: { requested_date: '2026-09-11', status: 'found', anchor_event_id: 200 },
    new_since_last_fetch: 0, total_available_within_30d: 54 }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

let extraLayoutHeight: number
let resizeCallbacks: Set<() => void>

beforeEach(() => {
  vi.useFakeTimers()
  extraLayoutHeight = 0
  resizeCallbacks = new Set()
  vi.mocked(fetchEvents).mockReset()
  vi.mocked(fetchDailyDigests).mockReset().mockResolvedValue({ digests: [] })
  useEventsStore.getState().reset()
  useDailyDigestStore.getState().reset()
  useAuthStore.setState({ user: null, isChecked: true, isLoading: false })
  useUIStore.setState({ l1: 'highlights' })
  useEventsStore.setState({ enabled: true, events: [event(100), event(99)],
    dateCounts: { '2026-09-15': 4, '2026-09-11': 2, '2026-09-10': 48 },
    dateAnchors: { '2026-09-10': 100 }, timelineStartDate: '2026-09-10', cursor: 5 })
  Object.defineProperty(window, 'scrollY', { configurable: true, writable: true, value: 0 })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 })
  Object.defineProperty(document.documentElement, 'scrollHeight', { configurable: true, value: 5000 })
  document.documentElement.style.overflow = ''
  vi.spyOn(window, 'scrollTo').mockImplementation(((options: ScrollToOptions) => {
    if (typeof options === 'object') window.scrollY = options.top ?? window.scrollY
  }) as typeof window.scrollTo)
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue([{}] as unknown as DOMRectList)
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    const index = useEventsStore.getState().events.findIndex((item) => item.id === Number(this.dataset.clusterId))
    const top = this.dataset.clusterId ? 160 + index * 200 + extraLayoutHeight - window.scrollY : 0
    return { top, bottom: top + 200, height: 200, left: 0, right: 1000, width: 1000, x: 0, y: top, toJSON() {} }
  })
  vi.stubGlobal('ResizeObserver', class {
    callback: () => void
    constructor(callback: () => void) { this.callback = callback }
    observe() { resizeCallbacks.add(this.callback) }
    unobserve() {}
    disconnect() { resizeCallbacks.delete(this.callback) }
  })
})

afterEach(() => {
  cleanup()
  useEventsStore.getState().reset()
  useDailyDigestStore.getState().reset()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

async function mount() {
  await act(async () => { render(<LatestEvents variant="page" />) })
}

describe('historical upward refresh', () => {
  it('wheel refresh requests the adjacent newer day, retains old cards and preserves the visible card offset', async () => {
    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    await mount()
    const old = document.querySelector<HTMLElement>('[data-cluster-id="100"]')!
    const before = old.getBoundingClientRect().top
    fireEvent.wheel(window, { deltaY: -100 })
    expect(fetchEvents).toHaveBeenCalledWith(expect.objectContaining({ targetDate: '2026-09-11', limit: 100 }))
    expect(screen.getByText('新闻 100')).toBeInTheDocument()
    expect(screen.getByText('正在加载 9月11日…')).toBeInTheDocument()
    expect(screen.getByTestId('highlights-newer-feedback')).toHaveClass('highlights-newer-feedback')
    expect(screen.getByTestId('highlights-newer-status').querySelector('svg')).toHaveClass('motion-reduce:animate-none')
    await act(async () => pending.resolve(newerResponse()))
    expect(screen.getByText('新闻 99')).toBeInTheDocument()
    expect(old.getBoundingClientRect().top).toBe(before)
    expect(window.scrollTo).toHaveBeenCalledWith({ top: 400, behavior: 'instant' })
    expect(document.activeElement).toBe(document.body)
  })

  it('preserves the user current scroll position when they move while the request is pending', async () => {
    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    await mount()
    fireEvent.wheel(window, { deltaY: -100 })
    window.scrollY = 120
    fireEvent.scroll(window)
    const currentTop = document.querySelector<HTMLElement>('[data-cluster-id="100"]')!.getBoundingClientRect().top
    await act(async () => pending.resolve(newerResponse()))
    expect(window.scrollY).toBe(520)
    expect(document.querySelector<HTMLElement>('[data-cluster-id="100"]')!.getBoundingClientRect().top).toBe(currentTop)
  })

  it('touch pull at the page top requests the adjacent day instead of page one', async () => {
    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    await mount()
    fireEvent.touchStart(window, { touches: [{ clientY: 100 }] })
    fireEvent.touchEnd(window, { changedTouches: [{ clientY: 190 }] })
    expect(fetchEvents).toHaveBeenCalledWith(expect.objectContaining({ targetDate: '2026-09-11' }))
    await act(async () => pending.resolve(newerResponse()))
    expect(useEventsStore.getState().events.map(({ id }) => id)).toEqual([200, 199, 100, 99])
  })

  it('shows a retry button on failure without changing content or scrolling to latest', async () => {
    vi.mocked(fetchEvents).mockRejectedValueOnce(new Error('网络暂时不可用'))
    await mount()
    await act(async () => fireEvent.wheel(window, { deltaY: -100 }))
    expect(screen.getByRole('alert')).toHaveTextContent('网络暂时不可用')
    expect(screen.getByText('新闻 100')).toBeInTheDocument()
    expect(screen.getByText('新闻 99')).toBeInTheDocument()
    expect(window.scrollTo).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: '重试加载 9月11日' })).toHaveClass('min-h-11')
    vi.mocked(fetchEvents).mockResolvedValueOnce(newerResponse())
    await act(async () => fireEvent.click(screen.getByRole('button', { name: '重试加载 9月11日' })))
    expect(fetchEvents).toHaveBeenCalledTimes(2)
    expect(useEventsStore.getState().events[0].id).toBe(200)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('keeps an instant success visible for 1500 ms without delaying the content or faking loading', async () => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(newerResponse())
    await mount()
    expect(screen.queryByTestId('highlights-newer-status')).toBeNull()
    await act(async () => fireEvent.wheel(window, { deltaY: -100 }))
    expect(screen.getByRole('status')).toHaveTextContent('已加载 9月11日 · 2 条，向上查看')
    expect(screen.queryByText('正在加载 9月11日…')).toBeNull()
    expect(screen.getByText('新闻 200')).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(1499))
    expect(screen.getByTestId('highlights-newer-status')).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(1))
    expect(screen.queryByTestId('highlights-newer-status')).toBeNull()
    expect(screen.getByText('新闻 200')).toBeInTheDocument()
  })

  it('retains a failed status until retry and replaces it with loading immediately', async () => {
    vi.mocked(fetchEvents).mockRejectedValueOnce(new Error('网络暂时不可用'))
    await mount()
    await act(async () => fireEvent.wheel(window, { deltaY: -100 }))
    act(() => vi.advanceTimersByTime(5000))
    expect(screen.getByRole('alert')).toHaveTextContent('9月11日加载失败')
    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    fireEvent.click(screen.getByRole('button', { name: '重试加载 9月11日' }))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByRole('status')).toHaveTextContent('正在加载 9月11日…')
    fireEvent.wheel(window, { deltaY: -100 })
    expect(fetchEvents).toHaveBeenCalledTimes(2)
    await act(async () => pending.resolve(newerResponse()))
  })

  it.each(['hidden_view', 'hidden_document', 'unmount'] as const)('clears completed feedback on %s', async (reason) => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(newerResponse())
    await mount()
    await act(async () => fireEvent.wheel(window, { deltaY: -100 }))
    expect(screen.getByRole('status')).toHaveTextContent('已加载')
    if (reason === 'hidden_view') act(() => useUIStore.setState({ l1: 'info' }))
    else if (reason === 'unmount') cleanup()
    else {
      vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
      fireEvent(document, new Event('visibilitychange'))
    }
    expect(useEventsStore.getState().newerFeedback).toBeNull()
    expect(screen.queryByTestId('highlights-newer-status')).toBeNull()
  })

  it('keeps normal refresh at the newest date and never offers an imaginary newer date', async () => {
    useEventsStore.setState({ dateCounts: { '2026-09-10': 48 }, timelineStartDate: null })
    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    await mount()
    fireEvent.wheel(window, { deltaY: -100 })
    expect(fetchEvents).toHaveBeenCalledWith({ page: 1, limit: 20, categories: [], timezoneOffsetMinutes: -480 })
    await act(async () => pending.resolve({ ...newerResponse(), events: [event(100), event(99)] }))
    expect(window.scrollTo).not.toHaveBeenCalled()
  })

  it.each(['directory', 'modal', 'away_from_top', 'search'] as const)('does not trigger a background refresh from %s', async (source) => {
    await mount()
    if (source === 'modal') document.documentElement.style.overflow = 'hidden'
    if (source === 'away_from_top') window.scrollY = 150
    if (source === 'search') act(() => useEventsStore.setState({ searchQuery: '模型', searching: true }))
    if (source === 'directory') {
      const rail = document.createElement('div')
      rail.setAttribute('data-date-navigation', '')
      document.body.append(rail)
      fireEvent.wheel(rail, { deltaY: -100 })
      rail.remove()
    } else fireEvent.wheel(window, { deltaY: -100 })
    expect(fetchEvents).not.toHaveBeenCalled()
  })

  it.each(['hidden_view', 'hidden_document', 'unmount'] as const)('cancels pending prepend and positioning on %s', async (reason) => {
    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    await mount()
    fireEvent.wheel(window, { deltaY: -100 })
    const old = useEventsStore.getState().events
    if (reason === 'hidden_view') act(() => useUIStore.setState({ l1: 'info' }))
    else if (reason === 'unmount') cleanup()
    else {
      vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
      fireEvent(document, new Event('visibilitychange'))
    }
    await act(async () => pending.resolve(newerResponse()))
    expect(useEventsStore.getState().events).toBe(old)
    expect(useEventsStore.getState().loadingNewer).toBe(false)
    expect(window.scrollTo).not.toHaveBeenCalled()
  })

  it('compensates a late digest layout shift, then releases control on a new user wheel gesture', async () => {
    const pending = deferred<FeedEventsResponse>()
    const digest = deferred<{ digests: [] }>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    await mount()
    vi.mocked(fetchDailyDigests).mockReturnValueOnce(digest.promise)
    fireEvent.wheel(window, { deltaY: -100 })
    await act(async () => pending.resolve(newerResponse()))
    const before = document.querySelector<HTMLElement>('[data-cluster-id="100"]')!.getBoundingClientRect().top
    extraLayoutHeight = 80
    act(() => {
      resizeCallbacks.forEach((callback) => callback())
      vi.advanceTimersByTime(32)
    })
    expect(document.querySelector<HTMLElement>('[data-cluster-id="100"]')!.getBoundingClientRect().top).toBe(before)
    fireEvent.wheel(window, { deltaY: 80 })
    const calls = vi.mocked(window.scrollTo).mock.calls.length
    extraLayoutHeight = 120
    await act(async () => digest.resolve({ digests: [] }))
    act(() => { resizeCallbacks.forEach((callback) => callback()); vi.advanceTimersByTime(120) })
    expect(window.scrollTo).toHaveBeenCalledTimes(calls)
  })
})
