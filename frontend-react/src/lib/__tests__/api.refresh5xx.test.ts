/**
 * BF-0708-1: refresh 返回 5xx 时,前端不得把用户当成"登录失效"。
 *
 * 背景:Supabase transaction pooler 回收空闲连接 → 后端 /api/auth/refresh 抛
 * EDBHANDLEREXITED → 500(修复后为 503)。旧代码 `tryRefresh` 用 `.then(r => r.ok)`
 * 判定,把 5xx 与 401 一视同仁 → setUser(null) + 跳 #login → 用户"看不到数据"。
 *
 * 语义边界(本文件锁死):
 * - 401 → 登录真失效 → 清 authStore + 跳 login(BF-0420-19 既有行为,必须保留)
 * - 5xx → 服务端故障 → 保持登录态,不跳转,向调用方抛错
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { getUserSettings } from '../api'
import { useAuthStore } from '../../store/authStore'

const LOGGED_IN = { id: 'u-1', username: 'tester', email: 't@e.com', role: 'user' } as never

beforeEach(() => {
  window.location.hash = ''
  useAuthStore.setState({ user: null, isLoading: false, isChecked: true })
  vi.restoreAllMocks()
})

/** 按 URL 分派 mock 响应,避免依赖调用顺序 */
function mockFetchByUrl(handler: (url: string) => { status: number; body?: unknown }) {
  const calls: string[] = []
  const mock = vi.fn(async (url: RequestInfo) => {
    const u = String(url)
    calls.push(u)
    const { status, body } = handler(u)
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => body ?? {},
    } as Response
  })
  vi.stubGlobal('fetch', mock)
  return { mock, calls }
}

describe('BF-0708-1 tryRefresh 对 5xx 的语义', () => {
  it('业务 401 + refresh 500 → 保持登录态,不跳 login,不清 authStore', async () => {
    useAuthStore.setState({ user: LOGGED_IN })
    mockFetchByUrl((url) =>
      url.includes('/api/auth/refresh') ? { status: 500 } : { status: 401 },
    )

    await expect(getUserSettings()).rejects.toThrow()

    expect(useAuthStore.getState().user).not.toBeNull()
    expect(window.location.hash).not.toContain('login')
  })

  it('业务 401 + refresh 503 → 同样保持登录态(修复后后端返回 503)', async () => {
    useAuthStore.setState({ user: LOGGED_IN })
    mockFetchByUrl((url) =>
      url.includes('/api/auth/refresh') ? { status: 503 } : { status: 401 },
    )

    await expect(getUserSettings()).rejects.toThrow()

    expect(useAuthStore.getState().user).not.toBeNull()
    expect(window.location.hash).not.toContain('login')
  })

  it('DB 故障时抛出的错误应说明服务不可用,而非"请先登录"', async () => {
    useAuthStore.setState({ user: LOGGED_IN })
    mockFetchByUrl((url) =>
      url.includes('/api/auth/refresh') ? { status: 503 } : { status: 401 },
    )

    const err = await getUserSettings().catch((e: Error) => e)
    expect(err).toBeInstanceOf(Error)
    expect((err as Error).message).not.toContain('请先登录')
  })

  // ── 回归护栏:不得因为修 5xx 而破坏真实的登录失效路径(BF-0420-19) ──

  it('回归:业务 401 + refresh 401 → 真失效,仍清 authStore 且跳 login', async () => {
    useAuthStore.setState({ user: LOGGED_IN })
    mockFetchByUrl((url) =>
      url.includes('/api/auth/refresh') ? { status: 401 } : { status: 401 },
    )

    await expect(getUserSettings()).rejects.toThrow()

    expect(useAuthStore.getState().user).toBeNull()
    expect(window.location.hash).toBe('#login')
  })

  it('回归:业务 401 + refresh 200 → 刷新成功,重试原请求并返回数据', async () => {
    useAuthStore.setState({ user: LOGGED_IN })
    let settingsCalls = 0
    mockFetchByUrl((url) => {
      if (url.includes('/api/auth/refresh')) return { status: 200 }
      settingsCalls += 1
      return settingsCalls === 1
        ? { status: 401 }
        : { status: 200, body: { discord_bot_token: null, has_discord_token: false } }
    })

    const res = await getUserSettings()
    expect(res.has_discord_token).toBe(false)
    expect(useAuthStore.getState().user).not.toBeNull()
    expect(window.location.hash).not.toContain('login')
  })

  it('回归:匿名用户 401 → 不触发 refresh,不跳转', async () => {
    useAuthStore.setState({ user: null })
    const { calls } = mockFetchByUrl(() => ({ status: 401 }))

    await expect(getUserSettings()).rejects.toThrow()

    expect(calls.some((u) => u.includes('/api/auth/refresh'))).toBe(false)
    expect(window.location.hash).not.toContain('login')
  })
})
