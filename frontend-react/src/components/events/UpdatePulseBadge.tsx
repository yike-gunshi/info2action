/**
 * v15.0 UpdatePulseBadge — 有更新提示 (DESIGN.md §15.7)
 *
 * 显示条件：cluster.has_update === true（per-user，后端按 R9.3 边界判定）。
 * 不做：infinite pulse 呼吸、NEW 标签、左边框高亮（feedback_anti_fomo_design）。
 */
import { useEffect, useState } from 'react'

interface UpdatePulseBadgeProps {
  active: boolean
  className?: string
}

export function UpdatePulseBadge({ active, className = '' }: UpdatePulseBadgeProps) {
  // 首次 active 转 true 时做一次 scale 0→1 入场动画；之后保持
  const [animateIn, setAnimateIn] = useState(false)
  useEffect(() => {
    if (active) {
      setAnimateIn(true)
    } else {
      setAnimateIn(false)
    }
  }, [active])

  if (!active) return null

  return (
    <span
      role="img"
      aria-label="有更新"
      className={`inline-flex items-center gap-1 rounded-full border border-primary/20 bg-primary/10 px-1.5 py-0.5 align-middle text-[11px] font-medium leading-none text-primary ${className}`}
      style={{
        transform: animateIn ? 'scale(1)' : 'scale(0)',
        transition: 'transform 200ms ease-out',
      }}
    >
      <span
        aria-hidden="true"
        className="h-1.5 w-1.5 rounded-full"
        style={{ backgroundColor: 'var(--pulse-update)' }}
      />
      <span>有更新</span>
    </span>
  )
}
