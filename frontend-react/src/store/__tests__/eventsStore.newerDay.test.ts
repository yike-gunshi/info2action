import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useEventsStore } from '../eventsStore'
import { fetchEvents, searchRecommend } from '../../lib/api'
import type { ClusterEvent, FeedEventsResponse, FeedEventsCursor } from '../../lib/types'

vi.mock('../../lib/api', () => ({ fetchEvents: vi.fn(), searchRecommend: vi.fn() }))

function event(id: number, day = '10'): ClusterEvent {
  return { id, ai_title: `event ${id}`, doc_count: 1, unique_source_count: 1,
    first_doc_at: `2026-09-${day}T08:00:00Z`, last_doc_at: null, platforms: [],
    cover_url: null, has_update: false, live_version: 1 }
}

const originalCursor = { version_id: 'version-1', scope_key: 'all', rank_after: 420 }
const counts = { '2026-09-15': 4, '2026-09-11': 2, '2026-09-10': 48, '2026-09-09': 20 }

function response(events: ClusterEvent[], extra: Partial<FeedEventsResponse> = {}): FeedEventsResponse {
  return { enabled: true, events, next_cursor: null, new_since_last_fetch: 0,
    total_available_within_30d: 74, date_counts: counts,
    read_model_version_id: 'version-1', scope_key: 'all', ...extra }
}

