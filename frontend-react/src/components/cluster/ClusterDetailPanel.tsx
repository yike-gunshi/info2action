import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Bookmark, ChevronLeft, ChevronRight, ExternalLink, EyeOff, Share2, ThumbsDown } from 'lucide-react'
import { toast } from 'sonner'
import { useClusterDetailStore } from '../../store/clusterDetailStore'
import type { ClusterDetail, ClusterMedia, ClusterSource } from '../../lib/types'
import { cn, eventPlatformName, platformClass, safeExternalUrl, stripMd } from '../../lib/utils'
import { parseClusterBreakdownSections, parseClusterSummary } from '../../lib/cluster-summary-parser'
import { proxiedImageUrl } from '../../lib/media'
import { PlatformBrandIcon } from '../shared/PlatformIcon'
import { requireAuth } from '../shared/AuthGate'
import { ReadingLightbox } from '../reading/ReadingLightbox'
import {
  ReadingModalBody,
  ReadingModalError,
  ReadingModalFooter,
  ReadingModalHeader,
  ReadingModalLoading,
  ReadingModalShell,
  readingModalFooterButtonClass,
} from '../reading/ReadingModal'
import { ReadingSummary } from '../reading/ReadingSummary'
import { formatReadingDateTime } from '../reading/readingUtils'
import type { ReadingKeyPoint } from '../reading/readingUtils'
import { buildInfoItemHref } from '../../lib/itemDeepLink'
import { ClusterActionZone } from './ClusterActionZone'
import { YoutubePlayer } from '../detail/YoutubePlayer'
import { copyTextToClipboard, mediaCapStyle } from '../detail/detailShared'

type ModalVariant = 'no-media' | 'single-media' | 'multi-media'

const INFO2ACT_SHARE_BASE_URL = 'https://www.info2act.com'

function compactClusterShareSummary(cluster: ClusterDetail): string {
  const parsed = parseClusterSummary(cluster.ai_summary)
  const raw = parsed.speedReview || cluster.ai_summary || ''
  const normalized = stripMd(raw).replace(/\s+/g, ' ').trim()
  if (!normalized) return ''
  return normalized.length > 100 ? `${normalized.slice(0, 100)}...` : normalized
}

function buildClusterShareText(cluster: ClusterDetail): string {
  const title = cluster.ai_title?.trim() || '一个事件'
  const summary = compactClusterShareSummary(cluster)
  const deepLink = `${INFO2ACT_SHARE_BASE_URL}#cluster=${cluster.id}`
  return `我正在 info2act 浏览「${title}」：${summary}\n一起看看吧 ${deepLink}`
}

function OfficialBadge() {
  return (
    <span className="inline-flex shrink-0 items-center rounded-[4px] bg-[var(--badge-official-bg)] px-1.5 py-0.5 text-[10px] font-medium leading-none text-[var(--badge-official-fg)]">
      官方
    </span>
  )
}

function uniquePush(urls: string[], value?: string | null) {
  const url = safeMediaUrl(value)
  if (url && !urls.includes(url)) urls.push(url)
}

function safeMediaUrl(value?: string | null): string | null {
  const url = value?.trim()
  if (!url) return null
  if (url.startsWith('/') && !url.startsWith('//')) return url
  return safeExternalUrl(url)
}

function collectMediaUrls(cluster: ClusterDetail, sources: ClusterSource[]): string[] {
  const urls: string[] = []
  cluster.media_urls?.forEach((url) => uniquePush(urls, url))
  uniquePush(urls, cluster.cover_url)
  sources.forEach((source) => {
    source.media_urls?.forEach((url) => uniquePush(urls, url))
    uniquePush(urls, source.cover_url)
  })
  return urls
}

function isGeneratedVideoPoster(url: string): boolean {
  return url.includes('/api/media/twitter-poster/') || url.includes('/video_posters/')
}

function collectVideoPosterUrls(cluster: ClusterDetail, sources: ClusterSource[]): Set<string> {
  const posters = new Set<string>()
  const media = [...(cluster.media ?? []), ...sources.flatMap((source) => source.media ?? [])]
  media.forEach((asset) => {
    if (asset.type !== 'video' && asset.type !== 'embed') return
    const poster = safeMediaUrl(asset.poster_url)
    if (poster) posters.add(poster)
  })
  return posters
}

