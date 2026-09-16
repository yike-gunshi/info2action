/**
 * v24.0 — LatestEvents 报纸化 chrome（DESIGN.md §21.2，tier 装配已退役）。
 *
 * 覆盖：
 *   - 日期与要点共享浅色块，移除报眉 Scotch rule 双线
 *   - 日界收束行「· M 月 D 日 共 N 条 ·」(mono 12px 两侧 hairline)
 *   - 当日未加载完(loaded < date_counts) → 不渲染日界线
 *   - 搜索态 / panel variant → 不渲染日界收束行
 *
 * 展示序断言见 LatestEvents.timeorder.test.tsx（严格时间倒序）。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor, within } from '@testing-library/react'
import { LatestEvents } from '../LatestEvents'
import { useEventsStore } from '../../../store/eventsStore'
import { useFeedStore } from '../../../store/feedStore'
import { useDailyDigestStore } from '../../../store/dailyDigestStore'
import type { ClusterEvent } from '../../../lib/types'
import { fetchDailyDigests, fetchEvents } from '../../../lib/api'

vi.mock('../../../lib/api', () => ({
  fetchEvents: vi.fn(),
  fetchDailyDigests: vi.fn(),
  searchRecommend: vi.fn(),
  markClusterSeen: vi.fn(),
  triggerFetchAll: vi.fn(),
  fetchFetchStatus: vi.fn(),
  fetchFeedSections: vi.fn(),
  fetchFeedPlatforms: vi.fn(),
  fetchFeed: vi.fn(),
}))

const mockFetchEvents = fetchEvents as unknown as ReturnType<typeof vi.fn>
const mockFetchDailyDigests = fetchDailyDigests as unknown as ReturnType<typeof vi.fn>

let nextId = 1

function makeCluster(overrides: Partial<ClusterEvent> = {}): ClusterEvent {
  return {
    id: nextId++,
    ai_title: `事件 ${nextId}`,
    ai_summary: null,
    doc_count: 2,
    unique_source_count: 2,
    category: 'models',
    first_doc_at: '2026-05-10T09:00:00Z',
    last_doc_at: '2026-05-10T09:30:00Z',
    platforms: ['twitter'],
    cover_url: null,
    has_update: false,
    live_version: 1,
    ...overrides,
  }
}

/** 北京时间构造 ISO，确保测试数据与精选统一刊期同源。 */
function isoAtBeijing(year: number, month: number, day: number, hour: number, minute = 0): string {
  return new Date(Date.UTC(year, month - 1, day, hour - 8, minute, 0, 0)).toISOString()
}

/** 北京 2026-05-10 的一天 5 条,按时间线降序 */
function makeDay() {
  const events = [
    makeCluster({ ai_title: '事件甲', first_doc_at: isoAtBeijing(2026, 5, 10, 9, 42) }),
    makeCluster({ ai_title: '事件乙', first_doc_at: isoAtBeijing(2026, 5, 10, 8, 15) }),
    makeCluster({ ai_title: '事件丙', first_doc_at: isoAtBeijing(2026, 5, 10, 7, 58) }),
    makeCluster({ ai_title: '事件丁', first_doc_at: isoAtBeijing(2026, 5, 10, 6, 50) }),
    makeCluster({ ai_title: '事件戊', first_doc_at: isoAtBeijing(2026, 5, 10, 5, 47) }),
  ]
  return { events }
}

