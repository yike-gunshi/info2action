/**
 * v15.0 LatestEvents — 时间线容器（DESIGN.md §15.5）
 * v24.0 批次①: 报眉 Scotch rule、日界收束（页面版式保留）。
 * 头版 tier 装配（头条/次条/简讯 + 防翻牌记忆）已退役：全部条目严格按时间倒序平铺。
 *
 * 仅当 enabled=true（event_aggregation_ready=true）时渲染。
 * 容器固定高 450px（移动 360px），内部 overflow-y: auto。
 * 首页支持触底分页，沿后端 next_cursor 持续下拉浏览历史事件窗口。
 *
 * 性能：events 数组用 useMemo 稳定引用（feedback_usememo_stable_array_ref）。
 * 文件只导出 LatestEvents（feedback_react_fast_refresh_no_mixed_export）。
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import type { UIEvent } from 'react'
import { CalendarDays, Loader2 } from 'lucide-react'
import { toast } from 'sonner'
import { useEventsStore } from '../../store/eventsStore'
import { useDailyDigestStore } from '../../store/dailyDigestStore'
import { useClusterDetailStore } from '../../store/clusterDetailStore'
import { useAuthStore } from '../../store/authStore'
import { useUIStore } from '../../store/uiStore'
import { fetchClusterStatuses } from '../../lib/api'
import { EventCard } from './EventCard'
import { DailyDigestStrip } from './DailyDigestStrip'
import { NewerDayFeedback } from './NewerDayFeedback'
import type { ClusterEvent } from '../../lib/types'
import { cn } from '../../lib/utils'
import { getBrowseableHighlightDates } from '../../lib/highlightsDates'

type LatestEventsVariant = 'panel' | 'page'

interface LatestEventsProps {
  /** 移动端用 360px 高，桌面 450px。可外部覆盖 */
  containerHeight?: number
  /** 列表为空且非 loading 时是否展示空态（推荐页 dashboard 用 true，搜索结果区可能用 false） */
  showEmptyState?: boolean
  /** v17.0: 容器内嵌的顶部插槽 — Header 之后、时间线之前。精选 tab 用作 L1 pill bar */
  topSlot?: React.ReactNode
  /** v19: page 为 Image2 精选页开放式时间线；panel 保留旧面板内滚动行为 */
  variant?: LatestEventsVariant
}

function SkeletonCard() {
  return (
    <div data-testid="event-skeleton" className="border-b border-border/70 px-5 py-3.5 sm:px-6 sm:py-4" style={{ minHeight: 120 }}>
      <div className="grid grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-[52px_minmax(0,1fr)_200px] sm:gap-y-0 lg:grid-cols-[56px_minmax(0,1fr)_200px]">
        <div
          className="mt-1 rounded bg-muted"
          style={{ height: 12, animation: 'event-skeleton-shimmer 1.5s linear infinite', backgroundImage: 'linear-gradient(90deg, var(--border) 0%, var(--muted) 50%, var(--border) 100%)', backgroundSize: '200% 100%' }}
        />
        <div className="min-w-0 space-y-3">
          <div
            className="rounded bg-muted"
            style={{ width: '74%', height: 18, animation: 'event-skeleton-shimmer 1.5s linear infinite', backgroundImage: 'linear-gradient(90deg, var(--border) 0%, var(--muted) 50%, var(--border) 100%)', backgroundSize: '200% 100%' }}
          />
          <div
            className="rounded bg-muted"
            style={{ width: '92%', height: 14, animation: 'event-skeleton-shimmer 1.5s linear infinite', backgroundImage: 'linear-gradient(90deg, var(--border) 0%, var(--muted) 50%, var(--border) 100%)', backgroundSize: '200% 100%' }}
          />
          <div
            className="rounded bg-muted"
            style={{ width: '48%', height: 20, animation: 'event-skeleton-shimmer 1.5s linear infinite', backgroundImage: 'linear-gradient(90deg, var(--border) 0%, var(--muted) 50%, var(--border) 100%)', backgroundSize: '200% 100%' }}
          />
        </div>
        <div className="relative hidden h-[120px] w-[200px] justify-self-end self-start sm:block sm:w-[200px] lg:w-[200px]">
          <div
            className="absolute inset-0 aspect-[5/3] h-full w-full rounded-md bg-muted"
            style={{ animation: 'event-skeleton-shimmer 1.5s linear infinite', backgroundImage: 'linear-gradient(90deg, var(--border) 0%, var(--muted) 50%, var(--border) 100%)', backgroundSize: '200% 100%' }}
          />
        </div>
      </div>
    </div>
  )
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center h-full px-6 text-center">
      <p className="text-[13px] text-muted-foreground mb-1">
        近期还没有可展示的聚合事件
      </p>
      <p className="text-[12px] text-muted-foreground/80">
        请浏览下方“为你推荐”或稍后再试
      </p>
    </div>
  )
}

