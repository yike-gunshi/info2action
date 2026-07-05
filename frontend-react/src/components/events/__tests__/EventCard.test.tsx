import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { EventCard } from '../EventCard'
import type { ClusterEvent } from '../../../lib/types'

function makeCluster(overrides: Partial<ClusterEvent> = {}): ClusterEvent {
  return {
    id: 42,
    ai_title: 'OpenAI 发布新模型路线更新',
    doc_count: 6,
    unique_source_count: 6,
    first_doc_at: '2026-04-23T09:10:00Z',
    last_doc_at: '2026-04-23T09:42:00Z',
    platforms: ['twitter', 'reddit', 'openai'],
    cover_url: null,
    has_update: false,
    live_version: 1,
    ...overrides,
  }
}

describe('EventCard', () => {
  afterEach(cleanup)

  it('显示标题,不展示“来源”文案', () => {
    render(<EventCard cluster={makeCluster()} onSelect={() => {}} />)
    const title = screen.getByText('OpenAI 发布新模型路线更新')
    expect(title).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3 }).className).toContain('font-event-title')
    expect(screen.getByRole('heading', { level: 3 }).className).toContain('font-medium')
    expect(screen.getByRole('heading', { level: 3 }).className).toContain('sm:font-semibold')
    expect(screen.getByTestId('event-card').className).toContain('hover:bg-muted')
    expect(screen.getByTestId('event-card').className).toContain('focus-visible:bg-muted')
    expect(screen.getByTestId('event-content').className).not.toContain('group-hover:bg-muted')
    expect(screen.queryByText(/来源/)).not.toBeInTheDocument()
  })

  it('A2 情报预览流: 外部 row 只显示摘要正文,不展示星标或 AI 速览文案', () => {
    render(
      <EventCard
        cluster={makeCluster({
          ai_summary: '**OpenAI** 官博宣布新模型路线,多源报道集中在能力边界和发布时间。',
        } as Partial<ClusterEvent>)}
        onSelect={() => {}}
      />,
    )

    expect(screen.getByText(/官博宣布新模型路线/)).toBeInTheDocument()
    const summary = screen.getByText(/官博宣布新模型路线/).closest('p')
    expect(summary).toHaveAttribute('data-testid', 'event-summary')
    expect(summary).not.toHaveTextContent('AI 速览')
    expect(summary).not.toHaveTextContent('✦')
    expect(summary?.className).toContain('line-clamp-2')
    expect(summary?.className).toContain('sm:line-clamp-3')
    expect(summary?.className).toContain('font-event-title')
    expect(summary?.className).toContain('text-[16px]')
    expect(summary?.className).toContain('leading-[1.58]')
    expect(summary?.className).not.toContain('min-h-')
    expect(summary?.className).not.toContain('[&_strong]:font-medium')
    expect(summary?.querySelector('strong')).toBeNull()
    expect(summary?.className).not.toContain('line-clamp-1')
    expect(summary?.className).not.toContain('bg-')
    expect(summary?.className).not.toContain('border-l')
    expect(screen.queryByText(/\*\*OpenAI\*\*/)).not.toBeInTheDocument()
    const time = screen.getByTestId('event-time')
    expect(time).toHaveAttribute('dateTime', '2026-04-23T09:10:00Z')
    expect(time.textContent || '').toMatch(/^\d{2}:\d{2}$/)
    expect(time.className).toContain('text-muted-foreground')
    expect(time.className).toContain('self-start')
    expect(time.className).toContain('mt-[8px]')
    expect(screen.getByRole('heading', { level: 3 }).className).toContain('leading-[1.32]')
    expect(screen.queryByText(/小时前|分钟前|天前/)).not.toBeInTheDocument()
  })

  it('最新事件摘要只显示速览部分,不露出【全文拆解】内容', () => {
    render(
      <EventCard
        cluster={makeCluster({
          ai_summary:
            '《穿着Prada的恶魔2》在亚洲上映后引发争议。\n\n' +
            '【全文拆解】\n上映与争议背景 - 观众反馈与抵制声浪',
        })}
        onSelect={() => {}}
      />,
    )

    expect(screen.getByText(/在亚洲上映后引发争议/)).toBeInTheDocument()
    expect(screen.queryByText(/全文拆解/)).not.toBeInTheDocument()
    expect(screen.queryByText(/上映与争议背景/)).not.toBeInTheDocument()
  })

  it('点击触发 onSelect(cluster.id)', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    render(<EventCard cluster={makeCluster({ id: 99 })} onSelect={onSelect} />)
    const card = screen.getByTestId('event-card')
    await user.click(card)
    expect(onSelect).toHaveBeenCalledWith(99, expect.objectContaining({ id: 99 }))  // B7: onSelect 携带 cluster
  })

  it('键盘 Enter 触发 onSelect(可访问性)', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    render(<EventCard cluster={makeCluster({ id: 7 })} onSelect={onSelect} />)
    const card = screen.getByTestId('event-card')
    card.focus()
    await user.keyboard('{Enter}')
    expect(onSelect).toHaveBeenCalledWith(7, expect.objectContaining({ id: 7 }))  // B7
  })

  it('has_update=false 时不渲染更新角标', () => {
    render(<EventCard cluster={makeCluster({ has_update: false })} onSelect={() => {}} />)
    expect(screen.queryByRole('img', { name: '有更新' })).toBeNull()
  })

  it('has_update=true 时也不渲染更新角标', () => {
    render(<EventCard cluster={makeCluster({ has_update: true })} onSelect={() => {}} />)
    expect(screen.queryByRole('img', { name: '有更新' })).toBeNull()
  })

  it('已读无更新事件进入置灰态', () => {
    render(
      <EventCard
        cluster={makeCluster({
          last_seen_version: 3,
          live_version: 3,
          has_update: false,
        })}
        onSelect={() => {}}
      />,
    )
    const card = screen.getByTestId('event-card')
    expect(card).toHaveAttribute('data-read-state', 'read')
    expect(card.className).toContain('opacity-')
  })

  it('已读后有更新事件仍按普通已读态展示', () => {
    render(
      <EventCard
        cluster={makeCluster({
          last_seen_version: 2,
          live_version: 3,
          has_update: true,
        })}
        onSelect={() => {}}
      />,
    )
    expect(screen.getByTestId('event-card')).toHaveAttribute('data-read-state', 'read')
    expect(screen.getByTestId('event-card').className).toContain('opacity-')
    expect(screen.queryByRole('img', { name: '有更新' })).toBeNull()
  })

  it('有 cover_url 时右侧保留 200px 媒体列,但不再固定高度', () => {
    render(
      <EventCard
        cluster={makeCluster({
          cover_url: '/images/events/openai-roadmap.jpg',
        })}
        onSelect={() => {}}
      />,
    )
    const thumb = screen.getByTestId('event-media-thumb')
    const image = screen.getByRole('img', { name: /事件配图/ })
    expect(thumb.className).toContain('aspect-[5/3]')
    expect(thumb.className).toContain('h-full')
    expect(thumb.className).toContain('w-full')
    expect(thumb.className).not.toContain('h-[112px]')
    // BF-0517-2: 事件图不再使用 ring 边框（loaded 前后都不应包含 ring-border）
    expect(thumb.className).not.toContain('ring-border')
    fireEvent.load(image)
    expect(thumb.className).not.toContain('ring-border')
    expect(thumb.className).toContain('opacity-100')
    const card = screen.getByTestId('event-card')
    expect(card).toHaveAttribute('data-has-media', 'true')
    expect(card.style.minHeight).toBe('')
    expect(screen.getByTestId('event-content').className).not.toContain('min-h-[128px]')
    expect(image).toHaveAttribute('src', '/images/events/openai-roadmap.jpg')
    const mediaSlot = screen.getByTestId('event-media-slot')
    expect(mediaSlot.className).toContain('w-[200px]')
    expect(mediaSlot.className).toContain('self-stretch')
    expect(mediaSlot.className).toContain('overflow-hidden')
    expect(mediaSlot.className).toContain('rounded-md')
    expect(mediaSlot.className).not.toContain('h-[120px]')
  })

  it('cover_url 加载失败时移除右侧图片区,正文铺满可用宽度', () => {
    render(
      <EventCard
        cluster={makeCluster({
          cover_url: 'https://example.invalid/broken.jpg',
        })}
        onSelect={() => {}}
      />,
    )
    fireEvent.error(screen.getByRole('img', { name: /事件配图/ }))

    expect(screen.queryByRole('img', { name: /事件配图/ })).toBeNull()
    expect(screen.queryByTestId('event-media-thumb')).toBeNull()
    expect(screen.queryByTestId('event-media-slot')).toBeNull()
    expect(screen.queryByTestId('event-media-blank')).toBeNull()
    const card = screen.getByTestId('event-card')
    expect(card).toHaveAttribute('data-has-media', 'false')
    expect(card.style.minHeight).toBe('')
    expect(card.className).toContain('sm:grid-cols-[72px_minmax(0,1fr)]')
    expect(card.className).toContain('lg:grid-cols-[80px_minmax(0,1fr)]')
    expect(card.className).toContain('grid-cols-1')
    expect(card.className).not.toContain('_200px')
    expect(screen.getByTestId('event-content').className).not.toContain('min-h-[128px]')
  })

  it('无 cover_url 时不渲染右侧图片区,正文铺满可用宽度', () => {
    render(<EventCard cluster={makeCluster({ cover_url: null })} onSelect={() => {}} />)
    const card = screen.getByTestId('event-card')
    expect(screen.queryByTestId('event-media-thumb')).toBeNull()
    expect(screen.queryByTestId('event-media-blank')).toBeNull()
    expect(screen.queryByLabelText('事件占位图')).toBeNull()
    expect(screen.queryByTestId('event-media-slot')).toBeNull()
    expect(card).toHaveAttribute('data-has-media', 'false')
    expect(card.className).toContain('sm:grid-cols-[72px_minmax(0,1fr)]')
    expect(card.className).toContain('lg:grid-cols-[80px_minmax(0,1fr)]')
    expect(card.className).toContain('grid-cols-1')
    expect(card.className).not.toContain('_200px')
    expect(card.style.minHeight).toBe('')
    expect(screen.getByTestId('event-content').className).not.toContain('min-h-[128px]')
  })

  it('分类拼接进标题文本并使用品牌橙色,不展示平台/作者来源信息', () => {
    render(
      <EventCard
        cluster={makeCluster({
          doc_count: 6,
          unique_source_count: 4,
          category: 'products',
          source_preview: [
            { platform: 'twitter', author: 'DiscusFish', source: 'following' },
            { platform: 'twitter', author: 'SamePlatform', source: 'following' },
            { platform: 'github', author: 'openai', source: 'trending' },
            { platform: 'reddit', author: 'malie_moon', source: 'LocalLLaMA' },
          ],
        })}
        onSelect={() => {}}
      />,
    )
    const heading = screen.getByRole('heading', { level: 3 })
    expect(heading).toHaveTextContent('产品 | OpenAI 发布新模型路线更新')
    expect(screen.getByTestId('event-title-text')).toHaveTextContent('产品 | OpenAI 发布新模型路线更新')
    expect(screen.getByTestId('event-category-label')).toHaveTextContent('产品')
    expect(screen.getByTestId('event-category-separator').textContent).toBe(' | ')
    expect(heading.className).toContain('font-event-title')
    expect(screen.getByTestId('event-category-label').className).toContain('text-[var(--brand)]')
    expect(screen.getByTestId('event-category-label').className).not.toContain('rounded')
    expect(screen.getByTestId('event-category-label').className).not.toContain('border')
    expect(screen.getByTestId('event-category-label').className).not.toContain('bg-')
    expect(screen.queryByTestId('event-source-line')).toBeNull()
    expect(screen.queryByTestId('event-platform-stack')).toBeNull()
    expect(screen.queryByTestId('event-platform-icon')).toBeNull()
    expect(screen.queryByTestId('event-platform-overflow')).toBeNull()
    expect(screen.queryByTestId('event-source-overflow')).toBeNull()
    expect(screen.queryByText(/阅读/)).toBeNull()
  })

  it('缺少 source_preview 时也不展示平台来源信息', () => {
    render(
      <EventCard
        cluster={makeCluster({
          unique_source_count: 4,
          category: 'models',
          platforms: ['twitter', 'reddit', 'github'],
        })}
        onSelect={() => {}}
      />,
    )
    expect(screen.getByTestId('event-category-label')).toHaveTextContent('模型')
    expect(screen.queryByTestId('event-source-line')).toBeNull()
    expect(screen.queryByTestId('event-platform-stack')).toBeNull()
    expect(screen.queryByTestId('event-platform-icon')).toBeNull()
    expect(screen.queryByTestId('event-platform-overflow')).toBeNull()
    expect(screen.queryByTestId('event-source-overflow')).toBeNull()
  })

  it('桌面端右侧保留 200x120 媒体轨', () => {
    render(
      <EventCard
        cluster={makeCluster({
          cover_url: '/images/events/with-cover.jpg',
        })}
        onSelect={() => {}}
      />,
    )
    const card = screen.getByTestId('event-card')
    const thumb = screen.getByTestId('event-media-thumb')

    expect(card.className).toContain('lg:grid-cols-[80px_minmax(0,1fr)_200px]')
    expect(thumb.className).toContain('aspect-[5/3]')
    expect(thumb.className).toContain('h-full')
    expect(thumb.className).toContain('w-full')
    expect(screen.getByTestId('event-media-slot').className).toContain('self-stretch')
    expect(screen.getByTestId('event-media-slot').className).not.toContain('h-[120px]')
  })

  it('data-cluster-id 属性可用于 Playwright 定位', () => {
    render(<EventCard cluster={makeCluster({ id: 123 })} onSelect={() => {}} />)
    const card = screen.getByTestId('event-card')
    expect(card.getAttribute('data-cluster-id')).toBe('123')
  })

  // The backend may still compute has_update from
  // (last_seen_version != null && live_version > last_seen_version), but the
  // update badge is intentionally hidden while this module is paused.
  it('R7.2 first-time viewer (last_seen_version=null, has_update=false) → 无角标', () => {
    render(
      <EventCard
        cluster={makeCluster({
          last_seen_version: null,
          live_version: 5,
          has_update: false,  // 后端按 R7.2 给 false（null 时 boundary）
        })}
        onSelect={() => {}}
      />,
    )
    expect(screen.queryByRole('img', { name: '有更新' })).toBeNull()
  })

  it('has_update=true (后端判定 live > seen) → 仍不显示角标', () => {
    render(
      <EventCard
        cluster={makeCluster({
          last_seen_version: 2,
          live_version: 5,
          has_update: true,
        })}
        onSelect={() => {}}
      />,
    )
    expect(screen.queryByRole('img', { name: '有更新' })).toBeNull()
    expect(screen.getByTestId('event-card')).toHaveAttribute('data-read-state', 'read')
  })

  it('R7.2 异常态 live_version <= last_seen_version → has_update=false → 无角标', () => {
    render(
      <EventCard
        cluster={makeCluster({
          last_seen_version: 7,
          live_version: 5,
          has_update: false,
        })}
        onSelect={() => {}}
      />,
    )
    expect(screen.queryByRole('img', { name: '有更新' })).toBeNull()
  })
})
