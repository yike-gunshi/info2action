import { create } from 'zustand'
import { tryRefresh } from '../lib/api/_client'
import { useAuthStore } from './authStore'
import { describeSummaryError, getAiSearchStatus, getAiSearchJob, postAiSearch, QUERY_MAX_CHARS, QUERY_VALIDATION_MESSAGE, POLL_INTERVAL_MS, RETRIEVAL_POLL_INTERVAL_MS, RETRIEVAL_IN_PROGRESS_MESSAGE, HIGHLIGHT_MS } from '../lib/api/aiSearch'
import type { AiSearchItem, AiSearchJobResponse, AiSearchJobStatus, AiSearchRetrievalStage, AiSearchStatusResponse } from '../lib/api/aiSearch'

export interface AiSearchSummary {
  status: AiSearchJobStatus | 'none'
  stage?: AiSearchRetrievalStage
  keywords?: string[]
  position?: number
  reason?: string | null
  contentMd: string | null
  citedIds: string[]
  error: string | null
  model: string | null
  elapsedMs: number | null
  citedCount: number | null
}
interface AiSearchState {
  query: string
  phase: 'idle' | 'submitting' | 'results'
  /** 正在取一个既有任务：冻结输入并显示骨架，但不是「搜索中」 */
  loadingJob: boolean
  jobId: string | null
  items: AiSearchItem[]
  vectorOk: boolean
  lexicalHits: number
  vectorHits: number
  summary: AiSearchSummary
  searchStatus: AiSearchStatusResponse | null
  statusLoading: boolean
  refreshStatus: () => Promise<void>
  invalidateStatus: () => void
  requestError: string | null
  requestNotice: string | null
  unavailable: boolean
  emptyResult: boolean
  highlightItemId: string | null
  cardOrder: string[]
  setQuery: (q: string) => void
  submit: (q: string) => Promise<void>
  loadJob: (id: string, hintQuery?: string) => Promise<void>
  poll: (immediate?: boolean) => void
  stopPolling: () => void
  highlight: (itemId: string, durationMs?: number) => void
  reset: () => void
}

const emptySummary = (): AiSearchSummary => ({
  status: 'none', contentMd: null, citedIds: [], error: null, reason: null,
  model: null, elapsedMs: null, citedCount: null,
})
const initialState = () => ({
  query: '', phase: 'idle' as const, jobId: null, items: [], vectorOk: true,
  lexicalHits: 0, vectorHits: 0, summary: emptySummary(), loadingJob: false,
  requestError: null, requestNotice: null, unavailable: false, emptyResult: false, highlightItemId: null, cardOrder: [],
})
let generation = 0
let statusGeneration = 0
let pollTimer: ReturnType<typeof setTimeout> | undefined
let highlightTimer: ReturnType<typeof setTimeout> | undefined
let visibilityListener: (() => void) | undefined
let polling = false
let inFlight = false
const pending = (status: AiSearchSummary['status']) => status === 'retrieving' || status === 'queued' || status === 'running'

