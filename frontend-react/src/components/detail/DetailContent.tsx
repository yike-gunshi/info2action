import { useState } from 'react'
import { ChevronDown, ImageOff, TriangleAlert } from 'lucide-react'
import { useDetailStore } from '../../store/detailStore'
import { cn } from '../../lib/utils'
import { renderMarkdownInline } from '../../lib/markdown-lite'
import { proxiedImageUrl } from '../../lib/media'
import type { FeedItem } from '../../lib/types'
import { ActionZone } from './ActionZone'
import { VideoPlayer } from './VideoPlayer'
import { YoutubePlayer } from './YoutubePlayer'
import { TranscriptPanel } from './TranscriptPanel'
import { SummaryUpdatedBadge } from './SummaryUpdatedBadge'
import { collectImages, extractVideoMp4Url, normalizeSummaryText, paragraphLines } from './detailContentUtils'
import type { KeyPointItem } from './detailContentUtils'
import { mediaCapStyle } from './detailShared'

export interface DetailContentProps {
  item: FeedItem
  contentExpanded: boolean
  setContentExpanded: (v: boolean) => void
  setLightboxSrc: (v: string | null) => void
  setLightboxImages: (v: string[]) => void
}

export function DetailContent({
  item,
  contentExpanded,
  setContentExpanded,
  setLightboxSrc,
  setLightboxImages,
}: DetailContentProps) {
  // Collect all images from media_json + cover_url + thumbnail
  const images = collectImages(item)
  // v12.2: 检测是否为视频帖 (media_json 首个 item type === 'video')
  const videoMp4Url = extractVideoMp4Url(item)
  // BF-0419-20: YouTube 走 iframe embed,video_id 来自 item.id (yt_{id})
  const youtubeVideoId = item.platform === 'youtube' && item.id.startsWith('yt_')
    ? item.id.slice(3)
    : null
  const hasVideo = !!videoMp4Url || !!youtubeVideoId
  const content = item.content || item.description || ''
  const TRUNCATE_LEN = 1000
  const needsTruncation = content.length > TRUNCATE_LEN
  const displayContent = contentExpanded ? content : content.slice(0, TRUNCATE_LEN)
  const hasSummaryBlock = !!normalizeSummaryText(item.ai_summary) || !!item.ai_key_points?.length

  // v12.2 ASR state for SummaryUpdatedBadge + failed_summary banner
  const asrSummaryUpdated = useDetailStore((s) => s.asrSummaryUpdated)
  const clearSummaryBadge = useDetailStore((s) => s.clearSummaryBadge)
  const retrySummary = useDetailStore((s) => s.retrySummary)
  const isSummaryFailed = item.asr_status === 'failed_summary'

  return (
    <>
      {/* 1. Media first for v19 single/multi-image modal variants; no-image keeps this area empty. */}
      {videoMp4Url && (
        <VideoPlayer mp4Url={videoMp4Url} itemId={item.id} />
      )}

      {!videoMp4Url && youtubeVideoId && (
        <YoutubePlayer videoId={youtubeVideoId} itemId={item.id} />
      )}

      {hasVideo && (
        <TranscriptPanel itemId={item.id} item={item} />
      )}

      {!hasVideo && images.length > 0 && (
        <ImageGrid images={images} onClickImage={(src) => { setLightboxImages(images); setLightboxSrc(src) }} />
      )}

      {/* 3a. v12.2: SummaryUpdatedBadge (ASR 刚刷新摘要后短暂显示) */}
      {asrSummaryUpdated && (
        <SummaryUpdatedBadge onExpired={clearSummaryBadge} />
      )}

      {/* 3b. v12.2: failed_summary 降级 banner
          v24.0 §21.6: emoji ⚠️ → lucide TriangleAlert;硬编码琥珀 → score 语义 token */}
      {isSummaryFailed && (
        <div
          role="alert"
          className="mb-2 flex items-center justify-between gap-2 rounded-[4px] px-3 py-2 text-[13px]"
          style={{
            background: 'var(--score-high-bg)',
            border: '1px solid color-mix(in srgb, var(--score-high) 32%, transparent)',
            color: 'var(--score-high)',
          }}
        >
          <span className="flex items-center gap-1.5">
            <TriangleAlert className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            转写已就绪,摘要暂未更新
          </span>
          <button
            onClick={() => retrySummary(item.id)}
            className="text-sm font-medium text-[var(--brand)] hover:underline"
          >
            重试摘要
          </button>
        </div>
      )}

      <ItemSummaryBlock
        summary={item.ai_summary}
        keyPoints={item.ai_key_points}
      />

      {/* 4. Original content */}
      {content && (
        <div
          className={cn('mb-4', hasSummaryBlock && 'border-t border-[var(--modal-divider)] pt-4')}
          data-testid="detail-body-content"
        >
          <div
            data-testid="detail-body-text"
            className="reading-body space-y-2.5"
            style={{ wordBreak: 'break-word' }}
          >
            {displayContent.split('\n').map((line, i) => (
              <p key={i}>
                {i === 0 && (
                  <strong data-testid="detail-original-label" className="mr-1.5 !text-[var(--brand)]">
                    原文：
                  </strong>
                )}
                {line || '\u00A0'}
              </p>
            ))}
            {needsTruncation && !contentExpanded && <span className="text-[var(--modal-text-faint)]">...</span>}
          </div>
          {needsTruncation && (
            <button
              onClick={() => setContentExpanded(!contentExpanded)}
              className="mt-2 flex items-center gap-1 text-sm font-medium text-[var(--brand)] transition-colors hover:text-[var(--brand)]"
            >
              {contentExpanded ? '收起' : '展开全文'}
              <ChevronDown className={cn('w-3 h-3 transition-transform', contentExpanded && 'rotate-180')} />
            </button>
          )}
        </div>
      )}

      {/* v21.0 action-revival: 行动点区块挂正文末,加同款分隔线过渡到"生成行动"。 */}
      <section
        data-testid="detail-action-zone"
        className={cn((hasSummaryBlock || content) && 'border-t border-[var(--modal-divider)] pt-4 mt-2')}
      >
        <ActionZone itemId={item.id} />
      </section>
    </>
  )
}

