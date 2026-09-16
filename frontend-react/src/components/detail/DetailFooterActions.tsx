import { useCallback } from 'react'
import { Bookmark, ExternalLink, Share2 } from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '../../lib/utils'
import type { FeedItem } from '../../lib/types'
import { buildItemShareText, copyTextToClipboard, paperSurfaceStyle } from './detailShared'

export function DetailFooterActions({ item, handleStar }: { item: FeedItem; handleStar: () => void }) {
  const bottomActionClass = 'flex h-12 w-full items-center justify-center gap-1.5 text-[13px] font-medium text-[var(--modal-text-muted)] transition-colors hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-inset'
  const handleShare = useCallback(async () => {
    try {
      await copyTextToClipboard(buildItemShareText(item))
      toast.success('分享链接已复制')
    } catch {
      toast.error('分享失败')
    }
  }, [item])

  return (
    <div
      className={cn(
        'modal-safe-footer grid flex-shrink-0 overflow-hidden border-t border-[var(--modal-border-soft)] bg-[var(--modal-surface)]',
        // E2: 无原文链接时用两列均分收藏/分享,不再留中间死区占位
        item.url ? 'grid-cols-3' : 'grid-cols-2',
      )}
      data-testid="detail-bottom-actions"
      style={paperSurfaceStyle}
    >
      <button
        type="button"
        onClick={handleStar}
        data-testid="detail-footer-star-button"
        className={cn(bottomActionClass, item.starred_at && 'text-[var(--brand)]')}
      >
        <Bookmark className={cn('h-3.5 w-3.5', item.starred_at && 'fill-current')} />
        {item.starred_at ? '已收藏' : '收藏'}
      </button>
      {item.url && (
        <a
          href={item.url}
          target="_blank"
          rel="noopener noreferrer"
          data-testid="detail-footer-original-link"
          className={bottomActionClass}
          aria-label="跳转原文"
        >
          <ExternalLink className="h-3.5 w-3.5" />
          跳转原文
        </a>
      )}
      <button type="button" onClick={handleShare} data-testid="detail-footer-share-button" className={bottomActionClass}>
        <Share2 className="h-3.5 w-3.5" />
        分享
      </button>
    </div>
  )
}
