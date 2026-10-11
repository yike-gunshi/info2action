import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { AiSearchPage } from '../AiSearchPage'

// 复制与分享的结果走页面级 toast（与「收藏」同一个）。
const toastSuccess = vi.fn()
vi.mock('sonner', () => ({ toast: { success: (...a: unknown[]) => toastSuccess(...a), error: vi.fn() } }))
import { apiFetch } from '../../../lib/api/_client'
import { getAiSearchStatus, POLL_INTERVAL_MS } from '../../../lib/api/aiSearch'
import { useAiSearchStore } from '../../../store/aiSearchStore'
import { useAuthStore } from '../../../store/authStore'

vi.mock('../../../lib/api/_client', () => ({ apiFetch: vi.fn() }))
vi.mock('../../../lib/api/aiSearch', async importOriginal => ({
  ...await importOriginal<typeof import('../../../lib/api/aiSearch')>(), getAiSearchStatus: vi.fn(),
}))
vi.mock('../../layout/TopBar', () => ({ TopBar: () => <header>TopBar</header> }))

const items = [{ id: 'one', title: '原始来源', platform: 'rss', fetched_at: '2026-09-21T12:00:00Z', url: 'https://example.com/source', author_name: '作者' }]

beforeEach(() => {
  vi.mocked(apiFetch).mockReset()
  useAiSearchStore.getState().reset()
  useAiSearchStore.getState().invalidateStatus()
  useAiSearchStore.setState({ unavailable: false })
  vi.mocked(getAiSearchStatus).mockReset().mockResolvedValue({ enabled: true, logged_in: true, can_search: true, reason: null, user_day_limit: 2 })
  useAuthStore.setState({ user: null, isChecked: true, isLoading: false })
  document.documentElement.style.setProperty('--ai-hist-max', '2')
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false })))
  window.history.replaceState(null, '', '/#ai-search')
})
afterEach(() => {
  cleanup()
  useAiSearchStore.getState().reset()
  useAuthStore.setState({ user: null, isChecked: false, isLoading: true })
  document.documentElement.style.removeProperty('--ai-hist-max')
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('AI search Wave B integration', () => {
  it.each(['done', 'running'] as const)('restores a %s history job using only GET and polls only while running', async status => {
    vi.mocked(apiFetch).mockImplementation(async url => url.startsWith('/api/ai-search/history')
      ? { anonymous: false, total: 1, items: [{ job_id: 'saved-job', query: '历史问题', created_at: '2026-09-20T12:00:00Z', status }] }
      : { job_id: 'saved-job', query: '历史问题', status, items, content_md: '保存的正文 [1]', cited_ids: ['one'] })
    render(<AiSearchPage />)
    const link = await screen.findByRole('link', { name: /历史问题/ })
    expect(screen.getByRole('region', { name: '搜索记录' })).toBeInTheDocument()
    vi.useFakeTimers()
    fireEvent.click(link)
    // jsdom schedules hashchange on the next task after the anchor navigation.
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(window.location.hash).toContain('#ai-search?job=saved-job')
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('历史问题')
    expect(useAiSearchStore.getState().summary.status).toBe(status)
    expect(screen.queryByRole('region', { name: '搜索记录' })).not.toBeInTheDocument()
    expect(apiFetch).toHaveBeenCalledWith('/api/ai-search/saved-job', undefined)
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS) })
    expect(vi.mocked(apiFetch).mock.calls.filter(([url]) => url === '/api/ai-search/saved-job')).toHaveLength(status === 'running' ? 2 : 1)
    expect(vi.mocked(apiFetch).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
  })

  it('reports copy and share through the page toast without restoring the input hint in results', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    vi.mocked(apiFetch).mockImplementation(async url => url.startsWith('/api/ai-search/history')
      ? { anonymous: true, items: [] }
      : { job_id: 'saved-job', query: '历史问题', status: 'done', items, content_md: '保存的正文 [1]', cited_ids: ['one'] })
    render(<AiSearchPage />)
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/api/ai-search/history?limit=2', expect.anything()))
    const hint = screen.getByRole('status')
    expect(hint).toBeEmptyDOMElement()
    act(() => {
      window.history.replaceState(null, '', '/#ai-search?job=saved-job')
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    fireEvent.click(await screen.findByRole('button', { name: '复制' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('已复制正文与参考信息'))
    expect(writeText.mock.calls[0][0]).toContain('保存的正文 [1]')
    // 结果态不渲染输入框和提示行；复制反馈只走 toast。
    expect(hint).not.toBeInTheDocument()
    expect(document.querySelector('#ai-search-hint')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '分享' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('分享链接已复制'))
    expect(writeText.mock.calls[1][0]).toBe(`${window.location.origin}/#ai-search?job=saved-job`)
    expect(document.querySelector('#ai-search-hint')).not.toBeInTheDocument()
    act(() => {
      window.history.replaceState(null, '', '/#ai-search')
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/api/ai-search/history?limit=2', expect.anything()))
    const idleHint = screen.getByRole('status')
    expect(idleHint).toBeEmptyDOMElement()
    expect(idleHint).toHaveClass('ai-search-hint-empty')
    expect(screen.queryByRole('button', { name: '复制' })).not.toBeInTheDocument()
  })
})
