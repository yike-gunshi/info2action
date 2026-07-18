/**
 * v15.0 事件聚合 store — 最新事件时间线状态管理。
 *
 * 职责：
 *   - 管理 cluster 时间线（snapshot-on-load，滚动期间不跳动）
 *   - 保留分页状态（首页时间线触底按 next_cursor 继续加载）
 *   - 新增事件计数（newSinceCount）
 *   - event_aggregation_ready feature flag（enabled）
 *
 * 决策：
 *   - 首次加载锁定 snapshotVersion（取首条 cluster.id，作为后续 re-fetch 的 since_version_snapshot）
 *   - refresh() 点刷新/悬浮按钮 → 重置 events + snapshot
 *   - loadMore() 由 LatestEvents 触底调用，next_cursor=null 时停止
 *   - Fast Refresh 硬约束：本文件只导出 useEventsStore（遵循 feedback_react_fast_refresh_no_mixed_export）
 */
import { create } from 'zustand'
import type { ClusterEvent, FeedEventsCursor, FeedEventsResponse } from '../lib/types'
import { fetchEvents, searchRecommend, markClusterSeen } from '../lib/api'

interface EventsState {
  enabled: boolean | null  // null = 尚未拉过，true/false = 拉过后的 flag
  events: ClusterEvent[]
  /** YYYY-MM-DD -> full filtered event count, independent of loaded pages */
  dateCounts: Record<string, number>
  cursor: FeedEventsCursor
  /** snapshot 基线：第一次 load 时取首条 event.id，用于后续 since_version_snapshot */
  snapshotVersion: number | null
  newSinceCount: number
  loading: boolean
  error: string | null
  /** refresh() 防抖用：是否正在重载 */
  refreshing: boolean
  /** 主动刷新后,如果首条未变化,由 UI toast 告知"已是最新"。 */
  refreshHint: string | null

  // v17.0: 「精选」tab L1 筛选 — categories OR
  filters: { categories: string[] }

  // 搜索态（推荐页 context=recommend 时双区命中）
  searchQuery: string
  searchResults: ClusterEvent[] | null  // null = 未搜索
  searchTotal: number
  searching: boolean
  // BF-0704-6: 后端搜索降级/请求失败时为 true,UI 显示提示而非假"无结果"
  searchDegraded: boolean

  init: () => Promise<void>
  loadMore: () => Promise<void>
  refresh: () => Promise<void>
  clearRefreshHint: () => void
  /** v17.0: 设置筛选 → 重置 events + 重新 fetch */
  setFilters: (filters: { categories: string[] }) => Promise<void>
  searchClusters: (query: string) => Promise<void>
  clearSearch: () => void
  /**
   * v15.1 R7.1：用户点开 cluster 弹窗时调用。
   * - 立即把本地 events[] 中该 cluster 的 has_update 置 false +
   *   last_seen_version 提升到 live_version（乐观更新，让角标立即消失）
   * - 后台调用 POST /api/clusters/:id/seen（失败 swallow，不阻塞渲染，
   *   feature-spec R7.1 验收）
   */
  markSeen: (clusterId: number) => void
  /** 清 cache 用（例如登出） */
  reset: () => void
}

let _searchTimer: ReturnType<typeof setTimeout> | null = null
let _searchSeq = 0
let _filterSeq = 0

interface FirstPageCacheEntry {
  enabled: boolean
  events: ClusterEvent[]
  dateCounts: Record<string, number>
  cursor: FeedEventsCursor
  snapshotVersion: number | null
}

const _firstPageCache = new Map<string, FirstPageCacheEntry>()

function filterKey(categories: string[]): string {
  return [...categories].sort().join(',')
}

function eventTime(e: ClusterEvent): number {
  const value = e.first_doc_at || e.last_doc_at
  return value ? new Date(value).getTime() || 0 : 0
}

function sortEvents(events: ClusterEvent[]): ClusterEvent[] {
  return [...events].sort((a, b) => {
    const byTime = eventTime(b) - eventTime(a)
    if (byTime !== 0) return byTime
    return b.id - a.id
  })
}

function orderedTimelineEvents(res: { events: ClusterEvent[]; read_model_version_id?: string | null; scope_key?: string | null }): ClusterEvent[] {
  if (res.read_model_version_id && res.scope_key) {
    return [...res.events]
  }
  return sortEvents(res.events)
}

function timelineTimezoneOffsetMinutesKey(value: number): string {
  return `tz=${Number.isFinite(value) ? Math.trunc(value) : 0}`
}

function cacheKey(categories: string[], timezoneOffsetMinutes: number): string {
  return `${timelineTimezoneOffsetMinutesKey(timezoneOffsetMinutes)}|${filterKey(categories)}`
}

function cacheFirstPage(categories: string[], timezoneOffsetMinutes: number, entry: FirstPageCacheEntry) {
  _firstPageCache.set(cacheKey(categories, timezoneOffsetMinutes), {
    ...entry,
    events: [...entry.events],
    dateCounts: { ...entry.dateCounts },
  })
}

