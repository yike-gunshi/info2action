import { describe, it, expect, beforeEach } from 'vitest'
import { resetClientSessionState } from '../sessionReset'
import { useActionStore } from '../actionStore'
import { useDetailStore } from '../detailStore'
import { useFeedStore } from '../feedStore'
import { useUIStore } from '../uiStore'
import type { ActionItem, FeedItem } from '../../lib/types'

const secretItem: FeedItem = {
  id: 'secret-item',
  title: 'Secret',
  platform: 'manual',
  fetched_at: '2026-01-01',
}

const secretAction: ActionItem = {
  id: 'secret-action',
  title: 'Secret action',
  type: 'research',
  status: 'pending',
  created_at: '2026-01-01',
}

beforeEach(() => {
  useDetailStore.setState({
    modalStack: [{ type: 'item', id: 'secret-item' }],
    itemDetail: secretItem,
    actionDetail: secretAction,
    detailCache: new Map([['secret-item', { item: secretItem, cachedAt: Date.now() }]]),
  })
  useActionStore.setState({
    actions: [secretAction],
    counts: { total: 1, pending: 1 },
    directions: [{ slug: 'secret', label: 'Secret', count: 1 }],
    isLoading: true,
  })
  useFeedStore.setState({
    sectionItems: new Map([['manual', [secretItem]]]),
    catCounts: { manual: 1 },
    searchResults: new Map([['manual', []]]),
    searchTotal: 1,
    isSearching: true,
    platformSectionItems: new Map([['manual', []]]),
    platformCounts: { manual: 1 },
    sourceCounts: { manual: { private: 1 } },
    clickedAtById: { 'secret-item': '2026-01-01' },
  })
  useUIStore.setState({ l1: 'actions', expandedKey: 'secret', searchQuery: 'private' })
})

describe('resetClientSessionState', () => {
  it('clears user-scoped stores on logout while preserving theme', () => {
    useUIStore.setState({ theme: 'dark' })

    resetClientSessionState()

    expect(useDetailStore.getState().modalStack).toEqual([])
    expect(useDetailStore.getState().itemDetail).toBeNull()
    expect(useDetailStore.getState().actionDetail).toBeNull()
    expect(useDetailStore.getState().detailCache.size).toBe(0)
    expect(useActionStore.getState().actions).toEqual([])
    expect(useActionStore.getState().counts).toEqual({})
    expect(useFeedStore.getState().sectionItems.size).toBe(0)
    expect(useFeedStore.getState().searchResults).toBeNull()
    expect(useUIStore.getState()).toMatchObject({
      // v18.0 nav-merge: sessionReset 默认 tab 改为 highlights
      l1: 'highlights',
      expandedKey: null,
      searchQuery: '',
      theme: 'dark',
    })
  })
})
