import type {
  ClusterFeedbackKind,
  AdminHighlightItemFeedbackKind,
  AdminHighlightOverrideAction,
  AdminHighlightManualDisplay,
} from '../types'
import { apiFetch } from './_client'

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


// ── Admin 总览台 (v23.0 admin-console) ──
// 契约真相源: .features/admin-console-v23/api-contract.md

export type AdminHealthLevel = 'ok' | 'warn' | 'crit' | 'unknown'

export type AdminHealthSignal = {
  key: string
  level: AdminHealthLevel
  label: string
  detail: string
  link: 'runs' | null
}

export type AdminHealthIncident = {
  severity: 'warn' | 'crit'
  text: string
  link: 'runs' | null
}

export type AdminTrendPoint = { date: string; value: number | null }

export type AdminConsoleMetrics = {
  total_users: number | null
  new_users_today: number | null
  new_users_7d: number | null
  active_users_1d: number | null
  active_users_7d: number | null
  info_click_users_7d: number | null
  info_click_items_7d: number | null
  info_click_items_total: number | null
  highlight_click_users_7d: number | null
  highlight_click_events_7d: number | null
  highlight_click_events_total: number | null
}

export type AdminInteractionsDetail = {
  starred_users: number | null
  starred_total: number | null
  read_users_7d: number | null
  read_items_7d: number | null
  latest_signup: { username: string; created_at: string } | null
}

// 站点流量 (Cloudflare 边缘, 含未注册访客)。外部 API, 可能缺配/拉取失败, 故 traffic 可选。
export type AdminTraffic =
  | { available: false; reason: 'not_configured' | 'cf_error'; error?: string }
  | {
      available: true
      source: 'cloudflare'
      generated_at: string
      uv_avg_7d: number | null
      uv_avg_30d: number | null
      pv_7d: number | null
      pv_30d: number | null
      uv_trend_30d: AdminTrendPoint[]
      pv_trend_30d: AdminTrendPoint[]
    }

export type AdminConsoleSummary =
  | { available: false; reason: 'remote_required' | 'remote_error'; error?: string }
  | {
      available: true
      generated_at: string
      c_metrics: AdminConsoleMetrics
      interactions_detail: AdminInteractionsDetail
      cost: { embedding_cost_yuan_24h: number | null; embedding_calls_24h: number | null }
      health: { signals: AdminHealthSignal[]; incidents: AdminHealthIncident[] }
      trends: { new_users_14d: AdminTrendPoint[]; fetch_success_rate_7d: AdminTrendPoint[] }
      traffic?: AdminTraffic
    }

export async function getAdminConsoleSummary(): Promise<AdminConsoleSummary> {
  return apiFetch('/api/admin/console/summary')
}

export type AdminHighlightsDays = 1 | 3 | 7
export type AdminHighlightsStationKey = 'ingested' | 'scored' | 'clustered' | 'summarized' | 'displayed'
export type AdminHighlightsDiffKey = 'scoring' | 'summary' | 'display'
export type AdminHighlightsFunnelView = 'panorama' | 'anomaly'
export type AdminHighlightsDisplay = 'all' | 'shown' | 'hidden'
export type AdminHighlightsStage =
  | 'pending'
  | 'displayed'
  | 'blocked_scoring'
  | 'blocked_summary'
  | 'blocked_display'
export type AdminHighlightsManualDisplay = AdminHighlightManualDisplay
export type AdminHighlightsItemFeedbackKind = AdminHighlightItemFeedbackKind
export type AdminHighlightsFeedbackKind = ClusterFeedbackKind | 'should_drop'

export type AdminHighlightsDims = {
  authority: number | null
  substance: number | null
  novelty: number | null
  timeliness: number | null
  audience_fit: number | null
}

export type AdminHighlightsRowFeedback = {
  kind: AdminHighlightsFeedbackKind | null
  note: string | null
}

