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
import { fetchEvents, searchRecommend } from '../lib/api'
import { getBrowseableHighlightDates } from '../lib/highlightsDates'

type ReadModelCursor = Exclude<FeedEventsCursor, number | null>

type NewerFeedback = { requestId: number; targetDate: string } & (
  | { status: 'loading' }
  | { status: 'success'; count: number }
  | { status: 'error'; message: string }
)

interface EventsState {
  enabled: boolean | null  // null = 尚未拉过，true/false = 拉过后的 flag
  events: ClusterEvent[]
  /** YYYY-MM-DD -> full filtered event count, independent of loaded pages */
  dateCounts: Record<string, number>
  /** Latest complete, unfiltered day-count snapshot; missing dates remain unknown. */
  allDateCounts: Record<string, number>
  /** Proven day heads present in the current content window, independent of DOM groups. */
  dateAnchors: Record<string, number>
  timelineStartDate: string | null
  navigation: TimelineNavigation | null
  /** Head snapshot identity survives reaching the end (cursor=null). */
  headVersionCursor: ReadModelCursor | null
  loadingNewer: boolean
  newerError: string | null
  /** Written with the request result so fast responses still receive visible feedback. */
  newerFeedback: NewerFeedback | null
  /** Only increments for an atomic prepend; lets the view preserve its old reading anchor. */
  prependVersion: number
  cursor: FeedEventsCursor
  /** snapshot 基线：第一次 load 时取首条 event.id，用于后续 since_version_snapshot */
  snapshotVersion: number | null
  newSinceCount: number
  loading: boolean
  /** Only the current category request; cached content may remain visible meanwhile. */
  filtering: boolean
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
  readSyncErrors: Record<number, boolean>
  readSnapshots: Record<number, ReadStatusSnapshot>
  readMutationVersion: number

  init: () => Promise<void>
  loadMore: () => Promise<void>
  loadNewerDay: () => Promise<void>
  cancelNewerDay: () => void
  clearNewerFeedback: (requestId: number) => void
  seekToCursor: (cursor: FeedEventsCursor) => Promise<void>
  seekToDate: (date: string) => Promise<void>
  backToLatest: () => Promise<void>
  finishNavigation: (requestId: number) => void
  refresh: (navigateToLatest?: boolean) => Promise<void>
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
  markSeen: (clusterId: number, rollbackable?: boolean) => void
  confirmSeen: (clusterId: number) => void
  rollbackSeen: (clusterId: number) => void
  applyReadStatuses: (statuses: Array<{ cluster_id: number; clicked_at: string | null; last_seen_version: number | null }>) => void
  /** 清 cache 用（例如登出） */
  reset: () => void
}

interface ReadStatusSnapshot {
  clicked_at?: string | null
  last_seen_version?: number | null
  has_update: boolean
}

interface TimelineNavigation {
  requestId: number
  kind: 'date' | 'latest'
  date: string | null
  status: 'loading' | 'ready' | 'error'
  anchorId: number | null
  error: string | null
}

let _searchTimer: ReturnType<typeof setTimeout> | null = null
let _searchSeq = 0
let _requestSeq = 0
let _filterRequestSeq: number | null = null
let _lastPageTailDate: string | null = null

interface FirstPageCacheEntry {
  enabled: boolean
  events: ClusterEvent[]
  dateCounts: Record<string, number>
  dateAnchors: Record<string, number>
  pageTailDate: string | null
  cursor: FeedEventsCursor
  snapshotVersion: number | null
  headVersionCursor: ReadModelCursor | null
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
    dateAnchors: { ...entry.dateAnchors },
  })
}

function getCachedFirstPage(categories: string[], timezoneOffsetMinutes: number): FirstPageCacheEntry | null {
  const cached = _firstPageCache.get(cacheKey(categories, timezoneOffsetMinutes))
  return cached ? { ...cached, events: [...cached.events], dateCounts: { ...cached.dateCounts }, dateAnchors: { ...cached.dateAnchors } } : null
}

const BEIJING_TIMELINE_TIMEZONE_OFFSET_MINUTES = -480

