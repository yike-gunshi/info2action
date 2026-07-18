/**
 * 精选页严格时间倒序（头版 tier 体系退役）。
 *
 * 覆盖：
 *   - 高信号条即使分数最高也不提升到日组首位，展示序 = 时间倒序
 *   - 数据全量替换（下拉刷新路径）后仍按时间倒序,无跨刷新置顶记忆
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, cleanup, act } from '@testing-library/react'
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

/** 高信号条（旧 S2 口径下必为当日 argmax）——用于断言它不再被提升 */
function strongCluster(overrides: Partial<ClusterEvent> = {}): ClusterEvent {
  return makeCluster({
    unique_source_count: 6,
    doc_count: 14,
    platforms: ['twitter', 'reddit', 'lingowhale'],
    cover_url: 'https://example.com/cover.jpg',
    ai_summary: '摘'.repeat(240),
    ...overrides,
  })
}

function localDateKey(date: Date): string {
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, '0'),
    String(date.getDate()).padStart(2, '0'),
  ].join('-')
}

function atToday(hour: number): string {
  const d = new Date()
  d.setHours(hour, 0, 0, 0)
  return d.toISOString()
}

/** 标准条标题含内联分类前缀（如「模型 | 」），断言前剥掉 */
function renderedTitles(): string[] {
  return Array.from(
    document.querySelectorAll('[data-testid="event-card"] [data-testid="event-title-text"]'),
  ).map((el) => (el.textContent || '').replace(/^.*\|\s*/, ''))
}

describe('精选页严格时间倒序（tier 退役）', () => {
  beforeEach(() => {
    nextId = 1
    mockFetchEvents.mockReset()
    mockFetchEvents.mockResolvedValue({ enabled: true, events: [], next_cursor: null })
    useEventsStore.getState().reset()
    useFeedStore.setState({ isFetching: false, fetchProgress: null })
  })

  afterEach(cleanup)

  it('高信号条不提升到日组首位，展示序严格 = 时间倒序', () => {
    const todayKey = localDateKey(new Date())
    const newest = makeCluster({ ai_title: '最新弱条', first_doc_at: atToday(11) })
    const strong = strongCluster({ ai_title: '高分旧条', first_doc_at: atToday(9) })
    const oldest = makeCluster({ ai_title: '最早弱条', first_doc_at: atToday(7) })
    useEventsStore.setState({
      enabled: true,
      events: [newest, strong, oldest],
      dateCounts: { [todayKey]: 3 },
      cursor: null,
    })

    render(<LatestEvents variant="page" />)

    expect(renderedTitles()).toEqual(['最新弱条', '高分旧条', '最早弱条'])
  })

  it('数据全量替换(刷新路径)后仍时间倒序，无跨刷新置顶记忆', () => {
    const todayKey = localDateKey(new Date())
    const strong = strongCluster({ ai_title: '高分旧条', first_doc_at: atToday(8) })
    const weak1 = makeCluster({ ai_title: '弱条一', first_doc_at: atToday(7) })
    const weak2 = makeCluster({ ai_title: '弱条二', first_doc_at: atToday(6) })
    useEventsStore.setState({
      enabled: true,
      events: [strong, weak1, weak2],
      dateCounts: { [todayKey]: 3 },
      cursor: null,
    })

    render(<LatestEvents variant="page" />)
    expect(renderedTitles()[0]).toBe('高分旧条')

    // 模拟刷新: 全量替换为含更新弱条的数据(refresh() 即此路径)
    const fresher = makeCluster({ ai_title: '刷新后新条', first_doc_at: atToday(10) })
    act(() => {
      useEventsStore.setState({
        events: [fresher, strong, weak1, weak2],
        dateCounts: { [todayKey]: 4 },
      })
    })

    expect(renderedTitles()).toEqual(['刷新后新条', '高分旧条', '弱条一', '弱条二'])
  })
})
