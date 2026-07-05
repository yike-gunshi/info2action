import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useFeedStore } from '../feedStore'
import { fetchFeedPlatforms, fetchFeedSections } from '../../lib/api'
import type { FeedItem } from '../../lib/types'

vi.mock('../../lib/api', () => ({
  fetchFeedSections: vi.fn(),
  fetchFeedPlatforms: vi.fn(),
  fetchFetchStatus: vi.fn(),
  triggerFetchAll: vi.fn(),
}))

function makeItem(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'item-1',
    title: 'Claude item',
    platform: 'twitter',
    fetched_at: '2026-05-14T10:00:00Z',
    ai_category: 'products',
    ...overrides,
  }
}

describe('feedStore.serverSearch remote performance behavior', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.mocked(fetchFeedSections).mockReset()
    vi.mocked(fetchFeedPlatforms).mockReset()
    useFeedStore.setState({
      sectionItems: new Map(),
      catCounts: {},
      platformSectionItems: new Map(),
      platformCounts: {},
      sourceCounts: {},
      platformSectionsLoaded: false,
      searchResults: null,
      searchTotal: 0,
      searchCatCounts: {},
      searchPlatformSectionItems: null,
      searchPlatformCounts: {},
      searchSourceCounts: {},
      searchPlatformCategoryCounts: {},
      searchPlatformLoading: false,
      isSearching: false,
    })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('publishes recommend search sections before the heavier platform search resolves', async () => {
    const doc = makeItem({ id: 'doc-1' })
    const platformDoc = makeItem({ id: 'platform-1' })
    let resolvePlatforms!: (value: {
      sections: Record<string, FeedItem[]>
      platform_counts: Record<string, number>
      source_counts: Record<string, Record<string, number>>
      category_counts?: Record<string, Record<string, number>>
    }) => void

    vi.mocked(fetchFeedSections).mockResolvedValue({
      sections: { products: [doc] },
      total: 1,
      cat_counts: { products: 1 },
    })
    vi.mocked(fetchFeedPlatforms).mockReturnValue(new Promise((resolve) => {
      resolvePlatforms = resolve
    }))

    useFeedStore.getState().serverSearch('claude')
    await vi.advanceTimersByTimeAsync(310)
    await Promise.resolve()

    let state = useFeedStore.getState()
    expect(state.isSearching).toBe(false)
    expect(state.searchResults?.get('products')?.[0]?.id).toBe('doc-1')
    expect(state.searchPlatformSectionItems).toBeNull()
    expect(state.searchPlatformLoading).toBe(true)

    resolvePlatforms({
      sections: { twitter: [platformDoc] },
      platform_counts: { twitter: 1 },
      source_counts: { twitter: { following: 1 } },
      category_counts: { twitter: { products: 1 } },
    })
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    state = useFeedStore.getState()
    expect(state.searchPlatformLoading).toBe(false)
    expect(state.searchPlatformSectionItems?.get('twitter')?.[0]?.id).toBe('platform-1')
  })

  it('keeps platform search results unavailable instead of publishing an empty map when platform search fails', async () => {
    const doc = makeItem({ id: 'doc-1' })
    vi.mocked(fetchFeedSections).mockResolvedValue({
      sections: { products: [doc] },
      total: 1,
      cat_counts: { products: 1 },
    })
    vi.mocked(fetchFeedPlatforms).mockRejectedValue(new Error('platform search timeout'))

    useFeedStore.getState().serverSearch('claude')
    await vi.advanceTimersByTimeAsync(310)
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    const state = useFeedStore.getState()
    expect(state.searchResults?.get('products')?.[0]?.id).toBe('doc-1')
    expect(state.searchPlatformLoading).toBe(false)
    expect(state.searchPlatformSectionItems).toBeNull()
  })

  it('does not publish a fake empty platform map when platform search returns degraded', async () => {
    const doc = makeItem({ id: 'doc-1' })
    vi.mocked(fetchFeedSections).mockResolvedValue({
      sections: { products: [doc] },
      total: 1,
      cat_counts: { products: 1 },
    })
    vi.mocked(fetchFeedPlatforms).mockResolvedValue({
      sections: {},
      platform_counts: {},
      source_counts: {},
      degraded: true,
      degraded_reason: 'platform_search_timeout',
    })

    useFeedStore.getState().serverSearch('claude')
    await vi.advanceTimersByTimeAsync(310)
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    const state = useFeedStore.getState()
    expect(state.searchResults?.get('products')?.[0]?.id).toBe('doc-1')
    expect(state.searchPlatformLoading).toBe(false)
    expect(state.searchPlatformSectionItems).toBeNull()
  })

  it('keeps prior search data instead of publishing fake zero when section search returns degraded', async () => {
    const previous = makeItem({ id: 'previous-doc' })
    useFeedStore.setState({
      searchResults: new Map([['products', [previous]]]),
      searchTotal: 9,
      searchCatCounts: { products: 9 },
    })
    vi.mocked(fetchFeedSections).mockResolvedValue({
      sections: {},
      total: 0,
      cat_counts: {},
      degraded: true,
      degraded_reason: 'section_search_timeout',
    })

    useFeedStore.getState().serverSearch('new query')
    await vi.advanceTimersByTimeAsync(310)
    await Promise.resolve()
    await Promise.resolve()

    const state = useFeedStore.getState()
    expect(state.isSearching).toBe(false)
    expect(state.searchPlatformLoading).toBe(false)
    expect(state.searchResults?.get('products')?.[0]?.id).toBe('previous-doc')
    expect(state.searchTotal).toBe(9)
    expect(state.searchCatCounts).toEqual({ products: 9 })
    expect(state.searchPlatformSectionItems).toBeNull()
    expect(fetchFeedPlatforms).not.toHaveBeenCalled()
  })

  // BF-0704-6: 降级不再对用户静默,store 必须暴露 searchDegraded 供 UI 提示
  it('marks searchDegraded when section search returns degraded and clears it on success', async () => {
    vi.mocked(fetchFeedSections).mockResolvedValue({
      sections: {},
      total: 0,
      cat_counts: {},
      degraded: true,
      degraded_reason: 'section_search_timeout',
    })

    useFeedStore.getState().serverSearch('claude')
    await vi.advanceTimersByTimeAsync(310)
    await Promise.resolve()
    await Promise.resolve()

    expect(useFeedStore.getState().searchDegraded).toBe(true)

    const doc = makeItem({ id: 'doc-1' })
    vi.mocked(fetchFeedSections).mockResolvedValue({
      sections: { products: [doc] },
      total: 1,
      cat_counts: { products: 1 },
    })
    vi.mocked(fetchFeedPlatforms).mockResolvedValue({
      sections: {},
      platform_counts: {},
      source_counts: {},
    })

    useFeedStore.getState().serverSearch('claude again')
    await vi.advanceTimersByTimeAsync(310)
    await Promise.resolve()
    await Promise.resolve()

    expect(useFeedStore.getState().searchDegraded).toBe(false)
    expect(useFeedStore.getState().searchResults?.get('products')?.[0]?.id).toBe('doc-1')
  })

  it('marks searchDegraded on fetch failure and resets it on clearSearch', async () => {
    vi.mocked(fetchFeedSections).mockRejectedValue(new Error('search down'))

    useFeedStore.getState().serverSearch('claude')
    await vi.advanceTimersByTimeAsync(310)
    await Promise.resolve()
    await Promise.resolve()

    expect(useFeedStore.getState().searchDegraded).toBe(true)

    useFeedStore.getState().clearSearch()
    expect(useFeedStore.getState().searchDegraded).toBe(false)
  })

  it('does not mark platform sections loaded when background prewarm returns degraded', async () => {
    vi.mocked(fetchFeedPlatforms).mockResolvedValue({
      sections: {},
      platform_counts: {},
      source_counts: {},
      degraded: true,
      degraded_reason: 'platforms_timeout',
    })

    await expect(useFeedStore.getState().ensurePlatformSections()).rejects.toThrow('platforms_timeout')

    const state = useFeedStore.getState()
    expect(state.platformSectionsLoaded).toBe(false)
    expect(state.platformSectionItems.size).toBe(0)
  })
})