export type AdminHighlightsFunnelResponse = {
  stations: Array<{ key: AdminHighlightsStationKey; count: number }>
  diffs: Array<{ key: AdminHighlightsDiffKey; count: number }>
  anomalies_count: number
  gate_disabled: boolean
}

export type AdminHighlightsItemRow = {
  id: string
  ingested_at: string | null
  title: string | null
  url: string | null
  cluster_id: number | null
  cluster_title: string | null
  score10: number | null
  reach: number | null
  dims: AdminHighlightsDims
  veto: string | null
  uncertainty: string | null
  reason: string | null
  feedback: AdminHighlightsRowFeedback
  stuck_at?: string | null
  error_summary?: string | null
}

export type AdminHighlightsClusterMember = {
  id: string
  title: string | null
  url: string | null
  platform: string | null
  source: string | null
  author_name: string | null
  fetched_at: string | null
  verdict: string | null
  score10: number | null
  reach: number | null
  dims: AdminHighlightsDims
  veto: string | null
  uncertainty: string | null
  reason: string | null
  feedback: AdminHighlightsRowFeedback
}

export type AdminHighlightsClusterRow = {
  id: number
  latest_at: string | null
  title: string | null
  dominant_category: string
  max_flag_score10: number | null
  score_inputs: {
    max_q?: number | null
    avg_q?: number | null
    scored_include_count?: number | null
    unique_source_count?: number | null
    [key: string]: unknown
  }
  deciding_item: {
    id: string
    title: string | null
    dims: AdminHighlightsDims
    reason: string | null
  }
  stage: AdminHighlightsStage
  blocked_reason: string | null
  displayed: boolean
  manual_display: AdminHighlightsManualDisplay
  feedback: AdminHighlightsRowFeedback
  members: AdminHighlightsClusterMember[]
}

export type AdminHighlightsFunnelRowsResponse =
  | {
      granularity: 'item'
      items: AdminHighlightsItemRow[]
      total: number
      page: number
      gate_disabled: boolean
      display_threshold: number | null
    }
  | {
      granularity: 'cluster'
      items: AdminHighlightsClusterRow[]
      total: number
      page: number
      gate_disabled: boolean
      display_threshold: number | null
    }

export async function getAdminHighlightsFunnel(params: {
  days: AdminHighlightsDays
  q?: string
  tag?: string
}): Promise<AdminHighlightsFunnelResponse> {
  const qs = new URLSearchParams({ days: String(params.days) })
  if (params.q) qs.set('q', params.q)
  if (params.tag) qs.set('tag', params.tag)
  return apiFetch(`/api/admin/highlights/funnel?${qs}`)
}

export async function getAdminHighlightsFunnelRows(params: {
  view: 'panorama' | 'anomaly'
  days: AdminHighlightsDays
  q?: string
  tag?: string
  display?: AdminHighlightsDisplay
  stage?: AdminHighlightsStage | ''
  page?: number
  limit?: number
}): Promise<AdminHighlightsFunnelRowsResponse> {
  const qs = new URLSearchParams({
    view: params.view,
    days: String(params.days),
  })
  if (params.q) qs.set('q', params.q)
  if (params.tag) qs.set('tag', params.tag)
  if (params.display && params.display !== 'all') qs.set('display', params.display)
  if (params.stage) qs.set('stage', params.stage)
  qs.set('page', String(params.page ?? 1))
  qs.set('limit', String(params.limit ?? 50))
  return apiFetch(`/api/admin/highlights/funnel/rows?${qs}`)
}

export async function setAdminHighlightOverride(
  id: number,
  action: AdminHighlightOverrideAction,
  note?: string,
): Promise<{
  ok: boolean
  manual_display: AdminHighlightsManualDisplay
  manual_display_at: string | null
  feedback_kind: ClusterFeedbackKind | null
  feedback_note: string | null
}> {
  return apiFetch(`/api/admin/highlights/clusters/${id}/override`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, note }),
  })
}
