import type { AdminTrendPoint } from '../../lib/api'

type SparklineProps = {
  points: AdminTrendPoint[]
  variant: 'bar' | 'line'
  height?: number
  className?: string
  /** 折线 y 轴的显示域；不传则按数据 min/max 归一化 */
  ariaLabel?: string
}

const INSUFFICIENT = '数据不足'

/**
 * 手写 SVG/CSS sparkline，零图表库依赖（DESIGN 模块 20.4）。
 * - bar：计数序列，0 值画低对比短柱不留空，非 0 用青翠 accent。
 * - line：比率序列，2px 青翠折线 + 面积填充 + 终点强调；null 断点不连线。
 * 有效点 < 2 时显示「数据不足」占位，不画图、不除零崩溃。
 */
export function Sparkline({ points, variant, height = 48, className, ariaLabel }: SparklineProps) {
  const valid = points.filter((p) => p.value !== null && Number.isFinite(p.value as number))
  if (valid.length < 2) {
    return (
      <div
        className={className}
        style={{ height, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
      >
        <span className="text-[11px] text-muted-foreground">{INSUFFICIENT}</span>
      </div>
    )
  }

  if (variant === 'bar') {
    return <BarSpark points={points} height={height} className={className} ariaLabel={ariaLabel} />
  }
  return <LineSpark points={points} height={height} className={className} ariaLabel={ariaLabel} />
}

type InnerSparkProps = {
  points: AdminTrendPoint[]
  height: number
  className?: string
  ariaLabel?: string
}

function BarSpark({ points, height, className, ariaLabel }: InnerSparkProps) {
  const values = points.map((p) => (p.value === null || !Number.isFinite(p.value) ? 0 : (p.value as number)))
  const max = Math.max(1, ...values)
  return (
    <div
      role="img"
      aria-label={ariaLabel}
      className={className}
      style={{ display: 'flex', alignItems: 'flex-end', gap: 3, height }}
    >
      {points.map((p, i) => {
        const v = values[i]
        const isZero = v <= 0
        const h = isZero ? 4 : Math.max(6, Math.round((v / max) * (height - 2)))
        return (
          <div
            key={p.date + i}
            title={`${p.date}: ${p.value ?? '—'}`}
            style={{
              flex: 1,
              minWidth: 2,
              height: h,
              borderRadius: '2px 2px 0 0',
              background: isZero ? 'var(--border)' : 'var(--primary)',
              opacity: isZero ? 1 : 0.9,
            }}
          />
        )
      })}
    </div>
  )
}

function LineSpark({ points, height, className, ariaLabel }: InnerSparkProps) {
  const W = 100
  const pad = 3
  const nums = points.map((p) => (p.value === null || !Number.isFinite(p.value) ? null : (p.value as number)))
  const present = nums.filter((n): n is number => n !== null)
  const lo = Math.min(...present)
  const hi = Math.max(...present)
  const span = hi - lo

  const xy = (i: number, v: number) => {
    const x = (i / (points.length - 1)) * W
    // 同值序列画水平中线（不除零）
    const t = span === 0 ? 0.5 : (v - lo) / span
    const y = height - pad - t * (height - pad * 2)
    return [x, y] as const
  }

  // 断点分段
  const segments: string[] = []
  let cur: string[] = []
  nums.forEach((n, i) => {
    if (n === null) {
      if (cur.length) segments.push(cur.join(' '))
      cur = []
      return
    }
    const [x, y] = xy(i, n)
    cur.push(`${cur.length === 0 ? 'M' : 'L'}${x.toFixed(2)},${y.toFixed(2)}`)
  })
  if (cur.length) segments.push(cur.join(' '))

  // 面积填充（首个连续段）
  const firstIdx = nums.findIndex((n) => n !== null)
  const lastIdx = nums.length - 1 - [...nums].reverse().findIndex((n) => n !== null)
  const [fx] = xy(firstIdx, nums[firstIdx] as number)
  const [lx, ly] = xy(lastIdx, nums[lastIdx] as number)
  const areaPath = `${segments[0]} L${lx.toFixed(2)},${(height - pad).toFixed(2)} L${fx.toFixed(2)},${(
    height - pad
  ).toFixed(2)} Z`

  return (
    <svg
      role="img"
      aria-label={ariaLabel}
      className={className}
      viewBox={`0 0 ${W} ${height}`}
      preserveAspectRatio="none"
      style={{ display: 'block', width: '100%', height, overflow: 'visible' }}
    >
      {segments.length === 1 && (
        <path d={areaPath} fill="var(--primary)" opacity={0.08} />
      )}
      {segments.map((d, i) => (
        <path
          key={i}
          d={d}
          fill="none"
          stroke="var(--primary)"
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
      ))}
      <circle cx={lx} cy={ly} r={2.6} fill="var(--primary)" vectorEffect="non-scaling-stroke" />
    </svg>
  )
}
