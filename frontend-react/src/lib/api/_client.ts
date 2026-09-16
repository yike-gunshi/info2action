export const BASE = '' // same origin, proxied by Vite in dev

/**
 * refresh 的三态结果(BF-0708-1)。
 *
 * 必须区分"登录真的失效"和"服务端暂时挂了" —— 旧代码用 `r.ok` 把两者
 * 混为一谈,DB 瞬断导致的 500 会把在线用户直接踢回登录页。
 */
export type RefreshOutcome = 'ok' | 'expired' | 'unavailable'

/**
 * BF-0708-2:"曾登录"非敏感标记。
 *
 * refresh_token cookie 是 HttpOnly + path=/api/auth/refresh,JS 读不到,无法直接
 * 判断"有没有 refresh cookie"。authMe() 在 App 挂载时 authStore.user 必为 null,
 * 也不能拿它当守卫(会杀掉"刷新页面靠 cookie 恢复登录"的唯一通道)。
 * 所以用这个标记:登录/续期成功写入,登出或会话确定失效(refresh 明确 4xx)时清除;
 * refresh 遇 5xx/断网(unavailable)不清 —— 服务端瞬断不能抹掉"曾登录"事实(BF-0708-1)。
 * 值恒为 '1',不含任何凭据;localStorage 不可用(隐私模式)时静默降级为修复前行为。
 */
export const HAS_SESSION_KEY = 'auth_has_session'

export function markHasSession() {
  try { localStorage.setItem(HAS_SESSION_KEY, '1') } catch { /* 隐私模式,静默 */ }
}

export function clearHasSession() {
  try { localStorage.removeItem(HAS_SESSION_KEY) } catch { /* 隐私模式,静默 */ }
}

export function hasSessionMarker(): boolean {
  try { return localStorage.getItem(HAS_SESSION_KEY) === '1' } catch { return false }
}

/** Attempt token refresh once, then give up */
export let refreshPromise: Promise<RefreshOutcome> | null = null

export async function tryRefresh(): Promise<RefreshOutcome> {
  if (refreshPromise) return refreshPromise
  refreshPromise = fetch(`${BASE}/api/auth/refresh`, {
    method: 'POST',
    credentials: 'same-origin',
  })
    .then((r): RefreshOutcome => {
      if (r.ok) return 'ok'
      // BF-0708-1: 5xx 是服务端故障(如 Supabase 连接被回收),登录态本身仍有效
      if (r.status >= 500) return 'unavailable'
      return 'expired'
    })
    // 网络不可达同理:不能因为断网就把用户登出
    .catch((): RefreshOutcome => 'unavailable')
    .finally(() => { refreshPromise = null })
  return refreshPromise
}

/**
 * 处理 401 响应的共享语义(BF-0420-15 + BF-0420-19 共根治;BF-0708-1 增补 5xx)。
 *
 * 返回:
 *   - 'retry'       → 刷 token 成功,caller 应重试原请求
 *   - 'expired'     → 用户本来登录但 refresh 判定失效,已清 authStore + 跳转登录页
 *   - 'unavailable' → refresh 因服务端故障失败,保留登录态,caller 提示稍后重试
 *   - 'anon'        → 本就匿名,不跳转,caller 自行给"请先登录"提示
 */
export async function handleUnauthorized(): Promise<'retry' | 'expired' | 'anon' | 'unavailable'> {
  const { useAuthStore } = await import('../../store/authStore')
  const currentUser = useAuthStore.getState().user
  if (!currentUser) return 'anon'

  const outcome = await tryRefresh()
  if (outcome === 'ok') return 'retry'
  // BF-0708-1: 服务端故障不得登出,否则一次 DB 抖动就清空所有在线用户
  if (outcome === 'unavailable') return 'unavailable'

  // BF-0420-19: refresh 失败时必须清 authStore,否则 UI 残留"已登录"头衔但 API 全挂
  useAuthStore.getState().setUser(null)
  clearHasSession()
  window.location.hash = 'login'
  return 'expired'
}

export const SERVICE_UNAVAILABLE_MSG = '服务暂时不可用,请稍后重试'

export function serviceUnavailableError(): Error & { status?: number } {
  const e = new Error(SERVICE_UNAVAILABLE_MSG) as Error & { status?: number }
  e.status = 503
  return e
}

export async function apiErrorFromResponse(res: Response): Promise<Error & { status?: number }> {
  const body = await res.json().catch(() => ({}))
  const msg = res.status === 401
    ? (body.detail || body.error || '请先登录(顶栏右上角)')
    : (body.detail || body.error || `API error: ${res.status}`)
  const err = new Error(msg) as Error & { status?: number }
  err.status = res.status
  return err
}

/** Generic fetch wrapper with 401 interceptor */
export async function apiFetch<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${url}`, {
    ...options,
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  })

  if (res.status === 401 && !url.includes('/api/auth/')) {
    const verdict = await handleUnauthorized()
    if (verdict === 'retry') {
      const retry = await fetch(`${BASE}${url}`, {
        ...options,
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          ...options?.headers,
        },
      })
      if (retry.ok) return retry.json()
      // 二次也 401 → 视作 expired;清 store + 跳转
      if (retry.status === 401) {
        const { useAuthStore } = await import('../../store/authStore')
        useAuthStore.getState().setUser(null)
        clearHasSession()
        window.location.hash = 'login'
        throw new Error('Session expired')
      }
      throw await apiErrorFromResponse(retry)
    } else if (verdict === 'expired') {
      throw new Error('Session expired')
    } else if (verdict === 'unavailable') {
      // BF-0708-1: DB/服务瞬断 —— 保持登录态,让用户重试而不是重新登录
      throw serviceUnavailableError()
    }
    // verdict === 'anon' → 继续走下方 !res.ok 分支,返"请先登录"
  }

  if (!res.ok) {
    throw await apiErrorFromResponse(res)
  }
  return res.json()
}
