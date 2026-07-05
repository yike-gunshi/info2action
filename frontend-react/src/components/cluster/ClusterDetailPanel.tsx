import { useCallback, useEffect, useMemo, useState } from 'react'
import type { CSSProperties, MouseEvent } from 'react'
import { Bookmark, ChevronLeft, ChevronRight, ExternalLink, Share2, X } from 'lucide-react'
import { toast } from 'sonner'
import { useClusterDetailStore } from '../../store/clusterDetailStore'
import type { ClusterDetail, ClusterSource } from '../../lib/types'
import { cn, eventPlatformName, platformClass, stripMd } from '../../lib/utils'
import { parseClusterBreakdownSections, parseClusterSummary } from '../../lib/cluster-summary-parser'
import { renderMarkdownInline, renderMarkdownLite } from '../../lib/markdown-lite'
import { proxiedImageUrl } from '../../lib/media'
import { PlatformBrandIcon } from '../shared/PlatformIcon'
import { requireAuth } from '../shared/AuthGate'
import { buildInfoItemHref } from '../../lib/itemDeepLink'

type KeyPointItem = string | { title: string; points: string[] }
type ModalVariant = 'no-media' | 'single-media' | 'multi-media'

const paperSurfaceStyle: CSSProperties = {
  backgroundImage: 'var(--modal-paper-texture)',
  backgroundSize: 'auto, 6px 6px',
}

const mediaCapStyle: CSSProperties = {
  aspectRatio: '16 / 9',
  maxHeight: 'min(180px, calc((var(--app-visual-height) - 32px - var(--modal-bottom-clearance)) * 0.25))',
}

const INFO2ACT_SHARE_BASE_URL = 'https://www.info2act.com'

async function copyTextToClipboard(text: string): Promise<void> {
  if (copyTextWithTextarea(text)) return

  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }

  throw new Error('copy failed')
}

function copyTextWithTextarea(text: string): boolean {
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.left = '0'
  textarea.style.top = '0'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.focus()
  textarea.select()
  textarea.setSelectionRange(0, text.length)
  try {
    return Boolean(document.execCommand?.('copy'))
  } finally {
    document.body.removeChild(textarea)
  }
}

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
  const url = value?.trim()
  if (url && !urls.includes(url)) urls.push(url)
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

function modalVariantFor(mediaCount: number): ModalVariant {
  if (mediaCount <= 0) return 'no-media'
  if (mediaCount === 1) return 'single-media'
  return 'multi-media'
}

function sourceClockTime(value?: string | null): string | null {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date)
}

// D1: 头部题注日期时间,格式 MM-DD HH:mm(如 07-04 04:30)。
function eventMetaDateTime(value?: string | null): string | null {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date).replace(/\//g, '-')
}

function safeExternalUrl(value?: string | null): string | null {
  if (!value) return null
  try {
    const url = new URL(value)
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null
  } catch {
    return null
  }
}

function paragraphLines(text: string): string[] {
  return text
    .split(/\n{2,}/)
    .map((line) => line.trim())
    .filter(Boolean)
}

