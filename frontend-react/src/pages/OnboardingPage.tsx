import { useState, useCallback } from 'react'
import { ArrowRight, ArrowLeft, Check } from 'lucide-react'
import { cn } from '../lib/utils'
import { updateUserProfile } from '../lib/api'
import { useAuthStore } from '../store/authStore'
import { ROLES, INTERESTS, TOOLS } from '../lib/profileOptions'

type Step = 0 | 1 | 2

interface Props {
  onComplete: () => void
}

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

  return (
    <div className="min-h-screen bg-background flex flex-col items-center justify-center p-4">
      <div className="w-full max-w-[520px]">
        {/* Progress */}
        <div className="flex items-center gap-2 mb-8">
          {[0, 1, 2].map((i) => (
            <div
              key={i}
              className={cn(
                'h-1.5 flex-1 rounded-full transition-colors',
                i <= step ? 'bg-primary' : 'bg-muted'
              )}
            />
          ))}
        </div>

        {/* Step 0: Role */}
        {step === 0 && (
          <div>
            <h2 className="text-xl font-bold text-foreground mb-1">你的角色</h2>
            <p className="text-sm text-muted-foreground mb-6">选择最贴近你的角色，帮助我们理解你的信息需求</p>
            <div className="grid grid-cols-2 gap-2">
              {ROLES.map((r) => (
                <button
                  key={r.id}
                  onClick={() => setRole(r.id)}
                  className={cn(
                    'p-3 rounded-lg border text-left transition-all',
                    role === r.id
                      ? 'border-primary bg-primary/5 ring-1 ring-primary'
                      : 'border-border hover:border-primary/50'
                  )}
                >
                  <div className="text-sm font-medium text-foreground">{r.label}</div>
                  {r.desc && <div className="text-xs text-muted-foreground mt-0.5">{r.desc}</div>}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Step 1: Interests */}
        {step === 1 && (
          <div>
            <h2 className="text-xl font-bold text-foreground mb-1">关注方向</h2>
            <p className="text-sm text-muted-foreground mb-6">选择你感兴趣的方向（至少 1 个）</p>
            <div className="flex flex-wrap gap-2">
              {INTERESTS.map((i) => (
                <button
                  key={i.id}
                  onClick={() => toggleSet(interests, setInterests, i.id)}
                  className={cn(
                    'px-3 py-1.5 rounded-full text-sm font-medium border transition-all',
                    interests.has(i.id)
                      ? 'border-primary bg-primary/10 text-primary'
                      : 'border-border text-muted-foreground hover:border-primary/50'
                  )}
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
            <h2 className="text-xl font-bold text-foreground mb-1">常用工具</h2>
            <p className="text-sm text-muted-foreground mb-6">选择你日常使用的 AI 工具（可跳过）</p>
            <div className="flex flex-wrap gap-2">
              {TOOLS.map((t) => (
                <button
                  key={t.id}
                  onClick={() => toggleSet(tools, setTools, t.id)}
                  className={cn(
                    'px-3 py-1.5 rounded-full text-sm font-medium border transition-all',
                    tools.has(t.id)
                      ? 'border-primary bg-primary/10 text-primary'
                      : 'border-border text-muted-foreground hover:border-primary/50'
                  )}
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
              className="flex items-center gap-1 px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors"
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
              'flex items-center gap-1 px-5 py-2 text-sm font-medium rounded-lg transition-colors',
              canNext
                ? 'bg-primary text-primary-foreground hover:bg-primary/90'
                : 'bg-muted text-muted-foreground cursor-not-allowed'
            )}
          >
            {saving ? '保存中...' : step === 2 ? '完成' : '下一步'}
            {step < 2 ? <ArrowRight className="w-4 h-4" /> : <Check className="w-4 h-4" />}
          </button>
        </div>

        {/* Step indicator */}
        <p className="text-center text-xs text-muted-foreground mt-4">
          {step + 1} / 3
        </p>
      </div>
    </div>
  )
}
