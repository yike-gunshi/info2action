import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'

const getAiSearchStatus = vi.hoisted(() => vi.fn())
vi.mock('../../../lib/api/aiSearch', async importOriginal => ({
  ...await importOriginal<typeof import('../../../lib/api/aiSearch')>(), getAiSearchStatus,
}))

beforeEach(() => {
  vi.resetModules()
  getAiSearchStatus.mockReset()
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

async function loadTopBar() {
  const { useAuthStore } = await import('../../../store/authStore')
  const { useAiSearchStore } = await import('../../../store/aiSearchStore')
  useAuthStore.setState({ user: null, isChecked: true, isLoading: false })
  useAiSearchStore.setState({ unavailable: false })
  return (await import('../TopBar')).TopBar
}

describe('TopBar status cache', () => {
  it.each([false, true, 'offline'] as const)('reserves an invisible, inaccessible entry on the first frame before status: %s', async outcome => {
    let resolve!: (status: { enabled: boolean }) => void
    let reject!: (error: Error) => void
    getAiSearchStatus.mockReturnValue(new Promise((done, fail) => { resolve = done; reject = fail }))
    const TopBar = await loadTopBar()
    const { unmount } = render(<TopBar />)
    const entry = screen.getByTestId('topbar-ai-entry')
    expect(entry).not.toBeVisible()
    expect(entry).toHaveStyle({ visibility: 'hidden' })
    expect(entry).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'AI 搜索' })).not.toBeInTheDocument()
    await act(async () => outcome === 'offline' ? reject(new Error('offline')) : resolve({ enabled: outcome }))
    if (outcome === false) expect(screen.queryByTestId('topbar-ai-entry')).not.toBeInTheDocument()
    else expect(screen.getByRole('button', { name: 'AI 搜索' })).toBeVisible()
    unmount()
    render(<TopBar />)
    if (outcome === false) expect(screen.queryByTestId('topbar-ai-entry')).not.toBeInTheDocument()
    else expect(screen.getByRole('button', { name: 'AI 搜索' })).toBeVisible()
    expect(getAiSearchStatus).toHaveBeenCalledOnce()
  })
  it('hides a disabled entry from status and reuses the module cache after remounting', async () => {
    getAiSearchStatus.mockResolvedValue({ enabled: false, logged_in: false, can_search: false, reason: 'disabled', user_day_limit: 2 })
    const TopBar = await loadTopBar()
    const { unmount } = render(<TopBar />)
    await waitFor(() => expect(screen.queryByTestId('topbar-ai-entry')).not.toBeInTheDocument())
    unmount()
    render(<TopBar />)
    expect(screen.queryByTestId('topbar-ai-entry')).not.toBeInTheDocument()
    expect(getAiSearchStatus).toHaveBeenCalledOnce()
  })

  it('keeps the enabled entry available even when daily quota is exhausted', async () => {
    getAiSearchStatus.mockResolvedValue({ enabled: true, logged_in: true, can_search: false, reason: 'quota_exhausted', user_day_limit: 2 })
    const TopBar = await loadTopBar()
    render(<TopBar />)
    await waitFor(() => expect(screen.getByTestId('topbar-ai-entry')).toBeVisible())
    expect(getAiSearchStatus).toHaveBeenCalledOnce()
    expect(screen.getByTestId('topbar-ai-entry')).toBeEnabled()
  })

  it('shows the entry when status fails and does not retry on remount', async () => {
    getAiSearchStatus.mockRejectedValue(new Error('offline'))
    const TopBar = await loadTopBar()
    const { unmount } = render(<TopBar />)
    await waitFor(() => expect(screen.getByTestId('topbar-ai-entry')).toBeVisible())
    expect(getAiSearchStatus).toHaveBeenCalledOnce()
    expect(screen.getByTestId('topbar-ai-entry')).toBeEnabled()
    unmount()
    render(<TopBar />)
    expect(screen.getByTestId('topbar-ai-entry')).toBeEnabled()
    expect(getAiSearchStatus).toHaveBeenCalledOnce()
  })
})
