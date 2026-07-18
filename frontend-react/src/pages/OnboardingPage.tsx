import { useState, useCallback } from 'react'
import { ArrowRight, ArrowLeft, Check } from 'lucide-react'
import { cn } from '../lib/utils'
import { updateUserProfile } from '../lib/api'
import { useAuthStore } from '../store/authStore'
import { ROLES, INTERESTS, TOOLS } from '../lib/profileOptions'
import { BrandWordmark } from '../components/shared/BrandWordmark'

type Step = 0 | 1 | 2

interface Props {
  onComplete: () => void
}

// v24.0 §21.6: 三步画像页从全靛蓝 AI-SaaS 换皮为纸面语言（结构不动）——
// 样板 = SettingsPage「编辑个人画像」弹窗(brand 进度条/brand-soft 选中态) + LoginPage 安静门。
export function OnboardingPage({ onComplete }: Props) {
  const [step, setStep] = useState<Step>(0)
  const [role, setRole] = useState<string | null>(null)
  const [interests, setInterests] = useState<Set<string>>(new Set())
  const [tools, setTools] = useState<Set<string>>(new Set())
  const [saving, setSaving] = useState(false)
  const setUser = useAuthStore((s) => s.setUser)
  const user = useAuthStore((s) => s.user)

  const toggleSet = (set: Set<string>, setFn: (s: Set<string>) => void, id: string) => {
    const next = new Set(set)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setFn(next)
  }

  const canNext = step === 0 ? !!role : step === 1 ? interests.size >= 1 : tools.size >= 0

  const handleFinish = useCallback(async () => {
    setSaving(true)
    try {
      await updateUserProfile({
        role: role!,
        interests: Array.from(interests),
        tools: Array.from(tools),
        onboarding_completed: true,
      })
      // Update auth store
      if (user) {
        setUser({ ...user, onboarding_completed: true })
      }
      onComplete()
    } catch (err) {
      console.error('Failed to save profile:', err)
    } finally {
      setSaving(false)
    }
  }, [role, interests, tools, user, setUser, onComplete])

  const handleNext = () => {
    if (step < 2) setStep((step + 1) as Step)
    else handleFinish()
  }

  const chipClass = (selected: boolean) =>
    cn(
      'rounded-[4px] border px-3 py-1.5 font-body-cjk text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]',
      selected
        ? 'border-[var(--brand-border)] bg-[var(--brand-soft)] text-[var(--brand)]'
        : 'border-border text-muted-foreground hover:border-[var(--brand-border)] hover:text-foreground',
    )

  return (
    <div className="min-h-screen bg-background flex flex-col items-center justify-center p-4">
      <div className="w-full max-w-[520px]">
        {/* Brand */}
        <div className="mb-8 text-center">
          <BrandWordmark className="text-[34px]" />
        </div>

        {/* Progress */}
        <div className="flex items-center gap-2 mb-8">
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
            <h2 className="mb-1 font-event-title text-[22px] font-semibold leading-tight text-foreground">你的角色</h2>
            <p className="mb-6 font-body-cjk text-sm text-muted-foreground">选择最贴近你的角色，帮助我们理解你的信息需求</p>
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
            <h2 className="mb-1 font-event-title text-[22px] font-semibold leading-tight text-foreground">关注方向</h2>
            <p className="mb-6 font-body-cjk text-sm text-muted-foreground">选择你感兴趣的方向（至少 1 个）</p>
            <div className="flex flex-wrap gap-2">
              {INTERESTS.map((i) => (
                <button
                  key={i.id}
                  onClick={() => toggleSet(interests, setInterests, i.id)}
                  className={chipClass(interests.has(i.id))}
                >
                  {interests.has(i.id) && <Check className="w-3.5 h-3.5 inline mr-1 -mt-0.5" />}
                  {i.label}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Step 2: Tools */}
        {step === 2 && (
          <div>
            <h2 className="mb-1 font-event-title text-[22px] font-semibold leading-tight text-foreground">常用工具</h2>
            <p className="mb-6 font-body-cjk text-sm text-muted-foreground">选择你日常使用的 AI 工具（可跳过）</p>
            <div className="flex flex-wrap gap-2">
              {TOOLS.map((t) => (
                <button
                  key={t.id}
                  onClick={() => toggleSet(tools, setTools, t.id)}
                  className={chipClass(tools.has(t.id))}
                >
                  {tools.has(t.id) && <Check className="w-3.5 h-3.5 inline mr-1 -mt-0.5" />}
                  {t.label}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Navigation */}
        <div className="flex items-center justify-between mt-8">
          {step > 0 ? (
            <button
              onClick={() => setStep((step - 1) as Step)}
              className="flex items-center gap-1 rounded-[4px] px-4 py-2 font-body-cjk text-sm font-medium text-muted-foreground transition-colors hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]"
            >
              <ArrowLeft className="w-4 h-4" />
              上一步
            </button>
          ) : (
            <div />
          )}

          <button
            onClick={handleNext}
            disabled={!canNext || saving}
            className={cn(
              'flex items-center gap-1 rounded-[4px] px-5 py-2 font-body-cjk text-sm font-semibold transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]',
              canNext
                ? 'bg-[var(--brand)] text-[var(--brand-foreground)] hover:bg-[color-mix(in_srgb,var(--brand)_86%,#171512)]'
                : 'bg-muted text-muted-foreground cursor-not-allowed'
            )}
          >
            {saving ? '保存中...' : step === 2 ? '完成' : '下一步'}
            {step < 2 ? <ArrowRight className="w-4 h-4" /> : <Check className="w-4 h-4" />}
          </button>
        </div>

        {/* Step indicator */}
        <p className="text-center font-mono text-xs text-muted-foreground mt-4">
          {step + 1} / 3
        </p>
      </div>
    </div>
  )
}
