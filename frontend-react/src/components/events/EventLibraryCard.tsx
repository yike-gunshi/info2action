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
        'cursor-pointer rounded-[4px] border border-border bg-card p-4 outline-none',
        'transition-colors duration-150 hover:border-[var(--brand-border)] hover:bg-white/90 focus-visible:ring-2 focus-visible:ring-ring/35 dark:hover:bg-card/80',
        delay > 0 && 'animate-blur-fade',
      )}
      style={delay > 0 ? { animationDelay: `${delay}ms` } : undefined}
      aria-label={`打开事件：${title}`}
    >
      {showImage && (
        <div className="relative mb-3 overflow-hidden rounded-[4px] aspect-[16/9]" data-testid="event-library-card-media">
          <img
            src={proxiedImageUrl(coverUrl)}
            alt=""
            className="h-full w-full object-cover"
            loading="lazy"
            referrerPolicy="no-referrer"
            onError={() => setImgError(true)}
          />
        </div>
      )}

      <div className="mb-2 flex min-w-0 items-center gap-2 text-[13px] font-medium text-muted-foreground">
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

      <h3 className="mb-2 font-event-title text-[20px] font-semibold leading-[1.36] text-foreground line-clamp-3" data-testid="event-library-card-title">
        {title}
      </h3>

      {summary && (
        <p className="mt-2 font-event-title text-[16px] font-medium leading-[1.58] text-muted-foreground line-clamp-4" data-testid="event-library-card-summary">
          <span className="mr-0.5 text-[var(--brand)]">✦</span>
          {summary}
        </p>
      )}

      <div className="mt-3 flex items-center gap-3 border-t border-border pt-2.5 text-[13px] font-mono text-muted-foreground" data-testid="event-library-card-meta">
        <span className="inline-flex items-center gap-1">
          <Layers className="h-3.5 w-3.5" />
          {cluster.doc_count} 条
        </span>
        <span className="inline-flex items-center gap-1">
          <CalendarDays className="h-3.5 w-3.5" />
          {relativeTime(entry.occurred_at)}
        </span>
      </div>
    </article>
  )
}
