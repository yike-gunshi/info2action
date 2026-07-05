import type {
  FeedItem,
  ActionItem,
  StatsResponse,
  HealthStatus,
  ClassificationConfig,
  FeedEventsResponse,
  ClusterDetail,
  ClusterSourcesResponse,
  ClusterAction,
  LibraryResponse,
  SearchRecommendResponse,
  SearchDocsResponse,
  SearchContext,
  InfoReadModelCursor,
  FeedEventsCursor,
  ActionBoardDirection,
  ActionDirectionSummary,
} from './types'
import type { AuthUser } from '../store/authStore'

const BASE = '' // same origin, proxied by Vite in dev

/** Attempt token refresh once, then give up */
let refreshPromise: Promise<boolean> | null = null

async function tryRefresh(): Promise<boolean> {
  if (refreshPromise) return refreshPromise
  refreshPromise = fetch(`${BASE}/api/auth/refresh`, {
    method: 'POST',
    credentials: 'same-origin',
  })
    .then((r) => r.ok)
    .finally(() => { refreshPromise = null })
  return refreshPromise
}

/**
 * 处理 401 响应的共享语义(BF-0420-15 + BF-0420-19 共根治)。
 *
 * 返回:
 *   - 'retry'   → 刷 token 成功,caller 应重试原请求
 *   - 'expired' → 用户本来登录但 refresh 失败,已清 authStore + 跳转登录页
 *   - 'anon'    → 本就匿名,不跳转,caller 自行给"请先登录"提示
 */
async function handleUnauthorized(): Promise<'retry' | 'expired' | 'anon'> {
  const { useAuthStore } = await import('../store/authStore')
  const currentUser = useAuthStore.getState().user
  if (!currentUser) return 'anon'

  const refreshed = await tryRefresh()
  if (refreshed) return 'retry'

  // BF-0420-19: refresh 失败时必须清 authStore,否则 UI 残留"已登录"头衔但 API 全挂
  useAuthStore.getState().setUser(null)
  window.location.hash = 'login'
  return 'expired'
}

async function apiErrorFromResponse(res: Response): Promise<Error & { status?: number }> {
  const body = await res.json().catch(() => ({}))
  const msg = res.status === 401
    ? (body.detail || body.error || '请先登录(顶栏右上角)')
    : (body.detail || body.error || `API error: ${res.status}`)
  const err = new Error(msg) as Error & { status?: number }
  err.status = res.status
  return err
}

