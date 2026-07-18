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
import { useCallback, useEffect, useMemo, useRef } from 'react'
import type { UIEvent } from 'react'
import { CalendarDays, Loader2 } from 'lucide-react'
import { toast } from 'sonner'
import { useEventsStore } from '../../store/eventsStore'
import { useClusterDetailStore } from '../../store/clusterDetailStore'
import { EventCard } from './EventCard'
import type { ClusterEvent } from '../../lib/types'
import { cn } from '../../lib/utils'

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
  if (!date) return 'unknown'
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, '0'),
    String(date.getDate()).padStart(2, '0'),
  ].join('-')
}

function formatDateLabel(date: Date | null): string {
  if (!date) return '时间未知'
  return `${date.getFullYear()}.${date.getMonth() + 1}.${date.getDate()}`
}

function formatWeekday(date: Date | null): string {
  if (!date) return ''
  return date.toLocaleDateString('zh-CN', { weekday: 'long' })
}

function formatEventTime(date: Date | null): string {
  if (!date) return ''
  return date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

/** §21.2 日界收束: 「· M 月 D 日 共 N 条 ·」mono 12px 居中,两侧 hairline 延伸 */
function DayEndRule({ date, count }: { date: Date | null; count: number }) {
  if (!date) return null
  return (
    <div data-testid="event-day-end" className="flex items-center gap-4 pb-1.5 pt-[22px]">
      <span aria-hidden="true" className="h-px flex-1 bg-border" />
      <span className="font-mono text-[12px] text-muted-foreground">
        {`· ${date.getMonth() + 1} 月 ${date.getDate()} 日 共 ${count} 条 ·`}
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
  const loading = useEventsStore((s) => s.loading)
  const refreshing = useEventsStore((s) => s.refreshing)
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

  // 初次挂载拉数据
  useEffect(() => {
    if (initRef.current) return
    initRef.current = true
    init()
  }, [init])

  const containerRef = useRef<HTMLDivElement>(null)

  // 稳定数组引用（feedback_usememo_stable_array_ref）
  // 搜索时用 searchResults 替代 events 渲染（双区独立）
  const isSearchActive = searchResults !== null
  const sourceEvents = isSearchActive ? searchResults : events
  const stableEvents = useMemo(() => sourceEvents ?? [], [sourceEvents])
  const timelineGroups = useMemo(() => groupByDate(stableEvents), [stableEvents])

  // 响应式高度（基于 viewport）
  const height = containerHeight ?? (typeof window !== 'undefined' && window.innerWidth < 1024 ? 360 : 450)

  const hasItems = stableEvents.length > 0
  const isInitialLoading = (loading || (isSearchActive && searching)) && !hasItems
  const canLoadMore = !isSearchActive && cursor !== null
  const isLoadingMore = !isSearchActive && loading && hasItems
  const handleScroll = (event: UIEvent<HTMLDivElement>) => {
    if (isPageVariant) return
    if (!canLoadMore || loading || refreshing) return
    const target = event.currentTarget
    const distanceToBottom = target.scrollHeight - target.scrollTop - target.clientHeight
    if (distanceToBottom <= 96) {
      void loadMore()
    }
  }
  // 搜索态零匹配
  const searchEmpty = isSearchActive && !searching && !hasItems

  useEffect(() => {
    if (enabled === false || !isPageVariant || !canLoadMore || loading || refreshing) return
    const handleWindowScroll = () => {
      // bf-0711 #2: 弹窗打开时不触发触底加载(同下拉刷新守卫)。
      if (document.documentElement.style.overflow === 'hidden') return
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
  }, [canLoadMore, enabled, isPageVariant, loadMore, loading, refreshing])

  useEffect(() => {
    if (!refreshHint) return
    toast.info(refreshHint)
    clearRefreshHint()
  }, [clearRefreshHint, refreshHint])

  useEffect(() => {
    if (!isPageVariant || enabled !== true || isSearchActive) return
    const maybeRefreshAtTop = () => {
      // bf-0711 #2: 弹窗打开时(modal 锁 html overflow:hidden)禁用下拉刷新——
      // 否则弹窗内滚轮/触摸事件冒泡到 window,背景在顶部时会误触发精选页刷新。
      if (document.documentElement.style.overflow === 'hidden') return
      const now = Date.now()
      if (now - manualRefreshAtRef.current < 1200) return
      if (window.scrollY > 1 || loading || refreshing) return
      manualRefreshAtRef.current = now
      void refresh()
    }
    const handleWheel = (event: WheelEvent) => {
      if (event.deltaY < -36) maybeRefreshAtTop()
    }
    const handleTouchStart = (event: TouchEvent) => {
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
  }, [enabled, isPageVariant, isSearchActive, loading, refresh, refreshing])

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

              return (
                <section
                  key={group.key}
                  data-testid="event-date-group"
                  aria-label={group.label}
                  // v24.1: 日组整体内缩 16px——正文区 ~960 宽,略窄于上方筛选 tab 行的 992(宽度层级)
                  className={cn('relative', isPageVariant && 'sm:px-4')}
                >
                  <div
                    data-testid="event-date-heading"
                    className={cn(
                      'relative text-[14px]',
                      isPageVariant
                        ? 'sticky top-[var(--highlights-date-top)] z-40 grid min-h-12 grid-cols-1 items-center gap-x-4 bg-background sm:grid-cols-[52px_minmax(0,1fr)] lg:grid-cols-[56px_minmax(0,1fr)]'
                        : 'sticky top-0 z-30 -mx-5 flex items-center gap-2.5 border-b border-border bg-card px-5 py-3 sm:-mx-6 sm:px-6',
                    )}
                  >
                    {!isPageVariant && <CalendarDays data-testid="event-date-icon" size={17} className="shrink-0 text-[var(--brand)]" aria-hidden="true" />}
                    {/* v24.1: 去缩进——日期文字、Scotch rule、时间列共享同一 x0 左缘 */}
                    <div className={cn(isPageVariant && 'flex items-baseline gap-2.5 sm:col-span-2')}>
                      <span
                        data-testid="event-date-label"
                        className={cn(
                          'tabular-nums text-foreground',
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
                          'font-body-cjk font-normal text-muted-foreground',
                          isPageVariant ? 'text-[13px]' : 'text-[14px]',
                        )}
                      >
                        {metaLabel}
                      </span>
                    </div>
                    {/* §21.2 报眉: Scotch rule 上粗下细双线(2px foreground + 3px 间隔 + 1px border),
                        随日期头 sticky,兼作滚动遮挡边界 */}
                    {isPageVariant && (
                      <div
                        data-testid="event-scotch-rule"
                        aria-hidden="true"
                        className="mt-1 h-[6px] border-b border-t-2 border-b-border border-t-foreground sm:col-span-2"
                      />
                    )}
                  </div>
                  {isPageVariant && groupIndex === 0 && refreshing && <RefreshInlineSpinner />}
                  {/* 严格时间倒序平铺(头版提升已退役)。
                      行区再缩进一档(v24.2): 筛选 tab > 日期/Scotch rule > 行区 的嵌套层级,行分隔线随行区缩进 */}
                  <div className={cn(isPageVariant && 'sm:pl-4')} data-testid="event-rows">
                    {group.events.map((c, idx) => {
                      const date = eventDate(c)
                      return (
                        <EventCard
                          key={c.id}
                          cluster={c}
                          onSelect={handleSelectEvent}
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
