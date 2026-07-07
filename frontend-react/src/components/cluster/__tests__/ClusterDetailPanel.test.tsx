import { describe, it, expect, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen, cleanup, waitFor } from '@testing-library/react'
import { toast } from 'sonner'
import { ClusterDetailPanel } from '../ClusterDetailPanel'
import type { ClusterDetail, ClusterSource } from '../../../lib/types'

const closeModal = vi.fn()
const toggleClusterStar = vi.fn()
const originalClipboard = navigator.clipboard
const originalExecCommand = document.execCommand

const cluster: ClusterDetail = {
  id: 42,
  ai_title: 'OpenAI 发布新模型路线更新',
  ai_summary:
    '【精华速览】**OpenAI** 官博宣布新模型路线,多源报道集中在能力边界。\n\n' +
    '【全文拆解】\n1. 能力边界\n- **能力边界** 是本轮讨论焦点\n2. 来源差异\n- 官博给出路线,社区补充影响范围',
  ai_key_points: ['**能力边界** 是本轮讨论焦点'],
  doc_count: 6,
  unique_source_count: 6,
  category: 'coding',
  platforms: ['twitter', 'rss', 'github'],
  first_doc_at: '2026-04-23T09:10:00Z',
  last_doc_at: '2026-04-23T09:42:00Z',
  cover_url: null,
  media_urls: [],
  live_version: 1,
  user_last_seen_version: null,
  viewer_status: {
    clicked_at: null,
    last_seen_version: null,
    starred_at: null,
  },
  is_visible_in_feed: true,
}

const sources: ClusterSource[] = [
  {
    item_id: 'item-1',
    title: '来源标题',
    author: 'OpenAI',
    platform: 'twitter',
    published_at: '2026-04-23T09:10:00Z',
    url: 'https://x.com/openai/status/1',
    cover_url: null,
    media_urls: [],
    is_primary_source: 1,
    authority_badge: 'official',
    snippet: '**Claude Code** 质量下降已确认为 bug。',
  },
]

vi.mock('../../../store/clusterDetailStore', () => ({
  useClusterDetailStore: (selector: (s: unknown) => unknown) =>
    selector({
      modalState: 'open',
      cluster,
      sources,
      error: null,
      closeModal,
      toggleClusterStar,
    }),
}))

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

vi.mock('../../shared/AuthGate', () => ({
  requireAuth: vi.fn(() => true),
}))

// v21.0: 事件弹窗内嵌 ClusterActionZone,本套件聚焦弹窗壳/媒体,行动区块存根即可。
vi.mock('../ClusterActionZone', () => ({
  ClusterActionZone: () => null,
}))

