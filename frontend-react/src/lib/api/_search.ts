import { apiFetch } from './_client'

export interface SearchRequest {
  signal: AbortSignal
  deadline: number
}

let activeSearchSignal: AbortSignal | undefined
export function setActiveSearchSignal(signal?: AbortSignal) { activeSearchSignal = signal }

/** Category and source requests share one search action's remaining time. */
export async function searchFetch<T>(url: string, options?: RequestInit, request?: SearchRequest): Promise<T> {
  const remaining = Math.min(120_000, (request?.deadline ?? Date.now() + 120_000) - Date.now())
  if (remaining <= 0) throw new Error('搜索超时，请重试')
  const signal = request?.signal ?? activeSearchSignal
  const controller = new AbortController()
  const cancel = () => controller.abort()
  signal?.addEventListener('abort', cancel, { once: true })
  if (signal?.aborted) controller.abort()
  const timer = setTimeout(cancel, remaining)
  try {
    return await apiFetch<T>(url, {
      ...options,
      signal: controller.signal,
      headers: { ...options?.headers, 'X-Search-Budget-Ms': String(Math.ceil(remaining)) },
    })
  } catch (error) {
    if (controller.signal.aborted) throw new Error('搜索超时或已取消，请重试')
    throw error
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', cancel)
  }
}
