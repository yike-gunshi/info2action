import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { HighlightsView } from '../HighlightsView'
import { useFeedStore } from '../../../store/feedStore'
import { useEventsStore } from '../../../store/eventsStore'

vi.mock('../../events/LatestEvents', () => ({
  LatestEvents: ({ topSlot, variant }: { topSlot?: ReactNode; variant?: string }) => (
    <div data-testid="mock-latest-events" data-variant={variant}>
      {topSlot}
    </div>
  ),
}))

describe('HighlightsView v19 shell', () => {
  const originalSetFilters = useEventsStore.getState().setFilters

  afterEach(() => {
    cleanup()
    useFeedStore.setState({ classification: null })
    useEventsStore.setState({
      filters: { categories: [] },
      setFilters: originalSetFilters,
    })
    vi.restoreAllMocks()
  })

  it('精选页主体使用开放式单列时间线容器，顶部展示 L1 分类筛选', () => {
    render(<HighlightsView />)

    const shell = screen.getByTestId('highlights-view-shell')
    expect(shell.className).toContain('mx-auto')
    expect(shell.className).toContain('max-w-[1040px]')
    expect(shell.className).toContain('pt-0')
    expect(shell.className).toContain('sm:pt-0')
    expect(shell.className).toContain('pb-5')
    expect(shell.className).not.toContain('py-5')
    expect(shell.className).not.toContain('sm:py-6')
    expect(screen.getByTestId('mock-latest-events')).toHaveAttribute('data-variant', 'page')

    const tabs = screen.getByTestId('highlights-filter-tabs')
    expect(tabs).toBeInTheDocument()
    expect(tabs.className).toContain('sticky')
    expect(tabs.className).toContain('top-[var(--highlights-l2-top)]')
    expect(tabs.className).toContain('z-50')
    expect(tabs.className).toContain('bg-background')
    expect(tabs.className).not.toContain('mb-1')
    expect(tabs.className).not.toContain('sm:mb-2')
    expect(screen.getByLabelText('精选分类筛选')).toBeInTheDocument()
    const tabsInner = screen.getByTestId('highlights-filter-tabs-inner')
    expect(tabsInner.className).toContain('w-full')
    expect(tabsInner.className).toContain('min-w-0')
    expect(tabsInner.className).toContain('border-b')
    // v24.2: 筛选 tab 左对齐,作为「全部 > 日期 > 行区」缩进层级的最左基准
    expect(tabsInner.className).toContain('justify-start')
    expect(tabsInner.className).not.toContain('sm:justify-center')
    const allTab = screen.getByTestId('highlights-filter-tab-all')
    expect(allTab.className).toContain('font-event-title')
    expect(allTab.className).toContain('text-[16px]')
    expect(allTab.className).toContain('border-b-2')
  })

  it('classification 加载后按 L1 顺序展示评测分类', () => {
    useFeedStore.setState({
      classification: {
        categories: [
          { id: 'other', name: '其他', visible: true, priority: 99 },
          { id: 'coding', name: '代码', visible: true, priority: 2 },
          { id: 'products', name: '产品', visible: true, priority: 1 },
          { id: 'models', name: '模型', visible: true, priority: 3 },
          { id: 'eval', name: '评测', visible: true, priority: 4 },
        ],
      },
    })

    render(<HighlightsView />)

    expect(screen.getByTestId('highlights-filter-tabs')).toBeInTheDocument()
    expect(screen.getByTestId('highlights-filter-tab-eval')).toHaveTextContent('评测')
    expect(screen.getByTestId('highlights-filter-tab-eval').className).toContain('font-event-title')
    expect(screen.getByTestId('highlights-filter-tab-eval').className).toContain('text-[16px]')
    expect(screen.getAllByRole('tab').map((tab) => tab.textContent)).toEqual([
      '全部',
      '产品',
      '代码',
      '模型',
      '评测',
    ])
  })

  it('点击 L2 分类 tab 回到页面顶部并刷新对应分类内容', async () => {
    const user = userEvent.setup()
    const setFilters = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(window, 'scrollTo', {
      configurable: true,
      value: vi.fn(),
    })
    useFeedStore.setState({
      classification: {
        categories: [
          { id: 'products', name: '产品', visible: true, priority: 1 },
        ],
      },
    })
    useEventsStore.setState({
      filters: { categories: [] },
      setFilters,
    })

    render(<HighlightsView />)
    await user.click(screen.getByTestId('highlights-filter-tab-products'))

    expect(window.scrollTo).toHaveBeenCalledWith({ top: 0 })
    expect(setFilters).toHaveBeenCalledWith({ categories: ['products'] })
  })

  it('点击当前已选 L2 tab 仍回到顶部并刷新当前内容', async () => {
    const user = userEvent.setup()
    const setFilters = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(window, 'scrollTo', {
      configurable: true,
      value: vi.fn(),
    })
    useFeedStore.setState({
      classification: {
        categories: [
          { id: 'products', name: '产品', visible: true, priority: 1 },
        ],
      },
    })
    useEventsStore.setState({
      filters: { categories: ['products'] },
      setFilters,
    })

    render(<HighlightsView />)
    await user.click(screen.getByTestId('highlights-filter-tab-products'))

    expect(window.scrollTo).toHaveBeenCalledWith({ top: 0 })
    expect(setFilters).toHaveBeenCalledWith({ categories: ['products'] })
  })
})
