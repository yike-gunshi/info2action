/** Public date directory boundary; never removes underlying events or changes older pagination. */
export const HIGHLIGHTS_ARCHIVE_START = '2026-07-03'

export function getBrowseableHighlightDates(counts: Record<string, number>): string[] {
  return Object.keys(counts).filter((date) => {
    if (date < HIGHLIGHTS_ARCHIVE_START || !/^\d{4}-\d{2}-\d{2}$/.test(date) || !Number.isFinite(counts[date]) || counts[date] <= 0) return false
    const stamp = Date.parse(`${date}T00:00:00Z`)
    return Number.isFinite(stamp) && new Date(stamp).toISOString().slice(0, 10) === date
  }).sort().reverse()
}
