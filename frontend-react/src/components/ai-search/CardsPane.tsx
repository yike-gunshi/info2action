import { useLayoutEffect, useMemo, useRef } from 'react'
import { useAiSearchStore } from '../../store/aiSearchStore'
import { useScrollbarReveal } from '../../hooks/useScrollbarReveal'
import { CitationCard } from './CitationCard'
import { isDesktopAiSearch, keepFocusVisible } from './aiSearchScroll'
import './CitationCard.css'

export function CardsPane() {
  const items = useAiSearchStore((state) => state.items)
  const cardOrder = useAiSearchStore((state) => state.cardOrder)
  const highlightItemId = useAiSearchStore((state) => state.highlightItemId)
  const status = useAiSearchStore((state) => state.summary.status)
  const containerRef = useRef<HTMLDivElement>(null)
  useScrollbarReveal(containerRef)
  const anchorRef = useRef<{ id: string; top: number } | null>(null)
  const itemById = useMemo(() => new Map(items.map((item, index) => [item.id, { item, n: index + 1 }])), [items])
  const showSkeleton = status === 'retrieving' || !items.length
  const visibleOrder = status === 'insufficient' ? items.slice(0, 5).map(item => item.id) : cardOrder

  useLayoutEffect(() => useAiSearchStore.subscribe((state, previous) => {
    if (!isDesktopAiSearch()) return
    if (state.cardOrder === previous.cardOrder || state.cardOrder.every((id, index) => id === previous.cardOrder[index])) return
    // Only anchor a reorder of this result set, never a different search.
    if (state.jobId !== previous.jobId || state.query !== previous.query) {
      anchorRef.current = null
      return
    }
    const oldIds = new Set(previous.cardOrder)
    if (state.cardOrder.length !== oldIds.size || state.cardOrder.some((id) => !oldIds.has(id))) {
      anchorRef.current = null
      return
    }
    const container = containerRef.current
    if (!container || anchorRef.current) return
    const containerTop = container.getBoundingClientRect().top + container.clientTop
    const containerBottom = containerTop + container.clientHeight
    const cards = container.querySelectorAll<HTMLElement>('[data-ai-card-id]')
    for (const card of cards) {
      const rect = card.getBoundingClientRect()
      if (rect.bottom > containerTop && rect.top < containerBottom) {
        anchorRef.current = { id: card.id, top: rect.top - containerTop }
        break
      }
    }
  }), [])

  useLayoutEffect(() => {
    const anchor = anchorRef.current
    anchorRef.current = null
    const container = containerRef.current
    if (!anchor || !container || !isDesktopAiSearch()) return
    const card = document.getElementById(anchor.id)
    if (!card || !container.contains(card)) return
    const containerTop = container.getBoundingClientRect().top + container.clientTop
    const delta = card.getBoundingClientRect().top - containerTop - anchor.top
    if (delta) container.scrollTop += delta
  }, [cardOrder])

  return (
    <div
      ref={containerRef}
      role="region"
      aria-label="引用内容"
      data-ai-scroll="cards"
      className="ai-search-cards"
      onFocusCapture={keepFocusVisible}
    >
      {showSkeleton ? (
        <div aria-hidden="true">
          {Array.from({ length: 8 }, (_, index) => (
            <div key={index} className="ai-citation-card ai-citation-card-skeleton">
              <div className="ai-citation-card-text">
                <div className="ai-citation-skeleton-source">
                  <span className="ai-citation-skeleton-bar animate-pulse motion-reduce:animate-none" />
                </div>
                <div className="ai-citation-skeleton-title">
                  <span className="ai-citation-skeleton-bar animate-pulse motion-reduce:animate-none" />
                  <span className="ai-citation-skeleton-bar animate-pulse motion-reduce:animate-none" />
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : null}
      {!showSkeleton ? visibleOrder.map((id) => {
        const entry = itemById.get(id)
        if (!entry) return null
        return (
          <CitationCard key={id} item={entry.item} number={entry.n} highlighted={highlightItemId === id} />
        )
      }) : null}
    </div>
  )
}
