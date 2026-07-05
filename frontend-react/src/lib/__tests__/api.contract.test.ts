import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  authMe,
  fetchAction,
  fetchActions,
  fetchActionsBoard,
  fetchActionsByItem,
  fetchStats,
  updateActionPriority,
} from '../api'
import { useAuthStore } from '../../store/authStore'

function jsonResponse(status: number, body: unknown = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

function mockFetchSequence(responses: Response[]) {
  const calls: Array<[RequestInfo | URL, RequestInit | undefined]> = []
  const queue = [...responses]
  const mock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    calls.push([url, init])
    const next = queue.shift()
    if (!next) throw new Error('fetch mock exhausted')
    return next
  })
  vi.stubGlobal('fetch', mock)
  return { mock, calls }
}

beforeEach(() => {
  useAuthStore.setState({ user: null, isLoading: false, isChecked: true })
  window.location.hash = ''
  vi.restoreAllMocks()
})

describe('api contract hardening', () => {
  it('apiFetch throws the retry response error after a successful refresh', async () => {
    useAuthStore.setState({
      user: { id: 'u1', username: 'alice', email: 'alice@test.local', role: 'user' },
    })
    mockFetchSequence([
      jsonResponse(401, { error: 'old access token' }),
      jsonResponse(200, { ok: true }),
      jsonResponse(500, { error: 'retry failed for real' }),
    ])

    await expect(fetchStats()).rejects.toMatchObject({
      message: 'retry failed for real',
      status: 500,
    })
  })

  it('authMe refreshes a valid refresh session during startup check', async () => {
    const expectedUser = { id: 'u1', username: 'alice', email: 'alice@test.local', role: 'user' }
    const { calls } = mockFetchSequence([
      jsonResponse(401, { error: 'expired access token', can_refresh: false }),
      jsonResponse(200, { ok: true }),
      jsonResponse(200, expectedUser),
    ])

    await expect(authMe()).resolves.toEqual(expectedUser)
    expect(calls.map(([url]) => String(url))).toEqual([
      '/api/auth/me',
      '/api/auth/refresh',
      '/api/auth/me',
    ])
  })

  it('authMe attempts refresh on startup 401 and treats refresh failure as anonymous', async () => {
    const { calls } = mockFetchSequence([
      jsonResponse(401, { error: '未登录', can_refresh: false }),
      jsonResponse(401, { error: '登录已过期' }),
    ])

    await expect(authMe()).rejects.toThrow('Not authenticated')
    expect(calls.map(([url]) => String(url))).toEqual([
      '/api/auth/me',
      '/api/auth/refresh',
    ])
  })

  it('updateActionPriority uses the PATCH backend contract and maps P-levels', async () => {
    const { calls } = mockFetchSequence([jsonResponse(200, { ok: true })])

    await updateActionPriority('act-1', 'P0')

    expect(String(calls[0][0])).toBe('/api/actions/act-1/priority')
    expect(calls[0][1]?.method).toBe('PATCH')
    expect(JSON.parse(String(calls[0][1]?.body))).toEqual({ priority: 'high' })
  })

  it('fetchActions maps UI priority filters and normalizes backend priorities for display', async () => {
    const { calls } = mockFetchSequence([
      jsonResponse(200, {
        actions: [
          { id: 'a1', title: 'Fix', type: 'implementation', status: 'dismissed', priority: 'high', created_at: '2026-01-01' },
        ],
        counts: { total: 1, dismissed: 1 },
        directions: [],
      }),
    ])

    const resp = await fetchActions({ priority: 'P1' })

    expect(String(calls[0][0])).toBe('/api/actions?priority=medium')
    expect(resp.actions[0].priority).toBe('P0')
    expect(resp.actions[0].status).toBe('dismissed')
  })

  it('fetchActionsBoard uses the board endpoint and normalizes nested card priorities', async () => {
    const { calls } = mockFetchSequence([
      jsonResponse(200, {
        counts: { total: 2, pending: 2 },
        directions: [
          {
            slug: 'implementation',
            label: '实施',
            count: 2,
            has_more: true,
            next_offset: 20,
            items: [
              { id: 'a1', title: 'Build', action_type: 'implement', status: 'pending', priority: 'medium', created_at: '2026-01-01' },
            ],
          },
        ],
        meta: { limit_per_direction: 20, offset: 0 },
      }),
    ])

    const resp = await fetchActionsBoard({
      action_type: 'implementation',
      priority: 'P1',
      source_filter: 'with-source',
      date_filter: 'week',
      limit_per_direction: 20,
    })

    expect(String(calls[0][0])).toBe('/api/actions/board?action_type=implement&priority=medium&source_filter=with-source&date_filter=week&limit_per_direction=20')
    expect(resp.directions[0].items[0].type).toBe('implementation')
    expect(resp.directions[0].items[0].priority).toBe('P1')
    expect(resp.directions[0].has_more).toBe(true)
  })

  it('fetchActionsBoard deduplicates identical in-flight board requests', async () => {
    let resolveFetch: (resp: Response) => void = () => {}
    const fetchPromise = new Promise<Response>((resolve) => {
      resolveFetch = resolve
    })
    const mock = vi.fn(() => fetchPromise)
    vi.stubGlobal('fetch', mock)

    const first = fetchActionsBoard({ limit_per_direction: 1 })
    const second = fetchActionsBoard({ limit_per_direction: 1 })
    resolveFetch(jsonResponse(200, {
      counts: { total: 1 },
      directions: [
        {
          slug: 'implementation',
          label: '实施',
          count: 1,
          has_more: false,
          next_offset: null,
          items: [
            { id: 'a1', title: 'Build', action_type: 'implement', status: 'pending', priority: 'medium', created_at: '2026-01-01' },
          ],
        },
      ],
    }))

    const [firstResp, secondResp] = await Promise.all([first, second])

    expect(mock).toHaveBeenCalledTimes(1)
    expect(firstResp.directions[0].items[0].id).toBe('a1')
    expect(secondResp.directions[0].items[0].id).toBe('a1')
  })

  it('fetchAction normalizes backend action_type to the UI action type', async () => {
    mockFetchSequence([
      jsonResponse(200, {
        id: 'act-cluster',
        title: 'Cluster action',
        action_type: 'implement',
        status: 'pending',
        priority: 'medium',
        created_at: '2026-01-01',
      }),
    ])

    const action = await fetchAction('act-cluster')

    expect(action.type).toBe('implementation')
    expect(action.priority).toBe('P1')
  })

  it('fetchAction deduplicates identical in-flight detail requests', async () => {
    let resolveFetch: (resp: Response) => void = () => {}
    const fetchPromise = new Promise<Response>((resolve) => {
      resolveFetch = resolve
    })
    const mock = vi.fn(() => fetchPromise)
    vi.stubGlobal('fetch', mock)

    const first = fetchAction('act-1')
    const second = fetchAction('act-1')
    resolveFetch(jsonResponse(200, {
      id: 'act-1',
      title: 'Detail',
      action_type: 'implement',
      status: 'pending',
      priority: 'medium',
      created_at: '2026-01-01',
    }))

    const [firstResp, secondResp] = await Promise.all([first, second])

    expect(mock).toHaveBeenCalledTimes(1)
    expect(firstResp.type).toBe('implementation')
    expect(secondResp.type).toBe('implementation')
  })

  it('fetchActionsByItem normalizes legacy JSON source item ids into arrays', async () => {
    mockFetchSequence([
      jsonResponse(200, {
        actions: [
          {
            id: 'act-generated',
            title: 'Generated action',
            action_type: 'investigate',
            status: 'pending',
            priority: 'medium',
            source_item_ids: '["doc-low-value"]',
            created_at: '2026-01-01',
          },
        ],
      }),
    ])

    const resp = await fetchActionsByItem('doc-low-value')

    expect(resp.actions[0].priority).toBe('P1')
    expect(resp.actions[0].source_item_ids).toEqual(['doc-low-value'])
  })
})
