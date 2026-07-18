import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { InfoCard } from '../InfoCard'
import { useFeedStore } from '../../../store/feedStore'
import type { FeedItem } from '../../../lib/types'

// Mock detailStore so we can assert openItem called on card click
const openItem = vi.fn()
const prefetchItem = vi.fn()

vi.mock('../../../store/detailStore', () => ({
  useDetailStore: (selector: (s: { openItem: typeof openItem; prefetchItem: typeof prefetchItem }) => unknown) =>
    selector({ openItem, prefetchItem }),
}))

let uniqueSeq = 0

function makeItem(overrides: Partial<FeedItem> = {}): FeedItem {
  uniqueSeq += 1
  return {
    id: `item-${uniqueSeq}`,
    title: 'Hello World 独立标题',
    platform: 'rss',
    fetched_at: '2026-04-19T00:00:00Z',
    ai_summary: '一段独立撰写的摘要，与标题不同源。',
    ...overrides,
  }
}

/** A7: 模拟图片加载完成并注入 naturalWidth/Height。 */
function loadMediaWith(w: number, h: number) {
  const img = screen.getByTestId('info-card-media').querySelector('img')!
  Object.defineProperty(img, 'naturalWidth', { configurable: true, value: w })
  Object.defineProperty(img, 'naturalHeight', { configurable: true, value: h })
  fireEvent.load(img)
  return img
}

/** 每个用例用唯一图 URL，绕开模块级 naturalWidth 缓存的跨用例污染。 */
function uniqueImg(): string {
  return `https://img.example/cover-${++uniqueSeq}.jpg`
}

/**
 * v24.1: InfoCard 回滚瀑布流白卡（用户实物验收定案）。
 * BF-0420-1 后:卡片列表区保持"点击=弹窗"单一交互。
 */
