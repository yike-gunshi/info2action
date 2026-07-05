import { memo, useEffect, useRef, useState } from 'react'
import { Heart, MessageCircle, Eye, Share2, Play, Bookmark, Star, GitFork } from 'lucide-react'
import { useDetailStore } from '../../store/detailStore'
import { useFeedStore } from '../../store/feedStore'
import { cn, relativeTime, stripMd, formatNumber, platformName } from '../../lib/utils'
import { PlatformIcon } from '../shared/PlatformIcon'
import type { FeedItem } from '../../lib/types'

/** Extract the first usable image URL from a media_json entry (string or object). */
function extractMediaUrl(entry: string | { url?: string } | undefined): string | null {
  if (!entry) return null
  if (typeof entry === 'string') return entry
  if (typeof entry === 'object' && entry.url) return entry.url
  return null
}

/** v12.2: 如果 media_json 首个 item 是视频,返回 mp4 URL (用作 InfoCard 封面). */
function extractVideoMp4Url(media: FeedItem['media_json']): string | null {
  if (!media || media.length === 0) return null
  const first = media[0]
  if (typeof first === 'object' && first !== null) {
    const v = first as { type?: string; url?: string }
    if (v.type === 'video' && v.url) return v.url
  }
  return null
}

const METRIC_ICONS: [string, typeof Heart][] = [
  ['likes', Heart],
  ['comments', MessageCircle],
  ['views', Eye],
  ['shares', Share2],
  ['plays', Play],
  ['bookmarks', Bookmark],
  ['stars', Star],
  ['forks', GitFork],
]

function stripRepeatedPlatformPrefix(authorName: string | undefined, platform: string): string {
  const raw = authorName?.trim()
  if (!raw) return ''

  const primary = platformName(platform)
  const aliases = [primary, platform, platform === 'twitter' ? 'Twitter' : '', platform === 'x' ? 'Twitter' : '']
    .filter(Boolean)

  for (const alias of aliases) {
    if (raw === alias) return ''
    for (const divider of [' · ', '・', ' - ', ' ｜ ', ' | ', ' / ']) {
      const prefix = `${alias}${divider}`
      if (raw.startsWith(prefix)) {
        return raw.slice(prefix.length).trim()
      }
    }
  }

  return raw
}

/** Platform-specific interaction metrics — Lucide icons, max 3 */
function MetricsRow({ item }: { item: FeedItem }) {
  const m = item.metrics_json
  if (!m || Object.keys(m).length === 0) return null

  const entries: { Icon: typeof Heart; value: string }[] = []
  for (const [key, Icon] of METRIC_ICONS) {
    if (m[key] && entries.length < 2) {
      entries.push({ Icon, value: formatNumber(m[key]) })
    }
  }

  if (entries.length === 0) return null
  return (
    <span className="inline-flex items-center gap-2 text-[13px] font-mono text-muted-foreground">
      {entries.map(({ Icon, value }, i) => (
        <span key={i} className="inline-flex items-center gap-0.5">
          <Icon className="w-3 h-3" />
          {value}
        </span>
      ))}
    </span>
  )
}

interface InfoCardProps {
  item: FeedItem
  delay?: number
  showReadState?: boolean
}

/**
 * Information card with platform-specific glow on hover + score color encoding.
 */
