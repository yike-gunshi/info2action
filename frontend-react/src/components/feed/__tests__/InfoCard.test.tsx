import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { InfoCard } from '../InfoCard'
import type { FeedItem } from '../../../lib/types'

// Mock detailStore so we can assert openItem called on card click
const openItem = vi.fn()
const prefetchItem = vi.fn()

vi.mock('../../../store/detailStore', () => ({
  useDetailStore: (selector: (s: { openItem: typeof openItem; prefetchItem: typeof prefetchItem }) => unknown) =>
    selector({ openItem, prefetchItem }),
}))

function makeItem(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'item-xyz',
    title: 'Hello World',
    platform: 'twitter',
    fetched_at: '2026-04-19T00:00:00Z',
    ...overrides,
  }
}

/**
 * BF-0420-1 后:卡片列表区保持"点击=弹窗"单一交互。
 * 放大按钮已移至 DetailPanel header(由 DetailPanel 测试覆盖)。
 */
describe('InfoCard', () => {
  beforeEach(() => {
    openItem.mockClear()
    prefetchItem.mockClear()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('InfoCard 上不再有放大按钮(BF-0420-1 改动后)', () => {
    render(<InfoCard item={makeItem({ title: 'Some Title' })} />)
    const btn = screen.queryByRole('button', { name: /^放大查看|^在新标签打开/ })
    expect(btn).toBeNull()
  })

  it('点击卡片主区触发 openItem(唯一交互路径)', async () => {
    const user = userEvent.setup()
    render(<InfoCard item={makeItem()} />)
    const card = screen.getByTestId('info-card')
    await user.click(card)
    expect(openItem).toHaveBeenCalledWith('item-xyz')
  })

  it('卡片键盘可达:可聚焦、Enter/Space 打开、聚焦预取(C1)', async () => {
    const user = userEvent.setup()
    render(<InfoCard item={makeItem({ title: 'Some Title' })} />)
    const card = screen.getByTestId('info-card')

    expect(card).toHaveAttribute('role', 'button')
    expect(card).toHaveAttribute('tabindex', '0')
    expect(card).toHaveAttribute('aria-label', 'Some Title')

    await user.tab()
    expect(card).toHaveFocus()
    expect(prefetchItem).toHaveBeenCalledWith('item-xyz')

    await user.keyboard('{Enter}')
    expect(openItem).toHaveBeenCalledWith('item-xyz')

    openItem.mockClear()
    await user.keyboard(' ')
    expect(openItem).toHaveBeenCalledWith('item-xyz')
  })

  it('采用 v19 2a 杂志感信息卡规格', () => {
    render(
      <InfoCard
        item={makeItem({
          title: '一条较长的 AI 产品新闻标题，用于验证杂志感标题层级',
          cover_url: '/images/card.jpg',
          author_name: 'X · DiscusFish',
          ai_summary: '这是一段 AI 速览摘要，用来验证摘要不再使用橙色底色、左边框或独立色块。',
        })}
      />,
    )

    const card = screen.getByTestId('info-card')
    expect(card.className).toContain('rounded-[4px]')
    expect(card.className).toContain('border')
    expect(card.className).toContain('p-4')
    expect(card.className).not.toContain('p-[18px]')
    expect(card.className).not.toContain('shadow')
    expect(card).toHaveAttribute('data-has-media', 'true')

    expect(screen.getByTestId('info-card-media').className).toContain('aspect-[16/9]')
    expect(screen.getByTestId('info-card-source')).toHaveTextContent('X')
    expect(screen.getByTestId('info-card-source')).toHaveTextContent('DiscusFish')
    expect(screen.getByTestId('info-card-source')).not.toHaveTextContent('X · DiscusFish')
    expect(screen.getByTestId('info-card-title').className).toContain('font-event-title')
    expect(screen.getByTestId('info-card-title').className).toContain('text-[20px]')
    expect(screen.getByTestId('info-card-title').className).toContain('leading-[1.36]')
    expect(screen.getByTestId('info-card-title').className).toContain('line-clamp-3')
  })

  it('AI 摘要只允许 ✦ 使用品牌色,正文不做色块或左边框', () => {
    render(
      <InfoCard
        item={makeItem({
          ai_summary: 'AI 摘要作为正文混排展示。',
        })}
      />,
    )

    const summary = screen.getByTestId('info-card-summary')
    expect(summary).toHaveTextContent('✦')
    expect(summary.className).toContain('font-event-title')
    expect(summary.className).toContain('text-[16px]')
    expect(summary.className).toContain('leading-[1.58]')
    expect(summary.className).toContain('line-clamp-5')
    expect(summary.className).not.toContain('bg-')
    expect(summary.className).not.toContain('border-l')
    expect(summary.querySelector('span')?.className).toContain('text-[var(--brand)]')
  })

  it('列表卡片时间优先表达原文发布时间', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-05-24T04:30:00Z'))
    render(
      <InfoCard
        item={makeItem({
          published_at: '2026-05-18T16:00:00Z',
          fetched_at: '2026-05-24T04:06:00Z',
        })}
      />,
    )

    expect(screen.getByText('5天前')).toBeInTheDocument()
    expect(screen.queryByText('24分钟前')).toBeNull()
  })

  it('无图卡不再展示低对比来源占位字样,直接从来源行开始', () => {
    render(
      <InfoCard
        item={makeItem({
          author_name: 'A Very Long Source Name That Should Be Truncated Instead Of Stretching The Card',
          ai_summary: '无图卡摘要仍是普通正文。',
        })}
      />,
    )

    const card = screen.getByTestId('info-card')
    expect(card).toHaveAttribute('data-has-media', 'false')
    expect(screen.queryByTestId('info-card-media')).toBeNull()
    expect(screen.queryByTestId('info-card-no-media-source-mark')).toBeNull()
    expect(screen.getByTestId('info-card-source')).toHaveTextContent('A Very Long Source Name')
  })
})
