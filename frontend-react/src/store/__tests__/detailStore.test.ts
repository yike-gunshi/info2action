import { describe, it, expect, beforeEach } from 'vitest'
import { useDetailStore, readDetailCache, DETAIL_CACHE_TTL_MS } from '../detailStore'
import type { FeedItem } from '../../lib/types'

describe('detailStore', () => {
  beforeEach(() => {
    useDetailStore.setState({
      modalStack: [],
      itemDetail: null,
      itemActions: [],
      actionDetail: null,
      detailCache: new Map(),
      isLoading: false,
    })
  })

  it('openItem pushes to stack', () => {
    useDetailStore.getState().openItem('item-1')
    expect(useDetailStore.getState().modalStack).toEqual([
      { type: 'item', id: 'item-1' },
    ])
  })

  it('openItem does not duplicate the same item at stack top', () => {
    useDetailStore.getState().openItem('item-1')
    useDetailStore.getState().openItem('item-1')
    expect(useDetailStore.getState().modalStack).toEqual([
      { type: 'item', id: 'item-1' },
    ])
  })

  it('openAction pushes to stack', () => {
    useDetailStore.getState().openAction('action-1')
    expect(useDetailStore.getState().modalStack).toEqual([
      { type: 'action', id: 'action-1' },
    ])
  })

  it('openAction does not duplicate the same action at stack top', () => {
    useDetailStore.getState().openAction('action-1')
    useDetailStore.getState().openAction('action-1')
    expect(useDetailStore.getState().modalStack).toEqual([
      { type: 'action', id: 'action-1' },
    ])
  })

  it('goBack pops stack top', () => {
    useDetailStore.getState().openItem('item-1')
    useDetailStore.getState().openAction('action-1')
    expect(useDetailStore.getState().modalStack.length).toBe(2)

    useDetailStore.getState().goBack()
    expect(useDetailStore.getState().modalStack).toEqual([
      { type: 'item', id: 'item-1' },
    ])
  })

  it('closeModal clears stack', () => {
    useDetailStore.getState().openItem('item-1')
    useDetailStore.getState().openAction('action-1')
    useDetailStore.getState().closeModal()

    expect(useDetailStore.getState().modalStack).toEqual([])
    expect(useDetailStore.getState().itemDetail).toBeNull()
    expect(useDetailStore.getState().actionDetail).toBeNull()
  })

  it('openAction -> openItem -> goBack returns to action', () => {
    useDetailStore.getState().openAction('action-1')
    useDetailStore.getState().openItem('item-1')

    expect(useDetailStore.getState().modalStack.length).toBe(2)
    expect(useDetailStore.getState().modalStack[1]).toEqual({ type: 'item', id: 'item-1' })

    useDetailStore.getState().goBack()
    expect(useDetailStore.getState().modalStack).toEqual([
      { type: 'action', id: 'action-1' },
    ])
  })

  it('cacheDetail stores items in cache with timestamp', () => {
    const item: FeedItem = { id: 'item-1', title: 'Cached', platform: 'twitter', fetched_at: '2026-01-01' }
    useDetailStore.getState().cacheDetail('item-1', item)
    const entry = useDetailStore.getState().detailCache.get('item-1')
    expect(entry?.item).toEqual(item)
    expect(typeof entry?.cachedAt).toBe('number')
  })

  it('readDetailCache returns item while within TTL', () => {
    const item: FeedItem = { id: 'item-1', title: 'Cached', platform: 'twitter', fetched_at: '2026-01-01' }
    const now = 1_700_000_000_000
    const cache = new Map([['item-1', { item, cachedAt: now }]])
    expect(readDetailCache(cache, 'item-1', now)).toEqual(item)
    expect(readDetailCache(cache, 'item-1', now + DETAIL_CACHE_TTL_MS - 1)).toEqual(item)
  })

  it('readDetailCache returns null after TTL expires', () => {
    const item: FeedItem = { id: 'item-1', title: 'Stale', platform: 'twitter', fetched_at: '2026-01-01' }
    const now = 1_700_000_000_000
    const cache = new Map([['item-1', { item, cachedAt: now }]])
    expect(readDetailCache(cache, 'item-1', now + DETAIL_CACHE_TTL_MS + 1)).toBeNull()
  })

  it('readDetailCache returns null for missing id', () => {
    const cache = new Map()
    expect(readDetailCache(cache, 'nope')).toBeNull()
  })
})
