import { create } from 'zustand'
import { fetchDailyDigests } from '../lib/api'
import type { DailyDigest } from '../lib/types'

interface DateRange {
  start: string
  end: string
}

interface DailyDigestState {
  digestsByDate: Record<string, DailyDigest>
  loadedRange: DateRange | null
  loading: boolean
  error: string | null
  loadRange: (start: string, end: string, force?: boolean) => Promise<void>
  reset: () => void
}

let loadSequence = 0
// The API allows end - start <= 31 days (32 dates including both endpoints).
const MAX_RANGE_SPAN_MS = 31 * 24 * 60 * 60 * 1000

function rangeContains(range: DateRange | null, start: string, end: string): boolean {
  return Boolean(range && range.start <= start && range.end >= end)
}

function mergedRange(current: DateRange | null, start: string, end: string): DateRange {
  if (!current) return { start, end }
  return {
    start: current.start < start ? current.start : start,
    end: current.end > end ? current.end : end,
  }
}

export const useDailyDigestStore = create<DailyDigestState>((set, get) => ({
  digestsByDate: {},
  loadedRange: null,
  loading: false,
  error: null,

  loadRange: async (start, end, force = false) => {
    const loadedRange = get().loadedRange
    const covered = rangeContains(loadedRange, start, end)
    if (!force && covered) return
    const combinedRange = mergedRange(loadedRange, start, end)
    const canCombine = Date.parse(combinedRange.end) - Date.parse(combinedRange.start) <= MAX_RANGE_SPAN_MS
    // Fetch the gap before claiming continuous coverage. A long-lived cache
    // can exceed the API window; in that case track only this requested range.
    const requestRange = covered || !canCombine ? { start, end } : combinedRange
    const nextLoadedRange = canCombine ? combinedRange : requestRange
    const sequence = ++loadSequence
    set({ loading: true, error: null })
    try {
      const response = await fetchDailyDigests(requestRange.start, requestRange.end)
      if (sequence !== loadSequence) return
      const nextDigests = { ...get().digestsByDate }
      for (const date of Object.keys(nextDigests)) {
        if (date >= requestRange.start && date <= requestRange.end) delete nextDigests[date]
      }
      for (const digest of response.digests) nextDigests[digest.date] = digest
      set({
        digestsByDate: nextDigests,
        loadedRange: nextLoadedRange,
        loading: false,
      })
    } catch (error) {
      if (sequence !== loadSequence) return
      set({
        loading: false,
        error: error instanceof Error ? error.message : 'Failed to load daily digests',
      })
    }
  },

  reset: () => {
    loadSequence += 1
    set({ digestsByDate: {}, loadedRange: null, loading: false, error: null })
  },
}))
