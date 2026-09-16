import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useEventsStore } from '../eventsStore'
import { fetchEvents } from '../../lib/api'
import type { ClusterEvent, FeedEventsResponse } from '../../lib/types'

vi.mock('../../lib/api', () => ({ fetchEvents: vi.fn(), searchRecommend: vi.fn() }))

function event(id: number, day = '07'): ClusterEvent {
  return { id, ai_title: `event ${id}`, doc_count: 1, unique_source_count: 1,
    first_doc_at: `2026-09-${day}T08:00:00Z`, last_doc_at: null, platforms: [],
    cover_url: null, has_update: false, live_version: 1 }
}

function response(count: number, extra: Partial<FeedEventsResponse> = {}): FeedEventsResponse {
  return { enabled: true, events: [event(70)], next_cursor: 2, new_since_last_fetch: 0,
    total_available_within_30d: count, date_counts: { '2026-09-07': count }, ...extra }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => { resolve = res })
  return { promise, resolve }
}

beforeEach(() => {
  useEventsStore.getState().reset()
  vi.mocked(fetchEvents).mockReset()
})

afterEach(() => useEventsStore.getState().reset())

describe('unfiltered day totals', () => {
  it('keeps full-day totals across category requests and updates when returning to all', async () => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(response(100))
    await useEventsStore.getState().init()
    expect(useEventsStore.getState().allDateCounts).toEqual({ '2026-09-07': 100 })
    expect(useEventsStore.getState().events).toHaveLength(1)

    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    const filtering = useEventsStore.getState().setFilters({ categories: ['coding'] })
    expect(useEventsStore.getState().allDateCounts).toEqual({ '2026-09-07': 100 })
    pending.resolve(response(3, { scope_key: 'categories:coding' }))
    await filtering
    expect(useEventsStore.getState().dateCounts).toEqual({ '2026-09-07': 3 })
    expect(useEventsStore.getState().allDateCounts).toEqual({ '2026-09-07': 100 })

    vi.mocked(fetchEvents).mockResolvedValueOnce(response(110, { scope_key: 'all' }))
    await useEventsStore.getState().setFilters({ categories: [] })
    expect(useEventsStore.getState().allDateCounts).toEqual({ '2026-09-07': 110 })
    expect(fetchEvents).toHaveBeenCalledTimes(3)
  })

  it('leaves totals unknown when the first request is filtered, including after filtered refresh', async () => {
    useEventsStore.setState({ filters: { categories: ['coding'] } })
    vi.mocked(fetchEvents).mockResolvedValue(response(3))
    await useEventsStore.getState().init()
    await useEventsStore.getState().refresh()
    expect(useEventsStore.getState().allDateCounts).toEqual({})
    expect(useEventsStore.getState().allDateCounts['2026-09-07']).toBeUndefined()
    expect(fetchEvents).toHaveBeenCalledTimes(2)
  })

  it('preserves the all snapshot while a filtered category refreshes', async () => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(response(100))
    await useEventsStore.getState().init()
    vi.mocked(fetchEvents).mockResolvedValueOnce(response(3))
    await useEventsStore.getState().setFilters({ categories: ['coding'] })
    vi.mocked(fetchEvents).mockResolvedValueOnce(response(4))
    await useEventsStore.getState().refresh()
    expect(useEventsStore.getState().dateCounts).toEqual({ '2026-09-07': 4 })
    expect(useEventsStore.getState().allDateCounts).toEqual({ '2026-09-07': 100 })
  })

  it.each(['refresh', 'backToLatest'] as const)('%s replaces the all snapshot with fresh all counts', async (action) => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(response(100))
    await useEventsStore.getState().init()
    vi.mocked(fetchEvents).mockResolvedValueOnce(response(110, { date_counts: { '2026-09-07': 110, '2026-09-06': 0 } }))
    await useEventsStore.getState()[action]()
    expect(useEventsStore.getState().allDateCounts).toEqual({ '2026-09-07': 110, '2026-09-06': 0 })
    expect(useEventsStore.getState().allDateCounts['2026-09-05']).toBeUndefined()
  })

  it.each([{ categories: [] }, { categories: ['coding'] }])('date seek accepts totals only for an unfiltered request: %j', async ({ categories }) => {
    useEventsStore.setState({ filters: { categories }, allDateCounts: { '2026-09-07': 100 } })
    vi.mocked(fetchEvents).mockResolvedValueOnce(response(110, {
      events: [event(60, '06')], date_counts: { '2026-09-07': 110, '2026-09-06': 20 },
      date_seek: { requested_date: '2026-09-06', status: 'found', anchor_event_id: 60 },
    }))
    await useEventsStore.getState().seekToDate('2026-09-06')
    expect(useEventsStore.getState().allDateCounts).toEqual(categories.length
      ? { '2026-09-07': 100 }
      : { '2026-09-07': 110, '2026-09-06': 20 })
  })

  it('keeps counts locked during ordinary pagination', async () => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(response(100))
    await useEventsStore.getState().init()
    vi.mocked(fetchEvents).mockResolvedValueOnce(response(999, { events: [event(69)] }))
    await useEventsStore.getState().loadMore()
    expect(useEventsStore.getState().allDateCounts).toEqual({ '2026-09-07': 100 })
  })

  it('can fill missing all metadata from a trusted all continuation', async () => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(response(100, { date_counts: undefined }))
    await useEventsStore.getState().init()
    expect(useEventsStore.getState().allDateCounts).toEqual({})
    vi.mocked(fetchEvents).mockResolvedValueOnce(response(100, { events: [event(69)] }))
    await useEventsStore.getState().loadMore()
    expect(useEventsStore.getState().allDateCounts).toEqual({ '2026-09-07': 100 })
  })

  it.each(['init', 'refresh', 'setFilters'] as const)('%s does not accept recent-fetch fallback counts as full-day totals', async (action) => {
    useEventsStore.setState({ allDateCounts: { '2026-09-07': 100 } })
    vi.mocked(fetchEvents)
      .mockResolvedValueOnce(response(0, { events: [], degraded: true }))
      .mockResolvedValueOnce(response(7))
    const state = useEventsStore.getState()
    if (action === 'setFilters') await state.setFilters({ categories: [] })
    else await state[action]()
    expect(useEventsStore.getState().dateCounts).toEqual({ '2026-09-07': 7 })
    expect(useEventsStore.getState().allDateCounts).toEqual({ '2026-09-07': 100 })
    expect(fetchEvents).toHaveBeenCalledTimes(2)
  })

  it.each([
    { date_counts: undefined },
    { degraded: true },
    { scope_key: 'categories:coding' },
  ])('does not replace known totals with unproven metadata: %j', async (extra) => {
    useEventsStore.setState({ allDateCounts: { '2026-09-07': 100 } })
    vi.mocked(fetchEvents).mockResolvedValueOnce(response(3, extra))
    await useEventsStore.getState().refresh()
    expect(useEventsStore.getState().allDateCounts).toEqual({ '2026-09-07': 100 })
  })

  it('ignores an older all response after a newer all refresh', async () => {
    const older = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(older.promise).mockResolvedValueOnce(response(110))
    const init = useEventsStore.getState().init()
    await useEventsStore.getState().refresh()
    older.resolve(response(100))
    await init
    expect(useEventsStore.getState().allDateCounts).toEqual({ '2026-09-07': 110 })
  })

  it('reset clears totals and prevents an outstanding response from restoring them', async () => {
    useEventsStore.setState({ allDateCounts: { '2026-09-07': 100 } })
    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    const refresh = useEventsStore.getState().refresh()
    useEventsStore.getState().reset()
    expect(useEventsStore.getState().allDateCounts).toEqual({})
    pending.resolve(response(110))
    await refresh
    expect(useEventsStore.getState().allDateCounts).toEqual({})
  })
})