function clearPolling() {
  clearTimeout(pollTimer)
  pollTimer = undefined
  if (visibilityListener) document.removeEventListener('visibilitychange', visibilityListener)
  visibilityListener = undefined
  polling = false
  inFlight = false
}
function stopRequests() {
  generation += 1
  clearPolling()
}
function jobState(response: AiSearchJobResponse, fallbackId: string) {
  const ids = response.items.map(item => item.id)
  const citedIds = response.cited_ids ?? []
  return {
    ...(response.query !== undefined ? { query: response.query } : {}),
    phase: 'results' as const, jobId: response.job_id || fallbackId,
    items: response.items, emptyResult: response.items.length === 0 && response.status === 'done',
    ...(response.vector_ok !== undefined ? { vectorOk: response.vector_ok } : {}),
    // The API aligns items with the stored citation numbers. Keep cardOrder
    // for CardsPane's scroll anchoring as pending candidates become cited.
    cardOrder: ids,
    summary: {
      status: response.status, position: response.position, reason: response.error ?? null,
      stage: response.stage, keywords: response.keywords ?? [],
      contentMd: response.content_md ?? null, citedIds,
      error: response.error ?? null, model: response.model ?? null,
      elapsedMs: response.elapsed_ms ?? null, citedCount: response.cited_count ?? null,
    },
  }
}
function requestErrorState(error: unknown, status?: AiSearchStatusResponse | null) {
  const detail = error as { status?: number; message?: string }
  const message = detail?.message ?? ''
  if (detail?.status === 409 && message === 'retrieval_in_progress') {
    return { requestError: null, requestNotice: RETRIEVAL_IN_PROGRESS_MESSAGE, unavailable: false }
  }
  if (message === 'ai_search_disabled') return { requestError: null, unavailable: true }
  if ((detail?.status === 401 && message === 'login_required') || (detail?.status === 429 && message === 'quota_exhausted')) {
    statusGeneration += 1
    const reason = message as 'login_required' | 'quota_exhausted'
    return { requestError: null, unavailable: false, statusLoading: false,
      searchStatus: { enabled: true, logged_in: reason !== 'login_required', can_search: false,
        user_day_limit: status ? status.user_day_limit : 2, reason } }
  }
  let requestError = '服务异常，请稍后再试'
  if (message === 'invalid_query') requestError = QUERY_VALIDATION_MESSAGE
  else if (message === 'job_not_found') requestError = describeSummaryError(message)!
  else if (message.includes('Rate limit')) requestError = '操作太频繁，请稍后再试'
  else if (/failed to fetch|network|offline|load failed|网络/i.test(message)) requestError = '网络异常，请稍后再试'
  return { requestError, unavailable: false }
}
function writeJobHash(id: string | null, query?: string) {
  if (id) {
    // Carry the question too. A refresh keeps the address bar, so the title can
    // show it immediately instead of sitting empty until the response lands
    // (user feedback 2026-09-22). The response stays authoritative.
    const q = (query ?? '').trim()
    window.location.hash = `ai-search?job=${encodeURIComponent(id)}`
      + (q ? `&q=${encodeURIComponent(q)}` : '')
  } else {
    // Clear an obsolete job URL without treating the submitted result as navigation.
    window.history.replaceState(window.history.state, '', '#ai-search')
  }
}