function modalVariantFor(mediaCount: number): ModalVariant {
  if (mediaCount <= 0) return 'no-media'
  if (mediaCount === 1) return 'single-media'
  return 'multi-media'
}

function youtubeVideoId(media: ClusterMedia | null | undefined): string | null {
  if (!media || (media.provider !== 'youtube' && !/youtu\.be|youtube\.com/i.test(media.url))) return null
  try {
    const url = new URL(media.url)
    return url.searchParams.get('v') || url.pathname.split('/').filter(Boolean).pop() || null
  } catch {
    return null
  }
}

function redditEmbedUrl(media: ClusterMedia | null | undefined): string | null {
  if (!media || media.type !== 'embed' || media.provider?.toLowerCase() !== 'reddit') return null
  const safeUrl = safeExternalUrl(media.url)
  if (!safeUrl) return null
  try {
    const url = new URL(safeUrl)
    return url.protocol === 'https:' && url.hostname === 'www.redditmedia.com' ? url.href : null
  } catch {
    return null
  }
}

function twitterVideoItemId(media: ClusterMedia | null | undefined, sources: ClusterSource[]): string | null {
  if (!media || media.type !== 'video') return null
  const mediaUrl = safeMediaUrl(media.url)
  if (!mediaUrl) return null

  const matchingSource = sources.find((source) =>
    source.platform === 'twitter'
      && source.media?.some((asset) => asset.type === 'video' && safeMediaUrl(asset.url) === mediaUrl),
  )
  if (matchingSource?.item_id) return matchingSource.item_id

  const sourceUrl = safeExternalUrl(media.source_url)
  if (!sourceUrl) return null
  try {
    const url = new URL(sourceUrl)
    if (!['x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'].includes(url.hostname.toLowerCase())) return null
    return url.pathname.match(/\/status\/(\d+)/)?.[1] ?? null
  } catch {
    return null
  }
}

