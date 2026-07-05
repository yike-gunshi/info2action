import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { useTheme } from '../useTheme'

function stubSystemTheme(matches = false) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })
}

describe('useTheme', () => {
  beforeEach(() => {
    window.localStorage.clear()
    window.history.replaceState({}, '', '/')
    document.documentElement.className = ''
    document.documentElement.removeAttribute('data-theme')
    stubSystemTheme(false)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('allows URL theme override for visual review without persisting preference', async () => {
    window.localStorage.setItem('i2a_theme', 'light')
    window.history.replaceState({}, '', '/?theme=dark')

    const { result } = renderHook(() => useTheme())

    expect(result.current.mode).toBe('dark')
    await waitFor(() => expect(document.documentElement).toHaveClass('dark'))
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
    expect(window.localStorage.getItem('i2a_theme')).toBe('light')
  })

  it('ignores invalid URL theme values and keeps stored preference', async () => {
    window.localStorage.setItem('i2a_theme', 'dark')
    window.history.replaceState({}, '', '/?theme=sepia')

    const { result } = renderHook(() => useTheme())

    expect(result.current.mode).toBe('dark')
    await waitFor(() => expect(document.documentElement).toHaveClass('dark'))
    expect(window.localStorage.getItem('i2a_theme')).toBe('dark')
  })
})
