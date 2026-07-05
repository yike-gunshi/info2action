import { useState, useMemo, useRef, useEffect, useLayoutEffect } from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { cn, platformName } from '../../lib/utils'
import { usePlatformSections, useFeedStore } from '../../store/feedStore'
import { useUIStore } from '../../store/uiStore'
import { InfoCard } from '../feed/InfoCard'
import { Masonry } from '../feed/Masonry'
import { L1PillBar } from './L1PillBar'
import { InfoSectionPillBar, scrollInfoSectionToTop } from '../shared/InfoSectionPillBar'
import { fetchFeedPlatformMore, fetchLingowhaleGroups } from '../../lib/api'
import { PLATFORM_ORDER } from '../../lib/platforms'
import type { FeedItem, FeedSection as FeedSectionType, InfoReadModelCursor } from '../../lib/types'

// v18.2: 来源 section 仍按 platform 分组；section 内 pill 统一使用 L1 内容分类。
const L1_PILL_PLATFORMS = new Set(PLATFORM_ORDER)

const BATCH = 50
const PLATFORM_PREFETCH_IDLE_DELAY_MS = 1200
const EMPTY_COUNT_MAP: Record<string, number> = {}
type PlatformPage = Awaited<ReturnType<typeof fetchFeedPlatformMore>>
type CachedPlatformPage = { items: FeedItem[]; total: number; nextCursor?: InfoReadModelCursor | null }

function mergeUniqueItems(existing: FeedItem[], incoming: FeedItem[]): { items: FeedItem[]; added: number } {
  const existingIds = new Set(existing.map((item) => item.id))
  const unique = incoming.filter((item) => !existingIds.has(item.id))
  return { items: [...existing, ...unique], added: unique.length }
}

function platformFilterCacheKey(
  platform: string,
  source: string | null | undefined,
  group: string | null | undefined,
  category: string | null | undefined,
  search: string,
): string {
  return JSON.stringify([platform, source ?? '', group ?? '', category ?? '', search])
}

function makePlatformCursor(
  versionId: string | null,
  platform: string,
  itemCount: number,
  source?: string | null,
  group?: string | null,
  category?: string | null,
): InfoReadModelCursor | null {
  if (!versionId || itemCount <= 0) return null
  let dimension = 'all'
  let value = ''
  if (source && group && !category) {
    dimension = 'group_source'
    value = `${group}::${source}`
  } else if (source) {
    dimension = 'source'
    value = source
  } else if (group) {
    dimension = 'group'
    value = group
  } else if (category) {
    dimension = 'category'
    value = category
  }
  return {
    version_id: versionId,
    scope_key: `platform=${platform}|dimension=${dimension}|value=${value}`,
    rank_after: itemCount,
  }
}

/** Source pill sort priority (lower = earlier) */
const SOURCE_PRIORITY: Record<string, number> = {
  recommend: 0,
  'for-you': 0,
  following: 1,
  feed: 2,
  search: 3,
  up: 4,
  rank: 5,
  hot: 6,
  dynamic: 7,
  watch_later: 8,
}

function sourceSortKey(source: string): number {
  const cleaned = source.replace(/^\d+-/, '')
  if (cleaned.startsWith('search-') || cleaned.startsWith('search:')) return SOURCE_PRIORITY.search ?? 99
  return SOURCE_PRIORITY[cleaned] ?? 50
}

const SOURCE_SUFFIXES = /-(公众号|网站|播客|RSS)$/

const biliSourceNames: Record<string, string> = {
  hot: '热门',
  rank: '排行',
  watch_later: '稍后再看',
  up: 'UP主',
  dynamic: '动态',
}

const twitterSourceNames: Record<string, string> = {
  following: '关注',
  for_you: '推荐',  // BF-0512-1: 修 key 'for-you' → 'for_you'（DB 实际 source 用下划线）
  'for-you': '推荐',  // 兼容旧值（如未来某抓取改用连字符）
}

// BF-0512-1: v16.0 keyword search 全下线，前端 source pill bar 兜底排除非期望 source
// （后端 _add_display_visibility 已排 'search:%'；裸 'search' 和 'user-submit' 是
//  v16.0 PRD §4.2 决策的隐含延伸 + user-submit 在 twitter 是数据混淆历史遗留）
const TWITTER_HIDDEN_SOURCES = new Set(['search', 'user-submit'])

function formatSourceName(source: string, platform: string): string {
  const cleaned = source.replace(/^\d+-/, '')

  if (platform === 'bilibili' && biliSourceNames[cleaned]) {
    return biliSourceNames[cleaned]
  }

  if (cleaned.startsWith('search-')) return cleaned.slice(7)
  if (cleaned.startsWith('search:')) return cleaned.slice(7)

  if (platform === 'twitter' && twitterSourceNames[cleaned]) {
    return twitterSourceNames[cleaned]
  }

  if (cleaned === 'lingowhale') return '公众号'

  return cleaned.replace(SOURCE_SUFFIXES, '')
}

