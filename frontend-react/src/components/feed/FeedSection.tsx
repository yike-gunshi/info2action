import { useMemo, useState, useRef, useEffect, useLayoutEffect } from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { cn } from '../../lib/utils'
import { InfoCard } from './InfoCard'
import { Masonry } from './Masonry'
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

/**
 * Max collapsed height for the masonry grid.
 * We dynamically clamp this to the actual rendered content height
 * so the gradient mask never sits over empty space.
 */
const COLLAPSED_MAX = 800

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

  const [showCount, setShowCount] = useState(BATCH)
  const [keywordFilter, setKeywordFilter] = useState<string | null>(null)
  const searchQuery = useUIStore((s) => s.searchQuery)
  const isGlobalSearchActive = useFeedStore((s) => s.searchResults !== null)
  const activeSearch = isGlobalSearchActive ? searchQuery.trim() : ''

  // Track previous visible count so only NEW cards get entrance animation
  const prevVisibleCountRef = useRef(0)

  // Lazy render: only render cards when section is near viewport
  const [hasBeenVisible, setHasBeenVisible] = useState(false)
  const lazyRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (hasBeenVisible || !lazyRef.current) return
    const observer = new IntersectionObserver(
      ([entry]) => { if (entry.isIntersecting) { setHasBeenVisible(true); observer.disconnect() } },
      { rootMargin: '200px 0px' },
    )
    observer.observe(lazyRef.current)
    return () => observer.disconnect()
  }, [hasBeenVisible])

  // Refs for measurement + collapse button
  const sectionRef = useRef<HTMLDivElement | null>(null)
  const masonryInnerRef = useRef<HTMLDivElement>(null)
  const [shortestColHeight, setShortestColHeight] = useState<number | null>(null)
  const [sectionVisible, setSectionVisible] = useState(false)

  // Show collapse button only when scrolling through expanded cards,
  // hide when reaching "展开更多" area or scrolling past the section
  useEffect(() => {
    if (!isExpanded || !masonryInnerRef.current) {
      setSectionVisible(false)
      return
    }
    const check = () => {
      const rect = masonryInnerRef.current!.getBoundingClientRect()
      // Masonry top is above viewport bottom AND masonry bottom is below viewport bottom
      // → user is scrolling through cards, hasn't reached the end yet
      setSectionVisible(rect.top < window.innerHeight && rect.bottom > window.innerHeight)
    }
    check()
    window.addEventListener('scroll', check, { passive: true })
    return () => window.removeEventListener('scroll', check)
  }, [isExpanded])

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

  useEffect(() => {
    if (!showSubcategoryFilters && keywordFilter) {
      setKeywordFilter(null)
    }
  }, [keywordFilter, showSubcategoryFilters])

  // Fetch from server when L2 pill changes (subcategory mode for v4.0; legacy keyword mode if no subcategories)
  useEffect(() => {
    if (!keywordFilter) {
      setKeywordResults(null)
      return
    }
    let cancelled = false
    setKeywordResults(null)
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
  // FE-1(B7): 已读态改由 InfoCard 逐卡订阅 clickedAtById[id],section 级
  // memo 不再依赖整个 clickedAtById——点一张卡只重渲染那张卡。
  const visibleItems = useMemo(
    () => filteredItems.slice(0, limit),
    [filteredItems, limit],
  )
  useEffect(() => {
    prevVisibleCountRef.current = visibleItems.length
  }, [visibleItems.length])

  const effectiveTotal = filteredTotal
  const hasMore = filteredItems.length > limit || filteredItems.length < filteredTotal
  const remaining = Math.max(effectiveTotal - visibleItems.length, 0)

  // Measure shortest column height — this becomes the clip line.
  useLayoutEffect(() => {
    if (!masonryInnerRef.current) return
    const container = masonryInnerRef.current.querySelector('[data-testid="masonry-columns"]')
    if (!container || container.children.length === 0) return
    const colHeights = Array.from(container.children).map((c) => (c as HTMLElement).offsetHeight)
    const shortest = Math.min(...colHeights)
    setShortestColHeight((current) => (current === shortest ? current : shortest))
  }, [visibleItems, isExpanded, hasMore])

  // Clip height: when hasMore, cut at shortest column so ALL columns have content past the line
  // Collapsed: also cap at COLLAPSED_MAX; Expanded: use shortest column directly
  const clipMaxHeight = hasMore && shortestColHeight != null && shortestColHeight > 100
    ? (isExpanded
      ? shortestColHeight - 40 // leave margin so gradient overlays actual card content
      : Math.min(shortestColHeight - 40, COLLAPSED_MAX))
    : hasMore
      ? COLLAPSED_MAX // fallback before measurement
      : undefined

  const handleCollapse = () => {
    const rect = sectionRef.current?.getBoundingClientRect()
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

  return (
    <div ref={(el) => { sectionRef.current = el; lazyRef.current = el }} id={`s-${section.key}`} className="mb-6" style={{ scrollMarginTop: '120px' }}>
      {showHeader && (
        <div className="flex items-center justify-between mb-3 px-1">
          <div className="flex items-center">
            <h2 className="text-[22px] font-bold text-foreground">{section.label}</h2>
            <span className="text-sm text-muted-foreground ml-2">
              {`${filteredTotal} 条`}
            </span>
          </div>
        </div>
      )}

      {/* v4.0 L2 filters (or legacy keyword filters) — v19 uses underline tabs instead of rounded pills. */}
      {showSubcategoryFilters && pillSource.length > 0 && (
        <InfoSectionPillBar
          sectionKey={section.key}
          items={[
            { key: null, label: '全部' },
            ...pillSource.map((p) => ({ key: p.id, label: p.label })),
          ]}
          activeKey={keywordFilter}
          onSelect={handleKeywordSelect}
        />
      )}

      {/* Card masonry — lazy rendered when section enters viewport */}
      {hasBeenVisible ? (
        <>
          <div
            className={cn('relative', hasMore && 'overflow-hidden')}
            style={clipMaxHeight != null ? { maxHeight: `${clipMaxHeight}px` } : undefined}
          >
            <div ref={masonryInnerRef}>
              <Masonry
                items={visibleItems}
                renderItem={(item, i) => {
                  // Only animate cards that are newly added (beyond previous count)
                  const isNew = i >= prevVisibleCountRef.current
                  const delay = isNew ? Math.min(i - prevVisibleCountRef.current, 19) * 30 : 0
                  return <InfoCard key={item.id} item={item} delay={delay} />
                }}
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
        </>
      ) : (
        /* Placeholder skeleton before section enters viewport */
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="h-48 bg-muted rounded-lg animate-skeleton" />
          ))}
        </div>
      )}

      {/* Fixed collapse button — only visible when section is in viewport */}
      {isExpanded && sectionVisible && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-[90]">
          <button
            onClick={handleCollapse}
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
