import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ExpandButton } from '../ExpandButton'

/** mock matchMedia 使 (pointer: coarse) 可控 */
function mockPointerCoarse(matches: boolean) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: query === '(pointer: coarse)' ? matches : false,
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

describe('ExpandButton', () => {
  let originalMatchMedia: typeof window.matchMedia | undefined
  let originalOpen: typeof window.open

  beforeEach(() => {
    originalMatchMedia = window.matchMedia
    originalOpen = window.open
    // 默认桌面(非触控)
    mockPointerCoarse(false)
    // 还原 hash
    window.location.hash = ''
  })

  afterEach(() => {
    cleanup()
    if (originalMatchMedia) {
      Object.defineProperty(window, 'matchMedia', {
        writable: true,
        configurable: true,
        value: originalMatchMedia,
      })
    }
    window.open = originalOpen
    vi.restoreAllMocks()
  })

  it('渲染按钮且 aria-label 包含 title 前 30 字', () => {
    const longTitle = '这是一个非常非常非常非常非常非常非常非常非常非常长的标题超过三十个字应该被截断'
    const { container } = render(<ExpandButton itemId="abc-123" title={longTitle} />)

    const btn = screen.getByRole('button')
    expect(btn).toBeInTheDocument()
    const label = btn.getAttribute('aria-label') || ''
    // 前 30 字
    expect(label).toContain(longTitle.slice(0, 30))
    // 超长尾巴
    expect(label).toContain('...')
    // 整体壳 — BF-0420-1 改为 "放大查看"(对齐用户心智 + 弹窗场景语义)
    expect(label.startsWith('放大查看「')).toBe(true)
    expect(container.querySelector('.lucide-maximize-2')).toBeInTheDocument()
  })

  it('点击按钮时 stopPropagation 生效:外层容器 click handler 不被调用', async () => {
    const outerClick = vi.fn()
    // 让 open mock 返回 truthy window 避免走到 hash fallback
    window.open = vi.fn().mockReturnValue({} as Window) as typeof window.open

    const user = userEvent.setup()
    render(
      <div onClick={outerClick} data-testid="outer">
        <ExpandButton itemId="abc-123" title="Hello" />
      </div>,
    )

    await user.click(screen.getByRole('button'))
    expect(outerClick).not.toHaveBeenCalled()
  })

  it('桌面(非触控)点击时调用 window.open,URL 形如信息页 item 弹窗深链', async () => {
    const openSpy = vi.fn().mockReturnValue({} as Window)
    window.open = openSpy as typeof window.open
    mockPointerCoarse(false)

    const user = userEvent.setup()
    render(<ExpandButton itemId="my item/1" title="Hi" />)
    await user.click(screen.getByRole('button'))

    expect(openSpy).toHaveBeenCalledTimes(1)
    const [calledUrl, target, features] = openSpy.mock.calls[0]
    expect(calledUrl).toMatch(/\/#v=info&d=my%20item%2F1$/)
    expect(target).toBe('_blank')
    expect(features).toBe('noopener,noreferrer')
  })

  it('触控设备(matchMedia pointer:coarse)不调用 window.open,改设 location.hash', async () => {
    mockPointerCoarse(true)
    const openSpy = vi.fn().mockReturnValue({} as Window)
    window.open = openSpy as typeof window.open

    const user = userEvent.setup()
    render(<ExpandButton itemId="touch-1" title="Hi" />)
    await user.click(screen.getByRole('button'))

    expect(openSpy).not.toHaveBeenCalled()
    expect(window.location.hash).toBe('#v=info&d=touch-1')
  })

  it('BF-0420-2 二轮:window.open 返回 null(Chrome noopener 模式)时,绝不修改 location.hash', async () => {
    // 真实 Chrome 在 noopener 模式下 window.open 返回 null,即使窗口成功打开
    mockPointerCoarse(false)
    const openSpy = vi.fn().mockReturnValue(null)
    window.open = openSpy as typeof window.open
    window.location.hash = '#v=starred'  // 模拟用户当前在收藏页

    const user = userEvent.setup()
    render(<ExpandButton itemId="shouldnt-navigate" title="X" />)
    await user.click(screen.getByRole('button'))

    expect(openSpy).toHaveBeenCalledTimes(1)
    // 关键断言:原 tab 的 hash 保持原样,不被改成 item 深链。
    expect(window.location.hash).toBe('#v=starred')
  })
})
