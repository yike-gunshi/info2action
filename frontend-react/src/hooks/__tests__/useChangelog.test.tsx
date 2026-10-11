import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ChangelogRelease } from '../../content/changelog'
import { CHANGELOG_SEEN_STORAGE_KEY, useChangelog } from '../useChangelog'

const { releases } = vi.hoisted(() => ({ releases: [] as ChangelogRelease[] }))
vi.mock('../../content/changelog', () => ({ CHANGELOG_RELEASES: releases }))

function release(id: string, publishedAt: string): ChangelogRelease {
  return {
    id,
    publishedAt,
    title: id,
    changes: [{ kind: 'feature', title: '发现产品更新', description: '在日志页查看近期变化。' }],
  }
}

function storageEvent(newValue: string | null, key: string | null = CHANGELOG_SEEN_STORAGE_KEY) {
  window.dispatchEvent(new StorageEvent('storage', { key, newValue }))
}

describe('useChangelog', () => {
  beforeEach(() => {
    window.localStorage.clear()
    releases.splice(0, releases.length, release('latest', '2026-09-21'), release('older', '2026-09-18'))
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-09-21T12:00:00Z'))
  })

  afterEach(() => {
    vi.restoreAllMocks()
    window.localStorage.clear()
    act(() => storageEvent(null, null))
    cleanup()
  })

  it('sorts published releases without mutating the source and excludes future or invalid dates', () => {
    releases.splice(0, releases.length,
      release('older', '2026-09-18'),
      release('future', '2026-09-22'),
      release('bad-date', 'not-a-date'),
      release('impossible-date', '2026-02-30'),
      release('non-iso-date', '09/20/2026'),
      release('latest', '2026-09-21T10:00:00Z'),
      release('future-today', '2026-09-21T20:00:00Z'),
    )
    const originalOrder = releases.map(({ id }) => id)

    const { result } = renderHook(useChangelog)

    expect(result.current.releases.map(({ id }) => id)).toEqual(['latest', 'older'])
    expect(releases.map(({ id }) => id)).toEqual(originalOrder)
    act(() => result.current.markSeen())
    expect(window.localStorage.getItem(CHANGELOG_SEEN_STORAGE_KEY)).toBe('latest')
  })

  it.each([{ unpublished: [] }, { unpublished: [release('future', '2026-09-22')] }])('has no unread indicator or storage writes when no release is published: %j', ({ unpublished }) => {
    releases.splice(0, releases.length, ...unpublished)
    const write = vi.spyOn(Storage.prototype, 'setItem')
    const { result } = renderHook(useChangelog)

    expect(result.current.releases).toEqual([])
    expect(result.current.hasUnread).toBe(false)
    act(() => result.current.markSeen())
    expect(write).not.toHaveBeenCalled()
  })

  it('is unread on the first visit and remembers opening the latest release', () => {
    const { result, unmount } = renderHook(useChangelog)

    expect(result.current.hasUnread).toBe(true)
    expect(window.localStorage.getItem(CHANGELOG_SEEN_STORAGE_KEY)).toBeNull()
    act(() => result.current.markSeen())
    expect(result.current.hasUnread).toBe(false)
    expect(window.localStorage.getItem(CHANGELOG_SEEN_STORAGE_KEY)).toBe('latest')

    unmount()
    const reopened = renderHook(useChangelog)
    expect(reopened.result.current.hasUnread).toBe(false)
  })

  it.each(['', '{broken', 'unknown-release', 'older'])('treats a non-current stored marker as unread: %s', (marker) => {
    window.localStorage.setItem(CHANGELOG_SEEN_STORAGE_KEY, marker)
    const { result } = renderHook(useChangelog)
    expect(result.current.hasUnread).toBe(true)
  })

  it('shows the indicator again when a newer release is shipped', () => {
    releases.splice(0, releases.length, release('older', '2026-09-18'))
    const firstVisit = renderHook(useChangelog)
    act(() => firstVisit.result.current.markSeen())
    firstVisit.unmount()

    releases.push(release('latest', '2026-09-21'))
    const updatedApp = renderHook(useChangelog)
    expect(updatedApp.result.current.hasUnread).toBe(true)
    act(() => updatedApp.result.current.markSeen())
    expect(updatedApp.result.current.hasUnread).toBe(false)
  })

  it('updates every mounted consumer when the page marks the release as seen', () => {
    const first = renderHook(useChangelog)
    const second = renderHook(useChangelog)

    act(() => first.result.current.markSeen())

    expect(first.result.current.hasUnread).toBe(false)
    expect(second.result.current.hasUnread).toBe(false)
  })

  it('syncs another tab marking the release as seen and clearing storage', () => {
    const { result } = renderHook(useChangelog)

    act(() => {
      window.localStorage.setItem(CHANGELOG_SEEN_STORAGE_KEY, 'latest')
      storageEvent('latest')
    })
    expect(result.current.hasUnread).toBe(false)

    act(() => {
      window.localStorage.clear()
      storageEvent(null, null)
    })
    expect(result.current.hasUnread).toBe(true)
  })

  it('ignores unrelated storage events', () => {
    const { result } = renderHook(useChangelog)
    act(() => storageEvent('latest', 'another-feature'))
    expect(result.current.hasUnread).toBe(true)
  })

  it('keeps working when reading storage is blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('Storage blocked') })
    const { result } = renderHook(useChangelog)

    expect(result.current.hasUnread).toBe(true)
    act(() => result.current.markSeen())
    expect(result.current.hasUnread).toBe(false)
  })

  it('clears the indicator for this session even when a stored older marker cannot be overwritten', () => {
    window.localStorage.setItem(CHANGELOG_SEEN_STORAGE_KEY, 'older')
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('Quota exceeded') })
    const first = renderHook(useChangelog)
    const second = renderHook(useChangelog)

    act(() => first.result.current.markSeen())

    expect(first.result.current.hasUnread).toBe(false)
    expect(second.result.current.hasUnread).toBe(false)
    expect(window.localStorage.getItem(CHANGELOG_SEEN_STORAGE_KEY)).toBe('older')
    first.unmount()
    const reopened = renderHook(useChangelog)
    expect(reopened.result.current.hasUnread).toBe(false)
  })
})
