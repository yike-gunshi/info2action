import { type PropsWithChildren } from 'react'
import { cn } from '../../lib/utils'

interface BlurFadeProps {
  delay?: number
  className?: string
}

/**
 * Blur Fade: entry animation with opacity + translateY + blur.
 * Ported from Magic UI.
 */
export function BlurFade({
  children,
  delay = 0,
  className,
}: PropsWithChildren<BlurFadeProps>) {
  return (
    <div
      className={cn('animate-blur-fade', className)}
      style={{ animationDelay: `${delay}ms` }}
    >
      {children}
    </div>
  )
}
