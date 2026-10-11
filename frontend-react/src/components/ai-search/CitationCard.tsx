import { memo, useState } from 'react'
import type { AiSearchItem } from '../../lib/api/aiSearch'
import { platformName } from '../../lib/utils'
import { useDetailStore } from '../../store/detailStore'
import { useFeedStore } from '../../store/feedStore'
import './CitationCard.css'

function cardMedia(item: AiSearchItem): { src: string | null; videoPoster: boolean } {
  const first = item.media_json?.[0]
  const media = typeof first === 'object' && first ? first as { type?: string; url?: string } : null
  const video = media?.type === 'video' && Boolean(media.url)
  if (item.platform === 'twitter' && video) {
    return { src: `/api/media/twitter-poster/${item.id}.jpg`, videoPoster: true }
  }
  const image = item.cover_url || item.thumbnail || (!video && (typeof first === 'string' ? first : media?.url)) || null
  const src = item.platform === 'twitter' && image?.startsWith('https://pbs.twimg.com/')
    ? `/api/media/twitter-photo/${item.id}/0.jpg`
    : image
  return { src, videoPoster: false }
}

function sourceLabel(item: AiSearchItem): string {
  const platform = platformName(item.platform)
  let author = item.author_name?.trim() ?? ''
  const aliases = [platform, item.platform, item.platform === 'twitter' || item.platform === 'x' ? 'Twitter' : ''].filter(Boolean)
  for (const alias of aliases) {
    if (author === alias) {
      author = ''
      break
    }
    const divider = [' · ', '・', ' - ', ' ｜ ', ' | ', ' / '].find((value) => author.startsWith(`${alias}${value}`))
    if (divider) {
      author = author.slice(alias.length + divider.length).trim()
      break
    }
  }
  return author ? `${platform} · ${author}` : platform
}

function shortDate(value: string | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return `${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getDate()).padStart(2, '0')}`
}

interface CitationCardProps {
  item: AiSearchItem
  number: number
  highlighted?: boolean
}

export const CitationCard = memo(function CitationCard({ item, number, highlighted = false }: CitationCardProps) {
  const openItem = useDetailStore((state) => state.openItem)
  const prefetchItem = useDetailStore((state) => state.prefetchItem)
  const clickedAt = useFeedStore((state) => state.clickedAtById[item.id])
  const isRead = Boolean(item.clicked_at || clickedAt)
  const { src, videoPoster } = cardMedia(item)
  const [failedSrc, setFailedSrc] = useState<string | null>(null)
  const time = item.published_at || item.fetched_at || item.created_at

  return (
    <button
      type="button"
      id={`ai-card-${item.id}`}
      data-ai-card-id={item.id}
      data-read={isRead ? 'true' : 'false'}
      aria-label={`[${number}] ${sourceLabel(item)} ${item.title}`}
      className={`ai-citation-card${highlighted ? ' ai-citation-card-highlighted' : ''}`}
      onClick={() => openItem(item.id)}
      onMouseEnter={() => prefetchItem(item.id)}
      onFocus={() => prefetchItem(item.id)}
    >
      {src && failedSrc !== src && (
        <img
          className="ai-citation-card-thumbnail"
          src={src}
          alt=""
          loading="lazy"
          decoding="async"
          referrerPolicy={videoPoster ? undefined : 'no-referrer'}
          onError={() => setFailedSrc(src)}
        />
      )}
      <span className="ai-citation-card-text">
        <span className="ai-citation-card-source">
          <span className="ai-citation-card-origin"><b>[{number}]</b>{sourceLabel(item)}</span>
          <time dateTime={time}>{shortDate(time)}</time>
        </span>
        <span className="ai-citation-card-title">{item.title}</span>
      </span>
    </button>
  )
})
