import { useMemo, useState, useEffect, useCallback, useRef, useLayoutEffect } from 'react'
import type { FeedItem } from '../../lib/types'

type MasonryItem = { id: string }
type EstimatedFeedShape = MasonryItem & Partial<FeedItem>

/** Rough height estimate — used for initial render before actual measurement. */
function estimateHeight(item: MasonryItem): number {
  const feedLike = item as EstimatedFeedShape
  let h = 96 // padding + source row + bottom meta + gaps
  const hasImage = feedLike.cover_url || feedLike.thumbnail || (feedLike.media_json && feedLike.media_json.length > 0)
  if (hasImage) h += 220
  const titleLen = (feedLike.title || '').length
  h += Math.min(Math.ceil(titleLen / 24), 3) * 28
  const summary = feedLike.ai_summary || feedLike.description || feedLike.content || ''
  if (summary) {
    const len = summary.length
    h += Math.min(Math.ceil(len / 32), 3) * 24 + 16
  }
  return h
}

/** Distribute items into columns by always picking the shortest column. */
function distributeToColumns<T extends MasonryItem>(
  items: T[],
  colCount: number,
  heightMap: Map<string, number>,
): T[][] {
  const cols: T[][] = Array.from({ length: colCount }, () => [])
  const heights = new Array(colCount).fill(0)

  items.forEach((item) => {
    const h = heightMap.get(item.id) ?? estimateHeight(item)
    const shortest = heights.indexOf(Math.min(...heights))
    cols[shortest].push(item)
    heights[shortest] += h + 12 // 12px gap
  })

  return cols
}

function getColumnCount(): number {
  if (typeof window === 'undefined') return 3
  const w = window.innerWidth
  if (w < 640) return 1
  if (w < 1024) return 2
  return 3
}

export function Masonry<T extends MasonryItem>({ items, renderItem, columns: columnsProp }: {
  items: T[]
  renderItem: (item: T, index: number) => React.ReactNode
  columns?: number
}) {
  const [autoColumns, setAutoColumns] = useState(getColumnCount)
  const containerRef = useRef<HTMLDivElement>(null)
  const heightCache = useRef<Map<string, number>>(new Map())
  const [revision, setRevision] = useState(0)
  const measured = useRef(false)

  const handleResize = useCallback(() => {
    setAutoColumns(getColumnCount())
    heightCache.current.clear() // column width changes → invalidate
  }, [])

  useEffect(() => {
    if (columnsProp) return
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [columnsProp, handleResize])

  const colCount = columnsProp ?? autoColumns
  const cols = useMemo(
    () => distributeToColumns(items, colCount, heightCache.current),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [items, colCount, revision],
  )

  // Reset measurement flag when items or column count change
  useEffect(() => {
    measured.current = false
  }, [items, colCount])

  // Measure actual card heights once after render (before paint), redistribute if needed
  useLayoutEffect(() => {
    if (measured.current || !containerRef.current) return
    const cards = containerRef.current.querySelectorAll<HTMLElement>('[data-masonry-id]')
    if (cards.length === 0) return
    let changed = false
    cards.forEach((el) => {
      const id = el.getAttribute('data-masonry-id')!
      const h = el.offsetHeight
      const cached = heightCache.current.get(id)
      if (!cached || Math.abs(cached - h) > 4) {
        heightCache.current.set(id, h)
        changed = true
      }
    })
    if (changed) {
      measured.current = true
      setRevision((r) => r + 1)
    }
  })

  return (
    <div ref={containerRef} className="flex items-start gap-4 sm:gap-6" data-testid="masonry-columns">
      {cols.map((colItems, colIdx) => (
        <div key={colIdx} className="flex min-w-0 flex-1 flex-col gap-4 sm:gap-6">
          {colItems.map((item, itemIdx) => (
            <div key={item.id} data-masonry-id={item.id}>
              {renderItem(item, itemIdx)}
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}