describe('InfoCard', () => {
  beforeEach(() => {
    openItem.mockClear()
    prefetchItem.mockClear()
    useFeedStore.setState({ clickedAtById: {}, classification: null })
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
    const item = makeItem()
    render(<InfoCard item={item} />)
    const card = screen.getByTestId('info-card')
    await user.click(card)
    expect(openItem).toHaveBeenCalledWith(item.id)
  })

  it('卡片键盘可达:可聚焦、Enter/Space 打开、聚焦预取(C1)', async () => {
    const user = userEvent.setup()
    const item = makeItem({ title: 'Some Title' })
    render(<InfoCard item={item} />)
    const card = screen.getByTestId('info-card')

    expect(card).toHaveAttribute('role', 'button')
    expect(card).toHaveAttribute('tabindex', '0')
    expect(card).toHaveAttribute('aria-label', 'Some Title')

    await user.tab()
    expect(card).toHaveFocus()
    expect(prefetchItem).toHaveBeenCalledWith(item.id)

    await user.keyboard('{Enter}')
    expect(openItem).toHaveBeenCalledWith(item.id)

    openItem.mockClear()
    await user.keyboard(' ')
    expect(openItem).toHaveBeenCalledWith(item.id)
  })

  it('采用 v19 2a 杂志感白卡规格（v24.1 回滚自报纸行骨架）', () => {
    render(
      <InfoCard
        item={makeItem({
          platform: 'twitter',
          title: '一条较长的 AI 产品新闻标题，用于验证杂志感标题层级',
          cover_url: uniqueImg(),
          author_name: 'X · DiscusFish',
          ai_summary: '这是一段 AI 速览摘要，用来验证摘要不再使用橙色底色、左边框或独立色块。',
        })}
      />,
    )

    const card = screen.getByTestId('info-card')
    expect(card.className).toContain('bg-card')
    expect(card.className).toContain('rounded-[4px]')
    expect(card.className).toContain('border')
    expect(card.className).toContain('p-4')
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

    expect(screen.getByTestId('info-card-meta')).toHaveTextContent('5天前')
    expect(screen.getByTestId('info-card-meta')).not.toHaveTextContent('24分钟前')
    expect(screen.getByTestId('info-card-meta').className).toContain('font-mono')
    expect(screen.getByTestId('info-card-meta').className).toContain('border-t')
  })

  it('无图卡直接从来源行开始,data-has-media=false', () => {
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
    expect(screen.getByTestId('info-card-source')).toHaveTextContent('A Very Long Source Name')
  })

  describe('A7 图片三档（v24 保留件，嫁接进 16:9 封面槽）', () => {
    it('r≥1.4 横图 → wide 档 16:9 槽 cover', () => {
      render(<InfoCard item={makeItem({ cover_url: uniqueImg() })} />)
      const img = loadMediaWith(1200, 630)
      const media = screen.getByTestId('info-card-media')
      expect(media).toHaveAttribute('data-media-tier', 'wide')
      expect(media.className).toContain('aspect-[16/9]')
      expect(img.className).toContain('object-cover')
      expect(img.className).not.toContain('object-top')
    })

    it('0.75≤r<1.4 近方图 → 16:9 槽 contain + muted 底纹 + 内 hairline（禁 blur）', () => {
      render(<InfoCard item={makeItem({ cover_url: uniqueImg() })} />)
      const img = loadMediaWith(600, 600)
      const media = screen.getByTestId('info-card-media')
      expect(media).toHaveAttribute('data-media-tier', 'square')
      expect(media.className).toContain('bg-muted')
      expect(media.className).not.toContain('blur')
      expect(img.className).toContain('object-contain')
      expect(media.querySelector('.ring-inset')).not.toBeNull()
    })

    it('r<0.75 长图 → 顶裁 + 底部渐隐 + 「长图」角标', () => {
      render(<InfoCard item={makeItem({ cover_url: uniqueImg() })} />)
      const img = loadMediaWith(600, 1200)
      const media = screen.getByTestId('info-card-media')
      expect(media).toHaveAttribute('data-media-tier', 'tall')
      expect(img.className).toContain('object-top')
      expect(media).toHaveTextContent('长图')
    })

    it('头像误判（r≈1 且短边<200px）→ 撤下封面槽，降级为来源行头像', () => {
      render(<InfoCard item={makeItem({ cover_url: uniqueImg() })} />)
      loadMediaWith(150, 150)
      expect(screen.queryByTestId('info-card-media')).toBeNull()
      expect(screen.getByTestId('info-card')).toHaveAttribute('data-has-media', 'false')
      const avatar = screen.getByTestId('info-card-demoted-avatar')
      expect(screen.getByTestId('info-card-source')).toContainElement(avatar)
      expect(avatar.className).toContain('rounded-full')
    })

    it('暗色下图片做 brightness(.92) 降档', () => {
      render(<InfoCard item={makeItem({ cover_url: uniqueImg() })} />)
      const img = screen.getByTestId('info-card-media').querySelector('img')!
      expect(img.className).toContain('dark:brightness-[.92]')
    })
  })

  describe('已读 = 墨水降档（v24 保留件，不恢复旧整卡 opacity-40）', () => {
    it('已读：标题降 muted-foreground、摘要降 /70、图 saturate(.6)，根节点不整卡压暗', () => {
      render(
        <InfoCard
          item={makeItem({
            clicked_at: '2026-04-19T01:00:00Z',
            cover_url: uniqueImg(),
            ai_summary: '已读摘要。',
          })}
        />,
      )
      const card = screen.getByTestId('info-card')
      expect(card).toHaveAttribute('data-read', 'true')
      expect(card.className).not.toContain('opacity-40')
      expect(screen.getByTestId('info-card-title').className).toContain('text-muted-foreground')
      expect(screen.getByTestId('info-card-summary').className).toContain('opacity-70')
      const img = screen.getByTestId('info-card-media').querySelector('img')!
      expect(img.className).toContain('saturate-[.6]')
      expect(img.className).toContain('opacity-[.85]')
    })

    it('showReadState=false 时不降档（收藏/历史场景）', () => {
      render(
        <InfoCard
          item={makeItem({ clicked_at: '2026-04-19T01:00:00Z' })}
          showReadState={false}
        />,
      )
      expect(screen.getByTestId('info-card')).toHaveAttribute('data-read', 'false')
      expect(screen.getByTestId('info-card-title').className).toContain('text-foreground')
    })

    it('本地点击状态同样触发墨水降档（逐卡订阅）', () => {
      const item = makeItem()
      useFeedStore.setState({ clickedAtById: { [item.id]: '2026-04-19T02:00:00Z' } })
      render(<InfoCard item={item} />)
      expect(screen.getByTestId('info-card')).toHaveAttribute('data-read', 'true')
      expect(screen.getByTestId('info-card-title').className).toContain('text-muted-foreground')
    })
  })
})
