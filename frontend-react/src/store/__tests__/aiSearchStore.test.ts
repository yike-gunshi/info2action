import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAiSearchStore } from '../aiSearchStore'
import { AiSearchAccessError, getAiSearchStatus, getAiSearchJob, postAiSearch } from '../../lib/api/aiSearch'
import type { AiSearchItem, AiSearchJobResponse, AiSearchPostResponse } from '../../lib/api/aiSearch'

vi.mock('../../lib/api/aiSearch', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../lib/api/aiSearch')>(),
  getAiSearchJob: vi.fn(), postAiSearch: vi.fn(), getAiSearchStatus: vi.fn(),
}))
const items: AiSearchItem[] = ['a', 'b', 'c'].map(id => ({ id, title: id, platform: 'rss', fetched_at: '' }))
const post = (overrides: Partial<AiSearchPostResponse> = {}): AiSearchPostResponse => ({
  job_id: 'job-1', query: 'test', query_lexical: 'test', window_days: 90,
  lexical_hits: 2, vector_hits: 3, vector_ok: true, items, summary: { status: 'queued', position: 2 }, ...overrides,
})
const job = (overrides: Partial<AiSearchJobResponse> = {}): AiSearchJobResponse => ({
  job_id: 'job-1', status: 'running', items, ...overrides,
})
function visibility(value: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value })
  document.dispatchEvent(new Event('visibilitychange'))
}

