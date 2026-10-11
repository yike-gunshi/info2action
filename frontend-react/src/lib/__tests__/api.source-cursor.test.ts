import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fetchFeedPlatformMore } from '../api/feed'

const cursor = { kind: 'live' as const, v: 1 as const, scope_key: 'twitter:all', after: { sort_at: null, fetched_at: '2026-01-01T00:00:00Z', relevance_score: null, id: 'last' } }
describe('source live cursor wire contract', () => {
  beforeEach(() => { vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ items: [], has_more: false }) })) })
  afterEach(() => { vi.unstubAllGlobals() })
  it('serializes the live cursor without read-model fields', async () => {
    await fetchFeedPlatformMore('twitter', 20, 50, undefined, undefined, undefined, undefined, undefined, cursor)
    const url = new URL(vi.mocked(fetch).mock.calls[0][0] as string, 'http://localhost')
    expect(JSON.parse(url.searchParams.get('cursor')!)).toEqual(cursor)
  })
})
