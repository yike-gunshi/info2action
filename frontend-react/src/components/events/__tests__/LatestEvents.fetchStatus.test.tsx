import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { fireEvent, render, screen, cleanup, waitFor } from '@testing-library/react'
import { LatestEvents } from '../LatestEvents'
import { useEventsStore } from '../../../store/eventsStore'
import { useFeedStore } from '../../../store/feedStore'
import type { ClusterEvent } from '../../../lib/types'
import { fetchEvents } from '../../../lib/api'
import { toast } from 'sonner'

vi.mock('../../../lib/api', () => ({
  fetchEvents: vi.fn(),
  searchRecommend: vi.fn(),
  markClusterSeen: vi.fn(),
  triggerFetchAll: vi.fn(),
  fetchFetchStatus: vi.fn(),
  fetchFeedSections: vi.fn(),
  fetchFeedPlatforms: vi.fn(),
  fetchFeed: vi.fn(),
}))

vi.mock('sonner', () => ({
  toast: {
    info: vi.fn(),
  },
}))

const mockFetchEvents = fetchEvents as unknown as ReturnType<typeof vi.fn>

function makeCluster(overrides: Partial<ClusterEvent> = {}): ClusterEvent {
  return {
    id: 1,
    ai_title: 'WayToAGI 更新知识库导航',
    doc_count: 3,
    unique_source_count: 3,
    first_doc_at: '2026-05-08T09:00:00Z',
    last_doc_at: '2026-05-08T09:30:00Z',
    platforms: ['waytoagi'],
    cover_url: null,
    has_update: false,
    live_version: 1,
    ...overrides,
  }
}

