import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import { ClusterFullPage } from '../ClusterFullPage'
import { fetchFeedItem } from '../../lib/api'
import type { FeedItem } from '../../lib/types'

const storeFns = vi.hoisted(() => ({
  loadFullPage: vi.fn(),
  loadActions: vi.fn(),
  startGenerate: vi.fn(),
  cancelGenerate: vi.fn(),
  resetGenerate: vi.fn(),
}))

const clusterFixture = vi.hoisted(() => ({
  id: 1326,
  ai_title: '本周 HN 热文',
  ai_summary: '聚合摘要',
  ai_key_points: [],
  doc_count: 5,
  platforms: ['rss'],
  first_doc_at: '2026-04-23T00:11:00Z',
  last_doc_at: '2026-04-23T00:11:00Z',
  cover_url: null,
  live_version: 1,
  user_last_seen_version: null,
  is_visible_in_feed: true,
}))

const storeState = vi.hoisted(() => ({
  current: {
    modalState: 'open',
    cluster: clusterFixture,
    sources: [
      {
        item_id: 'item-old',
        title: '较旧来源',
        author: 'Old',
        platform: 'rss',
        published_at: '2026-04-23T00:11:00Z',
        url: null as string | null,
        is_primary_source: 0,
        authority_badge: null,
        snippet: '旧来源摘要',
      },
      {
        item_id: 'item-new',
        title: '较新来源',
        author: 'New',
        platform: 'rss',
        published_at: '2026-04-23T09:11:00Z',
        url: null as string | null,
        is_primary_source: 1,
        authority_badge: null,
        snippet: '新来源摘要',
      },
    ],
  },
}))

vi.mock('../../store/clusterDetailStore', () => ({
  useClusterDetailStore: (selector: (s: unknown) => unknown) =>
    selector({
      ...storeState.current,
      actions: [],
      error: null,
      redirectTo: null,
      generating: false,
      generateStages: [0, 0, 0, 0],
      generateThinkingLines: [],
      generateAction: null,
      generateError: null,
      ...storeFns,
    }),
}))

vi.mock('../../components/shared/AuthGate', () => ({
  requireAuth: () => true,
}))

vi.mock('../../lib/api', () => ({
  fetchFeedItem: vi.fn().mockResolvedValue({
    id: 'item-new',
    title: '唯一来源',
    platform: 'rss',
    fetched_at: '2026-04-23T09:11:00Z',
    content: '唯一来源正文',
  }),
}))

