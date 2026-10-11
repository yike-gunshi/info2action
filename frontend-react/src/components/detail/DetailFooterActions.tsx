import { useCallback } from 'react'
import { Bookmark, ExternalLink, Share2 } from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '../../lib/utils'
import type { FeedItem } from '../../lib/types'
import { ReadingModalFooter, readingModalFooterButtonClass } from '../reading/ReadingModal'
import { buildItemShareText, copyTextToClipboard } from './detailShared'

export function DetailFooterActions({ item, handleStar }: { item: FeedItem; handleStar: () => void }) {
  const handleShare = useCallback(async () => {
    try {
      await copyTextToClipboard(buildItemShareText(item))
      toast.success('分享链接已复制')
    } catch {
      toast.error('分享失败')
    }
  }, [item])

  return (
    <ReadingModalFooter
      className={item.url ? 'grid-cols-3' : 'grid-cols-2'}
      testId="detail-bottom-actions"
    >
      <button
        type="button"
        onClick={handleStar}
        data-testid="detail-footer-star-button"
        className={cn(readingModalFooterButtonClass, item.starred_at && 'text-[var(--brand)]')}
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
          className={readingModalFooterButtonClass}
          aria-label="跳转原文"
        >
          <ExternalLink className="h-3.5 w-3.5" />
          跳转原文
        </a>
      )}
      <button type="button" onClick={handleShare} data-testid="detail-footer-share-button" className={readingModalFooterButtonClass}>
        <Share2 className="h-3.5 w-3.5" />
        分享
      </button>
    </ReadingModalFooter>
  )
}
