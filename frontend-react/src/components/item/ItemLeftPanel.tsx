/**
 * item 原文内容渲染面板
 *
 * 渲染顺序(BF-0420-8 用户新要求):
 *   Meta 块(头部 + 标题 + 互动数据)→ 媒体(视频/YouTube/图)→ ASR → 正文
 *
 * 设计规范:DESIGN.md 模块 14.3.3
 * 桌面 padding 24px / 移动 padding 16px,父容器控制 media query。
 */
import React, { useMemo, useState } from 'react'
import { ChevronDown, ImageOff, Heart, MessageCircle, Eye, Play, Share2, ThumbsUp, GitFork, Bookmark, Star } from 'lucide-react'

import { cn, formatNumber, platformName, relativeTime } from '../../lib/utils'
import { PlatformAvatar } from '../shared/PlatformIcon'
import { VideoPlayer } from '../detail/VideoPlayer'
import { YoutubePlayer } from '../detail/YoutubePlayer'
import { TranscriptPanel } from '../detail/TranscriptPanel'
import type { FeedItem } from '../../lib/types'

interface ItemLeftPanelProps {
  item: FeedItem
  showHeader?: boolean
  surface?: 'card' | 'plain'
  truncateContent?: boolean
  className?: string
}

const TRUNCATE_LEN = 2000

/** 从 media_json 提取第一个 type=video 的 mp4 URL(与 DetailPanel extractVideoMp4Url 等价) */
function extractVideoMp4Url(item: FeedItem): string | null {
  if (!item.media_json) return null
  for (const m of item.media_json) {
    if (typeof m === 'object' && m !== null) {
      const maybe = m as { type?: string; url?: string }
      if (maybe.type === 'video' && maybe.url) return maybe.url
    }
  }
  return null
}

/** 汇总 cover_url / media_json / thumbnail 所有图片 URL(与 DetailPanel collectImages 等价) */
function collectImages(item: FeedItem): string[] {
  const urls: string[] = []
  if (item.cover_url) urls.push(item.cover_url)
  if (item.media_json) {
    for (const m of item.media_json) {
      const u = typeof m === 'string' ? m : m?.url
      if (u && !urls.includes(u)) urls.push(u)
    }
  }
  if (item.thumbnail && !urls.includes(item.thumbnail)) urls.push(item.thumbnail)
  return urls
}

/** YouTube video_id 从 item.id (yt_xxx) 前缀取 */
function extractYoutubeId(item: FeedItem): string | null {
  if (item.platform === 'youtube' && item.id.startsWith('yt_')) {
    return item.id.slice(3)
  }
  return null
}

/** ASR 转写面板显示条件:视频类平台(Twitter 视频 / YouTube) */
function shouldShowTranscript(item: FeedItem, hasVideoMp4: boolean, hasYoutube: boolean): boolean {
  return hasYoutube || (item.platform === 'twitter' && hasVideoMp4)
}

// BF-0420-8: 互动数据从 ItemRightPanel 移入,嵌入 Meta 块
interface MetricEntry { Icon: typeof Heart; value: number; label: string }
type MetricKey = 'likes' | 'comments' | 'shares' | 'views' | 'plays' | 'danmaku' | 'upvotes' | 'stars' | 'forks' | 'collects'

const METRIC_META: Record<MetricKey, { Icon: typeof Heart; label: string }> = {
  likes: { Icon: Heart, label: '点赞' }, comments: { Icon: MessageCircle, label: '评论' },
  shares: { Icon: Share2, label: '转发' }, views: { Icon: Eye, label: '浏览' },
  plays: { Icon: Play, label: '播放' }, danmaku: { Icon: MessageCircle, label: '弹幕' },
  upvotes: { Icon: ThumbsUp, label: '赞' }, stars: { Icon: Star, label: 'Star' },
  forks: { Icon: GitFork, label: 'Fork' }, collects: { Icon: Bookmark, label: '收藏' },
}
const PLATFORM_METRICS: Record<string, MetricKey[]> = {
  twitter: ['likes', 'shares', 'comments', 'views'], xiaohongshu: ['likes', 'collects', 'comments'],
  bilibili: ['plays', 'danmaku', 'likes'], reddit: ['upvotes', 'comments'],
  hackernews: ['upvotes', 'comments'], github: ['stars', 'forks'],
}

function collectMetrics(item: FeedItem): MetricEntry[] {
  const m = item.metrics_json || {}
  const get: Record<MetricKey, number | undefined> = {
    likes: item.likes ?? m.likes ?? m.like_count,
    comments: item.comments ?? m.comments ?? m.comment_count,
    shares: item.shares ?? m.shares ?? m.retweets ?? m.share_count,
    views: item.views ?? m.views ?? m.view_count,
    plays: item.plays ?? m.plays ?? m.play_count,
    danmaku: item.danmaku ?? m.danmaku,
    upvotes: item.upvotes ?? m.upvotes ?? m.score ?? m.points,
    stars: item.stars ?? m.stars ?? m.stargazers_count,
    forks: item.forks ?? m.forks ?? m.forks_count,
    collects: item.bookmarks ?? m.collects ?? m.collect_count,
  }
  const keys = PLATFORM_METRICS[item.platform] ?? ['likes', 'comments']
  return keys.map((k) => ({ ...METRIC_META[k], value: get[k] ?? 0 })).filter((e) => e.value > 0)
}