function EndPlaceholder() {
  return (
    <div className="px-4 py-4 text-center text-[12px] text-muted-foreground">
      已展示全部事件
    </div>
  )
}

function LoadMoreHint({ loading }: { loading: boolean }) {
  // BF-0517-3: loading=true 显示旋转 spinner 替代纯文字，给"正在加载"明确动效反馈
  return (
    <div className="px-4 py-4 text-center text-[12px] text-muted-foreground">
      {loading ? (
        <span data-testid="load-more-spinner" className="inline-flex items-center gap-2" role="status" aria-live="polite">
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          <span>加载中…</span>
        </span>
      ) : (
        '继续下拉加载更多事件'
      )}
    </div>
  )
}

interface TimelineGroup {
  key: string
  label: string
  events: ClusterEvent[]
}

const HIGHLIGHTS_TIME_ZONE = 'Asia/Shanghai'
const highlightsDateFormatter = new Intl.DateTimeFormat('en-US', {
  timeZone: HIGHLIGHTS_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})
const highlightsWeekdayFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: HIGHLIGHTS_TIME_ZONE,
  weekday: 'long',
})
const highlightsTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: HIGHLIGHTS_TIME_ZONE,
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
})

interface HighlightsDateParts {
  year: string
  month: string
  day: string
}

function highlightsDateParts(date: Date | null): HighlightsDateParts | null {
  if (!date) return null
  const parts = highlightsDateFormatter.formatToParts(date)
  const year = parts.find((part) => part.type === 'year')?.value
  const month = parts.find((part) => part.type === 'month')?.value
  const day = parts.find((part) => part.type === 'day')?.value
  return year && month && day ? { year, month, day } : null
}

function RefreshInlineSpinner() {
  return (
    <div data-testid="highlights-refresh-spinner" className="flex justify-center pb-3 pt-1 text-[12px] text-muted-foreground">
      <span className="inline-flex items-center gap-2" role="status" aria-live="polite">
        <Loader2 className="h-3.5 w-3.5 animate-spin text-[var(--brand)]" aria-hidden="true" />
        <span>刷新中…</span>
      </span>
    </div>
  )
}