function ChannelsSkeleton({ embedded = false }: { embedded?: boolean }) {
  return (
    <div className={cn(embedded ? 'py-0' : 'max-w-[1200px] mx-auto px-4 py-4')} data-testid="channels-skeleton">
      <div className="mb-4 space-y-2">
        {[1, 2, 3].map((i) => (
          <div key={i} className="h-6 bg-muted rounded animate-skeleton" style={{ width: `${68 + i * 9}%` }} />
        ))}
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {Array.from({ length: 9 }).map((_, i) => (
          <div key={i} className="h-48 bg-muted rounded-lg animate-skeleton" />
        ))}
      </div>
    </div>
  )
}

function ChannelsEmptyState({ embedded = false, message }: { embedded?: boolean; message: string }) {
  return (
    <div className={cn(embedded ? 'py-12' : 'max-w-[1200px] mx-auto px-4 py-12', 'text-center')}>
      <p className="text-sm text-muted-foreground">{message}</p>
    </div>
  )
}

export function ChannelsView({ embedded = false }: { embedded?: boolean } = {}) {
  const sections = usePlatformSections()
  const platformSectionsLoaded = useFeedStore((s) => s.platformSectionsLoaded)
  const loadError = useFeedStore((s) => s.loadError)
  const setLoadError = useFeedStore((s) => s.setLoadError)
  const ensurePlatformSections = useFeedStore((s) => s.ensurePlatformSections)
  const isSearching = useFeedStore((s) => s.isSearching)
  const searchPlatformLoading = useFeedStore((s) => s.searchPlatformLoading)
  const searchResults = useFeedStore((s) => s.searchResults)
  const loadAttemptedRef = useRef(false)
  const emptyLoadedRetryRef = useRef(false)

  const sorted = [...sections]
    .filter((s) => PLATFORM_ORDER.includes(s.key))
    .sort((a, b) => PLATFORM_ORDER.indexOf(a.key) - PLATFORM_ORDER.indexOf(b.key))

  useEffect(() => {
    if (searchResults !== null || isSearching || searchPlatformLoading) return
    if (platformSectionsLoaded && sorted.length > 0) return
    if (platformSectionsLoaded && sorted.length === 0 && emptyLoadedRetryRef.current) return
    if (loadAttemptedRef.current) return
    loadAttemptedRef.current = true
    if (platformSectionsLoaded && sorted.length === 0) {
      emptyLoadedRetryRef.current = true
    }
    setLoadError(null)
    ensurePlatformSections()
      .catch((err) => {
        console.error('Failed to load platform data:', err)
        setLoadError('频道数据加载失败，请重试')
      })
    return () => {
      if (!platformSectionsLoaded) {
        loadAttemptedRef.current = false
      }
    }
  }, [
    isSearching,
    platformSectionsLoaded,
    sorted.length,
    searchPlatformLoading,
    searchResults,
    setLoadError,
    ensurePlatformSections,
  ])

  const isSearchActive = searchResults !== null || isSearching || searchPlatformLoading
  const loading = sorted.length === 0 && (isSearchActive
    ? (isSearching || searchPlatformLoading)
    : (!platformSectionsLoaded && !loadError))

  if (loading) {
    return <ChannelsSkeleton embedded={embedded} />
  }

  if (loadError && sorted.length === 0) {
    return <ChannelsEmptyState embedded={embedded} message={loadError} />
  }

  if (sorted.length === 0) {
    return <ChannelsEmptyState embedded={embedded} message="暂无频道内容" />
  }

  return (
    <div className={cn(embedded ? 'py-0' : 'max-w-[1200px] mx-auto px-4 py-4')}>
      {sorted.map((section) => (
        <PlatformSection
          key={section.key}
          section={section}
          showHeader
        />
      ))}
    </div>
  )
}

const COLLAPSED_MAX = 800