function seekResponse(extra: Partial<FeedEventsResponse> = {}): FeedEventsResponse {
  return response([event(200, '11'), event(199, '11'), event(100)], {
    date_seek: { requested_date: '2026-09-11', status: 'found', anchor_event_id: 200 }, ...extra,
  })
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function loadNewer() {
  expect(useEventsStore.getState().loadNewerDay).toEqual(expect.any(Function))
  return useEventsStore.getState().loadNewerDay()
}

beforeEach(() => {
  useEventsStore.getState().reset()
  vi.mocked(fetchEvents).mockReset()
  vi.mocked(searchRecommend).mockReset()
  useEventsStore.setState({ enabled: true, events: [event(100), event(99, '09')],
    dateCounts: counts, allDateCounts: counts, dateAnchors: { '2026-09-10': 100, '2026-09-09': 99 },
    timelineStartDate: '2026-09-10', snapshotVersion: 500, cursor: originalCursor })
})

afterEach(() => {
  useEventsStore.getState().reset()
  vi.useRealTimers()
})

describe('adjacent newer day loading', () => {
  it('publishes the target date immediately and the committed count even for an instant response', async () => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(seekResponse())
    const loading = loadNewer()
    const feedback = useEventsStore.getState().newerFeedback
    expect(feedback).toMatchObject({ status: 'loading', targetDate: '2026-09-11', requestId: expect.any(Number) })
    await loading
    expect(useEventsStore.getState().newerFeedback).toEqual({ ...feedback, status: 'success', count: 2 })
  })

  it.each(['seekToDate', 'setFilters', 'searchClusters', 'clearSearch', 'refresh', 'reset', 'cancelNewerDay'] as const)(
    '%s clears completed feedback as well as pending requests', async (action) => {
      vi.mocked(fetchEvents).mockResolvedValueOnce(seekResponse())
      await loadNewer()
      expect(useEventsStore.getState().newerFeedback?.status).toBe('success')
      const state = useEventsStore.getState()
      if (action === 'seekToDate') await state.seekToDate('2026-09-10')
      else if (action === 'setFilters') {
        vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(900)]))
        await state.setFilters({ categories: ['models'] })
      } else if (action === 'searchClusters') {
        vi.useFakeTimers()
        await state.searchClusters('模型')
      } else if (action === 'refresh') {
        vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(999, '15')]))
        await state.refresh()
      } else state[action]()
      expect(useEventsStore.getState().newerFeedback).toBeNull()
    },
  )

  it('a stale success dismissal cannot clear a newer request or its error', async () => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(seekResponse())
    await loadNewer()
    const oldId = useEventsStore.getState().newerFeedback!.requestId
    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    const loading = loadNewer()
    useEventsStore.getState().clearNewerFeedback(oldId)
    expect(useEventsStore.getState().newerFeedback).toMatchObject({ status: 'loading', targetDate: '2026-09-15' })
    pending.reject(new Error('网络暂时不可用'))
    await loading
    useEventsStore.getState().clearNewerFeedback(oldId)
    expect(useEventsStore.getState().newerFeedback).toMatchObject({ status: 'error', targetDate: '2026-09-15' })
  })

  it('reports a synchronous incomplete-anchor error with the intended target date', async () => {
    useEventsStore.setState({ dateAnchors: {} })
    await loadNewer()
    expect(fetchEvents).not.toHaveBeenCalled()
    expect(useEventsStore.getState().newerFeedback).toMatchObject({ status: 'error', targetDate: '2026-09-11', message: expect.stringContaining('起点不完整') })
  })

  it.each(['resolve', 'reject'] as const)('does not resurrect feedback after a cancelled request later %ss', async (completion) => {
    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    const loading = loadNewer()
    useEventsStore.getState().cancelNewerDay()
    if (completion === 'resolve') pending.resolve(seekResponse())
    else pending.reject(new Error('late failure'))
    await loading
    expect(useEventsStore.getState().newerFeedback).toBeNull()
  })

  it('prepends only the next available day and preserves the old window, snapshot and downward cursor', async () => {
    const old = useEventsStore.getState().events
    vi.mocked(fetchEvents).mockResolvedValueOnce(seekResponse())
    await loadNewer()
    expect(fetchEvents).toHaveBeenCalledWith({ targetDate: '2026-09-11', page: originalCursor,
      limit: 100, categories: [], timezoneOffsetMinutes: -480 })
    expect(useEventsStore.getState()).toMatchObject({
      events: [event(200, '11'), event(199, '11'), ...old], cursor: originalCursor,
      snapshotVersion: 500, timelineStartDate: '2026-09-11', loadingNewer: false, newerError: null,
      dateAnchors: { '2026-09-11': 200, '2026-09-10': 100, '2026-09-09': 99 }, prependVersion: 1,
    })
    expect(useEventsStore.getState().events[2]).toBe(old[0])
    expect(useEventsStore.getState().events[3]).toBe(old[1])
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(90, '09')]))
    await useEventsStore.getState().loadMore()
    expect(fetchEvents).toHaveBeenLastCalledWith({ page: originalCursor, limit: 20, categories: [], timezoneOffsetMinutes: -480 })
    expect(useEventsStore.getState().events.map(({ id }) => id)).toEqual([200, 199, 100, 99, 90])
  })

  it('loads a 300 item day in four sequential existing API pages and commits once at the proven boundary', async () => {
    const day = Array.from({ length: 300 }, (_, i) => event(400 - i, '11'))
    const prefix = Array.from({ length: 50 }, (_, i) => event(500 - i, '12'))
    const all = [...prefix, ...day, event(100)]
    const pageCounts = { ...counts, '2026-09-11': 300, '2026-09-12': 50 }
    const old = useEventsStore.getState().events
    const last = deferred<FeedEventsResponse>()
    let pendingRequests = 0
    let maxPending = 0
    vi.mocked(fetchEvents).mockImplementation(async (params) => {
      pendingRequests += 1
      maxPending = Math.max(maxPending, pendingRequests)
      const offset = params?.targetDate ? 0 : (params?.page as { rank_after: number }).rank_after
      const value = offset === 300 ? await last.promise : response(all.slice(offset, offset + 100), {
        date_counts: pageCounts,
        next_cursor: { version_id: 'version-1', scope_key: 'all', rank_after: offset + 100 },
        ...(offset === 0 ? { date_seek: { requested_date: '2026-09-11' as const, status: 'found' as const, anchor_event_id: 400 } } : {}),
      })
      pendingRequests -= 1
      return value
    })
    const loading = loadNewer()
    await vi.waitFor(() => expect(fetchEvents).toHaveBeenCalledTimes(4))
    expect(useEventsStore.getState().events).toBe(old)
    expect(useEventsStore.getState().loadingNewer).toBe(true)
    expect(useEventsStore.getState().newerFeedback).toMatchObject({ status: 'loading', targetDate: '2026-09-11' })
    last.resolve(response(all.slice(300), { date_counts: pageCounts }))
    await loading
    expect(useEventsStore.getState().newerFeedback).toMatchObject({ status: 'success', count: 300 })
    expect(maxPending).toBe(1)
    expect(vi.mocked(fetchEvents).mock.calls.every(([params]) => params?.limit === 100)).toBe(true)
    expect(useEventsStore.getState().events.map(({ id }) => id)).toEqual([...day, ...old].map(({ id }) => id))
    expect(useEventsStore.getState().prependVersion).toBe(1)
    expect(useEventsStore.getState().cursor).toEqual(originalCursor)
  })

  it('skips empty, malformed and out of range directory dates without fetching latest', async () => {
    useEventsStore.setState({ dateCounts: { '2026-09-15': 4, '2026-09-14': Number.NaN,
      '2026-09-13': 0, '2026-09-12': -2, '2026-09-11': 2,
      '2026-09-99': 3, '2026-07-02': 10, '2026-09-10': 48 } })
    vi.mocked(fetchEvents).mockResolvedValueOnce(seekResponse())
    await loadNewer()
    expect(fetchEvents).toHaveBeenCalledOnce()
    expect(fetchEvents).toHaveBeenCalledWith(expect.objectContaining({ targetDate: '2026-09-11' }))
  })

  it('does nothing when already at the latest day', async () => {
    useEventsStore.setState({ dateCounts: { '2026-09-10': 48 } })
    await loadNewer()
    expect(fetchEvents).not.toHaveBeenCalled()
    expect(useEventsStore.getState().prependVersion).toBe(0)
  })

  it.each(['503', 'not_found', 'missing_anchor', 'missing_old_head', 'missing_items', 'degraded', 'version_changed', 'scope_changed'] as const)(
    'retains content and exposes a retryable error for %s', async (failure) => {
      const old = useEventsStore.getState().events
      const value = seekResponse()
      if (failure === 'not_found') value.date_seek = { requested_date: '2026-09-11', status: 'not_found', anchor_event_id: null }
      if (failure === 'missing_anchor') value.date_seek = { requested_date: '2026-09-11', status: 'found', anchor_event_id: 999 }
      if (failure === 'missing_old_head') value.events = [event(200, '11'), event(199, '11'), event(98)]
      if (failure === 'missing_items') value.events = [event(200, '11'), event(100)]
      if (failure === 'degraded') value.degraded = true
      if (failure === 'version_changed') value.read_model_version_id = 'version-2'
      if (failure === 'scope_changed') value.scope_key = 'models'
      if (failure === '503') vi.mocked(fetchEvents).mockRejectedValueOnce(new Error('服务暂时不可用'))
      else vi.mocked(fetchEvents).mockResolvedValueOnce(value)
      await loadNewer()
      expect(useEventsStore.getState()).toMatchObject({ loadingNewer: false, newerError: expect.any(String), cursor: originalCursor, prependVersion: 0 })
      expect(useEventsStore.getState().events).toBe(old)
      vi.mocked(fetchEvents).mockResolvedValueOnce(seekResponse())
      await loadNewer()
      expect(useEventsStore.getState().events.map(({ id }) => id)).toEqual([200, 199, 100, 99])
      expect(useEventsStore.getState().newerError).toBeNull()
    },
  )

  it('rejects a repeated continuation cursor and never makes an unbounded loop', async () => {
    const next = { ...originalCursor, rank_after: 100 }
    vi.mocked(fetchEvents).mockResolvedValueOnce(seekResponse({ events: [event(200, '11')], next_cursor: next }))
      .mockResolvedValueOnce(response([event(199, '11')], { next_cursor: next }))
    const old = useEventsStore.getState().events
    await loadNewer()
    expect(fetchEvents).toHaveBeenCalledTimes(2)
    expect(useEventsStore.getState().events).toBe(old)
    expect(useEventsStore.getState().newerError).toEqual(expect.any(String))
  })

  it('rejects a backwards continuation cursor before fetching a previous page', async () => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(seekResponse({ events: [event(200, '11')],
      next_cursor: { ...originalCursor, rank_after: 100 } }))
      .mockResolvedValueOnce(response([event(199, '11')], { next_cursor: { ...originalCursor, rank_after: 50 } }))
      .mockResolvedValueOnce(response([event(100)]))
    const old = useEventsStore.getState().events
    await loadNewer()
    expect(fetchEvents).toHaveBeenCalledTimes(2)
    expect(useEventsStore.getState().events).toBe(old)
    expect(useEventsStore.getState().newerError).toEqual(expect.any(String))
  })

  it('rejects an old retained card reappearing under the newer date instead of rendering duplicate IDs', async () => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(seekResponse({ events: [event(200, '11'), event(99, '11'), event(100)] }))
    const old = useEventsStore.getState().events
    await loadNewer()
    expect(useEventsStore.getState().events).toBe(old)
    expect(useEventsStore.getState().newerError).toEqual(expect.any(String))
  })

  it('rejects a version change on a continuation page without committing its staged prefix', async () => {
    vi.mocked(fetchEvents).mockResolvedValueOnce(seekResponse({ events: [event(200, '11')],
      next_cursor: { ...originalCursor, rank_after: 100 } }))
      .mockResolvedValueOnce(response([event(199, '11'), event(100)], { read_model_version_id: 'version-2' }))
    const old = useEventsStore.getState().events
    await loadNewer()
    expect(useEventsStore.getState().events).toBe(old)
    expect(useEventsStore.getState().newerError).toEqual(expect.any(String))
  })

  it('keeps a head read-model version even when the original downward cursor is null', async () => {
    useEventsStore.getState().reset()
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(100)], {
      date_seek: { requested_date: '2026-09-10', status: 'found', anchor_event_id: 100 }, next_cursor: null,
    }))
    await useEventsStore.getState().seekToDate('2026-09-10')
    useEventsStore.getState().finishNavigation(useEventsStore.getState().navigation!.requestId)
    vi.mocked(fetchEvents).mockResolvedValueOnce(seekResponse())
    await loadNewer()
    expect(fetchEvents).toHaveBeenLastCalledWith(expect.objectContaining({ targetDate: '2026-09-11',
      page: expect.objectContaining({ version_id: 'version-1', scope_key: 'all' }) }))
    expect(useEventsStore.getState().cursor).toBeNull()
  })

  it('supports the existing numeric cursor fallback without changing the old downward cursor', async () => {
    useEventsStore.setState({ cursor: 7 })
    vi.mocked(fetchEvents).mockResolvedValueOnce(seekResponse({ read_model_version_id: undefined, scope_key: undefined }))
    await loadNewer()
    expect(useEventsStore.getState().events.map(({ id }) => id)).toEqual([200, 199, 100, 99])
    expect(useEventsStore.getState().cursor).toBe(7)
  })

  it('does not discard a concurrent optimistic read-status update on retained cards', async () => {
    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    const loading = loadNewer()
    useEventsStore.getState().markSeen(100, false)
    const readCard = useEventsStore.getState().events[0]
    pending.resolve(seekResponse())
    await loading
    expect(useEventsStore.getState().events[2]).toBe(readCard)
    expect(useEventsStore.getState().events[2].last_seen_version).toBe(1)
  })

  it.each(['seekToDate', 'setFilters', 'searchClusters', 'clearSearch', 'refresh', 'reset', 'cancelNewerDay'] as const)(
    '%s invalidates an in-flight prepend before it can replace the current window', async (action) => {
      const pending = deferred<FeedEventsResponse>()
      vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
      const loading = loadNewer()
      const state = useEventsStore.getState()
      if (action === 'seekToDate') await state.seekToDate('2026-09-09')
      else if (action === 'setFilters') {
        vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(900)], { scope_key: 'models' }))
        await state.setFilters({ categories: ['models'] })
      } else if (action === 'searchClusters') {
        vi.useFakeTimers()
        await state.searchClusters('模型')
      } else if (action === 'refresh') {
        vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(999, '15')]))
        await state.refresh()
      } else state[action]()
      const expected = useEventsStore.getState().events
      pending.resolve(seekResponse())
      await loading
      expect(useEventsStore.getState().events).toBe(expected)
      expect(useEventsStore.getState().loadingNewer).toBe(false)
      expect(useEventsStore.getState().prependVersion).toBe(0)
    },
  )

  it('deduplicates simultaneous upward gestures and blocks downward loading during staging', async () => {
    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    const loading = loadNewer()
    await loadNewer()
    await useEventsStore.getState().loadMore()
    expect(fetchEvents).toHaveBeenCalledOnce()
    pending.resolve(seekResponse())
    await loading
    expect(useEventsStore.getState().events.map(({ id }) => id)).toEqual([200, 199, 100, 99])
  })

  it('retains the original page tail proof after prepending', async () => {
    useEventsStore.getState().reset()
    const next: FeedEventsCursor = { ...originalCursor, rank_after: 60 }
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(100), event(99, '09')], {
      date_seek: { requested_date: '2026-09-10', status: 'found', anchor_event_id: 100 }, next_cursor: next,
    }))
    await useEventsStore.getState().seekToDate('2026-09-10')
    useEventsStore.getState().finishNavigation(useEventsStore.getState().navigation!.requestId)
    vi.mocked(fetchEvents).mockResolvedValueOnce(seekResponse())
    await loadNewer()
    vi.mocked(fetchEvents).mockResolvedValueOnce(response([event(80, '08')]))
    await useEventsStore.getState().loadMore()
    expect(useEventsStore.getState().dateAnchors['2026-09-08']).toBe(80)
  })
})
