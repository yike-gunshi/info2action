import { describe, it, expect, afterEach, vi } from 'vitest'
import { fireEvent, render, screen, cleanup, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ClusterRightPanel } from '../ClusterRightPanel'
import { ClusterLeftPanel } from '../ClusterLeftPanel'
import { fetchFeedItem } from '../../../lib/api'
import type { ClusterAction, ClusterDetail, ClusterSource, FeedItem } from '../../../lib/types'

class IntersectionObserverMock {
  observe = vi.fn()
  unobserve = vi.fn()
  disconnect = vi.fn()
}

vi.stubGlobal('IntersectionObserver', IntersectionObserverMock)

const scrollIntoView = vi.fn()
Object.defineProperty(Element.prototype, 'scrollIntoView', {
  configurable: true,
  value: scrollIntoView,
})

vi.mock('../../../lib/api', () => ({
  fetchFeedItem: vi.fn(),
}))

vi.mock('../../../store/clusterDetailStore', () => ({
  useClusterDetailStore: (selector: (s: unknown) => unknown) =>
    selector({
      startGenerate: vi.fn(),
      cancelGenerate: vi.fn(),
      generating: false,
      generateStages: [0, 0, 0, 0],
      generateThinkingLines: [],
      generateAction: null,
      generateError: null,
      actions: [],
      loadActions: vi.fn(),
      resetGenerate: vi.fn(),
    }),
}))

vi.mock('../../shared/AuthGate', () => ({
  requireAuth: () => true,
}))

const cluster: ClusterDetail = {
  id: 42,
  ai_title: 'OpenAI 发布新模型路线更新',
  ai_summary: '**OpenAI** 官博宣布新模型路线,多源报道集中在能力边界。',
  ai_key_points: ['**能力边界** 是本轮讨论焦点'],
  doc_count: 6,
  unique_source_count: 6,  // BF-0428-1
  platforms: ['twitter', 'rss'],
  first_doc_at: '2026-04-23T09:10:00Z',
  last_doc_at: '2026-04-23T09:42:00Z',
  cover_url: null,
  live_version: 1,
  user_last_seen_version: null,
  is_visible_in_feed: true,
}

const sources: ClusterSource[] = [
  {
    item_id: 'item-1',
    title: '来源标题',
    author: 'OpenAI',
    platform: 'twitter',
    published_at: '2026-04-23T09:10:00Z',
    url: 'https://example.com/source',
    is_primary_source: 1,
    authority_badge: 'official',
    snippet: '**Claude Code** 质量下降已确认为 bug。',
  },
]

const multiSources: ClusterSource[] = [
  sources[0],
  {
    ...sources[0],
    item_id: 'item-2',
    title: '第二个来源',
    url: null,
    is_primary_source: 0,
    authority_badge: null,
  },
]

