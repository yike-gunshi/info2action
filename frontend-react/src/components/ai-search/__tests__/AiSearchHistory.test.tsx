import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { AiSearchHistory } from '../AiSearchHistory'
import { getAiSearchHistory, type AiSearchHistoryItem, type AiSearchHistoryResponse } from '../../../lib/api/aiSearch'
import { useAuthStore } from '../../../store/authStore'
import { useAiSearchStore } from '../../../store/aiSearchStore'

vi.mock('../../../lib/api/aiSearch', async importOriginal => ({
  ...await importOriginal<typeof import('../../../lib/api/aiSearch')>(), getAiSearchHistory: vi.fn(),
}))

// 接口对无效会话答 200 + anonymous，所以过期的 access cookie 不会产生那个本该驱动续期的 401。
const tryRefresh = vi.fn()
vi.mock('../../../lib/api/_client', () => ({ tryRefresh: () => tryRefresh() }))

const user = { id: 'user-1', username: 'one', email: 'one@example.com', role: 'user' as const }
const item = (jobId: string, status: AiSearchHistoryItem['status'] = 'done'): AiSearchHistoryItem => ({
  job_id: jobId,
  query: `查询 ${jobId}`,
  created_at: '2026-09-20T12:00:00',
  status,
})

beforeEach(() => {
  vi.mocked(getAiSearchHistory).mockReset().mockResolvedValue({ anonymous: true, items: [] })
  useAuthStore.setState({ user: null, isChecked: true, isLoading: false })
  document.documentElement.style.setProperty('--ai-hist-max', '3')
  window.history.replaceState(null, '', '/#ai-search')
})

afterEach(() => {
  cleanup()
  document.documentElement.style.removeProperty('--ai-hist-max')
  useAuthStore.setState({ user: null, isChecked: false, isLoading: true })
  vi.restoreAllMocks()
})

