import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ChannelsView } from '../ChannelsView'
import { fetchFeedPlatformMore, fetchFeedPlatforms, fetchLingowhaleGroups } from '../../../lib/api'
import { useFeedStore } from '../../../store/feedStore'
import { useUIStore } from '../../../store/uiStore'
import type { FeedItem } from '../../../lib/types'

vi.mock('../../../lib/api', () => ({
  fetchFeedPlatforms: vi.fn(),
  fetchFeedPlatformMore: vi.fn(),
  fetchLingowhaleGroups: vi.fn(),
}))

// v24 §21.3: SectionFront 用 IO 做行级懒渲染，测试里让 section 立即可见
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

class ImmediateIntersectionObserverMock {
  private callback: IntersectionObserverCallback

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback
  }

  observe(target: Element) {
    this.callback([{ isIntersecting: true, target } as IntersectionObserverEntry], this as unknown as IntersectionObserver)
  }

  disconnect() {}
}

const mockFetchPlatformMore = fetchFeedPlatformMore as unknown as ReturnType<typeof vi.fn>
const mockFetchPlatforms = fetchFeedPlatforms as unknown as ReturnType<typeof vi.fn>
const mockFetchLingowhaleGroups = fetchLingowhaleGroups as unknown as ReturnType<typeof vi.fn>

function item(id: string, platform: string, source?: string): FeedItem {
  return {
    id,
    platform,
    source,
    title: `Item ${id}`,
    fetched_at: '2026-05-21T00:00:00Z',
  }
}

function items(prefix: string, platform: string, count: number, source?: string): FeedItem[] {
  return Array.from({ length: count }, (_, index) => item(`${prefix}-${index + 1}`, platform, source))
}

function resetFeedStore() {
  useFeedStore.setState({
    platformSectionItems: new Map(),
    platformCounts: {},
    sourceCounts: {},
    platformCategoryCounts: {},
    platformSectionsLoaded: true,
    platformReadModelVersionId: null,
    platformNextCursors: {},
    selectedCategory: {},
    clickedAtById: {},
    classification: {
      categories: [
        { id: 'products', name: '产品', visible: true, priority: 1 },
        { id: 'coding', name: 'Coding', visible: true, priority: 2 },
      ],
    },
    loadError: null,
    searchResults: null,
    searchPlatformSectionItems: null,
    searchPlatformCounts: {},
    searchSourceCounts: {},
    searchPlatformCategoryCounts: {},
    searchPlatformLoading: false,
    isSearching: false,
  })
  useUIStore.setState({ expandedKey: null, searchQuery: '' })
}

