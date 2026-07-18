import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { FeedSection } from '../FeedSection'
import { useFeedStore, useSectionItems } from '../../../store/feedStore'
import { useUIStore } from '../../../store/uiStore'
import type { FeedItem, FeedSection as FeedSectionType } from '../../../lib/types'
import { fetchFeedSectionMore } from '../../../lib/api'

vi.mock('../../../lib/api', () => ({
  fetchFeedSectionMore: vi.fn(),
}))

class IntersectionObserverMock {
  private callback: IntersectionObserverCallback

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback
  }

  observe(target: Element) {
    this.callback([{ isIntersecting: true, target } as IntersectionObserverEntry], this as unknown as IntersectionObserver)
  }

  disconnect() {}
}

const mockFetchSectionMore = fetchFeedSectionMore as unknown as ReturnType<typeof vi.fn>

function makeItem(id: string): FeedItem {
  return {
    id,
    title: `Item ${id}`,
    platform: 'twitter',
    fetched_at: '2026-04-30T00:00:00Z',
    ai_category: 'products',
  }
}

function makeSection(overrides: Partial<FeedSectionType> = {}): FeedSectionType {
  return {
    key: 'products',
    label: '产品',
    items: [makeItem('a')],
    count: 137,
    ...overrides,
  }
}

function makeItems(prefix: string, count: number): FeedItem[] {
  return Array.from({ length: count }, (_, index) => makeItem(`${prefix}-${index + 1}`))
}

function FeedSectionHarness() {
  const section = useSectionItems().find((item) => item.key === 'products')
  return section ? <FeedSection section={section} /> : null
}

