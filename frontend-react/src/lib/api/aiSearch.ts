import type { FeedItem } from '../types'
import { apiFetch } from './_client'

export const QUERY_MAX_CHARS = 200
export const POLL_INTERVAL_MS = 10_000
export const RETRIEVAL_POLL_INTERVAL_MS = 2_000
export const HIGHLIGHT_MS = 1_500
export const QUERY_VALIDATION_MESSAGE = `请输入 1 到 ${QUERY_MAX_CHARS} 字`
export const QUOTA_EXHAUSTED_MESSAGE = '当前额度已用完，明天 0 点恢复'
export const RETRIEVAL_IN_PROGRESS_MESSAGE = '上一个问题还在找资料，请稍等'

export interface AiSearchStatusResponse {
  enabled: boolean
  logged_in: boolean
  can_search: boolean
  reason: 'disabled' | 'login_required' | 'quota_exhausted' | null
  user_day_limit: number | null
}

export function describeSummaryError(code?: string | null): string | null {
  switch (code) {
    case 'insufficient': return '站内没有找到足够相关的内容，这次不计次数。换个更具体的说法试试。'
    case 'retrieval_failed':
    case 'retrieval_timeout': return '这次没能找完资料，可以再试一次，不计次数。'
    case 'hard_timeout':
    case 'timeout': return '这次总结用时太长，没能完成，可以再试一次。'
    case 'quota':
    case 'unavailable': return 'AI 总结服务暂时不可用，请稍后再来。'
    case 'quota_exhausted': return `${QUOTA_EXHAUSTED_MESSAGE}。`
    case 'candidates_unavailable': return '有几条原文刚刚下线了，重新搜索一次即可。'
    case 'job_not_found': return '这次的结果已失效，可以重新搜索。'
    default: return code ? '这次总结没有生成成功，可以再试一次。' : null
  }
}

export type AiSearchJobStatus = 'retrieving' | 'insufficient' | 'queued' | 'running' | 'done' | 'failed' | 'unavailable'
export type AiSearchRetrievalStage = 'understanding' | 'searching'
export interface AiSearchItem extends FeedItem { sim?: number | null }
export interface AiSearchPostResponse {
  job_id: string | null
  query: string
  /** Legacy POST includes retrieval results; v2 returns only job_id, query and summary. */
  query_lexical?: string
  window_days?: number
  lexical_hits?: number
  vector_hits?: number
  vector_ok?: boolean
  items?: AiSearchItem[]
  summary: { status: AiSearchJobStatus | 'none'; position?: number; reason?: string }
}
export interface AiSearchJobResponse {
  job_id: string
  query?: string
  status: AiSearchJobStatus
  stage?: AiSearchRetrievalStage
  keywords?: string[]
  vector_ok?: boolean
  position?: number
  content_md?: string | null
  cited_ids?: string[]
  items: AiSearchItem[]
  usage?: Record<string, unknown> | null
  elapsed_ms?: number | null
  error?: string | null
  model?: string | null
  cited_count?: number | null
}

export interface AiSearchHistoryItem {
  job_id: string
  query: string
  created_at: string
  status: AiSearchJobStatus
}

export type AiSearchHistoryResponse =
  | { anonymous: true; items: AiSearchHistoryItem[] }
  | { anonymous: false; items: AiSearchHistoryItem[]; total: number }

export class AiSearchAccessError extends Error {
  readonly status: 401 | 409 | 429
  constructor(public readonly reason: 'login_required' | 'retrieval_in_progress' | 'quota_exhausted') {
    super(reason)
    this.name = 'AiSearchAccessError'
    this.status = reason === 'login_required' ? 401 : reason === 'retrieval_in_progress' ? 409 : 429
  }
}

async function aiSearchFetch<T>(url: string, options?: RequestInit): Promise<T> {
  try {
    return await apiFetch<T>(url, options)
  } catch (error) {
    const detail = error as { status?: number; message?: string }
    if (detail?.status === 401 && detail.message === 'login_required') {
      throw new AiSearchAccessError('login_required')
    }
    if (detail?.status === 429 && detail.message === 'quota_exhausted') {
      throw new AiSearchAccessError('quota_exhausted')
    }
    if (detail?.status === 409 && detail.message === 'retrieval_in_progress') {
      throw new AiSearchAccessError('retrieval_in_progress')
    }
    throw error
  }
}

export function getAiSearchStatus(): Promise<AiSearchStatusResponse> {
  return aiSearchFetch('/api/ai-search/status')
}

export function postAiSearch(q: string): Promise<AiSearchPostResponse> {
  return aiSearchFetch('/api/ai-search', { method: 'POST', body: JSON.stringify({ q }) })
}
export function getAiSearchJob(jobId: string): Promise<AiSearchJobResponse> {
  return aiSearchFetch(`/api/ai-search/${encodeURIComponent(jobId)}`)
}
export function getAiSearchHistory(limit: number, signal?: AbortSignal): Promise<AiSearchHistoryResponse> {
  return aiSearchFetch(`/api/ai-search/history?limit=${encodeURIComponent(limit)}`, { signal })
}
