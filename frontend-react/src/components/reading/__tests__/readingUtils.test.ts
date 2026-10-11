import { describe, expect, it } from 'vitest'
import { formatReadingDateTime } from '../readingUtils'

describe('formatReadingDateTime', () => {
  it('formats valid values in local time with a four-digit year', () => {
    const local = new Date(2026, 3, 23, 9, 7, 42)
    expect(formatReadingDateTime(local.toISOString())).toBe('2026-04-23 09:07')
  })

  it('preserves invalid input and handles empty input', () => {
    expect(formatReadingDateTime('not-a-date')).toBe('not-a-date')
    expect(formatReadingDateTime(null)).toBe('')
  })
})