describe('AiSearchHistory', () => {
  it.each([
    ['retrieving', '找资料中'],
    ['insufficient', '资料不足'],
  ] as const)('opens the existing %s job with its new label without creating a search', async (status, label) => {
    const saved = item('v2/job', status)
    useAuthStore.setState({ user })
    vi.mocked(getAiSearchHistory).mockResolvedValue({ anonymous: false, items: [saved], total: 1 })
    const loadJob = vi.spyOn(useAiSearchStore.getState(), 'loadJob').mockResolvedValue(undefined)
    const submit = vi.spyOn(useAiSearchStore.getState(), 'submit').mockResolvedValue(undefined)
    render(<AiSearchHistory />)
    const link = await screen.findByRole('link', { name: `查询 v2/job ${label}` })
    expect(link).toHaveAttribute('href', `#ai-search?job=v2%2Fjob&q=${encodeURIComponent(saved.query)}`)
    expect(link).not.toHaveAttribute('aria-disabled')
    fireEvent.click(link)
    await waitFor(() => expect(window.location.hash).toBe(link.getAttribute('href')))
    fireEvent.click(link)
    expect(loadJob).toHaveBeenCalledOnce()
    expect(loadJob).toHaveBeenCalledWith(saved.job_id, saved.query)
    expect(submit).not.toHaveBeenCalled()
  })
  it('directly reloads the matching history URL without relying on a hashchange', async () => {
    const saved = item('saved/job')
    vi.mocked(getAiSearchHistory).mockResolvedValue({ anonymous: false, items: [saved], total: 1 })
    const loadJob = vi.spyOn(useAiSearchStore.getState(), 'loadJob').mockResolvedValue(undefined)
    render(<AiSearchHistory />)
    const link = await screen.findByRole('link', { name: /查询 saved\/job/ })
    const hash = link.getAttribute('href')!
    window.history.replaceState(null, '', hash)
    const click = new MouseEvent('click', { bubbles: true, cancelable: true })
    fireEvent(link, click)
    expect(click.defaultPrevented).toBe(true)
    expect(loadJob).toHaveBeenCalledOnce()
    expect(loadJob).toHaveBeenCalledWith(saved.job_id, saved.query)
    expect(window.location.hash).toBe(hash)
  })

  it('keeps navigation to a different history URL and modified clicks native', async () => {
    const saved = item('saved')
    vi.mocked(getAiSearchHistory).mockResolvedValue({ anonymous: false, items: [saved], total: 1 })
    const loadJob = vi.spyOn(useAiSearchStore.getState(), 'loadJob').mockResolvedValue(undefined)
    render(<AiSearchHistory />)
    const link = await screen.findByRole('link', { name: /查询 saved/ })
    const click = new MouseEvent('click', { bubbles: true, cancelable: true })
    fireEvent(link, click)
    expect(click.defaultPrevented).toBe(false)
    expect(loadJob).not.toHaveBeenCalled()
    await waitFor(() => expect(window.location.hash).toBe(link.getAttribute('href')))
    for (const modifiers of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }]) {
      const modified = new MouseEvent('click', { bubbles: true, cancelable: true, ...modifiers })
      fireEvent(link, modified)
      expect(modified.defaultPrevented).toBe(false)
    }
    expect(loadJob).not.toHaveBeenCalled()
  })
  it('does not duplicate the input login prompt for anonymous history', async () => {
    const { container } = render(<AiSearchHistory />)
    await waitFor(() => expect(getAiSearchHistory).toHaveBeenCalled())
    expect(screen.queryByRole('link', { name: '登录' })).not.toBeInTheDocument()
    expect(container.querySelector('.anon')).not.toBeInTheDocument()
    expect(container.querySelector('.none')).not.toBeInTheDocument()
    expect(container.querySelector('.hlist')).not.toBeInTheDocument()
  })

  it('renders only the empty-history guidance for a signed-in user without records', async () => {
    useAuthStore.setState({ user })
    vi.mocked(getAiSearchHistory).mockResolvedValue({ anonymous: false, items: [], total: 0 })
    const { container } = render(<AiSearchHistory />)
    expect(await screen.findByText('搜过的内容会出现在这里')).toBeInTheDocument()
    expect(container.querySelector('.none')).toBeInTheDocument()
    expect(container.querySelector('.anon')).not.toBeInTheDocument()
    expect(container.querySelector('.hlist')).not.toBeInTheDocument()
  })

  it('renders only the list and navigates done/running rows to existing jobs without a new search', async () => {
    useAuthStore.setState({ user })
    vi.mocked(getAiSearchHistory).mockResolvedValue({
      anonymous: false,
      items: [item('done/job'), item('active', 'running'), item('failed', 'failed')],
      total: 3,
    })
    const { container } = render(<AiSearchHistory />)
    await screen.findByText('最近搜索')
    expect(container.querySelector('.anon')).not.toBeInTheDocument()
    expect(container.querySelector('.none')).not.toBeInTheDocument()
    expect(container.querySelectorAll('.hrow')).toHaveLength(3)

    const done = screen.getByRole('link', { name: '查询 done/job 09/20' })
    // 链接同时带上问题：刷新保留地址栏，输入框才能立刻显示（用户 2026-09-22）
    expect(done.getAttribute('href')).toContain('#ai-search?job=done%2Fjob&q=')
    fireEvent.click(done)
    await waitFor(() => expect(window.location.hash).toContain('#ai-search?job=done%2Fjob'))

    const running = screen.getByRole('link', { name: '查询 active 进行中' })
    expect(running).toHaveAttribute('href', expect.stringContaining('#ai-search?job=active'))
    fireEvent.click(running)
    await waitFor(() => expect(window.location.hash).toContain('#ai-search?job=active'))

    const failed = screen.getByText('查询 failed').closest('.hrow')!
    expect(failed).toHaveAttribute('aria-disabled', 'true')
    expect(failed).not.toHaveAttribute('href')
    expect(failed).not.toHaveAttribute('tabindex')
    expect(failed).toHaveTextContent('未完成')
    fireEvent.click(failed)
    expect(window.location.hash).toContain('#ai-search?job=active')
    expect(getAiSearchHistory).toHaveBeenCalledTimes(1)
  })

  it('treats queued jobs as in progress and unavailable jobs as incomplete', async () => {
    vi.mocked(getAiSearchHistory).mockResolvedValue({
      anonymous: false,
      items: [item('queued', 'queued'), item('unavailable', 'unavailable')],
      total: 2,
    })
    render(<AiSearchHistory />)
    expect(await screen.findByRole('link', { name: '查询 queued 进行中' })).toHaveAttribute('href', expect.stringContaining('#ai-search?job=queued'))
    expect(screen.getByText('查询 unavailable').closest('.hrow')).toHaveTextContent('未完成')
    expect(screen.queryByRole('link', { name: /查询 unavailable/ })).not.toBeInTheDocument()
  })

  it('reads the CSS history limit, limits visible rows, and renders the remainder as plain text', async () => {
    document.documentElement.style.setProperty('--ai-hist-max', '2')
    vi.mocked(getAiSearchHistory).mockResolvedValue({
      anonymous: false,
      items: [item('one'), item('two'), item('three')],
      total: 5,
    })
    const { container } = render(<AiSearchHistory />)
    expect(await screen.findByText('还有 3 条')).toBeInTheDocument()
    expect(getAiSearchHistory).toHaveBeenCalledWith(2, expect.any(AbortSignal))
    expect(container.querySelectorAll('.hrow')).toHaveLength(2)
    expect(screen.queryByText('查询 three')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /还有|查看全部/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /还有|查看全部/ })).not.toBeInTheDocument()
  })

  it.each([2, 3])('omits the remainder when total %s is no greater than the CSS limit', async total => {
    vi.mocked(getAiSearchHistory).mockResolvedValue({
      anonymous: false,
      items: Array.from({ length: total }, (_, index) => item(String(index))),
      total,
    })
    render(<AiSearchHistory />)
    await screen.findByText('最近搜索')
    expect(screen.queryByText(/还有 \d+ 条/)).not.toBeInTheDocument()
  })

  it('waits for the existing auth check before requesting history', async () => {
    useAuthStore.setState({ isChecked: false, isLoading: true })
    render(<AiSearchHistory />)
    expect(getAiSearchHistory).not.toHaveBeenCalled()
    act(() => useAuthStore.setState({ isChecked: true, isLoading: false }))
    await waitFor(() => expect(getAiSearchHistory).toHaveBeenCalled())
    expect(getAiSearchHistory).toHaveBeenCalledTimes(1)
  })

  it('clears the previous account records immediately and refreshes when identity changes', async () => {
    useAuthStore.setState({ user })
    vi.mocked(getAiSearchHistory)
      .mockResolvedValueOnce({ anonymous: false, items: [item('private')], total: 1 })
      .mockResolvedValueOnce({ anonymous: true, items: [] })
    render(<AiSearchHistory />)
    await screen.findByText('查询 private')
    act(() => useAuthStore.setState({ user: null }))
    expect(screen.queryByText('查询 private')).not.toBeInTheDocument()
    await waitFor(() => expect(getAiSearchHistory).toHaveBeenCalled())
    expect(getAiSearchHistory).toHaveBeenCalledTimes(2)
  })

  it('aborts stale identity requests and ignores a response that arrives after the identity changed', async () => {
    let resolveOld!: (data: AiSearchHistoryResponse) => void
    vi.mocked(getAiSearchHistory)
      .mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
      .mockResolvedValueOnce({ anonymous: false, items: [], total: 0 })
    render(<AiSearchHistory />)
    const signal = vi.mocked(getAiSearchHistory).mock.calls[0][1]!
    act(() => useAuthStore.setState({ user }))
    expect(signal.aborted).toBe(true)
    await screen.findByText('搜过的内容会出现在这里')
    await act(async () => resolveOld({ anonymous: true, items: [] }))
    expect(screen.queryByText(/登录后可以查看自己的搜索记录/)).not.toBeInTheDocument()
    expect(screen.getByText('搜过的内容会出现在这里')).toBeInTheDocument()
  })

  it('aborts an outstanding request when unmounted', () => {
    vi.mocked(getAiSearchHistory).mockImplementation(() => new Promise(() => {}))
    const { unmount } = render(<AiSearchHistory />)
    const signal = vi.mocked(getAiSearchHistory).mock.calls[0][1]!
    expect(signal.aborted).toBe(false)
    unmount()
    expect(signal.aborted).toBe(true)
  })

  it('shows a request error without misrepresenting it as anonymous or empty history', async () => {
    vi.mocked(getAiSearchHistory).mockRejectedValue(new Error('network offline'))
    const { container } = render(<AiSearchHistory />)
    expect(await screen.findByRole('alert')).toHaveTextContent('搜索记录暂时无法加载，请稍后再试')
    expect(container.querySelector('.anon, .none, .hlist')).not.toBeInTheDocument()
  })

  it('does not substitute a hardcoded limit when the token is missing', async () => {
    document.documentElement.style.removeProperty('--ai-hist-max')
    render(<AiSearchHistory />)
    expect(await screen.findByRole('alert')).toHaveTextContent('搜索记录暂时无法加载')
    expect(getAiSearchHistory).not.toHaveBeenCalled()
  })

  describe('会话过期对账', () => {
    beforeEach(() => { tryRefresh.mockReset() })

    it('续期成功后重取，显示本人记录而不是「请登录」', async () => {
      useAuthStore.setState({ user, isChecked: true, isLoading: false })
      tryRefresh.mockResolvedValue('ok')
      vi.mocked(getAiSearchHistory)
        .mockResolvedValueOnce({ anonymous: true, items: [] })
        .mockResolvedValueOnce({ anonymous: false, items: [item('job-1')], total: 1 })
      render(<AiSearchHistory />)
      expect(await screen.findByText('查询 job-1')).toBeInTheDocument()
      expect(tryRefresh).toHaveBeenCalledTimes(1)
      expect(vi.mocked(getAiSearchHistory)).toHaveBeenCalledTimes(2)
      expect(useAuthStore.getState().user).not.toBeNull()
    })

    it('续期判定失效时清掉登录态，提示才不会自相矛盾', async () => {
      useAuthStore.setState({ user, isChecked: true, isLoading: false })
      tryRefresh.mockResolvedValue('expired')
      vi.mocked(getAiSearchHistory).mockResolvedValue({ anonymous: true, items: [] })
      render(<AiSearchHistory />)
      await waitFor(() => expect(useAuthStore.getState().user).toBeNull())
      expect(tryRefresh).toHaveBeenCalledTimes(1)
    })

    it('服务端抖动不登出（BF-0708-1）', async () => {
      useAuthStore.setState({ user, isChecked: true, isLoading: false })
      tryRefresh.mockResolvedValue('unavailable')
      vi.mocked(getAiSearchHistory).mockResolvedValue({ anonymous: true, items: [] })
      render(<AiSearchHistory />)
      await waitFor(() => expect(tryRefresh).toHaveBeenCalled())
      expect(useAuthStore.getState().user).not.toBeNull()
    })

    it('本就匿名时不触发续期', async () => {
      vi.mocked(getAiSearchHistory).mockResolvedValue({ anonymous: true, items: [] })
      render(<AiSearchHistory />)
      await waitFor(() => expect(getAiSearchHistory).toHaveBeenCalled())
      expect(tryRefresh).not.toHaveBeenCalled()
    })
  })
})
