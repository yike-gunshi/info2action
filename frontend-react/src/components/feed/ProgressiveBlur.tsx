/**
 * Progressive Blur — 变更8: 替代纯色渐变遮罩
 * 8 layers of increasing backdrop-filter blur.
 */
export function ProgressiveBlur({ height = 80 }: { height?: number }) {
  return (
    <div
      className="absolute bottom-0 left-0 right-0 pointer-events-none"
      style={{ height }}
    >
      {[...Array(8)].map((_, i) => (
        <div
          key={i}
          className="absolute left-0 right-0"
          style={{
            top: `${(i / 8) * 100}%`,
            height: `${(1 / 8) * 100}%`,
            backdropFilter: `blur(${(i + 1) * 1.5}px)`,
            WebkitBackdropFilter: `blur(${(i + 1) * 1.5}px)`,
            mask: 'linear-gradient(to bottom, transparent, black)',
            WebkitMask: 'linear-gradient(to bottom, transparent, black)',
          }}
        />
      ))}
    </div>
  )
}
