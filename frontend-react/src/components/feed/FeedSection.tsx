/**
 * v24.1: FeedSection = 「按类型」视角的数据容器。
 * 呈现交给 SectionFront（v24 板块眉 + 回滚的瀑布流白卡身体）；
 * 本文件负责 L2 子分类筛选、分页/预取/cursor、展开计数语义。
 *
 * 折叠/展开（v24.1 回滚为瀑布流机制）：
 *   折叠 = 渲染前 BATCH 条,masonry 裁到 ~800px + 渐变蒙版;
 *   展开 = 追加取页并放开可见计数（showCount）;收起滚回 section 顶。
 */
import { useMemo, useState, useRef, useEffect } from 'react'
import { SectionFront } from './SectionFront'
import { useUIStore } from '../../store/uiStore'
import { useFeedStore } from '../../store/feedStore'
import { fetchFeedSectionMore } from '../../lib/api'
import type { FeedItem, FeedSection as FeedSectionType, InfoReadModelCursor } from '../../lib/types'
import { InfoSectionPillBar, scrollInfoSectionToTop } from '../shared/InfoSectionPillBar'

const BATCH = 50
type SectionPage = Awaited<ReturnType<typeof fetchFeedSectionMore>>

function mergeUniqueItems(existing: FeedItem[], incoming: FeedItem[]): { items: FeedItem[]; added: number } {
  const existingIds = new Set(existing.map((item) => item.id))
  const unique = incoming.filter((item) => !existingIds.has(item.id))
  return { items: [...existing, ...unique], added: unique.length }
}

function makeSectionCursor(
  versionId: string | null,
  category: string,
  itemCount: number,
  subcategory?: string | null,
): InfoReadModelCursor | null {
  if (!versionId || itemCount <= 0) return null
  const dimension = subcategory ? 'section_subcategory' : 'section_category'
  const value = subcategory ? `${category}::${subcategory}` : category
  return {
    version_id: versionId,
    scope_key: `platform=_all|dimension=${dimension}|value=${value}`,
    rank_after: itemCount,
  }
}

interface FeedSectionProps {
  section: FeedSectionType
  showHeader?: boolean
  showSubcategoryFilters?: boolean
}