function ClusterMediaBlock({
  urls,
  video,
  videoItemId,
  onOpen,
  onError,
  isLightboxOpen,
}: {
  urls: string[]
  video?: ClusterMedia | null
  videoItemId?: string | null
  onOpen: (url: string, images: string[]) => void
  onError: (url: string) => void
  isLightboxOpen: boolean
}) {
  const [activeIndex, setActiveIndex] = useState(0)
  const [stripIndex, setStripIndex] = useState(0)
  const [isHovered, setIsHovered] = useState(false)
  // D5: 触屏无 hover,自动轮播每 4s 跳图无法停。用户首次交互(触摸/点点)后
  // 永久暂停自动轮播,改为纯手动;换事件(carouselKey 变)时重置。
  const [userPaused, setUserPaused] = useState(false)
  const stripRef = useRef<HTMLDivElement>(null)
  const safeVideoUrl = safeMediaUrl(video?.url)
  const safePosterUrl = safeMediaUrl(video?.poster_url)
  const safeSourceUrl = safeExternalUrl(video?.source_url) || safeExternalUrl(video?.url)
  const safeVideo = video && safeVideoUrl ? { ...video, url: safeVideoUrl } : null
  const videoId = youtubeVideoId(safeVideo)
  const redditUrl = redditEmbedUrl(safeVideo)
  const hasPlayableVideo = safeVideo?.type === 'video'
  const hasExternalMedia = safeVideo?.type === 'embed' && !videoId && !redditUrl && Boolean(safeSourceUrl)
  const hasPrimaryMedia = Boolean(videoId || redditUrl || hasPlayableVideo || hasExternalMedia)
  const hasCarousel = !hasPrimaryMedia && urls.length > 1
  const hasStripControls = hasPrimaryMedia && urls.length > 1
  const playableVideoUrl = videoItemId ? `/api/media/twitter-mp4/${encodeURIComponent(videoItemId)}` : safeVideoUrl
  const carouselKey = urls.join('\u0001')
  const activeUrl = urls[activeIndex] ?? urls[0] ?? ''

  useEffect(() => {
    setActiveIndex(0)
    setStripIndex(0)
    setIsHovered(false)
    setUserPaused(false)
  }, [carouselKey])

  useEffect(() => {
    if (!hasCarousel || isHovered || isLightboxOpen || userPaused) return
    const timer = window.setInterval(() => {
      setActiveIndex((index) => (index + 1) % urls.length)
    }, 4000)
    return () => window.clearInterval(timer)
  }, [activeIndex, carouselKey, hasCarousel, isHovered, isLightboxOpen, userPaused, urls.length])

  if (urls.length === 0 && !videoId && !redditUrl && !hasPlayableVideo && !hasExternalMedia) return null

  const moveStripTo = (nextIndex: number) => {
    const boundedIndex = Math.max(0, Math.min(nextIndex, urls.length - 1))
    setStripIndex(boundedIndex)
    const strip = stripRef.current
    const first = strip?.children.item(0) as HTMLElement | null
    const target = strip?.children.item(boundedIndex) as HTMLElement | null
    strip?.scrollTo?.({
      left: target && first ? target.offsetLeft - first.offsetLeft : 0,
      behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
    })
  }

  return (
    <figure className="mb-6 space-y-3">
      {videoId && <YoutubePlayer videoId={videoId} itemId={`cluster_${videoId}`} />}
      {redditUrl && (
        <div className="space-y-2">
          <iframe
            data-testid="cluster-media-reddit"
            src={redditUrl}
            title="Reddit 视频"
            loading="lazy"
            sandbox="allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox"
            allow="fullscreen; picture-in-picture"
            referrerPolicy="strict-origin-when-cross-origin"
            style={mediaCapStyle}
            className="aspect-video w-full rounded-[8px] border border-[var(--modal-border)] bg-black"
          />
          {safeSourceUrl && (
            <a href={safeSourceUrl} target="_blank" rel="noreferrer" className="reading-meta inline-flex">
              在 Reddit 打开原帖
            </a>
          )}
        </div>
      )}
      {hasPlayableVideo && !videoId && (
        <div
          data-testid="cluster-video-stage"
          className="flex w-full justify-center overflow-hidden rounded-[8px] border border-[var(--modal-border)] bg-black"
        >
          <video
            data-testid="cluster-media-video"
            controls
            playsInline
            preload="metadata"
            poster={safePosterUrl ? proxiedImageUrl(safePosterUrl) : undefined}
            style={{ maxHeight: '56vh' }}
            className="block h-auto w-auto max-w-full object-contain"
          >
            <source src={playableVideoUrl ?? undefined} />
          </video>
        </div>
      )}
      {hasExternalMedia && (
        <a data-testid="cluster-media-external" href={safeSourceUrl ?? undefined} target="_blank" rel="noreferrer" className="block">
          {safePosterUrl ? <img src={proxiedImageUrl(safePosterUrl)} alt="媒体海报" className="w-full rounded-[8px]" /> : '查看原文'}
        </a>
      )}
      {hasPrimaryMedia && urls.length > 0 && (
        <div className="flex items-center gap-1 sm:gap-2">
          {hasStripControls && (
            <button
              type="button"
              aria-label="上一张图片"
              disabled={stripIndex === 0}
              onClick={() => moveStripTo(stripIndex - 1)}
              className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full text-[var(--modal-text-muted)] transition-colors hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] disabled:cursor-default disabled:opacity-30"
            >
              <ChevronLeft className="h-5 w-5" />
            </button>
          )}
          <div
            ref={stripRef}
            data-testid="cluster-video-image-strip"
            aria-label="补充图片"
            onScroll={(event) => {
              const strip = event.currentTarget
              const children = Array.from(strip.children) as HTMLElement[]
              if (children.length === 0) return
              const origin = children[0].offsetLeft
              const closest = children.reduce((best, child, index) => (
                Math.abs(child.offsetLeft - origin - strip.scrollLeft)
                  < Math.abs(children[best].offsetLeft - origin - strip.scrollLeft)
                  ? index
                  : best
              ), 0)
              setStripIndex(closest)
            }}
            className="flex min-w-0 flex-1 snap-x snap-mandatory gap-2 overflow-x-auto overscroll-x-contain [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
          >
            {urls.map((url, index) => (
              <button
                key={url}
                type="button"
                data-testid="cluster-video-image"
                aria-label={`查看第 ${index + 1} 张图片`}
                onClick={() => onOpen(url, urls)}
                className="h-[76px] w-[112px] shrink-0 snap-start overflow-hidden rounded-[8px] border border-[var(--modal-border)] bg-[var(--modal-surface-muted)] p-0 text-left shadow-[0_1px_0_rgba(255,255,255,0.72)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] sm:h-24 sm:w-36"
              >
                <img
                  src={proxiedImageUrl(url)}
                  alt=""
                  referrerPolicy="no-referrer"
                  loading={index === 0 ? 'eager' : 'lazy'}
                  className="h-full w-full object-cover transition-opacity hover:opacity-95"
                  onError={() => onError(url)}
                />
              </button>
            ))}
          </div>
          {hasStripControls && (
            <button
              type="button"
              aria-label="下一张图片"
              disabled={stripIndex === urls.length - 1}
              onClick={() => moveStripTo(stripIndex + 1)}
              className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full text-[var(--modal-text-muted)] transition-colors hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] disabled:cursor-default disabled:opacity-30"
            >
              <ChevronRight className="h-5 w-5" />
            </button>
          )}
        </div>
      )}
      {!hasPrimaryMedia && urls.length > 0 && (
      <div
        data-testid="cluster-modal-media-grid"
        data-media-count={String(urls.length)}
        data-media-layout={urls.length === 1 ? 'single' : 'stacked'}
        onMouseEnter={() => setIsHovered(true)}
        onMouseLeave={() => setIsHovered(false)}
        onTouchStart={() => setUserPaused(true)}
        style={mediaCapStyle}
        className="relative w-full overflow-hidden rounded-[8px] border border-[var(--modal-border)] bg-[var(--modal-surface-muted)] shadow-[0_1px_0_rgba(255,255,255,0.72)]"
      >
        <button
          type="button"
          aria-label="放大查看事件图片"
          onClick={() => onOpen(activeUrl, urls)}
          className="group/media block h-full w-full p-0 text-left"
        >
          <img
            src={proxiedImageUrl(activeUrl)}
            alt=""
            referrerPolicy="no-referrer"
            loading="eager"
            className="h-full w-full object-cover transition-opacity group-hover/media:opacity-95"
            onError={() => onError(activeUrl)}
          />
        </button>
        {hasCarousel && (
          <div
            aria-label={`${urls.length} 张图片轮播`}
            className="absolute bottom-2.5 left-1/2 flex -translate-x-1/2 items-center gap-1.5 rounded-full bg-black/20 px-2 py-1 shadow-[0_1px_4px_rgba(0,0,0,0.22)] backdrop-blur-[2px]"
          >
            {urls.map((url, index) => (
              <button
                key={`${url}-${index}`}
                type="button"
                data-testid="cluster-media-carousel-dot"
                aria-label={`查看第 ${index + 1} 张图片`}
                aria-current={index === activeIndex ? 'true' : undefined}
                className={cn(
                  // 2.2: 视觉仍 10px 小圆点,用 ::before 把可点区域放大到 ~32×24px
                  // (不改 flex 布局),移动端更易点中。触屏点点会先触发容器 onTouchStart 暂停自动轮播。
                  'relative h-2.5 w-2.5 rounded-full bg-white p-0 transition-[opacity,transform] duration-150',
                  "before:absolute before:left-1/2 before:top-1/2 before:h-8 before:w-6 before:-translate-x-1/2 before:-translate-y-1/2 before:content-['']",
                  'hover:scale-125 hover:opacity-100 focus:outline-none',
                  index === activeIndex ? 'opacity-95' : 'opacity-45',
                )}
                onClick={(e) => {
                  e.stopPropagation()
                  setActiveIndex(index)
                }}
              />
            ))}
          </div>
        )}
      </div>
      )}
    </figure>
  )
}

