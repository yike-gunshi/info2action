import type { HTMLAttributes } from 'react'
import { cn } from '../../lib/utils'

type BrandWordmarkProps = HTMLAttributes<HTMLSpanElement>

export function BrandWordmark({ className, ...props }: BrandWordmarkProps) {
  return (
    <span
      aria-label="info2act"
      className={cn('brand-wordmark font-brand', className)}
      data-testid="brand-wordmark"
      {...props}
    >
      <span>info</span>
      <span className="brand-wordmark__two">2</span>
      <span>act</span>
    </span>
  )
}
