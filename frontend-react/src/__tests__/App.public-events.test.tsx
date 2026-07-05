import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import App from '../App'
import { authMe, fetchClassification, fetchFeedPlatforms, fetchFeedSections } from '../lib/api'
import { useAuthStore } from '../store/authStore'
import { useDetailStore } from '../store/detailStore'
import { useEventsStore } from '../store/eventsStore'
import { useFeedStore } from '../store/feedStore'
import { useUIStore } from '../store/uiStore'

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({ mode: 'light', setMode: vi.fn(), toggle: vi.fn() }),
}))

vi.mock('../hooks/useHash', () => ({
  useHash: () => ({ updateHash: vi.fn() }),
  // v18.0 nav-merge: App.tsx getInitialDashboardView 调用 mapLegacyL1
  mapLegacyL1: (raw: string | null | undefined) => {
    if (!raw) return 'highlights'
    if (raw === 'recommend' || raw === 'channels' || raw === 'info') return 'info'
    if (raw === 'highlights' || raw === 'actions') return raw
    return 'highlights'
  },
}))

vi.mock('../lib/api', () => ({
  authMe: vi.fn(),
  fetchFeedSections: vi.fn(),
  fetchFeedPlatforms: vi.fn(),
  fetchClassification: vi.fn(),
}))

vi.mock('../components/layout/TopBar', () => ({
  TopBar: () => <div data-testid="topbar" />,
}))

vi.mock('../components/layout/L2Pills', () => ({
  L2Pills: () => <div data-testid="l2-pills" />,
}))

vi.mock('../components/feed/FeedSection', () => ({
  FeedSection: () => <section data-testid="feed-section" />,
}))

vi.mock('../components/events/LatestEvents', () => ({
  LatestEvents: () => <div data-testid="latest-events" />,
}))

// v18.0 nav-merge: 信息 tab 复用 ChannelsView，但 import 改走 InfoView 别名
vi.mock('../components/info/InfoView', () => ({
  InfoView: () => <div data-testid="info-view" />,
}))

vi.mock('../components/cluster/ClusterDetailPanel', () => ({
  ClusterDetailPanel: () => <div data-testid="cluster-detail-panel" />,
}))

vi.mock('../components/detail/DetailPanel', () => ({
  DetailPanel: () => <div data-testid="detail-panel" />,
}))

vi.mock('../pages/ClusterFullPage', () => ({
  ClusterFullPage: ({ clusterId }: { clusterId: number }) => (
    <div data-testid="cluster-full-page">cluster {clusterId}</div>
  ),
}))

vi.mock('sonner', () => ({
  Toaster: () => <div data-testid="toaster" />,
  toast: {
    error: vi.fn(),
    info: vi.fn(),
    success: vi.fn(),
  },
}))

function resetStores() {
  useAuthStore.setState({ user: null, isLoading: true, isChecked: false })
  useDetailStore.setState({ modalStack: [] })
  useEventsStore.getState().reset()
  // v17.0: 默认 tab 改为 'highlights'（推翻原 'recommend' 默认）
  useUIStore.setState({ l1: 'highlights', expandedKey: null, searchQuery: '', theme: 'light' })
  useFeedStore.setState({
    sectionItems: new Map(),
    catCounts: {},
    platformSectionItems: new Map(),
    platformCounts: {},
    sourceCounts: {},
    clickedAtById: {},
    searchResults: null,
    searchTotal: 0,
    searchCatCounts: {},
    searchPlatformSectionItems: null,
    searchPlatformCounts: {},
    searchSourceCounts: {},
    searchPlatformCategoryCounts: {},
    searchPlatformLoading: false,
    isSearching: false,
    classification: null,
    isLoading: true,
    loadError: null,
    isFetching: false,
  })
}

describe('App public event aggregation entry', () => {
  beforeEach(() => {
    window.location.hash = ''
    resetStores()
    vi.mocked(authMe).mockRejectedValue(new Error('Not authenticated'))
    vi.mocked(fetchFeedSections).mockResolvedValue({ sections: {}, total: 0, cat_counts: {} })
    vi.mocked(fetchFeedPlatforms).mockResolvedValue({
      sections: {},
      platform_counts: {},
      source_counts: {},
    })
    vi.mocked(fetchClassification).mockResolvedValue({ categories: [] })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    resetStores()
  })

  it('renders LatestEvents on the anonymous default (highlights) dashboard', async () => {
    // v17.0: 默认 tab = 'highlights'（HighlightsView 内嵌 LatestEvents）
    render(<App />)

    expect(await screen.findByTestId('latest-events')).toBeInTheDocument()
  })

  it('renders highlights immediately even when recommend sections are still loading', async () => {
    vi.mocked(fetchFeedSections).mockImplementation(() => new Promise(() => {}))

    render(<App />)

    expect(await screen.findByTestId('latest-events')).toBeInTheDocument()
    expect(fetchFeedSections).not.toHaveBeenCalled()
  })

  it('keeps the toast viewport mounted on the cluster full page', async () => {
    window.location.hash = '#cluster=1326'

    render(<App />)

    expect(await screen.findByTestId('cluster-full-page')).toHaveTextContent('1326')
    expect(screen.getByTestId('toaster')).toBeInTheDocument()
  })

  it('keeps the action detail modal host mounted on the cluster full page', async () => {
    window.location.hash = '#cluster=1326'

    render(<App />)

    expect(await screen.findByTestId('cluster-full-page')).toHaveTextContent('1326')
    expect(screen.queryByTestId('detail-panel')).not.toBeInTheDocument()

    act(() => {
      useDetailStore.getState().openAction('act-1')
    })

    expect(await screen.findByTestId('detail-panel')).toBeInTheDocument()
  })

  it('does not start a full channel load while global search is pending', async () => {
    render(<App />)

    expect(await screen.findByTestId('latest-events')).toBeInTheDocument()
    vi.mocked(fetchFeedPlatforms).mockClear()

    act(() => {
      useFeedStore.setState({
        isSearching: true,
        searchResults: null,
        searchPlatformSectionItems: null,
        searchPlatformLoading: true,
      })
      // v18.0 nav-merge: 'channels' 已合并到 'info'
      useUIStore.setState({ l1: 'info', searchQuery: 'claude' })
    })
    await act(async () => {
      await Promise.resolve()
    })

    expect(fetchFeedPlatforms).not.toHaveBeenCalled()
    expect(screen.queryByTestId('info-view')).not.toBeInTheDocument()
  })

  it('shows channel search results without triggering the full channel loader', async () => {
    render(<App />)

    expect(await screen.findByTestId('latest-events')).toBeInTheDocument()
    vi.mocked(fetchFeedPlatforms).mockClear()

    act(() => {
      useFeedStore.setState({
        isSearching: false,
        searchResults: new Map([['products', []]]),
        searchPlatformSectionItems: new Map([['twitter', []]]),
        searchPlatformLoading: false,
      })
      // v18.0 nav-merge: 'channels' 已合并到 'info'
      useUIStore.setState({ l1: 'info', searchQuery: 'claude' })
    })

    expect(await screen.findByTestId('info-view')).toBeInTheDocument()
    expect(fetchFeedPlatforms).not.toHaveBeenCalled()
  })
})
