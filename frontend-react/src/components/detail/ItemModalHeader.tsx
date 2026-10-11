import { cn, eventPlatformName, platformClass } from '../../lib/utils'
import type { FeedItem } from '../../lib/types'
import { PlatformBrandIcon } from '../shared/PlatformIcon'
import { ReadingModalHeader } from '../reading/ReadingModal'
import { formatReadingDateTime } from '../reading/readingUtils'

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

  const meta = (
    <>
      <span className="flex min-w-0 items-center gap-1.5">
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
      </span>
      {time && (
        <>
          <span className="shrink-0 text-[var(--modal-text-subtle)]">·</span>
          <time className="shrink-0 font-mono tabular-nums" dateTime={time}>
            {formatReadingDateTime(time)}
          </time>
        </>
      )}
    </>
  )

  return (
    <ReadingModalHeader
      canGoBack={canGoBack}
      headerTestId="detail-modal-header"
      meta={meta}
      metaTestId="detail-source-line"
      onBack={goBack}
      onClose={handleClose}
      title={item.title}
      titleId="detail-modal-title"
      titleTestId="detail-title"
    />
  )
}
