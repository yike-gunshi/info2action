import { Zap, Loader2 } from 'lucide-react'

interface ActionBadgeProps {
  count: number
  isGenerating: boolean
}

/**
 * Header badge for action points — 变更11: 三态
 * - count > 0: emerald pill with count
 * - generating: amber spinner
 * - count === 0 && !generating: hidden
 */
export function ActionBadge({ count, isGenerating }: ActionBadgeProps) {
  if (isGenerating) {
    return (
      <span className="inline-flex items-center gap-1.5 text-sm font-semibold text-amber bg-amber-bg border border-amber-border rounded-full px-2 py-0.5 flex-shrink-0">
        <Loader2 className="w-2.5 h-2.5 animate-spin-fast" />
        生成中…
      </span>
    )
  }

  if (count > 0) {
    return (
      <span className="inline-flex items-center gap-1 text-sm font-semibold text-emerald bg-emerald-bg border border-emerald-border rounded-full px-2 py-0.5 flex-shrink-0">
        <Zap className="w-3 h-3" />
        <span className="font-bold">{count}</span> 个行动点
      </span>
    )
  }

  return null
}
