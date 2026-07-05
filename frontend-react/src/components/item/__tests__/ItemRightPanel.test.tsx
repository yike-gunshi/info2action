import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { ItemRightPanel } from '../ItemRightPanel'
import type { FeedItem } from '../../../lib/types'

// sonner toast 在 jsdom 下需要 mock,避免 Toaster 实例化副作用
vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  },
}))

/** 最小可渲染的 FeedItem */
function makeItem(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'item-1',
    title: 'Demo Title',
    platform: 'twitter',
    fetched_at: '2026-04-19T00:00:00Z',
    ...overrides,
  }
}

describe('ItemRightPanel', () => {
  let originalClipboard: Clipboard | undefined

  beforeEach(() => {
    originalClipboard = navigator.clipboard
  })

  afterEach(() => {
    cleanup()
    // 还原 clipboard
    if (originalClipboard) {
      Object.defineProperty(navigator, 'clipboard', {
        value: originalClipboard,
        configurable: true,
        writable: true,
      })
    }
    vi.restoreAllMocks()
  })

  it('有 ai_summary 时,右列渲染摘要文案', () => {
    const item = makeItem({
      ai_summary: '这是一条 AI 速览文案,总结本条内容的核心要点。',
    })
    render(<ItemRightPanel item={item} actions={[]} />)

    // 前缀
    expect(screen.getByText('✦ AI 速览')).toBeInTheDocument()
    expect(screen.getByText('✦ AI 速览')).toHaveClass('text-primary')
    expect(screen.getByText('✦ AI 速览').parentElement).toHaveClass('text-[16px]', 'text-foreground')
    // 正文
    expect(screen.getByText(/这是一条 AI 速览文案/)).toBeInTheDocument()
    // 降级文案 SHALL NOT 出现
    expect(screen.queryByText('该内容尚未生成 AI 总结')).not.toBeInTheDocument()
  })

  it('无 ai_summary / actions / metrics 时,显示降级文案"该内容尚未生成 AI 总结"', () => {
    const item = makeItem() // 无 summary / key_points / metrics
    render(<ItemRightPanel item={item} actions={[]} />)

    expect(screen.getByText('该内容尚未生成 AI 总结')).toBeInTheDocument()
    // ✦ AI 速览前缀 SHALL NOT 渲染
    expect(screen.queryByText('✦ AI 速览')).not.toBeInTheDocument()
  })

  it('BF-0420-7 / BF-0420-8: 右列 SHALL NOT 渲染反馈、分享按钮,以及 metrics 数据(已移至 Meta 块)', () => {
    const item = makeItem({
      ai_summary: '内容',
      metrics_json: { likes: 100, views: 9999 },
    })
    render(<ItemRightPanel item={item} actions={[]} />)

    const buttons = screen.queryAllByRole('button')
    const buttonTexts = buttons.map((b) => b.textContent?.trim() ?? '')
    expect(buttonTexts).not.toContain('反馈')
    expect(buttonTexts).not.toContain('分享')
    // metrics 数据(100 点赞 / 9999 浏览)不应再在右列出现
    expect(screen.queryByText('100')).toBeNull()
    expect(screen.queryByText('9,999')).toBeNull()
  })

  it('AI 要点也使用 16px 阅读字号', () => {
    const item = makeItem({
      ai_key_points: ['第一条要点'],
    })
    render(<ItemRightPanel item={item} actions={[]} />)

    expect(screen.getByText('第一条要点').closest('li')).toHaveClass('text-[16px]')
  })

  it('AI 面板使用轻紫色顶层身份,不再把摘要包进重色底卡片', () => {
    const item = makeItem({
      ai_summary: '内容',
    })
    const { container } = render(<ItemRightPanel item={item} actions={[]} />)

    const aside = container.querySelector('aside')
    expect(aside).toHaveClass('ai-summary-signal', 'rounded-[8px]')
    expect(aside?.className).not.toContain('ring-1')
    expect(container.querySelector('[aria-hidden="true"]')).toBeNull()
    expect(container.querySelector('section')?.className).not.toContain('bg-accent')
  })
})
