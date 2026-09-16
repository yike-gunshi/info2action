import type { FeedEventsResponse, ClusterDetail, ClusterSourcesResponse, ClusterAction, LibraryResponse, FeedEventsCursor, ClusterFeedbackKind } from '../types'
import { apiFetch, handleUnauthorized, serviceUnavailableError } from './_client'

// ── v15.0 事件聚合 ──

/** GET /api/feed/events — 时间线（由后端 pipeline 决定 visible）。
 *  optional auth；page=1 起始；since_version_snapshot 可选用于增量比对。 */
export async function fetchEvents(params?: {
  page?: FeedEventsCursor
  limit?: number
  sinceVersionSnapshot?: number | null
  fetchedSince?: string | null
  timezoneOffsetMinutes?: number
  targetDate?: string
  /** v17.0: L1 分类筛选,多个 categories 为 OR 关系,comma-separated */
  categories?: string[]
}): Promise<FeedEventsResponse> {
  const qs = new URLSearchParams()
  const page = params?.page
  if (page && typeof page === 'object' && page.version_id && page.scope_key && page.rank_after != null) {
    qs.set('cursor', JSON.stringify(page))
  } else if (typeof page === 'number') {
    qs.set('page', String(page))
  }
  if (params?.limit) qs.set('limit', String(params.limit))
  if (params?.sinceVersionSnapshot != null) {
    qs.set('since_version_snapshot', String(params.sinceVersionSnapshot))
  }
  if (params?.fetchedSince) qs.set('fetched_since', params.fetchedSince)
  if (params?.targetDate) qs.set('target_date', params.targetDate)
  if (params?.timezoneOffsetMinutes != null) {
    qs.set('timezone_offset_minutes', String(params.timezoneOffsetMinutes))
  }
  if (params?.categories && params.categories.length > 0) {
    qs.set('categories', params.categories.join(','))
  }
  const query = qs.toString()
  return apiFetch(`/api/feed/events${query ? `?${query}` : ''}`)
}

/** GET /api/clusters/:id — cluster 详情；merged_into 时返回 redirect_to。 */
export async function fetchClusterDetail(id: number): Promise<ClusterDetail> {
  return apiFetch(`/api/clusters/${id}`)
}

/** GET /api/clusters/:id/sources — 来源列表，按 is_primary_source DESC + rank。 */
export async function fetchClusterSources(id: number, params?: {
  page?: number
  limit?: number
}): Promise<ClusterSourcesResponse> {
  const qs = new URLSearchParams()
  if (params?.page) qs.set('page', String(params.page))
  if (params?.limit) qs.set('limit', String(params.limit))
  const query = qs.toString()
  return apiFetch(`/api/clusters/${id}/sources${query ? `?${query}` : ''}`)
}

/** GET /api/clusters/:id/bundle — detail + first-page sources in one request. */
export async function fetchClusterBundle(id: number, params?: {
  page?: number
  limit?: number
}): Promise<import('../types').ClusterBundleResponse> {
  const qs = new URLSearchParams()
  if (params?.page) qs.set('page', String(params.page))
  if (params?.limit) qs.set('limit', String(params.limit))
  const query = qs.toString()
  return apiFetch(`/api/clusters/${id}/bundle${query ? `?${query}` : ''}`)
}

/** POST /api/clusters/:id/click — 写 cluster_status.clicked_at + last_seen_version。 */
export async function clickCluster(id: number): Promise<{ ok: boolean; last_seen_version: number }> {
  return apiFetch(`/api/clusters/${id}/click`, { method: 'POST' })
}

export async function fetchClusterStatuses(clusterIds: number[]): Promise<{ statuses: Array<{ cluster_id: number; clicked_at: string | null; last_seen_version: number | null }> }> {
  return apiFetch('/api/clusters/status/batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cluster_ids: clusterIds }),
  })
}

export interface HighlightsReadingProgress {
  cluster_id: number
  resolved_cluster_id: number
  anchor_sort_at: string
  updated_at: string
  cursor: FeedEventsCursor
  resolution: string
}

export async function getHighlightsReadingProgress(): Promise<{ progress: HighlightsReadingProgress | null }> {
  return apiFetch('/api/reading-progress/highlights')
}

export async function putHighlightsReadingProgress(clusterId: number, keepalive = false): Promise<{ progress: HighlightsReadingProgress | null }> {
  return apiFetch('/api/reading-progress/highlights', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cluster_id: clusterId }),
    keepalive,
  })
}

