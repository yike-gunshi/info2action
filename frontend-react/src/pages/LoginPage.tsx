import { useState } from 'react'
import type { MouseEvent } from 'react'
import { Eye, EyeOff, Loader2 } from 'lucide-react'
import { cn } from '../lib/utils'
import { authLogin } from '../lib/api'
import { useAuthStore } from '../store/authStore'
import { useUIStore } from '../store/uiStore'
import { BrandWordmark } from '../components/shared/BrandWordmark'

export function LoginPage() {
  const setUser = useAuthStore((s) => s.setUser)
  const setL1 = useUIStore((s) => s.setL1)

  const [login, setLogin] = useState('')
  const [password, setPassword] = useState('')
  const [showPw, setShowPw] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const canSubmit = login.trim().length > 0 && password.length >= 1

  function handleBrandClick(e: MouseEvent<HTMLAnchorElement>) {
    e.preventDefault()
    setL1('highlights')
    window.location.hash = ''
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!canSubmit || loading) return
    setError('')
    setLoading(true)
    try {
      const user = await authLogin(login.trim(), password)
      setUser(user)
      window.location.hash = ''
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen bg-background">
      <header className="fixed inset-x-0 top-0 z-30 border-b border-border/80 bg-background/95 backdrop-blur-[2px]">
        <div className="mx-auto flex h-14 max-w-[1440px] items-center px-4 sm:px-5">
          <a
            href="#"
            onClick={handleBrandClick}
            className="rounded-[2px] text-[26px] transition-[filter] hover:brightness-95 focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            aria-label="返回精选"
            data-testid="login-topbar-logo"
          >
            <BrandWordmark />
          </a>
        </div>
      </header>

      <div className="flex min-h-screen items-center justify-center px-5 pb-8 pt-20">
        <form
          onSubmit={handleSubmit}
          className="w-full max-w-[340px] rounded-[6px] border border-border bg-card px-5 py-6 shadow-none sm:px-6 sm:py-7"
          data-testid="login-quiet-gate"
        >
          <div className="mb-7 text-center">
            <BrandWordmark
              className="text-[36px]"
              data-testid="login-wordmark"
            />
          </div>

          {/* Email / Username */}
          <label htmlFor="login-identifier" className="block mb-1.5 text-[13px] font-medium text-muted-foreground">
            邮箱或用户名
          </label>
          <input
            id="login-identifier"
            type="text"
            value={login}
            onChange={(e) => setLogin(e.target.value)}
            placeholder="请输入邮箱或用户名"
            autoFocus
            autoComplete="username"
            className="auth-input mb-4"
          />

          {/* Password */}
          <label htmlFor="login-password" className="block mb-1.5 text-[13px] font-medium text-muted-foreground">
            密码
          </label>
          <div className="relative mb-6">
            <input
              id="login-password"
              type={showPw ? 'text' : 'password'}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="请输入密码"
              autoComplete="current-password"
              className="auth-input pr-10"
            />
            <button
              type="button"
              onClick={() => setShowPw(!showPw)}
              className="absolute right-1 top-1/2 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-[4px] text-muted-foreground transition-colors hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]"
              aria-label={showPw ? '隐藏密码' : '显示密码'}
            >
              {showPw ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
          </div>

          {/* Error */}
          {error && (
            <p className="text-[12px] text-destructive mb-4 pl-0.5">{error}</p>
          )}

          {/* Submit */}
          <button
            type="submit"
            disabled={!canSubmit || loading}
            className={cn(
              'flex h-11 w-full items-center justify-center gap-2 rounded-[4px] font-body-cjk text-[15px] font-semibold text-[var(--brand-foreground)] transition-colors',
              'bg-[var(--brand)] hover:bg-[color-mix(in_srgb,var(--brand)_86%,#171512)]',
              'disabled:opacity-50 disabled:cursor-not-allowed',
            )}
            data-testid="login-submit"
          >
            {loading && <Loader2 className="w-4 h-4 animate-spin" />}
            登录
          </button>

          {/* Forgot password */}
          <p className="mt-4 text-center">
            <a
              href="#forgot-password"
              className="font-body-cjk text-[13px] text-muted-foreground transition-colors hover:text-[var(--brand)]"
            >
              忘记密码？
            </a>
          </p>

          {/* Footer */}
          <p className="mt-3 text-center text-[13px] text-muted-foreground">
            没有账号？{' '}
            <a
              href="#register"
              className="font-medium text-[var(--brand)] hover:underline"
            >
              注册
            </a>
          </p>
        </form>
      </div>
    </div>
  )
}
