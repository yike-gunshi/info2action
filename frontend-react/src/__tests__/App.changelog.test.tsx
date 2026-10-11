import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import App from '../App'
import { authMe, fetchClassification } from '../lib/api'
import { useAuthStore } from '../store/authStore'
import { useUIStore } from '../store/uiStore'
import { useDetailStore } from '../store/detailStore'
import { useClusterDetailStore } from '../store/clusterDetailStore'

vi.mock('../lib/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../lib/api')>(),
  authMe: vi.fn(),
  fetchClassification: vi.fn(),
}))
// Keep the actual App, TopBar, routing, theme and unread state; isolate feed data.
vi.mock('../components/highlights/HighlightsView', () => ({ HighlightsView: () => <div>精选内容</div> }))
vi.mock('../components/info/InfoView', () => ({ InfoView: () => <div>信息内容</div> }))
vi.mock('../components/actions/ActionsView', () => ({ ActionsView: () => <div>行动内容</div> }))
vi.mock('../components/layout/L2Pills', () => ({ L2Pills: () => null }))
vi.mock('../components/cluster/ClusterDetailPanel', () => ({ ClusterDetailPanel: () => null }))
vi.mock('../components/detail/DetailPanel', () => ({ DetailPanel: () => <div data-testid="detail-modal" /> }))
vi.mock('../pages/OnboardingPage', () => ({ OnboardingPage: () => <div>新手引导</div> }))

async function navigate(hash: string) {
  await act(async () => {
    window.history.replaceState(null, '', hash)
    window.dispatchEvent(new HashChangeEvent('hashchange'))
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  window.history.replaceState(null, '', '/#changelog')
  localStorage.clear()
  vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-09-22T12:00:00Z'))
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => { callback(0); return 0 })
  vi.stubGlobal('matchMedia', () => ({ matches: false }))
  useAuthStore.setState({ user: null, isLoading: true, isChecked: false })
  useUIStore.setState({ l1: 'highlights', expandedKey: null, searchQuery: '' })
  useDetailStore.getState().closeModal()
  useClusterDetailStore.getState().closeModal()
  vi.mocked(authMe).mockRejectedValue(new Error('Not authenticated'))
  vi.mocked(fetchClassification).mockResolvedValue({ categories: [] })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('public changelog integration', () => {
  it('renders immediately while authentication is pending, with no feed search or selected tab', () => {
    vi.mocked(authMe).mockReturnValue(new Promise(() => {}))
    render(<App />)
    expect(screen.getByRole('heading', { name: '更新日志', level: 1 })).toBeInTheDocument()
    expect(screen.getByTestId('topbar-changelog').nextElementSibling).toBe(screen.getByTestId('topbar-login'))
    expect(screen.queryByRole('button', { name: '搜索' })).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: '搜索信息' })).not.toBeInTheDocument()
    for (const tab of within(screen.getByRole('navigation', { name: '主导航' })).getAllByRole('button')) {
      expect(tab).not.toHaveAttribute('aria-current')
    }
  })

  it('remains public after an auth failure', async () => {
    render(<App />)
    await waitFor(() => expect(useAuthStore.getState().isChecked).toBe(true))
    expect(screen.getByRole('heading', { name: '更新日志', level: 1 })).toBeInTheDocument()
    expect(window.location.hash).toBe('#changelog')
    expect(fetchClassification).not.toHaveBeenCalled()
  })

  it('places the entry immediately before the avatar and bypasses onboarding', async () => {
    vi.mocked(authMe).mockResolvedValue({ id: 'u1', username: '很长的用户名字', email: 'reader@example.com', role: 'user', onboarding_completed: false })
    render(<App />)
    const avatar = await screen.findByTestId('topbar-user-trigger')
    expect(screen.getByTestId('topbar-changelog').nextElementSibling).toContainElement(avatar)
    expect(screen.getByRole('heading', { name: '更新日志', level: 1 })).toBeInTheDocument()
    expect(screen.queryByText('新手引导')).not.toBeInTheDocument()
  })

  it.each(['highlights', 'info', 'actions'])('returns to %s with a clean dashboard hash', async (view) => {
    render(<App />)
    fireEvent.click(screen.getByTestId(`topbar-tab-${view}`))
    await waitFor(() => expect(window.location.hash).toBe(`#v=${view}`))
    await waitFor(() => expect(screen.queryByRole('heading', { name: '更新日志', level: 1 })).not.toBeInTheDocument())
    expect(screen.getByTestId(`topbar-tab-${view}`)).toHaveAttribute('aria-current', 'page')
  })

  it('clears unread only after visiting and keeps the entry available on return', async () => {
    window.history.replaceState(null, '', '/#v=highlights')
    render(<App />)
    expect(screen.getByTestId('changelog-unread-dot')).toBeInTheDocument()
    expect(screen.getByTestId('topbar-changelog')).toHaveAttribute('href', '#changelog')
    fireEvent.click(screen.getByTestId('topbar-changelog'))
    await screen.findByRole('heading', { name: '更新日志', level: 1 })
    expect(screen.queryByTestId('changelog-unread-dot')).not.toBeInTheDocument()
    await navigate('#v=highlights')
    expect(screen.getByTestId('topbar-changelog')).toBeInTheDocument()
    expect(screen.queryByTestId('changelog-unread-dot')).not.toBeInTheDocument()
  })

  it('closes stale item and cluster modals on direct entry', async () => {
    useDetailStore.setState({ modalStack: [{ type: 'action', id: 'old-action' }] })
    useClusterDetailStore.setState({ modalState: 'open', modalClusterId: 42 })
    render(<App />)
    expect(screen.queryByTestId('detail-modal')).not.toBeInTheDocument()
    expect(useDetailStore.getState().modalStack).toEqual([])
    expect(useClusterDetailStore.getState().modalState).toBe('closed')
    await navigate('#v=highlights')
    expect(screen.queryByTestId('detail-modal')).not.toBeInTheDocument()
  })

  it('keeps the complete public shell usable when browser storage is blocked', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('Storage blocked') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('Storage blocked') })
    render(<App />)
    expect(screen.getByRole('heading', { name: '更新日志', level: 1 })).toBeInTheDocument()
    expect(screen.queryByTestId('changelog-unread-dot')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('topbar-tab-info'))
    await waitFor(() => expect(window.location.hash).toBe('#v=info'))
    await screen.findByText('信息内容')
  })
})
