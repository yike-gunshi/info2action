import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { AiSearchPage } from '../AiSearchPage'
import { useAuthStore } from '../../../store/authStore'
import { getAiSearchStatus } from '../../../lib/api/aiSearch'
import { useAiSearchStore } from '../../../store/aiSearchStore'
import { apiFetch, tryRefresh } from '../../../lib/api/_client'

vi.mock('../../../lib/api/_client', () => ({ apiFetch: vi.fn(), tryRefresh: vi.fn() }))
vi.mock('../../../lib/api/aiSearch', async importOriginal => ({
  ...await importOriginal<typeof import('../../../lib/api/aiSearch')>(), getAiSearchStatus: vi.fn(),
}))
vi.mock('../../layout/TopBar', () => ({ TopBar: () => <header>TopBar</header> }))

const skeletonQuery = '我想知道有哪些 prompt 或 skill 能一键将一个代码仓库转换为知识（codex/Claude code 可用），给我具体的实现说明或仓库链接'
// 骨架原句实际为 75 字；同时覆盖原句和扩展到 93 字的长问题。
const longQuery = `${skeletonQuery}，并说明适用范围、准备条件与使用步骤`
const usableStatus = { enabled: true, logged_in: true, can_search: true, reason: null, user_day_limit: 2 }

