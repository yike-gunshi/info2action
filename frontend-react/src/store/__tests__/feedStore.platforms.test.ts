import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useFeedStore } from '../feedStore'
import { useAuthStore } from '../authStore'
import { fetchFeedPlatforms } from '../../lib/api'

vi.mock('../../lib/api', () => ({ fetchFeedPlatforms: vi.fn(), fetchFeedSections: vi.fn(), fetchFetchStatus: vi.fn(), triggerFetchAll: vi.fn() }))
const card = { id: 'public-1', title: 'Public', platform: 'twitter', fetched_at: '2026-10-10T00:00:00Z' }
const good = { sections: { twitter: [card] }, platform_counts: { twitter: 30 }, source_counts: {}, category_counts: {}, counts_generated_at: '2026-10-10T00:00:00Z', overview_generated_at: '2026-10-10T00:00:00Z' }

describe('source overview availability and session isolation', () => {
  beforeEach(() => {
    vi.mocked(fetchFeedPlatforms).mockReset()
    useAuthStore.getState().setUser(null)
    useFeedStore.setState({ platformSectionItems: new Map(), platformCounts: {}, sourceCounts: {}, platformCategoryCounts: {}, platformSectionsLoaded: false, platformNextCursors: {}, loadError: null })
  })
  it('accepts a validated stale response and retains its update time', async () => {
    vi.mocked(fetchFeedPlatforms).mockResolvedValue({ ...good, degraded: true, stale: true })
    await useFeedStore.getState().ensurePlatformSections()
    expect(useFeedStore.getState().platformSectionItems.get('twitter')).toEqual([card])
    expect(useFeedStore.getState().platformOverviewStale).toBe(true)
    expect(useFeedStore.getState().platformOverviewGeneratedAt).toBe(good.overview_generated_at)
  })
  it('treats fresh empty data as loaded and does not fetch it again', async () => {
    vi.mocked(fetchFeedPlatforms).mockResolvedValue({ ...good, sections: {}, platform_counts: {} })
    await useFeedStore.getState().ensurePlatformSections()
    await useFeedStore.getState().ensurePlatformSections()
    expect(fetchFeedPlatforms).toHaveBeenCalledTimes(1)
  })
  it('force retry preserves previous cards on unavailable response', async () => {
    vi.mocked(fetchFeedPlatforms).mockResolvedValueOnce(good).mockResolvedValueOnce({ ...good, sections: {}, degraded: true })
    await useFeedStore.getState().ensurePlatformSections()
    await expect(useFeedStore.getState().ensurePlatformSections(true)).rejects.toThrow()
    expect(fetchFeedPlatforms).toHaveBeenCalledTimes(2)
    expect(useFeedStore.getState().platformSectionItems.get('twitter')).toEqual([card])
    expect(useFeedStore.getState().platformLoadError).toBeTruthy()
  })
  it('clears private data on logout and ignores a prior-account in-flight response', async () => {
    useAuthStore.getState().setUser({ id: 'alice', username: 'Alice', email: '', role: 'user' })
    let resolve!: (value: typeof good) => void
    vi.mocked(fetchFeedPlatforms).mockReturnValue(new Promise((r) => { resolve = r }))
    const pending = useFeedStore.getState().ensurePlatformSections()
    useAuthStore.getState().setUser(null)
    resolve({ ...good, sections: { manual: [{ ...card, id: 'private-alice', platform: 'manual' }] } } as unknown as typeof good)
    await pending
    expect(useFeedStore.getState().platformSectionItems.size).toBe(0)
    expect(useFeedStore.getState().platformSectionsLoaded).toBe(false)
  })
})
