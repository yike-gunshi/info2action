import { useState } from 'react'
import { Eye, EyeOff, Loader2, ArrowLeft } from 'lucide-react'
import { cn } from '../lib/utils'
import { authResetPassword } from '../lib/api'

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

  if (!token) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background p-5">
        <div className="w-full max-w-[400px] bg-card rounded-2xl p-10 shadow-subtle text-center max-sm:rounded-xl max-sm:p-6 max-sm:shadow-none">
          <p className="text-[14px] text-muted-foreground mb-4">重置链接无效或已过期</p>
          <a href="#forgot-password" className="text-primary text-[13px] font-medium hover:underline">
            重新发送重置链接
          </a>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-background p-5">
      <div className="w-full max-w-[400px] bg-card rounded-2xl p-10 shadow-subtle max-sm:rounded-xl max-sm:p-6 max-sm:shadow-none">
        {/* Brand */}
        <div className="flex items-center gap-2.5 mb-8">
          <div className="w-8 h-8 rounded-lg bg-primary flex items-center justify-center text-primary-foreground font-bold text-sm">
            i2a
          </div>
          <span className="text-xl font-extrabold text-foreground">info2act</span>
        </div>

        {success ? (
          <>
            <h2 className="text-lg font-semibold text-foreground mb-2">密码已重置</h2>
            <p className="text-[14px] text-muted-foreground mb-6">
              你的密码已成功重置，请使用新密码登录。
            </p>
            <a
              href="#login"
              className={cn(
                'w-full h-11 rounded-[10px] text-[15px] font-semibold text-white transition-colors flex items-center justify-center gap-2',
                'bg-primary hover:bg-primary/90',
              )}
            >
              去登录
            </a>
          </>
        ) : (
          <form onSubmit={handleSubmit}>
            <h2 className="text-lg font-semibold text-foreground mb-2">设置新密码</h2>
            <p className="text-[13px] text-muted-foreground mb-6">
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
                className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
                tabIndex={-1}
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
                'w-full h-11 rounded-[10px] text-[15px] font-semibold text-white transition-colors flex items-center justify-center gap-2',
                'bg-primary hover:bg-primary/90',
                'disabled:opacity-50 disabled:cursor-not-allowed',
              )}
            >
              {loading && <Loader2 className="w-4 h-4 animate-spin" />}
              重置密码
            </button>

            <p className="mt-6 text-center">
              <a href="#login" className="text-primary text-[13px] font-medium hover:underline flex items-center justify-center gap-1">
                <ArrowLeft className="w-3.5 h-3.5" />
                返回登录
              </a>
            </p>
          </form>
        )}
      </div>
    </div>
  )
}