function ModalSummary({
  summary,
  keyPoints,
}: {
  summary?: string | null
  keyPoints?: ReadingKeyPoint[] | null
}) {
  const hasSummary = !!summary
  const parts = parseClusterSummary(summary)
  const speedReview = parts.speedReview || (!parts.fullBreakdown ? summary || null : null)
  const sections = parseClusterBreakdownSections(parts.fullBreakdown)

  return (
    <ReadingSummary
      fallbackBreakdown={sections.length === 0 ? parts.fullBreakdown : null}
      keyPoints={hasSummary ? null : keyPoints}
      lead={speedReview}
      sections={sections}
      testIds={{
        container: 'cluster-summary-block',
        lead: 'cluster-speed-review',
        sections: 'cluster-full-breakdown',
        keyPoints: 'cluster-key-points',
        sectionNumber: 'cluster-breakdown-number',
        bulletDot: 'cluster-breakdown-bullet-dot',
      }}
    />
  )
}

function SourceCard({ source }: { source: ClusterSource }) {
  const originalUrl = safeExternalUrl(source.url)
  const href = originalUrl || buildInfoItemHref(source.item_id)
  const external = !!originalUrl
  const time = formatReadingDateTime(source.published_at)
  const platformLabel = eventPlatformName(source.platform)

  return (
    <a
      href={href}
      target={external ? '_blank' : undefined}
      rel={external ? 'noopener noreferrer' : undefined}
      data-testid="cluster-modal-source-row"
      data-link-kind={external ? 'original' : 'item-fallback'}
      className={cn(
        'group block rounded-[7px] border border-[var(--modal-border-soft)] bg-[var(--modal-surface-soft)] px-3 py-2.5 transition-colors',
        'hover:border-[var(--brand-border)] hover:bg-[var(--modal-hover-soft)]',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--modal-surface)]',
      )}
      aria-label={`${external ? '打开原始链接' : '定位信息弹窗'}: ${source.title}`}
      title={external ? '打开原始链接' : '定位信息弹窗'}
    >
      <div className="flex min-w-0 items-center gap-2.5">
        <span
          className={cn(
            'inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-[5px] text-[10px] font-bold leading-none',
            platformClass(source.platform),
          )}
          title={platformLabel}
          aria-hidden="true"
        >
          <PlatformBrandIcon platform={source.platform} className="h-3.5 w-3.5" />
        </span>
        <span
          data-testid="cluster-source-title"
          className="min-w-0 flex-1 truncate font-event-title text-[14px] font-semibold leading-[1.45] text-[var(--modal-text)]"
        >
          {source.title}
        </span>
        <span data-testid="cluster-source-platform" className="shrink-0 text-[12px] leading-none text-[var(--modal-text-faint)]">
          {platformLabel}
        </span>
        {source.author && (
          <>
            <span className="shrink-0 text-[var(--modal-text-subtle)]">·</span>
            <span className="max-w-[112px] shrink-0 truncate text-[12px] leading-none text-[var(--modal-text-faint)]">{source.author}</span>
          </>
        )}
        {time && (
          <>
            <span className="shrink-0 text-[var(--modal-text-subtle)]">·</span>
            <time
              dateTime={source.published_at || undefined}
              data-testid="cluster-source-time"
              className="shrink-0 font-mono text-[12px] leading-none text-[var(--modal-text-faint)] tabular-nums"
            >
              {time}
            </time>
          </>
        )}
        {source.authority_badge === 'official' && <OfficialBadge />}
      </div>
    </a>
  )
}

