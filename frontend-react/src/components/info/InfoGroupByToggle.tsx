/**
 * v18.0 Spec-2.5 rev2（2026-05-15 用户验收 rev1 后反馈触发）：
 * 信息 tab 内「来源 / 类型」视角切换。
 *
 * rev7（2026-05-21 L2 导航视觉收口）：作为 L2 导航行左侧静默文字切换。
 * 切换「类型 / 来源」后，同一行右侧 pill 铺设为对应分组。
 *   - disabled：`disabled:opacity-50 disabled:cursor-not-allowed`（PRD §Spec-2.5.E1）
 *
 * 用 button + aria-pressed 表达"toggle button"含义。
 *
 * 受控组件：父组件 InfoView 持有 groupBy 状态 + localStorage 同步逻辑。
 */
import { cn } from '../../lib/utils'

export type InfoGroupBy = 'platform' | 'category'

export interface InfoGroupByToggleProps {
  /** 当前选中的视角 */
  groupBy: InfoGroupBy
  /** 切换回调 */
  onChange: (next: InfoGroupBy) => void
  /** 切换中（数据请求中）禁用按钮防重复点击（Spec-2.5.E1） */
  disabled?: boolean
  /** 可选 className 注入（用于父级布局调整） */
  className?: string
}

const OPTIONS: Array<{ value: InfoGroupBy; label: string }> = [
  { value: 'category', label: '类型' },
  { value: 'platform', label: '来源' },
]

export function InfoGroupByToggle({
  groupBy,
  onChange,
  disabled = false,
  className,
}: InfoGroupByToggleProps) {
  return (
    <div
      aria-label="信息视角切换"
      className={cn(
        'inline-flex h-10 min-w-max items-center gap-6 sm:gap-8',
        className,
      )}
    >
      {OPTIONS.map((opt) => {
        const selected = opt.value === groupBy
        return (
          <button
            key={opt.value}
            type="button"
            aria-pressed={selected}
            disabled={disabled}
            onClick={() => {
              if (disabled || selected) return
              onChange(opt.value)
            }}
            data-groupby={opt.value}
            className={cn(
              'h-full shrink-0 border-b-2 px-0.5 font-event-title text-[16px] font-medium tracking-normal',
              'cursor-pointer transition-colors duration-150',
              'focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background',
              selected
                ? 'border-[var(--brand)] text-[var(--brand)]'
                : 'border-transparent text-muted-foreground hover:text-foreground',
              'disabled:opacity-50 disabled:cursor-not-allowed',
            )}
          >
            {opt.label}
          </button>
        )
      })}
    </div>
  )
}
