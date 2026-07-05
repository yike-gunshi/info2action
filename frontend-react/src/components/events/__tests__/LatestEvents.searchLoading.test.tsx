/**
 * BF-0704-6 rev3:精选页搜索加载态。
 *
 * 覆盖:
 *   - searching 期间显示"正在搜索 …"状态条,旧时间线压暗(opacity)且禁点
 *   - 搜索完成显示"共 N 个事件匹配"计数条(>1000 显示 1000+)
 *   - 非搜索态不渲染状态条/计数条
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

describe('BF-0704-6 rev3: LatestEvents 搜索加载态', () => {
  beforeEach(() => {
    mockFetchEvents.mockReset()
    mockFetchEvents.mockResolvedValue({ enabled: true, events: [makeCluster()], next_cursor: null })
    useEventsStore.getState().reset()
    useFeedStore.setState({ isFetching: false, fetchProgress: null })
  })

  afterEach(cleanup)

  it('searching 期间显示加载状态条并压暗旧时间线', () => {
    useEventsStore.setState({
      events: [makeCluster()],
      searching: true,
      searchQuery: 'openai',
      searchResults: null,
    })
    render(<LatestEvents variant="page" showEmptyState />)
    const loading = screen.getByTestId('events-search-loading')
    expect(loading.textContent).toContain('正在搜索')
    expect(loading.textContent).toContain('openai')
    const timeline = screen.getByTestId('event-timeline')
    expect(timeline.className).toContain('opacity-50')
    expect(timeline.className).toContain('pointer-events-none')
  })

  it('搜索完成显示计数条,时间线恢复正常', () => {
    useEventsStore.setState({
      events: [makeCluster()],
      searching: false,
      searchQuery: 'openai',
      searchResults: [makeCluster({ id: 2, ai_title: 'OpenAI 新模型' })],
      searchTotal: 608,
    })
    render(<LatestEvents variant="page" showEmptyState />)
    expect(screen.getByTestId('events-search-result-count').textContent).toContain('共 608 个事件匹配')
    expect(screen.queryByTestId('events-search-loading')).toBeNull()
    expect(screen.getByTestId('event-timeline').className).not.toContain('opacity-50')
  })

  it('计数超 1000 显示 1000+', () => {
    useEventsStore.setState({
      events: [makeCluster()],
      searching: false,
      searchQuery: 'agent',
      searchResults: [makeCluster({ id: 3 })],
      searchTotal: 1001,
    })
    render(<LatestEvents variant="page" showEmptyState />)
    expect(screen.getByTestId('events-search-result-count').textContent).toContain('1000+')
  })

  it('非搜索态不渲染状态条与计数条', () => {
    useEventsStore.setState({ events: [makeCluster()], searching: false, searchQuery: '', searchResults: null })
    render(<LatestEvents variant="page" showEmptyState />)
    expect(screen.queryByTestId('events-search-loading')).toBeNull()
    expect(screen.queryByTestId('events-search-result-count')).toBeNull()
  })
})