export function ClusterDetailPanel() {
  const modalState = useClusterDetailStore((s) => s.modalState)
  const modalClusterId = useClusterDetailStore((s) => s.modalClusterId)
  const cluster = useClusterDetailStore((s) => s.cluster)
  const sources = useClusterDetailStore((s) => s.sources)
  const closeModal = useClusterDetailStore((s) => s.closeModal)
  const openModal = useClusterDetailStore((s) => s.openModal)
  const toggleClusterStar = useClusterDetailStore((s) => s.toggleClusterStar)
  const submitClusterFeedback = useClusterDetailStore((s) => s.submitClusterFeedback)
  const [lightboxSrc, setLightboxSrc] = useState<string | null>(null)
  const [lightboxImages, setLightboxImages] = useState<string[]>([])
  const [failedMediaUrls, setFailedMediaUrls] = useState<string[]>([])
  const [starPending, setStarPending] = useState(false)
  const [feedbackPending, setFeedbackPending] = useState(false)
  const [isClosing, setIsClosing] = useState(false)

  // D8: 关闭走 180ms 出场动画再真正卸载,对齐信息弹窗 DetailPanel(此前事件弹窗直接消失)。
  // Esc / 遮罩 / X / 错误态关闭 都走这里;真正的 store closeModal 延后到动画结束。
  const handleClose = useCallback(() => {
    setIsClosing(true)
    window.setTimeout(() => {
      closeModal()
      setIsClosing(false)
    }, 180)
  }, [closeModal])

  const primaryVideo = useMemo(() => {
    if (!cluster) return null
    return [ ...(cluster.media ?? []), ...sources.flatMap((source) => source.media ?? []) ]
      .find((media) => media.type === 'video' || media.type === 'embed') ?? null
  }, [cluster, sources])
  const primaryVideoItemId = useMemo(
    () => twitterVideoItemId(primaryVideo, sources),
    [primaryVideo, sources],
  )
  const mediaUrls = useMemo(() => {
    if (!cluster) return []
    const videoPosters = collectVideoPosterUrls(cluster, sources)
    return collectMediaUrls(cluster, sources).filter(
      (url) => !videoPosters.has(url)
        && !(primaryVideo && isGeneratedVideoPoster(url))
        && !failedMediaUrls.includes(url),
    )
  }, [cluster, sources, primaryVideo, failedMediaUrls])

  useEffect(() => {
    setLightboxSrc(null)
    setLightboxImages([])
    setFailedMediaUrls([])
  }, [cluster?.id])

  useEffect(() => {
    if (modalState !== 'open') {
      setLightboxSrc(null)
      setLightboxImages([])
    } else {
      // 新弹窗打开时确保不残留上一次的关闭态(否则会带出场动画)
      setIsClosing(false)
    }
  }, [modalState])

  const handleJumpDetail = useCallback(() => {
    if (!cluster) return
    window.location.hash = `cluster=${cluster.id}`
  }, [cluster])

  const handleStar = useCallback(async () => {
    if (!cluster || starPending) return
    if (!requireAuth('收藏', { onLoginClick: handleClose })) return
    const wasStarred = !!cluster.viewer_status?.starred_at
    setStarPending(true)
    try {
      await toggleClusterStar(cluster.id)
      toast.success(wasStarred ? '已取消收藏' : '收藏成功')
    } catch {
      toast.error('操作失败')
    } finally {
      setStarPending(false)
    }
  }, [cluster, handleClose, starPending, toggleClusterStar])

  const handleShare = useCallback(async () => {
    if (!cluster) return
    try {
      await copyTextToClipboard(buildClusterShareText(cluster))
      toast.success('分享链接已复制')
    } catch {
      toast.error('分享失败')
    }
  }, [cluster])

  // v25.0 F-D(DESIGN.md 22.3): 质量反馈——同 kind 再点=撤销,失败回滚+toast,未登录走 requireAuth
  const handleFeedback = useCallback(async (kind: 'irrelevant' | 'low_quality') => {
    if (!cluster || feedbackPending) return
    if (!requireAuth('反馈', { onLoginClick: handleClose })) return
    const wasActive = cluster.viewer_status?.feedback_kind === kind
    setFeedbackPending(true)
    try {
      await submitClusterFeedback(cluster.id, kind)
      toast.success(wasActive ? '已撤销反馈' : '已收到反馈')
    } catch {
      toast.error('反馈失败，请重试')
    } finally {
      setFeedbackPending(false)
    }
  }, [cluster, feedbackPending, handleClose, submitClusterFeedback])

  if (modalState === 'closed') return null

  const markMediaFailed = (url: string) => {
    setFailedMediaUrls((prev) => prev.includes(url) ? prev : [...prev, url])
  }

  const openLightbox = (url: string, images: string[]) => {
    setLightboxImages(images)
    setLightboxSrc(url)
  }

  const closeLightbox = () => {
    setLightboxSrc(null)
  }

  const variant = modalVariantFor(mediaUrls.length)
  const platforms = cluster?.platforms?.length
    ? cluster.platforms
    : sources.map((source) => source.platform)
  // D1: 头部题注取主来源(平台徽标 + 来源名) + 事件时间 + 来源数
  const primarySource = sources.find((s) => s.is_primary_source) ?? sources[0]
  const metaPlatform = primarySource?.platform ?? platforms[0] ?? ''
  const metaSourceName = primarySource?.author?.trim() || (metaPlatform ? eventPlatformName(metaPlatform) : '')
  const metaTime = formatReadingDateTime(cluster?.first_doc_at)
  const metaSourceCount = cluster?.unique_source_count ?? sources.length
  const isStarred = !!cluster?.viewer_status?.starred_at
  const feedbackKind = cluster?.viewer_status?.feedback_kind ?? null
  const meta = (
    <>
      {metaSourceName && (
        <span className="flex min-w-0 items-center gap-1.5">
          {metaPlatform && (
            <span
              className={cn(
                'inline-flex h-[20px] w-[20px] shrink-0 items-center justify-center rounded-full border-2 border-[var(--modal-surface)] text-[10px] leading-none shadow-[0_1px_2px_rgba(26,25,23,0.16)]',
                platformClass(metaPlatform),
              )}
              title={eventPlatformName(metaPlatform)}
              aria-hidden="true"
            >
              <PlatformBrandIcon platform={metaPlatform} className="h-[66%] w-[66%]" />
            </span>
          )}
          <span className="min-w-0 truncate">{metaSourceName}</span>
        </span>
      )}
      {metaTime && (
        <>
          {metaSourceName && <span className="shrink-0 text-[var(--modal-text-subtle)]">·</span>}
          <time className="shrink-0 font-mono tabular-nums" dateTime={cluster?.first_doc_at}>
            {metaTime}
          </time>
        </>
      )}
      {metaSourceCount > 0 && (
        <>
          <span className="shrink-0 text-[var(--modal-text-subtle)]">·</span>
          <span className="shrink-0 tabular-nums">{metaSourceCount} 来源</span>
        </>
      )}
    </>
  )

  return (
    <>
      <ReadingModalShell
        ariaLabelledby="cluster-modal-title"
        dataAttributes={{ 'data-modal-variant': variant }}
        escapeDisabled={!!lightboxSrc}
        isClosing={isClosing}
        onClose={handleClose}
        panelTestId="cluster-detail-panel"
      >
        {modalState === 'loading' && (
          <ReadingModalLoading
            headerTestId="cluster-modal-header"
            onClose={handleClose}
            testId="cluster-modal-loading"
            title="精选详情"
            titleId="cluster-modal-title"
          />
        )}

        {modalState === 'error' && (
          <ReadingModalError
            headerTestId="cluster-modal-header"
            message="加载失败，请重试"
            onClose={handleClose}
            onRetry={() => { if (modalClusterId != null) void openModal(modalClusterId) }}
            testId="cluster-modal-error"
            title="精选详情"
            titleId="cluster-modal-title"
          />
        )}

        {modalState === 'open' && cluster && (
          <>
            <ReadingModalHeader
              headerTestId="cluster-modal-header"
              meta={meta}
              metaTestId="cluster-modal-meta"
              onClose={handleClose}
              title={cluster.ai_title}
              titleId="cluster-modal-title"
              titleTestId="cluster-modal-title"
            />

            <ReadingModalBody resetKey={cluster.id} testId="cluster-modal-scroll">
              <ClusterMediaBlock
                urls={mediaUrls}
                video={primaryVideo}
                videoItemId={primaryVideoItemId}
                onOpen={openLightbox}
                onError={markMediaFailed}
                isLightboxOpen={!!lightboxSrc}
              />

              <ModalSummary
                summary={cluster.ai_summary}
                keyPoints={cluster.ai_key_points}
              />

              {sources.length > 0 && (
                <section data-testid="cluster-modal-sources" className="pt-1">
                  <div className="space-y-2">
                    {sources.map((source) => (
                      <SourceCard key={source.item_id} source={source} />
                    ))}
                  </div>
                </section>
              )}

              {/* v21.0 action-revival: 事件弹窗内生成行动点,挂来源列表之后。 */}
              <section
                data-testid="cluster-modal-action-zone"
                className="mt-4 border-t border-[var(--modal-divider)] pt-4"
              >
                <ClusterActionZone clusterId={cluster.id} showExistingActions />
              </section>

              {/* v25.0 F-D → bf-0711 #1: 质量反馈从底部主操作栏下沉到正文底部轻量文字链接,
                  事件级语义不变(同 kind 再点=撤销)。不与 收藏/跳转/分享 主操作并列。 */}
              <div
                data-testid="cluster-feedback-inline"
                className="mt-5 flex flex-wrap items-center justify-center gap-x-2.5 gap-y-1 border-t border-[var(--modal-divider)] pt-4 text-[12px] text-[var(--modal-text-faint)]"
              >
                <span>这条对你没用？</span>
                <button
                  type="button"
                  onClick={() => handleFeedback('irrelevant')}
                  disabled={feedbackPending}
                  aria-pressed={feedbackKind === 'irrelevant'}
                  data-testid="cluster-feedback-irrelevant"
                  className={cn(
                    'inline-flex items-center gap-1 rounded-[4px] px-1.5 py-0.5 transition-colors hover:text-[var(--modal-text)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] disabled:cursor-not-allowed disabled:opacity-60',
                    feedbackKind === 'irrelevant' && 'text-[var(--brand)]',
                  )}
                >
                  <EyeOff className="h-3 w-3" />
                  {feedbackKind === 'irrelevant' ? '✓ 不感兴趣' : '不感兴趣'}
                </button>
                <span className="text-[var(--modal-text-subtle)]" aria-hidden="true">·</span>
                <button
                  type="button"
                  onClick={() => handleFeedback('low_quality')}
                  disabled={feedbackPending}
                  aria-pressed={feedbackKind === 'low_quality'}
                  data-testid="cluster-feedback-low-quality"
                  className={cn(
                    'inline-flex items-center gap-1 rounded-[4px] px-1.5 py-0.5 transition-colors hover:text-[var(--modal-text)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] disabled:cursor-not-allowed disabled:opacity-60',
                    feedbackKind === 'low_quality' && 'text-[var(--brand)]',
                  )}
                >
                  <ThumbsDown className="h-3 w-3" />
                  {feedbackKind === 'low_quality' ? '✓ 质量差' : '质量差'}
                </button>
              </div>
            </ReadingModalBody>

            <ReadingModalFooter className="grid-cols-3" testId="cluster-bottom-actions">
              <button
                type="button"
                onClick={handleStar}
                disabled={starPending}
                data-testid="cluster-footer-star-button"
                className={cn(readingModalFooterButtonClass, isStarred && 'text-[var(--brand)]')}
              >
                <Bookmark className={cn('h-3.5 w-3.5', isStarred && 'fill-current')} />
                {isStarred ? '已收藏' : '收藏'}
              </button>
              <button
                type="button"
                onClick={handleJumpDetail}
                data-testid="cluster-footer-detail-button"
                className={readingModalFooterButtonClass}
              >
                <ExternalLink className="h-3.5 w-3.5" />
                跳转详情
              </button>
              <button
                type="button"
                onClick={handleShare}
                data-testid="cluster-footer-share-button"
                className={readingModalFooterButtonClass}
              >
                <Share2 className="h-3.5 w-3.5" />
                分享
              </button>
            </ReadingModalFooter>
          </>
        )}
      </ReadingModalShell>

      {lightboxSrc && (
        <ReadingLightbox
          images={lightboxImages}
          onClose={closeLightbox}
          onNavigate={setLightboxSrc}
          src={lightboxSrc}
          testId="cluster-cover-lightbox"
        />
      )}
    </>
  )
}
