import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useEventsStore } from '../eventsStore'
import { fetchEvents, searchRecommend } from '../../lib/api'
import type { ClusterEvent, FeedEventsResponse } from '../../lib/types'

vi.mock('../../lib/api', () => ({ fetchEvents: vi.fn(), searchRecommend: vi.fn() }))

function event(id: number, day = '07'): ClusterEvent {
  return { id, ai_title: `event ${id}`, doc_count: 1, unique_source_count: 1,
    first_doc_at: `2026-09-${day}T08:00:00Z`, last_doc_at: null, platforms: [],
    cover_url: null, has_update: false, live_version: 1 }
}

function response(events: ClusterEvent[], extra: Partial<FeedEventsResponse> = {}): FeedEventsResponse {
  return { enabled: true, events, next_cursor: 2, new_since_last_fetch: 0,
    total_available_within_30d: 50, date_counts: { '2026-09-07': 30, '2026-09-06': 20 }, ...extra }
}

function seekResponse(day = '06', id = 60): FeedEventsResponse {
  return response([event(70), event(id, day), event(id - 1, day)], {
    next_cursor: { version_id: 'version', scope_key: 'all', rank_after: 40 },
    date_seek: { requested_date: `2026-09-${day}`, status: 'found', anchor_event_id: id },
  })
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

beforeEach(() => {
  useEventsStore.getState().reset()
  vi.mocked(fetchEvents).mockReset()
  vi.mocked(searchRecommend).mockReset()
})

afterEach(() => {
  useEventsStore.getState().reset()
  vi.useRealTimers()
})

describe('date anchors and navigation', () => {
  it('proves first-page and contiguous page-boundary day heads in Beijing time', async () => {
    const midnight = { ...event(70), first_doc_at: '2026-09-06T16:00:00Z' }
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([midnight]))
    await useEventsStore.getState().init()
    expect(useEventsStore.getState().dateAnchors).toEqual({ '2026-09-07': 70 })
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(60, '06'), event(59, '06')]))
    await useEventsStore.getState().loadMore()
    expect(useEventsStore.getState().dateAnchors).toEqual({ '2026-09-07': 70, '2026-09-06': 60 })
  })

  it('locates a proven loaded day without fetching and blocks paging until matching completion', async () => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(70), event(60, '06')]))
    await useEventsStore.getState().init()
    const original = useEventsStore.getState().events
    vi.mocked(fetchEvents).mockClear()
    await useEventsStore.getState().seekToDate('2026-09-06')
    const nav = useEventsStore.getState().navigation!
    expect(nav).toMatchObject({ kind: 'date', date: '2026-09-06', status: 'ready', anchorId: 60 })
    expect(useEventsStore.getState().events).toBe(original)
    expect(useEventsStore.getState().timelineStartDate).toBeNull()
    await useEventsStore.getState().loadMore()
    useEventsStore.getState().finishNavigation(nav.requestId - 1)
    expect(useEventsStore.getState().navigation).toBe(nav)
    expect(fetchEvents).not.toHaveBeenCalled()
    useEventsStore.getState().finishNavigation(nav.requestId)
    expect(useEventsStore.getState().navigation).toBeNull()
  })

  it.each(['init', 'refresh', 'setFilters'] as const)('%s cannot prove day heads from the reduced recent-fetch fallback', async (action) => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([], { degraded: true }))
      .mockResolvedValueOnce(response([event(61, '06'), event(51, '05')]))
    const state = useEventsStore.getState()
    if (action === 'setFilters') await state.setFilters({ categories: ['coding'] })
    else await state[action]()
    expect(fetchEvents).toHaveBeenLastCalledWith(expect.objectContaining({ fetchedSince: expect.any(String) }))
    expect(useEventsStore.getState().dateAnchors).toEqual({})
    vi.mocked(fetchEvents).mockResolvedValueOnce(seekResponse())
    await useEventsStore.getState().seekToDate('2026-09-06')
    expect(fetchEvents).toHaveBeenLastCalledWith(expect.objectContaining({ targetDate: '2026-09-06' }))
    expect(useEventsStore.getState().events[0].id).toBe(60)
  })

  it('does not reuse a reduced fallback cache tail to prove a normal-page boundary', async () => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([], { degraded: true }))
      .mockResolvedValueOnce(response([event(61, '06')]))
    await useEventsStore.getState().setFilters({ categories: ['coding'] })
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(70)]))
    await useEventsStore.getState().setFilters({ categories: ['models'] })
    vi.mocked(fetchEvents).mockRejectedValueOnce(new Error('keep fallback cache'))
    await useEventsStore.getState().setFilters({ categories: ['coding'] })
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(51, '05')]))
    await useEventsStore.getState().loadMore()
    expect(useEventsStore.getState().dateAnchors).toEqual({})
  })

  it('does not mistake a continue-reading page start for a day head', async () => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(70)]))
    await useEventsStore.getState().init()
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(51, '05'), event(40, '04')]))
    await useEventsStore.getState().seekToCursor(8)
    expect(useEventsStore.getState().dateAnchors['2026-09-05']).toBeUndefined()
    expect(useEventsStore.getState().dateAnchors['2026-09-04']).toBe(40)
    vi.mocked(fetchEvents).mockResolvedValueOnce(seekResponse('05', 59))
    await useEventsStore.getState().seekToDate('2026-09-05')
    expect(fetchEvents).toHaveBeenLastCalledWith({ targetDate: '2026-09-05', limit: 20, categories: [], timezoneOffsetMinutes: -480 })
    expect(useEventsStore.getState().events[0].id).toBe(59)
  })

  it('replaces from the raw anchor, preserves its original cursor and does not fill the page', async () => {
    useEventsStore.setState({ enabled: true, events: [event(99)], filters: { categories: ['coding', 'models'] } })
    const res = seekResponse()
    // Same timestamps and ascending ids expose accidental client re-sorting.
    res.events[2] = event(61, '06')
    vi.mocked(fetchEvents).mockResolvedValueOnce(res)
    await useEventsStore.getState().seekToDate('2026-09-06')
    expect(fetchEvents).toHaveBeenCalledOnce()
    expect(fetchEvents).toHaveBeenCalledWith({ targetDate: '2026-09-06', limit: 20, categories: ['coding', 'models'], timezoneOffsetMinutes: -480 })
    expect(useEventsStore.getState()).toMatchObject({
      events: res.events.slice(1), cursor: res.next_cursor, dateCounts: res.date_counts,
      dateAnchors: { '2026-09-06': 60 }, timelineStartDate: '2026-09-06',
    })
    const nav = useEventsStore.getState().navigation!
    await useEventsStore.getState().loadMore()
    expect(fetchEvents).toHaveBeenCalledOnce()
    useEventsStore.getState().finishNavigation(nav.requestId)
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(50, '05')], { next_cursor: null, read_model_version_id: 'version', scope_key: 'all' }))
    await useEventsStore.getState().loadMore()
    expect(fetchEvents).toHaveBeenLastCalledWith({ page: res.next_cursor, limit: 20, categories: ['coding', 'models'], timezoneOffsetMinutes: -480 })
    expect(useEventsStore.getState().events.map((item) => item.id)).toEqual([60, 61, 50])
    expect(useEventsStore.getState().dateAnchors['2026-09-05']).toBe(50)
  })

  it.each(['not_found', 'mismatched_date', 'missing_metadata', 'missing_anchor', '503'])('keeps current content on %s and permits retry', async (failure) => {
    const original = [event(70)]
    useEventsStore.setState({ enabled: true, events: original, cursor: 3, dateCounts: { '2026-09-07': 30 } })
    const res = seekResponse()
    if (failure === 'not_found') res.date_seek = { requested_date: '2026-09-06', status: 'not_found', anchor_event_id: null }
    if (failure === 'mismatched_date') res.date_seek!.requested_date = '2026-09-05'
    if (failure === 'missing_metadata') delete res.date_seek
    if (failure === 'missing_anchor') res.events = [event(70)]
    if (failure === '503') vi.mocked(fetchEvents).mockRejectedValueOnce(Object.assign(new Error('暂时不可用'), { status: 503 }))
    else vi.mocked(fetchEvents).mockResolvedValueOnce(res)
    await useEventsStore.getState().seekToDate('2026-09-06')
    expect(useEventsStore.getState().events).toBe(original)
    expect(useEventsStore.getState().cursor).toBe(3)
    expect(useEventsStore.getState().navigation).toMatchObject({ status: 'error', date: '2026-09-06', error: expect.any(String) })
    vi.mocked(fetchEvents).mockResolvedValueOnce(seekResponse())
    await useEventsStore.getState().seekToDate('2026-09-06')
    expect(useEventsStore.getState().navigation?.status).toBe('ready')
  })

  it('keeps ordinary filter caches separate and clears old-scope anchors while filtering', async () => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(70)]))
    await useEventsStore.getState().setFilters({ categories: ['coding'] })
    vi.mocked(fetchEvents).mockResolvedValueOnce(seekResponse())
    await useEventsStore.getState().seekToDate('2026-09-06')
    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    const change = useEventsStore.getState().setFilters({ categories: ['models'] })
    expect(useEventsStore.getState().dateAnchors).toEqual({})
    expect(useEventsStore.getState().dateCounts).toEqual({})
    await useEventsStore.getState().seekToDate('2026-09-06')
    expect(fetchEvents).toHaveBeenCalledTimes(3)
    pending.resolve(response([event(71)]))
    await change
    const reload = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(reload.promise)
    const cached = useEventsStore.getState().setFilters({ categories: ['coding'] })
    expect(useEventsStore.getState().events.map((item) => item.id)).toEqual([70])
    expect(useEventsStore.getState().dateAnchors).toEqual({ '2026-09-07': 70 })
    expect(useEventsStore.getState().filtering).toBe(true)
    reload.resolve(response([event(72)]))
    await cached
    expect(useEventsStore.getState().filtering).toBe(false)
  })

  it('preserves the proven server day head when replaying a client-sorted first-page cache', async () => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(71), event(72)]))
    await useEventsStore.getState().setFilters({ categories: ['coding'] })
    expect(useEventsStore.getState().dateAnchors['2026-09-07']).toBe(71)
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(80)]))
    await useEventsStore.getState().setFilters({ categories: ['models'] })
    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    const replay = useEventsStore.getState().setFilters({ categories: ['coding'] })
    expect(useEventsStore.getState().dateAnchors['2026-09-07']).toBe(71)
    pending.resolve(response([event(73)]))
    await replay
  })

  it('replays the server page tail instead of inferring it from client order', async () => {
    // A legacy missing first_doc_at can make server and client date ordering differ.
    const tail = { ...event(60, '06'), first_doc_at: '', last_doc_at: '2026-09-06T08:00:00Z' }
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(70), event(40, '04'), tail]))
    await useEventsStore.getState().setFilters({ categories: ['coding'] })
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(80)]))
    await useEventsStore.getState().setFilters({ categories: ['models'] })
    vi.mocked(fetchEvents).mockRejectedValueOnce(new Error('keep matching cache'))
    await useEventsStore.getState().setFilters({ categories: ['coding'] })
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(50, '05')]))
    await useEventsStore.getState().loadMore()
    expect(useEventsStore.getState().dateAnchors['2026-09-05']).toBe(50)
  })

  it('restores the current filtered first page before allowing the latest scroll', async () => {
    useEventsStore.setState({ enabled: true, events: [event(60, '06')], timelineStartDate: '2026-09-06', filters: { categories: ['coding'] } })
    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    const returning = useEventsStore.getState().backToLatest()
    expect(useEventsStore.getState().events[0].id).toBe(60)
    expect(useEventsStore.getState().navigation?.status).toBe('loading')
    expect(fetchEvents).toHaveBeenCalledWith({ page: 1, limit: 20, categories: ['coding'], timezoneOffsetMinutes: -480 })
    pending.resolve(response([event(70)]))
    await returning
    expect(useEventsStore.getState()).toMatchObject({ timelineStartDate: null, navigation: { kind: 'latest', status: 'ready' } })
    expect(useEventsStore.getState().events[0].id).toBe(70)
  })

  it('preserves an older window when returning to latest fails', async () => {
    const original = [event(60, '06')]
    useEventsStore.setState({ enabled: true, events: original, cursor: 4, timelineStartDate: '2026-09-06' })
    vi.mocked(fetchEvents).mockRejectedValueOnce(new Error('暂时不可用'))
    await useEventsStore.getState().backToLatest()
    expect(useEventsStore.getState()).toMatchObject({ events: original, cursor: 4, timelineStartDate: '2026-09-06', navigation: { kind: 'latest', status: 'error' } })
  })

  it('preserves an older window if both latest attempts degrade to empty', async () => {
    const original = [event(60, '06')]
    useEventsStore.setState({ enabled: true, events: original, cursor: 4, timelineStartDate: '2026-09-06' })
    vi.mocked(fetchEvents).mockResolvedValue(response([], { degraded: true, next_cursor: null }))
    await useEventsStore.getState().backToLatest()
    expect(fetchEvents).toHaveBeenCalledTimes(2)
    expect(useEventsStore.getState()).toMatchObject({ events: original, cursor: 4, timelineStartDate: '2026-09-06', navigation: { kind: 'latest', status: 'error' } })
  })

  it.each(['changed', 'fallback'] as const)('does not infer a boundary anchor across a %s read-model version', async (mode) => {
    const cursor = { version_id: 'original', scope_key: 'all', rank_after: 20 }
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(70)], { next_cursor: cursor, read_model_version_id: 'original', scope_key: 'all' }))
    await useEventsStore.getState().init()
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(61, '06')], mode === 'changed' ? { read_model_version_id: 'new', scope_key: 'all' } : {}))
    await useEventsStore.getState().loadMore()
    expect(useEventsStore.getState().dateAnchors['2026-09-06']).toBeUndefined()
  })

  it('cannot continue an old scope cursor after an uncached category request fails', async () => {
    useEventsStore.setState({ enabled: true, events: [event(70)], cursor: 4 })
    vi.mocked(fetchEvents).mockRejectedValueOnce(new Error('filter failed'))
    await useEventsStore.getState().setFilters({ categories: ['coding'] })
    expect(useEventsStore.getState().filtering).toBe(false)
    await useEventsStore.getState().loadMore()
    expect(fetchEvents).toHaveBeenCalledOnce()
    expect(useEventsStore.getState().events[0].id).toBe(70)
  })
})