describe('ChannelsView section-local pills', () => {
  beforeEach(() => {
    vi.stubGlobal('IntersectionObserver', IntersectionObserverMock)
    mockFetchPlatformMore.mockReset()
    mockFetchPlatforms.mockReset()
    mockFetchLingowhaleGroups.mockReset()
    mockFetchPlatformMore.mockResolvedValue({ items: [], total: 0 })
    resetFeedStore()
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('X section 使用 L1 内容分类 pill，不展示关注/推荐/书签 source pill', async () => {
    const scrollTo = vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 100 })
    useFeedStore.setState({
      platformSectionItems: new Map([[
        'twitter',
        [item('x1', 'twitter', 'following'), item('x2', 'twitter', 'for_you')],
      ]]),
      platformCounts: { twitter: 2 },
      sourceCounts: { twitter: { following: 1, for_you: 1 } },
      platformCategoryCounts: { twitter: { products: 1, coding: 1 } },
    })
    const user = userEvent.setup()

    render(<ChannelsView embedded />)
    const section = document.getElementById('s-twitter')!
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

    const allButton = screen.getByRole('button', { name: '全部' })
    const productButton = screen.getByRole('button', { name: '产品' })
    expect(screen.getByTestId('info-section-pill-bar-twitter').className).not.toContain('sticky')
    expect(allButton.className).toContain('border-[var(--brand)]')
    expect(allButton.className).not.toContain('rounded-full')
    expect(allButton.className).not.toContain('bg-foreground')
    expect(productButton.className).toContain('border-transparent')
    expect(screen.queryByRole('button', { name: '关注' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '推荐' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '书签' })).not.toBeInTheDocument()

    await user.click(productButton)

    await waitFor(() => {
      expect(mockFetchPlatformMore).toHaveBeenCalledWith(
        'twitter',
        0,
        50,
        undefined,
        undefined,
        'products',
        undefined,
      )
    })
    expect(scrollTo).not.toHaveBeenCalled()
  })

  it('L1 pill 请求失败时保留当前卡片，不把 section 写成空白', async () => {
    useFeedStore.setState({
      platformSectionItems: new Map([[
        'twitter',
        [item('x1', 'twitter', 'following'), item('x2', 'twitter', 'for_you')],
      ]]),
      platformCounts: { twitter: 40 },
      sourceCounts: { twitter: { following: 12, for_you: 28 } },
      platformCategoryCounts: { twitter: { products: 12, coding: 28 } },
    })
    mockFetchPlatformMore.mockRejectedValueOnce(new Error('read model timeout'))
    const user = userEvent.setup()

    render(<ChannelsView embedded />)
    await user.click(screen.getByRole('button', { name: '产品' }))

    await waitFor(() => expect(mockFetchPlatformMore).toHaveBeenCalled())
    expect(screen.getByText('12 条')).toBeInTheDocument()
    expect(screen.getByText('Item x1')).toBeInTheDocument()
    expect(screen.getByText('Item x2')).toBeInTheDocument()
    expect(screen.queryByText('0 条')).not.toBeInTheDocument()
  })

  it('L1 pill 返回 degraded 时保留当前卡片，不把 section 写成空白', async () => {
    useFeedStore.setState({
      platformSectionItems: new Map([[
        'twitter',
        [item('x1', 'twitter', 'following'), item('x2', 'twitter', 'for_you')],
      ]]),
      platformCounts: { twitter: 40 },
      sourceCounts: { twitter: { following: 12, for_you: 28 } },
      platformCategoryCounts: { twitter: { products: 12, coding: 28 } },
    })
    mockFetchPlatformMore.mockResolvedValueOnce({
      items: [],
      platform: 'twitter',
      total: 0,
      degraded: true,
      degraded_reason: 'read_model_timeout',
    })
    const user = userEvent.setup()

    render(<ChannelsView embedded />)
    await user.click(screen.getByRole('button', { name: '产品' }))

    await waitFor(() => expect(mockFetchPlatformMore).toHaveBeenCalled())
    expect(screen.getByText('12 条')).toBeInTheDocument()
    expect(screen.getByText('Item x1')).toBeInTheDocument()
    expect(screen.getByText('Item x2')).toBeInTheDocument()
    expect(screen.queryByText('0 条')).not.toBeInTheDocument()
  })

  it('公众号 section 使用 L1 内容分类 pill，不再拉取分组 pill 数据', async () => {
    useFeedStore.setState({
      platformSectionItems: new Map([[
        'lingowhale',
        [item('l1', 'lingowhale', 'Alpha-公众号')],
      ]]),
      platformCounts: { lingowhale: 1 },
      platformCategoryCounts: { lingowhale: { products: 1, coding: 1 } },
    })
    const user = userEvent.setup()
    vi.spyOn(window, 'scrollTo').mockImplementation(() => {})

    render(<ChannelsView embedded />)
    expect(screen.getAllByText('公众号').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: '产品' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'AI周报' })).not.toBeInTheDocument()
    expect(mockFetchLingowhaleGroups).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: '产品' }))

    await waitFor(() => {
      expect(mockFetchPlatformMore).toHaveBeenCalledWith(
        'lingowhale',
        0,
        50,
        undefined,
        undefined,
        'products',
        undefined,
      )
    })
  })

  it('v24.1 瀑布流回滚：首批 50 条全部渲染；展开更多请求下一页，成功追加前不虚减剩余数', async () => {
    const user = userEvent.setup()
    const firstPage = items('x-initial', 'twitter', 50)
    const secondPage = items('x-next', 'twitter', 50)
    let resolvePage: (value: unknown) => void = () => {}
    mockFetchPlatformMore.mockReturnValueOnce(new Promise((resolve) => { resolvePage = resolve }))
    useFeedStore.setState({
      platformSectionItems: new Map([['twitter', firstPage]]),
      platformCounts: { twitter: 120 },
      sourceCounts: { twitter: {} },
    })

    render(<ChannelsView embedded />)

    // 折叠态 = 前 BATCH(50) 条 masonry；不再是 lead/rail/brief 行分型折叠
    expect(screen.getAllByTestId('info-card')).toHaveLength(50)
    expect(screen.getByRole('button', { name: /展开更多/ })).toHaveTextContent('还有 70 条')

    // 点击：真正取下一页
    await user.click(screen.getByRole('button', { name: /展开更多/ }))

    await waitFor(() => {
      expect(mockFetchPlatformMore).toHaveBeenCalledWith(
        'twitter',
        0,
        50,
        undefined,
        undefined,
        undefined,
        undefined,
        firstPage.map((entry) => entry.id),
      )
    })
    expect(screen.queryByText('加载更多')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /展开更多/ })).toHaveTextContent('还有 70 条')

    resolvePage({
      items: secondPage,
      platform: 'twitter',
      total: 120,
      offset: 50,
      limit: 50,
      has_more: true,
      next_offset: 100,
    })

    await waitFor(() => expect(screen.getAllByTestId('info-card')).toHaveLength(100))
    expect(screen.getByRole('button', { name: /展开更多/ })).toHaveTextContent('还有 20 条')
  })

  it('重复点击已加载的 L1 pill 时直接复用首批缓存，不重复请求远端', async () => {
    const user = userEvent.setup()
    const baseItems = [item('x-all-1', 'twitter', 'following'), item('x-all-2', 'twitter', 'for_you')]
    const followingItems = [item('x-following-1', 'twitter', 'following')]
    mockFetchPlatformMore.mockResolvedValueOnce({
      items: followingItems,
      platform: 'twitter',
      total: 1,
      offset: 0,
      limit: 50,
      has_more: false,
      next_offset: null,
    })
    useFeedStore.setState({
      platformSectionItems: new Map([['twitter', baseItems]]),
      platformCounts: { twitter: 2 },
      sourceCounts: { twitter: { following: 1, for_you: 1 } },
      platformCategoryCounts: { twitter: { products: 1, coding: 1 } },
    })

    render(<ChannelsView embedded />)

    await user.click(screen.getByRole('button', { name: '产品' }))

    await waitFor(() => expect(screen.getByText('Item x-following-1')).toBeInTheDocument())
    expect(mockFetchPlatformMore).toHaveBeenCalledTimes(1)

    await user.click(screen.getByRole('button', { name: '全部' }))
    expect(screen.getByText('Item x-all-1')).toBeInTheDocument()

    mockFetchPlatformMore.mockClear()
    await user.click(screen.getByRole('button', { name: '产品' }))

    expect(screen.getByText('Item x-following-1')).toBeInTheDocument()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(mockFetchPlatformMore).not.toHaveBeenCalled()
  })

  it('后台展开预取进入视口后延迟启动，避免立即抢占 pill 点击请求', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('IntersectionObserver', ImmediateIntersectionObserverMock)
    const firstPage = items('x-initial', 'twitter', 50)
    useFeedStore.setState({
      platformSectionItems: new Map([['twitter', firstPage]]),
      platformCounts: { twitter: 120 },
      sourceCounts: { twitter: {} },
    })

    render(<ChannelsView embedded />)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1199)
    })
    expect(mockFetchPlatformMore).not.toHaveBeenCalled()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
    })

    expect(mockFetchPlatformMore).toHaveBeenCalledWith(
      'twitter',
      0,
      50,
      undefined,
      undefined,
      undefined,
      undefined,
      firstPage.map((entry) => entry.id),
    )
  })

  it('read model 首页有版本时，后台预取使用 cursor 而不是 exclude_ids', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('IntersectionObserver', ImmediateIntersectionObserverMock)
    const firstPage = items('x-initial', 'twitter', 50)
    useFeedStore.setState({
      platformSectionItems: new Map([['twitter', firstPage]]),
      platformCounts: { twitter: 120 },
      sourceCounts: { twitter: {} },
      platformReadModelVersionId: 'platform-rm-1',
      platformNextCursors: {
        twitter: {
          version_id: 'platform-rm-1',
          scope_key: 'platform=twitter|dimension=all|value=',
          rank_after: 49,
        },
      },
    })

    render(<ChannelsView embedded />)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1200)
    })

    expect(mockFetchPlatformMore).toHaveBeenCalledWith(
      'twitter',
      50,
      50,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      {
        version_id: 'platform-rm-1',
        scope_key: 'platform=twitter|dimension=all|value=',
        rank_after: 49,
      },
    )
  })
})