function getCachedFirstPage(categories: string[], timezoneOffsetMinutes: number): FirstPageCacheEntry | null {
  const cached = _firstPageCache.get(cacheKey(categories, timezoneOffsetMinutes))
  return cached ? { ...cached, events: [...cached.events], dateCounts: { ...cached.dateCounts } } : null
}

function timelineTimezoneOffsetMinutes(): number {
  return new Date().getTimezoneOffset()
}

function degradedEmptyEvents(res: FeedEventsResponse): boolean {
  return res.degraded === true && res.events.length === 0
}

function recentFallbackSince(): string {
  const date = new Date()
  date.setDate(date.getDate() - 7)
  return date.toISOString()
}

async function fetchEventsWithDegradedFallback(params: Parameters<typeof fetchEvents>[0]): Promise<FeedEventsResponse> {
  const res = await fetchEvents(params)
  if (!degradedEmptyEvents(res) || params?.fetchedSince) return res
  return fetchEvents({
    ...params,
    fetchedSince: recentFallbackSince(),
  })
}

export const useEventsStore = create<EventsState>((set, get) => ({
  enabled: null,
  events: [],
  dateCounts: {},
  cursor: null,
  snapshotVersion: null,
  newSinceCount: 0,
  loading: false,
  error: null,
  refreshing: false,
  refreshHint: null,
  filters: { categories: [] },
  searchQuery: '',
  searchResults: null,
  searchTotal: 0,
  searching: false,
  searchDegraded: false,

  init: async () => {
    if (get().loading) return
    if (get().enabled !== null && get().events.length > 0) return
    const { filters } = get()
    const timezoneOffsetMinutes = timelineTimezoneOffsetMinutes()
    const cached = getCachedFirstPage(filters.categories, timezoneOffsetMinutes)
    if (cached) {
      set({
        enabled: cached.enabled,
        events: cached.events,
        dateCounts: cached.dateCounts,
        cursor: cached.cursor,
        snapshotVersion: cached.snapshotVersion,
        newSinceCount: 0,
      })
    }
    set({ loading: true, error: null })
    try {
      const res = await fetchEventsWithDegradedFallback({
        page: 1,
        limit: 20,
        categories: filters.categories,
        timezoneOffsetMinutes,
      })
      const events = orderedTimelineEvents(res)
      const snap = events[0]?.id ?? null
      cacheFirstPage(filters.categories, timezoneOffsetMinutes, {
        enabled: res.enabled,
        events,
        dateCounts: res.date_counts ?? {},
        cursor: res.next_cursor,
        snapshotVersion: snap,
      })
      set({
        enabled: res.enabled,
        events,
        dateCounts: res.date_counts ?? {},
        cursor: res.next_cursor,
        snapshotVersion: snap,
        newSinceCount: 0,  // init 时不统计（刚锁定 snapshot）
        loading: false,
        refreshHint: null,
      })
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to load events'
      // UX-2(B8): 加载失败 ≠ 功能未启用——enabled 置 null 保留"未知",由
      // 组件渲染错误态+重试;原实现置 false 导致组件返回 null 整页静默空白
      set({ loading: false, error: msg, enabled: null })
    }
  },

  loadMore: async () => {
    const { cursor, loading, events, filters } = get()
    if (!cursor || loading) return
    set({ loading: true })
    try {
      const res = await fetchEvents({
        page: cursor,
        limit: 20,
        categories: filters.categories,
        timezoneOffsetMinutes: timelineTimezoneOffsetMinutes(),
      })
      // dedup by id（防后台 merge 导致重复）
      const existingIds = new Set(events.map((e) => e.id))
      const fresh = res.events.filter((e) => !existingIds.has(e.id))
      const nextEvents = res.read_model_version_id && res.scope_key
        ? [...events, ...fresh]
        : sortEvents([...events, ...fresh])
      const currentDateCounts = get().dateCounts
      const hasLockedDateCounts = Object.keys(currentDateCounts).length > 0
      set({
        events: nextEvents,
        dateCounts: hasLockedDateCounts ? currentDateCounts : (res.date_counts ?? currentDateCounts),
        cursor: res.next_cursor,
        loading: false,
      })
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to load more'
      set({ loading: false, error: msg })
    }
  },

  refresh: async () => {
    if (get().refreshing) return
    set({ refreshing: true, error: null })
    try {
      const { filters, snapshotVersion: previousSnapshotVersion } = get()
      const timezoneOffsetMinutes = timelineTimezoneOffsetMinutes()
      const res = await fetchEventsWithDegradedFallback({
        page: 1,
        limit: 20,
        categories: filters.categories,
        timezoneOffsetMinutes,
      })
      const events = orderedTimelineEvents(res)
      const snap = events[0]?.id ?? null
      cacheFirstPage(filters.categories, timezoneOffsetMinutes, {
        enabled: res.enabled,
        events,
        dateCounts: res.date_counts ?? {},
        cursor: res.next_cursor,
        snapshotVersion: snap,
      })
      set({
        enabled: res.enabled,
        events,
        dateCounts: res.date_counts ?? {},
        cursor: res.next_cursor,
        snapshotVersion: snap,
        newSinceCount: 0,
        refreshing: false,
        refreshHint: snap === previousSnapshotVersion ? '已是最新' : null,
      })
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Refresh failed'
      set({ refreshing: false, error: msg })
    }
  },

  clearRefreshHint: () => {
    set({ refreshHint: null })
  },

  setFilters: async (filters) => {
    // v17.0: 设置筛选后重新 fetch（从 page 1 起）。性能修复：
    // 保留旧结果或立即展示缓存，避免 pill 切换回到整块 skeleton。
    const { searchQuery } = get()
    const seq = ++_filterSeq
    const timezoneOffsetMinutes = timelineTimezoneOffsetMinutes()
    const cached = getCachedFirstPage(filters.categories, timezoneOffsetMinutes)
    set({
      filters,
      ...(cached
        ? {
            enabled: cached.enabled,
            events: cached.events,
            dateCounts: cached.dateCounts,
            cursor: cached.cursor,
            snapshotVersion: cached.snapshotVersion,
          }
        : {}),
      newSinceCount: 0,
      refreshHint: null,
      loading: true,
      error: null,
    })
    try {
      const res = await fetchEventsWithDegradedFallback({
        page: 1,
        limit: 20,
        categories: filters.categories,
        timezoneOffsetMinutes,
      })
      if (seq !== _filterSeq) return
      const events = orderedTimelineEvents(res)
      const snap = events[0]?.id ?? null
      cacheFirstPage(filters.categories, timezoneOffsetMinutes, {
        enabled: res.enabled,
        events,
        dateCounts: res.date_counts ?? {},
        cursor: res.next_cursor,
        snapshotVersion: snap,
      })
      set({
        enabled: res.enabled,
        events,
        dateCounts: res.date_counts ?? {},
        cursor: res.next_cursor,
        snapshotVersion: snap,
        loading: false,
      })
    } catch (e) {
      if (seq !== _filterSeq) return
      const msg = e instanceof Error ? e.message : 'Filter fetch failed'
      set({ loading: false, error: msg })
    }
    // v17.0: 切 pill 时若搜索激活,重新跑搜索（让结果立即反映新 categories）
    if (searchQuery && searchQuery.trim()) {
      void get().searchClusters(searchQuery)
    }
  },

  searchClusters: async (query) => {
    if (_searchTimer) clearTimeout(_searchTimer)
    const q = query.trim()
    const seq = ++_searchSeq
    set({ searchQuery: query })
    if (!q) {
      set({ searchResults: null, searchTotal: 0, searching: false, searchDegraded: false })
      return
    }
    set({ searching: true })
    _searchTimer = setTimeout(async () => {
      // v17.0: 精选 tab 搜索叠加 pill 筛选 — 传入当前 filters.categories
      const { filters } = get()
      try {
        const res = await searchRecommend(q, 30, { categories: filters.categories, eventsOnly: true })
        if (seq !== _searchSeq) return
        if (res.degraded) {
          // BF-0704-6: 降级时保留旧结果并显式提示,不发布假"无结果"
          set({ searching: false, searchDegraded: true })
          return
        }
        set({
          searchResults: sortEvents(res.events),
          searchTotal: res.events_total,
          searching: false,
          searchDegraded: false,
        })
      } catch {
        if (seq !== _searchSeq) return
        set({ searching: false, searchDegraded: true })
      }
    }, 300)
  },

  clearSearch: () => {
    _searchSeq += 1
    if (_searchTimer) clearTimeout(_searchTimer)
    set({ searchQuery: '', searchResults: null, searchTotal: 0, searching: false, searchDegraded: false })
  },

  markSeen: (clusterId) => {
    // 乐观更新本地 events / searchResults：
    //   has_update=false, last_seen_version=live_version
    const { events, searchResults } = get()
    const updateOne = (e: ClusterEvent): ClusterEvent =>
      e.id === clusterId
        ? { ...e, has_update: false, last_seen_version: e.live_version }
        : e
    set({
      events: events.map(updateOne),
      searchResults: searchResults ? searchResults.map(updateOne) : searchResults,
    })
    // fire-and-forget 后端写入；失败不影响渲染（R7.1 验收）
    markClusterSeen(clusterId).catch(() => {
      /* swallow per feature-spec R7.1 — backend write is best-effort */
    })
  },

  reset: () => {
    _firstPageCache.clear()
    _filterSeq += 1

    set({
      enabled: null,
      events: [],
      dateCounts: {},
      cursor: null,
      snapshotVersion: null,
      newSinceCount: 0,
      loading: false,
      error: null,
      refreshing: false,
      refreshHint: null,
      filters: { categories: [] },
      searchQuery: '',
      searchResults: null,
      searchTotal: 0,
      searching: false,
    })
  },
}))
