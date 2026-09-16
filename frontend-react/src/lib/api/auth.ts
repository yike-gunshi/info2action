import type { AuthUser } from '../../store/authStore'
import { BASE, apiFetch, markHasSession, clearHasSession, hasSessionMarker, tryRefresh } from './_client'

// ── Auth ──

export async function authLogin(login: string, password: string): Promise<AuthUser> {
  const res = await fetch(`${BASE}/api/auth/login`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ login, password }),
  })
  const body = await res.json().catch(() => ({}))

  // 403 = email not verified → redirect to verify page
  if (res.status === 403 && body.verify_email) {
    window.location.hash = `verify-email?email=${encodeURIComponent(body.email)}`
    throw new Error(body.error || '请先验证邮箱')
  }

  if (!res.ok) {
    throw new Error(body.error || body.detail || `Login failed: ${res.status}`)
  }
  markHasSession() // BF-0708-2
  return body.user
}

export async function authRegister(data: {
  username: string
  email: string
  password: string
  invite_code?: string // P1-4 开放注册时可省略
}): Promise<{ ok: boolean; verify_email: boolean; email: string; message: string }> {
  return apiFetch('/api/auth/register', {
    method: 'POST',
    body: JSON.stringify(data),
  })
}

export async function authLogout(): Promise<void> {
  try {
    await apiFetch('/api/auth/logout', { method: 'POST' })
  } finally {
    // BF-0708-2: 无论请求成败都清标记 —— 用户意图即登出
    clearHasSession()
  }
}

export async function authMe(): Promise<AuthUser> {
  const readMe = () => fetch(`${BASE}/api/auth/me`, { credentials: 'same-origin' })
  let res = await readMe()
  if (res.status === 401 && hasSessionMarker()) {
    // BF-0708-2: 匿名访客没有"曾登录"标记,不再空转 refresh(必 401 的白打请求)。
    // 标记在 → 走 BF-0708-1 三态续期,保住"刷新页面靠 cookie 恢复登录"通道。
    // 不能写 `if (outcome)` —— 'expired' / 'unavailable' 都是 truthy 字符串。
    const outcome = await tryRefresh()
    if (outcome === 'ok') res = await readMe()
    // refresh 明确 4xx = 会话真失效 → 清标记,后续页面加载不再空转;
    // 'unavailable'(5xx/断网)不清,瞬断恢复后仍可自动续期(BF-0708-1 语义)
    else if (outcome === 'expired') clearHasSession()
  }
  if (!res.ok) throw new Error('Not authenticated')
  const user: AuthUser = await res.json()
  markHasSession() // BF-0708-2: 会话已确认,标记丢失时自愈
  return user
}

export async function authRefresh(): Promise<{ ok: boolean }> {
  return apiFetch('/api/auth/refresh', { method: 'POST' })
}

export async function authVerifyEmail(email: string, code: string): Promise<{ ok: boolean; user: AuthUser }> {
  const res = await apiFetch<{ ok: boolean; user: AuthUser }>('/api/auth/verify-email', {
    method: 'POST',
    body: JSON.stringify({ email, code }),
  })
  markHasSession() // BF-0708-2: 后端验证邮箱即自动登录种 cookie(src/routes/auth.py)
  return res
}

export async function authResendCode(email: string): Promise<{ ok: boolean }> {
  return apiFetch('/api/auth/resend-code', {
    method: 'POST',
    body: JSON.stringify({ email }),
  })
}

export async function authForgotPassword(email: string): Promise<{ ok: boolean; message: string }> {
  return apiFetch('/api/auth/forgot-password', {
    method: 'POST',
    body: JSON.stringify({ email }),
  })
}

export async function authResetPassword(token: string, password: string): Promise<{ ok: boolean; message: string }> {
  return apiFetch('/api/auth/reset-password', {
    method: 'POST',
    body: JSON.stringify({ token, password }),
  })
}