describe('aiSearchStore', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    useAiSearchStore.getState().reset()
    useAiSearchStore.getState().invalidateStatus()
    useAiSearchStore.setState({ unavailable: false })
    vi.resetAllMocks()
    visibility('visible')
    window.location.hash = 'ai-search'
    vi.mocked(getAiSearchStatus).mockResolvedValue({ enabled: true, logged_in: true, can_search: true, reason: null, user_day_limit: 2 })
    vi.mocked(postAiSearch).mockResolvedValue(post())
    vi.mocked(getAiSearchJob).mockResolvedValue(job())
  })
  afterEach(() => { useAiSearchStore.getState().reset(); vi.useRealTimers() })

  it.each([
    Object.assign(new Error('internal'), { status: 500 }),
    Object.assign(new Error('bad gateway'), { status: 502 }),
    Object.assign(new Error('unavailable'), { status: 503 }),
    new TypeError('Failed to fetch'),
  ])('clears a failed job while preserving its URL after %s so it can be reopened', async error => {
    window.history.replaceState({ retained: true }, '', '#ai-search?job=job-1&q=test')
    const historyLength = window.history.length
    vi.mocked(getAiSearchJob).mockRejectedValueOnce(error).mockResolvedValueOnce(job({ status: 'done', query: 'test' }))
    await useAiSearchStore.getState().loadJob('job-1', 'test')
    expect(useAiSearchStore.getState()).toMatchObject({ phase: 'idle', loadingJob: false, jobId: null, query: 'test' })
    expect(window.location.hash).toBe('#ai-search?job=job-1&q=test')
    expect(window.history.state).toEqual({ retained: true })
    expect(window.history.length).toBe(historyLength)
    await useAiSearchStore.getState().loadJob('job-1')
    expect(getAiSearchJob).toHaveBeenCalledTimes(2)
    expect(useAiSearchStore.getState()).toMatchObject({ jobId: 'job-1', summary: { status: 'done' } })
  })

  it.each(['old-job', 'new-job'])('only reuses the previous question for the same job: %s', async id => {
    useAiSearchStore.setState({ jobId: 'old-job', query: '上个任务的问题' })
    let resolve!: (response: AiSearchJobResponse) => void
    vi.mocked(getAiSearchJob).mockReturnValueOnce(new Promise(done => { resolve = done }))
    const request = useAiSearchStore.getState().loadJob(id)
    expect(useAiSearchStore.getState().query).toBe(id === 'old-job' ? '上个任务的问题' : '')
    resolve(job({ job_id: id, query: '任务自己的问题', status: 'done' }))
    await request
    expect(useAiSearchStore.getState().query).toBe('任务自己的问题')
  })

  it('validates the trimmed 1–200 character query before sending', async () => {
    await useAiSearchStore.getState().submit('  ')
    await useAiSearchStore.getState().submit('a'.repeat(201))
    expect(postAiSearch).not.toHaveBeenCalled()
    expect(useAiSearchStore.getState().requestError).toBe('请输入 1 到 200 字')
  })
  it('publishes cards, metadata and resumable hash on submit', async () => {
    await useAiSearchStore.getState().submit(' test ')
    expect(useAiSearchStore.getState()).toMatchObject({ phase: 'results', query: 'test', items, lexicalHits: 2, vectorHits: 3, cardOrder: ['a', 'b', 'c'] })
    // hash 同时带上问题：刷新保留地址栏，输入框才能立刻显示而不是空等响应。
    expect(window.location.hash).toBe('#ai-search?job=job-1&q=test')
  })
  it('exposes exhausted daily quota and does not poll', async () => {
    vi.mocked(postAiSearch).mockRejectedValue(new AiSearchAccessError('quota_exhausted'))
    await useAiSearchStore.getState().submit('test')
    expect(useAiSearchStore.getState().searchStatus).toMatchObject({ can_search: false, reason: 'quota_exhausted' })
    await vi.advanceTimersByTimeAsync(20_000)
    expect(getAiSearchJob).not.toHaveBeenCalled()
  })
  it('refreshes quota after creating a job and preserves a consumed failure turn', async () => {
    vi.mocked(getAiSearchStatus).mockResolvedValue({ enabled: true, logged_in: true, can_search: false, reason: 'quota_exhausted', user_day_limit: 2 })
    vi.mocked(postAiSearch).mockResolvedValue(post({ summary: { status: 'failed', reason: 'internal' } }))
    await useAiSearchStore.getState().submit('test')
    await vi.advanceTimersByTimeAsync(0)
    expect(getAiSearchStatus).toHaveBeenCalledOnce()
    expect(useAiSearchStore.getState()).toMatchObject({ searchStatus: { reason: 'quota_exhausted' }, summary: { status: 'failed', error: 'internal' } })
  })
  it('does not refresh quota for a retrieval with no job', async () => {
    vi.mocked(postAiSearch).mockResolvedValue(post({ items: [], job_id: null, summary: { status: 'none' } }))
    await useAiSearchStore.getState().submit('test')
    expect(getAiSearchStatus).not.toHaveBeenCalled()
  })
  it.each(['login_required', 'quota_exhausted'] as const)('preserves the query and rejects stale status after %s', async reason => {
    let resolve!: (status: Awaited<ReturnType<typeof getAiSearchStatus>>) => void
    vi.mocked(getAiSearchStatus).mockReturnValue(new Promise(r => { resolve = r }))
    const statusRequest = useAiSearchStore.getState().refreshStatus()
    vi.mocked(postAiSearch).mockRejectedValue(new AiSearchAccessError(reason))
    await useAiSearchStore.getState().submit(' 保留问题 ')
    resolve({ enabled: true, logged_in: true, can_search: true, reason: null, user_day_limit: 2 })
    await statusRequest
    expect(useAiSearchStore.getState()).toMatchObject({ phase: 'idle', query: '保留问题', jobId: null, items: [], requestError: null, searchStatus: { can_search: false, reason } })
  })
  it('preserves the unlimited hint configuration on a login-required response', async () => {
    useAiSearchStore.setState({ searchStatus: { enabled: true, logged_in: true, can_search: true, reason: null, user_day_limit: null } })
    vi.mocked(postAiSearch).mockRejectedValue(new AiSearchAccessError('login_required'))
    await useAiSearchStore.getState().submit('test')
    expect(useAiSearchStore.getState().searchStatus?.user_day_limit).toBeNull()
  })
  it('shows empty results without starting a poll', async () => {
    vi.mocked(postAiSearch).mockResolvedValue(post({ items: [], job_id: null, summary: { status: 'none' } }))
    await useAiSearchStore.getState().submit('test')
    expect(useAiSearchStore.getState().emptyResult).toBe(true)
    await vi.advanceTimersByTimeAsync(20_000)
    expect(getAiSearchJob).not.toHaveBeenCalled()
  })
  it.each(['queued', 'running'] as const)('keeps %s jobs with pending items out of the terminal empty state', async status => {
    vi.mocked(getAiSearchJob).mockResolvedValueOnce(job({ status, items: [] })).mockResolvedValue(job({ status: 'done' }))
    await useAiSearchStore.getState().loadJob('job-1')
    expect(useAiSearchStore.getState()).toMatchObject({ emptyResult: false, summary: { status } })
    await vi.advanceTimersByTimeAsync(10_000)
    expect(useAiSearchStore.getState()).toMatchObject({ emptyResult: false, items, summary: { status: 'done' } })
  })
  it('polls a newly queued job whose items have not arrived yet', async () => {
    vi.mocked(postAiSearch).mockResolvedValue(post({ items: [] }))
    await useAiSearchStore.getState().submit('test')
    expect(useAiSearchStore.getState().emptyResult).toBe(false)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(getAiSearchJob).toHaveBeenCalledOnce()
    expect(useAiSearchStore.getState().items).toEqual(items)
  })
  it('preserves provider unavailable reason and vector fallback without polling', async () => {
    vi.mocked(postAiSearch).mockResolvedValue(post({ job_id: null, vector_ok: false, summary: { status: 'unavailable', reason: 'quota' } }))
    await useAiSearchStore.getState().submit('test')
    expect(useAiSearchStore.getState()).toMatchObject({ vectorOk: false, summary: { status: 'unavailable', error: 'quota', reason: 'quota' } })
    await vi.advanceTimersByTimeAsync(20_000)
    expect(getAiSearchJob).not.toHaveBeenCalled()
  })
  it('polls every 10 seconds, pauses hidden, and refreshes immediately when visible', async () => {
    await useAiSearchStore.getState().submit('test')
    await vi.advanceTimersByTimeAsync(9_999)
    expect(getAiSearchJob).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(getAiSearchJob).toHaveBeenCalledTimes(1)
    visibility('hidden')
    await vi.advanceTimersByTimeAsync(30_000)
    expect(getAiSearchJob).toHaveBeenCalledTimes(1)
    visibility('visible')
    await vi.advanceTimersByTimeAsync(0)
    expect(getAiSearchJob).toHaveBeenCalledTimes(2)
  })
  it('stops on done and retains the API citation order for both items and cards', async () => {
    const orderedItems = [items[2], items[0], items[1]]
    vi.mocked(getAiSearchJob).mockResolvedValue(job({ status: 'done', items: orderedItems, cited_ids: ['c', 'a'], content_md: 'Answer [1][2]' }))
    await useAiSearchStore.getState().submit('test')
    await vi.advanceTimersByTimeAsync(30_000)
    expect(getAiSearchJob).toHaveBeenCalledTimes(1)
    expect(useAiSearchStore.getState()).toMatchObject({ items: orderedItems, cardOrder: ['c', 'a', 'b'], summary: { status: 'done', contentMd: 'Answer [1][2]' } })
  })
  it('restores a saved job with the same numbering and item order as polling', async () => {
    const orderedItems = [items[2], items[0], items[1]]
    vi.mocked(getAiSearchJob).mockResolvedValue(job({ status: 'done', items: orderedItems, cited_ids: ['c', 'a'], content_md: 'Answer [1][2]' }))
    await useAiSearchStore.getState().loadJob('job-1')
    const state = useAiSearchStore.getState()
    expect(state.cardOrder).toEqual(['c', 'a', 'b'])
    expect(state.items.map(item => item.id)).toEqual(state.cardOrder)
    expect(state.items.slice(0, state.summary.citedIds.length).map(item => item.id)).toEqual(state.summary.citedIds)
  })
  it.each(['failed', 'unavailable'] as const)('stops on %s', async status => {
    vi.mocked(getAiSearchJob).mockResolvedValue(job({ status }))
    await useAiSearchStore.getState().submit('test')
    await vi.advanceTimersByTimeAsync(30_000)
    expect(getAiSearchJob).toHaveBeenCalledTimes(1)
  })
  it('continues polling after a transient network failure', async () => {
    vi.mocked(getAiSearchJob).mockRejectedValueOnce(new Error('offline')).mockResolvedValue(job({ status: 'done' }))
    await useAiSearchStore.getState().submit('test')
    await vi.advanceTimersByTimeAsync(20_000)
    expect(getAiSearchJob).toHaveBeenCalledTimes(2)
    expect(useAiSearchStore.getState().summary.status).toBe('done')
  })
  it('marks a missing polled job failed and stops timers and visibility refreshes', async () => {
    vi.mocked(getAiSearchJob).mockRejectedValue(Object.assign(new Error('job_not_found'), { status: 404 }))
    await useAiSearchStore.getState().submit('test')
    await vi.advanceTimersByTimeAsync(10_000)
    expect(useAiSearchStore.getState()).toMatchObject({
      phase: 'results', items, unavailable: false, requestError: null,
      summary: { status: 'failed', error: 'job_not_found', contentMd: null },
    })
    await vi.advanceTimersByTimeAsync(30_000)
    visibility('hidden')
    visibility('visible')
    await vi.advanceTimersByTimeAsync(10_000)
    expect(getAiSearchJob).toHaveBeenCalledTimes(1)
  })
  it('stops polling when the feature is explicitly disabled', async () => {
    vi.mocked(getAiSearchJob).mockRejectedValue(Object.assign(new Error('ai_search_disabled'), { status: 404 }))
    await useAiSearchStore.getState().submit('test')
    await vi.advanceTimersByTimeAsync(30_000)
    expect(useAiSearchStore.getState()).toMatchObject({ unavailable: true, requestError: null })
    expect(getAiSearchJob).toHaveBeenCalledTimes(1)
  })
  it('restores a URL job and polls it to completion', async () => {
    await useAiSearchStore.getState().loadJob('job-1')
    expect(useAiSearchStore.getState()).toMatchObject({ phase: 'results', jobId: 'job-1', items })
    await vi.advanceTimersByTimeAsync(10_000)
    expect(getAiSearchJob).toHaveBeenCalledTimes(2)
  })
  it('restores the response query and retains it when subsequent responses omit it', async () => {
    useAiSearchStore.getState().setQuery('previous query')
    vi.mocked(getAiSearchJob).mockResolvedValueOnce(job({ query: 'restored query' })).mockResolvedValue(job())
    await useAiSearchStore.getState().loadJob('job-1')
    expect(useAiSearchStore.getState().query).toBe('restored query')
    await vi.advanceTimersByTimeAsync(10_000)
    expect(useAiSearchStore.getState().query).toBe('restored query')
    await useAiSearchStore.getState().loadJob('job-1')
    expect(useAiSearchStore.getState().query).toBe('restored query')
  })
  it('distinguishes missing jobs from the feature being disabled', async () => {
    vi.mocked(getAiSearchJob).mockRejectedValue(Object.assign(new Error('job_not_found'), { status: 404 }))
    await useAiSearchStore.getState().loadJob('missing')
    expect(useAiSearchStore.getState()).toMatchObject({ phase: 'results', jobId: 'missing', unavailable: false, requestError: null,
      summary: { status: 'failed', error: 'job_not_found' } })
    vi.mocked(postAiSearch).mockRejectedValue(Object.assign(new Error('ai_search_disabled'), { status: 404 }))
    await useAiSearchStore.getState().submit('test')
    expect(useAiSearchStore.getState().unavailable).toBe(true)
  })
  it.each([
    ['invalid_query', '请输入 1 到 200 字'],
    ['job_not_found', '这次的结果已失效，可以重新搜索。'],
    ['Rate limit exceeded: 10 per minute', '操作太频繁，请稍后再试'],
    ['Failed to fetch', '网络异常，请稍后再试'],
    ['NetworkError when attempting to fetch resource.', '网络异常，请稍后再试'],
    ['Load failed', '网络异常，请稍后再试'],
    ['HTTP 503 private upstream detail', '服务异常，请稍后再试'],
  ])('translates request error %s', async (message, expected) => {
    vi.mocked(postAiSearch).mockRejectedValue(new Error(message))
    await useAiSearchStore.getState().submit('test')
    expect(useAiSearchStore.getState()).toMatchObject({ phase: 'idle', unavailable: false, requestError: expected })
  })
  it.each(['Not Found', 'ai_search_disabled extra'])('does not treat an unrelated 404 as feature-disabled: %s', async message => {
    vi.mocked(postAiSearch).mockRejectedValue(Object.assign(new Error(message), { status: 404 }))
    await useAiSearchStore.getState().submit('test')
    expect(useAiSearchStore.getState()).toMatchObject({ unavailable: false, requestError: '服务异常，请稍后再试' })
  })
  it('ignores old submit results after a new search', async () => {
    let resolve!: (response: AiSearchPostResponse) => void
    vi.mocked(postAiSearch).mockReturnValueOnce(new Promise(r => { resolve = r }))
    const stale = useAiSearchStore.getState().submit('old')
    await useAiSearchStore.getState().submit('new')
    resolve(post({ job_id: 'old-job', query: 'old' }))
    await stale
    expect(useAiSearchStore.getState().jobId).toBe('job-1')
    expect(window.location.hash).not.toContain('old-job')
  })
  it('ignores in-flight responses after leaving and removes visibility listeners', async () => {
    let resolve!: (response: AiSearchJobResponse) => void
    vi.mocked(getAiSearchJob).mockReturnValueOnce(new Promise(r => { resolve = r }))
    await useAiSearchStore.getState().submit('test')
    await vi.advanceTimersByTimeAsync(10_000)
    useAiSearchStore.getState().stopPolling()
    resolve(job({ status: 'done' }))
    await vi.advanceTimersByTimeAsync(30_000)
    visibility('hidden'); visibility('visible')
    expect(getAiSearchJob).toHaveBeenCalledTimes(1)
    expect(useAiSearchStore.getState().summary.status).toBe('queued')
  })

  it('clears obsolete job URLs without emitting navigation for no-job results', async () => {
    window.history.replaceState(null, '', '#ai-search?job=old')
    const replace = vi.spyOn(window.history, 'replaceState')
    vi.mocked(postAiSearch).mockResolvedValue(post({ job_id: null, summary: { status: 'unavailable', reason: 'quota' } }))
    await useAiSearchStore.getState().submit('test')
    expect(replace).toHaveBeenCalledWith(window.history.state, '', '#ai-search')
    expect(useAiSearchStore.getState().summary.status).toBe('unavailable')
    replace.mockRestore()
  })
  it('keeps known feature-disabled state when resetting the page', () => {
    useAiSearchStore.setState({ unavailable: true })
    useAiSearchStore.getState().reset()
    expect(useAiSearchStore.getState().unavailable).toBe(true)
  })
  it('ignores a pending submit after leaving without rewriting the route', async () => {
    let resolve!: (response: AiSearchPostResponse) => void
    vi.mocked(postAiSearch).mockReturnValueOnce(new Promise(r => { resolve = r }))
    const request = useAiSearchStore.getState().submit('test')
    useAiSearchStore.getState().stopPolling()
    window.location.hash = 'settings'
    resolve(post())
    await request
    expect(window.location.hash).toBe('#settings')
    expect(useAiSearchStore.getState().jobId).toBeNull()
  })
  it('does not let an old poll overwrite a new query or create an extra timer', async () => {
    let resolve!: (response: AiSearchJobResponse) => void
    vi.mocked(getAiSearchJob).mockReturnValueOnce(new Promise(r => { resolve = r }))
    await useAiSearchStore.getState().submit('old')
    await vi.advanceTimersByTimeAsync(10_000)
    vi.mocked(postAiSearch).mockResolvedValue(post({ job_id: 'new-job', query: 'new' }))
    await useAiSearchStore.getState().submit('new')
    resolve(job({ status: 'done', content_md: 'stale' }))
    await vi.advanceTimersByTimeAsync(0)
    expect(useAiSearchStore.getState()).toMatchObject({ query: 'new', jobId: 'new-job', summary: { status: 'queued', contentMd: null } })
    await vi.advanceTimersByTimeAsync(10_000)
    expect(getAiSearchJob).toHaveBeenCalledTimes(2)
    expect(getAiSearchJob).toHaveBeenLastCalledWith('new-job')
  })
  it('clears highlights 1.5 seconds after the latest selection', async () => {
    useAiSearchStore.getState().highlight('a')
    await vi.advanceTimersByTimeAsync(1_000)
    useAiSearchStore.getState().highlight('b')
    await vi.advanceTimersByTimeAsync(1_000)
    expect(useAiSearchStore.getState().highlightItemId).toBe('b')
    await vi.advanceTimersByTimeAsync(500)
    expect(useAiSearchStore.getState().highlightItemId).toBeNull()
  })
  it('uses the generated token duration supplied by the citation chip', async () => {
    useAiSearchStore.getState().highlight('a', 2200)
    await vi.advanceTimersByTimeAsync(2199)
    expect(useAiSearchStore.getState().highlightItemId).toBe('a')
    await vi.advanceTimersByTimeAsync(1)
    expect(useAiSearchStore.getState().highlightItemId).toBeNull()
  })
  it('restores the provider reason on an unavailable saved job without a numeric limit', async () => {
    vi.mocked(getAiSearchJob).mockResolvedValue(job({ status: 'unavailable', error: 'quota' }))
    await useAiSearchStore.getState().loadJob('job-1')
    expect(useAiSearchStore.getState().summary).toMatchObject({ status: 'unavailable', reason: 'quota' })
  })

  it('loadJob 先用地址栏里的问题填输入框，响应到了以服务端为准', async () => {
    // 刷新保留地址栏，所以问题能立刻显示，不必空等一秒（用户 2026-09-22 反馈）。
    vi.mocked(getAiSearchJob).mockImplementation(async () => {
      expect(useAiSearchStore.getState().query).toBe('来自地址栏的问题')
      expect(useAiSearchStore.getState().loadingJob).toBe(true)
      return { job_id: 'job-1', query: '服务端记录的问题', status: 'done', items: [],
        content_md: '正文', cited_ids: [] } as never
    })
    await useAiSearchStore.getState().loadJob('job-1', '来自地址栏的问题')
    expect(useAiSearchStore.getState().query).toBe('服务端记录的问题')
    expect(useAiSearchStore.getState().loadingJob).toBe(false)
  })
})
