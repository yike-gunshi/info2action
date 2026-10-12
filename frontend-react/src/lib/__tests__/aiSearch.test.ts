import { afterEach, describe, expect, it, vi } from 'vitest'
import { AiSearchAccessError, getAiSearchStatus, getAiSearchHistory, getAiSearchJob, postAiSearch } from '../api/aiSearch'
import { useAuthStore } from '../../store/authStore'

afterEach(() => { vi.unstubAllGlobals() })
describe('aiSearch API client', () => {
  it('preserves the retrieval-in-progress 409 as a distinct access reason', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: 'retrieval_in_progress' }), { status: 409 })))
    const request = postAiSearch('query')
    await expect(request).rejects.toMatchObject({ name: 'AiSearchAccessError', reason: 'retrieval_in_progress', status: 409 })
    await expect(request).rejects.toBeInstanceOf(AiSearchAccessError)
  })

  it.each([
    { job_id: 'v2-job', query: 'test', summary: { status: 'retrieving' } },
    { job_id: 'v1-job', query: 'test', query_lexical: 'test', window_days: 90, lexical_hits: 1, vector_hits: 0, vector_ok: false,
      items: [{ id: 'one', title: 'One', platform: 'rss', fetched_at: '' }], summary: { status: 'queued', position: 2 } },
  ])('accepts both minimal v2 and legacy POST contracts: $job_id', async response => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(response))))
    await expect(postAiSearch('test')).resolves.toEqual(response)
  })

  it('returns retrieval progress and vector_ok from GET', async () => {
    const response = { job_id: 'v2-job', status: 'retrieving', stage: 'searching', keywords: ['Muse', 'Cue'], vector_ok: false, items: [] }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(response))))
    await expect(getAiSearchJob('v2-job')).resolves.toEqual(response)
  })
  it('preserves the login-required 401 reason without starting an anonymous search', async () => {
    useAuthStore.setState({ user: null })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: 'login_required' }), { status: 401 })))
    await expect(postAiSearch('query')).rejects.toMatchObject({ name: 'AiSearchAccessError', reason: 'login_required', status: 401 })
  })
  it.each([
    { enabled: false, logged_in: false, can_search: false, reason: 'disabled', user_day_limit: 2 },
    { enabled: true, logged_in: false, can_search: false, reason: 'login_required', user_day_limit: 2 },
    { enabled: true, logged_in: true, can_search: false, reason: 'quota_exhausted', user_day_limit: 2 },
    { enabled: true, logged_in: true, can_search: true, reason: null, user_day_limit: null },
  ])('fetches public status without usage counts: $reason', async response => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response)))
    vi.stubGlobal('fetch', fetchMock)
    await expect(getAiSearchStatus()).resolves.toEqual(response)
    expect(fetchMock).toHaveBeenCalledWith('/api/ai-search/status', expect.objectContaining({ credentials: 'same-origin' }))
    expect(response).not.toHaveProperty('used')
    expect(response).not.toHaveProperty('remaining')
  })
  it('preserves the quota-exhausted 429 reason on search', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: 'quota_exhausted' }), { status: 429 })))
    const request = postAiSearch('query')
    await expect(request).rejects.toMatchObject({ name: 'AiSearchAccessError', reason: 'quota_exhausted', status: 429 })
    await expect(request).rejects.toBeInstanceOf(AiSearchAccessError)
  })
  it('keeps rate throttling distinct from the daily account quota', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: 'Rate limit exceeded' }), { status: 429 })))
    await expect(postAiSearch('query')).rejects.toMatchObject({ message: 'Rate limit exceeded', status: 429 })
  })
  it('uses apiFetch JSON requests and safely encodes job IDs', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ job_id: 'job-1' })))
    vi.stubGlobal('fetch', fetchMock)
    await postAiSearch('test')
    expect(fetchMock).toHaveBeenLastCalledWith('/api/ai-search', expect.objectContaining({ method: 'POST', body: '{"q":"test"}', credentials: 'same-origin' }))
    await getAiSearchJob('id/path')
    expect(fetchMock).toHaveBeenLastCalledWith('/api/ai-search/id%2Fpath', expect.anything())
  })
  it('retains status and missing-job error text for the page', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: 'job_not_found' }), { status: 404 })))
    await expect(getAiSearchJob('missing')).rejects.toMatchObject({ status: 404, message: 'job_not_found' })
  })
  it('requests history with the supplied CSS limit and cancellation signal', async () => {
    const response = { anonymous: false, items: [], total: 0 }
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response)))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()
    await expect(getAiSearchHistory(7, controller.signal)).resolves.toEqual(response)
    expect(fetchMock).toHaveBeenCalledWith('/api/ai-search/history?limit=7', expect.objectContaining({
      credentials: 'same-origin', signal: controller.signal,
    }))
    expect(fetchMock.mock.calls[0][1]).not.toHaveProperty('body')
  })
  it('accepts the anonymous history response without a total field', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ anonymous: true, items: [] }))))
    await expect(getAiSearchHistory(4)).resolves.toEqual({ anonymous: true, items: [] })
  })
})
