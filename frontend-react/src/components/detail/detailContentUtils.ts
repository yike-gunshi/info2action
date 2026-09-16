import type { FeedItem } from '../../lib/types'

export type KeyPointItem = string | { title: string; points: string[] }

export function normalizeSummaryText(summary?: string | null): string | null {
  const value = summary
    ?.replace(/^【精华速览】\s*/, '')
    .replace(/^精华速览[:：]\s*/, '')
    .trim()
  return value || null
}

export function paragraphLines(text: string): string[] {
  return text
    .split(/\n{2,}/)
    .map((line) => line.trim())
    .filter(Boolean)
}

export function collectImages(item: FeedItem): string[] {
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

export function getDetailModalVariant(item: FeedItem): 'no-media' | 'single-media' | 'multi-media' {
  if (extractVideoMp4Url(item) || (item.platform === 'youtube' && item.id.startsWith('yt_'))) return 'single-media'
  const imageCount = collectImages(item).length
  if (imageCount <= 0) return 'no-media'
  if (imageCount === 1) return 'single-media'
  return 'multi-media'
}

// v12.2: 检测视频帖并返回 mp4 直链 (否则 null)
export function extractVideoMp4Url(item: FeedItem): string | null {
  if (!item.media_json) return null
  for (const m of item.media_json) {
    if (typeof m === 'object' && m !== null) {
      const maybeVideo = m as { type?: string; url?: string }
      if (maybeVideo.type === 'video' && maybeVideo.url) return maybeVideo.url
    }
  }
  return null
}
