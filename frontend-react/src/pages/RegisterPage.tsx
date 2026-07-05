import { useEffect, useState } from 'react'
import { Eye, EyeOff, Loader2 } from 'lucide-react'
import { cn } from '../lib/utils'
import { authRegister } from '../lib/api'
import { AuthPageShell } from '../components/shared/AuthPageShell'

interface FieldError {
  username?: string
  email?: string
  password?: string
  confirmPw?: string
  invite_code?: string
}

// 模块级缓存 register-config:配置在会话内不变,避免每次进入注册页都先按
// "需要邀请码"渲染再收缩(卡片高度跳变,用户 2026-07-04 反馈)。
let cachedOpenRegistration: boolean | null = null

export function RegisterPage() {
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPw, setConfirmPw] = useState('')
  const [inviteCode, setInviteCode] = useState('')
  const [showPw, setShowPw] = useState(false)
  const [showConfirmPw, setShowConfirmPw] = useState(false)
  const [errors, setErrors] = useState<FieldError>({})
  const [serverError, setServerError] = useState('')
  const [loading, setLoading] = useState(false)
  // P1-4 开放注册三态:null=配置未知(不渲染表单,首次仅一次 ~100ms 请求),
  // true=开放(无邀请码),false=需要邀请码;请求失败保守回退为必填
  const [openRegistration, setOpenRegistration] = useState<boolean | null>(cachedOpenRegistration)

  useEffect(() => {
    if (openRegistration !== null) return
    let alive = true
    fetch('/api/auth/register-config')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        cachedOpenRegistration = Boolean(d?.open_registration)
        if (alive) setOpenRegistration(cachedOpenRegistration)
      })
      .catch(() => {
        cachedOpenRegistration = false
        if (alive) setOpenRegistration(false)
      })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function validate(): FieldError {
    const e: FieldError = {}
    if (!/^[a-zA-Z0-9_]{3,20}$/.test(username))
      e.username = '3-20 个字符，仅字母、数字、下划线'
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email))
      e.email = '请输入有效的邮箱地址'
    if (password.length < 8) e.password = '至少 8 个字符'
    if (confirmPw !== password) e.confirmPw = '两次密码不一致'
    if (openRegistration === false && !/^[A-Z0-9]{8}$/.test(inviteCode))
      e.invite_code = '8 位大写字母或数字'
    return e
  }

  function handleBlur(field: keyof FieldError) {
    const all = validate()
    setErrors((prev) => ({ ...prev, [field]: all[field] }))
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    const v = validate()
    setErrors(v)
    if (Object.keys(v).length > 0) return
    setServerError('')
    setLoading(true)
    try {
      const res = await authRegister({
        username: username.trim(),
        email: email.trim(),
        password,
        ...(inviteCode ? { invite_code: inviteCode } : {}),
      })
      // Redirect to verify email page
      if (res.verify_email) {
        window.location.hash = `verify-email?email=${encodeURIComponent(email.trim())}`
      }
    } catch (err) {
      setServerError(err instanceof Error ? err.message : '注册失败')
    } finally {
      setLoading(false)
    }
  }

  // 配置未知:渲染轻量占位,等 register-config 返回后一次性渲染最终形态,
  // 避免"先出邀请码再收起"的高度跳变
  if (openRegistration === null) {
    return (
      <AuthPageShell testId="register-quiet-gate">
        <div className="flex h-40 items-center justify-center" data-testid="register-config-loading">
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
      </AuthPageShell>
    )
  }

  return (
    <AuthPageShell testId="register-quiet-gate">
      <form onSubmit={handleSubmit}>
        {/* Username */}
        <Field label="用户名" error={errors.username}>
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            onBlur={() => handleBlur('username')}
            placeholder="请输入用户名"
            autoFocus
            autoComplete="username"
            className={cn('auth-input', errors.username && 'auth-input-error')}
          />
        </Field>

        {/* Email */}
        <Field label="邮箱地址" error={errors.email}>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            onBlur={() => handleBlur('email')}
            placeholder="请输入邮箱地址"
            autoComplete="email"
            className={cn('auth-input', errors.email && 'auth-input-error')}
          />
        </Field>

        {/* Password */}
        <Field label="密码" error={errors.password}>
          <div className="relative">
            <input
              type={showPw ? 'text' : 'password'}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onBlur={() => handleBlur('password')}
              placeholder="至少 8 个字符"
              autoComplete="new-password"
              className={cn('auth-input pr-10', errors.password && 'auth-input-error')}
            />
            <PwToggle show={showPw} onToggle={() => setShowPw(!showPw)} />
          </div>
        </Field>

        {/* Confirm Password */}
        <Field label="确认密码" error={errors.confirmPw}>
          <div className="relative">
            <input
              type={showConfirmPw ? 'text' : 'password'}
              value={confirmPw}
              onChange={(e) => setConfirmPw(e.target.value)}
              onBlur={() => handleBlur('confirmPw')}
              placeholder="请再次输入密码"
              autoComplete="new-password"
              className={cn('auth-input pr-10', errors.confirmPw && 'auth-input-error')}
            />
            <PwToggle show={showConfirmPw} onToggle={() => setShowConfirmPw(!showConfirmPw)} />
          </div>
        </Field>

        {/* Invite Code(开放注册时隐藏)*/}
        {openRegistration === false && (
          <Field label="邀请码" error={errors.invite_code}>
            <input
              type="text"
              value={inviteCode}
              onChange={(e) => setInviteCode(e.target.value.toUpperCase())}
              onBlur={() => handleBlur('invite_code')}
              placeholder="请输入邀请码"
              maxLength={8}
              autoComplete="off"
              className={cn(
                'auth-input font-mono tracking-[2px] uppercase',
                errors.invite_code && 'auth-input-error',
              )}
            />
          </Field>
        )}

        {/* Server error */}
        {serverError && (
          <p className="text-[12px] text-destructive mb-4 pl-0.5">{serverError}</p>
        )}

        {/* Terms notice */}
        <p className="mb-4 mt-5 text-center text-[12px] text-muted-foreground">
          注册即表示同意{' '}
          <a href="#terms" className="text-[var(--brand)] hover:underline">使用条款</a>
          {' '}和{' '}
          <a href="#privacy" className="text-[var(--brand)] hover:underline">隐私政策</a>
        </p>

        {/* Submit */}
        <button
          type="submit"
          disabled={loading}
          className={cn(
            'flex h-11 w-full items-center justify-center gap-2 rounded-[4px] font-body-cjk text-[15px] font-semibold text-[var(--brand-foreground)] transition-colors',
            'bg-[var(--brand)] hover:bg-[color-mix(in_srgb,var(--brand)_86%,#171512)]',
            'disabled:opacity-50 disabled:cursor-not-allowed',
          )}
          data-testid="register-submit"
        >
          {loading && <Loader2 className="w-4 h-4 animate-spin" />}
          注册
        </button>

        {/* Footer */}
        <p className="mt-4 text-center text-[13px] text-muted-foreground">
          已有账号？{' '}
          <a href="#login" className="font-medium text-[var(--brand)] hover:underline">
            登录
          </a>
        </p>
      </form>
    </AuthPageShell>
  )
}

function Field({ label, error, children }: { label: string; error?: string; children: React.ReactNode }) {
  return (
    <div className="mb-4">
      <label className="block mb-1.5 text-[13px] font-medium text-muted-foreground">
        {label}
      </label>
      {children}
      {error && <p className="text-[12px] text-destructive mt-1 pl-0.5">{error}</p>}
    </div>
  )
}

function PwToggle({ show, onToggle }: { show: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className="absolute right-1 top-1/2 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-[4px] text-muted-foreground transition-colors hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]"
      aria-label={show ? '隐藏密码' : '显示密码'}
    >
      {show ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
    </button>
  )
}
