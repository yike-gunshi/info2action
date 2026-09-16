import type { DailyDigestsResponse, FeedItem, StatsResponse, HealthStatus, ClassificationConfig, InfoReadModelCursor } from '../types'
import { apiFetch } from './_client'

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

export async function fetchDailyDigests(start: string, end: string): Promise<DailyDigestsResponse> {
  const qs = new URLSearchParams({ start, end })
  return apiFetch(`/api/feed/daily-digest?${qs}`)
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

export async function submitFeedback(
  itemId: string,
  type: 'positive' | 'irrelevant' | 'low_quality' | 'should_feature' | 'should_drop',
  text?: string,
): Promise<{ ok: boolean; active?: boolean }> {
  return apiFetch('/api/feedback', {
    method: 'POST',
    body: JSON.stringify({ item_id: String(itemId), type, text }),
  })
}

// ── Config ──

export async function fetchConfig(): Promise<Record<string, unknown>> {
  return apiFetch('/api/config')
}