/** POST /api/clusters/:id/star — 登录态切换 cluster 收藏。 */
export async function setClusterStar(id: number): Promise<{ ok: boolean; starred_at: string | null }> {
  return apiFetch(`/api/clusters/${id}/star`, { method: 'POST' })
}

/** v25.0 POST /api/clusters/:id/feedback — cluster 质量反馈，同 kind 再提交=撤销。 */
export async function setClusterFeedback(
  id: number,
  kind: ClusterFeedbackKind,
  note?: string,
): Promise<{ ok: boolean; feedback_kind: ClusterFeedbackKind | null; feedback_note?: string | null }> {
  return apiFetch(`/api/clusters/${id}/feedback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind, note }),
  })
}

/** v15.1 POST /api/clusters/:id/seen — 标记当前 live_version 为 last_seen_version。
 *  与 /click 区别：/seen 不更新 clicked_at，仅清更新角标。
 *  调用方应把失败 swallow 掉，不影响渲染（feature-spec R7.1）。 */
export async function markClusterSeen(
  id: number,
): Promise<{ cluster_id: number; last_seen_version: number }> {
  return apiFetch(`/api/clusters/${id}/seen`, { method: 'POST' })
}

/** GET /api/library — 历史/收藏的 item + cluster 混合个人内容库。 */
export async function fetchLibrary(params: {
  view: 'history' | 'starred'
  limit?: number
  offset?: number
}): Promise<LibraryResponse> {
  const qs = new URLSearchParams()
  qs.set('view', params.view)
  if (params.limit) qs.set('limit', String(params.limit))
  if (params.offset) qs.set('offset', String(params.offset))
  return apiFetch(`/api/library?${qs.toString()}`)
}

/** GET /api/clusters/:id/actions — 该 cluster 的当前用户 actions。 */
export async function fetchClusterActions(id: number): Promise<{ actions: ClusterAction[] }> {
  return apiFetch(`/api/clusters/${id}/actions`)
}

/** POST /api/clusters/:id/actions — SSE 流式生成行动点。
 *  绕过 apiFetch（fetch+原生 reader），按 feedback_sse_connection_close + feedback_sse_401_shared_helper。 */
export function generateClusterAction(
  clusterId: number,
  options: { userHint?: string; actionType?: string } = {},
  onEvent: (event: { type: string; [k: string]: unknown }) => void = () => {},
  onDone: () => void = () => {},
  onError: (err: Error) => void = () => {},
): AbortController {
  const controller = new AbortController()

  const doFetch = (): Promise<Response> =>
    fetch(`/api/clusters/${clusterId}/actions`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Connection: 'close' },
      body: JSON.stringify({
        user_hint: options.userHint ?? '',
        action_type: options.actionType ?? '',
      }),
      signal: controller.signal,
    })

  doFetch()
    .then(async (initial) => {
      let res = initial
      if (res.status === 401) {
        const verdict = await handleUnauthorized()
        if (verdict === 'retry') {
          res = await doFetch()
        } else if (verdict === 'expired') {
          const e = new Error('Session expired')
          ;(e as Error & { status?: number }).status = 401
          throw e
        } else if (verdict === 'unavailable') {
          // BF-0708-1: 服务端故障,不是没登录
          throw serviceUnavailableError()
        } else {
          const e = new Error('请先登录再生成行动点（顶栏右上角）')
          ;(e as Error & { status?: number }).status = 401
          throw e
        }
      }
      if (!res.ok) {
        const e = new Error(`Cluster action error: ${res.status}`)
        ;(e as Error & { status?: number }).status = res.status
        throw e
      }
      const reader = res.body?.getReader()
      if (!reader) throw new Error('No response body')

      const decoder = new TextDecoder()
      let buffer = ''
      let currentEventType = 'message'

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (line.startsWith('event: ')) {
            currentEventType = line.slice(7).trim()
          } else if (line.startsWith('data: ')) {
            const rawData = line.slice(6)
            try {
              const parsed = JSON.parse(rawData)
              onEvent({ type: currentEventType, ...parsed })
            } catch {
              onEvent({ type: currentEventType, raw: rawData })
            }
            currentEventType = 'message'
          }
        }
      }
      onDone()
    })
    .catch((err) => {
      if (err.name !== 'AbortError') onError(err)
    })

  return controller
}
