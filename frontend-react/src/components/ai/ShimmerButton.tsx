import { cn } from '../../lib/utils'
import type { ButtonHTMLAttributes, PropsWithChildren } from 'react'

/**
 * Shimmer sweep CTA button.
 * 変更25: "生成行动点" uses this.
 */
export function ShimmerButton({
  children,
  className,
  ...props
}: PropsWithChildren<ButtonHTMLAttributes<HTMLButtonElement>>) {
  return (
    <button
      className={cn(
        'shimmer-button bg-emerald text-white font-semibold px-6 py-2.5 rounded-lg',
        'hover:bg-emerald/90 transition-colors disabled:opacity-50',
        className,
      )}
      {...props}
    >
      {children}
    </button>
  )
}