// FE-1(B7): memo + 逐卡订阅已读态——点一张卡只重渲染那张卡,不再全页扩散
export const InfoCard = memo(function InfoCard({ item, delay = 0, showReadState = true }: InfoCardProps) {
  const openItem = useDetailStore((s) => s.openItem)
  const prefetchItem = useDetailStore((s) => s.prefetchItem)
  const localClickedAt = useFeedStore((s) => s.clickedAtById[item.id])
  const isRead = Boolean(item.clicked_at || localClickedAt)
  const [imgError, setImgError] = useState(false)
  const [posterError, setPosterError] = useState(false)
  const [posterLoaded, setPosterLoaded] = useState(false)
  const [allowPosterLoad, setAllowPosterLoad] = useState(false)

  // Robust summary fallback: ai_summary → description → content (truncated)
  const rawSummary = item.ai_summary || item.description || item.content || ''
  const summary = rawSummary ? stripMd(rawSummary) : ''
  const time = item.published_at || item.fetched_at || item.created_at
  // v12.2 Round 2: Twitter 视频帖封面走后端 ffmpeg 首帧缓存,避开 CDN Referer 校验
  const videoMp4Url = extractVideoMp4Url(item.media_json)
  const isTwitterVideo = !!videoMp4Url && item.platform === 'twitter'
  // Robust image fallback chain: cover_url → thumbnail → media_json[0]
  const rawImageUrl = item.cover_url
    || item.thumbnail
    || (!videoMp4Url ? extractMediaUrl(item.media_json?.[0]) : null)
    || null
  // BF-0515-twitter-image-proxy: pbs.twimg.com fails in many user environments
  // (local proxy / MITM TLS interception → ERR_CERT_COMMON_NAME_INVALID). Route
  // Twitter static photos through backend proxy.
  const imageUrl = (
    item.platform === 'twitter'
    && rawImageUrl
    && rawImageUrl.startsWith('https://pbs.twimg.com/')
  )
    ? `/api/media/twitter-photo/${item.id}/0.jpg`
    : rawImageUrl
  const sourceLabel = stripRepeatedPlatformPrefix(item.author_name, item.platform)
  const hasMedia = (isTwitterVideo && !posterError) || (imageUrl && !imgError)

  // B1(FE-15): 原实现固定延迟 4s 才加载海报(规避 ffmpeg 首帧生成期黑图),
  // 但海报已缓存时这 4s 纯属白等,且 mount ≠ 可见(列表深处的卡也在计时)。
  // 改为进入视口(提前 200px)即加载;首帧生成期由骨架 shimmer 覆盖。
  const mediaRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    setPosterLoaded(false)
    setPosterError(false)
    setAllowPosterLoad(false)
    if (!videoMp4Url || item.platform !== 'twitter') return
    const el = mediaRef.current
    if (!el || typeof IntersectionObserver === 'undefined') {
      setAllowPosterLoad(true)
      return
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setAllowPosterLoad(true)
          io.disconnect()
        }
      },
      { rootMargin: '200px' },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [item.id, item.platform, videoMp4Url])

  // Platform-specific glow CSS variable
  const glowVar = `var(--glow-${item.platform === 'xiaohongshu' ? 'xhs' : item.platform === 'bilibili' ? 'bili' : item.platform})`

  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      openItem(item.id)
    }
  }

  return (
    <div
      role="button"
      tabIndex={0}
      aria-label={item.title}
      className={cn(
        'cv-auto bg-card rounded-[4px] border border-border p-4',
        'cursor-pointer outline-none',
        'transition-colors duration-150 hover:border-[var(--brand-border)] hover:bg-white/90 dark:hover:bg-card/80',
        'focus-visible:border-[var(--brand-border)] focus-visible:bg-white/90 dark:focus-visible:bg-card/80',
        delay > 0 && 'animate-blur-fade',
        showReadState && isRead && 'opacity-40',
      )}
      style={{
        ...(delay > 0 ? { animationDelay: `${delay}ms` } : {}),
        '--hover-glow': glowVar,
      } as React.CSSProperties}
      data-testid="info-card"
      data-has-media={hasMedia ? 'true' : 'false'}
      onMouseEnter={() => prefetchItem(item.id)}
      onFocus={() => prefetchItem(item.id)}
      onClick={() => openItem(item.id)}
      onKeyDown={handleKey}
    >
      {/* v14.0 BF-0420-1: 放大按钮已移至 DetailPanel header;卡片列表区保持"点击=弹窗"单一交互 */}
      {/* Cover: Twitter 视频帖走后端 ffmpeg 首帧 API(避 CDN Referer 黑名单); 否则 img 回退链 */}
      {isTwitterVideo && !posterError ? (
        <div ref={mediaRef} className="relative mb-3 overflow-hidden rounded-[4px] bg-muted aspect-[16/9]" data-testid="info-card-media">
          {/* 骨架 shimmer — 首次 ffmpeg 抽帧 1-2s,期间不显示黑色(BF-0418-POSTER-BLACK) */}
          {!posterLoaded && (
            <div className="absolute inset-0 animate-pulse bg-gradient-to-r from-muted via-muted/60 to-muted" />
          )}
          {allowPosterLoad && (
            <img
              src={`/api/media/twitter-poster/${item.id}.jpg`}
              alt=""
              className={cn(
                'w-full h-full object-cover transition-opacity duration-200',
                posterLoaded ? 'opacity-100' : 'opacity-0',
              )}
              loading="lazy"
              decoding="async"
              onLoad={() => setPosterLoaded(true)}
              onError={() => setPosterError(true)}
            />
          )}
          {/* 左下角小播放按钮图标标记这是视频 */}
          <div className="absolute left-2 bottom-2 inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-black/60 text-white text-[11px] backdrop-blur-sm">
            <Play className="w-3 h-3 fill-current" />
            视频
          </div>
        </div>
      ) : imageUrl && !imgError ? (
        <div className="relative mb-3 overflow-hidden rounded-[4px] aspect-[16/9]" data-testid="info-card-media">
          <img
            src={imageUrl}
            alt=""
            className="w-full h-full object-cover"
            loading="lazy"
            referrerPolicy="no-referrer"
            onError={() => setImgError(true)}
          />
        </div>
      ) : null}

      <div className="mb-2 flex min-w-0 items-center gap-2 text-[14px] font-medium text-muted-foreground" data-testid="info-card-source">
        <PlatformIcon platform={item.platform} size="sm" />
        {sourceLabel && <span className="truncate">{sourceLabel}</span>}
      </div>

      {/* v19 2a: magazine-style title hierarchy, separated from source metadata. */}
      <h3 className="mb-2 font-event-title text-[20px] font-semibold leading-[1.36] text-foreground line-clamp-3" data-testid="info-card-title">
        {item.title}
      </h3>

      {/* Summary — v19: no colored block or left border; only the ✦ mark carries brand color. */}
      {summary && (
        <p className="mt-2 font-event-title text-[16px] font-medium leading-[1.58] text-muted-foreground line-clamp-5" data-testid="info-card-summary">
          {item.ai_summary && <span className="mr-0.5 text-[var(--brand)]">✦</span>}
          {summary}
        </p>
      )}

      {/* Bottom row: quiet meta with a hairline separator. */}
      <div className="mt-3 flex items-center border-t border-border pt-2.5 text-[13px] font-mono text-muted-foreground" data-testid="info-card-meta">
        <MetricsRow item={item} />
        <span className="ml-auto shrink-0">
          {time ? relativeTime(time) : null}
        </span>
      </div>
    </div>
  )
})
