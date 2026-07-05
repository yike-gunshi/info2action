import { useEffect, useState, useCallback } from 'react'
import { ArrowLeft, ArrowRight, Loader2, Lock, Eye, EyeOff, Check, Pencil, X } from 'lucide-react'
import { cn } from '../lib/utils'
import { useAuthStore } from '../store/authStore'
import { getUserSettings, updateUserSettings, getUserProfile, updateUserProfile } from '../lib/api'
import { ROLES, INTERESTS, TOOLS, getRoleLabel, getInterestLabels, getToolLabels } from '../lib/profileOptions'
import { toast } from 'sonner'
import { TopBar } from '../components/layout/TopBar'

export function SettingsPage() {
  const user = useAuthStore((s) => s.user)
  const [token, setToken] = useState('')
  const [masked, setMasked] = useState('')
  const [hasToken, setHasToken] = useState(false)
  const [showToken, setShowToken] = useState(false)
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(true)

  // Profile state
  const [profileRole, setProfileRole] = useState<string | null>(null)
  const [profileInterests, setProfileInterests] = useState<string[]>([])
  const [profileTools, setProfileTools] = useState<string[]>([])
  const [profileLoading, setProfileLoading] = useState(true)
  const [editModalOpen, setEditModalOpen] = useState(false)

  useEffect(() => {
    getUserSettings()
      .then((s) => {
        setMasked(s.discord_bot_token || '')
        setHasToken(s.has_discord_token)
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    getUserProfile()
      .then((res) => {
        if (res.profile) {
          setProfileRole(res.profile.role)
          setProfileInterests(res.profile.interests || [])
          setProfileTools(res.profile.tools || [])
        }
      })
      .catch(() => {})
      .finally(() => setProfileLoading(false))
  }, [])

  async function handleSave() {
    if (!token.trim()) return
    setSaving(true)
    try {
      await updateUserSettings({ discord_bot_token: token.trim() })
      setHasToken(true)
      setMasked(token.slice(0, 8) + '...' + token.slice(-4))
      setToken('')
      toast.success('Discord Token 已保存')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  async function handleClear() {
    setSaving(true)
    try {
      await updateUserSettings({ discord_bot_token: '' })
      setHasToken(false)
      setMasked('')
      setToken('')
      toast.success('Discord Token 已清除')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '清除失败')
    } finally {
      setSaving(false)
    }
  }

  const handleProfileSaved = useCallback((role: string | null, interests: string[], tools: string[]) => {
    setProfileRole(role)
    setProfileInterests(interests)
    setProfileTools(tools)
    setEditModalOpen(false)
  }, [])

  return (
    <div className="min-h-screen bg-background" style={{ overflowX: 'clip' }}>
      <TopBar activeL1={null} />

      <main className="mx-auto max-w-[840px] px-4 py-6 sm:px-5 sm:py-8">
        <div className="mb-6 px-1">
          <h1 className="font-display text-[28px] font-semibold leading-tight tracking-normal text-foreground">
            用户设置
          </h1>
          <p className="mt-1 font-body-cjk text-[13px] text-muted-foreground">
            管理账号、个人画像和行动派发配置
          </p>
        </div>

        {/* User info */}
        <section className="mb-5 rounded-[4px] border border-border bg-card p-5 shadow-none sm:p-6">
          <h2 className="mb-4 font-event-title text-[18px] font-semibold leading-tight text-foreground">账号信息</h2>
          <div className="grid grid-cols-[88px_minmax(0,1fr)] gap-x-4 gap-y-2 font-body-cjk text-sm">
            <div className="text-muted-foreground">用户名</div>
            <div className="min-w-0 truncate text-foreground">{user?.username}</div>
            <div className="text-muted-foreground">邮箱</div>
            <div className="min-w-0 truncate text-foreground">{user?.email}</div>
            <div className="text-muted-foreground">角色</div>
            <div className="text-foreground">{user?.role === 'admin' ? '管理员' : '用户'}</div>
          </div>
        </section>

        {/* Profile / Onboarding settings */}
        <section className="mb-5 rounded-[4px] border border-border bg-card p-5 shadow-none sm:p-6">
          <div className="mb-4 flex items-center justify-between gap-3">
            <h2 className="font-event-title text-[18px] font-semibold leading-tight text-foreground">个人画像</h2>
            <button
              onClick={() => setEditModalOpen(true)}
              className="inline-flex h-8 items-center gap-1.5 rounded-[4px] px-2.5 font-body-cjk text-sm font-medium text-[var(--brand)] transition-colors hover:bg-[var(--brand-soft)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              <Pencil className="h-3.5 w-3.5" />
              编辑
            </button>
          </div>

          {profileLoading ? (
            <div className="flex items-center gap-2 font-body-cjk text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin text-[var(--brand)]" />
              加载中...
            </div>
          ) : (
            <div className="space-y-4">
              <div>
                <span className="font-body-cjk text-[13px] text-muted-foreground">角色</span>
                <p className="mt-0.5 font-body-cjk text-sm font-medium text-foreground">{getRoleLabel(profileRole)}</p>
              </div>
              <div>
                <span className="font-body-cjk text-[13px] text-muted-foreground">关注方向</span>
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {profileInterests.length > 0 ? (
                    getInterestLabels(profileInterests).map((label) => (
                      <span key={label} className="rounded-[4px] border border-[var(--brand-border)] bg-[var(--brand-soft)] px-2.5 py-1 font-body-cjk text-[13px] font-medium text-[var(--brand)]">
                        {label}
                      </span>
                    ))
                  ) : (
                    <span className="font-body-cjk text-sm text-muted-foreground">未设置</span>
                  )}
                </div>
              </div>
              <div>
                <span className="font-body-cjk text-[13px] text-muted-foreground">常用工具</span>
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {profileTools.length > 0 ? (
                    getToolLabels(profileTools).map((label) => (
                      <span key={label} className="rounded-[4px] border border-border bg-muted px-2.5 py-1 font-body-cjk text-[13px] font-medium text-foreground">
                        {label}
                      </span>
                    ))
                  ) : (
                    <span className="font-body-cjk text-sm text-muted-foreground">未设置</span>
                  )}
                </div>
              </div>
            </div>
          )}
        </section>

        {/* Discord Token */}
        <section className="rounded-[4px] border border-border bg-card p-5 shadow-none sm:p-6">
          <h2 className="font-event-title text-[18px] font-semibold leading-tight text-foreground">Discord Bot Token</h2>
          <p className="mb-4 mt-1 font-body-cjk text-[13px] text-muted-foreground">
            配置你的 Discord Bot Token，用于将行动建议派发到你的 Discord
          </p>

          {loading ? (
            <div className="flex items-center gap-2 font-body-cjk text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin text-[var(--brand)]" />
              加载中...
            </div>
          ) : (
            <>
              {hasToken && (
                <div className="mb-4 flex flex-wrap items-center gap-2 font-body-cjk text-sm">
                  <span className="text-muted-foreground">当前 Token：</span>
                  <code className="rounded-[4px] bg-muted px-2 py-0.5 font-mono text-foreground">
                    {masked}
                  </code>
                  <button
                    onClick={handleClear}
                    disabled={saving}
                    className="ml-1 font-body-cjk text-xs text-destructive hover:underline"
                  >
                    清除
                  </button>
                </div>
              )}

              <div className="flex flex-col gap-2 sm:flex-row">
                <div className="relative flex-1">
                  <input
                    type={showToken ? 'text' : 'password'}
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                    placeholder={hasToken ? '输入新 Token 替换...' : '输入 Discord Bot Token...'}
                    className="auth-input pr-10"
                  />
                  <button
                    type="button"
                    onClick={() => setShowToken(!showToken)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
                    tabIndex={-1}
                  >
                    {showToken ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                  </button>
                </div>
                <button
                  onClick={handleSave}
                  disabled={saving || !token.trim()}
                  className={cn(
                    'inline-flex h-11 items-center justify-center gap-2 rounded-[4px] px-5 font-body-cjk text-sm font-semibold transition-colors',
                    'bg-[var(--brand)] text-[var(--brand-foreground)] hover:brightness-95',
                    'disabled:opacity-50 disabled:cursor-not-allowed',
                  )}
                >
                  {saving && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                  保存
                </button>
              </div>

              <p className="mt-3 flex items-center gap-1.5 font-body-cjk text-[12px] text-muted-foreground">
                <Lock className="h-3 w-3" />
                Token 以 AES-256-GCM 加密存储
              </p>
            </>
          )}
        </section>
      </main>

      {/* Profile edit modal */}
      {editModalOpen && (
        <ProfileEditModal
          initialRole={profileRole}
          initialInterests={profileInterests}
          initialTools={profileTools}
          onSave={handleProfileSaved}
          onClose={() => setEditModalOpen(false)}
        />
      )}
    </div>
  )
}

// ── Profile Edit Modal (reuses Onboarding-style UI) ──

function ProfileEditModal({
  initialRole,
  initialInterests,
  initialTools,
  onSave,
  onClose,
}: {
  initialRole: string | null
  initialInterests: string[]
  initialTools: string[]
  onSave: (role: string | null, interests: string[], tools: string[]) => void
  onClose: () => void
}) {
  type Step = 0 | 1 | 2
  const [step, setStep] = useState<Step>(0)
  const [role, setRole] = useState<string | null>(initialRole)
  const [interests, setInterests] = useState<Set<string>>(new Set(initialInterests))
  const [tools, setTools] = useState<Set<string>>(new Set(initialTools))
  const [saving, setSaving] = useState(false)

  const toggleSet = (set: Set<string>, setFn: (s: Set<string>) => void, id: string) => {
    const next = new Set(set)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setFn(next)
  }

  const canNext = step === 0 ? !!role : step === 1 ? interests.size >= 1 : true

  const handleFinish = useCallback(async () => {
    setSaving(true)
    try {
      await updateUserProfile({
        role: role!,
        interests: Array.from(interests),
        tools: Array.from(tools),
      })
      onSave(role, Array.from(interests), Array.from(tools))
      toast.success('个人画像已更新')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }, [role, interests, tools, onSave])

  const handleNext = () => {
    if (step < 2) setStep((step + 1) as Step)
    else handleFinish()
  }

  return (
    <div className="fixed inset-0 z-[700] flex items-center justify-center bg-black/50" onClick={onClose}>
      <div
        className="mx-4 w-full max-w-[520px] rounded-[6px] border border-border bg-card p-5 shadow-medium sm:p-6"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="mb-4 flex items-center justify-between">
          <h2 className="font-event-title text-[18px] font-semibold leading-tight text-foreground">编辑个人画像</h2>
          <button
            onClick={onClose}
            className="rounded-[4px] p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Progress */}
        <div className="mb-6 flex items-center gap-2">
          {[0, 1, 2].map((i) => (
            <div
              key={i}
              className={cn(
                'h-1.5 flex-1 rounded-[4px] transition-colors',
                i <= step ? 'bg-[var(--brand)]' : 'bg-muted'
              )}
            />
          ))}
        </div>

        {/* Step 0: Role */}
        {step === 0 && (
          <div>
            <h3 className="mb-1 font-body-cjk text-sm font-semibold text-foreground">你的角色</h3>
            <p className="mb-4 font-body-cjk text-xs text-muted-foreground">选择最贴近你的角色</p>
            <div className="grid grid-cols-2 gap-2">
              {ROLES.map((r) => (
                <button
                  key={r.id}
                  onClick={() => setRole(r.id)}
                  className={cn(
                    'rounded-[4px] border p-3 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]',
                    role === r.id
                      ? 'border-[var(--brand-border)] bg-[var(--brand-soft)]'
                      : 'border-border hover:border-[var(--brand-border)]'
                  )}
                >
                  <div className="font-body-cjk text-sm font-medium text-foreground">{r.label}</div>
                  {r.desc && <div className="mt-0.5 font-body-cjk text-xs text-muted-foreground">{r.desc}</div>}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Step 1: Interests */}
        {step === 1 && (
          <div>
            <h3 className="mb-1 font-body-cjk text-sm font-semibold text-foreground">关注方向</h3>
            <p className="mb-4 font-body-cjk text-xs text-muted-foreground">选择你感兴趣的方向（至少 1 个）</p>
            <div className="flex flex-wrap gap-2">
              {INTERESTS.map((i) => (
                <button
                  key={i.id}
                  onClick={() => toggleSet(interests, setInterests, i.id)}
                  className={cn(
                    'rounded-[4px] border px-3 py-1.5 font-body-cjk text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]',
                    interests.has(i.id)
                      ? 'border-[var(--brand-border)] bg-[var(--brand-soft)] text-[var(--brand)]'
                      : 'border-border text-muted-foreground hover:border-[var(--brand-border)] hover:text-foreground'
                  )}
                >
                  {interests.has(i.id) && <Check className="mr-1 inline h-3.5 w-3.5 -translate-y-px" />}
                  {i.label}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Step 2: Tools */}
        {step === 2 && (
          <div>
            <h3 className="mb-1 font-body-cjk text-sm font-semibold text-foreground">常用工具</h3>
            <p className="mb-4 font-body-cjk text-xs text-muted-foreground">选择你日常使用的 AI 工具（可跳过）</p>
            <div className="flex flex-wrap gap-2">
              {TOOLS.map((t) => (
                <button
                  key={t.id}
                  onClick={() => toggleSet(tools, setTools, t.id)}
                  className={cn(
                    'rounded-[4px] border px-3 py-1.5 font-body-cjk text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]',
                    tools.has(t.id)
                      ? 'border-[var(--brand-border)] bg-[var(--brand-soft)] text-[var(--brand)]'
                      : 'border-border text-muted-foreground hover:border-[var(--brand-border)] hover:text-foreground'
                  )}
                >
                  {tools.has(t.id) && <Check className="mr-1 inline h-3.5 w-3.5 -translate-y-px" />}
                  {t.label}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Navigation */}
        <div className="mt-6 flex items-center justify-between">
          {step > 0 ? (
            <button
              onClick={() => setStep((step - 1) as Step)}
              className="flex items-center gap-1 rounded-[4px] px-3 py-1.5 font-body-cjk text-sm font-medium text-muted-foreground transition-colors hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]"
            >
              <ArrowLeft className="h-4 w-4" />
              上一步
            </button>
          ) : (
            <div />
          )}

          <button
            onClick={handleNext}
            disabled={!canNext || saving}
            className={cn(
              'flex items-center gap-1 rounded-[4px] px-4 py-2 font-body-cjk text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]',
              canNext
                ? 'bg-[var(--brand)] text-[var(--brand-foreground)] hover:brightness-95'
                : 'bg-muted text-muted-foreground cursor-not-allowed'
            )}
          >
            {saving ? '保存中...' : step === 2 ? '保存' : '下一步'}
            {step < 2 ? <ArrowRight className="h-4 w-4" /> : <Check className="h-4 w-4" />}
          </button>
        </div>

        <p className="mt-3 text-center font-body-cjk text-xs text-muted-foreground">
          {step + 1} / 3
        </p>
      </div>
    </div>
  )
}
