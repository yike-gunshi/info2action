import { Maximize2 } from 'lucide-react'
import { cn } from '../../lib/utils'
import { buildInfoItemHash, buildInfoItemHref } from '../../lib/itemDeepLink'

export type ExpandButtonVariant = 'card' | 'modal'

export interface ExpandButtonProps {
  itemId: string
  /** 用于 aria-label,可选 */
  title?: string
  /**
   * variant='card':浮在 InfoCard 封面右上,absolute 定位,opacity 分级(BF-0420-1 后已停用此场景)
   * variant='modal':嵌入 DetailPanel header,无 absolute,无 opacity 分级,始终高可见
   */
  variant?: ExpandButtonVariant
  /** 点击后触发的附加回调(如关闭弹窗) */
  onClicked?: () => void
  /** 允许外部覆盖类 */
  className?: string
}

function truncateForAria(title: string | undefined): string {
  if (!title) return ''
  const trimmed = title.trim()
  if (trimmed.length <= 30) return trimmed
  return trimmed.slice(0, 30) + '...'
}

/**
 * ExpandButton — 在新标签打开信息页 item 弹窗深链。
 *
 * 桌面非触控:尝试 window.open 开新 tab。
 * 触控设备 / window.open 被拦截:降级为 location.hash = 'item=...' 同 tab。
 * onClick stopPropagation + preventDefault。
 *
 * 两种 variant:
 *   card   — 浮在 InfoCard 封面上(已停用,见 BF-0420-1)
 *   modal  — 嵌入 DetailPanel header(v14.0 主要场景)
 */
export function ExpandButton({
  itemId,
  title,
  variant = 'modal',
  onClicked,
  className,
}: ExpandButtonProps) {
  const ariaTail = truncateForAria(title)
  const ariaLabel = ariaTail ? `放大查看「${ariaTail}」` : '放大查看'

  const handleClick = (e: React.MouseEvent) => {
    e.stopPropagation()
    e.preventDefault()

    const url = `${window.location.origin}${buildInfoItemHref(itemId)}`
    const isTouch =
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(pointer: coarse)').matches

    // BF-0420-2 二轮修复:Chrome/Firefox 的 noopener 模式下 window.open 返回 null,
    // 不能据此判断"失败"后走 fallback — 否则原 tab 的 hash 被误改(用户反馈)。
    // 正确策略:按设备类型分支,不依赖 open 返回值。
    if (isTouch) {
      // 触控设备(移动 Safari / 微信内置)→ 同 tab 跳转
      window.location.hash = buildInfoItemHash(itemId)
    } else {
      // 桌面 → 只调 open,不验证返回值。被弹窗拦截时由浏览器提示,不回退原 tab
      window.open(url, '_blank', 'noopener,noreferrer')
    }

    // 弹窗场景点开后通知父级关闭(避免原 tab 残留 modal)
    onClicked?.()
  }

  // 共享基础:图标颜色 + focus-visible
  const baseClasses =
    'inline-flex items-center justify-center ' +
    'text-muted-foreground hover:text-foreground ' +
    'focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent focus-visible:outline-offset-2'

  const variantClasses =
    variant === 'modal'
      ? // 对齐 DetailPanel header 其他按钮(关闭/收藏/原文):w-7 h-7 + rounded-md + hover:bg-muted,无 scale
        'w-7 h-7 rounded-md hover:bg-muted transition-colors'
      : // 卡片封面浮层(已停用,保留作为历史兼容)
        'absolute top-2 right-2 w-[28px] h-[28px] rounded-lg ' +
        'bg-white/90 backdrop-blur-sm border border-border ' +
        'hover:bg-white hover:border-neutral-300 ' +
        'opacity-50 group-hover:opacity-100 ' +
        '[@media(pointer:coarse)]:opacity-[0.85] ' +
        'transition-[transform,background-color,opacity,border-color,color] duration-150 ease-out ' +
        'hover:scale-105 active:scale-95'

  return (
    <button
      type="button"
      onClick={handleClick}
      aria-label={ariaLabel}
      className={cn(baseClasses, variantClasses, className)}
    >
      <Maximize2 size={14} aria-hidden="true" />
    </button>
  )
}

export default ExpandButton