function PlatformSection({ section, showHeader = true }: { section: FeedSectionType; showHeader?: boolean }) {
  const platform = section.key
  const items = section.items
  const serverTotal = section.count
  const searchQuery = useUIStore((s) => s.searchQuery)
  const isSearchActive = useFeedStore((s) => s.searchResults !== null)
  const activeSearch = isSearchActive ? searchQuery.trim() : ''
  const serverSourceCounts = useFeedStore((s) => (
    s.searchResults ? s.searchSourceCounts[platform] : s.sourceCounts[platform]
  ) ?? EMPTY_COUNT_MAP)
  const platformReadModelVersionId = useFeedStore((s) => s.platformReadModelVersionId)
  const initialPlatformCursor = useFeedStore((s) => s.platformNextCursors[platform] ?? null)
  const expandedKey = useUIStore((s) => s.expandedKey)
  const setExpandedKey = useUIStore((s) => s.setExpandedKey)
  const [sourceFilter, setSourceFilter] = useState<string | null>(null)
  const [showCount, setShowCount] = useState(BATCH)
  // BF-0418-9 结构性修复：pill 切换走服务端拉该 source 的数据
  // 避免"某 source 数据被 fetched_at 更新的其他 source 挤出前 50 条客户端 filter 找不到"
  const [sourceItems, setSourceItems] = useState<FeedItem[] | null>(null)
  const [sourceTotal, setSourceTotal] = useState<number | null>(null)
  const [sourceCursor, setSourceCursor] = useState<InfoReadModelCursor | null>(null)
  const [sourceLoading, setSourceLoading] = useState(false)
  // BF-0419-10: 公众号订阅分组过滤(detail_json.group)
  const [groupFilter, setGroupFilter] = useState<string | null>(null)
  const [lwGroups, setLwGroups] = useState<Array<{ name: string; channels: Array<unknown>; item_count: number }> | null>(null)
  const [, setLwUngrouped] = useState<number>(0)  // BF-0512-3 第二轮: 仅 setter 用于追踪 API 返回的总数,UI 不再独立显示「未分组」
  const [groupItems, setGroupItems] = useState<FeedItem[] | null>(null)
  const [groupTotal, setGroupTotal] = useState<number | null>(null)
  const [groupCursor, setGroupCursor] = useState<InfoReadModelCursor | null>(null)
  const [groupLoading, setGroupLoading] = useState(false)

  // v18.2: 所有可见来源 section 都使用 L1 维度 pill。
  const isL1Dimension = L1_PILL_PLATFORMS.has(platform)
  const platformCategoryCounts = useFeedStore((s) => (
    s.searchResults ? s.searchPlatformCategoryCounts[platform] : s.platformCategoryCounts[platform]
  ) ?? EMPTY_COUNT_MAP)
  const selectedCategory = useFeedStore((s) => s.selectedCategory[platform] ?? null)
  const setSelectedCategory = useFeedStore((s) => s.setSelectedCategory)
  const classification = useFeedStore((s) => s.classification)
  const [categoryItems, setCategoryItems] = useState<FeedItem[] | null>(null)
  const [categoryTotal, setCategoryTotal] = useState<number | null>(null)
  const [categoryCursor, setCategoryCursor] = useState<InfoReadModelCursor | null>(null)
  const [, setCategoryLoading] = useState(false)
  const filterPageCacheRef = useRef<Map<string, CachedPlatformPage>>(new Map())

  const readCachedFilterPage = (
    source: string | null | undefined,
    group: string | null | undefined,
    category: string | null | undefined,
  ) => filterPageCacheRef.current.get(platformFilterCacheKey(platform, source, group, category, activeSearch))

  const writeCachedFilterPage = (
    source: string | null | undefined,
    group: string | null | undefined,
    category: string | null | undefined,
    page: PlatformPage,
  ) => {
    filterPageCacheRef.current.set(
      platformFilterCacheKey(platform, source, group, category, activeSearch),
      { items: page.items, total: page.total ?? page.items.length, nextCursor: page.next_cursor ?? null },
    )
  }

  // L1 id → 显示名 map (来自 classification.categories 已加载到 store)
  const categoryLabels = useMemo(() => {
    const map: Record<string, string> = {}
    if (classification) {
      for (const cat of classification.categories) {
        map[cat.id] = cat.name
      }
    }
    return map
  }, [classification])

  // BF-0512-5: L1 显示顺序数组 (按 classification.categories 顺序)
  // 与推荐页 L1 顺序保持一致，避免心智跳跃
  const categoryOrder = useMemo(() => {
    if (!classification) return [] as string[]
    return classification.categories.map((c) => c.id)
  }, [classification])

  // BF-0419-10: lingowhale 兼容路径保留；v18.2 当前 UI 不再展示分组 pill。
  useEffect(() => {
    if (platform !== 'lingowhale' || isL1Dimension) return
    let cancelled = false
    fetchLingowhaleGroups()
      .then((r) => {
        if (!cancelled) {
          setLwGroups(r.groups || [])
          setLwUngrouped(r.ungrouped_count || 0)
        }
      })
      .catch(() => { if (!cancelled) setLwGroups([]) })
    return () => { cancelled = true }
  }, [platform, isL1Dimension])

  // BF-0419-10/11: groupFilter (+可选二级 sourceFilter) 切换 → 组合 fetch
  // 后端 db.query_feed_by_platform 同时支持 group + source AND 关系过滤
  useEffect(() => {
    if (!groupFilter) {
      setGroupItems(null)
      setGroupTotal(null)
      setGroupCursor(null)
      return
    }
    const cached = readCachedFilterPage(sourceFilter, groupFilter, undefined)
    if (cached) {
      setGroupItems(cached.items)
      setGroupTotal(cached.total)
      setGroupCursor(cached.nextCursor ?? null)
      setGroupLoading(false)
      return
    }
    let cancelled = false
    setGroupLoading(true)
    // 二级 channel 过滤通过 sourceFilter 复用,因 source 字段就是 channel name
    fetchFeedPlatformMore(platform, 0, BATCH, sourceFilter ?? undefined, groupFilter, undefined, activeSearch || undefined)
      .then((r) => {
        if (!cancelled) {
          if (r.degraded) return
          writeCachedFilterPage(sourceFilter, groupFilter, undefined, r)
          setGroupItems(r.items)
          setGroupTotal(r.total ?? r.items.length)
          setGroupCursor(r.next_cursor ?? null)
        }
      })
      .catch(() => {
        if (!cancelled) {
          setGroupItems(null)
          setGroupTotal(null)
          setGroupCursor(null)
        }
      })
      .finally(() => { if (!cancelled) setGroupLoading(false) })
    return () => { cancelled = true }
  }, [groupFilter, sourceFilter, platform, activeSearch])

  const sectionKey = `ch-${platform}`
  const isExpanded = expandedKey === sectionKey

  // Refs for measurement + collapse button
  const sectionRef = useRef<HTMLDivElement>(null)
  const masonryInnerRef = useRef<HTMLDivElement>(null)
  const [shortestColHeight, setShortestColHeight] = useState<number | null>(null)
  const [sectionVisible, setSectionVisible] = useState(false)
  const [hasEnteredViewport, setHasEnteredViewport] = useState(false)

  useEffect(() => {
    const el = sectionRef.current
    if (!el || hasEnteredViewport) return
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setHasEnteredViewport(true)
          observer.disconnect()
        }
      },
      { rootMargin: '400px 0px' },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [hasEnteredViewport])

  // Measure shortest column height — this becomes the clip line
  useLayoutEffect(() => {
    if (!masonryInnerRef.current) return
    const container = masonryInnerRef.current.querySelector('[data-testid="masonry-columns"]')
    if (!container || container.children.length === 0) return
    const colHeights = Array.from(container.children).map((c) => (c as HTMLElement).offsetHeight)
    const shortest = Math.min(...colHeights)
    setShortestColHeight(shortest)
  })

  // Show collapse button only when scrolling through expanded cards,
  // hide when reaching "展开更多" area or scrolling past the section
  useEffect(() => {
    if (!isExpanded || !masonryInnerRef.current) {
      setSectionVisible(false)
      return
    }
    const check = () => {
      const rect = masonryInnerRef.current!.getBoundingClientRect()
      setSectionVisible(rect.top < window.innerHeight && rect.bottom > window.innerHeight)
    }
    check()
    window.addEventListener('scroll', check, { passive: true })
    return () => window.removeEventListener('scroll', check)
  }, [isExpanded])

  // Source counts from server (full distribution, not just loaded 50)
  const sources = useMemo(() => {
    const sourceMap = new Map<string, number>()
    for (const [src, count] of Object.entries(serverSourceCounts)) {
      if (!src) continue
      if (platform === 'bilibili' && src.replace(/^\d+-/, '').includes('feed')) continue
      // BF-0512-1: twitter 排除 v16.0 keyword search 全下线后的残留 source
      if (platform === 'twitter' && TWITTER_HIDDEN_SOURCES.has(src)) continue
      // 通用兜底: 任何 source 含 'search:' 前缀（v16.0 search:% 排除的兜底）
      if (src.startsWith('search:')) continue
      sourceMap.set(src, count)
    }
    const sorted = Array.from(sourceMap.entries()).sort(
      (a, b) => sourceSortKey(a[0]) - sourceSortKey(b[0]),
    )
    return new Map(sorted)
  }, [serverSourceCounts, platform])

  // pill 切换 → 服务端拉该 source 的数据（带取消竞态防护）
  // BF-0419-11: lingowhale 平台时由 groupFilter useEffect 统一处理(group+source 组合),此处跳过
  useEffect(() => {
    if (platform === 'lingowhale') return
    if (!sourceFilter) {
      setSourceItems(null)
      setSourceTotal(null)
      setSourceCursor(null)
      return
    }
    const cached = readCachedFilterPage(sourceFilter, undefined, undefined)
    if (cached) {
      setSourceItems(cached.items)
      setSourceTotal(cached.total)
      setSourceCursor(cached.nextCursor ?? null)
      setSourceLoading(false)
      return
    }
    let cancelled = false
    setSourceLoading(true)
    fetchFeedPlatformMore(platform, 0, BATCH, sourceFilter, undefined, undefined, activeSearch || undefined)
      .then((r) => {
        if (!cancelled) {
          if (r.degraded) return
          writeCachedFilterPage(sourceFilter, undefined, undefined, r)
          setSourceItems(r.items)
          setSourceTotal(r.total ?? r.items.length)
          setSourceCursor(r.next_cursor ?? null)
        }
      })
      .catch(() => {
        if (!cancelled) {
          setSourceItems(null)
          setSourceTotal(null)
          setSourceCursor(null)
        }
      })
      .finally(() => { if (!cancelled) setSourceLoading(false) })
    return () => { cancelled = true }
  }, [sourceFilter, platform, activeSearch])

  // v16.0 W4.T11: L1 pill 切换 → 服务端拉该 category 的数据 (带取消竞态防护)
  // 切回「全部」(selectedCategory === null) 清空 categoryItems, 走默认 section.items
  useEffect(() => {
    if (!isL1Dimension) return
    if (!selectedCategory) {
      setCategoryItems(null)
      setCategoryTotal(null)
      setCategoryCursor(null)
      return
    }
    // BF-0512-7 rev3: 切 pill 防页面跳动 — 记录 section 顶部在视口位置，
    // 数据更新后用 scrollBy 补回差值（masonry 高度变化导致后续 section 位移）
    const beforeTop = sectionRef.current?.getBoundingClientRect().top ?? 0

    const cached = readCachedFilterPage(undefined, undefined, selectedCategory)
    if (cached) {
      setCategoryItems(cached.items)
      setCategoryTotal(cached.total)
      setCategoryCursor(cached.nextCursor ?? null)
      setCategoryLoading(false)
      requestAnimationFrame(() => requestAnimationFrame(() => {
        const afterTop = sectionRef.current?.getBoundingClientRect().top ?? 0
        const diff = afterTop - beforeTop
        if (Math.abs(diff) > 1) {
          window.scrollBy({ top: diff, behavior: 'instant' as ScrollBehavior })
        }
      }))
      return
    }

    let cancelled = false
    setCategoryLoading(true)
    fetchFeedPlatformMore(platform, 0, BATCH, undefined, undefined, selectedCategory, activeSearch || undefined)
      .then((r) => {
        if (cancelled) return
        if (r.degraded) return
        writeCachedFilterPage(undefined, undefined, selectedCategory, r)
        setCategoryItems(r.items)
        setCategoryTotal(r.total ?? r.items.length)
        setCategoryCursor(r.next_cursor ?? null)
        // 双 rAF 等 React render + Masonry layout 完成后修正 scroll
        requestAnimationFrame(() => requestAnimationFrame(() => {
          if (cancelled) return
          const afterTop = sectionRef.current?.getBoundingClientRect().top ?? 0
          const diff = afterTop - beforeTop
          if (Math.abs(diff) > 1) {
            window.scrollBy({ top: diff, behavior: 'instant' as ScrollBehavior })
          }
        }))
      })
      .catch(() => {
        if (!cancelled) {
          setCategoryItems(null)
          setCategoryTotal(null)
          setCategoryCursor(null)
        }
      })
      .finally(() => { if (!cancelled) setCategoryLoading(false) })
    return () => { cancelled = true }
  }, [selectedCategory, platform, isL1Dimension, activeSearch])

  // Filter items by source / group / category
  const filteredItems = useMemo(() => {
    // v16.0 W4.T11: L1 维度 platform 且选了 category → 用服务端拉回的 categoryItems
    // BF-0512-7 rev2: categoryItems=null（loading 中）回退到 items 占位，
    // 防止 pill 切换瞬间 section 高度塌缩到 0 → 页面跳动（GitHub pill 切换尤明显）
    if (isL1Dimension && selectedCategory) {
      return categoryItems ?? items
    }
    // BF-0419-10: groupFilter 优先(公众号兼容路径)
    if (groupFilter) {
      return groupItems ?? items
    }
    // sourceFilter 激活：请求返回前保留当前 section.items，避免慢请求期间卡片区塌成空白。
    if (sourceFilter) {
      return sourceItems ?? items
    }
    // 未过滤：用 section.items（前 50 条），仅做 bilibili feed 垃圾过滤
    let result = items
    if (platform === 'bilibili') {
      result = result.filter((i) => !i.source?.replace(/^\d+-/, '').includes('feed'))
    }
    return result
  }, [items, sourceFilter, sourceItems, groupFilter, groupItems, isL1Dimension, selectedCategory, categoryItems, platform])

  const limit = isExpanded ? showCount : BATCH
  // FE-1(B7): 已读态改由 InfoCard 逐卡订阅 clickedAtById[id],section 级
  // memo 不再依赖整个 clickedAtById——点一张卡只重渲染那张卡。
  const visibleItems = useMemo(
    () => filteredItems.slice(0, limit),
    [filteredItems, limit],
  )
  // total：pill 激活时用服务端 source_counts/category_counts/分页 total 全库数字（而非仅取回的 50 条数量）
  // v16.0 W4.T11: L1 pill 激活 → 用 platformCategoryCounts[selectedCategory] 作为 total
  const effectiveTotal = (isL1Dimension && selectedCategory)
    ? (platformCategoryCounts[selectedCategory] ?? categoryTotal ?? filteredItems.length)
    : groupFilter
      ? (groupTotal ?? filteredItems.length)
      : sourceFilter
        ? (serverSourceCounts[sourceFilter] ?? sourceTotal ?? filteredItems.length)
        : serverTotal
  // hasMore: 任意筛选口径都以服务端 total 为准；未过滤时 effectiveTotal=section 全量。
  const hasMore = filteredItems.length > limit || effectiveTotal > filteredItems.length
  const remaining = Math.max(effectiveTotal - visibleItems.length, 0)
  const isFilterLoading = (sourceFilter && sourceLoading) || (groupFilter && groupLoading)

  // Clip height: when hasMore, cut at shortest column so ALL columns have content past the line
  const clipMaxHeight = hasMore && shortestColHeight != null && shortestColHeight > 100
    ? (isExpanded
      ? shortestColHeight - 40
      : Math.min(shortestColHeight - 40, COLLAPSED_MAX))
    : hasMore
      ? COLLAPSED_MAX
      : undefined

  const prevVisibleCountRef = useRef(0)
  const loadingMoreRef = useRef(false)
  const loadMoreRequestSeqRef = useRef(0)
  const prefetchedRef = useRef<{ scopeKey: string; offset: number; page: PlatformPage } | null>(null)
  const prefetchRef = useRef<{ scopeKey: string; offset: number; promise: Promise<PlatformPage> } | null>(null)
  const [platformCursor, setPlatformCursor] = useState<InfoReadModelCursor | null>(null)
  const loadMoreScopeKey = useMemo(
    () => [
      platform,
      sourceFilter ?? '',
      groupFilter ?? '',
      isL1Dimension ? (selectedCategory ?? '') : '',
      activeSearch,
    ].join('|'),
    [platform, sourceFilter, groupFilter, isL1Dimension, selectedCategory, activeSearch],
  )
  const loadMoreScopeRef = useRef(loadMoreScopeKey)

  useEffect(() => {
    loadMoreScopeRef.current = loadMoreScopeKey
    loadingMoreRef.current = false
    loadMoreRequestSeqRef.current += 1
    prefetchedRef.current = null
    prefetchRef.current = null
    setPlatformCursor(null)
  }, [loadMoreScopeKey])

  useEffect(() => {
    prevVisibleCountRef.current = visibleItems.length
  }, [visibleItems.length])

  useEffect(() => {
    if (!hasEnteredViewport) return
    if (sourceFilter || groupFilter || (isL1Dimension && selectedCategory)) return
    const offset = items.length
    if (offset >= serverTotal) return
    const scopeKey = loadMoreScopeKey
    let cancelled = false
    const timer = window.setTimeout(() => {
      if (cancelled) return
      const existing = prefetchedRef.current
      if (existing && existing.scopeKey === scopeKey && existing.offset === offset) return
      const inFlight = prefetchRef.current
      if (inFlight && inFlight.scopeKey === scopeKey && inFlight.offset === offset) return
      const excludeIds = items.map((item) => item.id)
      const cursor = activeSearch
        ? null
        : platformCursor ?? initialPlatformCursor ?? makePlatformCursor(platformReadModelVersionId, platform, offset)

      const promise = cursor
        ? fetchFeedPlatformMore(
          platform,
          offset,
          BATCH,
          undefined,
          undefined,
          undefined,
          activeSearch || undefined,
          undefined,
          cursor,
        )
        : fetchFeedPlatformMore(
          platform,
          0,
          BATCH,
          undefined,
          undefined,
          undefined,
          activeSearch || undefined,
          excludeIds,
        )
      const token = { scopeKey, offset, promise }
      prefetchRef.current = token
      promise
        .then((page) => {
          if (cancelled) return
          if (prefetchRef.current !== token) return
          if (loadMoreScopeRef.current !== scopeKey) return
          if (page.degraded) return
          if (page.items.length > 0) {
            prefetchedRef.current = { scopeKey, offset, page }
          }
        })
        .catch(() => {})
        .finally(() => {
          if (prefetchRef.current === token) {
            prefetchRef.current = null
          }
        })
    }, PLATFORM_PREFETCH_IDLE_DELAY_MS)

    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [
    hasEnteredViewport,
    sourceFilter,
    groupFilter,
    isL1Dimension,
    selectedCategory,
    items.length,
    serverTotal,
    loadMoreScopeKey,
    platform,
    activeSearch,
    platformCursor,
    initialPlatformCursor,
    platformReadModelVersionId,
  ])

  const handleLoadMore = () => {
    if (!isExpanded) {
      setExpandedKey(sectionKey)
    }
    if (loadingMoreRef.current) return
    if (groupFilter && groupItems === null) return
    if (!groupFilter && sourceFilter && sourceItems === null) return
    if (isL1Dimension && selectedCategory && categoryItems === null) return

    const offset = filteredItems.length
    if (offset >= effectiveTotal) return
    const excludeIds = filteredItems.map((item) => item.id)
    const cursor = activeSearch
      ? null
      : groupFilter
        ? groupCursor
        : (isL1Dimension && selectedCategory)
          ? categoryCursor
            : sourceFilter
              ? sourceCursor
              : platformCursor ?? initialPlatformCursor ?? makePlatformCursor(platformReadModelVersionId, platform, offset)
    const scopeKey = loadMoreScopeKey
    const requestSeq = ++loadMoreRequestSeqRef.current
    loadingMoreRef.current = true
    const prefetched = prefetchedRef.current
    let pagePromise: Promise<PlatformPage>
    if (!sourceFilter && !groupFilter && !(isL1Dimension && selectedCategory) && prefetched?.scopeKey === scopeKey && prefetched.offset === offset) {
      prefetchedRef.current = null
      pagePromise = Promise.resolve(prefetched.page)
    } else {
      const inFlight = prefetchRef.current
      if (!sourceFilter && !groupFilter && !(isL1Dimension && selectedCategory) && inFlight?.scopeKey === scopeKey && inFlight.offset === offset) {
        prefetchRef.current = null
        pagePromise = inFlight.promise
      } else {
        pagePromise = cursor
          ? fetchFeedPlatformMore(
            platform,
            offset,
            BATCH,
            sourceFilter ?? undefined,
            groupFilter ?? undefined,
            isL1Dimension ? (selectedCategory ?? undefined) : undefined,
            activeSearch || undefined,
            undefined,
            cursor,
          )
          : fetchFeedPlatformMore(
            platform,
            0,
            BATCH,
            sourceFilter ?? undefined,
            groupFilter ?? undefined,
            isL1Dimension ? (selectedCategory ?? undefined) : undefined,
            activeSearch || undefined,
            excludeIds,
          )
      }
    }

    pagePromise.then((res) => {
      if (loadMoreScopeRef.current !== scopeKey || loadMoreRequestSeqRef.current !== requestSeq) return
      if (res.degraded) return
      const total = res.total ?? null
      let added = 0
      if (groupFilter) {
        if (total !== null) setGroupTotal(total)
        const merged = mergeUniqueItems(groupItems ?? [], res.items)
        added = merged.added
        setGroupItems(merged.items)
        setGroupCursor(res.next_cursor ?? null)
      } else if (isL1Dimension && selectedCategory) {
        if (total !== null) setCategoryTotal(total)
        const merged = mergeUniqueItems(categoryItems ?? [], res.items)
        added = merged.added
        setCategoryItems(merged.items)
        setCategoryCursor(res.next_cursor ?? null)
      } else if (sourceFilter) {
        if (total !== null) setSourceTotal(total)
        const merged = mergeUniqueItems(sourceItems ?? [], res.items)
        added = merged.added
        setSourceItems(merged.items)
        setSourceCursor(res.next_cursor ?? null)
      } else {
        const merged = mergeUniqueItems(items, res.items)
        added = merged.added
        if (added > 0) {
          useFeedStore.getState().appendPlatformItems(platform, res.items)
        }
        setPlatformCursor(res.next_cursor ?? null)
      }
      if (added > 0) {
        setShowCount((prev) => prev + added)
      }
    }).catch(() => {})
      .finally(() => {
        if (loadMoreScopeRef.current === scopeKey && loadMoreRequestSeqRef.current === requestSeq) {
          loadingMoreRef.current = false
        }
      })
  }

  const handleGroupSelect = (nextGroup: string | null) => {
    setShowCount(BATCH)
    setGroupFilter(nextGroup)
    setSourceFilter(null)
    setSourceItems(null)
    setSourceTotal(null)
    const cached = nextGroup ? readCachedFilterPage(undefined, nextGroup, undefined) : undefined
    setGroupItems(cached?.items ?? null)
    setGroupTotal(cached?.total ?? null)
    setGroupCursor(cached?.nextCursor ?? null)
    setSourceCursor(null)
  }

  const handleLingowhaleChannelSelect = (nextSource: string | null) => {
    setShowCount(BATCH)
    setSourceFilter(nextSource)
    const cached = groupFilter ? readCachedFilterPage(nextSource, groupFilter, undefined) : undefined
    setGroupItems(cached?.items ?? null)
    setGroupTotal(cached?.total ?? null)
    setGroupCursor(cached?.nextCursor ?? null)
  }

  const handleSourceSelect = (nextSource: string | null) => {
    setShowCount(BATCH)
    setSourceFilter(nextSource)
    const cached = nextSource ? readCachedFilterPage(nextSource, undefined, undefined) : undefined
    setSourceItems(cached?.items ?? null)
    setSourceTotal(cached?.total ?? null)
    setSourceCursor(cached?.nextCursor ?? null)
  }

  const handleCategorySelect = (nextCategory: string | null) => {
    setShowCount(BATCH)
    const cached = nextCategory ? readCachedFilterPage(undefined, undefined, nextCategory) : undefined
    setCategoryItems(cached?.items ?? null)
    setCategoryTotal(cached?.total ?? null)
    setCategoryCursor(cached?.nextCursor ?? null)
    setSelectedCategory(platform, nextCategory)
  }

  const selectedLingowhaleChannels = (() => {
    if (platform !== 'lingowhale' || !groupFilter || groupFilter === '未分组' || !lwGroups) return []
    const selectedGroup = lwGroups.find((g) => g.name === groupFilter)
    return ((selectedGroup?.channels as Array<{ channel_id: string; name: string }> | undefined) ?? [])
      .map((ch) => ({
        key: ch.name,
        label: ch.name.replace(/-(公众号|播客|RSS|视频号|网站|微博)$/, ''),
        title: ch.name,
      }))
  })()

  return (
    <div ref={sectionRef} id={`s-${platform}`} className="mb-8" style={{ scrollMarginTop: '120px' }}>
      {/* Header */}
      {showHeader && (
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-[22px] font-bold text-foreground">
            {platformName(platform)}
            {/* BF-0512-7: 切 pill 后数字联动（用 effectiveTotal），跟推荐页 FeedSection.tsx:235 一致 */}
            <span className="text-sm font-normal text-muted-foreground ml-2">
              {effectiveTotal} 条
            </span>
          </h2>
        </div>
      )}

      {/* BF-0419-10: 公众号订阅分组 pills(detail_json.group),覆盖 source pills */}
      {/* BF-0512-3: 公众号 pill bar — 即便 lwGroups=[] 也渲染「全部」pill,
           满足 PRD §4.9.6 全局规则「每 section 第一个 pill = 「全部」默认选中」。
           lwGroups 加载失败/为空时（凭证过期、ECS 首次部署、数据迁移期），
           用户至少能看到「全部」pill 切换 UI;有数据后 group pill 自动加进来 */}
      {platform === 'lingowhale' && !isL1Dimension && lwGroups !== null && (
        <InfoSectionPillBar
          sectionKey={platform}
          items={[
            { key: null, label: '全部' },
            ...lwGroups.map((g) => ({
              key: g.name,
              label: g.name,
              title: `${g.channels.length} 个频道,${g.item_count} 篇内容（hover tooltip 见 cnt；BF-0512-7 跟推荐页一致 pill 纯文本）`,
            })),
          ]}
          activeKey={groupFilter}
          onSelect={handleGroupSelect}
          nestedRows={selectedLingowhaleChannels.length > 0 ? [{
            prefix: `↳ ${groupFilter}:`,
            items: selectedLingowhaleChannels,
            activeKey: sourceFilter,
            onSelect: handleLingowhaleChannelSelect,
            ariaLabel: `${groupFilter} 频道筛选`,
          }] : undefined}
          data-testid="info-section-pill-bar-lingowhale"
        />
      )}
          {/* BF-0512-3 第二轮: 删「未分组」独立 pill 渲染。
              PRD §4.9.5 S5 + 决策稿 §4.5: 「未分组」内容并入「全部」pill,
              不独立成 pill。BF-0419-11 时代加的「未分组」button 在 v16.0
              新决策下移除。lwUngrouped 数据仍 fetch（API 返 ungrouped_count
              用于「全部」pill 的总计算），但不渲染独立 button */}
          {/* BF-0512-7 rev2: 删「加载中...」字样（pill 后突兀），切 pill 时靠数据占位 */}

      {/* v18.2: L1 维度 pill (所有可见来源 section)
          强制「全部」首位, L1PillBar 内部已实现 */}
      {isL1Dimension && (
        <L1PillBar
          platform={platform}
          categoryCounts={platformCategoryCounts}
          categoryLabels={categoryLabels}
          categoryOrder={categoryOrder}
          selectedCategory={selectedCategory}
          onSelect={handleCategorySelect}
        />
      )}

      {/* Source pills 仅作兼容兜底；当前 PLATFORM_ORDER 内来源均使用 L1 pill。 */}
      {sources.size > 1 && platform !== 'lingowhale' && !isL1Dimension && (
        <InfoSectionPillBar
          sectionKey={platform}
          items={[
            { key: null, label: '全部' },
            ...Array.from(sources.entries()).map(([source]) => ({
              key: source,
              label: formatSourceName(source, platform),
            })),
          ]}
          activeKey={sourceFilter}
          onSelect={handleSourceSelect}
          data-testid={`info-section-pill-bar-${platform}`}
        />
      )}

      {/* Masonry with horizontal clip line + gradient mask */}
      <div
        className={cn(
          'relative transition-opacity duration-150',
          hasMore && 'overflow-hidden',
          isFilterLoading && 'opacity-80',
        )}
        aria-busy={isFilterLoading || undefined}
        style={clipMaxHeight != null ? { maxHeight: `${clipMaxHeight}px` } : undefined}
      >
        <div ref={masonryInnerRef}>
          <Masonry
            items={visibleItems}
            renderItem={(item, i) => (
              <InfoCard key={item.id} item={item} delay={Math.min(i, 19) * 30} />
            )}
          />
        </div>

        {/* Gradient mask — horizontal cut across all columns */}
        {hasMore && clipMaxHeight != null && (
          <div className="absolute bottom-0 left-0 right-0 h-24 bg-gradient-to-t from-background to-transparent pointer-events-none" />
        )}
      </div>

      {/* Expand / Load more button */}
      {hasMore && (
        <div className="flex justify-center mt-4">
          <button
            onClick={handleLoadMore}
            className="flex items-center gap-1.5 px-5 py-2 text-sm font-medium text-foreground bg-card border border-border hover:border-warm-400 shadow-subtle hover:shadow-medium rounded-full transition-all cursor-pointer"
          >
            展开更多
            {remaining > 0 && <span className="text-xs text-muted-foreground">还有 {remaining} 条</span>}
            <ChevronDown className="w-3.5 h-3.5 text-muted-foreground" />
          </button>
        </div>
      )}

      {/* Fixed collapse button — only visible when section is in viewport */}
      {isExpanded && sectionVisible && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-[90]">
          <button
            onClick={() => {
              const el = document.getElementById(`s-${platform}`)
              const rect = el?.getBoundingClientRect()
              if (rect && rect.top < 0) {
                scrollInfoSectionToTop(platform)
              }
              setExpandedKey(null)
              setShowCount(BATCH)
            }}
            className="flex items-center gap-1.5 px-5 py-2 text-sm font-medium text-foreground bg-card border border-border hover:border-warm-400 shadow-subtle hover:shadow-medium rounded-full transition-all cursor-pointer"
          >
            收起
            <ChevronUp className="w-3.5 h-3.5 text-muted-foreground" />
          </button>
        </div>
      )}
    </div>
  )
}
