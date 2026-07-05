import { useState } from 'react'
import { Loader2, ArrowLeft } from 'lucide-react'
import { cn } from '../lib/utils'
import { authForgotPassword } from '../lib/api'
import { AuthPageShell } from '../components/shared/AuthPageShell'

export function ForgotPasswordPage() {
  const [email, setEmail] = useState('')
  const [error, setError] = useState('')
  const [sent, setSent] = useState(false)
  const [loading, setLoading] = useState(false)

  const canSubmit = email.trim().length > 0 && email.includes('@')

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!canSubmit || loading) return
    setError('')
    setLoading(true)
    try {
      await authForgotPassword(email.trim().toLowerCase())
      setSent(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : '发送失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <AuthPageShell testId="forgot-password-quiet-gate">
      {sent ? (
        <>
          <h2 className="mb-2 text-[17px] font-semibold text-foreground">邮件已发送</h2>
          <p className="mb-6 text-[13px] text-muted-foreground">
            如果该邮箱已注册，你将收到密码重置链接。请检查收件箱（含垃圾邮件）。
          </p>
          <a
            href="#login"
            className="flex items-center gap-1 text-[13px] font-medium text-[var(--brand)] hover:underline"
          >
            <ArrowLeft className="w-3.5 h-3.5" />
            返回登录
          </a>
        </>
      ) : (
        <form onSubmit={handleSubmit}>
          <h2 className="mb-2 text-[17px] font-semibold text-foreground">忘记密码</h2>
          <p className="mb-6 text-[13px] text-muted-foreground">
            输入注册邮箱，我们将发送密码重置链接。
          </p>

          <label className="block mb-1.5 text-[13px] font-medium text-muted-foreground">
            邮箱
          </label>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="请输入注册邮箱"
            autoFocus
            autoComplete="email"
            className="auth-input mb-4"
          />

          {error && (
            <p className="mb-4 pl-0.5 text-[12px] text-destructive">{error}</p>
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
            发送重置链接
          </button>

          <p className="mt-4 text-center text-[13px] text-muted-foreground">
            <a
              href="#login"
              className="flex items-center justify-center gap-1 font-medium text-[var(--brand)] hover:underline"
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
