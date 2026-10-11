import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { TopBar } from '../TopBar'
import { useUIStore } from '../../../store/uiStore'
import { useAuthStore } from '../../../store/authStore'
import { useAiSearchStore } from '../../../store/aiSearchStore'

vi.mock('../../../lib/api/aiSearch', async importOriginal => ({
  ...await importOriginal<typeof import('../../../lib/api/aiSearch')>(),
  getAiSearchStatus: vi.fn().mockResolvedValue({ enabled: true }),
}))

beforeEach(() => {
  window.history.replaceState(null, '', '/')
  useUIStore.setState({ searchQuery: '', l1: 'highlights', theme: 'light' })
  useAuthStore.setState({ user: null, isLoading: false, isChecked: true })
  useAiSearchStore.setState({ unavailable: false })
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('TopBar AI entry', () => {
  it('is a public, accessible text button on desktop and mobile without a glow', async () => {
    render(<TopBar />)
    const entry = await screen.findByRole('button', { name: 'AI 搜索' })
    expect(entry).toHaveTextContent('AI')
    expect(entry).toHaveAttribute('data-testid', 'topbar-ai-entry')
    expect(entry).toHaveClass('min-h-11', 'min-w-11')
    expect(entry).toHaveClass('order-[var(--ai-entry-order)]', 'text-[length:var(--ai-entry-size)]', 'font-[number:var(--ai-entry-weight)]', 'text-[var(--ai-entry-color)]')
    expect(entry.closest('.sm\\:hidden')).toBeNull()
    expect(screen.queryByTestId('ai-search-glow')).toBeNull()
    fireEvent.click(entry)
    expect(window.location.hash).toBe('#ai-search')
  })
  it('carries the current query and preserves ordinary search', async () => {
    useUIStore.setState({ searchQuery: '  seedance 2.5 & 中文  ' })
    render(<TopBar />)
    fireEvent.click(await screen.findByRole('button', { name: 'AI 搜索' }))
    expect(window.location.hash).toBe('#ai-search?q=' + encodeURIComponent('seedance 2.5 & 中文'))
    expect(useUIStore.getState().searchQuery).toBe('  seedance 2.5 & 中文  ')
  })
  it('keeps the AI entry before the responsive icon group and retains the changelog entry', async () => {
    render(<TopBar />)
    const entry = await screen.findByRole('button', { name: 'AI 搜索' })
    const iconGroup = screen.getByTestId('topbar-theme-toggle').parentElement
    expect(entry.nextElementSibling).toBe(iconGroup)
    expect(iconGroup).toHaveClass('gap-0', 'sm:gap-1.5')
    expect(screen.getByTestId('topbar-changelog')).toHaveAttribute('href', '#changelog')
  })
  it('hides the entry after the API reports that the feature is unavailable', () => {
    useAiSearchStore.setState({ unavailable: true })
    render(<TopBar />)
    expect(screen.queryByRole('button', { name: 'AI 搜索' })).toBeNull()
  })
})
