import { useCallback, useMemo, useSyncExternalStore } from 'react'
import { CHANGELOG_RELEASES } from '../content/changelog'

export const CHANGELOG_SEEN_STORAGE_KEY = 'info2act.changelog.seen.v1'

const listeners = new Set<() => void>()
let sessionSeenId: string | null = null
let storageWriteFailed = false

function readSeenId(): string | null {
  if (storageWriteFailed) return sessionSeenId
  try {
    return window.localStorage.getItem(CHANGELOG_SEEN_STORAGE_KEY)
  } catch {
    return sessionSeenId
  }
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  const handleStorage = (event: StorageEvent) => {
    if (event.key !== CHANGELOG_SEEN_STORAGE_KEY && event.key !== null) return
    try {
      if (event.storageArea && event.storageArea !== window.localStorage) return
    } catch { /* Use the event value when browser storage access is unavailable. */ }
    sessionSeenId = event.newValue
    storageWriteFailed = false
    listener()
  }
  window.addEventListener('storage', handleStorage)
  return () => {
    listeners.delete(listener)
    window.removeEventListener('storage', handleStorage)
  }
}

function publicationTime(value: string): number {
  if (!/^\d{4}-\d{2}-\d{2}(?:T.*)?$/.test(value)) return NaN
  const day = value.slice(0, 10)
  const dayTime = Date.parse(`${day}T00:00:00Z`)
  // Date.parse normalizes impossible dates such as February 30 instead of rejecting them.
  if (!Number.isFinite(dayTime) || new Date(dayTime).toISOString().slice(0, 10) !== day) return NaN
  return Date.parse(value)
}

export function useChangelog() {
  const releases = useMemo(() => {
    const now = Date.now()
    return CHANGELOG_RELEASES
      .map((release) => ({ release, time: publicationTime(release.publishedAt) }))
      .filter(({ time }) => Number.isFinite(time) && time <= now)
      .sort((a, b) => b.time - a.time)
      .map(({ release }) => release)
  }, [])
  const latestId = releases[0]?.id
  const seenId = useSyncExternalStore(subscribe, readSeenId, () => null)

  // Call only after the log page has rendered published content, never on icon hover/click.
  const markSeen = useCallback(() => {
    if (!latestId) return
    sessionSeenId = latestId
    try {
      window.localStorage.setItem(CHANGELOG_SEEN_STORAGE_KEY, latestId)
      storageWriteFailed = false
    } catch {
      storageWriteFailed = true
    }
    listeners.forEach((listener) => listener())
  }, [latestId])

  return { releases, hasUnread: Boolean(latestId && latestId !== seenId), markSeen }
}
