import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen, cleanup, waitFor } from '@testing-library/react'
import { DetailPanel } from '../DetailPanel'
import { useDetailStore } from '../../../store/detailStore'
import { useFeedStore } from '../../../store/feedStore'
import { useActionStore } from '../../../store/actionStore'
import { useAuthStore } from '../../../store/authStore'
import type { ActionItem, FeedItem } from '../../../lib/types'
import { dismissAction, dispatchAction, fetchAction, fetchFeedItem, markActionDone, setActionStatus, setItemStatus, updateAction } from '../../../lib/api'
import { toast } from 'sonner'
import { requireAuth } from '../../shared/AuthGate'

vi.mock('../../../lib/api', () => ({
  fetchFeedItem: vi.fn(),
  fetchFeedItemsBundle: vi.fn(),
  translateItemAsr: vi.fn(),
  triggerItemAsr: vi.fn(),
  fetchActionsByItem: vi.fn().mockResolvedValue({ actions: [] }),
  fetchAction: vi.fn(),
  markActionDone: vi.fn().mockResolvedValue({ ok: true }),
  setActionStatus: vi.fn().mockResolvedValue({ ok: true, status: 'confirmed' }),
  dismissAction: vi.fn().mockResolvedValue({ ok: true }),
  dispatchAction: vi.fn().mockResolvedValue({ ok: true, thread_id: 'thread-1', thread_url: 'https://discord.test/thread-1' }),
  updateAction: vi.fn().mockResolvedValue({ ok: true }),
  setItemStatus: vi.fn().mockResolvedValue({}),
  submitFeedback: vi.fn().mockResolvedValue({}),
}))

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

vi.mock('../ActionZone', () => ({
  ActionZone: () => <div data-testid="mock-action-zone" />,
}))

vi.mock('../VideoPlayer', () => ({
  VideoPlayer: () => <div data-testid="mock-video-player" />,
}))

vi.mock('../YoutubePlayer', () => ({
  YoutubePlayer: () => <div data-testid="mock-youtube-player" />,
}))

vi.mock('../TranscriptPanel', () => ({
  TranscriptPanel: () => <div data-testid="mock-transcript-panel" />,
}))

vi.mock('../../shared/AuthGate', () => ({
  requireAuth: vi.fn(() => true),
}))

const mockFetchFeedItem = fetchFeedItem as unknown as ReturnType<typeof vi.fn>
const mockFetchAction = fetchAction as unknown as ReturnType<typeof vi.fn>
const mockSetItemStatus = setItemStatus as unknown as ReturnType<typeof vi.fn>
const mockMarkActionDone = markActionDone as unknown as ReturnType<typeof vi.fn>
const mockSetActionStatus = setActionStatus as unknown as ReturnType<typeof vi.fn>
const mockDismissAction = dismissAction as unknown as ReturnType<typeof vi.fn>
const mockDispatchAction = dispatchAction as unknown as ReturnType<typeof vi.fn>
const mockUpdateAction = updateAction as unknown as ReturnType<typeof vi.fn>
const mockRequireAuth = requireAuth as unknown as ReturnType<typeof vi.fn>
let clipboardWriteTextMock: ReturnType<typeof vi.fn>

function makeItem(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'item-a',
    title: 'OpenAI 发布一条重要产品更新',
    platform: 'twitter',
    author_name: 'DiscusFish',
    fetched_at: '2026-05-18T08:00:00Z',
    url: 'https://example.com/original',
    ai_summary: '这是一条用于验证详情弹窗的 AI 速览。',
    ai_key_points: ['分点一：验证分点拆解样式'],
    content: '正文内容',
    ...overrides,
  }
}

function makeAction(overrides: Partial<ActionItem> = {}): ActionItem {
  return {
    id: 'act-1',
    title: '集成 Claude Code + MiMo-V2.5-Pro 到 cc-switch 工作流',
    type: 'implementation',
    status: 'pending',
    priority: 'P0',
    created_at: '2026-05-30T01:20:00',
    steps: [
      '在 cc-switch 中新增 MiMo-V2.5-Pro 配置',
      '打开 GitHub 项目 https://github.com/anthropics/claude-code。',
      '用同一任务对比 Claude Code 与 MiMo 调用表现',
      '记录 token 成本、响应质量和失败场景',
    ],
    prompt: '1. 在 cc-switch 中新增 MiMo-V2.5-Pro 配置',
    reason: '官方文档 https://docs.anthropic.com 可作为升级依据。当前开发环境以 Claude Code + OpenClaw 为主，MiMo 的 Coding 能力和成本结构值得小规模实测。',
    source_item_ids: ['src-1', 'src-2'],
    source_items: [
      {
        id: 'src-1',
        platform: 'twitter',
        title: 'MiMo-V2.5-Pro 工具调用表现不错',
        ai_summary: '开发者反馈 MiMo 在编码任务中的稳定性与响应速度表现良好，值得小规模实测。',
      },
      {
        id: 'src-2',
        platform: 'github',
        title: 'cc-switch 多模型工作流配置参考',
        ai_summary: '参考 cc-switch 的多模型接入配置方式、提示词规范与运行策略。',
      },
    ],
    source_item_count: 2,
    ...overrides,
  }
}

