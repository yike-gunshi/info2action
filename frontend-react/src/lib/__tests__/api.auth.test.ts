/**
 * BF-0420-15 + BF-0420-19:401 共根治。
 *
 * 覆盖:
 * - generateActionFromItem 遇 401 的三条路径(anon / refresh-retry / refresh-fail)
 * - apiFetch 的 401 处理在 refresh 失败时清 authStore.user(BF-0420-19)
 * - 不泛化把"需要登录"当"生成失败"(BF-0420-15)
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { generateActionFromItem } from '../api'
import { useAuthStore } from '../../store/authStore'

// jsdom 里 window.location.hash 可直接赋值,但 hash=login 会引发导航副作用警告,
// 用 Object.defineProperty 静默 location-hash 写入
const originalHash = window.location.hash
beforeEach(() => {
  window.location.hash = originalHash
  useAuthStore.setState({ user: null, isLoading: false, isChecked: true })
  vi.restoreAllMocks()
})

function mockFetchSequence(responses: Array<Partial<Response> & { status: number }>) {
  const calls: RequestInfo[] = []
  const sequence = [...responses]
  const mock = vi.fn(async (url: RequestInfo) => {
    calls.push(url)
    const next = sequence.shift()
    if (!next) throw new Error('fetch mock exhausted')
    return {
      ok: next.status >= 200 && next.status < 300,
      body: next.body ?? null,
      json: async () => ({}),
      ...next,
    } as Response
  })
  vi.stubGlobal('fetch', mock)
  return { mock, calls }
}

describe('generateActionFromItem 401 处理', () => {
  it('匿名用户收到 401 → onError 带"请先登录"提示,不跳转 /login,不清 store', async () => {
    useAuthStore.setState({ user: null })
    mockFetchSequence([{ status: 401 }])

    const hashBefore = window.location.hash
    const onError = vi.fn()
    generateActionFromItem('item-1', {}, undefined, undefined, onError)
    await new Promise((r) => setTimeout(r, 20))

    expect(onError).toHaveBeenCalledOnce()
    const err = onError.mock.calls[0][0] as Error & { status?: number }
    expect(err.status).toBe(401)
    expect(err.message).toContain('请先登录')
    expect(window.location.hash).toBe(hashBefore)
  })

  it('登录态 401 → refresh 成功 → 重试(第二次请求带 SSE body)', async () => {
    useAuthStore.setState({
      user: { id: 'u1', username: 'alice', email: 'a@x.com', role: 'user' },
    })
    const sseBody = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('event: stage\ndata: {"index":0,"status":"done"}\n\n'))
        controller.close()
      },
    })
    const { mock } = mockFetchSequence([
      { status: 401 },              // 初次 generate → 401
      { status: 200, ok: true },    // /api/auth/refresh
      { status: 200, ok: true, body: sseBody }, // 重试 generate → 200 + SSE
    ])

    const events: unknown[] = []
    const onError = vi.fn()
    const onDone = vi.fn()
    generateActionFromItem('item-1', {}, (e) => events.push(e), onDone, onError)
    await new Promise((r) => setTimeout(r, 30))

    expect(onError).not.toHaveBeenCalled()
    expect(onDone).toHaveBeenCalledOnce()
    expect(events.length).toBeGreaterThan(0)
    // 验证三次 fetch 顺序: generate → refresh → generate-retry
    const urls = mock.mock.calls.map((c) => String(c[0]))
    expect(urls[0]).toContain('/api/actions/generate-from-item')
    expect(urls[1]).toContain('/api/auth/refresh')
    expect(urls[2]).toContain('/api/actions/generate-from-item')
  })

  it('登录态 401 → refresh 失败 → 清 authStore.user + 跳登录 + onError Session expired (BF-0420-19)', async () => {
    useAuthStore.setState({
      user: { id: 'u1', username: 'alice', email: 'a@x.com', role: 'user' },
    })
    mockFetchSequence([
      { status: 401 },                    // 初次 generate
      { status: 401, ok: false },         // refresh 失败
    ])

    const onError = vi.fn()
    generateActionFromItem('item-1', {}, undefined, undefined, onError)
    await new Promise((r) => setTimeout(r, 30))

    expect(onError).toHaveBeenCalledOnce()
    const err = onError.mock.calls[0][0] as Error & { status?: number }
    expect(err.message).toBe('Session expired')
    expect(err.status).toBe(401)
    // BF-0420-19 核心断言:authStore.user 必须被清空,不能残留"已登录"状态
    expect(useAuthStore.getState().user).toBeNull()
    expect(window.location.hash).toBe('#login')
  })
})

// ── D 深层断言:幂等 / 非 401 / anon 不反向污染 ──

describe('generateActionFromItem 401 深层断言', () => {
  it('D1: 非 401 错误不触发 handleUnauthorized,不改 authStore', async () => {
    useAuthStore.setState({
      user: { id: 'u1', username: 'alice', email: 'a@x.com', role: 'user' },
    })
    mockFetchSequence([{ status: 500 }])

    const hashBefore = window.location.hash
    const onError = vi.fn()
    generateActionFromItem('item-1', {}, undefined, undefined, onError)
    await new Promise((r) => setTimeout(r, 20))

    expect(onError).toHaveBeenCalledOnce()
    const err = onError.mock.calls[0][0] as Error & { status?: number }
    expect(err.status).toBe(500)
    // authStore 不应被清
    expect(useAuthStore.getState().user).not.toBeNull()
    expect(window.location.hash).toBe(hashBefore)
  })

  it('D2: 匿名 401 场景不调用 refresh,不改 authStore', async () => {
    useAuthStore.setState({ user: null })
    const { mock } = mockFetchSequence([{ status: 401 }])

    const onError = vi.fn()
    generateActionFromItem('item-1', {}, undefined, undefined, onError)
    await new Promise((r) => setTimeout(r, 20))

    const urls = mock.mock.calls.map((c) => String(c[0]))
    // 匿名场景绝不调 /api/auth/refresh(否则浪费请求 + 噪音日志)
    expect(urls.filter((u) => u.includes('/api/auth/refresh')).length).toBe(0)
    expect(onError).toHaveBeenCalledOnce()
    // authStore.user 原本就是 null,不应被额外 set(幂等)
    expect(useAuthStore.getState().user).toBeNull()
  })

  it('D3: 响应 body 为空 + 401 也能优雅处理(不解析 body)', async () => {
    useAuthStore.setState({ user: null })
    // body=null 模拟 middleware 返 HTML 时 json() 会抛错的场景
    mockFetchSequence([{ status: 401, body: null }])

    const onError = vi.fn()
    generateActionFromItem('item-1', {}, undefined, undefined, onError)
    await new Promise((r) => setTimeout(r, 20))

    expect(onError).toHaveBeenCalledOnce()
    // 不应因 json parse 失败导致 unhandled rejection
    const err = onError.mock.calls[0][0] as Error
    expect(err.message).toContain('请先登录')
  })

  it('D4: AbortController.abort() 后 onError 不触发(保持原有 AbortError 过滤)', async () => {
    useAuthStore.setState({ user: null })

    // fetch 模拟成 Promise,立即被 abort 取消
    const abortError = new Error('aborted')
    abortError.name = 'AbortError'
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(abortError)))

    const onError = vi.fn()
    const ctrl = generateActionFromItem('item-1', {}, undefined, undefined, onError)
    ctrl.abort()
    await new Promise((r) => setTimeout(r, 20))

    expect(onError).not.toHaveBeenCalled()
  })
})