function MetricsRow({ item }: { item: FeedItem }): React.ReactElement | null {
  const metrics = useMemo(() => collectMetrics(item), [item])
  if (metrics.length === 0) return null
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
      {metrics.map((m, i) => (
        <span
          key={i}
          className="inline-flex items-center gap-1 text-sm font-mono text-muted-foreground"
          title={m.label}
        >
          <m.Icon className="w-3.5 h-3.5" />
          {formatNumber(m.value)}
        </span>
      ))}
    </div>
  )
}

function GridImage({ src, single = false }: { src: string; single?: boolean }): React.ReactElement {
  const [err, setErr] = useState(false)
  if (err) {
    return (
      <div className="flex items-center justify-center bg-muted text-muted-foreground/40 w-full aspect-video rounded-[14px]">
        <ImageOff className="w-6 h-6" />
      </div>
    )
  }
  // BF-0420-21 rev2: 单图(弹窗内)限高 450px + object-contain,避免竖向长图撑爆弹窗
  // (v1 用 70vh 在大屏浏览器仍显示 700+px 太大,改绝对值 450px 保证信息密度)
  const singleCls = 'w-full max-h-[450px] object-contain rounded-[14px]'
  const gridCls = 'w-full rounded-[14px] object-cover'
  return (
    <img
      src={src}
      alt=""
      className={single ? singleCls : gridCls}
      referrerPolicy="no-referrer"
      loading="lazy"
      onError={() => setErr(true)}
    />
  )
}

function ImageBlock({ images }: { images: string[] }): React.ReactElement | null {
  if (images.length === 0) return null
  if (images.length === 1) {
    return <GridImage src={images[0]} single />
  }
  return (
    <div className="grid grid-cols-2 gap-2">
      {images.slice(0, 6).map((src, i) => (
        <GridImage key={i} src={src} />
      ))}
    </div>
  )
}

export function ItemLeftPanel({
  item,
  showHeader = true,
  surface = 'card',
  truncateContent = true,
  className,
}: ItemLeftPanelProps): React.ReactElement {
  const [expanded, setExpanded] = useState(false)

  const videoMp4Url = extractVideoMp4Url(item)
  const youtubeVideoId = extractYoutubeId(item)
  const hasVideo = !!videoMp4Url || !!youtubeVideoId
  const images = hasVideo ? [] : collectImages(item)

  const content = item.content || item.description || ''
  const needsTruncation = truncateContent && content.length > TRUNCATE_LEN
  const displayContent = expanded || !needsTruncation
    ? content
    : content.slice(0, TRUNCATE_LEN)
  const bodyTextClass = surface === 'plain'
    ? 'space-y-3 font-event-title text-[16px] leading-[1.82] tracking-[0] text-[#3F3A34] [&_strong]:font-bold [&_strong]:text-[#171512]'
    : 'space-y-2 text-[16px] leading-[1.7] text-foreground'

  const timeSource = item.published_at || item.fetched_at

  return (
    <div
      className={cn(
        'flex flex-col',
        surface === 'card' ? 'bg-card rounded-[8px] p-6' : 'bg-transparent p-0',
        surface === 'plain' ? 'gap-5' : 'gap-4',
        className,
      )}
    >
      {/* BF-0420-8 新顺序:Meta → 媒体 → ASR → 正文 */}

      {showHeader && (
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2 min-w-0">
            <PlatformAvatar
              platform={item.platform}
              avatarUrl={item.author_avatar}
              size={32}
            />
            <span className="text-sm font-medium text-foreground truncate max-w-[240px]">
              {item.author_name || platformName(item.platform)}
            </span>
            {item.author_name && (
              <span className="text-xs text-muted-foreground shrink-0">
                · {platformName(item.platform)}
              </span>
            )}
            {timeSource && (
              <span className="text-xs text-muted-foreground shrink-0 ml-auto">
                {relativeTime(timeSource)}
              </span>
            )}
          </div>
          {item.title && (
            <h1 className="text-lg font-bold leading-tight text-foreground">
              {item.title}
            </h1>
          )}
          <MetricsRow item={item} />
        </div>
      )}

      {/* 2. 媒体 — YouTube / 视频 / 图片;纯文本不渲染(R4.2) */}
      {youtubeVideoId && (
        <YoutubePlayer videoId={youtubeVideoId} itemId={item.id} />
      )}
      {!youtubeVideoId && videoMp4Url && (
        <VideoPlayer mp4Url={videoMp4Url} itemId={item.id} />
      )}
      {!hasVideo && images.length > 0 && (
        <ImageBlock images={images} />
      )}

      {/* 3. ASR 转写 — 仅视频类平台(Twitter 视频 / YouTube);紧贴视频,视频-转写联动 */}
      {shouldShowTranscript(item, !!videoMp4Url, !!youtubeVideoId) && (
        <TranscriptPanel itemId={item.id} item={item} />
      )}

      {/* 4. 正文 — 段落拆分,超过 2000 字截断;放最后(Twitter/YouTube 正文通常是简介性质,不是主内容) */}
      {content && (
        <div>
          <div
            data-testid="item-left-body-text"
            className={bodyTextClass}
            style={{ wordBreak: 'break-word' }}
          >
            {displayContent.split('\n').map((line, i) => (
              <p key={i}>{line || '\u00A0'}</p>
            ))}
            {needsTruncation && !expanded && (
              <span className="text-muted-foreground">...</span>
            )}
          </div>
          {needsTruncation && (
            <button
              onClick={() => setExpanded(!expanded)}
              className="flex items-center gap-1 text-sm text-primary hover:text-primary/80 mt-2 transition-colors"
            >
              {expanded ? '收起' : '展开全文'}
              <ChevronDown className={cn('w-3 h-3 transition-transform', expanded && 'rotate-180')} />
            </button>
          )}
        </div>
      )}
    </div>
  )
}
