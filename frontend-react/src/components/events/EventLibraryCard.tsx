import { useEffect, useState } from 'react'
import { CalendarDays, Layers } from 'lucide-react'
import type { LibraryClusterEntry } from '../../lib/types'
import { cn, eventPlatformName, relativeTime, stripMd } from '../../lib/utils'
import { parseClusterSummary } from '../../lib/cluster-summary-parser'
import { proxiedImageUrl } from '../../lib/media'
import { PlatformBrandIcon } from '../shared/PlatformIcon'
import { useClusterDetailStore } from '../../store/clusterDetailStore'
import { eventCategoryLabel } from '../../lib/eventCategories'

function summaryText(value?: string | null): string {
  const parsed = parseClusterSummary(value)
  const raw = parsed.speedReview || value || ''
  return stripMd(raw).replace(/\s+/g, ' ').trim()
}

function platformTitle(platforms: string[]): string {
  return platforms.map(eventPlatformName).join('、')
}

export function EventLibraryCard({ entry, delay = 0 }: { entry: LibraryClusterEntry; delay?: number }) {
  const cluster = entry.cluster
  const openModal = useClusterDetailStore((s) => s.openModal)
  const prefetchBundle = useClusterDetailStore((s) => s.prefetchBundle)
  const [imgError, setImgError] = useState(false)
  const coverUrl = cluster.cover_url?.trim() || ''
  const showImage = Boolean(coverUrl && !imgError)
  const summary = summaryText(cluster.ai_summary)
  const category = eventCategoryLabel(cluster.category)
  const title = cluster.ai_title?.trim() || '未命名事件'
  const visiblePlatforms = Array.from(new Set(cluster.platforms || [])).slice(0, 3)
  const platformOverflow = Math.max(0, (cluster.platforms || []).length - visiblePlatforms.length)

  useEffect(() => {
    setImgError(false)
  }, [cluster.id, coverUrl])

  // v24.0 §21.7: 对齐 §21.2 标准条行配方 —— 无卡片盒，20px 衬线题 + 2 行摘
  // + mono meta；图右置 200×120（移动端升通栏 16:9 顶置），行间 hairline 由列表容器分隔。
  return (
    <article
      role="button"
      tabIndex={0}
      onClick={() => openModal(cluster.id)}
      onMouseEnter={() => prefetchBundle(cluster.id)}
      onFocus={() => prefetchBundle(cluster.id)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          openModal(cluster.id)
        }
      }}
      data-testid="event-library-card"
      data-cluster-id={cluster.id}
      data-has-media={showImage ? 'true' : 'false'}
      className={cn(
        'group flex cursor-pointer flex-col gap-3 py-3.5 outline-none sm:flex-row sm:items-start sm:gap-5',
        'transition-colors duration-150 hover:bg-muted/60 focus-visible:bg-muted/70',
        delay > 0 && 'animate-blur-fade',
      )}
      style={delay > 0 ? { animationDelay: `${delay}ms` } : undefined}
      aria-label={`打开事件：${title}`}
    >
      <div className="min-w-0 flex-1">
        <div className="mb-1.5 flex min-w-0 items-center gap-2 text-[13px] font-medium text-muted-foreground">
          <span
            className="inline-flex min-w-0 items-center gap-1.5"
            title={platformTitle(cluster.platforms || [])}
            data-testid="event-library-card-platforms"
          >
            {visiblePlatforms.map((platform) => (
              <span key={platform} className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-muted text-muted-foreground">
                <PlatformBrandIcon platform={platform} className="h-3.5 w-3.5" />
              </span>
            ))}
            {platformOverflow > 0 && <span className="text-[12px]">+{platformOverflow}</span>}
          </span>
          {category && (
            <span className="shrink-0 text-[var(--brand)]" data-testid="event-library-card-category">
              {category}
            </span>
          )}
        </div>

        <h3 className="font-event-title text-[20px] font-semibold leading-[1.32] text-foreground line-clamp-2" data-testid="event-library-card-title">
          {title}
        </h3>

        {summary && (
          <p className="mt-1.5 font-event-title text-[16px] font-normal leading-[1.58] text-muted-foreground line-clamp-2" data-testid="event-library-card-summary">
            <span className="mr-0.5 text-[var(--brand)]">✦</span>
            {summary}
          </p>
        )}

        <div className="mt-2.5 flex items-center gap-3 font-mono text-[12px] text-muted-foreground" data-testid="event-library-card-meta">
          <span className="inline-flex items-center gap-1">
            <Layers className="h-3.5 w-3.5" />
            {cluster.doc_count} 条
          </span>
          <span className="inline-flex items-center gap-1">
            <CalendarDays className="h-3.5 w-3.5" />
            {relativeTime(entry.occurred_at)}
          </span>
        </div>
      </div>

      {showImage && (
        <div
          className="relative order-first shrink-0 overflow-hidden rounded-[4px] aspect-[16/9] sm:order-none sm:aspect-auto sm:h-[120px] sm:w-[200px]"
          data-testid="event-library-card-media"
        >
          <img
            src={proxiedImageUrl(coverUrl)}
            alt=""
            className="h-full w-full object-cover dark:brightness-[.92]"
            loading="lazy"
            referrerPolicy="no-referrer"
            onError={() => setImgError(true)}
          />
        </div>
      )}
    </article>
  )
}