describe('ClusterDetailPanel v19 rebuild', () => {
  afterEach(() => {
    cluster.ai_summary =
      '【精华速览】**OpenAI** 官博宣布新模型路线,多源报道集中在能力边界。\n\n' +
      '【全文拆解】\n1. 能力边界\n- **能力边界** 是本轮讨论焦点\n2. 来源差异\n- 官博给出路线,社区补充影响范围'
    cluster.cover_url = null
    cluster.media_urls = []
    cluster.category = 'coding'
    cluster.platforms = ['twitter', 'rss', 'github']
    cluster.viewer_status = {
      clicked_at: null,
      last_seen_version: null,
      starred_at: null,
    }
    sources.splice(0, sources.length, {
      item_id: 'item-1',
      title: '来源标题',
      author: 'OpenAI',
      platform: 'twitter',
      published_at: '2026-04-23T09:10:00Z',
      url: 'https://x.com/openai/status/1',
      cover_url: null,
      media_urls: [],
      is_primary_source: 1,
      authority_badge: 'official',
      snippet: '**Claude Code** 质量下降已确认为 bug。',
    })
    window.location.hash = ''
    document.documentElement.style.overflow = ''
    document.documentElement.style.paddingRight = ''
    vi.useRealTimers()
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: originalClipboard,
    })
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: originalExecCommand,
    })
    cleanup()
    closeModal.mockClear()
    toggleClusterStar.mockReset()
    vi.clearAllMocks()
  })

  it('D1: Header 展示两行标题 + 题注行(来源名·时间·来源数),平台图标移入题注', () => {
    render(<ClusterDetailPanel />)

    const header = screen.getByTestId('cluster-modal-header')
    const title = screen.getByTestId('cluster-modal-title')
    expect(header).toHaveClass('shrink-0')
    expect(header.className).toContain('bg-[var(--modal-surface)]')
    expect(header.className).not.toContain('border-b')
    expect(title).toHaveTextContent('OpenAI 发布新模型路线更新')
    expect(title).not.toHaveTextContent('Coding |')
    // v19.1 文字角色 Token:标题走 .reading-title(DESIGN.md §8.7);D1 后标题不再内联 flex 平台图标
    expect(title).toHaveClass('reading-title')
    expect(title).not.toHaveClass('flex')
    expect(title.querySelector('span')).toHaveClass('line-clamp-2')

    // D1: 频道 icon stack 从标题移除,改为题注行
    expect(screen.queryByTestId('cluster-channel-stack')).not.toBeInTheDocument()
    const meta = screen.getByTestId('cluster-modal-meta')
    expect(meta).toHaveClass('reading-meta')
    expect(meta).toHaveTextContent('OpenAI')     // 主来源名
    expect(meta).toHaveTextContent('6 来源')      // unique_source_count
    expect(meta.querySelector('time')).toBeInTheDocument()
    expect(meta.querySelector('svg')).toBeInTheDocument()  // 平台徽标图标

    expect(screen.getByLabelText('关闭').parentElement).toHaveClass('w-8')
    expect(screen.getByLabelText('关闭').querySelector('svg')).toHaveClass('h-5', 'w-5')
  })

  it('打开事件弹窗时锁定背景滚动,与信息弹窗保持一致', () => {
    document.documentElement.style.overflow = ''
    document.documentElement.style.paddingRight = ''

    const { unmount } = render(<ClusterDetailPanel />)

    expect(screen.getByTestId('cluster-detail-panel')).toBeInTheDocument()
    expect(document.documentElement.style.overflow).toBe('hidden')
    expect(document.documentElement.style.paddingRight).toBe('')

    unmount()
    expect(document.documentElement.style.overflow).toBe('')
  })

  it('事件弹窗使用共享 modal 语义 token,避免暗色模式继续保留浅色纸面', () => {
    render(<ClusterDetailPanel />)

    const panel = screen.getByTestId('cluster-detail-panel')
    expect(panel).toHaveAttribute('data-modal-theme', 'editorial')
    expect(panel.className).toContain('bg-[var(--modal-surface)]')
    expect(panel.className).toContain('text-[var(--modal-text)]')
    expect(panel.className).toContain('border-[var(--modal-border)]')
    expect(panel.className).not.toContain('bg-[#FAF8F5]')
    expect(panel.className).not.toContain('text-[#171512]')
    expect(screen.getByTestId('cluster-modal-header').className).toContain('bg-[var(--modal-surface)]')
    expect(screen.getByTestId('cluster-bottom-actions').className).toContain('bg-[var(--modal-surface)]')
    expect(screen.getByTestId('cluster-modal-source-row').className).toContain('bg-[var(--modal-surface-soft)]')
  })

  it('公众号平台在弹窗标题与来源卡中展示为公众号,并使用真实平台图形', () => {
    cluster.platforms = ['lingowhale']
    sources[0] = {
      ...sources[0],
      platform: 'lingowhale',
    }

    render(<ClusterDetailPanel />)

    // D1: 平台徽标在题注行,lingowhale 展示为公众号
    const meta = screen.getByTestId('cluster-modal-meta')
    expect(meta.querySelector('[title="公众号"]')).toBeInTheDocument()
    expect(meta.querySelector('svg')).toBeInTheDocument()
    expect(screen.getByTestId('cluster-source-platform')).toHaveTextContent('公众号')
    expect(screen.queryByText('\u8bed\u9cb8')).not.toBeInTheDocument()
  })

  it('右上只保留关闭,底部三栏展示收藏 / 跳转详情 / 分享', () => {
    render(<ClusterDetailPanel />)

    expect(screen.queryByText('原文')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('放大查看事件详情')).not.toBeInTheDocument()
    expect(screen.getByRole('dialog')).toHaveClass('modal-viewport-shell')
    const actions = screen.getByTestId('cluster-bottom-actions')
    expect(actions).toHaveClass(
      'modal-safe-footer',
      'grid',
      'flex-shrink-0',
      'grid-cols-3',
      'overflow-hidden',
      'border-t',
      'border-[var(--modal-border-soft)]',
      'bg-[var(--modal-surface)]',
    )
    expect(screen.getByTestId('cluster-footer-star-button')).toHaveTextContent('收藏')
    expect(screen.getByTestId('cluster-footer-detail-button')).toHaveTextContent('跳转详情')
    expect(screen.getByTestId('cluster-footer-share-button')).toHaveTextContent('分享')

    fireEvent.click(screen.getByTestId('cluster-footer-detail-button'))
    expect(window.location.hash).toBe('#cluster=42')

    // D8: 关闭走 180ms 出场动画后才真正 closeModal
    vi.useFakeTimers()
    fireEvent.click(screen.getByLabelText('关闭'))
    expect(closeModal).not.toHaveBeenCalled()
    act(() => {
      vi.advanceTimersByTime(180)
    })
    expect(closeModal).toHaveBeenCalledTimes(1)
    vi.useRealTimers()
  })

  it('底部收藏按钮监听 viewer_status 并调用 cluster 收藏切换', async () => {
    cluster.viewer_status = {
      clicked_at: null,
      last_seen_version: 1,
      starred_at: '2026-05-25T09:00:00Z',
    }
    toggleClusterStar.mockResolvedValueOnce({ ok: true, starred_at: null })

    render(<ClusterDetailPanel />)

    const starButton = screen.getByTestId('cluster-footer-star-button')
    expect(starButton).toHaveTextContent('已收藏')
    expect(starButton.querySelector('svg')).toHaveClass('fill-current')

    fireEvent.click(starButton)
    await waitFor(() => expect(toggleClusterStar).toHaveBeenCalledWith(42))
  })

  it('分享按钮优先在点击手势内用 execCommand 复制事件深链', async () => {
    const clipboardWriteText = vi.fn().mockRejectedValue(new Error('permission denied'))
    const execCommand = vi.fn().mockReturnValue(true)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboardWriteText },
    })
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: execCommand,
    })

    render(<ClusterDetailPanel />)

    fireEvent.click(screen.getByTestId('cluster-footer-share-button'))

    await waitFor(() => expect(execCommand).toHaveBeenCalledWith('copy'))
    expect(clipboardWriteText).not.toHaveBeenCalled()
    expect(document.querySelector('textarea')).not.toBeInTheDocument()
    expect(toast.success).toHaveBeenCalledWith('分享链接已复制')
  })

  it('execCommand 不可用时再降级到 Clipboard API', async () => {
    const clipboardWriteText = vi.fn().mockResolvedValue(undefined)
    const execCommand = vi.fn().mockReturnValue(false)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboardWriteText },
    })
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: execCommand,
    })

    render(<ClusterDetailPanel />)

    fireEvent.click(screen.getByTestId('cluster-footer-share-button'))

    await waitFor(() => expect(clipboardWriteText).toHaveBeenCalledTimes(1))
    expect(execCommand).toHaveBeenCalledWith('copy')
    expect(toast.success).toHaveBeenCalledWith('分享链接已复制')
  })

  it('精华速览使用内联正文样式,不展示 AI 速览标题或旧色块', () => {
    render(<ClusterDetailPanel />)

    const speedReview = screen.getByTestId('cluster-speed-review')
    const speedLabel = screen.getByText('精华速览：')
    expect(speedLabel).toBeInTheDocument()
    expect(speedLabel).toHaveClass('!text-[var(--brand)]')
    // v19.1: 速览正文走 .reading-body 角色(16px/1.75/400/次墨)
    expect(speedReview.className).toContain('reading-body')
    expect(screen.queryByText('AI 速览：')).not.toBeInTheDocument()
    expect(screen.queryByText('✦')).not.toBeInTheDocument()
    expect(screen.getByTestId('cluster-summary-block')).not.toHaveClass('ai-summary-signal')
    expect(screen.getByTestId('cluster-summary-block')).not.toHaveClass('rounded-[8px]')
    expect(screen.getByTestId('cluster-summary-block').className).toContain('text-[var(--modal-text-soft)]')
    expect(screen.getByText(/官博宣布新模型路线/)).toBeInTheDocument()
    expect(screen.queryByText(/\*\*OpenAI\*\*/)).not.toBeInTheDocument()
  })

  it('精华速览中的裸 URL 渲染为新 tab 外部链接', () => {
    cluster.ai_summary =
      '【精华速览】官方文档 https://docs.anthropic.com 建议更新 CLI。\n\n' +
      '【全文拆解】\n1. 关键链接\n- GitHub 项目 https://github.com/anthropics/claude-code。'

    render(<ClusterDetailPanel />)

    const docsLink = screen.getByRole('link', { name: 'https://docs.anthropic.com' })
    expect(docsLink).toHaveAttribute('href', 'https://docs.anthropic.com/')
    expect(docsLink).toHaveAttribute('target', '_blank')
    expect(docsLink).toHaveAttribute('rel', 'noopener noreferrer')
    expect(docsLink).toHaveClass('content-inline-link')

    const githubLink = screen.getByRole('link', { name: 'https://github.com/anthropics/claude-code' })
    expect(githubLink).toHaveAttribute('href', 'https://github.com/anthropics/claude-code')
    expect(screen.getByTestId('cluster-full-breakdown')).toHaveTextContent('https://github.com/anthropics/claude-code。')
  })

  it('全文拆解渲染为纵向编号分点,不再平铺成报告 markdown', () => {
    render(<ClusterDetailPanel />)

    const breakdown = screen.getByTestId('cluster-full-breakdown')
    expect(breakdown).toHaveTextContent('01')
    expect(breakdown).toHaveTextContent('能力边界')
    expect(breakdown).toHaveTextContent('02')
    expect(breakdown).toHaveTextContent('来源差异')
    expect(breakdown).toHaveTextContent('官博给出路线,社区补充影响范围')
    expect(screen.getAllByTestId('cluster-breakdown-number')[0]).toHaveClass('reading-section', 'text-[var(--brand)]')
    expect(screen.getAllByTestId('cluster-breakdown-bullet-dot')[0]).toHaveClass('bg-[var(--modal-text)]')
    expect(breakdown.className).not.toContain('border-b')
    expect(breakdown.className).toContain('py-4')
    expect(screen.getAllByTestId('cluster-breakdown-number')[0]).toHaveClass('reading-section')
    expect(breakdown.querySelector('h3')?.className).toContain('reading-section')
    expect(breakdown.querySelector('ul')?.className).toContain('reading-bullet')
    expect(breakdown.querySelector('ul')?.className).toContain('space-y-1')
    expect(breakdown.querySelector('article')?.className).toContain('pb-4')
    expect(screen.queryByText('【全文拆解】')).not.toBeInTheDocument()
    expect(screen.queryByText(/\*\*能力边界\*\*/)).not.toBeInTheDocument()
  })

  it('来源卡单行展示标题/平台/作者/时间,点击直接跳原始链接且不展示尾部跳转按钮', () => {
    render(<ClusterDetailPanel />)

    const row = screen.getByTestId('cluster-modal-source-row')
    expect(row).toHaveAttribute('href', 'https://x.com/openai/status/1')
    expect(row).toHaveAttribute('target', '_blank')
    expect(row).toHaveAttribute('data-link-kind', 'original')
    expect(row.className).toContain('bg-[var(--modal-surface-soft)]')
    expect(row).toHaveTextContent('来源标题')
    expect(screen.getByTestId('cluster-source-title')).toHaveTextContent('来源标题')
    expect(screen.getByTestId('cluster-source-title')).toHaveClass('font-event-title', 'truncate')
    expect(screen.getByTestId('cluster-source-platform')).toHaveTextContent('X')
    expect(row).toHaveTextContent('X')
    expect(row).toHaveTextContent('OpenAI')
    expect(screen.getByTestId('cluster-source-time')).toHaveTextContent(/\d{2}:\d{2}/)
    expect(row).not.toHaveTextContent('小时前')
    expect(screen.queryByText('来源')).not.toBeInTheDocument()
    expect(screen.queryByText('1 条')).not.toBeInTheDocument()
    expect(screen.queryByText(/质量下降已确认为 bug/)).not.toBeInTheDocument()
    expect(screen.queryByText(/\*\*Claude Code\*\*/)).not.toBeInTheDocument()
    expect(row.querySelector('.lucide-external-link')).not.toBeInTheDocument()
  })

  it('来源原始链接只允许 http/https,异常协议降级到 item 弹窗深链 fallback', () => {
    sources[0] = {
      ...sources[0],
      url: 'javascript:alert(1)',
    }

    render(<ClusterDetailPanel />)

    const row = screen.getByTestId('cluster-modal-source-row')
    expect(row).toHaveAttribute('href', '/#v=info&d=item-1')
    expect(row).toHaveAttribute('data-link-kind', 'item-fallback')
    expect(row).not.toHaveAttribute('target')
  })

  it('无图弹窗使用 v19.1 窄版宽度,不渲染图片占位,但保留底部操作区', () => {
    render(<ClusterDetailPanel />)

    const panel = screen.getByTestId('cluster-detail-panel')
    expect(panel).toHaveAttribute('data-modal-variant', 'no-media')
    expect(panel.className).toContain('w-[calc(100vw-24px)]')
    expect(panel.className).toContain('max-w-[720px]')
    expect(panel.className).not.toContain('sm:w-[720px]')
    expect(panel.className).not.toContain('max-w-[760px]')
    expect(panel.className).not.toContain('w-[880px]')
    expect(panel.className).toContain('bg-[var(--modal-surface)]')
    expect(panel.className).toContain('shadow-[var(--modal-shadow)]')
    expect(screen.queryByTestId('cluster-modal-media-grid')).not.toBeInTheDocument()
    expect(screen.queryByTestId('cluster-action-zone')).not.toBeInTheDocument()
    expect(screen.getByTestId('cluster-bottom-actions')).toBeInTheDocument()
  })

  it('单图使用正文区真实原图,16:9 且高度不超过弹窗 1/4', () => {
    cluster.cover_url = '/images/cluster-one.jpg'
    cluster.media_urls = ['/images/cluster-one.jpg']

    render(<ClusterDetailPanel />)

    const panel = screen.getByTestId('cluster-detail-panel')
    const media = screen.getByTestId('cluster-modal-media-grid')
    expect(panel).toHaveAttribute('data-modal-variant', 'single-media')
    expect(panel).toHaveClass('modal-viewport-panel')
    expect(panel.className).toContain('max-w-[720px]')
    expect(panel.className).not.toContain('sm:w-[720px]')
    expect(panel.className.split(/\s+/)).not.toContain('w-[760px]')
    expect(media).toHaveAttribute('data-media-count', '1')
    expect(media).toHaveAttribute('data-media-layout', 'single')
    expect(media).toHaveStyle({
      aspectRatio: '16 / 9',
      maxHeight: 'min(180px, calc((var(--app-visual-height) - 32px - var(--modal-bottom-clearance)) * 0.25))',
    })
    expect(media.className).not.toContain('h-[240px]')
    expect(media.className).toContain('rounded-[8px]')
    expect(media.className).toContain('border-[var(--modal-border)]')
    expect(screen.getByTestId('cluster-modal-header').compareDocumentPosition(media) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('媒体加载失败过滤到无图时不触发 hooks 顺序错误', () => {
    sources[0] = {
      ...sources[0],
      media_urls: ['/images/a.jpg', '/images/b.jpg'],
    }

    render(<ClusterDetailPanel />)

    const media = screen.getByTestId('cluster-modal-media-grid')
    fireEvent.error(media.querySelector('img')!)
    expect(screen.getByTestId('cluster-modal-media-grid')).toHaveAttribute('data-media-count', '1')

    fireEvent.error(screen.getByTestId('cluster-modal-media-grid').querySelector('img')!)
    expect(screen.queryByTestId('cluster-modal-media-grid')).not.toBeInTheDocument()
  })

  it('多图仍保持窄版弹窗,用图片内实体圆点替代数量标识', () => {
    sources[0] = {
      ...sources[0],
      media_urls: ['/images/a.jpg', '/images/b.jpg', '/images/c.jpg', '/images/d.jpg'],
    }

    render(<ClusterDetailPanel />)

    const panel = screen.getByTestId('cluster-detail-panel')
    const media = screen.getByTestId('cluster-modal-media-grid')
    expect(panel).toHaveAttribute('data-modal-variant', 'multi-media')
    expect(panel.className).toContain('max-w-[720px]')
    expect(panel.className).not.toContain('sm:w-[720px]')
    expect(panel.className).not.toContain('w-[880px]')
    expect(media).toHaveAttribute('data-media-count', '4')
    expect(media).toHaveAttribute('data-media-layout', 'stacked')
    expect(media).not.toHaveClass('grid-cols-2')
    expect(screen.queryByText('+3')).not.toBeInTheDocument()

    const dots = screen.getAllByTestId('cluster-media-carousel-dot')
    expect(dots).toHaveLength(4)
    expect(dots[0]).toHaveAttribute('aria-current', 'true')
    expect(dots[0]).toHaveClass('rounded-full', 'bg-white', 'opacity-95')
    expect(dots[1]).toHaveClass('rounded-full', 'bg-white', 'opacity-45')
    dots.forEach((dot) => {
      expect(dot.className).not.toContain('ring')
      expect(dot.className).not.toContain('border')
    })
  })

  it('多图分页点可点击并按 4 秒自动轮播,悬停时暂停', () => {
    vi.useFakeTimers()
    sources[0] = {
      ...sources[0],
      media_urls: ['/images/a.jpg', '/images/b.jpg', '/images/c.jpg', '/images/d.jpg'],
    }

    render(<ClusterDetailPanel />)

    const media = screen.getByTestId('cluster-modal-media-grid')
    const image = () => media.querySelector('img')
    expect(image()).toHaveAttribute('src', '/images/a.jpg')

    fireEvent.click(screen.getByLabelText('查看第 2 张图片'))
    expect(image()).toHaveAttribute('src', '/images/b.jpg')
    expect(screen.getByLabelText('查看第 2 张图片')).toHaveAttribute('aria-current', 'true')

    act(() => {
      vi.advanceTimersByTime(3999)
    })
    expect(image()).toHaveAttribute('src', '/images/b.jpg')

    act(() => {
      vi.advanceTimersByTime(1)
    })
    expect(image()).toHaveAttribute('src', '/images/c.jpg')

    fireEvent.mouseEnter(media)
    act(() => {
      vi.advanceTimersByTime(4000)
    })
    expect(image()).toHaveAttribute('src', '/images/c.jpg')

    fireEvent.mouseLeave(media)
    act(() => {
      vi.advanceTimersByTime(4000)
    })
    expect(image()).toHaveAttribute('src', '/images/d.jpg')
  })

  it('D5: 触屏首次交互后暂停自动轮播,分页点热区放大', () => {
    vi.useFakeTimers()
    sources[0] = {
      ...sources[0],
      media_urls: ['/images/a.jpg', '/images/b.jpg', '/images/c.jpg'],
    }

    render(<ClusterDetailPanel />)

    const media = screen.getByTestId('cluster-modal-media-grid')
    const image = () => media.querySelector('img')
    expect(image()).toHaveAttribute('src', '/images/a.jpg')

    // 触摸容器 → 永久暂停自动轮播
    fireEvent.touchStart(media)
    act(() => {
      vi.advanceTimersByTime(8000)
    })
    expect(image()).toHaveAttribute('src', '/images/a.jpg')

    // 暂停后仍可手动点点切换
    fireEvent.click(screen.getByLabelText('查看第 2 张图片'))
    expect(image()).toHaveAttribute('src', '/images/b.jpg')

    // 分页点用 ::before 放大热区(视觉 10px 不变)
    const dot = screen.getAllByTestId('cluster-media-carousel-dot')[0]
    expect(dot.className).toContain('before:h-8')
    expect(dot.className).toContain('h-2.5')
  })

  it('点击多图大图后支持左右按钮和键盘切换', () => {
    sources[0] = {
      ...sources[0],
      media_urls: ['/images/a.jpg', '/images/b.jpg', '/images/c.jpg', '/images/d.jpg'],
    }

    render(<ClusterDetailPanel />)

    fireEvent.click(screen.getByLabelText('查看第 2 张图片'))
    fireEvent.click(screen.getByLabelText('放大查看事件图片'))
    const lightbox = screen.getByTestId('cluster-cover-lightbox')
    expect(lightbox.querySelector('img')).toHaveAttribute('src', '/images/b.jpg')
    expect(screen.getByText('2 / 4')).toBeInTheDocument()

    const nextButton = screen.getByLabelText('下一张图片')
    expect(nextButton).toHaveClass('h-14', 'w-14')
    expect(nextButton.className).not.toContain('top-16')
    expect(nextButton.className).not.toContain('bottom-16')
    expect(nextButton.className).not.toContain('w-24')

    fireEvent.click(nextButton)
    expect(lightbox.querySelector('img')).toHaveAttribute('src', '/images/c.jpg')
    expect(screen.getByText('3 / 4')).toBeInTheDocument()

    const prevButton = screen.getByLabelText('上一张图片')
    expect(prevButton).toHaveClass('h-14', 'w-14')
    expect(prevButton.className).not.toContain('top-16')
    expect(prevButton.className).not.toContain('bottom-16')
    expect(prevButton.className).not.toContain('w-24')

    fireEvent.keyDown(document, { key: 'ArrowLeft' })
    expect(lightbox.querySelector('img')).toHaveAttribute('src', '/images/b.jpg')
    expect(screen.getByText('2 / 4')).toBeInTheDocument()

    fireEvent.keyDown(document, { key: 'ArrowLeft' })
    expect(lightbox.querySelector('img')).toHaveAttribute('src', '/images/a.jpg')

    fireEvent.keyDown(document, { key: 'ArrowRight' })
    expect(lightbox.querySelector('img')).toHaveAttribute('src', '/images/b.jpg')
  })

  it('第一张大图打开后显示可见的下一张按钮', () => {
    sources[0] = {
      ...sources[0],
      media_urls: ['/images/a.jpg', '/images/b.jpg', '/images/c.jpg'],
    }

    render(<ClusterDetailPanel />)

    fireEvent.click(screen.getByLabelText('放大查看事件图片'))
    expect(screen.getByText('1 / 3')).toBeInTheDocument()
    expect(screen.queryByLabelText('上一张图片')).not.toBeInTheDocument()

    const nextButton = screen.getByLabelText('下一张图片')
    expect(nextButton).toHaveClass('z-10', 'h-14', 'w-14')
    expect(nextButton.querySelector('span')).toHaveClass('bg-black/50')

    fireEvent.click(nextButton)
    expect(screen.getByTestId('cluster-cover-lightbox').querySelector('img')).toHaveAttribute('src', '/images/b.jpg')
    expect(screen.getByText('2 / 3')).toBeInTheDocument()
  })

  it('弹窗层级和遮罩对齐 v19 modal,Esc 可关闭', () => {
    render(<ClusterDetailPanel />)

    const backdrop = screen.getByRole('dialog')
    const panel = screen.getByTestId('cluster-detail-panel')
    expect(backdrop).toHaveClass('z-[900]', 'bg-black/60')
    expect(panel).toHaveClass('animate-modal-in')

    // D8: Esc 触发出场动画,180ms 后才 closeModal(对齐信息弹窗)
    vi.useFakeTimers()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(panel).toHaveClass('animate-modal-out')
    expect(backdrop).toHaveClass('animate-backdrop-out')
    expect(closeModal).not.toHaveBeenCalled()
    act(() => {
      vi.advanceTimersByTime(180)
    })
    expect(closeModal).toHaveBeenCalledTimes(1)
    vi.useRealTimers()
  })
})