function timelineTimezoneOffsetMinutes(): number {
  return BEIJING_TIMELINE_TIMEZONE_OFFSET_MINUTES
}

function eventDateKey(event: ClusterEvent | undefined): string | null {
  const value = event?.first_doc_at || event?.last_doc_at
  if (!value) return null
  const stamp = new Date(value).getTime()
  return Number.isFinite(stamp)
    ? new Date(stamp - BEIJING_TIMELINE_TIMEZONE_OFFSET_MINUTES * 60_000).toISOString().slice(0, 10)
    : null
}

function pageDateAnchors(events: ClusterEvent[], firstIsHead = false, previousDate: string | null = null): Record<string, number> {
  const anchors: Record<string, number> = {}
  events.forEach((event, index) => {
    const date = eventDateKey(event)
    if (date && ((index === 0 && firstIsHead) || (previousDate && previousDate > date)) && anchors[date] == null) {
      anchors[date] = event.id
    }
    previousDate = date
  })
  return anchors
}

function rememberPageTail(events: ClusterEvent[]) {
  _lastPageTailDate = eventDateKey(events[events.length - 1])
}

function degradedEmptyEvents(res: FeedEventsResponse): boolean {
  return res.degraded === true && res.events.length === 0
}

function recentFallbackSince(): string {
  const date = new Date()
  date.setDate(date.getDate() - 7)
  return date.toISOString()
}

async function fetchEventsWithDegradedFallback(params: Parameters<typeof fetchEvents>[0]): Promise<{ res: FeedEventsResponse; fullScope: boolean }> {
  const res = await fetchEvents(params)
  if (!degradedEmptyEvents(res) || params?.fetchedSince) return { res, fullScope: !params?.fetchedSince }
  const fallback = await fetchEvents({
    ...params,
    fetchedSince: recentFallbackSince(),
  })
  // Recent-fetch filtering can omit any day's head and breaks normal-page continuity.
  return { res: fallback, fullScope: false }
}

function allDayCounts(res: FeedEventsResponse, categories: string[], current: Record<string, number>, fullScope = true): Record<string, number> {
  if (categories.length || !fullScope || res.degraded || !res.date_counts || (res.scope_key && res.scope_key !== 'all')) return current
  return { ...res.date_counts }
}

function headVersionCursor(res: FeedEventsResponse): ReadModelCursor | null {
  return res.read_model_version_id && res.scope_key
    ? { version_id: res.read_model_version_id, scope_key: res.scope_key, rank_after: 0 }
    : null
}

