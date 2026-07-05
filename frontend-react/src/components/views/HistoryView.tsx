import { useEffect, useState, useCallback, useRef } from 'react'
import { Clock3 } from 'lucide-react'
import { fetchLibrary } from '../../lib/api'
import { InfoCard } from '../feed/InfoCard'
import { Masonry } from '../feed/Masonry'
import type { LibraryEntry } from '../../lib/types'
import { EventLibraryCard } from '../events/EventLibraryCard'
import { LibraryDateSectionHeader, LibraryEmptyState, LibraryPageHeader, LibraryPlatformFilter } from './LibraryChrome'

const PAGE_SIZE = 100

function groupByDate(items: LibraryEntry[]): { label: string; items: LibraryEntry[] }[] {
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const yesterday = new Date(today.getTime() - 86400000)

  const groups = {
    today: [] as LibraryEntry[],
    yesterday: [] as LibraryEntry[],
    earlier: [] as LibraryEntry[],
  }

  for (const item of items) {
    const d = new Date(item.occurred_at)
    if (d >= today) groups.today.push(item)
    else if (d >= yesterday) groups.yesterday.push(item)
    else groups.earlier.push(item)
  }

  // Sort each group by clicked_at DESC
  const sortDesc = (a: LibraryEntry, b: LibraryEntry) =>
    new Date(b.occurred_at).getTime() - new Date(a.occurred_at).getTime()
  groups.today.sort(sortDesc)
  groups.yesterday.sort(sortDesc)
  groups.earlier.sort(sortDesc)

  const result: { label: string; items: LibraryEntry[] }[] = []
  if (groups.today.length) result.push({ label: '今天', items: groups.today })
  if (groups.yesterday.length) result.push({ label: '昨天', items: groups.yesterday })
  if (groups.earlier.length) result.push({ label: '更早', items: groups.earlier })
  return result
}

function entryPlatforms(entry: LibraryEntry): string[] {
  return entry.type === 'item' ? [entry.item.platform] : entry.cluster.platforms
}

export function HistoryView() {
  const [items, setItems] = useState<LibraryEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)
  const [total, setTotal] = useState(0)
  const [platformFilter, setPlatformFilter] = useState<string | null>(null)
  const loadingMore = useRef(false)

  useEffect(() => {
    setLoading(true)
    fetchLibrary({ view: 'history', limit: PAGE_SIZE })
      .then((res) => {
        setItems(res.entries)
        setTotal(res.total)
      })
      .then(() => setLoadError(false))
      .catch((err) => {
        // UX-9(B8): 失败不再伪装成空态
        console.error('Failed to load history:', err)
        setLoadError(true)
      })
      .finally(() => setLoading(false))
  }, [reloadKey])

  const loadMore = useCallback(() => {
    if (loadingMore.current || items.length >= total) return
    loadingMore.current = true
    fetchLibrary({ view: 'history', limit: PAGE_SIZE, offset: items.length })
      .then((res) => {
        setItems((prev) => [...prev, ...res.entries])
      })
      .finally(() => {
        loadingMore.current = false
      })
  }, [items.length, total])

  const hasMore = items.length < total

  const header = (
    <LibraryPageHeader
      title="浏览历史"
      meta={loading ? '正在加载浏览记录' : `${total} 条 · 按最近浏览时间排列`}
    />
  )

  if (loading) {
    return (
      <div className="px-4 py-4">
        {header}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="h-48 rounded-[4px] bg-muted animate-skeleton" />
          ))}
        </div>
      </div>
    )
  }

  if (loadError && items.length === 0) {
    // UX-9(B8): 加载失败独立于空态呈现
    return (
      <div>
        {header}
        <div className="flex flex-col items-center justify-center gap-3 py-16 text-center" data-testid="library-error-state">
          <p className="text-[14px] text-muted-foreground">浏览记录加载失败,请重试</p>
          <button
            type="button"
            onClick={() => setReloadKey((k) => k + 1)}
            className="rounded-[4px] border border-border bg-card px-4 py-2 text-[13px] font-medium text-foreground transition-colors hover:border-[var(--brand-border)]"
          >
            重试
          </button>
        </div>
      </div>
    )
  }

  if (items.length === 0) {
    return (
      <LibraryEmptyState
        header={header}
        icon={Clock3}
        title="还没有浏览记录"
        description="点击信息卡片或事件弹窗后会自动记录"
      />
    )
  }

  // Platform pills
  const platforms = [...new Set(items.flatMap(entryPlatforms))]
  const filtered = platformFilter
    ? items.filter((entry) => entryPlatforms(entry).includes(platformFilter))
    : items
  const groups = groupByDate(filtered)

  return (
    <div className="px-4 py-4">
      {header}
      <LibraryPlatformFilter
        sectionKey="history"
        platforms={platforms}
        activePlatform={platformFilter}
        onSelect={setPlatformFilter}
      />

      {groups.map((group) => (
        <div key={group.label} className="mb-6">
          <LibraryDateSectionHeader label={group.label} count={group.items.length} />
          <Masonry
            items={group.items}
            renderItem={(item, i) => (
              item.type === 'item'
                ? <InfoCard item={item.item} delay={Math.min(i, 19) * 30} showReadState={false} />
                : <EventLibraryCard entry={item} delay={Math.min(i, 19) * 30} />
            )}
          />
        </div>
      ))}

      {/* Load more button */}
      {hasMore && (
        <div className="flex justify-center mt-6">
          <button
            onClick={loadMore}
            className="flex items-center gap-1.5 px-5 py-2 text-sm font-medium text-foreground bg-card border border-border hover:border-warm-400 shadow-subtle hover:shadow-medium rounded-full transition-all cursor-pointer"
          >
            加载更多
            <span className="text-xs text-muted-foreground">已加载 {items.length} / {total}</span>
          </button>
        </div>
      )}
    </div>
  )
}