export function FeedSection({
  section,
  showHeader = true,
  showSubcategoryFilters = true,
}: FeedSectionProps) {
  const expandedKey = useUIStore((s) => s.expandedKey)
  const setExpandedKey = useUIStore((s) => s.setExpandedKey)
  const isExpanded = expandedKey === section.key

  // 展开态下可见条数上限（折叠态恒 BATCH）
  const [showCount, setShowCount] = useState(BATCH)
  const [keywordFilter, setKeywordFilter] = useState<string | null>(null)
  const searchQuery = useUIStore((s) => s.searchQuery)
  const isGlobalSearchActive = useFeedStore((s) => s.searchResults !== null)
  const activeSearch = isGlobalSearchActive ? searchQuery.trim() : ''

  // SectionFront 进入视口后才允许后台预取
  const [hasBeenVisible, setHasBeenVisible] = useState(false)

  const classification = useFeedStore((s) => s.classification)
  const sectionReadModelVersionId = useFeedStore((s) => s.sectionReadModelVersionId)
  const initialSectionCursor = useFeedStore((s) => s.sectionNextCursors[section.key] ?? null)
  // v4.0: pill source is L2 subcategories (id + name); fall back to fallback_keywords
  // when classification config is pre-v4.0 so pre-deploy frontends don't break.
  const pillSource = useMemo(() => {
    if (!classification) return [] as Array<{ id: string; label: string }>
    const cat = classification.categories.find((c) => c.id === section.key)
    if (cat?.subcategories?.length) {
      return cat.subcategories
        .filter((s) => s.id !== 'other')  // hide L2 'other' from the pill bar
        .map((s) => ({ id: s.id, label: s.name }))
    }
    // Legacy fallback (pre-v4.0)
    return (cat?.fallback_keywords ?? []).map((kw) => ({ id: kw, label: kw }))
  }, [classification, section.key])

  // Server-side filter results (full-DB match, not just loaded batch)
  const [keywordResults, setKeywordResults] = useState<{
    items: FeedItem[]
    total: number
    nextCursor?: InfoReadModelCursor | null
  } | null>(null)
  const [keywordLoading, setKeywordLoading] = useState(false)

  useEffect(() => {
    if (!showSubcategoryFilters && keywordFilter) {
      setKeywordFilter(null)
    }
  }, [keywordFilter, showSubcategoryFilters])

  // Fetch from server when L2 pill changes (subcategory mode for v4.0; legacy keyword mode if no subcategories)
  useEffect(() => {
    if (!keywordFilter) {
      setKeywordResults(null)
      setKeywordLoading(false)
      return
    }
    let cancelled = false
    setKeywordResults(null)
    setKeywordLoading(true)
    // Decide whether to filter as subcategory id or legacy keyword
    const cat = classification?.categories.find((c) => c.id === section.key)
    const isSubcategory = !!cat?.subcategories?.some((s) => s.id === keywordFilter)
    const promise = isSubcategory
      ? fetchFeedSectionMore(section.key, 0, BATCH, undefined, keywordFilter, activeSearch || undefined)
      : fetchFeedSectionMore(section.key, 0, BATCH, keywordFilter, undefined, activeSearch || undefined)
    promise
      .then((res) => {
        if (cancelled) return
        if (res.degraded) return
        setKeywordResults({ items: res.items, total: res.total ?? res.items.length, nextCursor: res.next_cursor ?? null })
      })
      .catch(() => {
        if (!cancelled) setKeywordResults(null)
      })
      .finally(() => {
        if (!cancelled) setKeywordLoading(false)
      })
    setShowCount(BATCH)
    return () => {
      cancelled = true
    }
  }, [keywordFilter, section.key, classification, activeSearch])

  const filteredItems = keywordFilter ? (keywordResults?.items ?? section.items) : section.items
  const filteredTotal = keywordFilter
    ? (keywordResults?.total ?? section.count)
    : section.count

  const limit = isExpanded ? showCount : BATCH
  // FE-1(B7): 已读态由 InfoCard 逐卡订阅 clickedAtById[id],section 级
  // memo 不依赖整个 clickedAtById——点一张卡只重渲染那张卡。
  const visibleItems = useMemo(
    () => filteredItems.slice(0, limit),
    [filteredItems, limit],
  )

  const effectiveTotal = filteredTotal
  const hasMore = filteredItems.length > limit || filteredItems.length < filteredTotal
  const remaining = Math.max(effectiveTotal - visibleItems.length, 0)

  const handleCollapse = () => {
    const rect = document.getElementById(`s-${section.key}`)?.getBoundingClientRect()
    if (rect && rect.top < 0) {
      scrollInfoSectionToTop(section.key)
    }
    setExpandedKey(null)
    setShowCount(BATCH)
  }

  const handleKeywordSelect = (nextKey: string | null) => {
    setKeywordFilter(nextKey)
    setShowCount(BATCH)
  }

  const loadingMoreRef = useRef(false)
  const loadMoreRequestSeqRef = useRef(0)
  const prefetchedRef = useRef<{ scopeKey: string; offset: number; page: SectionPage } | null>(null)
  const prefetchRef = useRef<{ scopeKey: string; offset: number; promise: Promise<SectionPage> } | null>(null)
  const [sectionCursor, setSectionCursor] = useState<InfoReadModelCursor | null>(null)
  const loadMoreScopeKey = useMemo(
    () => [section.key, keywordFilter ?? '', activeSearch].join('|'),
    [section.key, keywordFilter, activeSearch],
  )
  const loadMoreScopeRef = useRef(loadMoreScopeKey)

  useEffect(() => {
    loadMoreScopeRef.current = loadMoreScopeKey
    loadingMoreRef.current = false
    loadMoreRequestSeqRef.current += 1
    prefetchedRef.current = null
    prefetchRef.current = null
    setSectionCursor(null)
  }, [loadMoreScopeKey])

  useEffect(() => {
    if (!hasBeenVisible) return
    if (keywordFilter) return
    const offset = section.items.length
    if (offset >= filteredTotal) return
    const scopeKey = loadMoreScopeKey
    const existing = prefetchedRef.current
    if (existing && existing.scopeKey === scopeKey && existing.offset === offset) return
    const inFlight = prefetchRef.current
    if (inFlight && inFlight.scopeKey === scopeKey && inFlight.offset === offset) return

    const cursor = activeSearch
      ? null
      : sectionCursor ?? initialSectionCursor ?? makeSectionCursor(sectionReadModelVersionId, section.key, offset)
    const promise = cursor
      ? fetchFeedSectionMore(section.key, offset, BATCH, undefined, undefined, activeSearch || undefined, cursor)
      : fetchFeedSectionMore(section.key, offset, BATCH, undefined, undefined, activeSearch || undefined)
    const token = { scopeKey, offset, promise }
    prefetchRef.current = token
    promise
      .then((page) => {
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
  }, [hasBeenVisible, keywordFilter, section.items.length, filteredTotal, loadMoreScopeKey, section.key, activeSearch, sectionCursor, initialSectionCursor, sectionReadModelVersionId])

  const handleLoadMore = () => {
    if (!isExpanded) {
      setExpandedKey(section.key)
    }
    if (loadingMoreRef.current) return
    if (keywordFilter && keywordResults === null) return
    const offset = filteredItems.length
    if (offset >= filteredTotal) return
    const scopeKey = loadMoreScopeKey
    const requestSeq = ++loadMoreRequestSeqRef.current
    loadingMoreRef.current = true

    if (keywordFilter) {
      const cat = classification?.categories.find((c) => c.id === section.key)
      const isSubcategory = !!cat?.subcategories?.some((s) => s.id === keywordFilter)
      const cursor = keywordResults?.nextCursor ?? null
      const promise = isSubcategory
        ? cursor
          ? fetchFeedSectionMore(section.key, offset, BATCH, undefined, keywordFilter, activeSearch || undefined, cursor)
          : fetchFeedSectionMore(section.key, offset, BATCH, undefined, keywordFilter, activeSearch || undefined)
        : cursor
          ? fetchFeedSectionMore(section.key, offset, BATCH, keywordFilter, undefined, activeSearch || undefined, cursor)
          : fetchFeedSectionMore(section.key, offset, BATCH, keywordFilter, undefined, activeSearch || undefined)
      promise.then((res) => {
        if (loadMoreScopeRef.current !== scopeKey || loadMoreRequestSeqRef.current !== requestSeq) return
        if (res.degraded) return
        const baseItems = keywordResults?.items ?? []
        const merged = mergeUniqueItems(baseItems, res.items)
        setKeywordResults({
          items: merged.items,
          total: res.total ?? keywordResults?.total ?? merged.items.length,
          nextCursor: res.next_cursor ?? null,
        })
        if (merged.added > 0) {
          setShowCount((prev) => prev + merged.added)
        }
      }).catch(() => {})
        .finally(() => {
          if (loadMoreScopeRef.current === scopeKey && loadMoreRequestSeqRef.current === requestSeq) {
            loadingMoreRef.current = false
          }
        })
      return
    }

    const prefetched = prefetchedRef.current
    let pagePromise: Promise<SectionPage>
    if (prefetched?.scopeKey === scopeKey && prefetched.offset === offset) {
      prefetchedRef.current = null
      pagePromise = Promise.resolve(prefetched.page)
    } else {
      const inFlight = prefetchRef.current
      if (inFlight?.scopeKey === scopeKey && inFlight.offset === offset) {
        prefetchRef.current = null
        pagePromise = inFlight.promise
      } else {
        const cursor = activeSearch
          ? null
          : sectionCursor ?? initialSectionCursor ?? makeSectionCursor(sectionReadModelVersionId, section.key, offset)
        pagePromise = cursor
          ? fetchFeedSectionMore(section.key, offset, BATCH, undefined, undefined, activeSearch || undefined, cursor)
          : fetchFeedSectionMore(section.key, offset, BATCH, undefined, undefined, activeSearch || undefined)
      }
    }

    pagePromise
      .then((res) => {
        if (loadMoreScopeRef.current !== scopeKey || loadMoreRequestSeqRef.current !== requestSeq) return
        if (res.degraded) return
        const merged = mergeUniqueItems(section.items, res.items)
        if (merged.added > 0) {
          useFeedStore.getState().appendCategoryItems(section.key, res.items)
          setShowCount((prev) => prev + merged.added)
        }
        setSectionCursor(res.next_cursor ?? null)
      })
      .catch(() => {})
      .finally(() => {
        if (loadMoreScopeRef.current === scopeKey && loadMoreRequestSeqRef.current === requestSeq) {
          loadingMoreRef.current = false
        }
      })
  }

  // v4.0 L2 filters (or legacy keyword filters) — 板块眉同行右侧 underline tabs
  const pillBar = showSubcategoryFilters && pillSource.length > 0 ? (
    <InfoSectionPillBar
      sectionKey={section.key}
      items={[
        { key: null, label: '全部' },
        ...pillSource.map((p) => ({ key: p.id, label: p.label })),
      ]}
      activeKey={keywordFilter}
      onSelect={handleKeywordSelect}
      className="mb-0 w-auto max-w-full border-b-0"
    />
  ) : undefined

  return (
    <SectionFront
      sectionKey={section.key}
      label={showHeader ? section.label : undefined}
      count={showHeader ? effectiveTotal : undefined}
      items={visibleItems}
      hasMore={hasMore}
      remaining={remaining}
      isExpanded={isExpanded}
      onLoadMore={handleLoadMore}
      onCollapse={handleCollapse}
      onBecameVisible={() => setHasBeenVisible(true)}
      pillBar={pillBar}
      filterLoading={Boolean(keywordFilter && keywordLoading)}
    />
  )
}