export const useEventsStore = create<EventsState>((set, get) => ({
  enabled: null,
  events: [],
  dateCounts: {},
  allDateCounts: {},
  dateAnchors: {},
  timelineStartDate: null,
  navigation: null,
  headVersionCursor: null,
  loadingNewer: false,
  newerError: null,
  newerFeedback: null,
  prependVersion: 0,
  cursor: null,
  snapshotVersion: null,
  newSinceCount: 0,
  loading: false,
  filtering: false,
  error: null,
  refreshing: false,
  refreshHint: null,
  filters: { categories: [] },
  searchQuery: '',
  searchResults: null,
  searchTotal: 0,
  searching: false,
  searchDegraded: false,
  readSyncErrors: {},
  readSnapshots: {},
  readMutationVersion: 0,

  init: async () => {
    if (get().loading) return
    if (get().enabled !== null && get().events.length > 0) return
    const requestId = ++_requestSeq
    const { filters } = get()
    const timezoneOffsetMinutes = timelineTimezoneOffsetMinutes()
    const cached = getCachedFirstPage(filters.categories, timezoneOffsetMinutes)
    if (cached) {
      _lastPageTailDate = cached.pageTailDate
      set({
        enabled: cached.enabled,
        events: cached.events,
        dateCounts: cached.dateCounts,
        dateAnchors: cached.dateAnchors,
        timelineStartDate: null,
        cursor: cached.cursor,
        snapshotVersion: cached.snapshotVersion,
        headVersionCursor: cached.headVersionCursor,
        newSinceCount: 0,
      })
    }
    set({ loading: true, filtering: false, refreshing: false, loadingNewer: false, newerError: null, newerFeedback: null, error: null, navigation: null })
    try {
      const { res, fullScope } = await fetchEventsWithDegradedFallback({
        page: 1,
        limit: 20,
        categories: filters.categories,
        timezoneOffsetMinutes,
      })
      if (requestId !== _requestSeq) return
      const events = orderedTimelineEvents(res)
      rememberPageTail(fullScope ? res.events : [])
      const snap = events[0]?.id ?? null
      cacheFirstPage(filters.categories, timezoneOffsetMinutes, {
        enabled: res.enabled,
        events,
        dateCounts: res.date_counts ?? {},
        dateAnchors: fullScope ? pageDateAnchors(res.events, true) : {},
        pageTailDate: _lastPageTailDate,
        cursor: res.next_cursor,
        snapshotVersion: snap,
        headVersionCursor: headVersionCursor(res),
      })
      set({
        enabled: res.enabled,
        events,
        dateCounts: res.date_counts ?? {},
        allDateCounts: allDayCounts(res, filters.categories, get().allDateCounts, fullScope),
        dateAnchors: fullScope ? pageDateAnchors(res.events, true) : {},
        timelineStartDate: null,
        cursor: res.next_cursor,
        snapshotVersion: snap,
        headVersionCursor: headVersionCursor(res),
        newSinceCount: 0,  // init 时不统计（刚锁定 snapshot）
        loading: false,
        refreshHint: null,
      })
    } catch (e) {
      if (requestId !== _requestSeq) return
      const msg = e instanceof Error ? e.message : 'Failed to load events'
      // UX-2(B8): 加载失败 ≠ 功能未启用——enabled 置 null 保留"未知",由
      // 组件渲染错误态+重试;原实现置 false 导致组件返回 null 整页静默空白
      set({ loading: false, error: msg, enabled: null })
    }
  },

  loadMore: async () => {
    const { cursor, loading, loadingNewer, refreshing, navigation, filters, searchQuery, searching } = get()
    if (!cursor || loading || loadingNewer || refreshing || searchQuery.trim() || searching || (navigation && navigation.status !== 'error')) return
    const requestId = _requestSeq
    set({ loading: true })
    try {
      const res = await fetchEvents({
        page: cursor,
        limit: 20,
        categories: filters.categories,
        timezoneOffsetMinutes: timelineTimezoneOffsetMinutes(),
      })
      if (requestId !== _requestSeq) return
      const { events, dateAnchors, timelineStartDate } = get()
      // A different read-model version cannot prove continuity at this page boundary.
      const sameVersion = typeof cursor !== 'object' || cursor.version_id === res.read_model_version_id
      const nextAnchors = { ...pageDateAnchors(res.events, false, sameVersion ? _lastPageTailDate : null), ...dateAnchors }
      rememberPageTail(res.events)
      // dedup by id（防后台 merge 导致重复）
      const existingIds = new Set(events.map((e) => e.id))
      const fresh = res.events.filter((e) => !existingIds.has(e.id))
      const nextEvents = timelineStartDate || (res.read_model_version_id && res.scope_key)
        ? [...events, ...fresh]
        : sortEvents([...events, ...fresh])
      const currentDateCounts = get().dateCounts
      const hasLockedDateCounts = Object.keys(currentDateCounts).length > 0
      const allDateCounts = get().allDateCounts
      set({
        events: nextEvents,
        dateCounts: hasLockedDateCounts ? currentDateCounts : (res.date_counts ?? currentDateCounts),
        allDateCounts: Object.keys(allDateCounts).length ? allDateCounts : allDayCounts(res, filters.categories, allDateCounts),
        dateAnchors: nextAnchors,
        cursor: res.next_cursor,
        loading: false,
      })
    } catch (e) {
      if (requestId !== _requestSeq) return
      const msg = e instanceof Error ? e.message : 'Failed to load more'
      set({ loading: false, error: msg })
    }
  },

  loadNewerDay: async () => {
    const state = get()
    if (state.loading || state.loadingNewer || state.refreshing || state.filtering || state.searchQuery.trim() || state.searching
      || (state.navigation && state.navigation.status !== 'error')) return
    const oldHead = state.events[0]
    const oldDate = eventDateKey(oldHead)
    if (!oldHead || !oldDate) return
    const targetDate = getBrowseableHighlightDates(state.dateCounts).reverse().find((date) => date > oldDate)
    if (!targetDate) return
    const requestId = ++_requestSeq
    if (state.dateAnchors[oldDate] !== oldHead.id) {
      const message = '当前日期起点不完整，请先在时间线重新选择当前日期，再向上加载'
      set({ newerError: message, newerFeedback: { requestId, targetDate, status: 'error', message } })
      return
    }
    const pinned = state.headVersionCursor ?? (typeof state.cursor === 'object' ? state.cursor : null)
    const categories = [...state.filters.categories]
    const limit = 100 // Existing API limit; normal downward pagination remains 20.
    const continuityError = '日期内容已变化，未改变当前阅读位置。请重试；仍失败可重新选择当前日期'
    set({ loadingNewer: true, newerError: null, newerFeedback: { requestId, targetDate, status: 'loading' }, navigation: null })
    try {
      let result = await fetchEvents({ targetDate, ...(pinned ? { page: pinned } : {}), limit, categories, timezoneOffsetMinutes: timelineTimezoneOffsetMinutes() })
      if (requestId !== _requestSeq) return
      const seek = result.date_seek
      const anchorIndex = seek?.status === 'found' ? result.events.findIndex((event) => event.id === seek.anchor_event_id) : -1
      if (seek?.requested_date !== targetDate || anchorIndex < 0 || eventDateKey(result.events[anchorIndex]) !== targetDate) throw new Error(continuityError)
      const expectedCount = result.date_counts?.[targetDate] ?? state.dateCounts[targetDate]
      if (!Number.isSafeInteger(expectedCount) || expectedCount <= 0) throw new Error(continuityError)
      const expectedVersion = pinned ?? headVersionCursor(result)
      const staged: ClusterEvent[] = []
      const stagedIds = new Set<number>()
      const retainedIds = new Set(state.events.map((event) => event.id))
      const seenCursors = new Set<string>()
      let previousCursor: FeedEventsCursor = null
      // One extra partial first page and one boundary page are sufficient. Never loop on an unstable cursor.
      const maxPages = Math.ceil(expectedCount / limit) + 2
      for (let pageIndex = 0; pageIndex < maxPages; pageIndex += 1) {
        if (requestId !== _requestSeq) return
        const actualVersion = headVersionCursor(result)
        if (result.degraded || !result.enabled || (expectedVersion
          ? actualVersion?.version_id !== expectedVersion.version_id || actualVersion?.scope_key !== expectedVersion.scope_key
          : actualVersion !== null)) throw new Error(continuityError)
        const rows = pageIndex === 0 ? result.events.slice(anchorIndex) : result.events
        let reachedOldHead = false
        for (const item of rows) {
          if (item.id === oldHead.id && eventDateKey(item) === oldDate) { reachedOldHead = true; break }
          if (eventDateKey(item) !== targetDate || stagedIds.has(item.id) || retainedIds.has(item.id)) throw new Error(continuityError)
          stagedIds.add(item.id)
          staged.push(item)
        }
        if (reachedOldHead) {
          const current = get()
          if (staged.length !== expectedCount || current.events[0]?.id !== oldHead.id) throw new Error(continuityError)
          set({
            events: [...staged, ...current.events],
            dateAnchors: { ...current.dateAnchors, [targetDate]: staged[0].id },
            timelineStartDate: targetDate,
            headVersionCursor: expectedVersion,
            loadingNewer: false, newerError: null, prependVersion: current.prependVersion + 1,
            newerFeedback: { requestId, targetDate, status: 'success', count: staged.length },
          })
          // Preserve downward cursor, page tail, locked counts and the existing first-page cache.
          return
        }
        if (staged.length > expectedCount || !rows.length || !result.next_cursor) throw new Error(continuityError)
        const cursorKey = JSON.stringify(result.next_cursor)
        if (seenCursors.has(cursorKey)) throw new Error(continuityError)
        const next = result.next_cursor
        if (typeof next === 'number'
          ? !Number.isSafeInteger(next) || next < 2
          : !Number.isSafeInteger(next.rank_after) || Number(next.rank_after) < 1) throw new Error(continuityError)
        if (previousCursor && (typeof previousCursor === 'number'
          ? typeof next !== 'number' || next <= previousCursor
          : typeof next !== 'object' || next.version_id !== previousCursor.version_id
            || next.scope_key !== previousCursor.scope_key || Number(next.rank_after) <= Number(previousCursor.rank_after))) throw new Error(continuityError)
        seenCursors.add(cursorKey)
        previousCursor = next
        result = await fetchEvents({ page: next, limit, categories, timezoneOffsetMinutes: timelineTimezoneOffsetMinutes() })
      }
      throw new Error(continuityError)
    } catch (error) {
      if (requestId !== _requestSeq) return
      const message = error instanceof Error ? error.message : '加载失败，请重试'
      set({ loadingNewer: false, newerError: message, newerFeedback: { requestId, targetDate, status: 'error', message } })
    }
  },

  cancelNewerDay: () => {
    if (get().loadingNewer) _requestSeq += 1
    if (get().loadingNewer || get().newerError || get().newerFeedback) set({ loadingNewer: false, newerError: null, newerFeedback: null })
  },

  clearNewerFeedback: (requestId) => {
    const feedback = get().newerFeedback
    // Expiring an old success must never dismiss a newer request or a retryable error.
    if (feedback?.requestId === requestId && feedback.status === 'success') set({ newerFeedback: null })
  },

  seekToCursor: async (cursor) => {
    if (!cursor) return
    const requestId = ++_requestSeq
    set({ loading: true, filtering: false, refreshing: false, loadingNewer: false, newerError: null, newerFeedback: null, error: null, navigation: null })
    try {
      const res = await fetchEvents({ page: cursor, limit: 20, categories: [], timezoneOffsetMinutes: timelineTimezoneOffsetMinutes() })
      if (requestId !== _requestSeq) return
      const ids = new Set(get().events.map((event) => event.id))
      const events = [...get().events, ...res.events.filter((event) => !ids.has(event.id))]
      const firstIsHead = cursor === 1 || (typeof cursor === 'object' && cursor.rank_after === 0)
      const dateAnchors = { ...pageDateAnchors(res.events, firstIsHead), ...get().dateAnchors }
      rememberPageTail(res.events)
      set({ events: orderedTimelineEvents({ ...res, events }), dateAnchors, cursor: res.next_cursor, loading: false })
    } catch (error) {
      if (requestId !== _requestSeq) return
      set({ loading: false, error: error instanceof Error ? error.message : 'Failed to seek events' })
    }
  },

  seekToDate: async (date) => {
    const { filters, searchQuery, searching, dateAnchors, events } = get()
    if (searchQuery.trim() || searching || _filterRequestSeq === _requestSeq) return
    const requestId = ++_requestSeq
    const anchorId = dateAnchors[date]
    const loadedAnchor = anchorId != null && events.some((event) => event.id === anchorId && eventDateKey(event) === date)
    const navigation: TimelineNavigation = { requestId, kind: 'date', date, status: loadedAnchor ? 'ready' : 'loading', anchorId: loadedAnchor ? anchorId : null, error: null }
    set({ navigation, loading: !loadedAnchor, filtering: false, refreshing: false, loadingNewer: false, newerError: null, newerFeedback: null, error: null })
    if (loadedAnchor) return
    try {
      // Date lookups keep the original scope; no recent-fetch degraded fallback.
      const res = await fetchEvents({ targetDate: date, limit: 20, categories: filters.categories, timezoneOffsetMinutes: timelineTimezoneOffsetMinutes() })
      if (requestId !== _requestSeq) return
      const seek = res.date_seek
      if (!seek || seek.requested_date !== date) throw new Error('日期定位结果异常，请重试')
      if (seek.status === 'not_found') throw new Error('该日期暂无可浏览内容，请选择其他日期或重试')
      const index = res.events.findIndex((event) => event.id === seek.anchor_event_id)
      if (seek.status !== 'found' || index < 0 || eventDateKey(res.events[index]) !== date) throw new Error('日期定位结果异常，请重试')
      const nextEvents = res.events.slice(index)
      rememberPageTail(nextEvents)
      set({
        enabled: res.enabled, events: nextEvents, dateCounts: res.date_counts ?? get().dateCounts,
        allDateCounts: allDayCounts(res, filters.categories, get().allDateCounts),
        dateAnchors: pageDateAnchors(nextEvents, true), timelineStartDate: date,
        headVersionCursor: headVersionCursor(res),
        cursor: res.next_cursor, loading: false, refreshHint: null,
        navigation: { ...navigation, status: 'ready', anchorId: seek.anchor_event_id },
      })
    } catch (error) {
      if (requestId !== _requestSeq) return
      set({ loading: false, navigation: { ...navigation, status: 'error', error: error instanceof Error ? error.message : '日期加载失败，请重试' } })
    }
  },

  backToLatest: async () => {
    if (get().searchQuery.trim() || get().searching || _filterRequestSeq === _requestSeq) return
    await get().refresh(true)
  },

  finishNavigation: (requestId) => {
    const navigation = get().navigation
    if (!navigation || navigation.requestId !== requestId) return
    if (navigation.status === 'loading') _requestSeq += 1
    set(navigation.status === 'error'
      ? { navigation: null }
      : { navigation: null, loading: false, filtering: false, refreshing: false })
  },

  refresh: async (navigateToLatest = false) => {
    if (get().refreshing && !navigateToLatest) return
    const requestId = ++_requestSeq
    const navigation: TimelineNavigation | null = navigateToLatest
      ? { requestId, kind: 'latest', date: null, status: 'loading', anchorId: null, error: null }
      : null
    set({ refreshing: true, loading: false, filtering: false, loadingNewer: false, newerError: null, newerFeedback: null, error: null, navigation })
    try {
      const { filters, snapshotVersion: previousSnapshotVersion } = get()
      const timezoneOffsetMinutes = timelineTimezoneOffsetMinutes()
      const { res, fullScope } = await fetchEventsWithDegradedFallback({
        page: 1,
        limit: 20,
        categories: filters.categories,
        timezoneOffsetMinutes,
      })
      if (requestId !== _requestSeq) return
      if (navigateToLatest && degradedEmptyEvents(res)) throw new Error('最新内容暂时不可用，请重试')
      const events = orderedTimelineEvents(res)
      rememberPageTail(fullScope ? res.events : [])
      const snap = events[0]?.id ?? null
      cacheFirstPage(filters.categories, timezoneOffsetMinutes, {
        enabled: res.enabled,
        events,
        dateCounts: res.date_counts ?? {},
        dateAnchors: fullScope ? pageDateAnchors(res.events, true) : {},
        pageTailDate: _lastPageTailDate,
        cursor: res.next_cursor,
        snapshotVersion: snap,
        headVersionCursor: headVersionCursor(res),
      })
      set({
        enabled: res.enabled,
        events,
        dateCounts: res.date_counts ?? {},
        allDateCounts: allDayCounts(res, filters.categories, get().allDateCounts, fullScope),
        dateAnchors: fullScope ? pageDateAnchors(res.events, true) : {},
        timelineStartDate: null,
        navigation: navigation ? { ...navigation, status: 'ready' } : null,
        cursor: res.next_cursor,
        snapshotVersion: snap,
        headVersionCursor: headVersionCursor(res),
        newSinceCount: 0,
        refreshing: false,
        refreshHint: snap === previousSnapshotVersion ? '已是最新' : null,
      })
    } catch (e) {
      if (requestId !== _requestSeq) return
      const msg = e instanceof Error ? e.message : 'Refresh failed'
      set({ refreshing: false, error: msg, navigation: navigation ? { ...navigation, status: 'error', error: msg } : null })
    }
  },

  clearRefreshHint: () => {
    set({ refreshHint: null })
  },

  setFilters: async (filters) => {
    // v17.0: 设置筛选后重新 fetch（从 page 1 起）。性能修复：
    // 保留旧结果或立即展示缓存，避免 pill 切换回到整块 skeleton。
    const seq = ++_requestSeq
    _filterRequestSeq = seq
    _searchSeq += 1
    if (_searchTimer) clearTimeout(_searchTimer)
    const timezoneOffsetMinutes = timelineTimezoneOffsetMinutes()
    const cached = getCachedFirstPage(filters.categories, timezoneOffsetMinutes)
    _lastPageTailDate = cached?.pageTailDate ?? null
    set({
      filters,
      filtering: true,
      dateAnchors: cached?.dateAnchors ?? {},
      dateCounts: cached?.dateCounts ?? {},
      cursor: cached?.cursor ?? null,
      timelineStartDate: null,
      navigation: null,
      headVersionCursor: cached?.headVersionCursor ?? null,
      loadingNewer: false,
      newerError: null,
      newerFeedback: null,
      refreshing: false,
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
      const { res, fullScope } = await fetchEventsWithDegradedFallback({
        page: 1,
        limit: 20,
        categories: filters.categories,
        timezoneOffsetMinutes,
      })
      if (seq !== _requestSeq) return
      _filterRequestSeq = null
      const events = orderedTimelineEvents(res)
      rememberPageTail(fullScope ? res.events : [])
      const snap = events[0]?.id ?? null
      cacheFirstPage(filters.categories, timezoneOffsetMinutes, {
        enabled: res.enabled,
        events,
        dateCounts: res.date_counts ?? {},
        dateAnchors: fullScope ? pageDateAnchors(res.events, true) : {},
        pageTailDate: _lastPageTailDate,
        cursor: res.next_cursor,
        snapshotVersion: snap,
        headVersionCursor: headVersionCursor(res),
      })
      set({
        enabled: res.enabled,
        events,
        dateCounts: res.date_counts ?? {},
        allDateCounts: allDayCounts(res, filters.categories, get().allDateCounts, fullScope),
        dateAnchors: fullScope ? pageDateAnchors(res.events, true) : {},
        cursor: res.next_cursor,
        snapshotVersion: snap,
        headVersionCursor: headVersionCursor(res),
        loading: false,
        filtering: false,
      })
    } catch (e) {
      if (seq !== _requestSeq) return
      _filterRequestSeq = null
      const msg = e instanceof Error ? e.message : 'Filter fetch failed'
      set({ loading: false, filtering: false, error: msg })
    }
    // v17.0: 切 pill 时若搜索激活,重新跑搜索（让结果立即反映新 categories）
    const { searchQuery } = get()
    if (searchQuery && searchQuery.trim()) {
      void get().searchClusters(searchQuery)
    }
  },

  searchClusters: async (query) => {
    get().cancelNewerDay()
    const navigation = get().navigation
    if (navigation) get().finishNavigation(navigation.requestId)
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
    get().cancelNewerDay()
    const navigation = get().navigation
    if (navigation) get().finishNavigation(navigation.requestId)
    _searchSeq += 1
    if (_searchTimer) clearTimeout(_searchTimer)
    set({ searchQuery: '', searchResults: null, searchTotal: 0, searching: false, searchDegraded: false })
  },

  markSeen: (clusterId, rollbackable = true) => {
    // 乐观更新本地 events / searchResults：
    //   has_update=false, last_seen_version=live_version
    const { events, searchResults } = get()
    const updateOne = (e: ClusterEvent): ClusterEvent =>
      e.id === clusterId
        ? { ...e, has_update: false, last_seen_version: e.live_version }
        : e
    const previous = events.find((event) => event.id === clusterId)
      ?? searchResults?.find((event) => event.id === clusterId)
    const snapshot = previous
      ? {
          clicked_at: previous.clicked_at,
          last_seen_version: previous.last_seen_version,
          has_update: previous.has_update,
        }
      : null
    const snapshots = { ...get().readSnapshots }
    if (rollbackable && snapshot) snapshots[clusterId] = snapshot
    else delete snapshots[clusterId]
    set({
      events: events.map(updateOne),
      searchResults: searchResults ? searchResults.map(updateOne) : searchResults,
      readSyncErrors: Object.fromEntries(Object.entries(get().readSyncErrors).filter(([id]) => Number(id) !== clusterId)),
      readSnapshots: snapshots,
      readMutationVersion: get().readMutationVersion + 1,
    })
  },

  confirmSeen: (clusterId) => {
    const snapshots = { ...get().readSnapshots }
    const errors = { ...get().readSyncErrors }
    delete snapshots[clusterId]
    delete errors[clusterId]
    set({
      readSnapshots: snapshots,
      readSyncErrors: errors,
      readMutationVersion: get().readMutationVersion + 1,
    })
  },

  rollbackSeen: (clusterId) => {
    const snapshot = get().readSnapshots[clusterId]
    if (!snapshot) return
    const restore = (event: ClusterEvent) => event.id === clusterId
      ? {
          ...event,
          clicked_at: snapshot.clicked_at,
          last_seen_version: snapshot.last_seen_version,
          has_update: snapshot.has_update,
        }
      : event
    const searchResults = get().searchResults
    const snapshots = { ...get().readSnapshots }
    delete snapshots[clusterId]
    set({
      events: get().events.map(restore),
      searchResults: searchResults ? searchResults.map(restore) : searchResults,
      readSyncErrors: { ...get().readSyncErrors, [clusterId]: true },
      readSnapshots: snapshots,
      readMutationVersion: get().readMutationVersion + 1,
    })
  },

  applyReadStatuses: (statuses) => {
    const byId = new Map(statuses.map((status) => [status.cluster_id, status]))
    const apply = (event: ClusterEvent) => {
      const status = byId.get(event.id)
      if (!status || get().readSnapshots[event.id]) return event
      const clickedAt = status.clicked_at ?? undefined
      const hasUpdate = status.last_seen_version != null && status.last_seen_version < event.live_version
      if (
        event.clicked_at === clickedAt
        && event.last_seen_version === status.last_seen_version
        && event.has_update === hasUpdate
      ) return event
      return {
        ...event,
        clicked_at: clickedAt,
        last_seen_version: status.last_seen_version,
        has_update: hasUpdate,
      }
    }
    const searchResults = get().searchResults
    const events = get().events
    const nextEvents = events.map(apply)
    const nextSearchResults = searchResults ? searchResults.map(apply) : searchResults
    const errors = { ...get().readSyncErrors }
    statuses.forEach((status) => {
      if (status.clicked_at) delete errors[status.cluster_id]
    })
    if (
      nextEvents.every((event, index) => event === events[index])
      && (!searchResults || nextSearchResults?.every((event, index) => event === searchResults[index]))
      && Object.keys(errors).length === Object.keys(get().readSyncErrors).length
    ) return
    set({ events: nextEvents, searchResults: nextSearchResults, readSyncErrors: errors })
  },

  reset: () => {
    _firstPageCache.clear()
    _requestSeq += 1
    _filterRequestSeq = null
    _lastPageTailDate = null
    _searchSeq += 1
    if (_searchTimer) clearTimeout(_searchTimer)
    _searchTimer = null

    set({
      enabled: null,
      events: [],
      dateCounts: {},
      allDateCounts: {},
      dateAnchors: {},
      timelineStartDate: null,
      navigation: null,
      headVersionCursor: null,
      loadingNewer: false,
      newerError: null,
      newerFeedback: null,
      prependVersion: 0,
      cursor: null,
      snapshotVersion: null,
      newSinceCount: 0,
      loading: false,
      filtering: false,
      error: null,
      refreshing: false,
      refreshHint: null,
      filters: { categories: [] },
      searchQuery: '',
      searchResults: null,
      searchTotal: 0,
      searching: false,
      searchDegraded: false,
      readSyncErrors: {},
      readSnapshots: {},
      readMutationVersion: 0,
    })
  },
}))