describe('LatestEvents relaxed highlights timeline', () => {
  beforeEach(() => {
    mockFetchEvents.mockReset()
    mockFetchEvents.mockResolvedValue({ enabled: true, events: [makeCluster()], next_cursor: null })
    vi.mocked(toast.info).mockReset()
    useEventsStore.getState().reset()
    useFeedStore.setState({ isFetching: false, fetchProgress: null })
  })

  afterEach(cleanup)

  it('不再展示标题、抓取进度和手动刷新按钮', async () => {
    useFeedStore.setState({
      isFetching: true,
      fetchProgress: {
        stages: [
          { id: 'ai_enrich', name: 'AI 统一理解', status: 'running', platform: 'waytoagi', percent: 60 },
        ],
        current_stage: 0,
        total_new: 0,
        platform: 'waytoagi',
        percent: 60,
        result_status: 'running',
      },
    })

    render(<LatestEvents />)

    expect(await screen.findByText('WayToAGI 更新知识库导航')).toBeInTheDocument()
    expect(screen.queryByText('最新事件')).toBeNull()
    expect(screen.queryByTestId('latest-events-fetch-status')).toBeNull()
    expect(screen.queryByRole('button', { name: '全局抓取并刷新最新事件' })).toBeNull()
  })

  it('topSlot 直接作为容器顶部内容展示', async () => {
    render(<LatestEvents topSlot={<div>全部 产品 Coding</div>} />)

    expect(await screen.findByText('WayToAGI 更新知识库导航')).toBeInTheDocument()
    expect(screen.getByTestId('latest-events-top-slot')).toHaveTextContent('全部 产品 Coding')
    expect(screen.queryByTestId('latest-events-fetch-status')).toBeNull()
  })

  it('page variant 使用开放式页面滚动，不渲染外层卡片和 topSlot', async () => {
    render(<LatestEvents variant="page" topSlot={<div>不应展示的分类</div>} />)

    expect(await screen.findByText('WayToAGI 更新知识库导航')).toBeInTheDocument()
    expect(screen.queryByTestId('latest-events-top-slot')).toBeNull()
    expect(screen.queryByText('不应展示的分类')).toBeNull()

    const page = screen.getByTestId('latest-events-page')
    expect(page.className).toBe('mb-8')

    const scroller = screen.getByTestId('latest-events-scroll')
    expect(scroller.style.height).toBe('')
    expect(scroller.style.overflowY).toBe('')
    expect(scroller.style.overflowAnchor).toBe('none')

    const timeline = screen.getByTestId('event-timeline')
    expect(timeline.className).toContain('px-0')

	    const heading = screen.getByTestId('event-date-heading')
	    expect(heading.className).toContain('sticky')
	    expect(heading.className).toContain('top-[var(--highlights-date-top)]')
	    expect(heading.className).toContain('z-40')
	    expect(heading.className).toContain('grid-cols-1')
	    expect(heading.className).toContain('min-h-12')
	    expect(heading.className).toContain('items-center')
	    expect(heading.className).toContain('sm:grid-cols-[72px_minmax(0,1fr)]')
	    expect(heading.className).toContain('lg:grid-cols-[80px_minmax(0,1fr)]')
	    expect(heading.className).not.toContain('pt-0')
	    expect(heading.className).not.toContain('pb-3.5')
	    expect(heading.className).not.toContain('border-b')
	    expect(heading.className).not.toContain('top-0')
	    expect(heading.className).not.toContain('-mx-5')
	    expect(screen.getByTestId('event-date-label').parentElement?.className).not.toContain('ml-[6px]')
	    expect(screen.getByTestId('event-date-label').parentElement?.className).toContain('sm:ml-[30px]')
	    expect(screen.getByTestId('event-date-label').parentElement?.className).toContain('lg:ml-[38px]')
	    const headingLabel = screen.getByTestId('event-date-label')
	    expect(headingLabel.className).toContain('text-[22px]')
	    expect(headingLabel.className).not.toContain('text-[26px]')
	    const headingMeta = screen.getByTestId('event-date-meta')
	    expect(headingMeta.className).toContain('font-body-cjk')
	    expect(headingMeta.className).toContain('text-[13px]')
	    expect(headingMeta).not.toHaveTextContent('共')
	    expect(screen.queryByTestId('event-date-icon')).toBeNull()
		  })

  it('page variant 主动刷新中在首个日期行和事件之间渲染小型 spinner', async () => {
    const first = makeCluster({
      id: 100,
      ai_title: '当前顶部事件',
      first_doc_at: '2026-05-26T08:23:00Z',
      platforms: ['twitter'],
    })
    useEventsStore.setState({
      enabled: true,
      events: [first],
      dateCounts: { '2026-05-26': 87 },
      cursor: null,
      refreshing: true,
    })

    render(<LatestEvents variant="page" />)

    expect(screen.getByText('当前顶部事件')).toBeInTheDocument()
    const heading = screen.getByTestId('event-date-heading')
    const spinner = screen.getByTestId('highlights-refresh-spinner')
    expect(heading.nextElementSibling).toBe(spinner)
    expect(spinner).toHaveTextContent('刷新中…')
    expect(screen.queryByTestId('highlights-update-chip')).toBeNull()
  })

  it('refreshHint 使用 toast 展示已是最新', async () => {
    useEventsStore.setState({
      enabled: true,
      events: [makeCluster({ id: 100, ai_title: '当前顶部事件' })],
      dateCounts: { '2026-05-08': 1 },
      cursor: null,
      refreshHint: '已是最新',
    })

    render(<LatestEvents variant="page" />)

    await waitFor(() => expect(toast.info).toHaveBeenCalledWith('已是最新'))
    expect(useEventsStore.getState().refreshHint).toBeNull()
  })

  it('按首发日期分组展示时间线,每条显示 first_doc_at 的 HH:mm', async () => {
    const first = makeCluster({
      id: 11,
      ai_title: 'TinyLlama 1.1B Phase 5 完成',
      first_doc_at: '2026-05-10T09:24:00Z',
      last_doc_at: '2026-05-10T09:59:00Z',
    })
    const second = makeCluster({
      id: 12,
      ai_title: 'Andrej Karpathy 发布 LLM Wiki',
      first_doc_at: '2026-05-10T09:25:00Z',
      last_doc_at: '2026-05-10T10:10:00Z',
    })
    const yesterday = makeCluster({
      id: 13,
      ai_title: 'SeatCompress 推出 Agent Catalog',
      first_doc_at: '2026-05-08T22:31:00Z',
      last_doc_at: '2026-05-08T22:45:00Z',
    })
    mockFetchEvents.mockResolvedValueOnce({
      enabled: true,
      events: [first, second, yesterday],
      next_cursor: null,
      new_since_last_fetch: 0,
      total_available_within_30d: 3,
    })

    render(<LatestEvents />)

    expect(await screen.findByText('TinyLlama 1.1B Phase 5 完成')).toBeInTheDocument()
    expect(screen.getByText('Andrej Karpathy 发布 LLM Wiki')).toBeInTheDocument()
    expect(screen.getByText('SeatCompress 推出 Agent Catalog')).toBeInTheDocument()

    const headings = screen.getAllByTestId('event-date-heading')
    expect(headings).toHaveLength(2)
    expect(headings[0]).toHaveTextContent('2026.5.10')
    expect(headings[0]).toHaveTextContent('星期')
    expect(headings[0]).toHaveTextContent('2 条更新')
    expect(headings[0]).not.toHaveTextContent('共')
    expect(headings[1].textContent).toMatch(/^2026\.5\.(9|8)/)
    expect(headings[0].className).toContain('sticky')
    expect(headings[0].className).not.toContain('col-start-1')
    expect(screen.queryByTestId('event-timeline-line')).toBeNull()

    const times = screen.getAllByTestId('event-time')
    expect(times.map((node) => node.getAttribute('dateTime'))).toEqual([
      second.first_doc_at,
      first.first_doc_at,
      yesterday.first_doc_at,
    ])
    expect(times.every((node) => /^\d{2}:\d{2}$/.test(node.textContent || ''))).toBe(true)
    expect(screen.queryByText(/分钟前|刚刚|小时前/)).toBeNull()
    expect(screen.queryByText(/查看全部/)).toBeNull()
    expect(screen.getAllByTestId('event-card').every((card) => card.className.includes('border-b'))).toBe(true)
  })

  it('日期标题使用 API 全量 date_counts,不随分页追加而改变', async () => {
    const first = makeCluster({
      id: 31,
      ai_title: '第一页事件',
      first_doc_at: '2026-05-10T09:24:00Z',
    })
    const second = makeCluster({
      id: 32,
      ai_title: '第二页同日事件',
      first_doc_at: '2026-05-10T10:24:00Z',
    })
    mockFetchEvents
      .mockResolvedValueOnce({
        enabled: true,
        events: [first],
        next_cursor: 2,
        new_since_last_fetch: 0,
        total_available_within_30d: 2,
        date_counts: { '2026-05-10': 2 },
      })
      .mockResolvedValueOnce({
        enabled: true,
        events: [second],
        next_cursor: null,
        new_since_last_fetch: 0,
        total_available_within_30d: 2,
        date_counts: { '2026-05-10': 99 },
      })

    render(<LatestEvents />)

    expect(await screen.findByText('第一页事件')).toBeInTheDocument()
    expect(screen.getByTestId('event-date-heading')).toHaveTextContent('2 条更新')
    const scroller = screen.getByTestId('latest-events-scroll')
    Object.defineProperty(scroller, 'clientHeight', { configurable: true, value: 100 })
    Object.defineProperty(scroller, 'scrollHeight', { configurable: true, get: () => 500 })
    scroller.scrollTop = 420

    fireEvent.scroll(scroller)

    expect(await screen.findByText('第二页同日事件')).toBeInTheDocument()
    expect(screen.getByTestId('event-date-heading')).toHaveTextContent('2 条更新')
  })

  it('滚到底按 next_cursor 继续分页加载历史事件', async () => {
    const first = makeCluster({
      id: 21,
      ai_title: '第一页事件',
      first_doc_at: '2026-05-10T09:24:00Z',
    })
    const second = makeCluster({
      id: 22,
      ai_title: '第二页事件',
      first_doc_at: '2026-05-09T09:24:00Z',
      last_doc_at: '2026-05-09T09:50:00Z',
    })
    mockFetchEvents
      .mockResolvedValueOnce({
        enabled: true,
        events: [first],
        next_cursor: 2,
      })
      .mockResolvedValueOnce({
        enabled: true,
        events: [second],
        next_cursor: null,
      })

    render(<LatestEvents />)

    expect(await screen.findByText('第一页事件')).toBeInTheDocument()
    expect(screen.getByText('继续下拉加载更多事件')).toBeInTheDocument()
    const scroller = screen.getByTestId('latest-events-scroll')
    const scrollHeight = 500
    Object.defineProperty(scroller, 'clientHeight', { configurable: true, value: 100 })
    Object.defineProperty(scroller, 'scrollHeight', { configurable: true, get: () => scrollHeight })
    scroller.scrollTop = 420

    fireEvent.scroll(scroller)
    expect(await screen.findByText('第二页事件')).toBeInTheDocument()
    // v17.0: fetchEvents 加 categories 参数 (精选 tab L1 chip 多 OR), 默认空列表
    expect(mockFetchEvents).toHaveBeenCalledWith({
      page: 2,
      limit: 20,
      categories: [],
      timezoneOffsetMinutes: expect.any(Number),
    })
    expect(screen.getByText('已展示全部事件')).toBeInTheDocument()
    expect((scroller as HTMLElement).style.overflowAnchor).toBe('none')
  })

  it('日期标题使用 API 全量 date_counts,不随分页追加而改变', async () => {
    const first = makeCluster({
      id: 31,
      ai_title: '第一页事件',
      first_doc_at: '2026-05-10T09:24:00Z',
    })
    const second = makeCluster({
      id: 32,
      ai_title: '第二页同日事件',
      first_doc_at: '2026-05-10T10:24:00Z',
    })
    mockFetchEvents
      .mockResolvedValueOnce({
        enabled: true,
        events: [first],
        next_cursor: 2,
        new_since_last_fetch: 0,
        total_available_within_30d: 2,
        date_counts: { '2026-05-10': 2 },
      })
      .mockResolvedValueOnce({
        enabled: true,
        events: [second],
        next_cursor: null,
        new_since_last_fetch: 0,
        total_available_within_30d: 2,
        date_counts: { '2026-05-10': 99 },
      })

    render(<LatestEvents />)

    expect(await screen.findByText('第一页事件')).toBeInTheDocument()
    expect(screen.getByTestId('event-date-heading')).toHaveTextContent('2 条更新')
    const scroller = screen.getByTestId('latest-events-scroll')
    Object.defineProperty(scroller, 'clientHeight', { configurable: true, value: 100 })
    Object.defineProperty(scroller, 'scrollHeight', { configurable: true, get: () => 500 })
    scroller.scrollTop = 420

    fireEvent.scroll(scroller)

    expect(await screen.findByText('第二页同日事件')).toBeInTheDocument()
    expect(screen.getByTestId('event-date-heading')).toHaveTextContent('2 条更新')
    expect(screen.getByTestId('event-date-heading')).not.toHaveTextContent('共')
  })
})