/** Generic fetch wrapper with 401 interceptor */
async function apiFetch<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${url}`, {
    ...options,
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  })

  if (res.status === 401 && !url.includes('/api/auth/')) {
    const verdict = await handleUnauthorized()
    if (verdict === 'retry') {
      const retry = await fetch(`${BASE}${url}`, {
        ...options,
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          ...options?.headers,
        },
      })
      if (retry.ok) return retry.json()
      // 二次也 401 → 视作 expired;清 store + 跳转
      if (retry.status === 401) {
        const { useAuthStore } = await import('../store/authStore')
        useAuthStore.getState().setUser(null)
        window.location.hash = 'login'
        throw new Error('Session expired')
      }
      throw await apiErrorFromResponse(retry)
    } else if (verdict === 'expired') {
      throw new Error('Session expired')
    }
    // verdict === 'anon' → 继续走下方 !res.ok 分支,返"请先登录"
  }

  if (!res.ok) {
    throw await apiErrorFromResponse(res)
  }
  return res.json()
}

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

// ── Auth ──

export async function authLogin(login: string, password: string): Promise<AuthUser> {
  const res = await fetch(`${BASE}/api/auth/login`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ login, password }),
  })
  const body = await res.json().catch(() => ({}))

  // 403 = email not verified → redirect to verify page
  if (res.status === 403 && body.verify_email) {
    window.location.hash = `verify-email?email=${encodeURIComponent(body.email)}`
    throw new Error(body.error || '请先验证邮箱')
  }

  if (!res.ok) {
    throw new Error(body.error || body.detail || `Login failed: ${res.status}`)
  }
  return body.user
}

export async function authRegister(data: {
  username: string
  email: string
  password: string
  invite_code?: string // P1-4 开放注册时可省略
}): Promise<{ ok: boolean; verify_email: boolean; email: string; message: string }> {
  return apiFetch('/api/auth/register', {
    method: 'POST',
    body: JSON.stringify(data),
  })
}

export async function authLogout(): Promise<void> {
  await apiFetch('/api/auth/logout', { method: 'POST' })
}

export async function authMe(): Promise<AuthUser> {
  const readMe = () => fetch(`${BASE}/api/auth/me`, { credentials: 'same-origin' })
  let res = await readMe()
  if (res.status === 401) {
    const refreshed = await tryRefresh()
    if (refreshed) res = await readMe()
  }
  if (!res.ok) throw new Error('Not authenticated')
  return res.json()
}

export async function authRefresh(): Promise<{ ok: boolean }> {
  return apiFetch('/api/auth/refresh', { method: 'POST' })
}

export async function authVerifyEmail(email: string, code: string): Promise<{ ok: boolean; user: AuthUser }> {
  return apiFetch('/api/auth/verify-email', {
    method: 'POST',
    body: JSON.stringify({ email, code }),
  })
}

export async function authResendCode(email: string): Promise<{ ok: boolean }> {
  return apiFetch('/api/auth/resend-code', {
    method: 'POST',
    body: JSON.stringify({ email }),
  })
}

export async function authForgotPassword(email: string): Promise<{ ok: boolean; message: string }> {
  return apiFetch('/api/auth/forgot-password', {
    method: 'POST',
    body: JSON.stringify({ email }),
  })
}

export async function authResetPassword(token: string, password: string): Promise<{ ok: boolean; message: string }> {
  return apiFetch('/api/auth/reset-password', {
    method: 'POST',
    body: JSON.stringify({ token, password }),
  })
}

// ── User Settings ──

export async function getUserSettings(): Promise<{
  discord_bot_token: string | null
  has_discord_token: boolean
}> {
  return apiFetch('/api/user/settings')
}

export async function updateUserSettings(data: {
  discord_bot_token?: string
}): Promise<{ ok: boolean }> {
  return apiFetch('/api/user/settings', {
    method: 'PUT',
    body: JSON.stringify(data),
  })
}

// ── User Profile ──

export interface UserProfile {
  role: string | null
  interests: string[]
  tools: string[]
  manifest: string | null
}

export async function getUserProfile(): Promise<{ profile: UserProfile | null; onboarding_completed: boolean }> {
  return apiFetch('/api/user/profile')
}

export async function updateUserProfile(data: {
  role?: string
  interests?: string[]
  tools?: string[]
  manifest?: string
  onboarding_completed?: boolean
}): Promise<{ ok: boolean; profile: UserProfile }> {
  return apiFetch('/api/user/profile', {
    method: 'PUT',
    body: JSON.stringify(data),
  })
}

// ── Admin ──

export interface InviteCode {
  code: string
  created_by: string
  used_by: string | null
  max_uses: number
  used_count: number
  expires_at: string | null
  created_at: string
}

export async function getInviteCodes(): Promise<{ codes: InviteCode[] }> {
  return apiFetch('/api/admin/invite-codes')
}

export async function createInviteCodes(count: number = 1, maxUses: number = 1): Promise<{ codes: string[] }> {
  return apiFetch('/api/admin/invite-codes', {
    method: 'POST',
    body: JSON.stringify({ count, max_uses: maxUses }),
  })
}

export async function deleteInviteCode(code: string): Promise<{ ok: boolean }> {
  return apiFetch(`/api/admin/invite-codes/${code}`, { method: 'DELETE' })
}

export interface AdminUser {
  id: string
  username: string
  email: string
  role: string
  created_at: string
  last_login_at: string | null
}

export async function getUsers(): Promise<{ users: AdminUser[] }> {
  return apiFetch('/api/admin/users')
}

export interface FetchRunDistribution {
  platform?: string
  source?: string
  pill?: string
  count: number
}

export interface FetchRunAudit {
  version?: string
  source?: string
  duration_sec?: number | null
  stage_durations_sec?: Record<string, number>
  result_status?: string | null
  new_items_count?: number | null
  platform_counts?: FetchRunDistribution[]
  platform_source_counts?: FetchRunDistribution[]
  pill_counts?: FetchRunDistribution[]
  ai_summary?: {
    summarized?: number | null
    failed?: number | null
    pending?: number | null
  }
  event_cluster?: {
    clustered_items?: number | null
    touched_clusters?: number | null
    published_clusters?: number | null
  }
  errors?: Array<{ scope: string; message: string }>
}

export interface FetchRunSummary {
  id: number
  started_at: string
  finished_at: string | null
  status: string
  error_msg?: string | null
  duration_sec?: number | null
  total_new_items?: number | null
  audit: FetchRunAudit
}

export interface FetchRunItem {
  id: string
  title: string
  platform: string
  source: string
  url?: string | null
  pill?: string
  ai_status: 'summarized' | 'failed' | 'pending' | string
  cluster_status: 'clustered' | 'pending' | string
  cluster_id?: number | null
  created_at?: string
  fetched_at?: string
}

export interface EmbeddingUsageSummary {
  total_calls: number
  success_calls: number
  failed_calls: number
  input_count: number
  input_chars: number
  input_bytes: number
  estimated_tokens_attempted: number
  estimated_tokens_success: number
  output_count: number
  estimated_cost_yuan_success: number
  estimated_cost_yuan_all: number
}

export interface EmbeddingUsageGroup {
  source?: string | null
  stage?: string | null
  provider?: string | null
  model?: string | null
  status?: string | null
  run_id?: number | null
  calls: number
  success_calls?: number
  input_count: number
  input_chars?: number
  estimated_tokens: number
  output_count: number
  estimated_cost_yuan: number
}

export interface EmbeddingUsageLog {
  id: number
  created_at: string
  provider: string
  model?: string | null
  mode?: string | null
  source?: string | null
  stage?: string | null
  run_id?: number | null
  caller_file?: string | null
  caller_func?: string | null
  input_count: number
  input_chars: number
  input_bytes: number
  estimated_tokens: number
  output_count: number
  output_dim?: number | null
  status: string
  error?: string | null
  latency_ms?: number | null
  price_yuan_per_1k_tokens?: number | null
  estimated_cost_yuan?: number | null
  item_ids_json?: string | null
}

export interface EmbeddingUsageResponse {
  hours: number
  run_id?: number | null
  summary: EmbeddingUsageSummary
  by_source: EmbeddingUsageGroup[]
  by_run: EmbeddingUsageGroup[]
  logs: EmbeddingUsageLog[]
  limit: number
}

export interface AdminOverviewResponse {
  codes: InviteCode[]
  users: AdminUser[]
  fetch_runs: {
    runs: FetchRunSummary[]
    limit: number
    offset: number
  }
  embedding_usage: EmbeddingUsageResponse
}

export async function getAdminOverview(): Promise<AdminOverviewResponse> {
  return apiFetch('/api/admin/overview')
}

export async function getFetchRuns(params?: {
  limit?: number
  offset?: number
}): Promise<{ runs: FetchRunSummary[]; limit: number; offset: number }> {
  const qs = new URLSearchParams()
  if (params?.limit) qs.set('limit', String(params.limit))
  if (params?.offset) qs.set('offset', String(params.offset))
  return apiFetch(`/api/admin/fetch-runs?${qs}`)
}

export async function getFetchRun(runId: number): Promise<{ run: FetchRunSummary }> {
  return apiFetch(`/api/admin/fetch-runs/${runId}`)
}

export async function getFetchRunItems(
  runId: number,
  params?: {
    platform?: string
    source?: string
    limit?: number
    offset?: number
  },
): Promise<{
  run_id: number
  platform?: string
  source_name?: string
  items: FetchRunItem[]
  total: number
  limit: number
  offset: number
}> {
  const qs = new URLSearchParams()
  if (params?.platform) qs.set('platform', params.platform)
  if (params?.source) qs.set('source', params.source)
  if (params?.limit) qs.set('limit', String(params.limit))
  if (params?.offset) qs.set('offset', String(params.offset))
  return apiFetch(`/api/admin/fetch-runs/${runId}/items?${qs}`)
}

export async function getEmbeddingUsage(params?: {
  hours?: number
  runId?: number
  limit?: number
}): Promise<EmbeddingUsageResponse> {
  const qs = new URLSearchParams()
  if (params?.hours) qs.set('hours', String(params.hours))
  if (params?.runId) qs.set('run_id', String(params.runId))
  if (params?.limit) qs.set('limit', String(params.limit))
  return apiFetch(`/api/admin/embedding-usage?${qs}`)
}

// ── Feed ──

export async function fetchFeed(params?: {
  page?: number
  per_page?: number
  limit?: number
  offset?: number
  platform?: string
  category?: string
  source?: string
  starred?: boolean
  clicked?: boolean
  search?: string
}): Promise<{ items: FeedItem[]; total: number }> {
  const qs = new URLSearchParams()
  if (params?.limit) qs.set('limit', String(params.limit))
  if (params?.offset) qs.set('offset', String(params.offset))
  if (params?.platform) qs.set('platform', params.platform)
  if (params?.source) qs.set('source', params.source)
  if (params?.starred) qs.set('starred', 'true')
  if (params?.clicked) qs.set('clicked', 'true')
  if (params?.search) qs.set('search', params.search)
  return apiFetch(`/api/feed?${qs}`)
}

export async function fetchFeedItem(id: string): Promise<FeedItem> {
  return apiFetch(`/api/feed/item/${String(id)}`)
}

export async function fetchFeedItemsBundle(ids: string[]): Promise<{ items: FeedItem[] }> {
  const unique = Array.from(new Set(ids.map((id) => String(id)).filter(Boolean))).slice(0, 30)
  if (unique.length === 0) return { items: [] }
  const qs = new URLSearchParams({ ids: unique.join(',') })
  return apiFetch(`/api/feed/items/bundle?${qs}`)
}

export interface ItemAsrResponse {
  task_id: string | null
  status: 'success' | 'running' | string
  asr_text?: string | null
  asr_segments?: unknown
  asr_text_cn?: string | null
  asr_segments_cn?: unknown
  asr_cost_yuan?: number | null
  ai_summary?: string | null
  asr_duration_sec?: number | null
}

export async function triggerItemAsr(itemId: string, skipTranscript: boolean = false): Promise<ItemAsrResponse> {
  const qs = skipTranscript ? '?skip_transcript=1' : ''
  return apiFetch(`/api/items/${String(itemId)}/asr${qs}`, { method: 'POST' })
}

export function itemAsrStreamUrl(itemId: string): string {
  return `/api/items/${String(itemId)}/asr/stream`
}

export async function translateItemAsr(itemId: string): Promise<{
  asr_text_cn?: string | null
  asr_segments_cn?: (string | null)[] | null
}> {
  return apiFetch(`/api/items/${String(itemId)}/asr/translate`, { method: 'POST' })
}

export async function fetchStats(): Promise<StatsResponse> {
  return apiFetch('/api/stats')
}

export async function fetchClassification(): Promise<ClassificationConfig> {
  return apiFetch('/api/classification')
}

// ── Submit URL ──

export async function submitUrl(url: string): Promise<Record<string, unknown>> {
  return apiFetch('/api/submit-url', {
    method: 'POST',
    body: JSON.stringify({ url }),
  })
}

export async function fetchSubmitStatus(taskId: string): Promise<Record<string, unknown>> {
  return apiFetch('/api/submit-url/status', {
    method: 'POST',
    body: JSON.stringify({ task_id: taskId }),
  })
}

// ── Item Status (star, click, read, hide) ──

export async function setItemStatus(itemId: string, action: 'clicked' | 'starred' | 'hidden' | 'read'): Promise<void> {
  await apiFetch('/api/status', {
    method: 'POST',
    body: JSON.stringify({ item_id: String(itemId), action }),
  })
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

// ── Feed (pre-grouped) ──

export async function fetchFeedSections(params?: { search?: string }): Promise<{
  sections: Record<string, FeedItem[]>
  total: number
  cat_counts?: Record<string, number>
  read_model_version_id?: string | null
  section_next_cursors?: Record<string, InfoReadModelCursor | null>
  degraded?: boolean
  fallback_reason?: string | null
  degraded_reason?: string | null
}> {
  const qs = new URLSearchParams()
  if (params?.search) qs.set('search', params.search)
  const query = qs.toString()
  return apiFetch(`/api/feed/sections${query ? `?${query}` : ''}`)
}

export async function fetchFeedSectionMore(
  category: string,
  offset: number,
  limit = 50,
  keyword?: string,
  subcategory?: string,
  search?: string,
  cursor?: InfoReadModelCursor | null,
): Promise<{
  items: FeedItem[]
  category: string
  total?: number
  offset?: number
  limit?: number
  has_more?: boolean
  next_offset?: number | null
  next_cursor?: InfoReadModelCursor | null
  read_model_version_id?: string | null
  scope_key?: string | null
  degraded?: boolean
  fallback_reason?: string | null
  degraded_reason?: string | null
}> {
  const qs = new URLSearchParams({ category, offset: String(offset), limit: String(limit) })
  if (keyword) qs.set('keyword', keyword)
  if (subcategory) qs.set('subcategory', subcategory)
  if (search) qs.set('search', search)
  if (cursor?.version_id && cursor.scope_key && cursor.rank_after != null) {
    qs.set('cursor', JSON.stringify(cursor))
  }
  return apiFetch(`/api/feed/sections/more?${qs}`)
}

export async function fetchFeedPlatforms(params?: { search?: string }): Promise<{
  sections: Record<string, FeedItem[]>
  platform_counts: Record<string, number>
  source_counts: Record<string, Record<string, number>>
  /** v16.0 W3.T7: 每 platform 的 L1 分布 {l1_id: count}, 仅在 GitHub/Reddit/RSS/HN/WayToAGI/Manual 等以 L1 维度 pill 的 section 使用 */
  category_counts?: Record<string, Record<string, number>>
  overview_generated_at?: string
  overview_max_fetched_at?: string | null
  sample_limit?: number | null
  read_model_version_id?: string | null
  platform_next_cursors?: Record<string, InfoReadModelCursor | null>
  degraded?: boolean
  fallback_reason?: string | null
  degraded_reason?: string | null
}> {
  const qs = new URLSearchParams()
  if (params?.search) qs.set('search', params.search)
  const query = qs.toString()
  return apiFetch(`/api/feed/platforms${query ? `?${query}` : ''}`)
}

export async function fetchFeedPlatformMore(platform: string, offset: number, limit = 50, source?: string, group?: string, category?: string, search?: string, excludeIds?: string[], cursor?: InfoReadModelCursor | null): Promise<{
  items: FeedItem[]
  platform: string
  category?: string | null
  total?: number
  offset?: number
  limit?: number
  has_more?: boolean
  next_offset?: number | null
  next_cursor?: InfoReadModelCursor | null
  read_model_version_id?: string | null
  scope_key?: string | null
  degraded?: boolean
  fallback_reason?: string | null
  degraded_reason?: string | null
}> {
  const qs = new URLSearchParams({ platform, offset: String(offset), limit: String(limit) })
  if (source) qs.set('source', source)
  if (group) qs.set('group', group)
  if (category) qs.set('category', category)
  if (search) qs.set('search', search)
  if (excludeIds?.length) qs.set('exclude_ids', excludeIds.slice(0, 200).join(','))
  if (cursor?.version_id && cursor.scope_key && cursor.rank_after != null) {
    qs.set('cursor', JSON.stringify(cursor))
  }
  return apiFetch(`/api/feed/platforms/more?${qs}`)
}

/** BF-0419-10/11: 拿公众号订阅分组列表 + 每组 item 数 + 未分组桶 */
export async function fetchLingowhaleGroups(): Promise<{
  groups: Array<{
    name: string
    group_id: string
    channels: Array<{ channel_id: string; name: string }>
    is_standalone?: boolean
    item_count: number  // BF-0419-11: DB 里该 group 实际 item 数
  }>
  channel_map: Record<string, string>
  ungrouped_count: number  // BF-0419-11: detail_json.group='未分组' 或 NULL 的 item 数
}> {
  return apiFetch('/api/lingowhale/groups')
}

// ── Fetch (trigger backend source fetching) ──

export interface FetchProgressStage {
  id?: string
  name: string
  status: string
  new_count?: number
  platform?: string
  percent?: number
  message?: string
}

export interface FetchProgress {
  mode?: string
  stages: FetchProgressStage[]
  current_stage: number
  total_new: number
  platform?: string
  percent?: number
  result_status?: 'running' | 'success' | 'partial' | 'failed' | string
  message?: string
}

export interface FetchStatusResponse {
  running: boolean
  finished_at: string | null
  progress?: FetchProgress
}

export async function triggerFetchAll(): Promise<{ ok: boolean; msg: string }> {
  return apiFetch('/api/fetch', { method: 'POST' })
}

export async function fetchFetchStatus(): Promise<FetchStatusResponse> {
  return apiFetch('/api/fetch/status')
}

// ── Health ──

export async function fetchHealth(): Promise<HealthStatus> {
  return apiFetch('/api/health')
}

// ── Feedback ──

export async function submitFeedback(itemId: string, type: 'positive' | 'irrelevant' | 'low_quality', text?: string): Promise<void> {
  await apiFetch('/api/feedback', {
    method: 'POST',
    body: JSON.stringify({ item_id: String(itemId), type, text }),
  })
}

// ── Config ──

export async function fetchConfig(): Promise<Record<string, unknown>> {
  return apiFetch('/api/config')
}

// ── v15.0 事件聚合 ──

/** GET /api/feed/events — 时间线（由后端 pipeline 决定 visible）。
 *  optional auth；page=1 起始；since_version_snapshot 可选用于增量比对。 */
export async function fetchEvents(params?: {
  page?: FeedEventsCursor
  limit?: number
  sinceVersionSnapshot?: number | null
  fetchedSince?: string | null
  timezoneOffsetMinutes?: number
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
}): Promise<import('./types').ClusterBundleResponse> {
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

/** POST /api/clusters/:id/star — 登录态切换 cluster 收藏。 */
export async function setClusterStar(id: number): Promise<{ ok: boolean; starred_at: string | null }> {
  return apiFetch(`/api/clusters/${id}/star`, { method: 'POST' })
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

/** GET /api/search?q=&context= — 上下文感知搜索（recommend 双区，其他只 docs）。
 *  v17.0: 加 categories 参数（精选 tab pill 筛选叠加搜索） */
type ContextSearchOptions = {
  categories?: string[]
  eventsOnly?: boolean
}

export async function contextSearch(
  q: string,
  context: SearchContext = 'recommend',
  limit = 30,
  options?: ContextSearchOptions,
): Promise<SearchRecommendResponse | SearchDocsResponse> {
  const qs = new URLSearchParams({ q, context, limit: String(limit) })
  if (options?.categories && options.categories.length > 0) {
    qs.set('categories', options.categories.join(','))
  }
  if (options?.eventsOnly) qs.set('events_only', '1')
  return apiFetch(`/api/search?${qs}`)
}

export async function searchRecommend(q: string, limit = 30, options?: ContextSearchOptions): Promise<SearchRecommendResponse> {
  return contextSearch(q, 'recommend', limit, options) as Promise<SearchRecommendResponse>
}
