import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { InfoSidebar } from '../InfoSidebar'
import { useFeedStore } from '../../../store/feedStore'
import { useUIStore } from '../../../store/uiStore'

describe('InfoSidebar', () => {
  beforeEach(() => {
    // FE-8(Wave C): 滚动监听仅在 info tab 激活时挂载,测试需显式置位
    useUIStore.setState({ l1: 'info' })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    cleanup()
    document.body.innerHTML = ''
    useFeedStore.setState({
      classification: null,
      catCounts: {},
      platformCounts: {},
      searchResults: null,
      searchCatCounts: {},
      searchPlatformSectionItems: null,
      searchPlatformCounts: {},
    })
  })

  it('使用居中的 L2 单行吸顶 rail，而不是左侧固定列或双行布局', () => {
    render(<InfoSidebar groupBy="platform" onGroupByChange={vi.fn()} />)
    const subbar = screen.getByTestId('info-subbar')
    expect(subbar.className).toContain('sticky')
    expect(subbar.className).toContain('top-[84px]')
    expect(subbar.className).toContain('sm:top-[52px]')
    expect(subbar.className).toContain('bg-background')
    expect(subbar.className).toContain('h-10')
    expect(subbar.className).toContain('py-0')
    expect(subbar.className).toContain('mb-0')
    expect(subbar.className).not.toContain('border-b')
    expect(subbar.className).not.toContain('mb-5')
    expect(subbar.className).not.toContain('backdrop-blur')

    const inner = screen.getByTestId('info-subbar-inner')
    expect(inner.className).toContain('flex')
    expect(inner.className).toContain('justify-center')
    expect(inner.className).toContain('border-b')
    expect(inner.className).toContain('border-border/70')
    expect(inner.className).toContain('max-w-[1168px]')
    expect(inner.className).not.toContain('grid')
    expect(inner.className).not.toContain('grid-cols-[auto_minmax(0,1fr)]')
    expect(inner.className).toContain('items-center')
    const rail = screen.getByTestId('info-l2-rail')
    expect(rail.className).toContain('justify-center')
    expect(rail.className).toContain('gap-6')
    expect(rail.className).toContain('sm:gap-8')
    const groupByRow = screen.getByTestId('info-groupby-row')
    expect(groupByRow.className).toContain('shrink-0')
    expect(rail.children[0]).toBe(groupByRow)
    expect(rail.children[1]).toBe(screen.getByTestId('info-l2-divider'))
    expect(rail.children[2]).toBe(screen.getByTestId('info-group-nav-shell'))
    expect(screen.getByRole('button', { name: '来源' }).className).toContain('border-b-2')
    expect(screen.getByRole('button', { name: '来源' }).className).not.toContain('rounded-full')
    expect(screen.queryByText('｜')).toBeNull()
    const divider = screen.getByTestId('info-l2-divider')
    expect(divider).toHaveTextContent('|')
    expect(divider.className).toContain('font-event-title')
    expect(divider.className).toContain('text-[16px]')
    expect(screen.queryByTestId('info-group-divider')).toBeNull()

    const shell = screen.getByTestId('info-group-nav-shell')
    expect(shell.className).toContain('max-w-full')
    const nav = screen.getByTestId('info-group-nav')
    expect(nav.className).toContain('overflow-x-auto')
    expect(nav.className).toContain('scrollbar-hide')
    expect(nav.className).toContain('gap-6')
    expect(nav.className).toContain('justify-center')
    expect(nav.className).not.toContain('border-b')
    expect(screen.queryByTestId('info-group-nav-spacer-start')).toBeNull()
    expect(screen.queryByTestId('info-group-nav-spacer-end')).toBeNull()
    expect(screen.queryByTestId('info-subbar-right-spacer')).toBeNull()
    expect(screen.queryByTestId('info-group-fade-left')).toBeNull()
    expect(screen.queryByTestId('info-group-fade-right')).toBeNull()
    expect(screen.getByTestId('info-group-twitter').className).toContain('border-b-2')
    expect(screen.getByTestId('info-group-twitter').className).toContain('border-[var(--brand)]')
    expect(screen.getByTestId('info-group-twitter').className).not.toContain('rounded-full')
    expect(screen.queryByTestId('info-group-chevron-left')).toBeNull()
    expect(screen.queryByTestId('info-group-chevron-right')).toBeNull()
  })

  it('只有 L2 rail 真实 overflow 时才渲染纯文字 chevron 和边缘 fade', async () => {
    render(<InfoSidebar groupBy="platform" onGroupByChange={vi.fn()} />)
    const nav = screen.getByTestId('info-group-nav')
    Object.defineProperty(nav, 'clientWidth', { configurable: true, value: 120 })
    Object.defineProperty(nav, 'scrollWidth', { configurable: true, value: 360 })
    Object.defineProperty(nav, 'scrollLeft', { configurable: true, value: 0 })

    act(() => {
      window.dispatchEvent(new Event('resize'))
    })

    await waitFor(() => {
      expect(screen.getByTestId('info-group-chevron-left').querySelector('svg')).toBeInTheDocument()
      expect(screen.getByTestId('info-group-chevron-right').querySelector('svg')).toBeInTheDocument()
    })
    expect(screen.getByTestId('info-group-chevron-left').className).toContain('bg-transparent')
    expect(screen.getByTestId('info-group-chevron-left').className).toContain('top-1/2')
    expect(screen.getByTestId('info-group-chevron-left').className).toContain('-translate-y-1/2')
    expect(screen.getByTestId('info-group-chevron-left').className).not.toContain('rounded-full')
    expect(screen.getByTestId('info-group-chevron-left').className).not.toContain('shadow')
    expect(screen.getByTestId('info-group-chevron-right').className).toContain('bg-transparent')
    expect(screen.getByTestId('info-group-chevron-right').className).toContain('top-1/2')
    expect(screen.getByTestId('info-group-chevron-right').className).toContain('-translate-y-1/2')
    expect(screen.getByTestId('info-group-chevron-right').className).not.toContain('rounded-full')
    expect(screen.getByTestId('info-group-chevron-right').className).not.toContain('shadow')
    expect(screen.getByTestId('info-group-fade-left')).toBeInTheDocument()
    expect(screen.getByTestId('info-group-fade-right')).toBeInTheDocument()
    expect(nav.className).toContain('justify-start')
  })

  it('页面滚动到某个 section 时，L2 pill 自动切换到该 section', async () => {
    const sectionRects: Record<string, number> = {
      twitter: -800,
      lingowhale: -420,
      waytoagi: -120,
      github: 80,
      reddit: 720,
    }
    for (const [key, top] of Object.entries(sectionRects)) {
      const el = document.createElement('section')
      el.id = `s-${key}`
      el.getBoundingClientRect = () => ({
        x: 0,
        y: top,
        left: 0,
        right: 100,
        top,
        bottom: top + 360,
        width: 100,
        height: 360,
        toJSON: () => ({}),
      } as DOMRect)
      document.body.appendChild(el)
    }

    render(<InfoSidebar groupBy="platform" onGroupByChange={vi.fn()} />)

    await waitFor(() => {
      expect(screen.getByTestId('info-group-github')).toHaveAttribute('aria-current', 'true')
    })
    expect(screen.getByTestId('info-group-twitter')).not.toHaveAttribute('aria-current')
  })

  it('点击评测后会在 section 高度变化后重新对齐评测锚点', () => {
    vi.useFakeTimers()
    let scrollY = 0
    Object.defineProperty(window, 'scrollY', {
      configurable: true,
      get: () => scrollY,
    })
    const scrollTo = vi.spyOn(window, 'scrollTo').mockImplementation(((options?: ScrollToOptions | number, y?: number) => {
      if (typeof options === 'object') {
        scrollY = Number(options.top ?? scrollY)
      } else if (typeof y === 'number') {
        scrollY = y
      }
    }) as typeof window.scrollTo)
    useFeedStore.setState({
      classification: {
        categories: [
          { id: 'products', name: '产品', visible: true, priority: 1 },
          { id: 'models', name: '模型', visible: true, priority: 5 },
          { id: 'eval', name: '评测', visible: true, priority: 6 },
        ],
      },
    })
    const absoluteTops: Record<string, number> = {
      products: 0,
      models: 720,
      eval: 1600,
    }
    for (const key of Object.keys(absoluteTops)) {
      const el = document.createElement('section')
      el.id = `s-${key}`
      el.getBoundingClientRect = () => {
        const top = absoluteTops[key] - scrollY
        return {
          x: 0,
          y: top,
          left: 0,
          right: 100,
          top,
          bottom: top + 360,
          width: 100,
          height: 360,
          toJSON: () => ({}),
        } as DOMRect
      }
      document.body.appendChild(el)
    }

    render(<InfoSidebar groupBy="category" onGroupByChange={vi.fn()} />)
    screen.getByTestId('info-subbar').getBoundingClientRect = () => ({
      x: 0,
      y: 60,
      left: 0,
      right: 100,
      top: 60,
      bottom: 100,
      width: 100,
      height: 40,
      toJSON: () => ({}),
    } as DOMRect)

    fireEvent.click(screen.getByTestId('info-group-eval'))

    expect(screen.getByTestId('info-group-eval')).toHaveAttribute('aria-current', 'true')
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 1500, behavior: 'smooth' })

    absoluteTops.eval = 1820
    act(() => {
      vi.advanceTimersByTime(160)
    })

    expect(scrollTo).toHaveBeenLastCalledWith({ top: 1720, behavior: 'auto' })
    expect(screen.getByTestId('info-group-eval')).toHaveAttribute('aria-current', 'true')
  })

  it('按频道模式使用固定平台目录且不在 pill 内展示数量', () => {
    useFeedStore.setState({
      platformCounts: { twitter: 36, lingowhale: 24, github: 18 },
    })

    render(<InfoSidebar groupBy="platform" onGroupByChange={vi.fn()} />)
    const xButton = screen.getByTestId('info-group-twitter')
    expect(xButton).toBeInTheDocument()
    expect(xButton).toHaveTextContent('X')
    expect(xButton.className).toContain('h-full')
    expect(xButton.className).toContain('border-b-2')
    expect(xButton.className).toContain('font-event-title')
    expect(xButton.className).toContain('text-[16px]')
    expect(xButton.className).not.toContain('rounded-full')
    expect(xButton.className).not.toContain('bg-[var(--brand-soft)]')
    expect(screen.getByTestId('info-group-lingowhale')).toHaveTextContent('公众号')
    expect(screen.getByTestId('info-group-github')).toHaveTextContent('GitHub')
    expect(screen.getByTestId('info-group-lingowhale')).not.toHaveTextContent('24')
    expect(screen.getByTestId('info-group-github')).not.toHaveTextContent('18')
    expect(screen.queryByText(/条/)).toBeNull()
  })

  it('分类配置未加载时仍保留顶部分类 pill 骨架', () => {
    render(<InfoSidebar groupBy="category" onGroupByChange={vi.fn()} />)
    expect(screen.getByTestId('info-group-products')).toBeInTheDocument()
    expect(screen.getByTestId('info-group-efficiency_tools')).toBeInTheDocument()
    expect(screen.getByTestId('info-group-models')).toBeInTheDocument()
    expect(screen.getByTestId('info-group-eval')).toHaveTextContent('评测')
    expect(screen.queryByTestId('info-group-other')).toBeNull()
  })

  it('按分类模式在搜索态仍保留 classification 全部分组', () => {
    useFeedStore.setState({
      classification: {
        categories: [
          { id: 'products', name: '产品', visible: true, priority: 1 },
          { id: 'tools', name: '工具', visible: true, priority: 2 },
          { id: 'other', name: '其他', visible: true, priority: 99 },
        ],
      },
      searchResults: new Map([['products', []]]),
      searchCatCounts: { products: 0 },
    })

    render(<InfoSidebar groupBy="category" onGroupByChange={vi.fn()} />)
    expect(screen.getByTestId('info-group-products')).toHaveTextContent('产品')
    expect(screen.getByTestId('info-group-tools')).toBeInTheDocument()
    expect(screen.queryByTestId('info-group-other')).toBeNull()
    expect(screen.queryByText(/条/)).toBeNull()
  })
})
