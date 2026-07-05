import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'

// B6 integration: verify TTL actually gates openItem / prefetchItem at the store level.
// Mock the api module so network calls become observable vi.fn().
vi.mock('../../lib/api', () => ({
  fetchFeedItem: vi.fn(async (id: string) => ({
    id,
    title: `fresh-${id}`,
    platform: 'twitter',
    fetched_at: '2026-01-01',
  })),
}))

import { useDetailStore, DETAIL_CACHE_TTL_MS } from '../detailStore'
import * as api from '../../lib/api'
import type { FeedItem } from '../../lib/types'

const asMock = (f: unknown) => f as ReturnType<typeof vi.fn>

function seedCache(id: string, cachedAt: number, overrides: Record<string, unknown> = {}) {
  const item: FeedItem = { id, title: `cached-${id}`, platform: 'twitter', fetched_at: '2026-01-01', ...overrides }
  useDetailStore.setState({
    detailCache: new Map([[id, { item, cachedAt }]]),
  })
  return item
}

describe('detailStore B6 cache TTL integration', () => {
  beforeEach(() => {
    useDetailStore.setState({
      modalStack: [],
      itemDetail: null,
      itemActions: [],
      actionDetail: null,
      detailCache: new Map(),
      isLoading: false,
    })
    asMock(api.fetchFeedItem).mockClear()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('openItem: cache hit within TTL → itemDetail hydrates synchronously', () => {
    const cached = seedCache('item-1', Date.now())
    useDetailStore.getState().openItem('item-1')
    const s = useDetailStore.getState()
    expect(s.itemDetail).toEqual(cached)
    expect(s.isLoading).toBe(false)
  })

  it('openItem: cache expired → itemDetail null, isLoading true (cache ignored)', () => {
    seedCache('item-2', Date.now() - DETAIL_CACHE_TTL_MS - 1000)
    useDetailStore.getState().openItem('item-2')
    const s = useDetailStore.getState()
    expect(s.itemDetail).toBeNull()
    expect(s.isLoading).toBe(true)
  })

  it('openItem: no cache → itemDetail null, isLoading true', () => {
    useDetailStore.getState().openItem('item-3')
    const s = useDetailStore.getState()
    expect(s.itemDetail).toBeNull()
    expect(s.isLoading).toBe(true)
  })

  it('prefetchItem: cache fresh → fetchFeedItem NOT called (short-circuit)', async () => {
    seedCache('item-4', Date.now())
    useDetailStore.getState().prefetchItem('item-4')
    // microtask flush
    await Promise.resolve()
    expect(asMock(api.fetchFeedItem)).not.toHaveBeenCalled()
  })

  it('prefetchItem: cache expired → fetchFeedItem called (refresh)', async () => {
    seedCache('item-5', Date.now() - DETAIL_CACHE_TTL_MS - 1000)
    useDetailStore.getState().prefetchItem('item-5')
    await Promise.resolve()
    await Promise.resolve()
    expect(asMock(api.fetchFeedItem)).toHaveBeenCalledWith('item-5')
  })

  it('prefetchItem: cold cache → fetchFeedItem called', async () => {
    useDetailStore.getState().prefetchItem('item-6')
    await Promise.resolve()
    await Promise.resolve()
    expect(asMock(api.fetchFeedItem)).toHaveBeenCalledWith('item-6')
  })
})
