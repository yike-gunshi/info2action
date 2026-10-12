import { useEffect, useState } from 'react'
import { getAiSearchHistory, type AiSearchHistoryItem, type AiSearchHistoryResponse } from '../../lib/api/aiSearch'
import { useAuthStore } from '../../store/authStore'
import { useAiSearchStore } from '../../store/aiSearchStore'
import { tryRefresh } from '../../lib/api/_client'
import './AiSearchHistory.css'

interface HistoryState {
  userId: string | null
  limit: number
  data?: AiSearchHistoryResponse
  error?: string
}

function historyDate(createdAt: string): string {
  const date = new Date(createdAt)
  if (!Number.isFinite(date.getTime())) return '日期未知'
  return `${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getDate()).padStart(2, '0')}`
}

function HistoryRow({ item }: { item: AiSearchHistoryItem }) {
  const running = item.status === 'queued' || item.status === 'running'
  const retrieving = item.status === 'retrieving'
  const contents = <>
    <span className="hq" title={item.query}>{item.query}</span>
    {item.status === 'done'
      ? <time className="hd" dateTime={item.created_at}>{historyDate(item.created_at)}</time>
      : <span className="hd">{retrieving ? '找资料中' : item.status === 'insufficient' ? '资料不足' : running ? '进行中' : '未完成'}</span>}
  </>

  // 打开已保存的任务不新建搜索；失败行仍不可点。
  const available = item.status === 'done' || item.status === 'insufficient' || retrieving || running
  const href = `#ai-search?job=${encodeURIComponent(item.job_id)}&q=${encodeURIComponent(item.query)}`
  return available
    ? <a className={`hrow${running || retrieving ? ' run' : ''}`} href={href} onClick={event => {
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || window.location.hash !== href) return
      event.preventDefault()
      void useAiSearchStore.getState().loadJob(item.job_id, item.query)
    }}>{contents}</a>
    : <div className="hrow fail" aria-disabled="true">{contents}</div>
}

export function AiSearchHistory() {
  const userId = useAuthStore(s => s.user?.id ?? null)
  const authReady = useAuthStore(s => s.isChecked && !s.isLoading)
  const [history, setHistory] = useState<HistoryState | null>(null)

  useEffect(() => {
    setHistory(null)
    if (!authReady) return

    const limit = Number(getComputedStyle(document.documentElement).getPropertyValue('--ai-hist-max').trim())
    if (!Number.isInteger(limit) || limit < 1) {
      setHistory({ userId, limit: 0, error: '搜索记录暂时无法加载，请稍后再试' })
      return
    }

    const controller = new AbortController()

    // This endpoint answers 200 + anonymous for a caller without a valid session, so
    // that the page can tell "not signed in" from "signed in with no records" — two
    // different empty states. The cost is that an expired access cookie no longer
    // produces the 401 that would normally drive a refresh: the history would read
    // "please sign in" while the app still believes it is signed in, and any search
    // started from here would be stored with no owner.
    // So when the answer contradicts what the app believes, reconcile it here.
    async function load(): Promise<void> {
      let data = await getAiSearchHistory(limit, controller.signal)
      if (data.anonymous && userId) {
        const outcome = await tryRefresh()
        if (controller.signal.aborted) return
        if (outcome === 'ok') {
          data = await getAiSearchHistory(limit, controller.signal)
        } else if (outcome === 'expired') {
          // Stop claiming a session the server will not honour; the sign-in prompt
          // below is then telling the truth rather than contradicting the header.
          useAuthStore.getState().setUser(null)
        }
        // 'unavailable' 是服务端抖动，不动登录态（BF-0708-1），这轮就按匿名渲染
      }
      if (!controller.signal.aborted) setHistory({ userId, limit, data })
    }

    void load().catch(() => {
      if (!controller.signal.aborted) {
        setHistory({ userId, limit, error: '搜索记录暂时无法加载，请稍后再试' })
      }
    })
    return () => controller.abort()
  }, [userId, authReady])

  // Do not show the previous account's records while the next request starts.
  const current = authReady && history?.userId === userId ? history : null
  const data = current?.data
  const items = data?.items.slice(0, current?.limit) ?? []
  const remaining = data && !data.anonymous && current && data.total > current.limit
    ? Math.max(0, data.total - items.length)
    : 0

  return <section className="ai-search-history hist" aria-label="搜索记录">
    {current?.error ? <p className="hist-error" role="alert">{current.error}</p> : null}
    {data ? data.anonymous
      ? null
      : items.length === 0
        ? <p className="none">搜过的内容会出现在这里</p>
        : <div className="hlist">
          <span className="hlab">最近搜索</span>
          <div>{items.map(item => <HistoryRow key={item.job_id} item={item} />)}</div>
          {remaining > 0 ? <p className="hlab hist-more">还有 {remaining} 条</p> : null}
        </div>
      : null}
  </section>
}
