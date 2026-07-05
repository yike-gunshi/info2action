import { useEffect, useRef, useState } from 'react'
import { Loader2 } from 'lucide-react'
import { cn } from '../lib/utils'
import { authVerifyEmail, authResendCode } from '../lib/api'
import { useAuthStore } from '../store/authStore'
import { AuthPageShell } from '../components/shared/AuthPageShell'

export function VerifyEmailPage() {
  const setUser = useAuthStore((s) => s.setUser)

  // Get email from hash params: #verify-email?email=xxx
  const hashParams = new URLSearchParams(window.location.hash.split('?')[1] || '')
  const email = hashParams.get('email') || ''

  const [digits, setDigits] = useState<string[]>(['', '', '', '', '', ''])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [cooldown, setCooldown] = useState(60)
  const [resending, setResending] = useState(false)
  const inputRefs = useRef<(HTMLInputElement | null)[]>([])

  // Cooldown timer
  useEffect(() => {
    if (cooldown <= 0) return
    const t = setTimeout(() => setCooldown((c) => c - 1), 1000)
    return () => clearTimeout(t)
  }, [cooldown])

  function handleChange(index: number, value: string) {
    // Only accept digits
    const digit = value.replace(/\D/g, '').slice(-1)
    const next = [...digits]
    next[index] = digit
    setDigits(next)
    setError('')

    // Auto-advance to next input
    if (digit && index < 5) {
      inputRefs.current[index + 1]?.focus()
    }

    // Auto-submit when all 6 digits filled
    if (digit && index === 5) {
      const code = next.join('')
      if (code.length === 6) {
        handleSubmit(code)
      }
    }
  }

  function handleKeyDown(index: number, e: React.KeyboardEvent) {
    if (e.key === 'Backspace' && !digits[index] && index > 0) {
      inputRefs.current[index - 1]?.focus()
    }
  }

  function handlePaste(e: React.ClipboardEvent) {
    e.preventDefault()
    const pasted = e.clipboardData.getData('text').replace(/\D/g, '').slice(0, 6)
    if (!pasted) return
    const next = [...digits]
    for (let i = 0; i < pasted.length; i++) {
      next[i] = pasted[i]
    }
    setDigits(next)
    if (pasted.length === 6) {
      handleSubmit(pasted)
    } else {
      inputRefs.current[pasted.length]?.focus()
    }
  }

  async function handleSubmit(code?: string) {
    const finalCode = code || digits.join('')
    if (finalCode.length !== 6 || loading) return
    setError('')
    setLoading(true)
    try {
      const res = await authVerifyEmail(email, finalCode)
      setUser(res.user)
      window.location.hash = ''
    } catch (err) {
      setError(err instanceof Error ? err.message : '验证失败')
      // Clear digits on error
      setDigits(['', '', '', '', '', ''])
      inputRefs.current[0]?.focus()
    } finally {
      setLoading(false)
    }
  }

  async function handleResend() {
    if (cooldown > 0 || resending) return
    setResending(true)
    try {
      await authResendCode(email)
      setCooldown(60)
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '发送失败')
    } finally {
      setResending(false)
    }
  }

  if (!email) {
    return (
      <AuthPageShell testId="verify-email-quiet-gate">
        <div className="text-center">
          <p className="mb-4 text-[13px] text-muted-foreground">缺少邮箱参数</p>
          <a href="#register" className="font-medium text-[var(--brand)] hover:underline">
            返回注册
          </a>
        </div>
      </AuthPageShell>
    )
  }

  return (
    <AuthPageShell testId="verify-email-quiet-gate">
      <div className="text-center">
        <h1 className="mb-2 text-[17px] font-semibold text-foreground">验证你的邮箱</h1>
        <p className="mb-6 text-[13px] text-muted-foreground">
          验证码已发送到 <span className="font-medium text-foreground">{email}</span>
        </p>

        {/* 6-digit input */}
        <div className="mb-6 flex justify-center gap-2" onPaste={handlePaste}>
          {digits.map((d, i) => (
            <input
              key={i}
              ref={(el) => { inputRefs.current[i] = el }}
              type="text"
              inputMode="numeric"
              maxLength={1}
              value={d}
              onChange={(e) => handleChange(i, e.target.value)}
              onKeyDown={(e) => handleKeyDown(i, e)}
              autoFocus={i === 0}
              className={cn(
                'h-[52px] w-11 rounded-[6px] border text-center text-lg font-semibold transition-[border-color,box-shadow] duration-150',
                'bg-card text-foreground',
                error ? 'border-destructive' : 'border-input',
                'focus:border-[var(--brand)] focus:outline-none',
                'focus:shadow-[0_0_0_3px_var(--brand-border)]',
              )}
            />
          ))}
        </div>

        {/* Error */}
        {error && (
          <p className="mb-4 text-[12px] text-destructive">{error}</p>
        )}

        {/* Loading */}
        {loading && (
          <div className="mb-4 flex items-center justify-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="w-4 h-4 animate-spin" />
            验证中...
          </div>
        )}

        {/* Resend */}
        <div className="text-[13px] text-muted-foreground">
          没有收到？{' '}
          {cooldown > 0 ? (
            <span className="text-muted-foreground">{cooldown}s 后可重发</span>
          ) : (
            <button
              onClick={handleResend}
              disabled={resending}
              className="font-medium text-[var(--brand)] hover:underline disabled:opacity-50"
            >
              {resending ? '发送中...' : '重新发送'}
            </button>
          )}
        </div>

        {/* Back */}
        <p className="mt-6 text-[13px] text-muted-foreground">
          <a href="#login" className="font-medium text-[var(--brand)] hover:underline">
            返回登录
          </a>
        </p>
      </div>
    </AuthPageShell>
  )
}
