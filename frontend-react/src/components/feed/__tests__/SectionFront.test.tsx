import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SectionFront } from '../SectionFront'
import { useFeedStore } from '../../../store/feedStore'
import type { FeedItem } from '../../../lib/types'

const openItem = vi.fn()
const prefetchItem = vi.fn()

vi.mock('../../../store/detailStore', () => ({
  useDetailStore: (selector: (s: { openItem: typeof openItem; prefetchItem: typeof prefetchItem }) => unknown) =>
    selector({ openItem, prefetchItem }),
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

function makeItem(id: string, overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    id,
    title: `Item ${id}`,
    platform: 'rss',
    fetched_at: '2026-07-10T00:00:00Z',
    ai_summary: `摘要 ${id}`,
    ...overrides,
  }
}

function makeItems(count: number): FeedItem[] {
  return Array.from({ length: count }, (_, i) => makeItem(`i-${i + 1}`))
}

const baseProps = {
  sectionKey: 'products',
  label: '产品',
  count: 128,
  hasMore: true,
  remaining: 96,
  isExpanded: false,
  onLoadMore: () => {},
  onCollapse: () => {},
}

describe('SectionFront（v24.1 板块壳：v24 板块眉 + 瀑布流白卡身体）', () => {
  beforeEach(() => {
    vi.stubGlobal('IntersectionObserver', IntersectionObserverMock)
    useFeedStore.setState({ clickedAtById: {}, classification: null })
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('板块眉：brand 短标线压通栏 hairline + 22px/700 衬线板块名 + body-cjk 13px 计数 + 桌面 sticky（移动不 sticky）', () => {
    render(
      <SectionFront
        {...baseProps}
        items={makeItems(3)}
        pillBar={<div data-testid="stub-l2">L2</div>}
      />,
    )

    const head = screen.getByTestId('section-front-head')
    // 移动端不 sticky：sticky 只在 sm: 断点生效
    expect(head.className).toContain('sm:sticky')
    expect(head.className).toContain('sm:top-[92px]')
    expect(head.className).not.toMatch(/(^|\s)sticky(\s|$)/)
    expect(head.className).toContain('bg-background')

    const rule = screen.getByTestId('section-front-rule')
    const shortLine = rule.querySelector('.bg-\\[var\\(--brand\\)\\]')
    expect(shortLine).not.toBeNull()
    expect(shortLine!.className).toContain('w-[26px]')
    expect(shortLine!.className).toContain('h-[2px]')
    expect(rule.querySelector('.bg-border')).not.toBeNull()

    const name = screen.getByRole('heading', { name: '产品' })
    expect(name.className).toContain('font-event-title')
    expect(name.className).toContain('text-[22px]')
    expect(name.className).toContain('font-bold')

    const count = screen.getByText('128 条')
    expect(count.className).toContain('font-body-cjk')  // v24.2: 计数对齐精选 folio meta(13px body)
    expect(count.className).toContain('text-[13px]')
    expect(count.className).toContain('font-normal')

    // L2 与板块名同行右侧
    expect(screen.getByTestId('section-front-l2')).toContainElement(screen.getByTestId('stub-l2'))
  })

  it('身体 = masonry 白卡瀑布流：条目渲染进 masonry-columns，统一卡片无行分型', () => {
    render(<SectionFront {...baseProps} items={makeItems(6)} />)

    const columns = screen.getByTestId('masonry-columns')
    const cards = screen.getAllByTestId('info-card')
    expect(cards).toHaveLength(6)
    cards.forEach((card) => {
      expect(columns.contains(card)).toBe(true)
      expect(card.className).toContain('bg-card')
      expect(card.className).toContain('rounded-[4px]')
      expect(card).not.toHaveAttribute('data-variant')
    })
  })

  it('hasMore 时折叠夹取：max-height 裁切（测量前回退 800px）+ 底部渐变蒙版', () => {
    render(<SectionFront {...baseProps} items={makeItems(6)} />)

    const body = screen.getByTestId('section-front-body')
    expect(body.className).toContain('overflow-hidden')
    expect(body.style.maxHeight).toBe('800px')
    expect(body.querySelector('.bg-gradient-to-t.from-background')).not.toBeNull()
  })

  it('hasMore=false 时不裁切、无渐变蒙版、无展开按钮', () => {
    render(
      <SectionFront
        {...baseProps}
        hasMore={false}
        remaining={0}
        items={makeItems(3)}
      />,
    )

    const body = screen.getByTestId('section-front-body')
    expect(body.className).not.toContain('overflow-hidden')
    expect(body.style.maxHeight).toBe('')
    expect(body.querySelector('.bg-gradient-to-t.from-background')).toBeNull()
    expect(screen.queryByRole('button', { name: /展开更多/ })).toBeNull()
  })

  it('展开按钮：hairline 边框无阴影（v24 样式保留，非旧阴影胶囊），点击回调容器', async () => {
    const user = userEvent.setup()
    const onLoadMore = vi.fn()
    render(
      <SectionFront
        {...baseProps}
        items={makeItems(3)}
        onLoadMore={onLoadMore}
      />,
    )

    const button = screen.getByRole('button', { name: /展开更多/ })
    expect(button).toHaveTextContent('还有 96 条')
    expect(button.className).toContain('border-border')
    expect(button.className).toContain('rounded-[4px]')
    expect(button.className).not.toContain('shadow')
    expect(button.className).not.toContain('rounded-full')
    await user.click(button)
    expect(onLoadMore).toHaveBeenCalledTimes(1)
  })

  it('筛选加载中：身体降透明度 + aria-busy', () => {
    render(<SectionFront {...baseProps} items={makeItems(3)} filterLoading />)
    const body = screen.getByTestId('section-front-body')
    expect(body.className).toContain('opacity-80')
    expect(body).toHaveAttribute('aria-busy', 'true')
  })

  it('不传 label 时不渲染板块眉（Image2 嵌入态）', () => {
    render(<SectionFront {...baseProps} label={undefined} count={undefined} items={makeItems(2)} />)
    expect(screen.queryByTestId('section-front-head')).toBeNull()
    expect(screen.getAllByTestId('info-card')).toHaveLength(2)
  })
})