beforeEach(() => {
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
  vi.mocked(apiFetch).mockReset()
  vi.mocked(tryRefresh).mockReset()
  document.documentElement.style.removeProperty('--ai-hist-max')
  useAiSearchStore.getState().reset()
  useAiSearchStore.getState().invalidateStatus()
  useAiSearchStore.setState({ unavailable: false })
  useAuthStore.setState({ user: { id: 'u', username: 'u', email: 'u@example.com', role: 'user' }, isChecked: true, isLoading: false })
  vi.mocked(getAiSearchStatus).mockReset().mockImplementation(() => new Promise(() => {}))
  window.history.replaceState(null, '', '/#ai-search')
})
afterEach(() => { document.documentElement.style.removeProperty('--ai-hist-max'); cleanup(); useAiSearchStore.getState().reset(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('AiSearchPage route lifecycle', () => {
  it.each([500, 503, 'offline'])('retries the same history row after opening it fails: %s', async failure => {
    document.documentElement.style.setProperty('--ai-hist-max', '3')
    vi.mocked(getAiSearchStatus).mockResolvedValue(usableStatus)
    let attempts = 0
    vi.mocked(apiFetch).mockImplementation(async url => {
      if (url.includes('/history')) return { anonymous: false, total: 1, items: [
        { job_id: 'saved', query: '同一条记录', status: 'done', created_at: '2026-10-10T01:00:00Z' },
      ] }
      if (++attempts === 1) throw Object.assign(new Error(failure === 'offline' ? 'Failed to fetch' : 'internal'), { status: failure })
      return { job_id: 'saved', query: '同一条记录', status: 'done', items: [{ id: 'one', title: '原文', platform: 'rss', fetched_at: '' }], content_md: '恢复的正文' }
    })
    render(<AiSearchPage />)
    fireEvent.click(await screen.findByRole('link', { name: /同一条记录/ }))
    await screen.findByRole('alert')
    expect(window.location.hash).toBe(`#ai-search?job=saved&q=${encodeURIComponent('同一条记录')}`)
    expect(useAiSearchStore.getState().jobId).toBeNull()
    fireEvent.click(await screen.findByRole('link', { name: /同一条记录/ }))
    expect(await screen.findByText('恢复的正文')).toBeInTheDocument()
    expect(attempts).toBe(2)
  })

  it('preserves a share URL without q after a transient failure and restores the job on remount', async () => {
    document.documentElement.style.setProperty('--ai-hist-max', '3')
    window.history.replaceState(null, '', '#ai-search?job=shared')
    let attempts = 0
    vi.mocked(apiFetch).mockImplementation(async url => {
      if (url.includes('/history')) return { anonymous: false, total: 0, items: [] }
      if (++attempts === 1) throw Object.assign(new Error('bad gateway'), { status: 502 })
      return { job_id: 'shared', query: '分享任务的问题', status: 'done', items: [
        { id: 'one', title: '原文', platform: 'rss', fetched_at: '' },
      ], content_md: '刷新找回的正文' }
    })
    const page = render(<AiSearchPage />)
    await screen.findByRole('alert')
    expect(window.location.hash).toBe('#ai-search?job=shared')
    expect(useAiSearchStore.getState().jobId).toBeNull()
    page.unmount()
    render(<AiSearchPage />)
    expect(await screen.findByText('刷新找回的正文')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('分享任务的问题')
    expect(attempts).toBe(2)
  })
  it('waits for the initial auth check before fetching status and keeps anonymous input editable', async () => {
    useAuthStore.setState({ user: null, isChecked: false, isLoading: true })
    render(<AiSearchPage />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '预填问题' } })
    expect(screen.getByRole('textbox')).toBeEnabled()
    expect(screen.getByRole('button', { name: 'AI 搜索' })).toBeDisabled()
    expect(getAiSearchStatus).not.toHaveBeenCalled()
    vi.mocked(getAiSearchStatus).mockResolvedValue({ enabled: true, logged_in: false, can_search: false, reason: 'login_required', user_day_limit: 2 })
    act(() => useAuthStore.setState({ isChecked: true, isLoading: false }))
    expect(await screen.findByText(/AI 搜索需要登录，每人每天可用 2 次/)).toBeInTheDocument()
    await waitFor(() => expect(getAiSearchStatus).toHaveBeenCalledOnce())
    expect(screen.getByRole('link', { name: '登录' })).toHaveAttribute('href', '#login')
    expect(screen.queryByText(/登录后可以查看自己的搜索记录/)).not.toBeInTheDocument()
    fireEvent.submit(screen.getByRole('textbox').closest('form')!)
    expect(vi.mocked(apiFetch).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
    expect(screen.getByRole('textbox')).toHaveValue('预填问题')
  })

  it('keeps anonymous submission blocked when status fails', async () => {
    useAuthStore.setState({ user: null })
    vi.mocked(getAiSearchStatus).mockRejectedValue(new Error('offline'))
    render(<AiSearchPage />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '仍然可以输入' } })
    await waitFor(() => expect(useAiSearchStore.getState().statusLoading).toBe(false))
    expect(screen.getByRole('button', { name: 'AI 搜索' })).toBeDisabled()
    expect(screen.getByRole('textbox')).toBeEnabled()
    fireEvent.submit(screen.getByRole('textbox').closest('form')!)
    expect(vi.mocked(apiFetch).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
  })

  it.each([2, null])('uses the configured login hint without remaining counts: %s', async limit => {
    useAuthStore.setState({ user: null })
    vi.mocked(getAiSearchStatus).mockResolvedValue({ enabled: true, logged_in: false, can_search: false, reason: 'login_required', user_day_limit: limit })
    render(<AiSearchPage />)
    await waitFor(() => expect(useAiSearchStore.getState().searchStatus?.user_day_limit).toBe(limit))
    const hint = document.querySelector('#ai-search-hint')!
    expect(hint).toHaveTextContent(limit === null ? 'AI 搜索需要登录 · 登录' : 'AI 搜索需要登录，每人每天可用 2 次 · 登录')
    expect(hint.textContent).not.toMatch(/剩余|今天还|已用/)
    expect(screen.getAllByRole('link', { name: '登录' })).toHaveLength(1)
  })

  it('shows the existing disabled view from a successful status response', async () => {
    vi.mocked(getAiSearchStatus).mockResolvedValue({ enabled: false, logged_in: true, can_search: false, reason: 'disabled', user_day_limit: 2 })
    render(<AiSearchPage />)
    expect(await screen.findByText('AI 搜索暂未开放')).toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('keeps exhausted history accessible and applies quota to a prefilled failed result', async () => {
    document.documentElement.style.setProperty('--ai-hist-max', '3')
    vi.mocked(getAiSearchStatus).mockResolvedValue({ enabled: true, logged_in: true, can_search: false, reason: 'quota_exhausted', user_day_limit: 2 })
    vi.mocked(apiFetch).mockImplementation(async url => url.includes('/history')
      ? { anonymous: false, total: 1, items: [{ job_id: 'saved', query: '历史失败', status: 'failed', created_at: '2026-10-10T01:00:00Z' }] }
      : { job_id: 'saved', query: '历史失败', status: 'failed', error: 'timeout', items: [] })
    render(<AiSearchPage />)
    // 失败的记录只显示「未完成」、不可点；失败结果仍可凭链接打开
    const history = (await screen.findByText('历史失败')).closest('.hrow')!
    expect(history).toHaveAttribute('aria-disabled', 'true')
    expect(screen.queryByRole('link', { name: /历史失败/ })).not.toBeInTheDocument()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '输入的新问题' } })
    expect(screen.getByRole('button', { name: 'AI 搜索' })).toBeDisabled()
    expect(screen.getByRole('status')).toHaveTextContent('当前额度已用完，明天 0 点恢复')
    act(() => { window.location.hash = '#ai-search?job=saved' })
    await screen.findByText('这次总结用时太长，没能完成，可以再试一次。', { selector: '.ai-summary-state-title' })
    expect(screen.getByRole('textbox')).toHaveValue('历史失败')
    expect(screen.getByRole('textbox')).toBeEnabled()
    expect(screen.getByRole('button', { name: 'AI 搜索' })).toBeDisabled()
    expect(document.querySelector('#ai-search-hint')).toHaveTextContent('当前额度已用完，明天 0 点恢复')
    expect(vi.mocked(apiFetch).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
    document.documentElement.style.removeProperty('--ai-hist-max')
  })

  it.each(['login_required', 'quota_exhausted'] as const)('updates controls on a submitted %s without cards or losing the question', async reason => {
    vi.mocked(getAiSearchStatus).mockResolvedValue(usableStatus)
    vi.mocked(apiFetch).mockImplementation(async (_url, options) => {
      if (options?.method === 'POST') throw Object.assign(new Error(reason), { status: reason === 'login_required' ? 401 : 429 })
      return { anonymous: false, total: 0, items: [] }
    })
    render(<AiSearchPage />)
    await waitFor(() => expect(useAiSearchStore.getState().statusLoading).toBe(false))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '保留失败问题' } })
    fireEvent.click(screen.getByRole('button', { name: 'AI 搜索' }))
    await waitFor(() => expect(useAiSearchStore.getState().searchStatus?.reason).toBe(reason))
    expect(screen.getByRole('textbox')).toHaveValue('保留失败问题')
    expect(screen.getByRole('textbox')).toBeEnabled()
    expect(screen.getByRole('button', { name: 'AI 搜索' })).toBeDisabled()
    expect(screen.getByRole('status')).toHaveTextContent(reason === 'login_required' ? 'AI 搜索需要登录' : '当前额度已用完，明天 0 点恢复')
    expect(screen.queryByLabelText('引用内容')).not.toBeInTheDocument()
  })

  it('uses authoritative logged-in status for a legacy administrator without an authStore user', async () => {
    vi.mocked(getAiSearchStatus).mockResolvedValue(usableStatus)
    useAuthStore.setState({ user: null })
    render(<AiSearchPage />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '管理员问题' } })
    await waitFor(() => expect(screen.getByRole('button', { name: 'AI 搜索' })).toBeEnabled())
    expect(screen.queryByRole('link', { name: '登录' })).not.toBeInTheDocument()
  })

  it('keeps administrators usable when an exhausted status was retained', async () => {
    vi.mocked(getAiSearchStatus).mockResolvedValue(usableStatus)
    useAuthStore.setState({ user: { id: 'admin', username: 'admin', email: '', role: 'admin' } })
    render(<AiSearchPage />)
    await waitFor(() => expect(useAiSearchStore.getState().statusLoading).toBe(false))
    act(() => useAiSearchStore.setState({ searchStatus: { enabled: true, logged_in: true, can_search: false, reason: 'quota_exhausted', user_day_limit: 2 } }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '管理员问题' } })
    expect(screen.getByRole('button', { name: 'AI 搜索' })).toBeEnabled()
    expect(screen.queryByText(/当前额度已用完/)).not.toBeInTheDocument()
  })

  it('ignores a previous identity status and refreshes on sign-in', async () => {
    let resolveOld!: (response: Awaited<ReturnType<typeof getAiSearchStatus>>) => void
    useAuthStore.setState({ user: null })
    vi.mocked(getAiSearchStatus).mockResolvedValue(usableStatus).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    render(<AiSearchPage />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '保留草稿' } })
    act(() => useAuthStore.setState({ user: { id: 'new-user', username: 'new', email: '', role: 'user' } }))
    await waitFor(() => expect(getAiSearchStatus).toHaveBeenCalledTimes(2))
    await act(async () => resolveOld({ enabled: true, logged_in: false, can_search: false, reason: 'login_required', user_day_limit: 2 }))
    expect(screen.getByRole('button', { name: 'AI 搜索' })).toBeEnabled()
    expect(screen.getByRole('textbox')).toHaveValue('保留草稿')
    expect(screen.queryByRole('link', { name: '登录' })).not.toBeInTheDocument()
  })

  it('refreshes an expired access cookie once when public status contradicts a known session', async () => {
    vi.mocked(tryRefresh).mockResolvedValue('ok')
    vi.mocked(getAiSearchStatus)
      .mockResolvedValueOnce({ enabled: true, logged_in: false, can_search: false, reason: 'login_required', user_day_limit: 2 })
      .mockResolvedValueOnce({ enabled: true, logged_in: true, can_search: true, reason: null, user_day_limit: 2 })
    render(<AiSearchPage />)
    await waitFor(() => expect(useAiSearchStore.getState().searchStatus?.logged_in).toBe(true))
    expect(tryRefresh).toHaveBeenCalledOnce()
    expect(getAiSearchStatus).toHaveBeenCalledTimes(2)
  })

  it.each(['expired', 'unavailable'] as const)('handles public-status reconciliation when refresh is %s', async outcome => {
    vi.mocked(tryRefresh).mockResolvedValue(outcome)
    vi.mocked(getAiSearchStatus).mockResolvedValue({ enabled: true, logged_in: false, can_search: false, reason: 'login_required', user_day_limit: 2 })
    render(<AiSearchPage />)
    await waitFor(() => expect(useAiSearchStore.getState().searchStatus?.reason).toBe('login_required'))
    expect(useAuthStore.getState().user === null).toBe(outcome === 'expired')
    expect(tryRefresh).toHaveBeenCalledOnce()
    expect(screen.getByRole('textbox')).toBeEnabled()
    expect(screen.getByRole('button', { name: 'AI 搜索' })).toBeDisabled()
  })

  it('renders the input, submit button and hint in the idle state', () => {
    render(<AiSearchPage />)
    expect(screen.getByRole('textbox', { name: 'AI 搜索' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'AI 搜索' })).toBeInTheDocument()
    expect(document.querySelector('#ai-search-hint')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '换一个问题' })).not.toBeInTheDocument()
  })

  it.each([skeletonQuery, longQuery])('renders the full submitted question as the only h1: %s', query => {
    vi.mocked(apiFetch).mockReturnValue(new Promise(() => {}))
    render(<AiSearchPage />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: query } })
    fireEvent.click(screen.getByRole('button', { name: 'AI 搜索' }))
    expect(useAiSearchStore.getState().phase).toBe('submitting')
    expect(Array.from(longQuery)).toHaveLength(93)
    const title = screen.getByRole('heading', { level: 1 })
    expect(title.textContent).toBe(query)
    expect(title.textContent).not.toMatch(/…|\.\.\./)
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /AI 搜索/ })).not.toBeInTheDocument()
    expect(document.querySelector('#ai-search-hint')).not.toBeInTheDocument()
  })

  it('lets a new question invalidate an in-flight submission without restoring its results or URL', async () => {
    let resolve!: (value: unknown) => void
    const response = new Promise(r => { resolve = r })
    vi.mocked(apiFetch).mockReturnValue(response)
    render(<AiSearchPage />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: longQuery } })
    fireEvent.click(screen.getByRole('button', { name: 'AI 搜索' }))
    act(() => {
      window.history.replaceState(null, '', '/#ai-search')
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect(screen.getByRole('textbox')).toHaveFocus()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '新问题' } })
    await act(async () => {
      resolve({ job_id: 'stale-job', query: longQuery, items: [], summary: { status: 'none' } })
      await response
    })
    expect(useAiSearchStore.getState()).toMatchObject({ phase: 'idle', query: '新问题', jobId: null, items: [] })
    expect(window.location.hash).toBe('#ai-search')
    expect(screen.getByRole('textbox')).toHaveValue('新问题')
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument()
  })

  it('shows the URL question as a title immediately while a saved job loads', () => {
    window.history.replaceState(null, '', `/#ai-search?job=saved-job&q=${encodeURIComponent(longQuery)}`)
    vi.mocked(apiFetch).mockReturnValue(new Promise(() => {}))
    render(<AiSearchPage />)
    expect(useAiSearchStore.getState().loadingJob).toBe(true)
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe(longQuery)
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(document.querySelector('.ai-summary-skeleton')).toBeInTheDocument()
    act(() => {
      window.history.replaceState(null, '', '/#ai-search')
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect(useAiSearchStore.getState()).toMatchObject({ phase: 'idle', jobId: null, loadingJob: false })
    expect(window.location.hash).toBe('#ai-search')
    expect(screen.getByRole('textbox')).toHaveFocus()
  })

  it.each([
    { status: 'done', error: null },
    { status: 'running', error: null },
    { status: 'queued', error: null },
  ] as const)('keeps the h1 for $status and resets on navigation to the bare AI route', ({ status, error }) => {
    render(<AiSearchPage />)
    act(() => {
      useAiSearchStore.setState({ phase: 'results', jobId: 'saved-job', query: longQuery,
        items: [{ id: 'one', title: '来源一', platform: 'rss', fetched_at: '' }],
        summary: { ...useAiSearchStore.getState().summary, status, error, contentMd: status === 'done' ? '# 结论\n\n正文' : null } })
      window.history.replaceState(null, '', `/#ai-search?job=saved-job&q=${encodeURIComponent(longQuery)}`)
    })
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe(longQuery)
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'AI 搜索' })).not.toBeInTheDocument()
    expect(document.querySelector('#ai-search-hint')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '换一个问题' })).not.toBeInTheDocument()
    expect(document.querySelector('.ai-search-qhead')).toBeNull()
    if (status === 'done') expect(screen.getByText(/基于/, { selector: '.ai-summary-meta' })).toHaveClass('ai-summary-meta')
    const reset = vi.spyOn(useAiSearchStore.getState(), 'reset')
    act(() => {
      window.history.replaceState(null, '', '/#ai-search')
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect(reset).toHaveBeenCalledOnce()
    expect(useAiSearchStore.getState()).toMatchObject({ phase: 'idle', query: '', jobId: null, items: [], summary: { status: 'none', contentMd: null } })
    expect(window.location.hash).toBe('#ai-search')
    const input = screen.getByRole('textbox', { name: 'AI 搜索' })
    expect(input).toBeEnabled()
    expect(input).toHaveFocus()
    expect(input).toHaveValue('')
    fireEvent.change(input, { target: { value: '下一个问题' } })
    expect(input).toHaveValue('下一个问题')
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument()
  })

  it.each([
    { status: 'failed', error: 'hard_timeout', emptyResult: false, message: '这次总结用时太长，没能完成，可以再试一次。', disabled: false, cards: true },
    { status: 'unavailable', error: 'quota', emptyResult: false, message: 'AI 总结服务暂时不可用，请稍后再来。', disabled: false, cards: true },
    { status: 'failed', error: 'job_not_found', emptyResult: false, message: '这次的结果已失效，可以重新搜索。', disabled: false, cards: false },
    { status: 'none', error: null, emptyResult: true, message: '站内近 90 天没有找到相关内容，换个说法试试。', disabled: false, cards: false },
  ] as const)('uses an editable prefilled field for $status / $error / empty=$emptyResult', ({ status, error, emptyResult, message, disabled, cards }) => {
    render(<AiSearchPage />)
    act(() => useAiSearchStore.setState({ phase: 'results', query: longQuery, emptyResult,
      // Retained items must stay hidden after expiry or an empty result.
      items: [{ id: 'one', title: '来源一', platform: 'rss', fetched_at: '' }],
      summary: { ...useAiSearchStore.getState().summary, status, error } }))
    const input = screen.getByRole('textbox', { name: 'AI 搜索' })
    expect(input).toBeEnabled()
    expect(input).toHaveValue(longQuery)
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument()
    expect(screen.getByText(message, { selector: '.ai-summary-state-title' })).toBeInTheDocument()
    const button = screen.getByRole('button', { name: 'AI 搜索' })
    if (disabled) expect(button).toBeDisabled()
    else expect(button).toBeEnabled()
    if (cards) expect(screen.getByLabelText('引用内容')).toBeInTheDocument()
    else expect(screen.queryByLabelText('引用内容')).not.toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: '目录' })).not.toBeInTheDocument()
    expect(document.querySelector('.ai-search-form')).toHaveAttribute('data-glow', 'false')
    fireEvent.change(input, { target: { value: '修改后的问题' } })
    expect(input).toHaveValue('修改后的问题')
    if (disabled) {
      expect(button).toBeDisabled()
      fireEvent.submit(input.closest('form')!)
      expect(vi.mocked(apiFetch).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
    }
  })

  it('submits the edited failure query through the idle submit flow and updates the job hash', async () => {
    let resolve!: (value: unknown) => void
    const response = new Promise(r => { resolve = r })
    vi.mocked(apiFetch).mockImplementation(url => url === '/api/ai-search' ? response : Promise.resolve({ anonymous: true, items: [] }))
    render(<AiSearchPage />)
    act(() => useAiSearchStore.setState({ phase: 'results', jobId: 'failed-job', query: longQuery,
      summary: { ...useAiSearchStore.getState().summary, status: 'failed', error: 'hard_timeout' } }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '  修改后的问题  ' } })
    fireEvent.click(screen.getByRole('button', { name: 'AI 搜索' }))
    expect(apiFetch).toHaveBeenCalledWith('/api/ai-search', { method: 'POST', body: JSON.stringify({ q: '修改后的问题' }) })
    expect(useAiSearchStore.getState().phase).toBe('submitting')
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('修改后的问题')
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    await act(async () => {
      resolve({ job_id: 'new-job', query: '修改后的问题', items: [{ id: 'one', title: '来源一', platform: 'rss', fetched_at: '' }],
        vector_ok: true, summary: { status: 'queued' } })
      await response
    })
    await waitFor(() => expect(window.location.hash).toBe(`#ai-search?job=new-job&q=${encodeURIComponent('修改后的问题')}`))
    expect(document.querySelector('.ai-summary-skeleton')).toBeInTheDocument()
  })

  it('restores the URL query in the expired-job search field', async () => {
    window.history.replaceState(null, '', `/#ai-search?job=missing-job&q=${encodeURIComponent(longQuery)}`)
    vi.mocked(apiFetch).mockRejectedValue(Object.assign(new Error('job_not_found'), { status: 404 }))
    render(<AiSearchPage />)
    expect(await screen.findByRole('textbox')).toHaveValue(longQuery)
    expect(screen.getByText('这次的结果已失效，可以重新搜索。', { selector: '.ai-summary-state-title' })).toBeInTheDocument()
    expect(screen.queryByLabelText('引用内容')).not.toBeInTheDocument()
  })

  it('clears a retained ordinary-search query when the AI entry is opened from a result', () => {
    render(<AiSearchPage />)
    act(() => useAiSearchStore.setState({ phase: 'results', query: longQuery,
      summary: { ...useAiSearchStore.getState().summary, status: 'failed', error: 'hard_timeout' } }))
    act(() => {
      window.history.replaceState(null, '', '/#ai-search?q=retained-site-query')
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect(window.location.hash).toBe('#ai-search')
    expect(useAiSearchStore.getState()).toMatchObject({ phase: 'idle', query: '', jobId: null, items: [] })
    expect(screen.getByRole('textbox')).toHaveValue('')
    expect(screen.getByRole('textbox')).toHaveFocus()
  })

  it('prefills q without submitting and updates when another AI entry is clicked', () => {
    window.history.replaceState(null, '', '/#ai-search?q=seedance%202.5')
    render(<AiSearchPage />)
    expect(screen.getByRole('textbox')).toHaveValue('seedance 2.5')
    expect(apiFetch).not.toHaveBeenCalled()
    act(() => {
      window.history.replaceState(null, '', '/#ai-search?q=Claude')
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect(screen.getByRole('textbox')).toHaveValue('Claude')
    expect(apiFetch).not.toHaveBeenCalled()
  })
  it('restores a job from the URL, displays results, and stops polling on unmount', async () => {
    window.history.replaceState(null, '', '/#ai-search?job=job-1')
    vi.mocked(apiFetch).mockResolvedValue({ job_id: 'job-1', status: 'running', items: [{ id: 'one', title: 'One', platform: 'rss', fetched_at: '' }] })
    const stop = vi.spyOn(useAiSearchStore.getState(), 'stopPolling')
    const { unmount } = render(<AiSearchPage />)
    expect(await screen.findByRole('region', { name: 'AI 总结' })).toBeInTheDocument()
    expect(apiFetch).toHaveBeenCalledWith('/api/ai-search/job-1', undefined)
    unmount()
    expect(stop).toHaveBeenCalled()
  })
  it('shows the feature-disabled 404 response and a return link', async () => {
    vi.mocked(apiFetch).mockRejectedValue(Object.assign(new Error('ai_search_disabled'), { status: 404 }))
    render(<AiSearchPage />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'test' } })
    fireEvent.click(screen.getByRole('button', { name: 'AI 搜索' }))
    expect(await screen.findByText('AI 搜索暂未开放')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '返回' })).toHaveAttribute('href', '#')
  })
  it('keeps controls usable after a request failure', async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error('network offline'))
    render(<AiSearchPage />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'test' } })
    fireEvent.click(screen.getByRole('button', { name: 'AI 搜索' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('网络异常，请稍后再试'))
    expect(screen.getByRole('textbox')).toBeEnabled()
  })
  it('loads a different job after a mounted hashchange and restores its query', async () => {
    window.history.replaceState(null, '', '/#ai-search?job=job-1')
    vi.mocked(apiFetch)
      .mockResolvedValueOnce({ job_id: 'job-1', query: 'first query', status: 'running', items: [{ id: 'one', title: 'One', platform: 'rss', fetched_at: '' }] })
      .mockResolvedValueOnce({ job_id: 'job-2', query: 'second query', status: 'done', items: [{ id: 'one', title: 'One', platform: 'rss', fetched_at: '' }] })
    const loadJob = vi.spyOn(useAiSearchStore.getState(), 'loadJob')
    render(<AiSearchPage />)
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('first query'))
    act(() => {
      window.history.replaceState(null, '', '/#ai-search?job=job-2')
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('second query'))
    // 第二个参数是地址栏里的问题（这条用例的 hash 没带 q，所以是 undefined）：
    // 刷新保留地址栏时，标题靠它立刻显示而不是空等响应。
    expect(loadJob).toHaveBeenLastCalledWith('job-2', undefined)
    expect(apiFetch).toHaveBeenLastCalledWith('/api/ai-search/job-2', undefined)
    expect(useAiSearchStore.getState()).toMatchObject({ jobId: 'job-2', query: 'second query', summary: { status: 'done' } })
    act(() => window.dispatchEvent(new HashChangeEvent('hashchange')))
    expect(loadJob).toHaveBeenCalledTimes(2)
  })
})
