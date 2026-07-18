import { useState } from 'react'
import { Eye, EyeOff, Loader2, ArrowLeft } from 'lucide-react'
import { cn } from '../lib/utils'
import { authResetPassword } from '../lib/api'
import { AuthPageShell } from '../components/shared/AuthPageShell'

export function ResetPasswordPage() {
  const [password, setPassword] = useState('')
  const [confirmPw, setConfirmPw] = useState('')
  const [showPw, setShowPw] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState(false)
  const [loading, setLoading] = useState(false)

  // Extract token from hash: #reset-password?token=xxx
  const hashParams = new URLSearchParams(window.location.hash.split('?')[1] || '')
  const token = hashParams.get('token') || ''

  const canSubmit = password.length >= 8 && confirmPw === password && token.length > 0

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!canSubmit || loading) return
    setError('')
    setLoading(true)
    try {
      await authResetPassword(token, password)
      setSuccess(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : '重置失败')
    } finally {
      setLoading(false)
    }
  }

  // v24.0 §21.6: 残留 i2a 方块标退役 → 复用 AuthPageShell(v20.5 安静门,卡内 BrandWordmark)
  if (!token) {
    return (
      <AuthPageShell testId="reset-password-quiet-gate">
        <div className="text-center">
          <p className="mb-4 text-[14px] text-muted-foreground">重置链接无效或已过期</p>
          <a
            href="#forgot-password"
            className="inline-flex items-center gap-1 text-[13px] font-medium text-[var(--brand)] hover:underline"
          >
            重新发送重置链接
          </a>
        </div>
      </AuthPageShell>
    )
  }

  return (
    <AuthPageShell testId="reset-password-quiet-gate">
      {success ? (
        <>
          <h2 className="mb-2 text-[17px] font-semibold text-foreground">密码已重置</h2>
          <p className="mb-6 text-[14px] text-muted-foreground">
            你的密码已成功重置，请使用新密码登录。
          </p>
          <a
            href="#login"
            className={cn(
              'flex h-11 w-full items-center justify-center gap-2 rounded-[4px] font-body-cjk text-[15px] font-semibold text-[var(--brand-foreground)] transition-colors',
              'bg-[var(--brand)] hover:bg-[color-mix(in_srgb,var(--brand)_86%,#171512)]',
            )}
          >
            去登录
          </a>
        </>
      ) : (
        <form onSubmit={handleSubmit}>
          <h2 className="mb-2 text-[17px] font-semibold text-foreground">设置新密码</h2>
          <p className="mb-6 text-[13px] text-muted-foreground">
            请输入新密码（至少 8 个字符）。
          </p>

          {/* New Password */}
          <label className="block mb-1.5 text-[13px] font-medium text-muted-foreground">
            新密码
          </label>
          <div className="relative mb-4">
            <input
              type={showPw ? 'text' : 'password'}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="至少 8 个字符"
              autoFocus
              autoComplete="new-password"
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

          {/* Confirm Password */}
          <label className="block mb-1.5 text-[13px] font-medium text-muted-foreground">
            确认密码
          </label>
          <input
            type="password"
            value={confirmPw}
            onChange={(e) => setConfirmPw(e.target.value)}
            placeholder="再次输入新密码"
            autoComplete="new-password"
            className="auth-input mb-6"
          />

          {confirmPw && confirmPw !== password && (
            <p className="text-[12px] text-destructive mb-4 pl-0.5 -mt-4">两次输入的密码不一致</p>
          )}

          {error && (
            <p className="text-[12px] text-destructive mb-4 pl-0.5">{error}</p>
          )}

          <button
            type="submit"
            disabled={!canSubmit || loading}
            className={cn(
              'flex h-11 w-full items-center justify-center gap-2 rounded-[4px] font-body-cjk text-[15px] font-semibold text-[var(--brand-foreground)] transition-colors',
              'bg-[var(--brand)] hover:bg-[color-mix(in_srgb,var(--brand)_86%,#171512)]',
              'disabled:opacity-50 disabled:cursor-not-allowed',
            )}
          >
            {loading && <Loader2 className="w-4 h-4 animate-spin" />}
            重置密码
          </button>

          <p className="mt-6 text-center">
            <a
              href="#login"
              className="inline-flex items-center justify-center gap-1 text-[13px] font-medium text-muted-foreground transition-colors hover:text-[var(--brand)]"
            >
              <ArrowLeft className="w-3.5 h-3.5" />
              返回登录
            </a>
          </p>
        </form>
      )}
    </AuthPageShell>
  )
}
