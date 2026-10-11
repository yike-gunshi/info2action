import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { searchFetch } from '../api/_search'
import { apiFetch } from '../api/_client'
vi.mock('../api/_client', () => ({ apiFetch: vi.fn() }))

beforeEach(() => { vi.useFakeTimers(); vi.mocked(apiFetch).mockReset() })
afterEach(() => { vi.useRealTimers() })

it('sends the remaining shared budget for the source stage', async () => {
  const controller = new AbortController()
  const request = { signal: controller.signal, deadline: Date.now() + 120_000 }
  vi.mocked(apiFetch).mockResolvedValue({})
  await searchFetch('/api/feed/sections?search=AI', undefined, request)
  await vi.advanceTimersByTimeAsync(80_000)
  await searchFetch('/api/feed/platforms?search=AI', undefined, request)
  expect(vi.mocked(apiFetch).mock.calls[1][1]?.headers).toMatchObject({ 'X-Search-Budget-Ms': '40000' })
})

it('does not issue another request when the search action has expired', async () => {
  await expect(searchFetch('/api/feed/platforms?search=AI', undefined, { signal: new AbortController().signal, deadline: Date.now() - 1 })).rejects.toThrow('搜索超时')
  expect(apiFetch).not.toHaveBeenCalled()
})

it('aborts the active network request on a keyword switch', async () => {
  const controller = new AbortController()
  vi.mocked(apiFetch).mockImplementation((_url, options) => new Promise((_resolve, reject) => options?.signal?.addEventListener('abort', () => reject(new Error('aborted')))))
  const pending = searchFetch('/api/feed/sections?search=AI', undefined, { signal: controller.signal, deadline: Date.now() + 120_000 })
  const expectation = expect(pending).rejects.toThrow('已取消')
  controller.abort()
  await expectation
  expect(vi.getTimerCount()).toBe(0)
})

it('aborts a request when its remaining time runs out', async () => {
  vi.mocked(apiFetch).mockImplementation((_url, options) => new Promise((_resolve, reject) => options?.signal?.addEventListener('abort', () => reject(new Error('aborted')))))
  const pending = searchFetch('/api/feed/sections?search=AI', undefined, { signal: new AbortController().signal, deadline: Date.now() + 50 })
  const expectation = expect(pending).rejects.toThrow('搜索超时')
  await vi.advanceTimersByTimeAsync(51)
  await expectation
})
