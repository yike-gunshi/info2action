/**
 * v15.0 EventCard — 时间线单 cluster 卡 (DESIGN.md §15.6)
 *
 * 日期与连续时间轴由 LatestEvents 容器统一渲染；单卡展示 cluster 首个 item 的 HH:mm。
 * 行间不使用分割线，靠时间、dot、留白和 hover 状态区分。
 * 整卡可点 → onSelect(cluster.id)
 *
 * v24 头版三档半行配方（lede/second/brief）已退役：全部卡片统一标准条渲染。
 */
import { memo, useEffect, useMemo, useState } from 'react'
import { cn } from '../../lib/utils'
import { parseClusterSummary } from '../../lib/cluster-summary-parser'
import { proxiedImageUrl } from '../../lib/media'
import type { ClusterEvent } from '../../lib/types'
import { eventCategoryLabel } from '../../lib/eventCategories'

interface EventCardProps {
  cluster: ClusterEvent
  onSelect: (id: number, cluster: ClusterEvent) => void
  onPrefetch?: (id: number) => void
  timeLabel?: string
  isFirstInGroup?: boolean
}

function formatEventClock(value?: string | null): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function plainSummaryText(value: string): string {
  return value
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/[*_~#]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

function EventMediaThumb({
  cluster,
  coverUrl,
  isRead,
  onError,
}: {
  cluster: ClusterEvent
  coverUrl: string
  isRead: boolean
  onError: () => void
}) {
  const [loaded, setLoaded] = useState(false)
  const imageUrl = proxiedImageUrl(coverUrl)

  useEffect(() => {
    setLoaded(false)
  }, [coverUrl])

  return (
    <img
      data-testid="event-media-thumb"
      src={imageUrl}
      alt={`${cluster.ai_title} 事件配图`}
      className={cn(
        'absolute inset-0 h-full w-full rounded-md object-cover transition-opacity',
        'aspect-[5/3]',
        'dark:border dark:border-border',
        // BF-0517-2: 去掉 ring-1 ring-border/70 — 在浅色主题下与图片透明边缘混合显蓝难看
        loaded ? 'opacity-100' : 'opacity-0',
        loaded && isRead && 'opacity-70 grayscale-[0.25]',
      )}
      loading="lazy"
      onLoad={() => setLoaded(true)}
      onError={onError}
    />
  )
}

// FE-7(B7): memo——列表级 store 变化(loading 翻转/markSeen/refresh)不再
// 重渲染未变化的卡片;onSelect 由父组件保证引用稳定
export const EventCard = memo(function EventCard({
  cluster,
  onSelect,
  onPrefetch,
  timeLabel,
  isFirstInGroup = false,
}: EventCardProps) {
  const handleClick = () => onSelect(cluster.id, cluster)
  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      onSelect(cluster.id, cluster)
    }
  }

  // FE-7(B7): 多条正则的 markdown 解析 memo 化——原先每次渲染重跑,
  // loadMore/markSeen/refresh 时已加载的全部卡片一起重解析
  const summaryText = useMemo(() => {
    const whyRead = cluster.why_read?.trim()
    if (whyRead) return plainSummaryText(whyRead)
    const summary = parseClusterSummary(cluster.ai_summary).speedReview
    return summary ? plainSummaryText(summary) : ''
  }, [cluster.ai_summary, cluster.why_read])
  const categoryLabel = eventCategoryLabel(cluster.category)
  const displayTime = cluster.first_doc_at || cluster.last_doc_at
  const displayTimeLabel = timeLabel ?? formatEventClock(displayTime)
  const readState: 'new' | 'read' = cluster.last_seen_version != null ? 'read' : 'new'
  const isRead = readState === 'read'
  const coverUrl = cluster.cover_url?.trim() || ''
  const [failedCoverUrl, setFailedCoverUrl] = useState<string | null>(null)
  const showImage = Boolean(coverUrl && failedCoverUrl !== coverUrl)
  const showInlineCategory = Boolean(categoryLabel)

  // v24.1: 时间列 52/56px + 栏距统一 16px（用户实物验收——时间列左侧空白过大）
  const gridColsClasses = showImage
    ? 'gap-x-4 sm:grid-cols-[52px_minmax(0,1fr)_200px] lg:grid-cols-[56px_minmax(0,1fr)_200px]'
    : 'gap-x-4 sm:grid-cols-[52px_minmax(0,1fr)] lg:grid-cols-[56px_minmax(0,1fr)]'

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={handleClick}
      onMouseEnter={() => onPrefetch?.(cluster.id)}
      onFocus={() => onPrefetch?.(cluster.id)}
      onKeyDown={handleKey}
      data-cluster-id={cluster.id}
      data-read-state={readState}
      data-has-media={showImage ? 'true' : 'false'}
      data-first-in-group={isFirstInGroup ? 'true' : 'false'}
      data-testid="event-card"
      className={cn(
        'cv-auto-event group cursor-pointer outline-none',
        'relative z-10 grid grid-cols-1 gap-y-2 border-b border-border/50 transition-[background-color,opacity] hover:bg-muted focus-visible:bg-muted sm:gap-y-0',
        'py-3.5 sm:py-4',
        gridColsClasses,
        isRead && 'opacity-60',
      )}
    >
      <div data-testid="event-card-layout" className="contents">
        <div data-testid="event-time-column" className="self-start text-left sm:text-right">
          <time
            data-testid="event-time"
            dateTime={displayTime || undefined}
            className="block self-start text-left font-mono text-[12px] font-medium tabular-nums leading-none text-muted-foreground sm:mt-[8px] sm:text-right sm:text-[14px]"
          >
            {displayTimeLabel}
          </time>
        </div>

        <div
          data-testid="event-content"
          className="min-w-0 rounded-md py-0.5 transition-colors group-focus-visible:ring-2 group-focus-visible:ring-ring/35 sm:px-1"
        >
          <h3 className="flex min-w-0 items-baseline gap-2 font-event-title text-[18px] font-medium leading-[1.32] text-foreground sm:text-[20px] sm:font-semibold">
            <span data-testid="event-title-text" className="min-w-0 line-clamp-2">
              {showInlineCategory && (
                <>
                  <span data-testid="event-category-label" className="text-[var(--brand)]">{categoryLabel}</span>
                  <span data-testid="event-category-separator">{' | '}</span>
                </>
              )}
              {cluster.ai_title}
            </span>
          </h3>

          {summaryText && (
            <p
              data-testid="event-summary"
              className="mt-2 font-event-title font-medium tracking-normal text-muted-foreground text-[16px] leading-[1.58] line-clamp-3"
            >
              {summaryText}
            </p>
          )}
        </div>

        {showImage && (
          <div
            data-testid="event-media-slot"
            className="relative hidden min-h-0 w-[200px] justify-self-end self-stretch overflow-hidden rounded-md sm:block sm:w-[200px] lg:w-[200px]"
          >
            <EventMediaThumb
              cluster={cluster}
              coverUrl={coverUrl}
              isRead={isRead}
              onError={() => setFailedCoverUrl(coverUrl)}
            />
          </div>
        )}
      </div>
    </div>
  )
})
