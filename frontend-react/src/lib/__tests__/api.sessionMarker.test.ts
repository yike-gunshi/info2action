/**
 * BF-0708-2:匿名态不再空转 /api/auth/refresh(hasSession 标记守卫)。
 *
 * 背景:refresh_token cookie 是 HttpOnly + path=/api/auth/refresh,JS 读不到,
 * authMe() 在 App 挂载时 authStore.user 必为 null,不能照搬 handleUnauthorized()
 * 的 currentUser 守卫(那会杀掉"刷新页面靠 cookie 恢复登录"的唯一通道)。
 * 守卫依据是登录时写、登出/会话确定失效时清的 localStorage 标记 auth_has_session。
 *
 * 覆盖:
 * - 匿名(无标记)authMe 遇 401 不发 refresh(本案核心)
 * - 标记在 + refresh ok → 恢复登录通道不回归(BF-0708-2 设计陷阱)
 * - 标记在 + refresh 401(expired)→ 清标记,后续加载自愈不再空转
 * - 标记在 + refresh 5xx(unavailable)→ 标记保留(BF-0708-1 语义不回归)
 * - 标记生命周期:authLogin / authVerifyEmail / authMe 成功写,authLogout /
 *   handleUnauthorized expired / apiFetch 二次 401 清
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { authMe, authLogin, authLogout, authVerifyEmail, getUserSettings } from '../api'
import { useAuthStore } from '../../store/authStore'

const HAS_SESSION_KEY = 'auth_has_session'
const originalHash = window.location.hash

beforeEach(() => {
  window.location.hash = originalHash
  localStorage.clear()
  useAuthStore.setState({ user: null, isLoading: false, isChecked: true })
  vi.restoreAllMocks()
})

function mockFetchSequence(responses: Array<Partial<Response> & { status: number }>) {
  const calls: string[] = []
  const sequence = [...responses]
  const mock = vi.fn(async (url: RequestInfo) => {
    calls.push(String(url))
    const next = sequence.shift()
    if (!next) throw new Error(`fetch mock exhausted at ${String(url)}`)
    return {
      ok: next.status >= 200 && next.status < 300,
      json: async () => ({}),
      ...next,
    } as Response
  })
  vi.stubGlobal('fetch', mock)
  return { mock, calls }
}

const fakeUser = { id: 'u1', username: 'alice', email: 'a@x.com', role: 'user' as const }

describe('authMe 的 hasSession 守卫(BF-0708-2 核心)', () => {
  it('匿名(无标记):/api/auth/me 401 → 不发 /api/auth/refresh,抛 Not authenticated', async () => {
    const { calls } = mockFetchSequence([{ status: 401 }])

    await expect(authMe()).rejects.toThrow('Not authenticated')

    expect(calls).toEqual(['/api/auth/me'])
    expect(calls.filter((u) => u.includes('/api/auth/refresh')).length).toBe(0)
  })

  it('标记在 + refresh ok → 重试 /api/auth/me 恢复登录(刷新页面通道不回归)', async () => {
    localStorage.setItem(HAS_SESSION_KEY, '1')
    const { calls } = mockFetchSequence([
      { status: 401 },                                    // me 初次
      { status: 200 },                                    // refresh ok
      { status: 200, json: async () => fakeUser },        // me 重试
    ])

    const user = await authMe()

    expect(user.username).toBe('alice')
    expect(calls).toEqual(['/api/auth/me', '/api/auth/refresh', '/api/auth/me'])
    expect(localStorage.getItem(HAS_SESSION_KEY)).toBe('1')
  })

  it('标记在 + refresh 401(expired)→ 抛错 + 清标记;第二次加载不再发 refresh(自愈)', async () => {
    localStorage.setItem(HAS_SESSION_KEY, '1')
    mockFetchSequence([
      { status: 401 },  // me
      { status: 401 },  // refresh → 会话真失效
    ])

    await expect(authMe()).rejects.toThrow('Not authenticated')
    expect(localStorage.getItem(HAS_SESSION_KEY)).toBeNull()

    // 第二次页面加载:标记已清,不再空转 refresh
    const { calls } = mockFetchSequence([{ status: 401 }])
    await expect(authMe()).rejects.toThrow('Not authenticated')
    expect(calls).toEqual(['/api/auth/me'])
  })

  it('标记在 + refresh 5xx(unavailable)→ 抛错但标记保留(BF-0708-1 语义:瞬断不抹"曾登录")', async () => {
    localStorage.setItem(HAS_SESSION_KEY, '1')
    mockFetchSequence([
      { status: 401 },  // me
      { status: 500 },  // refresh → 服务端故障
    ])

    await expect(authMe()).rejects.toThrow('Not authenticated')
    expect(localStorage.getItem(HAS_SESSION_KEY)).toBe('1')
  })

  it('authMe 直接 200 → 写标记(用户手动清 localStorage 后自愈)', async () => {
    mockFetchSequence([{ status: 200, json: async () => fakeUser }])

    const user = await authMe()

    expect(user.id).toBe('u1')
    expect(localStorage.getItem(HAS_SESSION_KEY)).toBe('1')
  })
})

describe('标记生命周期', () => {
  it('authLogin 成功 → 写标记', async () => {
    mockFetchSequence([{ status: 200, json: async () => ({ user: fakeUser }) }])

    const user = await authLogin('alice', 'pw')

    expect(user.username).toBe('alice')
    expect(localStorage.getItem(HAS_SESSION_KEY)).toBe('1')
  })

  it('authLogin 失败 → 不写标记', async () => {
    mockFetchSequence([{ status: 401, json: async () => ({ error: '密码错误' }) }])

    await expect(authLogin('alice', 'bad')).rejects.toThrow()
    expect(localStorage.getItem(HAS_SESSION_KEY)).toBeNull()
  })

  it('authVerifyEmail 成功(后端自动登录种 cookie)→ 写标记', async () => {
    mockFetchSequence([{ status: 200, json: async () => ({ ok: true, user: fakeUser }) }])

    const res = await authVerifyEmail('a@x.com', '123456')

    expect(res.ok).toBe(true)
    expect(localStorage.getItem(HAS_SESSION_KEY)).toBe('1')
  })

  it('authLogout 成功 → 清标记', async () => {
    localStorage.setItem(HAS_SESSION_KEY, '1')
    mockFetchSequence([{ status: 200 }])

    await authLogout()

    expect(localStorage.getItem(HAS_SESSION_KEY)).toBeNull()
  })

  it('authLogout 后端 500 → 仍清标记(用户意图即登出)', async () => {
    localStorage.setItem(HAS_SESSION_KEY, '1')
    mockFetchSequence([{ status: 500 }])

    await expect(authLogout()).rejects.toThrow()
    expect(localStorage.getItem(HAS_SESSION_KEY)).toBeNull()
  })

  it('登录态业务请求 401 → refresh 401 → handleUnauthorized expired 清标记 + 清 store + 跳登录', async () => {
    useAuthStore.setState({ user: fakeUser })
    localStorage.setItem(HAS_SESSION_KEY, '1')
    mockFetchSequence([
      { status: 401 },  // /api/user/settings
      { status: 401 },  // refresh → expired
    ])

    await expect(getUserSettings()).rejects.toThrow('Session expired')

    expect(localStorage.getItem(HAS_SESSION_KEY)).toBeNull()
    expect(useAuthStore.getState().user).toBeNull()
    expect(window.location.hash).toBe('#login')
  })

  it('登录态业务请求 401 → refresh ok → 重试仍 401 → 二次 401 路径也清标记', async () => {
    useAuthStore.setState({ user: fakeUser })
    localStorage.setItem(HAS_SESSION_KEY, '1')
    mockFetchSequence([
      { status: 401 },  // /api/user/settings
      { status: 200 },  // refresh ok
      { status: 401 },  // 重试仍 401
    ])

    await expect(getUserSettings()).rejects.toThrow('Session expired')

    expect(localStorage.getItem(HAS_SESSION_KEY)).toBeNull()
    expect(useAuthStore.getState().user).toBeNull()
  })

  it('登录态业务请求 401 → refresh 5xx(unavailable)→ 标记与 store 都保留(BF-0708-1)', async () => {
    useAuthStore.setState({ user: fakeUser })
    localStorage.setItem(HAS_SESSION_KEY, '1')
    mockFetchSequence([
      { status: 401 },  // /api/user/settings
      { status: 503 },  // refresh → 服务端故障
    ])

    await expect(getUserSettings()).rejects.toThrow('服务暂时不可用')

    expect(localStorage.getItem(HAS_SESSION_KEY)).toBe('1')
    expect(useAuthStore.getState().user).not.toBeNull()
  })
})
