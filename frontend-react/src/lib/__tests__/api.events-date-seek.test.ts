import { afterEach, describe, expect, expectTypeOf, it, vi } from 'vitest'

import { fetchEvents } from '../api'
import type { FeedEventsResponse } from '../types'

const ordinary: FeedEventsResponse = {
  enabled: true,
  events: [],
  next_cursor: 2,
  new_since_last_fetch: 0,
  total_available_within_30d: 45,
  date_counts: { '2026-09-04': 24, '2026-09-03': 21 },
}

function mockResponse(body: unknown = ordinary, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('fetchEvents date seek contract', () => {
  it('encodes the date with category filters, supplied Beijing offset and object cursor', async () => {
    const fetchMock = mockResponse()
    const cursor = { version_id: 'version-1', scope_key: 'all', rank_after: 40 }

    await fetchEvents({
      targetDate: '2026-09-03',
      categories: ['coding', 'products'],
      timezoneOffsetMinutes: -480,
      page: cursor,
      limit: 20,
    })

    const url = new URL(String(fetchMock.mock.calls[0][0]), 'http://localhost')
    expect(url.pathname).toBe('/api/feed/events')
    expect(Object.fromEntries(url.searchParams)).toEqual({
      target_date: '2026-09-03',
      categories: 'coding,products',
      timezone_offset_minutes: '-480',
      cursor: JSON.stringify(cursor),
      limit: '20',
    })
    expect(fetchMock.mock.calls[0][1].credentials).toBe('same-origin')
  })

  it('leaves ordinary first-page requests and responses free of date fields', async () => {
    const fetchMock = mockResponse()

    const result = await fetchEvents()

    expect(fetchMock.mock.calls[0][0]).toBe('/api/feed/events')
    expect(result).toEqual(ordinary)
    expect(result).not.toHaveProperty('date_seek')
  })

  it('preserves ordinary numeric pagination and incremental query parameters', async () => {
    const fetchMock = mockResponse()
    await fetchEvents({
      page: 3,
      limit: 20,
      sinceVersionSnapshot: 0,
      fetchedSince: '2026-09-03T16:00:00Z',
      timezoneOffsetMinutes: -480,
      categories: ['models'],
    })

    const url = new URL(String(fetchMock.mock.calls[0][0]), 'http://localhost')
    expect(Object.fromEntries(url.searchParams)).toEqual({
      page: '3', limit: '20', since_version_snapshot: '0',
      fetched_since: '2026-09-03T16:00:00Z',
      timezone_offset_minutes: '-480', categories: 'models',
    })
  })

  it.each([
    { requested_date: '2026-09-03', status: 'found', anchor_event_id: 124 },
    { requested_date: '2026-08-01', status: 'not_found', anchor_event_id: null },
  ] as const)('passes through $status metadata and the full response', async (dateSeek) => {
    const body: FeedEventsResponse = { ...ordinary, date_seek: dateSeek }
    mockResponse(body)

    await expect(fetchEvents({ targetDate: dateSeek.requested_date })).resolves.toEqual(body)
  })

  it('preserves the existing 503 rejection including its status and detail', async () => {
    const fetchMock = mockResponse({ detail: 'Date lookup timed out' }, 503)

    await expect(fetchEvents({ targetDate: '2026-09-03' })).rejects.toMatchObject({
      message: 'Date lookup timed out', status: 503,
    })
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('narrows anchor types by seek status and keeps metadata optional', () => {
    type DateSeek = NonNullable<FeedEventsResponse['date_seek']>
    expectTypeOf<Extract<DateSeek, { status: 'found' }>['anchor_event_id']>().toEqualTypeOf<number>()
    expectTypeOf<Extract<DateSeek, { status: 'not_found' }>['anchor_event_id']>().toEqualTypeOf<null>()
    expectTypeOf<DateSeek['requested_date']>().toEqualTypeOf<string>()
    expectTypeOf<undefined>().toMatchTypeOf<FeedEventsResponse['date_seek']>()
  })
})