export const useAiSearchStore = create<AiSearchState>((set, get) => ({
  ...initialState(),
  searchStatus: null,
  statusLoading: false,
  invalidateStatus: () => {
    statusGeneration += 1
    set({ searchStatus: null, statusLoading: false })
  },
  refreshStatus: async () => {
    const current = ++statusGeneration
    set({ statusLoading: true })
    try {
      let searchStatus = await getAiSearchStatus()
      // Status is a public 200 endpoint, so an expired access cookie does not
      // trigger the API client's usual 401 refresh. Reconcile once here too.
      if (current !== statusGeneration) return
      if (searchStatus.enabled && !searchStatus.logged_in && useAuthStore.getState().user) {
        const outcome = await tryRefresh()
        if (current !== statusGeneration) return
        if (outcome === 'ok') searchStatus = await getAiSearchStatus()
        else if (outcome === 'expired') useAuthStore.getState().setUser(null)
      }
      if (current === statusGeneration) set({ searchStatus, statusLoading: false, unavailable: !searchStatus.enabled })
    } catch {
      // A transient status failure does not revoke a known session or hide the page.
      if (current === statusGeneration) set({ statusLoading: false })
    }
  },
  setQuery: query => set({ query, requestNotice: null }),
  submit: async q => {
    const query = q.trim()
    if (Array.from(query).length < 1 || Array.from(query).length > QUERY_MAX_CHARS) {
      set({ requestError: QUERY_VALIDATION_MESSAGE })
      return
    }
    stopRequests()
    clearTimeout(highlightTimer)
    const current = generation
    set({ ...initialState(), query, phase: 'submitting' })
    try {
      const response = await postAiSearch(query)
      if (current !== generation) return
      const items = response.items ?? []
      set({
        phase: 'results', query: response.query, jobId: response.job_id,
        items, vectorOk: response.vector_ok ?? true, lexicalHits: response.lexical_hits ?? 0,
        vectorHits: response.vector_hits ?? 0, emptyResult: items.length === 0 && response.summary.status === 'none',
        cardOrder: items.map(item => item.id),
        summary: { ...emptySummary(), status: response.summary.status, position: response.summary.position,
          reason: response.summary.reason ?? null, error: response.summary.reason ?? null },
      })
      writeJobHash(response.job_id, response.query || get().query)
      // Reconcile availability; v2 consumes writing quota only after retrieval succeeds.
      if (response.job_id) void get().refreshStatus()
      get().poll(response.summary.status === 'retrieving')
    } catch (error) {
      if (current !== generation) return
      set({ phase: 'idle', ...requestErrorState(error, get().searchStatus) })
    }
  },
  loadJob: async (id, hintQuery) => {
    stopRequests()
    const current = generation
    const previous = get()
    // Opening an existing job is a fetch, not a search. 'submitting' drives the
    // spinner and the glow, so using it here made a finished result look like it
    // was still being searched for the whole time the page was loading
    // (user feedback 2026-09-22). 'results' with an empty summary renders the
    // skeleton instead, which is what this state actually is.
    set({ ...initialState(), query: hintQuery ?? (previous.jobId === id ? previous.query : ''), phase: 'results', jobId: id, loadingJob: true })
    try {
      const response = await getAiSearchJob(id)
      if (current !== generation) return
      set({ ...jobState(response, id), loadingJob: false })
      // 把问题补进地址栏。writeJobHash 只在提交时跑，所以从搜索记录点进来、
      // 或打开别人分享的链接时，地址里没有 q，刷新就又变回空标题
      // （用户 2026-09-22 反馈）。这里用 replaceState，不制造新的历史记录。
      const known = response.query || get().query
      if (known) {
        const want = `#ai-search?job=${encodeURIComponent(id)}&q=${encodeURIComponent(known)}`
        if (window.location.hash !== want) window.history.replaceState(window.history.state, '', want)
      }
      get().poll()
    } catch (error) {
      if (current !== generation) return
      const detail = error as { status?: number; message?: string }
      if (detail?.status === 404 && detail.message === 'job_not_found') {
        set({ phase: 'results', jobId: id, requestError: null, loadingJob: false,
          summary: { ...emptySummary(), status: 'failed', error: 'job_not_found' } })
      } else {
        set({ phase: 'idle', jobId: null, loadingJob: false, ...requestErrorState(error) })
      }
    }
  },
  poll: (immediate = false) => {
    if (polling || !get().jobId || !pending(get().summary.status)) return
    polling = true
    const current = generation
    const id = get().jobId!
    let retrievalStatusRefreshed = false
    const schedule = () => {
      if (current !== generation || !polling || document.visibilityState === 'hidden') return
      clearTimeout(pollTimer)
      const interval = get().summary.status === 'retrieving' ? RETRIEVAL_POLL_INTERVAL_MS : POLL_INTERVAL_MS
      pollTimer = setTimeout(() => { void tick() }, interval)
    }
    const tick = async () => {
      if (current !== generation || !polling || inFlight || document.visibilityState === 'hidden') return
      inFlight = true
      try {
        const response = await getAiSearchJob(id)
        if (current !== generation) return
        const retrievalFinished = get().summary.status === 'retrieving' && response.status !== 'retrieving'
        set(jobState(response, id))
        if (retrievalFinished && !retrievalStatusRefreshed) {
          retrievalStatusRefreshed = true
          void get().refreshStatus()
        }
        if (!pending(response.status)) clearPolling()
      } catch (error) {
        if (current !== generation) return
        // Network failures are transient; disabled or removed jobs cannot recover by polling.
        const detail = error as { status?: number; message?: string }
        if (detail?.status === 404 && detail.message === 'job_not_found') {
          set({ summary: { ...emptySummary(), status: 'failed', error: 'job_not_found' }, requestError: null })
          clearPolling()
        } else if (detail?.message === 'ai_search_disabled') {
          set(requestErrorState(error))
          clearPolling()
        }
      } finally {
        if (current === generation) {
          inFlight = false
          schedule()
        }
      }
    }
    visibilityListener = () => {
      clearTimeout(pollTimer)
      if (document.visibilityState !== 'hidden') void tick()
    }
    document.addEventListener('visibilitychange', visibilityListener)
    if (immediate) void tick()
    else schedule()
  },
  stopPolling: stopRequests,
  highlight: (itemId, durationMs = HIGHLIGHT_MS) => {
    clearTimeout(highlightTimer)
    set({ highlightItemId: itemId })
    highlightTimer = setTimeout(() => set({ highlightItemId: null }), durationMs)
  },
  reset: () => {
    stopRequests()
    clearTimeout(highlightTimer)
    set({ ...initialState(), unavailable: get().unavailable })
  },
}))
