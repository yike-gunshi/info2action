import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useEventsStore } from '../eventsStore'
import type { FeedEventsResponse, ClusterEvent, SearchRecommendResponse } from '../../lib/types'

// Mock the api module
vi.mock('../../lib/api', () => ({
  fetchEvents: vi.fn(),
  searchRecommend: vi.fn(),
  markClusterSeen: vi.fn(),
}))

import { fetchEvents, searchRecommend, markClusterSeen } from '../../lib/api'

function makeEvent(id: number, overrides: Partial<ClusterEvent> = {}): ClusterEvent {
  return {
    id,
    ai_title: `Event ${id}`,
    doc_count: 3,
    unique_source_count: 3,
    first_doc_at: '2026-04-23T09:00:00Z',
    last_doc_at: '2026-04-23T09:30:00Z',
    platforms: ['twitter'],
    cover_url: null,
    has_update: false,
    live_version: 1,
    ...overrides,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

beforeEach(() => {
  // 重置 store 到初始态
  useEventsStore.getState().reset()
  vi.mocked(fetchEvents).mockReset()
  vi.mocked(searchRecommend).mockReset()
  vi.mocked(markClusterSeen).mockReset()
  // markClusterSeen 默认返回成功 — markSeen 是 fire-and-forget，
  // 失败 swallow（feature-spec R7.1）
  vi.mocked(markClusterSeen).mockResolvedValue({
    cluster_id: 0,
    last_seen_version: 0,
  })
})

afterEach(() => {
  useEventsStore.getState().reset()
})

describe('eventsStore.init', () => {
  it('成功拉取后 events / cursor / snapshotVersion 写入', async () => {
    const resp: FeedEventsResponse = {
      enabled: true,
      events: [
        makeEvent(10, { first_doc_at: '2026-04-24T09:00:00Z', last_doc_at: '2026-04-24T09:30:00Z' }),
        makeEvent(8, { first_doc_at: '2026-04-20T09:00:00Z', last_doc_at: '2026-04-26T09:30:00Z' }),
      ],
      next_cursor: 2,
      new_since_last_fetch: 0,
      total_available_within_30d: 5,
    }
    vi.mocked(fetchEvents).mockResolvedValue(resp)
    await useEventsStore.getState().init()
    const s = useEventsStore.getState()
    expect(s.enabled).toBe(true)
    expect(s.events).toHaveLength(2)
    expect(s.events.map((e) => e.id)).toEqual([10, 8])
    // v17.0: fetchEvents 加 categories 参数 (精选 tab L1 chip 多 OR), 默认空列表
    expect(fetchEvents).toHaveBeenCalledWith({
      page: 1,
      limit: 20,
      categories: [],
      timezoneOffsetMinutes: expect.any(Number),
    })
    expect(s.cursor).toBe(2)
    expect(s.snapshotVersion).toBe(10) // 取首发时间倒排后首条 event.id
    expect(s.loading).toBe(false)
  })

  it('失败时 enabled 保持未知(null)+ error 写入(UX-2: 加载失败≠功能未启用)', async () => {
    vi.mocked(fetchEvents).mockRejectedValue(new Error('Network'))
    await useEventsStore.getState().init()
    const s = useEventsStore.getState()
    expect(s.enabled).toBe(null)
    expect(s.error).toBe('Network')
    expect(s.loading).toBe(false)
  })

  it('events 为空时 snapshotVersion=null', async () => {
    vi.mocked(fetchEvents).mockResolvedValue({
      enabled: true,
      events: [],
      next_cursor: null,
      new_since_last_fetch: 0,
      total_available_within_30d: 0,
    })
    await useEventsStore.getState().init()
    expect(useEventsStore.getState().snapshotVersion).toBe(null)
  })

  it('默认精选降级为空时用近 7 天窗口重试一次,避免页面空白', async () => {
    vi.mocked(fetchEvents)
      .mockResolvedValueOnce({
        enabled: true,
        events: [],
        next_cursor: null,
        new_since_last_fetch: 0,
        total_available_within_30d: 0,
        degraded: true,
      })
      .mockResolvedValueOnce({
        enabled: true,
        events: [makeEvent(20)],
        next_cursor: null,
        new_since_last_fetch: 0,
        total_available_within_30d: 1,
        date_counts: { '2026-05-27': 1 },
      })

    await useEventsStore.getState().init()

    expect(fetchEvents).toHaveBeenCalledTimes(2)
    expect(fetchEvents).toHaveBeenNthCalledWith(1, {
      page: 1,
      limit: 20,
      categories: [],
      timezoneOffsetMinutes: expect.any(Number),
    })
    expect(fetchEvents).toHaveBeenNthCalledWith(2, expect.objectContaining({
      page: 1,
      limit: 20,
      categories: [],
      timezoneOffsetMinutes: expect.any(Number),
      fetchedSince: expect.any(String),
    }))
    expect(useEventsStore.getState().events.map((event) => event.id)).toEqual([20])
  })
})

describe('eventsStore.loadMore', () => {
  it('cursor=null 时不发请求', async () => {
    useEventsStore.setState({ cursor: null, events: [makeEvent(1)] })
    await useEventsStore.getState().loadMore()
    expect(fetchEvents).not.toHaveBeenCalled()
  })

  it('追加去重(已有 id 不重复 push)', async () => {
    useEventsStore.setState({
      cursor: 2,
      events: [makeEvent(1), makeEvent(2)],
      dateCounts: { '2026-04-23': 2 },
    })
    vi.mocked(fetchEvents).mockResolvedValue({
      enabled: true,
      events: [makeEvent(2), makeEvent(3)], // id=2 已存在
      next_cursor: null,
      new_since_last_fetch: 0,
      total_available_within_30d: 3,
      date_counts: { '2026-04-23': 99 },
    })
    await useEventsStore.getState().loadMore()
    const s = useEventsStore.getState()
    expect(s.events.map((e) => e.id)).toEqual([3, 2, 1])
    expect(s.dateCounts).toEqual({ '2026-04-23': 2 })
    expect(s.cursor).toBe(null) // 到底
  })

  it('read model cursor 对象时继续用 cursor 参数加载下一页', async () => {
    const cursor = {
      version_id: '00000000-0000-0000-0000-00000000beef',
      scope_key: 'all',
      rank_after: 20,
    }
    useEventsStore.setState({
      cursor,
      events: [makeEvent(1)],
    })
    vi.mocked(fetchEvents).mockResolvedValue({
      enabled: true,
      events: [makeEvent(2)],
      next_cursor: null,
      new_since_last_fetch: 0,
      total_available_within_30d: 2,
    })

    await useEventsStore.getState().loadMore()

    expect(fetchEvents).toHaveBeenCalledWith({
      page: cursor,
      limit: 20,
      categories: [],
      timezoneOffsetMinutes: expect.any(Number),
    })
    expect(useEventsStore.getState().events.map((e) => e.id)).toEqual([2, 1])
  })
})

describe('eventsStore.refresh', () => {
  it('重置 events + 重新锁定 snapshotVersion', async () => {
    useEventsStore.setState({
      events: [makeEvent(5)],
      snapshotVersion: 5,
      newSinceCount: 3,
    })
    vi.mocked(fetchEvents).mockResolvedValue({
      enabled: true,
      events: [makeEvent(20), makeEvent(15)],
      next_cursor: 2,
      new_since_last_fetch: 0,
      total_available_within_30d: 5,
    })
    await useEventsStore.getState().refresh()
    const s = useEventsStore.getState()
    expect(s.events).toHaveLength(2)
    expect(s.snapshotVersion).toBe(20)
    expect(s.newSinceCount).toBe(0) // 重置计数
  })

  it('首条不变时写入 refreshHint,用于提示已是最新', async () => {
    useEventsStore.setState({
      events: [makeEvent(100)],
      snapshotVersion: 100,
      refreshHint: null,
    })
    vi.mocked(fetchEvents).mockResolvedValue({
      enabled: true,
      events: [makeEvent(100)],
      next_cursor: null,
      new_since_last_fetch: 0,
      total_available_within_30d: 1,
    })

    await useEventsStore.getState().refresh()

    expect(fetchEvents).toHaveBeenCalledWith({
      page: 1,
      limit: 20,
      categories: [],
      timezoneOffsetMinutes: expect.any(Number),
    })
    expect(useEventsStore.getState().refreshHint).toBe('已是最新')
  })
})

describe('eventsStore.setFilters', () => {
  it('keeps existing events visible while a pill filter request is pending', async () => {
    const pending = deferred<FeedEventsResponse>()
    const existing = [makeEvent(1), makeEvent(2)]
    useEventsStore.setState({
      events: existing,
      cursor: 2,
      snapshotVersion: 1,
      filters: { categories: [] },
    })
    vi.mocked(fetchEvents).mockReturnValue(pending.promise)

    const promise = useEventsStore.getState().setFilters({ categories: ['products'] })

    expect(useEventsStore.getState().filters.categories).toEqual(['products'])
    expect(useEventsStore.getState().events).toEqual(existing)
    expect(useEventsStore.getState().loading).toBe(true)
    expect(fetchEvents).toHaveBeenCalledWith({
      page: 1,
      limit: 20,
      categories: ['products'],
      timezoneOffsetMinutes: expect.any(Number),
    })

    pending.resolve({
      enabled: true,
      events: [makeEvent(20)],
      next_cursor: null,
      new_since_last_fetch: 0,
      total_available_within_30d: 1,
    })
    await promise

    expect(useEventsStore.getState().events.map((e) => e.id)).toEqual([20])
    expect(useEventsStore.getState().loading).toBe(false)
  })

  it('serves cached first-page events immediately on repeated pill filters', async () => {
    vi.mocked(fetchEvents).mockResolvedValueOnce({
      enabled: true,
      events: [makeEvent(30)],
      next_cursor: null,
      new_since_last_fetch: 0,
      total_available_within_30d: 1,
    })
    await useEventsStore.getState().setFilters({ categories: ['products'] })

    vi.mocked(fetchEvents).mockResolvedValueOnce({
      enabled: true,
      events: [makeEvent(31)],
      next_cursor: null,
      new_since_last_fetch: 0,
      total_available_within_30d: 1,
    })
    const promise = useEventsStore.getState().setFilters({ categories: [] })
    await promise
    expect(useEventsStore.getState().events.map((e) => e.id)).toEqual([31])

    const pending = deferred<FeedEventsResponse>()
    vi.mocked(fetchEvents).mockReturnValueOnce(pending.promise)
    const cachedPromise = useEventsStore.getState().setFilters({ categories: ['products'] })

    expect(useEventsStore.getState().events.map((e) => e.id)).toEqual([30])
    expect(useEventsStore.getState().loading).toBe(true)

    pending.resolve({
      enabled: true,
      events: [makeEvent(32)],
      next_cursor: null,
      new_since_last_fetch: 0,
      total_available_within_30d: 1,
    })
    await cachedPromise

    expect(useEventsStore.getState().events.map((e) => e.id)).toEqual([32])
  })
})

describe('eventsStore.searchClusters / clearSearch', () => {
  it('空字符串清空搜索结果', async () => {
    useEventsStore.setState({ searchResults: [makeEvent(1)], searchTotal: 1 })
    await useEventsStore.getState().searchClusters('   ')
    const s = useEventsStore.getState()
    expect(s.searchResults).toBe(null)
    expect(s.searchTotal).toBe(0)
  })

  it('防抖后调 searchRecommend 并写入结果', async () => {
    vi.useFakeTimers()
    const resp: SearchRecommendResponse = {
      events: [makeEvent(7)],
      events_total: 1,
      docs: [],
      docs_total: 0,
    }
    vi.mocked(searchRecommend).mockResolvedValue(resp)
    useEventsStore.getState().searchClusters('openai')
    expect(useEventsStore.getState().searching).toBe(true)
    vi.advanceTimersByTime(310)
    // 等微任务
    await Promise.resolve()
    await Promise.resolve()
    vi.useRealTimers()
    // v17.0: searchRecommend 加 categories 参数；性能分支仅拉 events 区。
    expect(searchRecommend).toHaveBeenCalledWith('openai', 30, { categories: [], eventsOnly: true })
  })

  it('clearSearch 清空所有搜索状态', () => {
    useEventsStore.setState({
      searchQuery: 'x',
      searchResults: [makeEvent(1)],
      searchTotal: 1,
      searching: true,
    })
    useEventsStore.getState().clearSearch()
    const s = useEventsStore.getState()
    expect(s.searchQuery).toBe('')
    expect(s.searchResults).toBe(null)
    expect(s.searchTotal).toBe(0)
    expect(s.searching).toBe(false)
  })

  // BF-0704-6: 后端搜索超时降级必须显式暴露,不得渲染假"无结果"
  it('degraded 响应设置 searchDegraded 并保留旧结果', async () => {
    vi.useFakeTimers()
    useEventsStore.setState({ searchResults: [makeEvent(3)], searchTotal: 1 })
    vi.mocked(searchRecommend).mockResolvedValue({
      events: [],
      events_total: 0,
      docs: [],
      docs_total: 0,
      degraded: true,
      degraded_reason: 'context_search_events_unavailable',
    })
    useEventsStore.getState().searchClusters('openai')
    await vi.advanceTimersByTimeAsync(310)
    await Promise.resolve()
    vi.useRealTimers()
    const s = useEventsStore.getState()
    expect(s.searchDegraded).toBe(true)
    expect(s.searching).toBe(false)
    // 不发布假空结果,保留上一次结果
    expect(s.searchResults?.map((e) => e.id)).toEqual([3])
  })

  it('正常响应重置 searchDegraded,请求异常置 searchDegraded', async () => {
    vi.useFakeTimers()
    useEventsStore.setState({ searchDegraded: true })
    vi.mocked(searchRecommend).mockResolvedValue({
      events: [makeEvent(9)],
      events_total: 1,
      docs: [],
      docs_total: 0,
    })
    useEventsStore.getState().searchClusters('claude')
    await vi.advanceTimersByTimeAsync(310)
    await Promise.resolve()
    expect(useEventsStore.getState().searchDegraded).toBe(false)
    expect(useEventsStore.getState().searchResults?.map((e) => e.id)).toEqual([9])

    vi.mocked(searchRecommend).mockRejectedValue(new Error('network down'))
    useEventsStore.getState().searchClusters('gemini')
    await vi.advanceTimersByTimeAsync(310)
    await Promise.resolve()
    vi.useRealTimers()
    const s = useEventsStore.getState()
    expect(s.searchDegraded).toBe(true)
    expect(s.searching).toBe(false)
    // 异常同样不发布假空结果
    expect(s.searchResults?.map((e) => e.id)).toEqual([9])
  })

  it('clearSearch 重置 searchDegraded', () => {
    useEventsStore.setState({ searchDegraded: true })
    useEventsStore.getState().clearSearch()
    expect(useEventsStore.getState().searchDegraded).toBe(false)
  })
})

describe('eventsStore.markSeen (v15.1 R7.1)', () => {
  it('乐观更新 events: has_update=false + last_seen_version=live_version', () => {
    useEventsStore.setState({
      events: [
        makeEvent(1, { live_version: 5, last_seen_version: 2, has_update: true }),
        makeEvent(2, { live_version: 3, last_seen_version: 3, has_update: false }),
      ],
    })
    useEventsStore.getState().markSeen(1)
    const s = useEventsStore.getState()
    expect(s.events[0].has_update).toBe(false)
    expect(s.events[0].last_seen_version).toBe(5)
    // 其他 cluster 不动
    expect(s.events[1].has_update).toBe(false)
    expect(s.events[1].last_seen_version).toBe(3)
  })

  it('调用 markClusterSeen API 一次', () => {
    useEventsStore.setState({
      events: [makeEvent(99, { live_version: 7, has_update: true })],
    })
    useEventsStore.getState().markSeen(99)
    expect(markClusterSeen).toHaveBeenCalledTimes(1)
    expect(markClusterSeen).toHaveBeenCalledWith(99)
  })

  it('API 失败不抛异常（fire-and-forget swallow）', async () => {
    vi.mocked(markClusterSeen).mockRejectedValueOnce(new Error('500'))
    useEventsStore.setState({
      events: [makeEvent(7, { live_version: 2, has_update: true })],
    })
    // 不应抛
    expect(() => useEventsStore.getState().markSeen(7)).not.toThrow()
    // 即使 API 失败，乐观更新仍然生效
    const s = useEventsStore.getState()
    expect(s.events[0].has_update).toBe(false)
    // 让 microtask flush 触发 .catch
    await Promise.resolve()
    await Promise.resolve()
  })

  it('searchResults 内若有该 cluster 也同步更新', () => {
    useEventsStore.setState({
      events: [],
      searchResults: [
        makeEvent(11, { live_version: 4, last_seen_version: 1, has_update: true }),
        makeEvent(12, { live_version: 4, last_seen_version: 4, has_update: false }),
      ],
    })
    useEventsStore.getState().markSeen(11)
    const s = useEventsStore.getState()
    expect(s.searchResults![0].has_update).toBe(false)
    expect(s.searchResults![0].last_seen_version).toBe(4)
  })

  it('未命中 id 时不动 events', () => {
    const init = makeEvent(5, { has_update: true, last_seen_version: 0, live_version: 3 })
    useEventsStore.setState({ events: [init] })
    useEventsStore.getState().markSeen(999)
    expect(useEventsStore.getState().events[0]).toEqual(init)
  })
})
