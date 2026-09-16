import { apiFetch } from './_client'

export type AdminSourceStatus = 'active' | 'paused' | 'pending' | 'broken' | 'not_fetched' | 'deleted' | string

export type AdminSourceAttemptOutcome = 'success' | 'no_new' | 'failed' | 'missed' | 'interrupted' | 'retrying'

export interface AdminSourceAttempt {
  run_id: number
  source_id?: number
  handle?: string
  outcome: AdminSourceAttemptOutcome
  attempts?: number
  duration_ms?: number
  new_count?: number
  error_code?: string | null
  error?: string | null
  finished_at?: string | null
}

export interface AdminSourceHealth {
  last_fetched_at: string | null
  inserted_7d: number | null
  consecutive_failures: number | null
  latest_attempt?: AdminSourceAttempt | null
}

export interface AdminSource {
  id: number
  platform: string
  source_key: string
  display_name: string
  status: AdminSourceStatus
  config_json: unknown
  origin: string | null
  validated_at: string | null
  created_at: string
  updated_at: string
  health?: AdminSourceHealth
}

export interface AdminSourceGroup {
  platform: string
  sources: AdminSource[]
}

export interface AdminSourcesResponse {
  groups: AdminSourceGroup[]
  total: number
  latest_x_run?: AdminXRunSummary | null
  x_list?: AdminXListStatus | null
}

export interface AdminXRunSummary {
  run_id: number
  started_at: string | null
  finished_at: string | null
  planned: number
  attempted: number
  succeeded: number
  no_new: number
  failed: number
  missed: number
  mode?: string
  list_id?: string | null
  unmatched_posts?: number
}

export interface AdminXListStatus {
  configured: boolean
  mode: 'list'
  list_id: string | null
  list_url: string | null
  registry_count: number
  synced_count: number
  pending_count: number
  synced_handles: string[]
  pending_handles: string[]
  last_synced_at: string | null
  last_error: string | null
  failed?: Array<{ handle: string; error: string }>
  unassigned_handles?: string[]
  lists?: AdminXListGroupStatus[]
}

export interface AdminXListGroupStatus {
  key: string
  name: string
  list_id: string
  list_url: string
  registry_count: number
  synced_count: number
  pending_count: number
  synced_handles: string[]
  pending_handles: string[]
  last_synced_at: string | null
  last_error: string | null
}

export interface AdminSourcePreviewItem {
  title?: string | null
  url?: string | null
  published_at?: string | null
  summary?: string | null
}

export interface AdminSourceValidateResponse {
  status: 'ok' | 'empty' | 'deferred'
  platform: string
  source_key: string
  preview: AdminSourcePreviewItem[]
  display_name?: string | null
  reason?: string | null
  warning?: string | null
}

export interface AdminSourceCreatePayload {
  platform: string
  source_key: string
  display_name?: string | null
  status?: 'active' | 'paused' | 'pending' | 'broken' | 'not_fetched'
  config_json?: unknown
  validated_at?: string | null
}

export type AdminSourceUpdatePayload = {
  status?: 'active' | 'paused'
  config_json?: unknown
}

export interface AdminSourceReconcileItem {
  platform: string
  source_key: string
  display_name: string
  id?: number
}

export interface AdminSourceReconcileResponse {
  missing: AdminSourceReconcileItem[]
  imported: AdminSourceReconcileItem[]
  note?: string | null
}

export type AdminSourceAlgoParams = {
  hackernews_count: number | null
  github_trending_count: number | null
  bilibili_hot_count: number | null
  bilibili_rank_count: number | null
  bilibili_videos_per_up: number | null
}

export async function getAdminSources(): Promise<AdminSourcesResponse> {
  return apiFetch('/api/admin/sources')
}

export async function syncAdminXList(full = false): Promise<AdminXListStatus> {
  return apiFetch('/api/admin/sources/x-list/sync', {
    method: 'POST',
    body: JSON.stringify({ full }),
  })
}

export interface AdminWechatSearchChannel {
  channel_id: string
  name: string
  description: string | null
  avatar_url: string | null
  has_subscribed: boolean
  last_7d_count: number
  subscriber_count: number
  is_official: boolean
  already_in_registry: boolean
}

export async function searchWechatSources(q: string, limit = 20): Promise<{ channels: AdminWechatSearchChannel[] }> {
  const qs = new URLSearchParams({ q, limit: String(limit) })
  return apiFetch(`/api/admin/sources/search-wechat?${qs}`)
}

export interface AdminSyncResult {
  imported: number
  existing: number
  total: number
  note?: string | null
}

export async function syncLingowhaleSources(): Promise<AdminSyncResult> {
  return apiFetch('/api/admin/sources/sync-lingowhale', { method: 'POST', body: '{}' })
}

export async function validateAdminSource(data: {
  platform: string
  source_key: string
}): Promise<AdminSourceValidateResponse> {
  return apiFetch('/api/admin/sources/validate', {
    method: 'POST',
    body: JSON.stringify(data),
  })
}

export async function createAdminSource(data: AdminSourceCreatePayload): Promise<{ ok: boolean; source: AdminSource }> {
  return apiFetch('/api/admin/sources', {
    method: 'POST',
    body: JSON.stringify(data),
  })
}

export async function updateAdminSource(
  id: number,
  data: AdminSourceUpdatePayload,
): Promise<{ ok: boolean; source: AdminSource }> {
  return apiFetch(`/api/admin/sources/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  })
}

export async function deleteAdminSource(id: number): Promise<{ ok: boolean; source: AdminSource }> {
  return apiFetch(`/api/admin/sources/${id}`, { method: 'DELETE' })
}

export async function reconcileLingowhaleSources(data?: {
  import_keys?: string[]
}): Promise<AdminSourceReconcileResponse> {
  return apiFetch('/api/admin/sources/lingowhale/reconcile', {
    method: 'POST',
    body: JSON.stringify(data ?? {}),
  })
}

export async function getAdminSourceAlgoParams(): Promise<{ params: AdminSourceAlgoParams }> {
  return apiFetch('/api/admin/sources/algo-params')
}

export async function updateAdminSourceAlgoParams(params: AdminSourceAlgoParams): Promise<{
  ok: boolean
  params: AdminSourceAlgoParams
}> {
  return apiFetch('/api/admin/sources/algo-params', {
    method: 'PATCH',
    body: JSON.stringify({ params }),
  })
}
