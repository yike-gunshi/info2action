import { useRef, useCallback, type PropsWithChildren } from 'react'
import { cn } from '../../lib/utils'

interface MagicCardProps {
  className?: string
  glowColor?: string
  style?: React.CSSProperties
}

/**
 * Magic Card: mouse-following radial gradient glow on hover.
 * Ported from Magic UI — pure CSS + mousemove.
 */
export function MagicCard({
  children,
  className,
  glowColor = 'rgba(79, 82, 228, 0.06)',
  style,
}: PropsWithChildren<MagicCardProps>) {
  const ref = useRef<HTMLDivElement>(null)

  const handleMouseMove = useCallback(
    (e: React.MouseEvent) => {
      if (!ref.current) return
      const rect = ref.current.getBoundingClientRect()
      ref.current.style.setProperty('--mouse-x', `${e.clientX - rect.left}px`)
      ref.current.style.setProperty('--mouse-y', `${e.clientY - rect.top}px`)
    },
    [],
  )

  const handleMouseLeave = useCallback(() => {
    if (!ref.current) return
    ref.current.style.removeProperty('--mouse-x')
    ref.current.style.removeProperty('--mouse-y')
  }, [])

  return (
    <div
      ref={ref}
      onMouseMove={handleMouseMove}
      onMouseLeave={handleMouseLeave}
      className={cn('group relative overflow-hidden', className)}
      style={style}
    >
      {/* Glow layer */}
      <div
        className="pointer-events-none absolute inset-0 rounded-[inherit] opacity-0 group-hover:opacity-100 transition-opacity duration-300"
        style={{
          background: `radial-gradient(300px circle at var(--mouse-x, 50%) var(--mouse-y, 50%), ${glowColor}, transparent 60%)`,
        }}
      />
      {children}
    </div>
  )
}