describe('ClusterFullPage panels', () => {
  afterEach(() => {
    cleanup()
    scrollIntoView.mockClear()
    vi.mocked(fetchFeedItem).mockReset()
  })

  it('右栏 AI 区块渲染速览,summary 存在时不重复渲染 keyPoints', () => {
    render(<ClusterRightPanel cluster={cluster} sources={sources} actions={[] as ClusterAction[]} />)

    expect(screen.getByText('精华速览')).toBeInTheDocument()
    expect(screen.getByText(/官博宣布新模型路线/)).toBeInTheDocument()
    // summary 已经包含结构化正文时,keyPoints 不再重复追加一份。
    expect(screen.queryByText(/是本轮讨论焦点/)).not.toBeInTheDocument()
    // markdown-lite 仍剥离 ** 标记
    expect(screen.queryByText(/\*\*OpenAI\*\*/)).not.toBeInTheDocument()
  })

  it('左栏来源卡 snippet 使用 markdown-lite,不透传 ** 标记', () => {
    render(<ClusterLeftPanel sources={multiSources} />)

    expect(screen.getAllByText(/质量下降已确认为 bug/).length).toBeGreaterThan(0)
    expect(screen.queryByText(/\*\*Claude Code\*\*/)).not.toBeInTheDocument()
  })

  it('左栏来源卡平台 badge 固定在标题前,不单独换行', () => {
    render(<ClusterLeftPanel sources={multiSources} />)

    const headingRow = screen.getAllByTestId('cluster-source-heading-row')[0]
    const platformBadge = screen.getAllByTestId('cluster-source-platform-badge')[0]
    const title = screen.getByRole('heading', { name: '来源标题' })

    expect(headingRow).toHaveClass('flex', 'items-start', 'min-w-0')
    expect(headingRow).not.toHaveClass('flex-wrap')
    expect(platformBadge).toHaveClass('shrink-0')
    expect(title).toHaveClass('min-w-0', 'flex-1')
  })

  it('左栏来源卡固定跳转按钮直接打开原文,不再进入 item 详情页', () => {
    render(<ClusterLeftPanel sources={multiSources} />)

    const sourceLink = screen.getByRole('link', { name: '打开原文: 来源标题' })
    expect(sourceLink).toHaveAttribute('href', 'https://example.com/source')
    expect(sourceLink).toHaveAttribute('target', '_blank')
    expect(sourceLink).toHaveAttribute('rel', expect.stringContaining('noopener'))
  })

  it('展开长正文时使用阅读字号并提供悬浮收起按钮', async () => {
    const user = userEvent.setup()
    const longContent = Array.from({ length: 80 }, (_, i) => `第 ${i + 1} 段正文，产品方法论的场景推演需要完整上下文。`).join('\n')
    vi.mocked(fetchFeedItem).mockResolvedValue({
      id: 'item-1',
      title: '来源标题',
      platform: 'twitter',
      fetched_at: '2026-04-23T09:10:00Z',
      content: longContent,
    } as FeedItem)

    const { container } = render(<ClusterLeftPanel sources={multiSources} />)

    const sourceCard = screen.getAllByTestId('cluster-source-card')[0]
    sourceCard.getBoundingClientRect = vi.fn(() => ({
      top: 80,
      bottom: window.innerHeight + 1200,
      left: 0,
      right: 760,
      width: 760,
      height: 1800,
      x: 0,
      y: 80,
      toJSON: () => ({}),
    }))

    await user.click(sourceCard)

    const expanded = await screen.findByTestId('cluster-expanded-content')
    expect(expanded).toHaveClass('font-event-title', 'text-[16px]', 'leading-[1.82]', 'tracking-[0]')
    expect(expanded).not.toHaveClass('border-t')
    expect(await screen.findByTestId('item-left-body-text')).toHaveClass(
      'font-event-title',
      'text-[16px]',
      'leading-[1.82]',
      'tracking-[0]',
      'text-[#3F3A34]',
    )
    expect(expanded).toHaveTextContent('第 1 段正文')
    expect(await screen.findByRole('button', { name: '收起当前展开全文' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '收起当前展开全文' }))
    await waitFor(() => {
      expect(container.querySelector('[data-testid="cluster-expanded-content"]')).not.toBeInTheDocument()
    })
  })

  it('右栏不渲染来源导航区块', () => {
    render(
      <>
        <ClusterLeftPanel sources={multiSources} />
        <ClusterRightPanel cluster={cluster} sources={sources} actions={[] as ClusterAction[]} />
      </>,
    )

    expect(screen.queryByText('来源导航')).not.toBeInTheDocument()
    expect(screen.queryByText('点击定位左侧原文')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /跳到来源/ })).not.toBeInTheDocument()
  })

  it('单来源左栏默认直接展开原文,不显示 item 标题卡片', async () => {
    vi.mocked(fetchFeedItem).mockResolvedValue({
      id: 'item-1',
      title: '来源标题',
      platform: 'twitter',
      fetched_at: '2026-04-23T09:10:00Z',
      content: '单来源正文从这里直接开始。',
    } as FeedItem)

    render(<ClusterLeftPanel sources={sources} />)

    expect(screen.queryByRole('button', { name: /展开来源/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '打开原文: 来源标题' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('single-source-original-link')).not.toBeInTheDocument()
    const expanded = await screen.findByTestId('cluster-expanded-content')
    expect(expanded).toHaveTextContent('单来源正文从这里直接开始。')
    expect(expanded).not.toHaveClass('border-t')
    expect(screen.queryByText('来源标题')).not.toBeInTheDocument()
  })

  it('已生成行动点展示 prompt 或失败原因,避免只有标题', () => {
    render(
      <ClusterRightPanel
        cluster={cluster}
        sources={sources}
        showActions
        actions={[
          {
            id: 'action-1',
            title: '[Event] OpenAI 发布新模型路线更新',
            action_type: 'investigate',
            prompt: '整理 OpenAI 新模型路线更新的事实脉络和待验证问题。',
            priority: 'normal',
            status: 'pending',
            cluster_version: 1,
            is_stale: 0,
            reason: 'fallback: LLM 输出未能解析为 action JSON',
          },
        ] as ClusterAction[]}
      />,
    )

    expect(screen.getByText('行动内容')).toBeInTheDocument()
    expect(screen.getByText(/整理 OpenAI 新模型路线更新/)).toBeInTheDocument()
    expect(screen.getByText('生成依据')).toBeInTheDocument()
    expect(screen.getByText(/fallback: LLM 输出未能解析/)).toBeInTheDocument()
  })
})