describe('v24.0 LatestEvents 报纸化 chrome', () => {
  beforeEach(() => {
    nextId = 1
    mockFetchEvents.mockReset()
    mockFetchEvents.mockResolvedValue({ enabled: true, events: [], next_cursor: null })
    mockFetchDailyDigests.mockReset()
    mockFetchDailyDigests.mockReturnValue(new Promise(() => {}))
    useEventsStore.getState().reset()
    useDailyDigestStore.getState().reset()
    useDailyDigestStore.setState({ loadedRange: { start: '2026-05-10', end: '2026-05-10' } })
    useFeedStore.setState({ isFetching: false, fetchProgress: null })
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllEnvs()
  })

  it('page variant: 速览带紧邻日期标题之后、当日首卡之前', () => {
    const { events } = makeDay()
    const digestDate = '2026-05-10'
    useEventsStore.setState({ enabled: true, events, dateCounts: { '2026-05-10': 5 }, allDateCounts: { '2026-05-10': 5 }, cursor: null })
    useDailyDigestStore.setState({
      loadedRange: { start: digestDate, end: digestDate },
      digestsByDate: {
        [digestDate]: {
          date: digestDate,
          status: 'final',
          entries: [{ rank: 1, cluster_id: events[0].id, title: '当日速览', source_count: 4, links: [] }],
          updated_at: '2026-05-11T02:00:00Z',
        },
      },
    })

    render(<LatestEvents variant="page" />)

    const group = screen.getByTestId('event-date-group')
    const heading = screen.getByTestId('event-date-heading')
    const strip = screen.getByTestId('daily-digest-strip')
    const firstCard = screen.getAllByTestId('event-card')[0]
    expect(heading.compareDocumentPosition(strip) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(strip.compareDocumentPosition(firstCard) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(group.contains(strip)).toBe(true)
    expect(heading).toHaveClass('highlights-day-heading')
    expect(heading).not.toHaveClass('sticky')
    expect(heading).toHaveAttribute('data-has-digest', 'true')
    expect(heading.nextElementSibling).toBe(strip)
    expect(heading.parentElement).toBe(group)
    expect(heading).toHaveTextContent('精选 1')
    expect(heading).toHaveTextContent('全部 5')
    expect(screen.queryByTestId('event-scotch-rule')).toBeNull()
  })

  it('UTC-7 本地跨日事件仍归入同一北京刊期，并与速览同组展示北京时间', async () => {
    vi.stubEnv('TZ', 'America/Los_Angeles')
    const events = [
      makeCluster({
        ai_title: '8 月 2 日事件',
        first_doc_at: '2026-08-02T07:30:00Z',
        last_doc_at: '2026-08-02T07:30:00Z',
      }),
      makeCluster({
        ai_title: '8 月 1 日事件',
        first_doc_at: '2026-08-02T06:30:00Z',
        last_doc_at: '2026-08-02T06:30:00Z',
      }),
    ]
    useEventsStore.setState({
      enabled: true,
      events,
      dateCounts: { '2026-08-02': 2 },
      allDateCounts: { '2026-08-02': 2 },
      cursor: null,
    })
    useDailyDigestStore.setState({
      loadedRange: null,
      digestsByDate: {
        '2026-08-02': {
          date: '2026-08-02',
          status: 'rolling',
          entries: [{ rank: 1, cluster_id: events[0].id, title: '8 月 2 日速览', source_count: 2, links: [] }],
          updated_at: '2026-08-02T08:00:00Z',
        },
      },
    })

    render(<LatestEvents variant="page" />)

    await waitFor(() => expect(mockFetchDailyDigests).toHaveBeenCalledWith('2026-08-02', '2026-08-02'))
    const groups = screen.getAllByTestId('event-date-group')
    expect(groups).toHaveLength(1)
    expect(within(groups[0]).getByTestId('event-date-label')).toHaveTextContent('2026.8.2')
    expect(within(groups[0]).getByTestId('event-date-meta')).toHaveTextContent('星期日 · 精选 1 全部 2')
    expect(within(groups[0]).getByTestId('daily-digest-strip')).toHaveTextContent('8 月 2 日速览')
    expect(groups[0].querySelector(`[data-cluster-id="${events[0].id}"]`)).not.toBeNull()
    expect(within(groups[0]).getAllByTestId('event-time').map((node) => node.textContent)).toEqual(['15:30', '14:30'])
    expect(screen.getAllByText('8 月 2 日速览')).toHaveLength(1)
  })

  it('page variant: 无要点时保留独立日期栏，不展示黑色双线', () => {
    const { events } = makeDay()
    useEventsStore.setState({ enabled: true, events, dateCounts: { '2026-05-10': 5 }, cursor: null })

    render(<LatestEvents variant="page" />)

    const heading = screen.getByTestId('event-date-heading')
    expect(heading).toHaveClass('highlights-day-heading')
    expect(heading).not.toHaveAttribute('data-has-digest')
    expect(heading).toHaveTextContent('精选 0')
    expect(screen.queryByTestId('daily-digest-strip')).toBeNull()
    expect(screen.queryByTestId('event-scotch-rule')).toBeNull()
  })

  it('全天总览不把分类条数或已加载条数当作全部，并保留真实要点数量', () => {
    const { events } = makeDay()
    useEventsStore.setState({ enabled: true, events, filters: { categories: ['models'] }, dateCounts: { '2026-05-10': 24 }, allDateCounts: { '2026-05-10': 48 } })
    useDailyDigestStore.setState({ digestsByDate: {
      '2026-05-10': { date: '2026-05-10', status: 'final', updated_at: '2026-05-11T02:00:00Z', entries: [
        { rank: 1, cluster_id: events[0].id, title: '真实要点', source_count: 4, links: [] },
      ] },
    } })

    render(<LatestEvents variant="page" />)

    expect(screen.getByTestId('event-date-total-count')).toHaveTextContent('全部 48')
    expect(screen.getByTestId('event-date-digest-count')).toHaveTextContent('精选 1')
    expect(screen.getAllByTestId('event-card')).toHaveLength(5)
  })

  it('尚未获取全天统计和要点时显示未知，不伪造 0 或已加载数量', () => {
    const { events } = makeDay()
    useEventsStore.setState({ enabled: true, events, dateCounts: { '2026-05-10': 24 }, filters: { categories: ['models'] } })
    useDailyDigestStore.setState({ loadedRange: null })

    render(<LatestEvents variant="page" />)

    expect(screen.getByTestId('event-date-total-count')).toHaveTextContent('全部 —')
    expect(screen.getByTestId('event-date-digest-count')).toHaveTextContent('精选 —')
  })

  it('日界收束: 日期组末尾渲染「· M 月 D 日 共 N 条 ·」mono 行,两侧 hairline', () => {
    const { events } = makeDay()
    useEventsStore.setState({ enabled: true, events, dateCounts: { '2026-05-10': 5 }, cursor: null })

    render(<LatestEvents variant="page" />)

    const dayEnd = screen.getByTestId('event-day-end')
    expect(dayEnd).toHaveTextContent('· 5 月 10 日 共 5 条 ·')
    const label = dayEnd.querySelector('span.font-mono') as HTMLElement
    expect(label.className).toContain('text-[12px]')
    expect(label.className).toContain('text-muted-foreground')
    expect(dayEnd.querySelectorAll('span.bg-border').length).toBe(2)
    // 日界条数用 dateCounts 全量口径,不随加载页数变化
    expect(dayEnd.textContent).toContain('共 5 条')
  })

  it('当日未加载完(loaded < date_counts) → 不渲染日界线', () => {
    const { events } = makeDay()
    useEventsStore.setState({ enabled: true, events, dateCounts: { '2026-05-10': 8 }, cursor: 2 })

    render(<LatestEvents variant="page" />)

    expect(screen.queryByTestId('event-day-end')).toBeNull()
  })

  it('搜索态 → 不渲染日界收束行', () => {
    const { events } = makeDay()
    useEventsStore.setState({
      enabled: true,
      events: [],
      dateCounts: {},
      cursor: null,
      searchQuery: '事件',
      searching: false,
      searchResults: events,
      searchTotal: events.length,
    })

    render(<LatestEvents variant="page" />)

    const cards = screen.getAllByTestId('event-card')
    expect(cards).toHaveLength(5)
    expect(cards.every((card) => card.getAttribute('data-tier') == null)).toBe(true)
    expect(screen.queryByTestId('event-day-end')).toBeNull()
  })

  it('panel variant: 不渲染 Scotch rule 与日界收束(报纸化只上精选页)', () => {
    const { events } = makeDay()
    useEventsStore.setState({ enabled: true, events, dateCounts: { '2026-05-10': 5 }, cursor: null })

    render(<LatestEvents />)

    expect(screen.queryByTestId('event-scotch-rule')).toBeNull()
    expect(screen.queryByTestId('event-day-end')).toBeNull()
  })
})
