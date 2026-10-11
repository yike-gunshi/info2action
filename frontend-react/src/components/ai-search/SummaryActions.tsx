import { useEffect, useRef, useState } from 'react'
import { Copy, Share2 } from 'lucide-react'
import { toast } from 'sonner'
import type { AiSearchItem } from '../../lib/api/aiSearch'
import { useAiSearchStore } from '../../store/aiSearchStore'
import './SummaryActions.css'

function metadata(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null
}

function summaryWithReferences(contentMd: string, items: AiSearchItem[]): string {
  const seen = new Set<number>()
  const references = []
  for (const match of contentMd.matchAll(/\[(\d{1,3})\]/g)) {
    const n = Number(match[1])
    const item = items[n - 1]
    if (!item || seen.has(n)) continue
    seen.add(n)
    // Stored citation numbers address the API's citation-first item order.
    // Source bodies must never enter this JSON.
    references.push({
      n,
      title: metadata(item.title),
      url: metadata(item.url),
      author_name: metadata(item.author_name),
      platform: metadata(item.platform),
      source: metadata(item.source),
      published_at: metadata(item.published_at),
      created_at: metadata(item.created_at),
      fetched_at: metadata(item.fetched_at),
    })
  }
  return `${contentMd}\n\n参考信息（JSON）\n${JSON.stringify(references, null, 2)}`
}

export function SummaryActions({ onFeedback }: { onFeedback?: (message: string) => void }) {
  const jobId = useAiSearchStore(state => state.jobId)
  const contentMd = useAiSearchStore(state => state.summary.contentMd)
  const items = useAiSearchStore(state => state.items)
  const operation = useRef(0)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setBusy(false)
    return () => { operation.current += 1 }
  }, [jobId, contentMd])

  async function writeClipboard(text: string, success: string, failure: string) {
    const currentOperation = ++operation.current
    const isCurrent = () => {
      const state = useAiSearchStore.getState()
      return operation.current === currentOperation && state.jobId === jobId && state.summary.contentMd === contentMd
    }
    setBusy(true)
    // Result goes to the page-level toast (same one 收藏 uses), not the hint row
    // under the search box: that row sits far from the buttons and reads as part
    // of the search field (user feedback 2026-09-22).
    onFeedback?.('')
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable')
      await navigator.clipboard.writeText(text)
      if (isCurrent()) toast.success(success)
    } catch {
      if (isCurrent()) toast.error(failure)
    } finally {
      if (isCurrent()) setBusy(false)
    }
  }

  function copySummary() {
    if (busy || !contentMd?.trim()) return
    void writeClipboard(summaryWithReferences(contentMd, items), '已复制正文与参考信息', '复制失败，请重试')
  }

  function shareSummary() {
    if (busy || !jobId) return
    // Build from origin + pathname only. Never carry the current query string:
    // the backend accepts `?token=<shared secret>` as authentication
    // (src/middleware/auth.py), so a user who signed in through such a URL would
    // otherwise hand that credential to everyone they share the summary with.
    const url = new URL(window.location.pathname, window.location.origin)
    url.hash = `ai-search?job=${encodeURIComponent(jobId)}`
    void writeClipboard(url.href, '分享链接已复制', '分享链接复制失败，请重试')
  }

  return (
    <div className="ai-summary-actions">
      <button type="button" aria-label="复制" className="ai-summary-action" disabled={busy || !contentMd?.trim()} onClick={copySummary}>
        <Copy aria-hidden="true" />
      </button>
      <button type="button" aria-label="分享" className="ai-summary-action" disabled={busy || !jobId} onClick={shareSummary}>
        <Share2 aria-hidden="true" />
      </button>
    </div>
  )
}
