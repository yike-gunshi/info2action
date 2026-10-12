import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useAiSearchStore } from '../../store/aiSearchStore'
import { useAuthStore } from '../../store/authStore'
import { TopBar } from '../layout/TopBar'
import { useBodyScrollLock } from '../../hooks/useBodyScrollLock'
import { AiSearchInput } from './AiSearchInput'
import { AiSearchResults } from './AiSearchResults'
import { CardsPane } from './CardsPane'
import { AiSearchHistory } from './AiSearchHistory'
import { SummaryToc } from './SummaryToc'
import { AI_SEARCH_DESKTOP_QUERY } from './aiSearchScroll'
import './ai-search.tokens.css'
import './ai-search.css'

export function AiSearchPage() {
  const phase = useAiSearchStore((s) => s.phase)
  const query = useAiSearchStore((s) => s.query)
  const hasItems = useAiSearchStore((s) => s.items.length > 0)
  const loadingJob = useAiSearchStore((s) => s.loadingJob)
  const pendingSummary = useAiSearchStore((s) => s.summary.status === 'retrieving' || s.summary.status === 'queued' || s.summary.status === 'running')
  const unavailable = useAiSearchStore((s) => s.unavailable)
  const requestError = useAiSearchStore((s) => s.requestError)
  const emptyResult = useAiSearchStore((s) => s.emptyResult)
  const staleResult = useAiSearchStore((s) => (s.summary.error || s.summary.reason) === 'job_not_found')
  const retrievalFailed = useAiSearchStore((s) => ['retrieval_failed', 'retrieval_timeout'].includes(s.summary.error || s.summary.reason || ''))
  const vectorOk = useAiSearchStore((s) => s.vectorOk)
  const jobId = useAiSearchStore((s) => s.jobId)
  const authReady = useAuthStore((s) => s.isChecked && !s.isLoading)
  const userId = useAuthStore((s) => s.user?.id ?? null)
  const userRole = useAuthStore((s) => s.user?.role)
  const [feedback, setFeedback] = useState('')
  const [desktop, setDesktop] = useState(() => window.matchMedia?.(AI_SEARCH_DESKTOP_QUERY).matches ?? false)
  const pageRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const focusOnReset = useRef(false)
  const paneRef = useRef<HTMLDivElement>(null)
  const centered = phase === 'idle'
  const hasResults = !centered
  const loading = phase === 'submitting' || loadingJob || pendingSummary
  const showCards = hasResults && !emptyResult && !staleResult && !retrievalFailed && !unavailable && (hasItems || loading)

  useEffect(() => { setFeedback('') }, [jobId, phase])

  useEffect(() => {
    const state = useAiSearchStore.getState()
    state.invalidateStatus()
    if (authReady) void state.refreshStatus()
    return () => useAiSearchStore.getState().invalidateStatus()
  }, [authReady, userId, userRole])

  useBodyScrollLock(desktop, { scrollbarGutter: 'auto' })

  useEffect(() => {
    const media = window.matchMedia?.(AI_SEARCH_DESKTOP_QUERY)
    if (!media) return
    const update = () => setDesktop(media.matches)
    media.addEventListener?.('change', update)
    return () => media.removeEventListener?.('change', update)
  }, [])

  useLayoutEffect(() => {
    if (!centered || !focusOnReset.current) return
    inputRef.current?.focus()
    focusOnReset.current = false
  }, [centered, query, jobId])

  useEffect(() => {
    if (!desktop) return
    const routeWheel = (event: WheelEvent) => {
      const page = pageRef.current
      const pane = paneRef.current
      const target = event.target
      // Portalled dialogs and menus keep their own scrolling; zoom stays native.
      if (event.ctrlKey || event.defaultPrevented || !page || !pane || !(target instanceof Element)) return
      if (!page.contains(target) && target !== document.body && target !== document.documentElement) return
      if (target.closest('[role="dialog"], [role="menu"], [data-ai-scroll="cards"]')) return
      // The table of contents scrolls itself when it overflows (long document on a
      // short window). Merely letting the event through is not enough — the page
      // router below calls preventDefault, so the wheel has to be applied to the
      // list here or its hidden entries stay unreachable (review finding 2026-09-22).
      const toc = target.closest<HTMLElement>('[data-ai-scroll="toc"]')
      if (toc) {
        if (toc.scrollHeight > toc.clientHeight) {
          event.preventDefault()
          toc.scrollTop += event.deltaY
        }
        return
      }
      // Popovers need no special role to keep native scrolling. Walk past an
      // exhausted inner list to any ancestor that can still consume this wheel.
      for (let ancestor: Element | null = target; ancestor && ancestor !== pane; ancestor = ancestor.parentElement) {
        if (!(ancestor instanceof HTMLElement)) continue
        const style = getComputedStyle(ancestor)
        if (!/^(auto|scroll|overlay)$/.test(style.overflowY || style.overflow)) continue
        const maxScroll = ancestor.scrollHeight - ancestor.clientHeight
        if (maxScroll > 0 && (event.deltaY > 0 ? ancestor.scrollTop < maxScroll : event.deltaY < 0 && ancestor.scrollTop > 0)) return
      }
      event.preventDefault()
      const unit = event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? pane.clientHeight
        : event.deltaMode === WheelEvent.DOM_DELTA_LINE ? Number.parseFloat(getComputedStyle(pane).lineHeight) : 1
      pane.scrollTop += event.deltaY * (Number.isFinite(unit) ? unit : 1)
    }
    window.addEventListener('wheel', routeWheel, { passive: false })
    return () => window.removeEventListener('wheel', routeWheel)
  }, [desktop])

  useEffect(() => {
    function readHash(initial = false) {
      const hash = window.location.hash.slice(1)
      if (hash !== 'ai-search' && !hash.startsWith('ai-search?')) return
      const params = new URLSearchParams(hash.split('?')[1] ?? '')
      const id = params.get('job')
      const state = useAiSearchStore.getState()
      if (id) {
        if (initial || id !== state.jobId) void state.loadJob(id, params.get('q') ?? undefined)
      } else {
        focusOnReset.current = !initial
        state.reset()
        // From a result, the top-bar AI entry starts a blank question even if
        // the site's ordinary search field still contains a previous query.
        if (!initial && state.phase !== 'idle') {
          window.history.replaceState(window.history.state, '', '#ai-search')
        } else {
          state.setQuery(params.get('q') ?? '')
        }
      }
    }
    readHash(true)
    const onHash = () => readHash()
    window.addEventListener('hashchange', onHash)
    return () => {
      window.removeEventListener('hashchange', onHash)
      useAiSearchStore.getState().stopPolling()
    }
  }, [])

  return (
    <div ref={pageRef} className="ai-search-page">
      <TopBar activeL1={null} />
      <main className="ai-search-body" data-idle={centered} data-columns={showCards}>
        {unavailable ? (
          <div className="ai-search-unavailable grid place-content-center gap-4 text-center text-sm">
            <p>AI 搜索暂未开放</p>
            <a href="#" className="text-[var(--brand)] hover:underline">返回</a>
          </div>
        ) : (
          <div className="ai-search-results">
            <div className="ai-search-left">
              <div className="ai-search-doc">
                <div className="ai-search-controls">
                  {centered ? <AiSearchInput feedback={feedback} inputRef={inputRef} /> : null}
                  {requestError ? <p role="alert" className="ai-search-request-error">{requestError}</p> : null}
                  {!vectorOk ? <p className="ai-search-vector">向量检索失败，仅按关键词匹配</p> : null}
                </div>
                {centered ? <AiSearchHistory /> : null}
                {hasResults ? <AiSearchResults paneRef={paneRef} onFeedback={setFeedback} inputRef={inputRef} query={query} /> : null}
              </div>
            </div>
            {showCards ? <SummaryToc paneRef={paneRef} reserveSpace loading={loading} /> : null}
            {showCards ? <CardsPane /> : null}
          </div>
        )}
      </main>
    </div>
  )
}