function ClusterMediaBlock({
  urls,
  onOpen,
  onError,
  isLightboxOpen,
}: {
  urls: string[]
  onOpen: (url: string, images: string[]) => void
  onError: (url: string) => void
  isLightboxOpen: boolean
}) {
  const [activeIndex, setActiveIndex] = useState(0)
  const [isHovered, setIsHovered] = useState(false)
  // D5: 触屏无 hover,自动轮播每 4s 跳图无法停。用户首次交互(触摸/点点)后
  // 永久暂停自动轮播,改为纯手动;换事件(carouselKey 变)时重置。
  const [userPaused, setUserPaused] = useState(false)
  const hasCarousel = urls.length > 1
  const carouselKey = urls.join('\u0001')
  const activeUrl = urls[activeIndex] ?? urls[0] ?? ''

  useEffect(() => {
    setActiveIndex(0)
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

  if (urls.length === 0) return null

  return (
    <figure className="mb-6">
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
    </figure>
  )
}

function SummaryLead({ text }: { text: string | null }) {
  if (!text) return null
  const lines = paragraphLines(text)
  if (lines.length === 0) return null

  return (
    <div
      data-testid="cluster-speed-review"
      className="reading-body border-b border-[var(--modal-divider)] pb-5"
    >
      <p>
        <strong className="mr-1.5 !text-[var(--brand)]">精华速览：</strong>
        {renderMarkdownInline(lines[0])}
      </p>
      {lines.slice(1).map((line, index) => (
        <p key={index} className="mt-2.5">
          {renderMarkdownInline(line)}
        </p>
      ))}
    </div>
  )
}

function BreakdownSections({ fullBreakdown }: { fullBreakdown: string | null }) {
  if (!fullBreakdown) return null

  const sections = parseClusterBreakdownSections(fullBreakdown)
  if (sections.length === 0) {
    return (
      <div
        data-testid="cluster-full-breakdown"
        className="reading-bullet py-4 [&_ul]:my-2.5 [&_ul]:space-y-1"
      >
        {renderMarkdownLite(fullBreakdown)}
      </div>
    )
  }

  return (
    <section data-testid="cluster-full-breakdown" className="py-4">
      <div className="space-y-0">
        {sections.map((section, index) => (
          <article
            key={`${section.title}-${index}`}
            className={cn(
              index > 0 && 'border-t border-[var(--modal-divider)] pt-3.5',
              index < sections.length - 1 && 'pb-4',
            )}
          >
            <div className="mb-2 flex items-baseline gap-2.5">
              <span
                data-testid="cluster-breakdown-number"
                className="reading-section leading-none text-[var(--brand)]"
              >
                {String(index + 1).padStart(2, '0')}
              </span>
              <h3 className="reading-section min-w-0">
                {renderMarkdownInline(section.title)}
              </h3>
            </div>
            {section.points.length > 0 && (
              <ul className="reading-bullet space-y-1 pl-6 sm:pl-[38px]">
                {section.points.map((point, pointIndex) => (
                  <li key={`${point}-${pointIndex}`} className="relative">
                    <span
                      data-testid="cluster-breakdown-bullet-dot"
                      className="absolute -left-3.5 top-[0.8em] h-1 w-1 rounded-full bg-[var(--modal-text)] sm:-left-4"
                      aria-hidden="true"
                    />
                    {renderMarkdownInline(point)}
                  </li>
                ))}
              </ul>
            )}
          </article>
        ))}
      </div>
    </section>
  )
}

function KeyPointsFallback({ keyPoints }: { keyPoints?: KeyPointItem[] | null }) {
  if (!keyPoints?.length) return null

  return (
    <ul data-testid="cluster-key-points" className="reading-bullet border-b border-[var(--modal-divider)] py-5">
      {keyPoints.map((point, index) => {
        if (typeof point === 'string') {
          return (
            <li key={index} className="relative mb-1.5 pl-4 last:mb-0">
              <span className="absolute left-0 top-[0.8em] h-1 w-1 rounded-full bg-[var(--modal-text)]" aria-hidden="true" />
              {renderMarkdownInline(point)}
            </li>
          )
        }
        return (
          <li key={index} className="mb-3 last:mb-0">
            <div className="reading-section">{renderMarkdownInline(point.title)}</div>
            <ul className="mt-1.5 space-y-1.5">
              {point.points.map((subPoint, subIndex) => (
                <li key={subIndex} className="relative pl-4">
                  <span className="absolute left-0 top-[0.8em] h-1 w-1 rounded-full bg-[var(--modal-text)]" aria-hidden="true" />
                  {renderMarkdownInline(subPoint)}
                </li>
              ))}
            </ul>
          </li>
        )
      })}
    </ul>
  )
}

function ModalSummary({
  summary,
  keyPoints,
}: {
  summary?: string | null
  keyPoints?: KeyPointItem[] | null
}) {
  const hasSummary = !!summary
  const hasPoints = !!keyPoints?.length
  if (!hasSummary && !hasPoints) return null

  const parts = parseClusterSummary(summary)
  const speedReview = parts.speedReview || (!parts.fullBreakdown ? summary || null : null)

  return (
    <section
      data-testid="cluster-summary-block"
      className="mb-6 text-[var(--modal-text-soft)]"
    >
      <SummaryLead text={speedReview} />
      <BreakdownSections fullBreakdown={parts.fullBreakdown} />
      {!hasSummary && hasPoints && <KeyPointsFallback keyPoints={keyPoints} />}
    </section>
  )
}

function SourceCard({ source }: { source: ClusterSource }) {
  const originalUrl = safeExternalUrl(source.url)
  const href = originalUrl || buildInfoItemHref(source.item_id)
  const external = !!originalUrl
  const time = sourceClockTime(source.published_at)
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
  const cluster = useClusterDetailStore((s) => s.cluster)
  const sources = useClusterDetailStore((s) => s.sources)
  const error = useClusterDetailStore((s) => s.error)
  const closeModal = useClusterDetailStore((s) => s.closeModal)
  const toggleClusterStar = useClusterDetailStore((s) => s.toggleClusterStar)
  const [lightboxSrc, setLightboxSrc] = useState<string | null>(null)
  const [lightboxImages, setLightboxImages] = useState<string[]>([])
  const [failedMediaUrls, setFailedMediaUrls] = useState<string[]>([])
  const [starPending, setStarPending] = useState(false)
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

  const mediaUrls = useMemo(() => {
    if (!cluster) return []
    return collectMediaUrls(cluster, sources).filter((url) => !failedMediaUrls.includes(url))
  }, [cluster, sources, failedMediaUrls])

  useEffect(() => {
    setLightboxSrc(null)
    setLightboxImages([])
    setFailedMediaUrls([])
  }, [cluster?.id])

  useEffect(() => {
    if (modalState === 'closed') return
    const html = document.documentElement
    const prevOverflow = html.style.overflow
    html.style.overflow = 'hidden'
    return () => {
      html.style.overflow = prevOverflow
    }
  }, [modalState])

  useEffect(() => {
    if (modalState === 'closed') return
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        if (lightboxSrc) setLightboxSrc(null)
        else handleClose()
      }
      if (lightboxSrc && lightboxImages.length > 1) {
        const idx = lightboxImages.indexOf(lightboxSrc)
        if (idx < 0) return
        if (e.key === 'ArrowLeft' && idx > 0) {
          e.preventDefault()
          setLightboxSrc(lightboxImages[idx - 1])
        }
        if (e.key === 'ArrowRight' && idx < lightboxImages.length - 1) {
          e.preventDefault()
          setLightboxSrc(lightboxImages[idx + 1])
        }
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [modalState, handleClose, lightboxSrc, lightboxImages])

  useEffect(() => {
    if (modalState !== 'open') {
      setLightboxSrc(null)
      setLightboxImages([])
    } else {
      // 新弹窗打开时确保不残留上一次的关闭态(否则会带出场动画)
      setIsClosing(false)
    }
  }, [modalState])

  const handleBackdropClick = (e: MouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget) handleClose()
  }

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
  const metaTime = eventMetaDateTime(cluster?.first_doc_at)
  const metaSourceCount = cluster?.unique_source_count ?? sources.length
  const isStarred = !!cluster?.viewer_status?.starred_at
  const bottomActionClass = 'flex h-12 w-full items-center justify-center gap-1.5 text-[13px] font-medium text-[var(--modal-text-muted)] transition-colors hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-inset disabled:cursor-not-allowed disabled:opacity-60'
  const lightboxIndex = lightboxSrc ? lightboxImages.indexOf(lightboxSrc) : -1
  const hasLightboxNavigation = lightboxImages.length > 1 && lightboxIndex >= 0

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="cluster-modal-title"
      onClick={handleBackdropClick}
      className={cn(
        'modal-viewport-shell fixed inset-0 z-[900] flex bg-black/60',
        isClosing ? 'animate-backdrop-out' : 'animate-backdrop-in',
      )}
    >
      <div
        data-testid="cluster-detail-panel"
        data-modal-theme="editorial"
        data-modal-variant={variant}
        className={cn(
          'modal-viewport-panel flex w-[calc(100vw-24px)] max-w-[720px] flex-col overflow-hidden rounded-[10px] border border-[var(--modal-border)] bg-[var(--modal-surface)] text-[var(--modal-text)] shadow-[var(--modal-shadow)]',
          isClosing ? 'animate-modal-out' : 'animate-modal-in',
        )}
        style={paperSurfaceStyle}
        onClick={(e) => e.stopPropagation()}
      >
        {modalState === 'loading' && (
          <div className="flex min-h-[220px] items-center justify-center p-6">
            <div className="text-sm text-muted-foreground">加载中…</div>
          </div>
        )}

        {modalState === 'error' && (
          <div className="flex min-h-[220px] flex-col items-center justify-center gap-2 p-6">
            <div className="text-sm text-foreground">加载失败</div>
            <div className="text-xs text-muted-foreground">{error}</div>
            <button
              type="button"
              onClick={handleClose}
              className="mt-2 rounded-[6px] bg-[var(--brand)] px-4 py-2 text-sm font-medium text-[var(--brand-foreground)]"
            >
              关闭
            </button>
          </div>
        )}

        {modalState === 'open' && cluster && (
          <>
            <header
              data-testid="cluster-modal-header"
              className="shrink-0 bg-[var(--modal-surface)] px-4 py-5 sm:px-10"
              style={paperSurfaceStyle}
            >
              <div className="flex items-start gap-2">
                <div className="min-w-0 flex-1">
                  <h2
                    id="cluster-modal-title"
                    data-testid="cluster-modal-title"
                    className="reading-title min-w-0"
                  >
                    <span className="line-clamp-2">{cluster.ai_title}</span>
                  </h2>
                  {/* D1: 标题下题注行 —— 主来源(平台徽标+来源名) · 事件时间 · 来源数。
                      平台图标从标题行挪到这里,标题得以安心两行不被挤。 */}
                  <div
                    data-testid="cluster-modal-meta"
                    className="reading-meta mt-2 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1"
                  >
                    {metaSourceName && (
                      <span className="flex min-w-0 items-center gap-1.5">
                        {metaPlatform && (
                          <span
                            className={cn(
                              'inline-flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-full border-2 border-[var(--modal-surface)] text-[10px] leading-none shadow-[0_1px_2px_rgba(26,25,23,0.16)]',
                              platformClass(metaPlatform),
                            )}
                            title={eventPlatformName(metaPlatform)}
                            aria-hidden="true"
                          >
                            <PlatformBrandIcon platform={metaPlatform} className="h-[62%] w-[62%]" />
                          </span>
                        )}
                        <span className="min-w-0 truncate">{metaSourceName}</span>
                      </span>
                    )}
                    {metaTime && (
                      <>
                        {metaSourceName && <span className="shrink-0 text-[var(--modal-text-subtle)]">·</span>}
                        <time className="shrink-0 font-mono tabular-nums" dateTime={cluster.first_doc_at}>
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
                  </div>
                </div>

                <div className="flex w-8 shrink-0 items-center justify-end gap-0.5">
                  <button
                    type="button"
                    onClick={handleClose}
                    aria-label="关闭"
                    title="关闭"
                    className="relative flex h-7 w-7 items-center justify-center rounded-[5px] text-[var(--modal-text-faint)] transition-colors before:absolute before:-inset-2 before:content-[''] hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)]"
                  >
                    <X className="h-5 w-5" />
                  </button>
                </div>
              </div>
            </header>

            <div className="flex-1 overflow-y-auto px-4 pb-7 pt-0 sm:px-10 sm:pb-8">
              <ClusterMediaBlock
                urls={mediaUrls}
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
            </div>

            <div
              className="modal-safe-footer grid flex-shrink-0 grid-cols-3 overflow-hidden border-t border-[var(--modal-border-soft)] bg-[var(--modal-surface)]"
              data-testid="cluster-bottom-actions"
              style={paperSurfaceStyle}
            >
              <button
                type="button"
                onClick={handleStar}
                disabled={starPending}
                data-testid="cluster-footer-star-button"
                className={cn(bottomActionClass, isStarred && 'text-[var(--brand)]')}
              >
                <Bookmark className={cn('h-3.5 w-3.5', isStarred && 'fill-current')} />
                {isStarred ? '已收藏' : '收藏'}
              </button>
              <button
                type="button"
                onClick={handleJumpDetail}
                data-testid="cluster-footer-detail-button"
                className={bottomActionClass}
              >
                <ExternalLink className="h-3.5 w-3.5" />
                跳转详情
              </button>
              <button
                type="button"
                onClick={handleShare}
                data-testid="cluster-footer-share-button"
                className={bottomActionClass}
              >
                <Share2 className="h-3.5 w-3.5" />
                分享
              </button>
            </div>
          </>
        )}
      </div>

      {lightboxSrc && (
        <div
          data-testid="cluster-cover-lightbox"
          className="fixed inset-0 z-[920] flex items-center justify-center bg-black/85 p-4"
          onClick={(e) => {
            e.stopPropagation()
            closeLightbox()
          }}
        >
          <button
            type="button"
            aria-label="关闭图片预览"
            className="absolute right-5 top-5 z-10 flex h-9 w-9 items-center justify-center rounded-full bg-black/50 text-white shadow-lg backdrop-blur-sm transition-colors hover:bg-black/65"
            onClick={(e) => {
              e.stopPropagation()
              closeLightbox()
            }}
          >
            <X className="h-[18px] w-[18px]" />
          </button>
          {hasLightboxNavigation && lightboxIndex > 0 && (
            <button
              type="button"
              aria-label="上一张图片"
              title="上一张图片"
              className="group absolute left-4 top-1/2 z-10 flex h-14 w-14 -translate-y-1/2 items-center justify-center rounded-full text-white outline-none transition-colors focus-visible:ring-2 focus-visible:ring-white/70"
              onClick={(e) => {
                e.stopPropagation()
                setLightboxSrc(lightboxImages[lightboxIndex - 1])
              }}
            >
              <span className="flex h-11 w-11 items-center justify-center rounded-full bg-black/50 shadow-lg backdrop-blur-sm transition-colors group-hover:bg-black/65 group-focus-visible:bg-black/65">
                <ChevronLeft className="h-6 w-6" />
              </span>
            </button>
          )}
          {hasLightboxNavigation && lightboxIndex < lightboxImages.length - 1 && (
            <button
              type="button"
              aria-label="下一张图片"
              title="下一张图片"
              className="group absolute right-4 top-1/2 z-10 flex h-14 w-14 -translate-y-1/2 items-center justify-center rounded-full text-white outline-none transition-colors focus-visible:ring-2 focus-visible:ring-white/70"
              onClick={(e) => {
                e.stopPropagation()
                setLightboxSrc(lightboxImages[lightboxIndex + 1])
              }}
            >
              <span className="flex h-11 w-11 items-center justify-center rounded-full bg-black/50 shadow-lg backdrop-blur-sm transition-colors group-hover:bg-black/65 group-focus-visible:bg-black/65">
                <ChevronRight className="h-6 w-6" />
              </span>
            </button>
          )}
          {hasLightboxNavigation && (
            <span className="absolute bottom-4 left-1/2 z-10 -translate-x-1/2 rounded-full bg-black/40 px-3 py-1 font-mono text-[12px] text-white/80">
              {lightboxIndex + 1} / {lightboxImages.length}
            </span>
          )}
          <img
            src={proxiedImageUrl(lightboxSrc)}
            alt=""
            referrerPolicy="no-referrer"
            className="max-h-[90vh] max-w-[92vw] rounded-[6px] object-contain"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </div>
  )
}