function eventDate(cluster: ClusterEvent): Date | null {
  const value = cluster.first_doc_at || cluster.last_doc_at
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function formatDateKey(date: Date | null): string {
  const parts = highlightsDateParts(date)
  return parts ? `${parts.year}-${parts.month}-${parts.day}` : 'unknown'
}

function formatDateLabel(date: Date | null): string {
  const parts = highlightsDateParts(date)
  return parts ? `${parts.year}.${Number(parts.month)}.${Number(parts.day)}` : '时间未知'
}

function formatWeekday(date: Date | null): string {
  if (!date) return ''
  return highlightsWeekdayFormatter.format(date)
}

function formatEventTime(date: Date | null): string {
  if (!date) return ''
  return highlightsTimeFormatter.format(date)
}

/** §21.2 日界收束: 「· M 月 D 日 共 N 条 ·」mono 12px 居中,两侧 hairline 延伸 */
function DayEndRule({ date, count }: { date: Date | null; count: number }) {
  const parts = highlightsDateParts(date)
  if (!parts) return null
  return (
    <div data-testid="event-day-end" className="flex items-center gap-4 pb-1.5 pt-[22px]">
      <span aria-hidden="true" className="h-px flex-1 bg-border" />
      <span className="font-mono text-[12px] text-muted-foreground">
        {`· ${Number(parts.month)} 月 ${Number(parts.day)} 日 共 ${count} 条 ·`}
      </span>
      <span aria-hidden="true" className="h-px flex-1 bg-border" />
    </div>
  )
}

function groupByDate(events: ClusterEvent[]): TimelineGroup[] {
  const groups: TimelineGroup[] = []
  const indexByKey = new Map<string, number>()

  events.forEach((event) => {
    const date = eventDate(event)
    const key = formatDateKey(date)
    const existingIndex = indexByKey.get(key)
    if (existingIndex == null) {
      indexByKey.set(key, groups.length)
      groups.push({ key, label: formatDateLabel(date), events: [event] })
      return
    }
    groups[existingIndex].events.push(event)
  })

  return groups
}

export function LatestEvents({ containerHeight, showEmptyState = true, topSlot, variant = 'panel' }: LatestEventsProps) {
  const events = useEventsStore((s) => s.events)
  const dateCounts = useEventsStore((s) => s.dateCounts)
  const allDateCounts = useEventsStore((s) => s.allDateCounts)
  const navigation = useEventsStore((s) => s.navigation)
  const highlightsVisible = useUIStore((s) => s.l1 === 'highlights')
  const loading = useEventsStore((s) => s.loading)
  const refreshing = useEventsStore((s) => s.refreshing)
  const loadingNewer = useEventsStore((s) => s.loadingNewer)
  const prependVersion = useEventsStore((s) => s.prependVersion)
  const refreshHint = useEventsStore((s) => s.refreshHint)
  const init = useEventsStore((s) => s.init)
  const loadMore = useEventsStore((s) => s.loadMore)
  const refresh = useEventsStore((s) => s.refresh)
  const clearRefreshHint = useEventsStore((s) => s.clearRefreshHint)
  const cursor = useEventsStore((s) => s.cursor)
  const enabled = useEventsStore((s) => s.enabled)
  const error = useEventsStore((s) => s.error)
  const searchResults = useEventsStore((s) => s.searchResults)
  const searchTotal = useEventsStore((s) => s.searchTotal)
  const searchDegraded = useEventsStore((s) => s.searchDegraded)
  const searching = useEventsStore((s) => s.searching)
  const searchQuery = useEventsStore((s) => s.searchQuery)
  const readSyncErrors = useEventsStore((s) => s.readSyncErrors)
  const applyReadStatuses = useEventsStore((s) => s.applyReadStatuses)
  const loadDailyDigestRange = useDailyDigestStore((s) => s.loadRange)
  const digestsByDate = useDailyDigestStore((s) => s.digestsByDate)
  const digestLoadedRange = useDailyDigestStore((s) => s.loadedRange)
  const user = useAuthStore((s) => s.user)
  const openModal = useClusterDetailStore((s) => s.openModal)
  // FE-7(B7): 稳定回调,避免内联箭头击穿 EventCard 的 memo
  const handleSelectEvent = useCallback(
    (id: number, c: ClusterEvent) => { void openModal(id, c) },
    [openModal],
  )
  const prefetchBundle = useClusterDetailStore((s) => s.prefetchBundle)
  const initRef = useRef(false)
  const isPageVariant = variant === 'page'
  const manualRefreshAtRef = useRef(0)
  const touchStartYRef = useRef<number | null>(null)
  const refreshWasActiveRef = useRef(false)
  const prependAnchorRef = useRef<{ id: string; documentTop: number; version: number } | null>(null)

  // 初次挂载拉数据
  useEffect(() => {
    if (initRef.current) return
    initRef.current = true
    init()
  }, [init])

  const containerRef = useRef<HTMLDivElement>(null)
  // 搜索时用 searchResults 替代 events 渲染（双区独立）
  const isSearchActive = searchResults !== null
  const sourceEvents = isSearchActive ? searchResults : events
  const stableEvents = useMemo(() => sourceEvents ?? [], [sourceEvents])
  const loadedClusterIdsKey = stableEvents.slice(0, 500).map((event) => event.id).join(',')
  const navigating = Boolean(navigation && navigation.status !== 'error')

  const loadNewerDay = useCallback(() => {
    const state = useEventsStore.getState()
    if (state.loadingNewer || state.loading || state.refreshing || state.filtering || state.searchQuery.trim() || state.searching) return
    const cards = [...(containerRef.current?.querySelectorAll<HTMLElement>('[data-cluster-id]') ?? [])]
    const card = cards.find((element) => {
      const rect = element.getBoundingClientRect()
      return rect.bottom > 0 && rect.top < window.innerHeight
    }) ?? cards[0]
    prependAnchorRef.current = card ? {
      id: card.dataset.clusterId!, documentTop: card.getBoundingClientRect().top + window.scrollY, version: state.prependVersion,
    } : null
    void state.loadNewerDay()
  }, [])

  useLayoutEffect(() => {
    const anchor = prependAnchorRef.current
    if (!anchor || loadingNewer) return
    const container = containerRef.current
    if (anchor.version === prependVersion || !container || !isPageVariant || !highlightsVisible || navigating || isSearchActive || searchQuery.trim()) {
      prependAnchorRef.current = null
      return
    }
    let frame = 0
    let stableFrames = 0
    let stopped = false
    const stop = () => {
      stopped = true
      cancelAnimationFrame(frame)
      observer?.disconnect()
      unsubscribe()
      window.removeEventListener('wheel', onUserIntent)
      window.removeEventListener('touchmove', onUserIntent)
      window.removeEventListener('pointerdown', onUserIntent)
      window.removeEventListener('keydown', onUserIntent)
      if (prependAnchorRef.current === anchor) prependAnchorRef.current = null
    }
    const schedule = () => {
      if (stopped) return
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(preservePosition)
    }
    const preservePosition = () => {
      if (stopped) return
      if (prependAnchorRef.current !== anchor) { stop(); return }
      if (document.visibilityState === 'hidden' || useUIStore.getState().l1 !== 'highlights') { stop(); return }
      const element = container.querySelector<HTMLElement>(`[data-cluster-id="${anchor.id}"]`)
      if (!element) { stop(); return }
      const documentTop = element.getBoundingClientRect().top + window.scrollY
      const delta = documentTop - anchor.documentTop
      // Add only inserted layout height to the *current* scroll position. User scrolling
      // during the fetch is retained; this is not a jump to the new day's heading.
      if (Math.abs(delta) > 0.5) window.scrollTo({ top: Math.max(0, window.scrollY + delta), behavior: 'instant' })
      anchor.documentTop = documentTop
      stableFrames = Math.abs(delta) <= 0.5 ? stableFrames + 1 : 0
      if (stableFrames >= 2) {
        if (!useDailyDigestStore.getState().loading) stop()
        return
      }
      schedule()
    }
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule)
    const unsubscribe = useDailyDigestStore.subscribe(schedule)
    const onUserIntent = (event: Event) => {
      if (event instanceof KeyboardEvent && !['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) return
      stop()
    }
    observer?.observe(container)
    window.addEventListener('wheel', onUserIntent, { passive: true })
    window.addEventListener('touchmove', onUserIntent, { passive: true })
    window.addEventListener('pointerdown', onUserIntent)
    window.addEventListener('keydown', onUserIntent)
    preservePosition()
    return stop
  }, [highlightsVisible, isPageVariant, isSearchActive, loadingNewer, navigating, prependVersion, searchQuery])

  useEffect(() => {
    if (!isPageVariant) return
    const cancel = () => {
      prependAnchorRef.current = null
      useEventsStore.getState().cancelNewerDay()
    }
    const onVisibility = () => { if (document.visibilityState === 'hidden') cancel() }
    if (!highlightsVisible || isSearchActive || searchQuery.trim()) cancel()
    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener('pagehide', cancel)
    return () => {
      cancel()
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('pagehide', cancel)
    }
  }, [highlightsVisible, isPageVariant, isSearchActive, searchQuery])

  // 稳定数组引用（feedback_usememo_stable_array_ref）
  const timelineGroups = useMemo(() => groupByDate(stableEvents), [stableEvents])
  const dailyDigestRange = useMemo(() => {
    if (!isPageVariant || isSearchActive) return null
    const dateKeys = timelineGroups
      .map((group) => group.key)
      .filter((key) => key !== 'unknown')
      .sort()
    if (dateKeys.length === 0) return null
    return { start: dateKeys[0], end: dateKeys[dateKeys.length - 1] }
  }, [isPageVariant, isSearchActive, timelineGroups])

  useEffect(() => {
    const force = refreshWasActiveRef.current && !refreshing
    refreshWasActiveRef.current = refreshing
    if (!dailyDigestRange || refreshing) return
    void loadDailyDigestRange(dailyDigestRange.start, dailyDigestRange.end, force)
  }, [dailyDigestRange, loadDailyDigestRange, refreshing])

  useEffect(() => {
    if (!user || !loadedClusterIdsKey) return
    const clusterIds = loadedClusterIdsKey.split(',').map(Number)
    const refreshStatuses = () => {
      const mutationVersion = useEventsStore.getState().readMutationVersion
      void fetchClusterStatuses(clusterIds).then((result) => {
        if (useEventsStore.getState().readMutationVersion !== mutationVersion) return
        applyReadStatuses(result.statuses)
      }).catch(() => {})
    }
    refreshStatuses()
    window.addEventListener('focus', refreshStatuses)
    const onVisibility = () => { if (document.visibilityState === 'visible') refreshStatuses() }
    document.addEventListener('visibilitychange', onVisibility)
    return () => { window.removeEventListener('focus', refreshStatuses); document.removeEventListener('visibilitychange', onVisibility) }
  }, [applyReadStatuses, loadedClusterIdsKey, user])

  useEffect(() => {
    if (!user || typeof BroadcastChannel === 'undefined') return
    const channel = new BroadcastChannel('info2act-cluster-read')
    channel.onmessage = (event) => {
      const clusterId = Number(event.data?.clusterId)
      if (Number.isFinite(clusterId)) useEventsStore.getState().markSeen(clusterId, false)
    }
    return () => channel.close()
  }, [user])

  // 响应式高度（基于 viewport）
  const height = containerHeight ?? (typeof window !== 'undefined' && window.innerWidth < 1024 ? 360 : 450)

  const hasItems = stableEvents.length > 0
  const isInitialLoading = (loading || (isSearchActive && searching)) && !hasItems
  const canLoadMore = !isSearchActive && cursor !== null
  const isLoadingMore = !isSearchActive && loading && hasItems
  const handleScroll = (event: UIEvent<HTMLDivElement>) => {
    if (isPageVariant) return
    if (!canLoadMore || loading || loadingNewer || refreshing) return
    const target = event.currentTarget
    const distanceToBottom = target.scrollHeight - target.scrollTop - target.clientHeight
    if (distanceToBottom <= 96) {
      void loadMore()
    }
  }
  // 搜索态零匹配
  const searchEmpty = isSearchActive && !searching && !hasItems

  useEffect(() => {
    if (enabled === false || !isPageVariant || !highlightsVisible || !canLoadMore || loading || loadingNewer || refreshing || navigating || searchQuery.trim() || searching) return
    const handleWindowScroll = () => {
      // bf-0711 #2: 弹窗打开时不触发触底加载(同下拉刷新守卫)。
      if (document.documentElement.style.overflow === 'hidden') return
      if (document.visibilityState === 'hidden' || !containerRef.current?.getClientRects().length) return
      const root = document.documentElement
      const viewportHeight = window.innerHeight || root.clientHeight
      const scrollTop = window.scrollY || root.scrollTop
      const distanceToBottom = root.scrollHeight - scrollTop - viewportHeight
      if (distanceToBottom <= 160) {
        void loadMore()
      }
    }
    window.addEventListener('scroll', handleWindowScroll, { passive: true })
    handleWindowScroll()
    return () => window.removeEventListener('scroll', handleWindowScroll)
  }, [canLoadMore, enabled, highlightsVisible, isPageVariant, loadMore, loading, loadingNewer, navigating, refreshing, searchQuery, searching])

  useEffect(() => {
    if (!refreshHint) return
    toast.info(refreshHint)
    clearRefreshHint()
  }, [clearRefreshHint, refreshHint])

  useEffect(() => {
    if (!isPageVariant || !highlightsVisible || enabled !== true || isSearchActive || searchQuery.trim() || searching || navigating) return
    const maybeRefreshAtTop = () => {
      // bf-0711 #2: 弹窗打开时(modal 锁 html overflow:hidden)禁用下拉刷新——
      // 否则弹窗内滚轮/触摸事件冒泡到 window,背景在顶部时会误触发精选页刷新。
      if (document.documentElement.style.overflow === 'hidden') return
      if (document.visibilityState === 'hidden' || !containerRef.current?.getClientRects().length) return
      const now = Date.now()
      if (now - manualRefreshAtRef.current < 1200) return
      if (window.scrollY > 1 || loading || loadingNewer || refreshing) return
      manualRefreshAtRef.current = now
      const state = useEventsStore.getState()
      const firstDate = state.events[0] ? formatDateKey(eventDate(state.events[0])) : null
      const newestDate = getBrowseableHighlightDates(state.dateCounts)[0]
      if (firstDate && newestDate && newestDate > firstDate) loadNewerDay()
      else void refresh()
    }
    const handleWheel = (event: WheelEvent) => {
      if (event.ctrlKey || (event.target instanceof Element && event.target.closest('[data-date-navigation]'))) return
      if (event.deltaY < -36) maybeRefreshAtTop()
    }
    const handleTouchStart = (event: TouchEvent) => {
      if (event.target instanceof Element && event.target.closest('[data-date-navigation]')) return
      if (window.scrollY > 1) return
      touchStartYRef.current = event.touches[0]?.clientY ?? null
    }
    const handleTouchEnd = (event: TouchEvent) => {
      const startY = touchStartYRef.current
      touchStartYRef.current = null
      const endY = event.changedTouches[0]?.clientY ?? null
      if (startY == null || endY == null) return
      if (endY - startY > 72) maybeRefreshAtTop()
    }
    window.addEventListener('wheel', handleWheel, { passive: true })
    window.addEventListener('touchstart', handleTouchStart, { passive: true })
    window.addEventListener('touchend', handleTouchEnd, { passive: true })
    return () => {
      window.removeEventListener('wheel', handleWheel)
      window.removeEventListener('touchstart', handleTouchStart)
      window.removeEventListener('touchend', handleTouchEnd)
    }
  }, [enabled, highlightsVisible, isPageVariant, isSearchActive, loadNewerDay, loading, loadingNewer, navigating, refresh, refreshing, searchQuery, searching])

  const scrollStyle = isPageVariant
    ? ({ overflowAnchor: 'none' } as const)
    : ({ height, overflowY: 'auto', overflowAnchor: 'none' } as const)

  if (enabled === false) {
    // event_aggregation_ready=false → 后端明确表示精选未启用,不渲染
    return null
  }

  // UX-2(B8): 首次加载失败(无任何已加载事件)→ 错误态 + 重试,
  // 替代原先的整页静默空白
  if (error && events.length === 0 && !loading) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 py-16 text-center" data-testid="events-error-state">
        <p className="text-[14px] text-muted-foreground">精选加载失败,请稍后重试</p>
        <button
          type="button"
          onClick={() => { void init() }}
          className="rounded-[4px] border border-border bg-card px-4 py-2 text-[13px] font-medium text-foreground transition-colors hover:border-[var(--brand-border)]"
        >
          重试
        </button>
      </div>
    )
  }

  return (
    <div
      data-testid={isPageVariant ? 'latest-events-page' : undefined}
      className={isPageVariant ? 'mb-8' : 'mb-4 overflow-hidden rounded-[4px] border border-border bg-card'}
    >
      {isPageVariant && highlightsVisible && !isSearchActive && !searchQuery.trim()
        ? <NewerDayFeedback onRetry={loadNewerDay} /> : null}
      {/* v18.1: 精选页只保留分类 pill；抓取进度和标题交给后台/导航语境承载 */}
      {!isPageVariant && topSlot && (
        <div data-testid="latest-events-top-slot" className="px-5 py-4 border-b border-border sm:px-6">
          {topSlot}
          {isSearchActive && (
            <div className="mt-2 text-[12px] text-muted-foreground">
              {searching
                ? '搜索中…'
                : searchDegraded
                  ? '搜索暂时不可用，请稍后重试'
                  : `共 ${searchTotal > 1000 ? '1000+' : searchTotal} 个事件匹配`}
            </div>
          )}
        </div>
      )}
      {/* Scrollable list */}
      <div
        ref={containerRef}
        data-testid="latest-events-scroll"
        onScroll={isPageVariant ? undefined : handleScroll}
        style={scrollStyle}
      >
        {isInitialLoading && (
          <>
            <SkeletonCard />
            <SkeletonCard />
            <SkeletonCard />
          </>
        )}

        {!isInitialLoading && !hasItems && showEmptyState && !isSearchActive && <EmptyState />}

        {/* BF-0704-6 rev3: 搜索加载态(page variant;卡片 variant 由 topSlot 文案承载)。
            searching 从输入防抖开始即为 true,用户打完字立刻有反馈 */}
        {isPageVariant && searching && searchQuery.trim() && (
          <div
            data-testid="events-search-loading"
            className="mb-4 flex items-center gap-2 rounded-md border border-border bg-muted px-4 py-2 text-[13px] text-muted-foreground"
          >
            <Loader2 size={14} className="animate-spin" aria-hidden="true" />
            正在搜索 “{searchQuery.trim()}”…
          </div>
        )}

        {isPageVariant && isSearchActive && !searching && !searchDegraded && (
          <div
            data-testid="events-search-result-count"
            className="mb-4 rounded-md border border-border bg-muted px-4 py-2 text-[13px] text-muted-foreground"
          >
            共 {searchTotal > 1000 ? '1000+' : searchTotal} 个事件匹配
          </div>
        )}

        {/* BF-0704-6: 搜索降级(后端超时)时显式提示,覆盖精选页 page variant(搜索框在 TopBar) */}
        {searchDegraded && !searching && searchQuery.trim() && (
          <div
            data-testid="events-search-degraded-hint"
            className={cn(
              'rounded-md border border-border bg-muted px-4 py-2 text-[13px] text-muted-foreground',
              isPageVariant ? 'mb-4' : 'mx-5 my-3 sm:mx-6',
            )}
          >
            搜索暂时不可用，请稍后重试
          </div>
        )}

        {searchEmpty && !searchDegraded && (
          <div className="flex items-center justify-center h-full text-[13px] text-muted-foreground">
            最新事件无匹配 “{searchQuery}”
          </div>
        )}

        {hasItems && (
          <div
            data-testid="event-timeline"
            className={cn(
              isPageVariant ? 'px-0' : 'px-5 sm:px-6',
              // rev3: 搜索进行中旧内容压暗禁点,明确"下面是旧内容"
              searching && searchQuery.trim() && 'opacity-50 pointer-events-none transition-opacity',
            )}
          >
            {timelineGroups.map((group, groupIndex) => {
              const groupDate = eventDate(group.events[0])
              const weekday = formatWeekday(groupDate)
              const fullDayCount = isSearchActive ? group.events.length : (dateCounts[group.key] ?? group.events.length)
              const metaLabel = [weekday, `${fullDayCount} 条更新`].filter(Boolean).join(' · ')
              const isDaySummary = isPageVariant && !isSearchActive && group.key !== 'unknown'
              const digest = digestsByDate[group.key]
              const digestLoaded = digestLoadedRange && group.key >= digestLoadedRange.start && group.key <= digestLoadedRange.end
              const digestCount = digest ? digest.entries.length : digestLoaded ? 0 : null

              return (
                <section
                  key={group.key}
                  data-testid="event-date-group"
                  data-highlight-date={isPageVariant && !isSearchActive && group.key !== 'unknown' ? group.key : undefined}
                  aria-label={group.label}
                  className="relative"
                >
                  <div
                    data-testid="event-date-heading"
                    data-has-digest={isDaySummary && Boolean(digest?.entries.length) ? true : undefined}
                    tabIndex={isPageVariant ? -1 : undefined}
                    role={isPageVariant ? 'heading' : undefined}
                    aria-level={isPageVariant ? 2 : undefined}
                    className={cn(
                      'relative text-[14px]',
                      isPageVariant
                        ? 'flex min-h-12 items-center bg-background'
                        : 'sticky top-0 z-30 -mx-5 flex items-center gap-2.5 border-b border-border bg-card px-5 py-3 sm:-mx-6 sm:px-6',
                      isDaySummary && 'highlights-day-heading',
                    )}
                  >
                    {!isPageVariant && <CalendarDays data-testid="event-date-icon" size={17} className="shrink-0 text-[var(--brand)]" aria-hidden="true" />}
                    <div className={cn(isPageVariant && 'flex flex-wrap items-baseline gap-x-2.5 gap-y-2')}>
                      <span
                        data-testid="event-date-label"
                        className={cn(
                          'tabular-nums',
                          !isDaySummary && 'text-foreground',
                          isPageVariant
                            ? 'font-display text-[22px] font-semibold leading-none'
                            : 'font-mono text-[16px] font-semibold',
                        )}
                      >
                        {group.label}
                      </span>
                      <span
                        data-testid="event-date-meta"
                        className={cn(
                          'font-body-cjk font-normal',
                          !isDaySummary && 'text-muted-foreground',
                          isPageVariant ? 'text-[13px]' : 'text-[14px]',
                        )}
                      >
                        {isDaySummary ? (
                          <>
                            <span className="whitespace-nowrap">{weekday} · </span>
                            <span className="whitespace-nowrap" data-testid="event-date-digest-count">精选 {digestCount ?? '—'}</span>
                            {'　'}
                            <span className="whitespace-nowrap" data-testid="event-date-total-count">全部 {allDateCounts?.[group.key] ?? '—'}</span>
                          </>
                        ) : metaLabel}
                      </span>
                    </div>
                  </div>
                  {isPageVariant && !isSearchActive && group.key !== 'unknown' && (
                    <DailyDigestStrip date={group.key} />
                  )}
                  {isPageVariant && groupIndex === 0 && refreshing && <RefreshInlineSpinner />}
                  {/* 严格时间倒序平铺(头版提升已退役)，行区外框与日期标题共享左边界。 */}
                  <div data-testid="event-rows">
                    {group.events.map((c, idx) => {
                      const date = eventDate(c)
                      return (
                        <EventCard
                          key={c.id}
                          cluster={c}
                          onSelect={handleSelectEvent}
                          readSyncError={readSyncErrors[c.id] === true}
                          onRetry={() => { void openModal(c.id, c) }}
                          onPrefetch={prefetchBundle}
                          timeLabel={formatEventTime(date)}
                          isFirstInGroup={idx === 0}
                        />
                      )
                    })}
                  </div>
                  {/* §21.2 日界收束: 无限流切成「一天一版」;搜索是检索模式不渲染;
                      整天加载完才渲染——半加载画收束线是对「完结感」撒谎 */}
                  {isPageVariant && !isSearchActive && group.key !== 'unknown' &&
                    group.events.length >= (dateCounts[group.key] ?? group.events.length) && (
                    <DayEndRule date={groupDate} count={fullDayCount} />
                  )}
                </section>
              )
            })}
          </div>
        )}

        {hasItems && canLoadMore && <LoadMoreHint loading={isLoadingMore} />}
        {hasItems && !isSearchActive && !canLoadMore && <EndPlaceholder />}
      </div>
    </div>
  )
}