describe('timeline request ownership', () => {
  it('an older category response cannot clear a newer category pending flag', async () => {
    const older = deferred<FeedEventsResponse>()
    const newer = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise)
    const first = useEventsStore.getState().setFilters({ categories: ['coding'] })
    const second = useEventsStore.getState().setFilters({ categories: ['models'] })
    older.resolve(response([event(70)]))
    await first
    expect(useEventsStore.getState().filtering).toBe(true)
    newer.reject(new Error('current category failed'))
    await second
    expect(useEventsStore.getState().filtering).toBe(false)
  })

  it.each(['refresh', 'seekToCursor', 'reset'] as const)('%s clears a superseded category pending flag', async (action) => {
    const category = deferred<FeedEventsResponse>()
    const replacement = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(category.promise).mockReturnValueOnce(replacement.promise)
    const first = useEventsStore.getState().setFilters({ categories: ['coding'] })
    expect(useEventsStore.getState().filtering).toBe(true)
    const state = useEventsStore.getState()
    const second = action === 'seekToCursor' ? state.seekToCursor(8) : state[action]()
    expect(useEventsStore.getState().filtering).toBe(false)
    category.resolve(response([event(70)]))
    await first
    expect(useEventsStore.getState().filtering).toBe(false)
    replacement.resolve(response([event(60, '06')]))
    await second
  })

  it('ordinary pagination does not disable changing dates as category filtering', async () => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(70)]))
    await useEventsStore.getState().init()
    const page = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(page.promise).mockResolvedValueOnce(seekResponse())
    const more = useEventsStore.getState().loadMore()
    expect(useEventsStore.getState()).toMatchObject({ loading: true, filtering: false })
    await useEventsStore.getState().seekToDate('2026-09-06')
    expect(useEventsStore.getState()).toMatchObject({ filtering: false, navigation: { status: 'ready' } })
    page.resolve(response([event(69)]))
    await more
  })

  it.each(['init', 'loadMore', 'refresh', 'setFilters', 'seekToCursor'] as const)('ignores an older %s response after a date seek', async (action) => {
    useEventsStore.setState({ events: [event(70)], cursor: 2 })
    const old = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(old.promise)
    const state = useEventsStore.getState()
    const pending = action === 'setFilters' ? state.setFilters({ categories: [] })
      : action === 'seekToCursor' ? state.seekToCursor(5) : state[action]()
    // A category request blocks navigation until it settles; refresh explicitly supersedes it.
    if (action === 'setFilters') {
      vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(71)]))
      await useEventsStore.getState().refresh()
    }
    vi.mocked(fetchEvents).mockResolvedValueOnce(seekResponse())
    await useEventsStore.getState().seekToDate('2026-09-06')
    old.resolve(response([event(99)]))
    await pending
    expect(useEventsStore.getState().events.map((item) => item.id)).toEqual([60, 59])
    expect(useEventsStore.getState().navigation?.status).toBe('ready')
  })

  it.each(['resolve', 'reject'] as const)('ignores older date %s and matching completion cannot release a newer request', async (outcome) => {
    const old = deferred<FeedEventsResponse>()
    const newer = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(old.promise).mockReturnValueOnce(newer.promise)
    const first = useEventsStore.getState().seekToDate('2026-09-06')
    const firstId = useEventsStore.getState().navigation!.requestId
    const second = useEventsStore.getState().seekToDate('2026-09-05')
    useEventsStore.getState().finishNavigation(firstId)
    if (outcome === 'resolve') old.resolve(seekResponse())
    else old.reject(new Error('old failure'))
    await first
    expect(useEventsStore.getState().navigation).toMatchObject({ date: '2026-09-05', status: 'loading' })
    newer.resolve(seekResponse('05', 50))
    await second
    expect(useEventsStore.getState().events[0].id).toBe(50)
  })

  it.each(['finishNavigation', 'searchClusters', 'clearSearch', 'reset'] as const)('%s cancels an in-flight date request without a late replacement', async (action) => {
    vi.useFakeTimers()
    const original = [event(70)]
    useEventsStore.setState({ enabled: true, events: original })
    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    const seeking = useEventsStore.getState().seekToDate('2026-09-06')
    const state = useEventsStore.getState()
    if (action === 'finishNavigation') state.finishNavigation(state.navigation!.requestId)
    else if (action === 'searchClusters') void state.searchClusters('模型')
    else state[action]()
    pending.resolve(seekResponse())
    await seeking
    expect(useEventsStore.getState().navigation).toBeNull()
    expect(useEventsStore.getState().events).toEqual(action === 'reset' ? [] : original)
    expect(useEventsStore.getState().loading).toBe(false)
  })

  it('blocks date and latest navigation during search debounce', async () => {
    vi.useFakeTimers()
    void useEventsStore.getState().searchClusters('模型')
    await useEventsStore.getState().seekToDate('2026-09-06')
    await useEventsStore.getState().backToLatest()
    expect(fetchEvents).not.toHaveBeenCalled()
    expect(useEventsStore.getState().navigation).toBeNull()
  })

  it('dismissing a date error does not release a later ordinary pagination request', async () => {
    useEventsStore.setState({ enabled: true, events: [event(70)], cursor: 2 })
    vi.mocked(fetchEvents).mockRejectedValueOnce(new Error('seek failed'))
    await useEventsStore.getState().seekToDate('2026-09-06')
    const failedId = useEventsStore.getState().navigation!.requestId
    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    const more = useEventsStore.getState().loadMore()
    useEventsStore.getState().finishNavigation(failedId)
    expect(useEventsStore.getState().loading).toBe(true)
    pending.resolve(response([event(60, '06')]))
    await more
  })

  it.each(['setFilters', 'refresh', 'backToLatest', 'seekToCursor'] as const)('a newer %s prevents date completion from replacing its window', async (action) => {
    useEventsStore.setState({ enabled: true, events: [event(70)], cursor: 2 })
    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise).mockResolvedValueOnce(response([event(99)]))
    const seeking = useEventsStore.getState().seekToDate('2026-09-06')
    const state = useEventsStore.getState()
    if (action === 'setFilters') await state.setFilters({ categories: ['coding'] })
    else if (action === 'seekToCursor') await state.seekToCursor(5)
    else await state[action]()
    const expected = useEventsStore.getState().events
    pending.resolve(seekResponse())
    await seeking
    expect(useEventsStore.getState().events).toBe(expected)
    expect(useEventsStore.getState().navigation?.kind).toBe(action === 'backToLatest' ? 'latest' : undefined)
  })

  it('an older paging failure cannot clear the loading flag of a date request', async () => {
    useEventsStore.setState({ enabled: true, events: [event(70)], cursor: 2 })
    const old = deferred<FeedEventsResponse>()
    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(old.promise).mockReturnValueOnce(pending.promise)
    const more = useEventsStore.getState().loadMore()
    const seek = useEventsStore.getState().seekToDate('2026-09-06')
    old.reject(new Error('old page failed'))
    await more
    expect(useEventsStore.getState()).toMatchObject({ loading: true, error: null, navigation: { status: 'loading' } })
    pending.resolve(seekResponse())
    await seek
  })
})