function openWith(item: FeedItem, items: FeedItem[] = [item]) {
  useFeedStore.setState({
    sectionItems: new Map([['products', items]]),
  })
  useDetailStore.setState({
    modalStack: [{ type: 'item', id: item.id }],
    itemDetail: item,
    itemActions: [],
    actionDetail: null,
    isLoading: false,
    asrSummaryUpdated: false,
  })
}

function openActionWith(action: ActionItem, options: { mockFetch?: boolean } = {}) {
  useActionStore.setState({
    actions: [action],
    counts: { total: 1, pending: 1 },
    directions: [],
    isLoading: false,
    focusedActionId: null,
  })
  useDetailStore.setState({
    modalStack: [{ type: 'action', id: action.id }],
    itemDetail: null,
    itemActions: [],
    actionDetail: null,
    isLoading: false,
    asrSummaryUpdated: false,
  })
  if (options.mockFetch !== false) mockFetchAction.mockResolvedValue(action)
}

describe('DetailPanel v19 modal variants', () => {
  beforeEach(() => {
    window.location.hash = ''
    try { localStorage.clear() } catch { /* jsdom */ }
    clipboardWriteTextMock = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboardWriteTextMock },
    })
    mockFetchFeedItem.mockReset()
    mockFetchAction.mockReset()
    mockSetItemStatus.mockClear()
    mockMarkActionDone.mockClear()
    mockDismissAction.mockClear()
    mockDispatchAction.mockClear()
    mockUpdateAction.mockClear()
    mockRequireAuth.mockReset()
    mockRequireAuth.mockReturnValue(true)
    useDetailStore.getState().closeModal()
    useDetailStore.setState({
      modalStack: [],
      itemDetail: null,
      detailCache: new Map(),
    })
    useFeedStore.setState({ sectionItems: new Map() })
    useActionStore.setState({
      actions: [],
      counts: {},
      directions: [],
      isLoading: false,
      focusedActionId: null,
    })
    useAuthStore.setState({
      user: { id: 'u1', username: 'tester', email: 'tester@example.com', role: 'user', has_discord_token: true },
      isLoading: false,
      isChecked: true,
    })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('无图弹窗复用事件弹窗窄版纸张壳,标题/来源/摘要进入 magazine header/body', async () => {
    openWith(makeItem({ cover_url: undefined, media_json: undefined, thumbnail: undefined }))
    render(<DetailPanel />)

    const panel = await screen.findByTestId('detail-panel')
    expect(panel).toHaveAttribute('data-detail-variant', 'no-media')
    expect(panel.className).toContain('w-[calc(100vw-24px)]')
    expect(panel.className).toContain('max-w-[720px]')
    expect(panel.className).not.toContain('sm:w-[720px]')
    expect(panel.className).toContain('bg-[var(--modal-surface)]')
    expect(panel.className).toContain('border-[var(--modal-border)]')
    expect(panel.className).toContain('shadow-[var(--modal-shadow)]')
    expect(screen.queryByTestId('detail-media-grid')).toBeNull()

    expect(screen.getByTestId('detail-modal-header').className).toContain('bg-[var(--modal-surface)]')
    // v19.2: 标题走 .reading-title 角色 + 两行 clamp(不再单行截断);字体族由角色类内部提供

    expect(screen.getByTestId('detail-title').className).toContain('reading-title')
    expect(screen.getByTestId('detail-title').className).toContain('line-clamp-2')
    expect(screen.getByTestId('detail-title').className).not.toContain('sm:text-[24px]')
    expect(screen.getByTestId('detail-source-line')).toHaveTextContent('DiscusFish')
    expect(screen.getByTestId('detail-source-line')).not.toHaveTextContent('X ·')
    expect(screen.getByTestId('detail-source-line').className).toContain('reading-meta')
    expect(screen.getByTestId('detail-source-line').querySelector('span')).toHaveClass('rounded-full', 'h-[20px]', 'w-[20px]')

    const summary = screen.getByTestId('detail-ai-summary')
    expect(summary).toHaveTextContent('精华速览：')
    expect(summary).toHaveTextContent('分点一：验证分点拆解样式')
    expect(summary).not.toHaveTextContent('AI 速览：')
    expect(summary.className).not.toContain('bg-')
    expect(summary.className).not.toContain('border-l')
    // v19.2: 信息弹窗迁入 reading-* 角色体系(§8.7),不再写死字号/行高/650
    expect(screen.getByTestId('detail-summary-lead')).toHaveClass('reading-body', 'pb-5')
    expect(screen.getByTestId('detail-key-points')).toHaveClass(
      'reading-bullet',
      'space-y-1',
      'pl-6',
      'sm:pl-[38px]',
    )
    expect(screen.getByText('精华速览：')).toHaveClass('!text-[var(--brand)]')
    expect(summary.compareDocumentPosition(screen.getByTestId('detail-body-content')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByTestId('detail-body-content')).toHaveClass('border-t', 'border-[var(--modal-divider)]', 'pt-4')
    expect(screen.getByTestId('detail-body-text')).toHaveClass('reading-body')
    expect(screen.getByTestId('detail-original-label')).toHaveTextContent('原文：')
    expect(screen.getByTestId('detail-original-label')).toHaveClass('!text-[var(--brand)]')
    // v21.0 action-revival: 行动点区块恢复挂载到信息弹窗正文末。
    expect(screen.getByTestId('mock-action-zone')).toBeInTheDocument()
    expect(screen.getByTestId('detail-bottom-actions')).toHaveClass('modal-safe-footer', 'grid-cols-3')
    expect(screen.getByTestId('detail-bottom-actions')).toHaveTextContent('收藏')
    expect(screen.getByTestId('detail-bottom-actions')).toHaveTextContent('跳转原文')
    expect(screen.getByTestId('detail-bottom-actions')).not.toHaveTextContent('复制链接')
    expect(screen.getByTestId('detail-bottom-actions')).toHaveTextContent('分享')
    expect(screen.getByTestId('detail-footer-star-button')).toHaveClass('w-full', 'h-12')
    expect(screen.getByTestId('detail-footer-original-link')).toHaveClass('w-full', 'h-12')
    expect(screen.getByTestId('detail-footer-share-button')).toHaveClass('w-full', 'h-12')
  })

  it('结构化要点正文使用正文灰,加粗词不超过小标题色', async () => {
    openWith(makeItem({
      ai_key_points: [
        {
          title: '产品发布',
          points: [
            '**OpenAI** 表示 **GPT-5** 面向企业客户',
            '企业版价格为 **20 美元**',
          ],
        },
      ],
    }))
    render(<DetailPanel />)

    await screen.findByTestId('detail-panel')
    const keyPoints = screen.getByTestId('detail-key-points')
    // v19.2: 要点走 .reading-bullet 角色,strong 由 .reading-bullet strong(700+主墨)接管
    expect(keyPoints).toHaveClass('reading-bullet')
    expect(screen.getByText('OpenAI').tagName).toBe('STRONG')
    expect(screen.getByText('GPT-5').tagName).toBe('STRONG')
    expect(screen.getByText('20 美元').tagName).toBe('STRONG')
    expect(screen.getByText('OpenAI').closest('ul')).toHaveClass('reading-bullet')
  })

  it('右上操作区只保留关闭,原文入口移动到底部', async () => {
    openWith(makeItem())
    render(<DetailPanel />)

    await screen.findByTestId('detail-panel')
    expect(screen.getByTestId('detail-header-actions')).toHaveClass('w-8')
    expect(screen.queryByTestId('detail-original-link')).not.toBeInTheDocument()
    expect(screen.queryByTestId('detail-maximize-button')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('放大查看 item 详情')).not.toBeInTheDocument()
    expect(screen.getByLabelText('关闭')).toBeInTheDocument()
    expect(screen.getByTestId('detail-footer-original-link')).toHaveAttribute('href', 'https://example.com/original')
    expect(screen.getByTestId('detail-footer-original-link')).toHaveTextContent('跳转原文')
  })

  it('打开详情弹窗只锁定背景滚动,不再额外补偿 html 右侧 padding', async () => {
    Object.defineProperty(window, 'innerWidth', {
      configurable: true,
      value: 1000,
    })
    Object.defineProperty(document.documentElement, 'clientWidth', {
      configurable: true,
      value: 985,
    })
    document.documentElement.style.paddingRight = ''
    document.documentElement.style.overflow = ''

    openWith(makeItem())
    render(<DetailPanel />)

    await screen.findByTestId('detail-panel')
    expect(document.documentElement.style.overflow).toBe('hidden')
    expect(document.documentElement.style.paddingRight).toBe('')
  })

  it('信息弹窗和行动弹窗使用共享 modal 语义 token,暗色模式不继承浅色纸面', async () => {
    openWith(makeItem({ cover_url: undefined, media_json: undefined, thumbnail: undefined }))
    render(<DetailPanel />)

    const itemPanel = await screen.findByTestId('detail-panel')
    expect(itemPanel).toHaveAttribute('data-modal-theme', 'editorial')
    expect(itemPanel.className).toContain('bg-[var(--modal-surface)]')
    expect(itemPanel.className).toContain('text-[var(--modal-text)]')
    expect(itemPanel.className).toContain('border-[var(--modal-border)]')
    expect(itemPanel.className).not.toContain('bg-[#FAF8F5]')
    expect(itemPanel.className).not.toContain('text-[#171512]')
    expect(screen.getByTestId('detail-modal-header').className).toContain('bg-[var(--modal-surface)]')
    expect(screen.getByTestId('detail-bottom-actions').className).toContain('bg-[var(--modal-surface)]')

    cleanup()
    useDetailStore.getState().closeModal()

    openActionWith(makeAction())
    render(<DetailPanel />)

    const actionPanel = await screen.findByTestId('detail-panel')
    expect(actionPanel).toHaveAttribute('data-modal-theme', 'editorial')
    expect(actionPanel.className).toContain('bg-[var(--modal-surface)]')
    // v2 §13.4: footer 移除,状态改由顶部状态区维护
    expect(screen.getByTestId('action-status-stepper')).toBeInTheDocument()
  })

  it('E2: 无原文 URL 时底部两列均分收藏/分享,不留中间死区占位', async () => {
    openWith(makeItem({ url: undefined }))
    render(<DetailPanel />)

    await screen.findByTestId('detail-panel')
    expect(screen.queryByTestId('detail-original-link')).not.toBeInTheDocument()
    expect(screen.queryByTestId('detail-footer-original-link')).not.toBeInTheDocument()
    // E2: 不再渲染空占位;底栏改两列(收藏/分享均分)
    expect(screen.queryByTestId('detail-footer-original-placeholder')).not.toBeInTheDocument()
    const footer = screen.getByTestId('detail-bottom-actions')
    expect(footer).toHaveClass('grid-cols-2')
    expect(footer.className).not.toContain('grid-cols-3')
    expect(footer).toHaveTextContent('收藏')
    expect(footer).toHaveTextContent('分享')
    expect(screen.queryByLabelText('放大查看 item 详情')).not.toBeInTheDocument()
    expect(screen.getByLabelText('关闭')).toBeInTheDocument()
  })

  it('关闭 item 弹窗时清理 URL 里的 d 参数', async () => {
    window.location.hash = '#v=info&d=item-a&s=products'
    openWith(makeItem())
    render(<DetailPanel />)

    await screen.findByTestId('detail-panel')
    fireEvent.click(screen.getByLabelText('关闭'))

    await waitFor(() => expect(useDetailStore.getState().modalStack).toHaveLength(0))
    expect(window.location.hash).toBe('#v=info&s=products')
  })

  it('分享按钮复制 info2act 浏览文案、100 字以内 AI 总结和 item 深链,并显示成功 toast', async () => {
    const longSummary = '这是一个超过一百个字的AI总结内容用于验证复制分享时会被截断保留重点，同时不再带原文链接，只保留信息弹窗自己的深链，方便别人打开后直接看到同一个 item 弹窗内容。这段文字继续补充到超过一百字。为了确保测试覆盖截断逻辑，这里继续追加足够多的中文字符直到明显超过一百个字符。'
    openWith(makeItem({ ai_summary: longSummary }))
    render(<DetailPanel />)

    await screen.findByTestId('detail-panel')
    fireEvent.click(screen.getByTestId('detail-footer-share-button'))

    await waitFor(() => expect(clipboardWriteTextMock).toHaveBeenCalledTimes(1))
    const copiedText = clipboardWriteTextMock.mock.calls[0][0] as string
    const expectedSummary = `${longSummary.slice(0, 100)}...`
    expect(copiedText).toContain('我正在 info2act 浏览「OpenAI 发布一条重要产品更新」')
    expect(copiedText).not.toContain('https://example.com/original')
    expect(copiedText).toContain(`：${expectedSummary}\n一起看看吧 https://www.info2act.com#v=info&d=item-a`)
    expect(longSummary.slice(0, 100).length).toBeLessThanOrEqual(100)
    expect(copiedText).not.toContain(longSummary.slice(100))
    expect(toast.success).toHaveBeenCalledWith('分享链接已复制')
  })

  it('收藏按钮仍可点击并写入收藏状态', async () => {
    openWith(makeItem())
    render(<DetailPanel />)

    await screen.findByTestId('detail-panel')
    fireEvent.click(screen.getByTestId('detail-footer-star-button'))

    await waitFor(() => expect(mockSetItemStatus).toHaveBeenCalledWith('item-a', 'starred'))
    expect(toast.success).toHaveBeenCalledWith('收藏成功')
  })

  it('收藏乐观状态不会被随后加载出的原文 detail 覆盖', async () => {
    openWith(makeItem({ content: '' }))
    render(<DetailPanel />)

    await screen.findByTestId('detail-panel')
    fireEvent.click(screen.getByTestId('detail-footer-star-button'))
    await waitFor(() => expect(screen.getByTestId('detail-footer-star-button')).toHaveTextContent('已收藏'))

    act(() => {
      useDetailStore.setState({ itemDetail: makeItem({ content: '后加载出的正文内容', starred_at: undefined }) })
    })

    expect(screen.getByTestId('detail-footer-star-button')).toHaveTextContent('已收藏')
    expect(screen.getByTestId('detail-body-text')).toHaveTextContent('后加载出的正文内容')
  })

  it('未登录点击收藏后的去登录回调会关闭弹窗', async () => {
    mockRequireAuth.mockImplementation((_label: string, options?: { onLoginClick?: () => void }) => {
      options?.onLoginClick?.()
      return false
    })
    openWith(makeItem())
    render(<DetailPanel />)

    await screen.findByTestId('detail-panel')
    fireEvent.click(screen.getByTestId('detail-footer-star-button'))

    await waitFor(() => expect(useDetailStore.getState().modalStack).toHaveLength(0))
    expect(mockSetItemStatus).not.toHaveBeenCalledWith('item-a', 'starred')
  })

  it('行动弹窗中的决策理由和执行步骤 URL 可点击并在新 tab 打开', async () => {
    openActionWith(makeAction())
    render(<DetailPanel />)

    await screen.findByTestId('detail-panel')
    const docsLink = screen.getByRole('link', { name: 'https://docs.anthropic.com' })
    expect(docsLink).toHaveAttribute('href', 'https://docs.anthropic.com/')
    expect(docsLink).toHaveAttribute('target', '_blank')
    expect(docsLink).toHaveAttribute('rel', 'noopener noreferrer')
    expect(docsLink).toHaveClass('content-inline-link')

    const githubLink = screen.getByRole('link', { name: 'https://github.com/anthropics/claude-code' })
    expect(githubLink).toHaveAttribute('href', 'https://github.com/anthropics/claude-code')
    expect(screen.getByText(/打开 GitHub 项目/)).toHaveTextContent('https://github.com/anthropics/claude-code。')
  })

  it('长标题在 header 单行截断', async () => {
    openWith(makeItem({
      title: '这是一个非常非常长的标题,用于验证信息 item 弹窗在窄版科技杂志壳里不会把右上角按钮挤掉',
    }))
    render(<DetailPanel />)

    const title = await screen.findByTestId('detail-title')
    expect(title).toHaveClass('line-clamp-2')
    expect(title).toHaveAttribute('title', '这是一个非常非常长的标题,用于验证信息 item 弹窗在窄版科技杂志壳里不会把右上角按钮挤掉')
    expect(screen.getByTestId('detail-header-actions')).toHaveClass('w-8')
  })

  it('单图弹窗使用事件弹窗同款 16:9 媒体带和 1/4 高度 cap', async () => {
    openWith(makeItem({ cover_url: '/images/detail-one.jpg' }))
    render(<DetailPanel />)

    const panel = await screen.findByTestId('detail-panel')
    expect(panel).toHaveAttribute('data-detail-variant', 'single-media')
    expect(panel).toHaveClass('modal-viewport-panel')
    expect(panel.className).toContain('max-w-[720px]')
    expect(panel.className).not.toContain('sm:w-[720px]')
    const media = screen.getByTestId('detail-media-grid')
    expect(media).toHaveAttribute('data-media-count', '1')
    expect(media).toHaveAttribute('data-media-layout', 'single')
    expect(media).toHaveStyle({
      aspectRatio: '16 / 9',
      maxHeight: 'min(180px, calc((var(--app-visual-height) - 32px - var(--modal-bottom-clearance)) * 0.25))',
    })
    expect(media.className).toContain('rounded-[8px]')
    expect(media.className).toContain('border-[var(--modal-border)]')
  })

  it('多图弹窗仍保持窄版,首图媒体带显示 +N 而不是 2x2 网格', async () => {
    openWith(makeItem({
      media_json: [
        { url: '/images/a.jpg' },
        { url: '/images/b.jpg' },
        { url: '/images/c.jpg' },
        { url: '/images/d.jpg' },
      ],
    }))
    render(<DetailPanel />)

    const panel = await screen.findByTestId('detail-panel')
    expect(panel).toHaveAttribute('data-detail-variant', 'multi-media')
    expect(panel.className).toContain('max-w-[720px]')
    expect(panel.className).not.toContain('sm:w-[720px]')
    expect(panel.className).not.toContain('w-[880px]')
    const media = screen.getByTestId('detail-media-grid')
    expect(media).toHaveAttribute('data-media-count', '4')
    expect(media).toHaveAttribute('data-media-layout', 'stacked')
    expect(media).not.toHaveClass('grid-cols-2')
    expect(screen.getByText('+3')).toBeInTheDocument()
  })

  it('视频 item 保留 VideoPlayer 和 TranscriptPanel 路径', async () => {
    openWith(makeItem({
      media_json: [{ type: 'video', url: '/videos/a.mp4' } as unknown as { url?: string }],
    }))
    render(<DetailPanel />)

    const panel = await screen.findByTestId('detail-panel')
    expect(panel).toHaveAttribute('data-detail-variant', 'single-media')
    expect(screen.getByTestId('mock-video-player')).toBeInTheDocument()
    expect(screen.getByTestId('mock-transcript-panel')).toBeInTheDocument()
    expect(screen.getByTestId('mock-video-player').compareDocumentPosition(screen.getByTestId('mock-transcript-panel')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByTestId('mock-transcript-panel').compareDocumentPosition(screen.getByTestId('detail-ai-summary')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByTestId('detail-media-grid')).not.toBeInTheDocument()
  })

  it('行动弹窗复用纸张壳,标题下方使用轻量 meta 和绝对时间', async () => {
    openActionWith(makeAction())
    render(<DetailPanel />)

    const panel = await screen.findByTestId('detail-panel')
    expect(panel.className).toContain('w-[calc(100vw-24px)]')
    expect(panel.className).toContain('max-w-[720px]')
    expect(panel.className).not.toContain('sm:w-[720px]')
    expect(panel.className).toContain('bg-[var(--modal-surface)]')
    expect(panel.className).toContain('border-[var(--modal-border)]')
    expect(panel.className).toContain('shadow-[var(--modal-shadow)]')

    expect(screen.getByTestId('detail-modal-header')).toHaveClass('bg-[var(--modal-surface)]', 'border-[var(--modal-divider)]')
    expect(screen.getByTestId('action-modal-title')).toHaveClass('reading-title', 'line-clamp-2')
    expect(screen.getByTestId('action-modal-title')).toHaveTextContent('集成 Claude Code + MiMo-V2.5-Pro 到 cc-switch 工作流')

    const meta = screen.getByTestId('action-modal-meta')
    // BF-0706-2(#5): 删除 类型/优先级/状态 标签(状态已移到顶部 stepper),meta 仅保留绝对时间
    expect(meta).not.toHaveTextContent('实践')
    expect(meta).not.toHaveTextContent('P0')
    expect(meta).not.toHaveTextContent('待处理')
    expect(meta).toHaveTextContent('2026-05-30 01:20')
    expect(meta).not.toHaveTextContent('优先级：')
    expect(meta).not.toHaveTextContent('状态：')
    expect(meta).not.toHaveTextContent('来自 16:16')
  })

  it('行动弹窗内容顺序为理由、行动点、执行、关联信息,来源行新开 tab 跳转', async () => {
    openActionWith(makeAction())
    render(<DetailPanel />)

    const points = await screen.findByTestId('action-modal-points')
    const sources = screen.getByTestId('action-modal-sources')
    const reason = screen.getByTestId('action-modal-reason')
    const execution = screen.getByTestId('action-modal-execution')
    expect(points).toHaveTextContent('行动点')
    expect(points).toHaveTextContent('在 cc-switch 中新增 MiMo-V2.5-Pro 配置')
    expect(points.querySelector('ul')).toHaveClass('reading-bullet')
    expect(sources).toHaveTextContent('关联信息')
    expect(sources).toHaveTextContent('MiMo-V2.5-Pro 工具调用表现不错')
    // v21.0: 决策理由标题改为"为什么做",且提前到第一位。
    expect(reason).toHaveTextContent('为什么做')
    expect(reason).toHaveTextContent('当前开发环境以 Claude Code + OpenClaw 为主')
    expect(reason.querySelector('div')).toHaveClass('reading-body')
    // 顺序: 理由 → 行动点 → 执行 → 关联信息
    expect(reason.compareDocumentPosition(points) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(points.compareDocumentPosition(execution) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(execution.compareDocumentPosition(sources) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    const sourceLink = screen.getAllByTestId('action-modal-source-row')[0]
    expect(sourceLink).toHaveAttribute('target', '_blank')
    expect(sourceLink).toHaveAttribute('rel', 'noopener noreferrer')
    expect(sourceLink).toHaveAttribute('href', '/#v=info&d=src-1')
    expect(useDetailStore.getState().modalStack).toEqual([{ type: 'action', id: 'act-1' }])
  })

  it('行动弹窗关联信息复用事件来源行样式,URL 标题降级为 markdown 渲染后的摘要', async () => {
    openActionWith(makeAction({
      source_item_ids: ['src-md'],
      source_items: [
        {
          id: 'src-md',
          platform: 'twitter',
          title: 'https://t.co/13Dw7x4ogX',
          ai_summary: '**小米**发布 **MiMo-V2.5** 和 **MiMo-V2.5-Pro**，在 **AA 榜** 上与 Kimi K2.6 对比。',
        },
      ],
    }))
    render(<DetailPanel />)

    const sourceRow = await screen.findByTestId('action-modal-source-row')
    expect(sourceRow).toHaveClass('rounded-[7px]', 'border-[var(--modal-border-soft)]', 'bg-[var(--modal-surface-soft)]', 'px-3', 'py-2.5')
    expect(sourceRow).toHaveAttribute('target', '_blank')
    expect(sourceRow).toHaveAttribute('href', '/#v=info&d=src-md')
    expect(sourceRow.textContent).not.toContain('**')

    const title = screen.getByTestId('action-modal-source-title')
    expect(title).toHaveClass('font-event-title', 'text-[14px]', 'leading-[1.45]', 'truncate')
    expect(title).toHaveTextContent('小米发布 MiMo-V2.5 和 MiMo-V2.5-Pro')
    expect(title).not.toHaveTextContent('https://t.co/13Dw7x4ogX')
    expect(title.querySelector('strong')).not.toBeNull()
    expect(screen.getByTestId('action-modal-source-platform')).toHaveTextContent('X')
  })

  it('行动弹窗不先渲染列表半成品,等完整详情 payload 到齐后一次展示', async () => {
    let resolveAction: (value: ActionItem) => void = () => {}
    const actionWithoutSourceItems = makeAction({ source_items: undefined })
    const actionWithSourceItems = makeAction()
    mockFetchAction.mockReturnValue(new Promise<ActionItem>((resolve) => { resolveAction = resolve }))
    openActionWith(actionWithoutSourceItems, { mockFetch: false })
    render(<DetailPanel />)

    await screen.findByTestId('detail-panel')
    expect(screen.queryByTestId('action-modal-title')).toBeNull()
    expect(screen.queryByTestId('action-modal-points')).toBeNull()
    expect(screen.queryByTestId('action-status-stepper')).toBeNull()
    expect(screen.queryByTestId('action-modal-sources')).toBeNull()
    expect(screen.queryByText(/查看源内容/)).toBeNull()
    expect(screen.queryByText(/#src-1/)).toBeNull()

    await act(async () => {
      resolveAction(actionWithSourceItems)
    })
    expect(await screen.findByTestId('action-modal-title')).toHaveTextContent('集成 Claude Code + MiMo-V2.5-Pro 到 cc-switch 工作流')
    expect(screen.getByTestId('action-modal-points')).toHaveTextContent('在 cc-switch 中新增 MiMo-V2.5-Pro 配置')
    expect(screen.getByTestId('action-modal-reason')).toHaveTextContent('当前开发环境以 Claude Code + OpenClaw 为主')
    expect(await screen.findByTestId('action-modal-sources')).toHaveTextContent('MiMo-V2.5-Pro 工具调用表现不错')
    expect(screen.getByTestId('action-status-stepper')).toHaveTextContent('执行中')
  })

  it('行动弹窗已有完整详情 payload 时不重复请求,避免二次覆盖跳变', async () => {
    const action = makeAction()
    openActionWith(action, { mockFetch: false })
    useDetailStore.setState({ actionDetail: action })

    render(<DetailPanel />)

    expect(await screen.findByTestId('action-modal-title')).toHaveTextContent('集成 Claude Code + MiMo-V2.5-Pro 到 cc-switch 工作流')
    expect(screen.getByTestId('action-modal-points')).toHaveTextContent('在 cc-switch 中新增 MiMo-V2.5-Pro 配置')
    expect(screen.getByTestId('action-modal-reason')).toHaveTextContent('当前开发环境以 Claude Code + OpenClaw 为主')
    expect(screen.getByTestId('action-modal-sources')).toHaveTextContent('MiMo-V2.5-Pro 工具调用表现不错')
    expect(mockFetchAction).not.toHaveBeenCalled()
  })

  it('顶部状态区可切换状态(执行中),派发在执行区且置 dispatched (v2 §13.4)', async () => {
    openActionWith(makeAction())
    render(<DetailPanel />)

    const stepper = await screen.findByTestId('action-status-stepper')
    expect(stepper).toHaveTextContent('待处理')
    expect(stepper).toHaveTextContent('执行中')
    expect(stepper).toHaveTextContent('已完成')
    expect(stepper).toHaveTextContent('忽略')

    // 派发在执行区(pending 时可点,直接按钮无 tab)→ dispatched
    fireEvent.click(screen.getByTestId('exec-dispatch'))
    await waitFor(() => expect(mockDispatchAction).toHaveBeenCalledWith('act-1'))

    // 状态区点"已完成" → setActionStatus(done)
    fireEvent.click(screen.getByTestId('status-step-done'))
    await waitFor(() => expect(mockSetActionStatus).toHaveBeenCalledWith('act-1', 'done'))
    await waitFor(() => expect(useActionStore.getState().actions[0].status).toBe('done'))
  })

  it('跟踪类行动执行区显示跟踪提示,不出现代码块/复制 (track)', async () => {
    openActionWith(makeAction({ action_type: 'track', type: 'track' }))
    render(<DetailPanel />)
    const exec = await screen.findByTestId('action-execution')
    expect(exec).toHaveTextContent('跟踪项')
    expect(screen.queryByTestId('exec-command')).toBeNull()
    expect(screen.queryByTestId('exec-copy-prompt')).toBeNull()
  })

  it('执行区代码块显示 prompt,右上角一个复制按钮复制 prompt,不改状态 (v2 §13.3)', async () => {
    openActionWith(makeAction({ prompt: '去做这件事的完整指令' }))
    render(<DetailPanel />)
    const code = await screen.findByTestId('exec-command')
    expect(code.textContent).toContain('去做这件事的完整指令')
    // 只有一个复制按钮(不再有 复制命令 / 仅复制 Prompt 两个)
    fireEvent.click(screen.getByTestId('exec-copy-prompt'))
    await waitFor(() => expect(clipboardWriteTextMock).toHaveBeenCalledWith('去做这件事的完整指令'))
    // 复制不改状态
    expect(mockSetActionStatus).not.toHaveBeenCalled()
    expect(useActionStore.getState().actions[0].status).toBe('pending')
  })

  it('行动详情加载失败展示错误占位 (D5)', async () => {
    mockFetchAction.mockResolvedValue(null)
    openActionWith(makeAction(), { mockFetch: false })
    useDetailStore.setState({ actionDetail: null })
    render(<DetailPanel />)
    expect(await screen.findByText('行动点未找到或加载失败')).toBeInTheDocument()
  })

  it('ArrowDown 在已加载列表内切到下一条,不关闭弹窗', async () => {
    const first = makeItem({ id: 'item-a' })
    const second = makeItem({ id: 'item-b', title: '下一条' })
    mockFetchFeedItem.mockResolvedValue(second)
    openWith(first, [first, second])
    render(<DetailPanel />)

    await screen.findByTestId('detail-panel')
    await act(async () => {
      fireEvent.keyDown(window, { key: 'ArrowDown' })
    })

    const stack = useDetailStore.getState().modalStack
    expect(stack[stack.length - 1]?.id).toBe('item-b')
  })
})