describe('FeedSection', () => {
  beforeEach(() => {
    vi.stubGlobal('IntersectionObserver', IntersectionObserverMock)
    vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
    mockFetchSectionMore.mockReset()
    mockFetchSectionMore.mockResolvedValue({
      items: [],
      category: 'products',
      total: 137,
    })
    useUIStore.setState({ expandedKey: null })
    useFeedStore.setState({
      sectionItems: new Map(),
      catCounts: {},
      searchResults: null,
      searchCatCounts: {},
      sectionReadModelVersionId: null,
      sectionNextCursors: {},
      classification: {
        categories: [
          {
            id: 'products',
            name: '产品',
            visible: true,
            subcategories: [
              { id: 'ai_video', name: 'AI 视频' },
              { id: 'ai_search', name: 'AI 搜索' },
            ],
          },
        ],
      },
    })
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('section header 不再渲染单 section 刷新按钮', () => {
    render(<FeedSection section={makeSection()} />)
    expect(screen.queryByTitle('刷新')).not.toBeInTheDocument()
  })

  it('section 局部筛选使用下划线样式，不再作为 sticky 第三级 tab', () => {
    render(<FeedSection section={makeSection()} />)
    const bar = screen.getByTestId('info-section-pill-bar')
    const allButton = screen.getByRole('button', { name: '全部' })
    const videoButton = screen.getByRole('button', { name: 'AI 视频' })

    expect(bar.className).not.toContain('sticky')
    expect(bar.className).toContain('bg-background')
    expect(allButton.className).toContain('border-b-2')
    expect(allButton.className).toContain('border-[var(--brand)]')
    expect(allButton.className).toContain('text-[var(--brand)]')
    expect(allButton.className).toContain('font-event-title')  // v24.2: pill 字体对齐 topbar 导航(16px 衬线)
    expect(allButton.className).toContain('text-[16px]')
    expect(allButton.className).not.toContain('rounded-full')
    expect(allButton.className).not.toContain('bg-foreground')
    expect(videoButton.className).toContain('border-transparent')
    expect(videoButton.className).toContain('text-muted-foreground')
    expect(videoButton.className).not.toContain('hover:bg-muted')
  })

  it('信息页 Image2 嵌入态可以隐藏 section 标题和 L2 筛选', () => {
    render(<FeedSection section={makeSection()} showHeader={false} showSubcategoryFilters={false} />)

    expect(screen.queryByText('137 条')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '全部' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'AI 视频' })).not.toBeInTheDocument()
  })

  it('切换 L2 pill 后 header 只展示筛选条数,不展示 pill 名称', async () => {
    const user = userEvent.setup()
    mockFetchSectionMore.mockResolvedValue({
      items: [makeItem('filtered-a')],
      category: 'products',
      total: 15,
    })

    render(<FeedSection section={makeSection()} />)
    await user.click(screen.getByRole('button', { name: 'AI 视频' }))

    await waitFor(() => expect(screen.getByText('15 条')).toBeInTheDocument())
    expect(screen.queryByText('AI 视频 15 条')).not.toBeInTheDocument()
  })

  it('L2 pill 结果中的卡片会叠加本地已点击状态（v24 墨水降档,不再整卡 opacity-40）', async () => {
    const user = userEvent.setup()
    mockFetchSectionMore.mockResolvedValue({
      items: [makeItem('filtered-a')],
      category: 'products',
      total: 15,
    })

    render(<FeedSection section={makeSection()} />)
    await user.click(screen.getByRole('button', { name: 'AI 视频' }))

    await waitFor(() => expect(screen.getByText('Item filtered-a')).toBeInTheDocument())
    expect(screen.getByTestId('info-card')).toHaveAttribute('data-read', 'false')
    expect(screen.getByTestId('info-card-title').className).toContain('text-foreground')

    act(() => {
      useFeedStore.getState().markClicked('filtered-a')
    })

    await waitFor(() => expect(screen.getByTestId('info-card')).toHaveAttribute('data-read', 'true'))
    expect(screen.getByTestId('info-card').className).not.toContain('opacity-40')
    expect(screen.getByTestId('info-card-title').className).toContain('text-muted-foreground')
  })

  it('切换 section 局部 pill 后不主动滚动页面', async () => {
    const user = userEvent.setup()
    const scrollTo = vi.mocked(window.scrollTo)
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 100 })
    mockFetchSectionMore.mockResolvedValue({
      items: [makeItem('filtered-a')],
      category: 'products',
      total: 15,
    })

    render(<FeedSection section={makeSection()} />)
    const section = document.getElementById('s-products')!
    section.getBoundingClientRect = () => ({
      x: 0,
      y: 0,
      left: 0,
      right: 0,
      top: 360,
      bottom: 760,
      width: 0,
      height: 400,
      toJSON: () => ({}),
    } as DOMRect)

    await user.click(screen.getByRole('button', { name: 'AI 视频' }))

    expect(scrollTo).not.toHaveBeenCalled()
  })

  it('切换 L2 pill 时不展示“搜索中…”', async () => {
    const user = userEvent.setup()
    mockFetchSectionMore.mockReturnValue(new Promise(() => {}))

    render(<FeedSection section={makeSection()} />)
    await user.click(screen.getByRole('button', { name: 'AI 视频' }))

    expect(screen.queryByText('搜索中…')).not.toBeInTheDocument()
  })

  it('L2 pill 请求失败时保留原 section 卡片和真实总数，不写成假空', async () => {
    const user = userEvent.setup()
    mockFetchSectionMore.mockImplementation((_category, _offset, _limit, _keyword, subcategory) => {
      if (subcategory === 'ai_video') return Promise.reject(new Error('read model timeout'))
      return Promise.resolve({ items: [], category: 'products', total: 137 })
    })

    render(<FeedSection section={makeSection()} />)
    await user.click(screen.getByRole('button', { name: 'AI 视频' }))

    await waitFor(() => expect(mockFetchSectionMore).toHaveBeenCalled())
    expect(screen.getByText('137 条')).toBeInTheDocument()
    expect(screen.getByText('Item a')).toBeInTheDocument()
    expect(screen.queryByText('0 条')).not.toBeInTheDocument()
  })

  it('L2 pill 返回 degraded 时保留原 section 卡片和真实总数，不写成假空', async () => {
    const user = userEvent.setup()
    mockFetchSectionMore.mockImplementation((_category, _offset, _limit, _keyword, subcategory) => {
      if (subcategory === 'ai_video') {
        return Promise.resolve({
          items: [],
          category: 'products',
          total: 0,
          degraded: true,
          degraded_reason: 'read_model_timeout',
        })
      }
      return Promise.resolve({ items: [], category: 'products', total: 137 })
    })

    render(<FeedSection section={makeSection()} />)
    await user.click(screen.getByRole('button', { name: 'AI 视频' }))

    await waitFor(() => expect(mockFetchSectionMore).toHaveBeenCalled())
    expect(screen.getByText('137 条')).toBeInTheDocument()
    expect(screen.getByText('Item a')).toBeInTheDocument()
    expect(screen.queryByText('0 条')).not.toBeInTheDocument()
  })

  it('连续切换 L2 pill 时不沿用上一个筛选条数', async () => {
    const user = userEvent.setup()
    mockFetchSectionMore.mockImplementation((_category, _offset, _limit, _keyword, subcategory) => {
      if (subcategory === 'ai_video') {
        return Promise.resolve({
          items: [makeItem('filtered-a')],
          category: 'products',
          total: 15,
        })
      }
      if (subcategory === 'ai_search') {
        return new Promise(() => {})
      }
      return Promise.resolve({
        items: [],
        category: 'products',
        total: 137,
      })
    })

    render(<FeedSection section={makeSection()} />)
    await user.click(screen.getByRole('button', { name: 'AI 视频' }))
    await waitFor(() => expect(screen.getByText('15 条')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'AI 搜索' }))

    expect(screen.queryByText('15 条')).not.toBeInTheDocument()
    expect(screen.getByText('137 条')).toBeInTheDocument()
  })

  it('v24.1 瀑布流回滚：首批 50 条全部渲染进 masonry，展开按钮显示剩余数', () => {
    const firstPage = makeItems('initial', 50)
    useFeedStore.setState({
      sectionItems: new Map([['products', firstPage]]),
      catCounts: { products: 120 },
    })

    render(<FeedSectionHarness />)

    // 折叠态 = 前 BATCH(50) 条 masonry + 裁切蒙版；不再是 lead/rail/brief 行分型
    const cards = screen.getAllByTestId('info-card')
    expect(cards).toHaveLength(50)
    cards.forEach((card) => expect(card).not.toHaveAttribute('data-variant'))
    expect(screen.getByTestId('masonry-columns')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /展开更多/ })).toHaveTextContent('还有 70 条')
  })

  it('展开更多命中预取时仍追加在已有卡片后面', async () => {
    const user = userEvent.setup()
    const firstPage = makeItems('initial', 50)
    const secondPage = makeItems('next', 50)
    mockFetchSectionMore.mockResolvedValueOnce({
      items: secondPage,
      category: 'products',
      total: 120,
      offset: 50,
      limit: 50,
      has_more: true,
      next_offset: 100,
    })
    useFeedStore.setState({
      sectionItems: new Map([['products', firstPage]]),
      catCounts: { products: 120 },
    })

    render(<FeedSectionHarness />)

    await waitFor(() => {
      expect(mockFetchSectionMore).toHaveBeenCalledWith(
        'products',
        50,
        50,
        undefined,
        undefined,
        undefined,
      )
    })

    const beforeTitles = screen.getAllByTestId('info-card').map((card) => card.textContent)
    expect(beforeTitles).toHaveLength(50)

    await user.click(screen.getByRole('button', { name: /展开更多/ }))

    await waitFor(() => expect(screen.getAllByTestId('info-card')).toHaveLength(100))
    const afterTitles = screen.getAllByTestId('info-card').map((card) => card.textContent)
    for (const title of beforeTitles) {
      expect(afterTitles).toContain(title)
    }
    expect(afterTitles.filter((title) => title?.includes('Item next-'))).toHaveLength(50)
    expect(useFeedStore.getState().sectionItems.get('products')?.map((item) => item.id)).toEqual([
      ...firstPage.map((item) => item.id),
      ...secondPage.map((item) => item.id),
    ])
    expect(screen.queryByText('加载更多')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /展开更多/ })).toHaveTextContent('还有 20 条')
  })

  it('展开更多直接拉下一页并追加真实卡片，成功前不虚减剩余数', async () => {
    const user = userEvent.setup()
    const firstPage = makeItems('initial', 50)
    const secondPage = makeItems('next', 50)
    let resolvePage: (value: unknown) => void = () => {}
    mockFetchSectionMore.mockReturnValueOnce(new Promise((resolve) => { resolvePage = resolve }))
    useFeedStore.setState({
      sectionItems: new Map([['products', firstPage]]),
      catCounts: { products: 120 },
    })

    render(<FeedSectionHarness />)

    expect(screen.getAllByTestId('info-card')).toHaveLength(50)
    const expandButton = screen.getByRole('button', { name: /展开更多/ })
    expect(expandButton).toHaveTextContent('还有 70 条')

    await user.click(expandButton)

    await waitFor(() => {
      expect(mockFetchSectionMore).toHaveBeenCalledWith(
        'products',
        50,
        50,
        undefined,
        undefined,
        undefined,
      )
    })
    expect(screen.queryByText('加载更多')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /展开更多/ })).toHaveTextContent('还有 70 条')

    resolvePage({
      items: secondPage,
      category: 'products',
      total: 120,
      offset: 50,
      limit: 50,
      has_more: true,
      next_offset: 100,
    })

    await waitFor(() => expect(screen.getAllByTestId('info-card')).toHaveLength(100))
    expect(screen.getByRole('button', { name: /展开更多/ })).toHaveTextContent('还有 20 条')
  })

  it('read model 首页有版本时，下一页请求携带稳定 cursor', async () => {
    const firstPage = makeItems('initial', 50)
    useFeedStore.setState({
      sectionItems: new Map([['products', firstPage]]),
      catCounts: { products: 120 },
      sectionReadModelVersionId: 'rm-version-1',
      sectionNextCursors: {
        products: {
          version_id: 'rm-version-1',
          scope_key: 'platform=_all|dimension=section_category|value=products',
          rank_after: 49,
        },
      },
    })

    render(<FeedSectionHarness />)

    await waitFor(() => {
      expect(mockFetchSectionMore).toHaveBeenCalledWith(
        'products',
        50,
        50,
        undefined,
        undefined,
        undefined,
        {
          version_id: 'rm-version-1',
          scope_key: 'platform=_all|dimension=section_category|value=products',
          rank_after: 49,
        },
      )
    })
  })
})
