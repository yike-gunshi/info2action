/**
 * v24.1: InfoCard 回滚瀑布流白卡（用户实物验收定案——杂志式条目流阅读体验不佳）。
 *
 * 卡片骨架 = v19 2a 白卡规格：`bg-card` 1px 边框 4px 圆角 p-4、16:9 封面、
 * 来源行（PlatformIcon + 作者）、20px 衬线标题 clamp-3、16px/500 衬线摘要 clamp-5
 * 带 ✦、hairline mono meta 底栏（互动指标 + 相对时间）。
 *
 * 保留 v24 新件（用户点名，不随卡片回滚）：
 * - A7 图片三档（嫁接进 16:9 封面槽）：横图 cover / 近方 contain + muted 底纹 +
 *   1px 内 hairline（禁 blur）/ 长图顶裁 + 底部渐隐 + 「长图」角标 / 头像误判
 *   （r≈1 且短边<200px）降级为来源行 16px 头像；暗色全图 brightness(.92)。
 * - 已读 = 墨水降档（§21.1-7）：标题降 muted-foreground、摘要 /70、
 *   图 saturate(.6)+opacity(.85)；旧整卡 opacity-40 不恢复（AA 对比度）。
 *
 * lead/secondary/micro/repo/brief 行分型随 v24.1 退役——所有条目回到统一卡片。
 */
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

/** v12.2: 如果 media_json 首个 item 是视频,返回 mp4 URL (用作 InfoCard 封面来源判定). */
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

/** Platform-specific interaction metrics — Lucide icons, max 2 */
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

/* ---------------------------------------------------------------- A7 三档 */

type MediaTier = 'unknown' | 'wide' | 'square' | 'tall' | 'avatar'

/** 模块级 naturalWidth/Height 缓存 — 同一 URL 跨卡片/跨挂载只测一次。 */
const mediaDimCache = new Map<string, { w: number; h: number }>()

function computeTier(dim: { w: number; h: number } | undefined): MediaTier {
  if (!dim || !dim.w || !dim.h) return 'unknown'
  const r = dim.w / dim.h
  // 头像误判：r≈1（0.9-1.1）且短边 <200px → 不当封面
  if (r >= 0.9 && r <= 1.1 && Math.min(dim.w, dim.h) < 200) return 'avatar'
  if (r >= 1.4) return 'wide'
  if (r >= 0.75) return 'square'
  return 'tall'
}

interface InfoCardProps {
  item: FeedItem
  delay?: number
  showReadState?: boolean
  className?: string
}

/**
 * Information card — 白卡瀑布流单元。
 */
