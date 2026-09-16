import type { ActionItem, ActionBoardDirection, ActionDirectionSummary } from '../types'
import { apiFetch, handleUnauthorized, serviceUnavailableError } from './_client'

export function toBackendActionPriority(priority: string): string {
  if (priority === 'P0') return 'high'
  if (priority === 'P1') return 'medium'
  if (priority === 'P2') return 'low'
  if (priority === 'BUG') return 'bug'
  return priority
}

export function fromBackendActionPriority(priority?: string): ActionItem['priority'] | undefined {
  if (!priority) return undefined
  if (priority === 'high') return 'P0'
  if (priority === 'medium') return 'P1'
  if (priority === 'low') return 'P2'
  if (priority === 'bug') return 'BUG'
  if (priority === 'P0' || priority === 'P1' || priority === 'P2' || priority === 'BUG') return priority
  return undefined
}

function fromBackendActionType(type?: string): ActionItem['type'] | undefined {
  if (!type) return undefined
  if (type === 'investigate' || type === 'research') return 'research'
  if (type === 'implement' || type === 'implementation') return 'implementation'
  if (type === 'content') return 'content'
  return undefined
}

export function toBackendActionType(type: string): string {
  if (type === 'research') return 'investigate'
  if (type === 'implementation') return 'implement'
  return type
}

function normalizeSourceItemIds(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map(String).filter(Boolean)
  }
  if (typeof value !== 'string') return []
  try {
    const parsed = JSON.parse(value)
    return Array.isArray(parsed) ? parsed.map(String).filter(Boolean) : []
  } catch {
    return []
  }
}

function normalizeAction(action: ActionItem): ActionItem {
  const sourceItemIds = normalizeSourceItemIds(
    (action as ActionItem & { source_item_ids?: unknown }).source_item_ids,
  )
  const rawType = action.type || fromBackendActionType(action.action_type) || action.action_type || 'investigate'
  return {
    ...action,
    type: rawType,
    priority: fromBackendActionPriority(action.priority),
    source_item_ids: sourceItemIds,
  }
}

function normalizeActionsResponse<T extends { actions: ActionItem[] }>(resp: T): T {
  return { ...resp, actions: resp.actions.map(normalizeAction) }
}


// ── Actions ──

export interface ActionsResponse {
  actions: ActionItem[]
  counts: Record<string, number>
  directions: Array<string | ActionDirectionSummary>
  meta?: {
    limit?: number
    offset?: number
    degraded?: boolean
    query_strategy?: string
    [key: string]: unknown
  }
}

export interface ActionsBoardResponse {
  counts: Record<string, number>
  directions: ActionBoardDirection[]
  meta?: {
    limit_per_direction?: number
    offset?: number
    degraded?: boolean
    read_model?: boolean | string
    [key: string]: unknown
  }
}

const actionsBoardInflight = new Map<string, Promise<ActionsBoardResponse>>()
const actionDetailInflight = new Map<string, Promise<ActionItem>>()

export async function fetchActions(params?: {
  status?: string
  action_type?: string
  priority?: string
}): Promise<ActionsResponse> {
  const qs = new URLSearchParams()
  if (params?.status) qs.set('status', params.status)
  if (params?.action_type) qs.set('action_type', toBackendActionType(params.action_type))
  if (params?.priority) qs.set('priority', toBackendActionPriority(params.priority))
  const resp = await apiFetch<ActionsResponse>(`/api/actions?${qs}`)
  return normalizeActionsResponse(resp)
}

export async function fetchActionsBoard(params?: {
  status?: string
  action_type?: string
  priority?: string
  source_filter?: 'with-source' | 'no-source'
  date_filter?: 'today' | 'week'
  direction?: string
  limit_per_direction?: number
  offset?: number
}): Promise<ActionsBoardResponse> {
  const qs = new URLSearchParams()
  if (params?.status) qs.set('status', params.status)
  if (params?.action_type) qs.set('action_type', toBackendActionType(params.action_type))
  if (params?.priority) qs.set('priority', toBackendActionPriority(params.priority))
  if (params?.source_filter) qs.set('source_filter', params.source_filter)
  if (params?.date_filter) qs.set('date_filter', params.date_filter)
  if (params?.direction) qs.set('direction', params.direction)
  if (params?.limit_per_direction) qs.set('limit_per_direction', String(params.limit_per_direction))
  if (params?.offset) qs.set('offset', String(params.offset))
  const path = `/api/actions/board?${qs}`
  const existing = actionsBoardInflight.get(path)
  if (existing) return existing
  const request = apiFetch<ActionsBoardResponse>(path)
    .then((resp) => ({
      ...resp,
      directions: (resp.directions || []).map((direction) => ({
        ...direction,
        items: (direction.items || []).map(normalizeAction),
      })),
    }))
    .finally(() => {
      actionsBoardInflight.delete(path)
    })
  actionsBoardInflight.set(path, request)
  return request
}

