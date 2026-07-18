/**
 * v24.0 — LatestEvents 报纸化 chrome（DESIGN.md §21.2，tier 装配已退役）。
 *
 * 覆盖：
 *   - 报眉 Scotch rule 双线(page variant 日期头内,panel 不渲染)
 *   - 日界收束行「· M 月 D 日 共 N 条 ·」(mono 12px 两侧 hairline)
 *   - 当日未加载完(loaded < date_counts) → 不渲染日界线
 *   - 搜索态 / panel variant → 不渲染日界收束行
 *
 * 展示序断言见 LatestEvents.timeorder.test.tsx（严格时间倒序）。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { LatestEvents } from '../LatestEvents'
import { useEventsStore } from '../../../store/eventsStore'
import { useFeedStore } from '../../../store/feedStore'
import type { ClusterEvent } from '../../../lib/types'
import { fetchEvents } from '../../../lib/api'

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

const mockFetchEvents = fetchEvents as unknown as ReturnType<typeof vi.fn>

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

/** 本地时间构造 ISO(日期分组按浏览器本地时区,固定 UTC 字面量会跨时区劈日) */
function isoAtLocal(year: number, month: number, day: number, hour: number, minute = 0): string {
  return new Date(year, month - 1, day, hour, minute, 0, 0).toISOString()
}

/** 本地 2026-05-10 的一天 5 条,按时间线降序 */
function makeDay() {
  const events = [
    makeCluster({ ai_title: '事件甲', first_doc_at: isoAtLocal(2026, 5, 10, 9, 42) }),
    makeCluster({ ai_title: '事件乙', first_doc_at: isoAtLocal(2026, 5, 10, 8, 15) }),
    makeCluster({ ai_title: '事件丙', first_doc_at: isoAtLocal(2026, 5, 10, 7, 58) }),
    makeCluster({ ai_title: '事件丁', first_doc_at: isoAtLocal(2026, 5, 10, 6, 50) }),
    makeCluster({ ai_title: '事件戊', first_doc_at: isoAtLocal(2026, 5, 10, 5, 47) }),
  ]
  return { events }
}

describe('v24.0 LatestEvents 报纸化 chrome', () => {
  beforeEach(() => {
    nextId = 1
    mockFetchEvents.mockReset()
    mockFetchEvents.mockResolvedValue({ enabled: true, events: [], next_cursor: null })
    useEventsStore.getState().reset()
    useFeedStore.setState({ isFetching: false, fetchProgress: null })
  })

  afterEach(cleanup)

  it('page variant: 日期头带 Scotch rule 双线(2px foreground + 3px 间隔 + 1px border)', () => {
    const { events } = makeDay()
    useEventsStore.setState({ enabled: true, events, dateCounts: { '2026-05-10': 5 }, cursor: null })

    render(<LatestEvents variant="page" />)

    const heading = screen.getByTestId('event-date-heading')
    const scotch = screen.getByTestId('event-scotch-rule')
    expect(heading.contains(scotch)).toBe(true)
    expect(scotch.className).toContain('h-[6px]')
    expect(scotch.className).toContain('border-t-2')
    expect(scotch.className).toContain('border-t-foreground')
    expect(scotch.className).toContain('border-b')
    expect(scotch.className).toContain('border-b-border')
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