function ItemSummaryBlock({
  summary,
  keyPoints,
}: {
  summary?: string | null
  keyPoints?: KeyPointItem[] | null
}) {
  const normalizedSummary = normalizeSummaryText(summary)
  const hasPoints = !!keyPoints?.length
  if (!normalizedSummary && !hasPoints) return null

  return (
    <section
      data-testid="detail-ai-summary"
      className="mb-5 text-[var(--modal-text-soft)]"
    >
      {normalizedSummary && (
        <div
          data-testid="detail-summary-lead"
          className={cn(
            'reading-body pb-5',
            hasPoints && 'border-b border-[var(--modal-divider)]',
          )}
        >
          {paragraphLines(normalizedSummary).map((line, index) => (
            <p key={index} className={index > 0 ? 'mt-2.5' : undefined}>
              {index === 0 && (
                <strong className="mr-1.5 !text-[var(--brand)]">精华速览：</strong>
              )}
              {renderMarkdownInline(line)}
            </p>
          ))}
        </div>
      )}

      {hasPoints && (
        <ul data-testid="detail-key-points" className={cn(
          'reading-bullet space-y-1 py-4 pl-6 sm:pl-[38px]',
        )}>
          {(keyPoints ?? []).map((point, index) => {
            if (typeof point === 'string') {
              return (
                <li key={index} className="relative">
                  <span className="absolute -left-3.5 top-[0.78em] h-1 w-1 rounded-full bg-[var(--modal-text)] sm:-left-4" aria-hidden="true" />
                  {renderMarkdownInline(point)}
                </li>
              )
            }
            return (
              <li key={index} className="relative -ml-6 pb-3.5 last:pb-0 sm:-ml-[38px]">
                <div className="mb-2 flex items-baseline gap-2.5">
                  <span className="reading-section leading-none text-[var(--brand)]">
                    {String(index + 1).padStart(2, '0')}
                  </span>
                  <div className="reading-section min-w-0">
                    {renderMarkdownInline(point.title)}
                  </div>
                </div>
                {point.points?.length > 0 && (
                  <ul className="reading-bullet space-y-1 pl-6 sm:pl-[38px]">
                    {point.points.map((subPoint, subIndex) => (
                      <li key={subIndex} className="relative">
                        <span className="absolute -left-3.5 top-[0.78em] h-1 w-1 rounded-full bg-[var(--modal-text)] sm:-left-4" aria-hidden="true" />
                        {renderMarkdownInline(subPoint)}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

/** v12.3 BF-0418-XIMG: image onError fallback avoids broken browser icons. */
function GridImage({ src }: { src: string }) {
  const [err, setErr] = useState(false)
  if (err) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-[var(--modal-surface-muted)] text-[var(--modal-text-faint)]">
        <ImageOff className="w-6 h-6" />
      </div>
    )
  }
  return (
    <img
      src={src}
      alt=""
      className="h-full w-full cursor-zoom-in object-cover transition-opacity hover:opacity-95"
      onError={() => setErr(true)}
      loading="lazy"
      referrerPolicy="no-referrer"
    />
  )
}

function ImageGrid({ images, onClickImage }: { images: string[]; onClickImage: (src: string) => void }) {
  const primaryUrl = images[0]
  const extraCount = Math.max(0, images.length - 1)

  return (
    <figure className="mb-6">
      <button
        type="button"
        data-testid="detail-media-grid"
        data-media-count={String(images.length)}
        data-media-layout={images.length === 1 ? 'single' : 'stacked'}
        aria-label="放大查看图片"
        onClick={() => onClickImage(primaryUrl)}
        style={mediaCapStyle}
        className="group/media relative block w-full overflow-hidden rounded-[8px] border border-[var(--modal-border)] bg-[var(--modal-surface-muted)] p-0 text-left shadow-[0_1px_0_rgba(255,255,255,0.72)]"
      >
        <GridImage src={proxiedImageUrl(primaryUrl)} />
        {extraCount > 0 && (
          <span className="absolute bottom-2 right-2 rounded-full border border-white/35 bg-black/70 px-2 py-0.5 font-mono text-[11px] font-semibold leading-none text-white backdrop-blur-sm">
            +{extraCount}
          </span>
        )}
      </button>
    </figure>
  )
}
