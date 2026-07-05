/**
 * v18.0 Spec-2.5（rev1）：InfoCategoryView 组件测试。
 *
 * 覆盖：
 *   - 已有 sectionItems 时 mount 不重新拉数据，立即渲染（PRD §Spec-2.5.7 持久化恢复）
 *   - sectionItems 为空时 mount 触发 fetchFeedSections（PRD §Spec-2.5.3 切到分类视角首次拉数据）
 *   - 渲染分类 sections（标题为分类中文名 / 顺序按 classification.priority）（PRD §Spec-2.5.5）
 *   - fetch 失败显示错误 + 重试按钮（PRD §Spec-2.5.E2）
 *   - onLoadingChange 回调联动（PRD §Spec-2.5.E1 切换中 disable segment toggle）
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('../../feed/FeedSection', () => ({
  FeedSection: ({
    section,
    showHeader,
    showSubcategoryFilters,
  }: {
    section: { key: string; label: string; items: unknown[]; count: number }
    showHeader?: boolean
    showSubcategoryFilters?: boolean
  }) => (
    <div
      data-testid={`fs-${section.key}`}
      data-count={section.count}
      data-label={section.label}
      data-show-header={String(showHeader)}
      data-show-subcategory-filters={String(showSubcategoryFilters)}
    >
      {section.label} ({section.items.length})
    </div>
  ),
}))

const fetchFeedSectionsMock = vi.fn()
vi.mock('../../../lib/api', () => ({
  fetchFeedSections: (...args: unknown[]) => fetchFeedSectionsMock(...args),
}))

import { InfoCategoryView } from '../InfoCategoryView'
import { useFeedStore } from '../../../store/feedStore'

function resetStore() {
  useFeedStore.setState({
    sectionItems: new Map(),
    catCounts: {},
    classification: {
      categories: [
        { id: 'products', name: '产品', visible: true, priority: 1 },
        { id: 'efficiency_tools', name: '工具', visible: true, priority: 2 },
        { id: 'tech', name: '技术', visible: true, priority: 3 },
        { id: 'other', name: '其他', visible: false, priority: 99 },
      ],
      // 兼容 ClassificationConfig 的可选字段
    } as never,
    searchResults: null,
    searchCatCounts: {},
    isLoading: false,
    loadError: null,
  })
}

describe('InfoCategoryView', () => {
  beforeEach(() => {
    resetStore()
    fetchFeedSectionsMock.mockReset()
  })
  afterEach(() => cleanup())

  it('已有 sectionItems 时 mount 不重新拉数据，立即渲染（持久化恢复 §2.5.7）', async () => {
    useFeedStore.getState().setSections(
      {
        products: [{ id: 'p1', platform: 'twitter', title: 't' } as never],
        efficiency_tools: [{ id: 't1', platform: 'github', title: 't2' } as never],
      },
      { products: 5, efficiency_tools: 8 },
    )
    render(<InfoCategoryView />)
    expect(fetchFeedSectionsMock).not.toHaveBeenCalled()
    expect(await screen.findByTestId('info-category-view')).toBeInTheDocument()
    // 至少有一个 section 渲染
    expect(screen.getByTestId('fs-products')).toBeInTheDocument()
    expect(screen.getByTestId('fs-efficiency_tools')).toBeInTheDocument()
  })

  it('sectionItems 为空时 mount 触发 fetchFeedSections 并渲染（首次切到分类视角 §2.5.3）', async () => {
    fetchFeedSectionsMock.mockResolvedValue({
      sections: {
        products: [{ id: 'p1', platform: 'twitter', title: 'pt' } as never],
        tech: [{ id: 'tc1', platform: 'github', title: 'tt' } as never],
      },
      total: 100,
      cat_counts: { products: 30, tech: 70 },
    })
    render(<InfoCategoryView />)
    await waitFor(() => expect(fetchFeedSectionsMock).toHaveBeenCalledTimes(1))
    expect(await screen.findByTestId('fs-products')).toBeInTheDocument()
    expect(screen.getByTestId('fs-tech')).toBeInTheDocument()
    // section count 来自 catCounts，而非 items.length
    expect(screen.getByTestId('fs-products')).toHaveAttribute('data-count', '30')
    expect(screen.getByTestId('fs-tech')).toHaveAttribute('data-count', '70')
  })

  it('section 标题 = 分类中文名（§2.5.5 sections 走 classification.name）', async () => {
    useFeedStore.getState().setSections(
      { products: [{ id: 'p1' } as never] },
      { products: 1 },
    )
    render(<InfoCategoryView />)
    await waitFor(() => expect(screen.getByTestId('fs-products')).toBeInTheDocument())
    expect(screen.getByTestId('fs-products')).toHaveAttribute('data-label', '产品')
  })

  it('embedded=true 时恢复 section 标题，局部筛选回到内容区上下文', async () => {
    useFeedStore.getState().setSections(
      { products: [{ id: 'p1' } as never] },
      { products: 1 },
    )
    render(<InfoCategoryView embedded />)
    await waitFor(() => expect(screen.getByTestId('fs-products')).toBeInTheDocument())
    expect(screen.getByTestId('fs-products')).toHaveAttribute('data-show-header', 'true')
    expect(screen.getByTestId('fs-products')).toHaveAttribute('data-show-subcategory-filters', 'true')
  })

  it('fetch 失败显示「分类视角加载失败，请重试」+ 重试按钮，点击重试后再调一次 API（§2.5.E2）', async () => {
    fetchFeedSectionsMock.mockRejectedValueOnce(new Error('500 boom'))
    render(<InfoCategoryView />)
    await waitFor(() => expect(screen.getByText('分类视角加载失败，请重试')).toBeInTheDocument())
    const retry = screen.getByRole('button', { name: '重试' })
    fetchFeedSectionsMock.mockResolvedValue({
      sections: { products: [{ id: 'p1' } as never] },
      total: 1,
      cat_counts: { products: 1 },
    })
    const user = userEvent.setup()
    await user.click(retry)
    await waitFor(() => expect(fetchFeedSectionsMock).toHaveBeenCalledTimes(2))
    expect(await screen.findByTestId('fs-products')).toBeInTheDocument()
  })

  it('onLoadingChange 回调在 fetch 开始/结束时分别触发 true/false（§2.5.E1 联动 segment disable）', async () => {
    fetchFeedSectionsMock.mockResolvedValue({
      sections: { products: [{ id: 'p1' } as never] },
      total: 1,
      cat_counts: { products: 1 },
    })
    const onLoadingChange = vi.fn()
    render(<InfoCategoryView onLoadingChange={onLoadingChange} />)
    await waitFor(() => expect(fetchFeedSectionsMock).toHaveBeenCalled())
    await waitFor(() => expect(onLoadingChange).toHaveBeenLastCalledWith(false))
    // 至少触发过 true（loading 开始）和 false（loading 结束）
    expect(onLoadingChange.mock.calls.flat()).toContain(true)
    expect(onLoadingChange.mock.calls.flat()).toContain(false)
  })
})

// ── BF-0704-6 rev3:信息模块搜索加载态 ──
import { useUIStore } from '../../../store/uiStore'

describe('BF-0704-6 rev3: InfoCategoryView 搜索加载态', () => {
  afterEach(() => {
    cleanup()
    useUIStore.setState({ searchQuery: '' })
    useFeedStore.setState({ isSearching: false })
  })

  it('搜索进行中显示加载状态条并压暗内容', async () => {
    fetchFeedSectionsMock.mockResolvedValue({
      sections: { products: [{ id: 'p1' } as never] },
      total: 1,
      cat_counts: { products: 1 },
    })
    render(<InfoCategoryView />)
    await waitFor(() => expect(screen.getByTestId('fs-products')).toBeInTheDocument())
    useUIStore.setState({ searchQuery: 'openai' })
    useFeedStore.setState({ isSearching: true })
    const loading = await screen.findByTestId('info-search-loading')
    expect(loading.textContent).toContain('正在搜索')
    expect(loading.textContent).toContain('openai')
    const view = screen.getByTestId('info-category-view')
    expect(view.className).toContain('opacity-50')
  })

  it('搜索结束后状态条消失,内容恢复', async () => {
    fetchFeedSectionsMock.mockResolvedValue({
      sections: { products: [{ id: 'p1' } as never] },
      total: 1,
      cat_counts: { products: 1 },
    })
    render(<InfoCategoryView />)
    await waitFor(() => expect(screen.getByTestId('fs-products')).toBeInTheDocument())
    useUIStore.setState({ searchQuery: 'openai' })
    useFeedStore.setState({ isSearching: false })
    expect(screen.queryByTestId('info-search-loading')).toBeNull()
    expect(screen.getByTestId('info-category-view').className).not.toContain('opacity-50')
  })
})