describe('ClusterFullPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    storeState.current = {
      modalState: 'open',
      cluster: clusterFixture,
      sources: [
        {
          item_id: 'item-old',
          title: '较旧来源',
          author: 'Old',
          platform: 'rss',
          published_at: '2026-04-23T00:11:00Z',
          url: null,
          is_primary_source: 0,
          authority_badge: null,
          snippet: '旧来源摘要',
        },
        {
          item_id: 'item-new',
          title: '较新来源',
          author: 'New',
          platform: 'rss',
          published_at: '2026-04-23T09:11:00Z',
          url: null,
          is_primary_source: 1,
          authority_badge: null,
          snippet: '新来源摘要',
        },
      ],
    }
    Object.defineProperty(window, 'location', {
      value: { ...window.location, origin: 'http://127.0.0.1:3567', hash: '#cluster=1326' },
      writable: true,
    })
    vi.mocked(fetchFeedItem).mockResolvedValue({
      id: 'item-new',
      title: '唯一来源',
      platform: 'rss',
      fetched_at: '2026-04-23T09:11:00Z',
      content: '唯一来源正文',
    } as FeedItem)
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('顶部不再渲染无效分享按钮', () => {
    render(<ClusterFullPage clusterId={1326} />)

    expect(screen.queryByRole('button', { name: '分享链接' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '返回' })).toBeInTheDocument()
    expect(screen.getByTestId('brand-wordmark')).toHaveClass('font-brand')
    expect(screen.getByTestId('cluster-full-topbar-title')).toHaveClass(
      'font-event-title',
      'text-[18px]',
      'tracking-[0]',
    )
  })

  it('多来源按发布时间倒排展示', () => {
    render(<ClusterFullPage clusterId={1326} />)

    const cards = screen.getAllByTestId('cluster-source-card')
    expect(cards[0]).toHaveTextContent('较新来源')
    expect(cards[1]).toHaveTextContent('较旧来源')
  })

  // v24.0 §21.5: 内页标题区 = kicker(mono 12px) + 28px 衬线 h1 + Scotch rule 双线
  it('左栏渲染报纸内页标题区(kicker + 衬线大标题 + Scotch rule)', () => {
    render(<ClusterFullPage clusterId={1326} />)

    const kicker = screen.getByTestId('cluster-full-kicker')
    expect(kicker.className).toContain('font-mono')
    expect(kicker.className).toContain('text-[12px]')
    expect(kicker).toHaveTextContent('2 个来源')
    expect(kicker).toHaveTextContent('5 条报道')

    const heading = screen.getByRole('heading', { level: 1 })
    expect(heading).toHaveTextContent('本周 HN 热文')
    expect(heading.className).toContain('font-event-title')
    expect(heading.className).toContain('sm:text-[28px]')
    expect(heading.className).toContain('font-bold')

    const scotch = screen.getByTestId('cluster-full-scotch-rule')
    expect(scotch.className).toContain('border-t-2')
    expect(scotch.className).toContain('border-t-foreground')
    expect(scotch.className).toContain('border-b-border')

    // mini-header 标题降级为普通文本,页面只保留一个 h1
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
  })

  it('短来源列表也使用双栏独立滚动和中间分隔线', () => {
    render(<ClusterFullPage clusterId={1326} />)

    expect(screen.getByTestId('cluster-full-grid')).toHaveClass(
      'lg:h-[calc(100dvh-104px)]',
      'lg:overflow-hidden',
    )
    expect(screen.getByTestId('cluster-source-scroll')).toHaveClass('event-detail-scrollbar', 'lg:overflow-y-auto')
    expect(screen.getByTestId('cluster-summary-scroll')).toHaveClass('event-detail-scrollbar', 'lg:overflow-y-auto', 'lg:border-l', 'lg:border-dashed')
    expect(screen.getByRole('complementary')).toHaveClass('h-full')
  })

  it('长来源列表使用固定高度证据看板,左右容器内部滚动', () => {
    storeState.current = {
      ...storeState.current,
      sources: [
        ...storeState.current.sources,
        {
          item_id: 'item-3',
          title: '第三条来源',
          author: 'Third',
          platform: 'rss',
          published_at: '2026-04-23T10:11:00Z',
          url: null,
          is_primary_source: 0,
          authority_badge: null,
          snippet: '第三条摘要',
        },
        {
          item_id: 'item-4',
          title: '第四条来源',
          author: 'Fourth',
          platform: 'rss',
          published_at: '2026-04-23T11:11:00Z',
          url: null,
          is_primary_source: 0,
          authority_badge: null,
          snippet: '第四条摘要',
        },
      ],
    }

    render(<ClusterFullPage clusterId={1326} />)

    expect(screen.getByTestId('cluster-full-grid')).toHaveClass(
      'lg:h-[calc(100dvh-104px)]',
      'lg:overflow-hidden',
    )
    expect(screen.getByTestId('cluster-source-scroll')).toHaveClass('event-detail-scrollbar', 'lg:overflow-y-auto')
    expect(screen.getByTestId('cluster-summary-scroll')).toHaveClass('event-detail-scrollbar', 'lg:overflow-y-auto')
    expect(screen.getByRole('complementary')).toHaveClass('h-full')
  })

  it('单来源 cluster 也停留在事件详情页并默认展开原文', async () => {
    storeState.current = {
      modalState: 'open',
      cluster: { ...clusterFixture, doc_count: 1 },
      sources: [
        {
          item_id: 'item-new',
          title: '唯一来源',
          author: 'New',
          platform: 'rss',
          published_at: '2026-04-23T09:11:00Z',
          url: 'https://example.com/original',
          is_primary_source: 1,
          authority_badge: null,
          snippet: '唯一来源摘要',
        },
      ],
    }
    const replaceState = vi.spyOn(window.history, 'replaceState')

    render(<ClusterFullPage clusterId={1326} />)

    expect(replaceState).not.toHaveBeenCalledWith({}, '', '#item=item-new')
    expect(screen.getByTestId('cluster-full-grid')).toBeInTheDocument()
    await waitFor(() => expect(fetchFeedItem).toHaveBeenCalledWith('item-new'))
    expect(await screen.findByText('唯一来源正文')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('本周 HN 热文')
    expect(screen.queryByText('事件详情')).not.toBeInTheDocument()
    expect(screen.queryByText('唯一来源')).not.toBeInTheDocument()
    expect(screen.getByTestId('cluster-source-card')).toHaveClass('border-0')
    const originalLink = screen.getByTestId('cluster-topbar-original-link')
    expect(originalLink).toHaveAttribute('href', 'https://example.com/original')
    expect(originalLink).not.toHaveTextContent('原文')
    expect(originalLink).toHaveClass('text-[16px]')
    expect(originalLink.querySelector('svg')).toBeInTheDocument()
  })
})