// FE-1(B7): memo + 逐卡订阅已读态——点一张卡只重渲染那张卡,不再全页扩散
export const InfoCard = memo(function InfoCard({
  item,
  delay = 0,
  showReadState = true,
  className,
}: InfoCardProps) {
  const openItem = useDetailStore((s) => s.openItem)
  const prefetchItem = useDetailStore((s) => s.prefetchItem)
  const localClickedAt = useFeedStore((s) => s.clickedAtById[item.id])
  const isRead = showReadState && Boolean(item.clicked_at || localClickedAt)
  const [imgError, setImgError] = useState(false)
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
  // BF-0515-twitter-image-proxy: pbs.twimg.com 在本地代理/MITM 环境证书报错,走后端代理
  const staticImageUrl = (
    item.platform === 'twitter'
    && rawImageUrl
    && rawImageUrl.startsWith('https://pbs.twimg.com/')
  )
    ? `/api/media/twitter-photo/${item.id}/0.jpg`
    : rawImageUrl
  const mediaSrc = isTwitterVideo ? `/api/media/twitter-poster/${item.id}.jpg` : staticImageUrl
  const sourceLabel = stripRepeatedPlatformPrefix(item.author_name, item.platform)

  // A7: naturalWidth/Height → 三档（模块级缓存,onLoad 只测一次）
  const [mediaDim, setMediaDim] = useState<{ w: number; h: number } | undefined>(
    () => (mediaSrc ? mediaDimCache.get(mediaSrc) : undefined),
  )
  useEffect(() => {
    setMediaDim(mediaSrc ? mediaDimCache.get(mediaSrc) : undefined)
    setImgError(false)
  }, [mediaSrc])
  const tier = computeTier(mediaDim)
  const avatarDemoted = tier === 'avatar'

  const handleImgLoad = (e: React.SyntheticEvent<HTMLImageElement>) => {
    const img = e.currentTarget
    if (isTwitterVideo) setPosterLoaded(true)
    if (!img.naturalWidth || !img.naturalHeight) return
    const dim = { w: img.naturalWidth, h: img.naturalHeight }
    if (mediaSrc) mediaDimCache.set(mediaSrc, dim)
    setMediaDim((current) => (current && current.w === dim.w && current.h === dim.h ? current : dim))
  }

  const hasMedia = !avatarDemoted && !imgError && Boolean(mediaSrc)

  // B1(FE-15): Twitter 视频海报进入视口(提前 200px)才加载;首帧生成期由骨架 shimmer 覆盖。
  const rootRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    setPosterLoaded(false)
    setAllowPosterLoad(false)
    if (!isTwitterVideo || !hasMedia) return
    const el = rootRef.current
    if (!el || typeof IntersectionObserver === 'undefined') {
      setAllowPosterLoad(true)
      return
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setAllowPosterLoad(true)
          io.disconnect()
        }
      },
      { rootMargin: '200px' },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [item.id, isTwitterVideo, hasMedia])

  // §21.3 移动端：预取从 hover 改 IO 进视口（触屏无 hover;prefetchItem 自带去重）
  useEffect(() => {
    if (typeof window === 'undefined' || typeof IntersectionObserver === 'undefined') return
    if (!window.matchMedia?.('(hover: none)').matches) return
    const el = rootRef.current
    if (!el) return
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          prefetchItem(item.id)
          io.disconnect()
        }
      },
      { rootMargin: '80px' },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [item.id, prefetchItem])

  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      openItem(item.id)
    }
  }

  /* ---------------------------------------------------------- A7 封面槽 */

  // 近方图（square）：16:9 槽 contain + muted 底纹 + 1px 内 hairline（禁 blur）
  const containTreatment = tier === 'square'
  // 长图（tall）：顶裁 + 底部渐隐 + 「长图」角标
  const tallCrop = tier === 'tall'
  const fitClass = containTreatment ? 'object-contain' : tallCrop ? 'object-cover object-top' : 'object-cover'
  const imgInkClass = cn('dark:brightness-[.92]', isRead && 'saturate-[.6] opacity-[.85]')

  // A7 头像降级：撤下封面槽,降级为来源行 16px 头像
  const demotedAvatar = avatarDemoted && mediaSrc ? (
    <img
      src={mediaSrc}
      alt=""
      className="h-4 w-4 shrink-0 rounded-full"
      loading="lazy"
      referrerPolicy="no-referrer"
      data-testid="info-card-demoted-avatar"
    />
  ) : null

  const titleInk = isRead ? 'text-muted-foreground' : 'text-foreground'

  return (
    <div
      ref={rootRef}
      role="button"
      tabIndex={0}
      aria-label={item.title}
      className={cn(
        'cv-auto bg-card rounded-[4px] border border-border p-4',
        'cursor-pointer outline-none',
        'transition-colors duration-150 hover:border-[var(--brand-border)] hover:bg-white/90 dark:hover:bg-card/80',
        'focus-visible:border-[var(--brand-border)] focus-visible:bg-white/90 dark:focus-visible:bg-card/80',
        delay > 0 && 'animate-blur-fade',
        className,
      )}
      style={delay > 0 ? ({ animationDelay: `${delay}ms` } as React.CSSProperties) : undefined}
      data-testid="info-card"
      data-has-media={hasMedia ? 'true' : 'false'}
      data-read={isRead ? 'true' : 'false'}
      onMouseEnter={() => prefetchItem(item.id)}
      onFocus={() => prefetchItem(item.id)}
      onClick={() => openItem(item.id)}
      onKeyDown={handleKey}
    >
      {/* v14.0 BF-0420-1: 放大按钮已移至 DetailPanel header;卡片列表区保持"点击=弹窗"单一交互 */}
      {/* Cover: 16:9 封面槽 + A7 三档;Twitter 视频帖走后端 ffmpeg 首帧 API(避 CDN Referer 黑名单) */}
      {hasMedia && (
        <div
          className={cn(
            'relative mb-3 overflow-hidden rounded-[4px] aspect-[16/9]',
            (containTreatment || (isTwitterVideo && !posterLoaded)) && 'bg-muted',
          )}
          data-testid="info-card-media"
          data-media-tier={tier}
        >
          {/* 骨架 shimmer — 首次 ffmpeg 抽帧 1-2s,期间不显示黑色(BF-0418-POSTER-BLACK) */}
          {isTwitterVideo && !posterLoaded && (
            <div className="absolute inset-0 animate-pulse bg-gradient-to-r from-muted via-muted/60 to-muted" />
          )}
          {(!isTwitterVideo || allowPosterLoad) && mediaSrc && (
            <img
              src={mediaSrc}
              alt=""
              className={cn(
                'h-full w-full',
                fitClass,
                imgInkClass,
                isTwitterVideo && 'transition-opacity duration-200',
                isTwitterVideo && !posterLoaded && 'opacity-0',
              )}
              loading="lazy"
              decoding="async"
              referrerPolicy={isTwitterVideo ? undefined : 'no-referrer'}
              onLoad={handleImgLoad}
              onError={() => setImgError(true)}
            />
          )}
          {containTreatment && (
            <span className="pointer-events-none absolute inset-0 rounded-[4px] ring-1 ring-inset ring-border" aria-hidden="true" />
          )}
          {tallCrop && (
            <>
              <span className="pointer-events-none absolute inset-x-0 bottom-0 h-2 bg-gradient-to-t from-muted to-transparent" aria-hidden="true" />
              <span className="absolute bottom-1 right-1 rounded-[3px] bg-foreground/70 px-1 font-mono text-[12px] leading-4 text-background">
                长图
              </span>
            </>
          )}
          {/* 左下角小播放按钮图标标记这是视频 */}
          {isTwitterVideo && (
            <div className="absolute left-2 bottom-2 inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-black/60 text-white text-[11px] backdrop-blur-sm">
              <Play className="w-3 h-3 fill-current" />
              视频
            </div>
          )}
        </div>
      )}

      <div className="mb-2 flex min-w-0 items-center gap-2 text-[14px] font-medium text-muted-foreground" data-testid="info-card-source">
        <PlatformIcon platform={item.platform} size="sm" />
        {demotedAvatar}
        {sourceLabel && <span className="truncate">{sourceLabel}</span>}
      </div>

      {/* v19 2a: magazine-style title hierarchy, separated from source metadata. */}
      <h3
        className={cn('mb-2 font-event-title text-[20px] font-semibold leading-[1.36] line-clamp-3', titleInk)}
        data-testid="info-card-title"
      >
        {item.title}
      </h3>

      {/* Summary — v19: no colored block or left border; only the ✦ mark carries brand color. */}
      {summary && (
        <p
          className={cn(
            'mt-2 font-event-title text-[16px] font-medium leading-[1.58] text-muted-foreground line-clamp-5',
            isRead && 'opacity-70',
          )}
          data-testid="info-card-summary"
        >
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
