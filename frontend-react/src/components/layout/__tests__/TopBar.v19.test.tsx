import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { TopBar } from '../TopBar'
import { useAuthStore } from '../../../store/authStore'
import { useUIStore } from '../../../store/uiStore'

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

describe('TopBar v19 Image2 constraints', () => {
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

  it('G1: 切 tab 记忆并恢复滚动位置;同 tab 再点回顶部', () => {
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb: FrameRequestCallback) => {
      cb(0)
      return 0
    })
    let scrollY = 0
    Object.defineProperty(window, 'scrollY', { configurable: true, get: () => scrollY })
    const scrollToSpy = window.scrollTo as unknown as ReturnType<typeof vi.fn>

    render(<TopBar />)

    // 在精选 tab 滚到 500,切到信息 → 存精选=500
    scrollY = 500
    fireEvent.click(screen.getByTestId('topbar-tab-info'))
    // 切回精选 → 必恢复 500(往返一致,不受模块级 Map 历史影响)
    scrollY = 123
    fireEvent.click(screen.getByTestId('topbar-tab-highlights'))
    expect(scrollToSpy).toHaveBeenLastCalledWith({ top: 500 })

    // 同 tab 再点 → 回顶部 0
    scrollY = 777
    fireEvent.click(screen.getByTestId('topbar-tab-highlights'))
    expect(scrollToSpy).toHaveBeenLastCalledWith({ top: 0 })
  })

  it('renders the locked editorial shell: full logo, centered three tabs, compact sticky bar', () => {
    render(<TopBar />)

    const topbar = screen.getByTestId('topbar')
    expect(topbar.className).toContain('sticky')
    expect(topbar.className).toContain('top-0')
    expect(topbar.className).toContain('bg-background')
    const grid = screen.getByTestId('topbar-grid')
    expect(grid.className).toContain('min-h-[84px]')
    expect(grid.className).toContain('grid-rows-[48px_36px]')
    expect(grid.className).toContain('sm:h-[52px]')

    const logo = screen.getByTestId('topbar-logo')
    expect(logo).toHaveTextContent('info2act')
    expect(logo.className).toContain('font-brand')
    expect(logo.className).toContain('text-[26px]')
    expect(logo.className).toContain('font-[700]')
    expect(logo.querySelector('.brand-wordmark__two')).toHaveTextContent('2')
    expect(topbar).not.toHaveTextContent('i2a')

    const nav = screen.getByRole('navigation', { name: '主导航' })
    expect(nav.className).toContain('row-start-2')
    expect(nav.className).toContain('border-t')
    expect(within(nav).getAllByRole('button').map((button) => button.textContent)).toEqual([
      '精选',
      '信息',
      '行动',
    ])
    expect(screen.getByTestId('topbar-tab-highlights')).toHaveAttribute('aria-current', 'page')
    expect(screen.getByTestId('topbar-tab-highlights').className).toContain('font-event-title')
    expect(screen.getByTestId('topbar-tab-highlights').className).toContain('after:bg-[var(--brand)]')
  })

  it('keeps public utility icons on the right while notification stays hidden', () => {
    render(<TopBar />)

    const search = screen.getByTestId('topbar-search')
    expect(search).toBeInTheDocument()
    expect(search.className).toContain('hidden')
    expect(search.className).toContain('sm:flex')
    expect(search.className).toContain('w-9')
    expect(screen.queryByRole('textbox', { name: '搜索信息' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '搜索' })).toBeInTheDocument()
    expect(screen.getByLabelText('提交链接')).toBeInTheDocument()
    expect(screen.getByLabelText('切换主题')).toBeInTheDocument()
    const login = screen.getByTestId('topbar-login')
    expect(login).toBeInTheDocument()
    expect(login.className).toContain('w-9')
    expect(login.className).toContain('sm:w-auto')
    expect(login.className).toContain('font-event-title')
    expect(login.className).toContain('text-[16px]')
    expect(login.className).toContain('font-normal')
    expect(login.className).toContain('leading-none')
    expect(login.className).toContain('text-[var(--brand)]')
    expect(login.className).not.toContain('font-semibold')
    const loginText = within(login).getByText('登录')
    expect(loginText.className).toContain('h-4')
    expect(loginText.className).toContain('items-center')
    expect(loginText.className).toContain('leading-none')

    expect(screen.queryByLabelText('通知')).not.toBeInTheDocument()
  })

  it('expands search into a compact pinned topbar input', () => {
    render(<TopBar />)

    fireEvent.click(screen.getByRole('button', { name: '搜索' }))

    const search = screen.getByTestId('topbar-search')
    expect(search.className).toContain('w-[180px]')
    expect(search.className).toContain('md:w-[220px]')
    expect(search.className).toContain('lg:w-[260px]')
    const input = screen.getByRole('textbox', { name: '搜索信息' })
    expect(input).toBeInTheDocument()
    expect(input.className).toContain('bg-transparent')
    expect(input.className).toContain('font-event-title')
    expect(screen.getByRole('button', { name: '清除搜索' })).toBeInTheDocument()
  })

  it('keeps account-only items inside the avatar menu when logged in', () => {
    useAuthStore.setState({
      user: {
        id: 'u-1',
        username: 'Ada',
        email: 'ada@example.com',
        role: 'user',
      },
      isLoading: false,
      isChecked: true,
    })

    render(<TopBar />)

    expect(screen.queryByTestId('topbar-login')).not.toBeInTheDocument()
    const userTrigger = screen.getByTestId('topbar-user-trigger')
    expect(userTrigger).toBeInTheDocument()
    expect(userTrigger).toHaveAttribute('aria-label', '用户菜单')
    expect(screen.getByLabelText('提交链接')).toBeInTheDocument()
    expect(screen.getByLabelText('切换主题')).toBeInTheDocument()
    expect(screen.queryByLabelText('通知')).not.toBeInTheDocument()

    fireEvent.click(userTrigger)

    expect(screen.queryByText('快速入口')).not.toBeInTheDocument()
    expect(screen.getByText('我的收藏')).toBeInTheDocument()
    expect(screen.getByText('浏览历史')).toBeInTheDocument()
  })

  it('allows utility pages to reuse TopBar without highlighting dashboard tabs', () => {
    render(<TopBar activeL1={null} />)

    expect(screen.getByTestId('topbar-tab-highlights')).not.toHaveAttribute('aria-current')
    expect(screen.getByTestId('topbar-tab-info')).not.toHaveAttribute('aria-current')
    expect(screen.getByTestId('topbar-tab-actions')).not.toHaveAttribute('aria-current')
  })
})
