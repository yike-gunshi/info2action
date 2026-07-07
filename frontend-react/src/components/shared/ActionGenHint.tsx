/**
 * v21.0 action-revival (B2/B3): 生成表单顶部的画像强提示 + 今日额度显示。
 * 在 ActionZone / ClusterActionZone 的表单态复用。
 */
import { useEffect, useState } from 'react'
import { AlertTriangle } from 'lucide-react'
import { toast } from 'sonner'
import { fetchActionQuota, getUserProfile, type ActionQuota } from '../../lib/api'
import { ACTION_PROFILE_PROMPT } from '../../lib/actionProfilePrompt'
import { useAuthStore } from '../../store/authStore'

export function ActionGenHint() {
  const user = useAuthStore((s) => s.user)
  const [quota, setQuota] = useState<ActionQuota | null>(null)
  const [hasManifest, setHasManifest] = useState<boolean | null>(null)

  useEffect(() => {
    if (!user) return
    let cancelled = false
    fetchActionQuota().then((q) => { if (!cancelled) setQuota(q) }).catch(() => {})
    getUserProfile()
      .then((res) => { if (!cancelled) setHasManifest(!!res.profile?.manifest) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [user])

  const copyPrompt = async () => {
    try {
      await navigator.clipboard.writeText(ACTION_PROFILE_PROMPT)
      toast.success('画像 Prompt 已复制,交给你的 AI 执行后到设置页粘贴')
    } catch {
      toast.error('复制失败,请到设置页手动复制')
    }
  }

  if (!user) return null

  return (
    <div className="mb-3 space-y-2">
      {hasManifest === false && (
        <div
          data-testid="gen-no-profile-hint"
          className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-[12px] leading-relaxed text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300"
        >
          <div className="flex items-start gap-1.5">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>
              你还没有配置个人画像,生成结果会偏通用。
              <button type="button" onClick={copyPrompt} className="mx-1 font-semibold underline underline-offset-2">复制画像 Prompt</button>
              <a href="#settings" className="font-semibold underline underline-offset-2">去设置页粘贴 →</a>
            </span>
          </div>
        </div>
      )}
      {quota && !quota.unlimited && (
        <div data-testid="gen-quota" className="text-right text-[11px] text-muted-foreground">
          今日剩余 {quota.remaining}/{quota.limit}
        </div>
      )}
    </div>
  )
}
