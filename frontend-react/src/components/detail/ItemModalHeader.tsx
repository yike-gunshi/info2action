import { ArrowLeft, X } from 'lucide-react'
import { cn, eventPlatformName, platformClass, relativeTime } from '../../lib/utils'
import type { FeedItem } from '../../lib/types'
import { PlatformBrandIcon } from '../shared/PlatformIcon'
import { paperSurfaceStyle } from './detailShared'

export function ItemModalHeader({
  item,
  canGoBack,
  goBack,
  handleClose,
}: {
  item: FeedItem
  canGoBack: boolean
  goBack: () => void
  handleClose: () => void
}) {
  const platformLabel = eventPlatformName(item.platform)
  const time = item.published_at || item.fetched_at
  const sourceLabel = item.author_name?.trim() || '来源'

  return (
    <header
      data-testid="detail-modal-header"
      className="shrink-0 bg-[var(--modal-surface)] px-4 py-5 sm:px-10"
      style={paperSurfaceStyle}
    >
      <div className="flex items-start gap-4">
        {canGoBack && (
          <button
            type="button"
            onClick={goBack}
            aria-label="返回上一条"
            title="返回"
            className="mt-0.5 relative flex h-7 w-7 shrink-0 items-center justify-center rounded-[5px] text-[var(--modal-text-faint)] before:absolute before:-inset-2 before:content-[''] transition-colors hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)]"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
          </button>
        )}

        <div className="min-w-0 flex-1">
          <h2
            id="detail-modal-title"
            data-testid="detail-title"
            className="reading-title line-clamp-2"
            title={item.title}
          >
            {item.title}
          </h2>
          <div
            className="reading-meta mt-2 flex min-w-0 items-center gap-2"
            data-testid="detail-source-line"
          >
            <span
              className={cn(
                'inline-flex h-[20px] w-[20px] shrink-0 items-center justify-center rounded-full border-2 border-[var(--modal-surface)] text-[10px] leading-none shadow-[0_1px_2px_rgba(26,25,23,0.16)]',
                platformClass(item.platform),
              )}
              title={platformLabel}
              aria-hidden="true"
            >
              <PlatformBrandIcon platform={item.platform} className="h-[66%] w-[66%]" />
            </span>
            <span className="min-w-0 truncate">{sourceLabel}</span>
            {time && (
              <>
                <span className="shrink-0 text-[var(--modal-text-subtle)]">·</span>
                <time className="shrink-0 font-mono tabular-nums" dateTime={time}>
                  {relativeTime(time)}
                </time>
              </>
            )}
          </div>
        </div>

        <div className="flex w-8 shrink-0 items-center justify-end" data-testid="detail-header-actions">
          <button
            type="button"
            onClick={handleClose}
            aria-label="关闭"
            title="关闭"
            className="relative flex h-7 w-7 items-center justify-center rounded-[5px] before:absolute before:-inset-2 before:content-[''] text-[var(--modal-text-faint)] transition-colors hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)]"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </header>
  )
}
