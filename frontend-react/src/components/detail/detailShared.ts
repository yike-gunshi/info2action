import type { CSSProperties } from 'react'
import { buildInfoItemShareUrl } from '../../lib/itemDeepLink'
import type { FeedItem } from '../../lib/types'
import { normalizeSummaryText } from './detailContentUtils'

export const paperSurfaceStyle: CSSProperties = {
  backgroundImage: 'var(--modal-paper-texture)',
  backgroundSize: 'auto, 6px 6px',
}

export const mediaCapStyle: CSSProperties = {
  aspectRatio: '16 / 9',
  maxHeight: 'min(180px, calc((var(--app-visual-height) - 32px - var(--modal-bottom-clearance)) * 0.25))',
}

export function compactShareSummary(summary?: string | null): string {
  const normalized = normalizeSummaryText(summary)?.replace(/\s+/g, ' ').trim() || ''
  if (!normalized) return ''
  return normalized.length > 100 ? `${normalized.slice(0, 100)}...` : normalized
}

export function buildItemShareText(item: FeedItem): string {
  const title = item.title?.trim() || '一条信息'
  const summary = compactShareSummary(item.ai_summary)
  const itemDeepLink = buildInfoItemShareUrl(item.id)
  return `我正在 info2act 浏览「${title}」：${summary}\n一起看看吧 ${itemDeepLink}`
}

export async function copyTextToClipboard(text: string): Promise<void> {
  if (copyTextWithTextarea(text)) return

  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }

  throw new Error('copy failed')
}

export function copyTextWithTextarea(text: string): boolean {
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
