import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { TopBar } from '../TopBar'
import { useAuthStore } from '../../../store/authStore'
import { useUIStore } from '../../../store/uiStore'

const GITHUB_URL = 'https://github.com/yike-gunshi/info2action'

function resetStores() {
  useUIStore.setState({
    l1: 'highlights',
    expandedKey: null,
    searchQuery: '',
    theme: 'light',
  })
  useAuthStore.setState({
    user: null,
    isLoading: false,
    isChecked: true,
  })
}

function stubBrowserApis() {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
}

describe('TopBar GitHub 入口 (oss-release v20.0 F1)', () => {
  beforeEach(() => {
    window.location.hash = ''
    window.localStorage.clear()
    resetStores()
    stubBrowserApis()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('A1 未登录桌面：右侧渲染 GitHub 外链按钮，新标签安全打开', () => {
    render(<TopBar />)
    const link = screen.getByTestId('topbar-github')
    expect(link).toHaveAttribute('href', GITHUB_URL)
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(link).toHaveAttribute('aria-label', 'GitHub 开源仓库')
    // 桌面平铺、移动端隐藏（<sm 收进用户菜单）
    expect(link.className).toContain('hidden')
    expect(link.className).toContain('sm:inline-flex')
  })

  it('A1b 已登录桌面：GitHub 按钮同样可见', () => {
    useAuthStore.setState({
      user: { id: 1, username: 'tester', email: 't@example.com', role: 'user' } as never,
    })
    render(<TopBar />)
    expect(screen.getByTestId('topbar-github')).toHaveAttribute('href', GITHUB_URL)
  })

  it('A3 移动端已登录：用户菜单含「GitHub 开源仓库」项（仅 <sm 显示）', () => {
    useAuthStore.setState({
      user: { id: 1, username: 'tester', email: 't@example.com', role: 'user' } as never,
    })
    render(<TopBar />)
    fireEvent.click(screen.getByTestId('topbar-user-trigger'))
    const item = screen.getByTestId('menu-github')
    expect(item).toHaveAttribute('href', GITHUB_URL)
    expect(item).toHaveAttribute('target', '_blank')
    expect(item).toHaveAttribute('rel', 'noopener noreferrer')
    expect(item.className).toContain('sm:hidden')
  })

  it('A4 未登录：无用户菜单，也不存在菜单版 GitHub 项（降级态不报错）', () => {
    render(<TopBar />)
    expect(screen.queryByTestId('topbar-user-trigger')).toBeNull()
    expect(screen.queryByTestId('menu-github')).toBeNull()
  })
})
