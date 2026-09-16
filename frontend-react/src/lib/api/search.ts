import type { SearchRecommendResponse, SearchDocsResponse, SearchContext } from '../types'
import { apiFetch } from './_client'

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