export async function fetchActionsByItem(itemId: string): Promise<{ actions: ActionItem[] }> {
  const resp = await apiFetch<{ actions: ActionItem[] }>(`/api/actions/by-item?item_id=${String(itemId)}`)
  return normalizeActionsResponse(resp)
}

export async function fetchAction(id: string): Promise<ActionItem> {
  const path = `/api/actions/${String(id)}`
  const existing = actionDetailInflight.get(path)
  if (existing) return existing
  const request = apiFetch<ActionItem>(path)
    .then((action) => normalizeAction(action))
    .finally(() => {
      actionDetailInflight.delete(path)
    })
  actionDetailInflight.set(path, request)
  return request
}

export async function createAction(data: Partial<ActionItem>): Promise<{ ok: boolean; action_id: string }> {
  return apiFetch('/api/actions', {
    method: 'POST',
    body: JSON.stringify(data),
  })
}

export async function updateAction(id: string, data: Partial<ActionItem>): Promise<{ ok: boolean }> {
  const payload = {
    ...data,
    ...(data.priority ? { priority: toBackendActionPriority(data.priority) } : {}),
  }
  return apiFetch(`/api/actions/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export async function deleteAction(id: string): Promise<void> {
  await apiFetch(`/api/actions/${id}`, { method: 'DELETE' })
}

export async function markActionDone(id: string, conclusion?: string): Promise<{ ok: boolean }> {
  return apiFetch(`/api/actions/${id}/done`, {
    method: 'POST',
    body: JSON.stringify({ conclusion }),
  })
}

export async function dismissAction(id: string, reason?: string): Promise<{ ok: boolean }> {
  return apiFetch(`/api/actions/${id}/dismiss`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export async function updateActionPriority(id: string, priority: string): Promise<{ ok: boolean }> {
  return apiFetch(`/api/actions/${id}/priority`, {
    method: 'PATCH',
    body: JSON.stringify({ priority: toBackendActionPriority(priority) }),
  })
}

export async function dispatchAction(id: string): Promise<{ thread_id: string; thread_url: string }> {
  return apiFetch(`/api/actions/${id}/dispatch`, {
    method: 'POST',
  })
}

/** v21.0 (E2): 复制命令到本地执行后,标记为执行中(owner-scoped,不触发服务器代执行)。 */
export async function markActionExecuting(id: string): Promise<{ ok: boolean }> {
  return apiFetch(`/api/actions/${id}/mark-executing`, {
    method: 'POST',
  })
}

/** v21.0 v2 (§13.4): owner-scoped 状态切换,供行动详情三段式 stepper 调用。 */
export async function setActionStatus(
  id: string,
  status: 'pending' | 'confirmed' | 'done' | 'dismissed',
): Promise<{ ok: boolean; status: string }> {
  return apiFetch(`/api/actions/${id}/status`, {
    method: 'POST',
    body: JSON.stringify({ status }),
  })
}

export interface ActionQuota {
  limit: number
  used: number
  remaining: number
  over_limit: boolean
  unlimited?: boolean
  reset_at?: string | null
}

/** v21.0 (B3): 当日生成配额快照。 */
export async function fetchActionQuota(): Promise<ActionQuota> {
  return apiFetch('/api/user/action-quota')
}

// ── Action Generation (SSE) ──

export interface SSEEvent {
  type: string      // event type: thinking, thinking-ai, stage, result, error
  data: string      // raw data string
  text?: string     // thinking text
  stage?: number    // stage index (0-3)
  name?: string     // stage name
  ok?: boolean      // result success
  action?: Record<string, unknown>  // generated action data
  message?: string  // error message
}

export function generateActionFromItem(
  itemId: string,
  options: {
    actionType?: string
    userHint?: string
  } = {},
  onEvent: (event: SSEEvent) => void = () => {},
  onDone: () => void = () => {},
  onError: (err: Error) => void = () => {},
): AbortController {
  const controller = new AbortController()

  const doFetch = (): Promise<Response> =>
    fetch('/api/actions/generate-from-item', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Connection: 'close' },
      body: JSON.stringify({
        item_id: String(itemId),
        action_type: options.actionType,
        user_hint: options.userHint,
      }),
      signal: controller.signal,
    })

  doFetch()
    .then(async (initial) => {
      // BF-0420-15: SSE fetch 绕过 apiFetch,自己跑 401 处理(refresh + retry + redirect)
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
          // 匿名用户:友好提示,不跳转(不是所有人都想登录才能看首页)
          const e = new Error('请先登录再生成行动点(顶栏右上角)')
          ;(e as Error & { status?: number }).status = 401
          throw e
        }
      }
      if (!res.ok) {
        const e = new Error(`Generate error: ${res.status}`)
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
              onEvent({ type: currentEventType, data: rawData, ...parsed })
            } catch {
              onEvent({ type: currentEventType, data: rawData, text: rawData })
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
