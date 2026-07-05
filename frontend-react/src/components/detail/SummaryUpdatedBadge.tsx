/**
 * v12.2 F50: AI 摘要刚被 ASR 链路刷新后的 pill 提示.
 * 4s 总时长 (200ms 进 + 3.6s 保 + 200ms 出), 过期后自动 unmount.
 * 设计规范: docs/DESIGN.md 模块 13.5
 */
import React, { useEffect, useState } from 'react'

interface Props {
  onExpired: () => void
}

export function SummaryUpdatedBadge({ onExpired }: Props): React.ReactElement | null {
  const [phase, setPhase] = useState<'enter' | 'stable' | 'exit'>('enter')

  useEffect(() => {
    // 200ms 后进入 stable
    const t1 = setTimeout(() => setPhase('stable'), 200)
    // 3800ms 开始 fade out
    const t2 = setTimeout(() => setPhase('exit'), 3800)
    // 4000ms 完全消失通知父组件
    const t3 = setTimeout(onExpired, 4000)
    return () => {
      clearTimeout(t1)
      clearTimeout(t2)
      clearTimeout(t3)
    }
  }, [onExpired])

  const opacity = phase === 'enter' ? 0 : phase === 'stable' ? 1 : 0
  const translateY = phase === 'enter' ? '-2px' : '0'

  return (
    <div
      role="status"
      aria-live="polite"
      className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs bg-primary/10 text-primary border border-primary/20 mb-2"
      style={{
        opacity,
        transform: `translateY(${translateY})`,
        transition: 'opacity 200ms ease-out, transform 200ms ease-out',
      }}
    >
      ✨ 摘要已基于视频转写更新
    </div>
  )
}
