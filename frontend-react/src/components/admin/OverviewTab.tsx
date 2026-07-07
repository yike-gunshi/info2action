import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Loader2, RefreshCw } from 'lucide-react'
import { getAdminConsoleSummary } from '../../lib/api'
import type {
  AdminConsoleSummary,
  AdminHealthLevel,
  AdminHealthSignal,
  AdminTrendPoint,
} from '../../lib/api'
import { cn } from '../../lib/utils'
import { Sparkline } from './Sparkline'

type OverviewTabProps = {
  reloadSignal: number
  onOpenRuns: () => void
}

function fmtNum(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  return new Intl.NumberFormat('zh-CN').format(v)
}

function fmtCost(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  if (v > 0 && v < 0.0001) return '<¥0.0001'
  return `¥${v.toFixed(4)}`
}

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  } catch {
    return '—'
  }
}

const LEVEL_SYMBOL: Record<AdminHealthLevel, string> = { ok: '●', warn: '▲', crit: '✕', unknown: '?' }
const LEVEL_TEXT: Record<AdminHealthLevel, string> = { ok: '正常', warn: '注意', crit: '异常', unknown: '未知' }
const LEVEL_PILL: Record<AdminHealthLevel, string> = {
  ok: 'a-pill-ok',
  warn: 'a-pill-warn',
  crit: 'a-pill-crit',
  unknown: 'a-pill-unknown',
}

export function OverviewTab({ reloadSignal, onOpenRuns }: OverviewTabProps) {
  const [data, setData] = useState<AdminConsoleSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await getAdminConsoleSummary()
      setData(res)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load, reloadSignal])

  if (loading) return <OverviewSkeleton />

  if (error) {
    return (
      <ErrorPanel
        title="总览加载失败"
        detail={error}
        action={
          <button
            onClick={() => void load()}
            className="inline-flex h-8 items-center gap-1.5 rounded-[4px] border border-border px-2.5 font-body-cjk text-sm font-medium text-foreground transition-colors hover:border-muted-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            重试
          </button>
        }
      />
    )
  }

  if (!data || !data.available) {
    // 非 remote 模式 / 远程不可达 → 降级态（spec R2）
    const reason = data && 'reason' in data ? data.reason : 'remote_required'
    return (
      <ErrorPanel
        title="总览需连接远程数据源"
        detail={
          reason === 'remote_error'
            ? '远程数据库暂不可达，C 端指标无法读取。健康信号见下方（若有）。'
            : '当前实例未启用远程数据源（本地 SQLite / 自托管模式）。C 端指标与趋势仅在生产远程库可用。'
        }
        action={
          <button
            onClick={() => void load()}
            className="inline-flex h-8 items-center gap-1.5 rounded-[4px] border border-border px-2.5 font-body-cjk text-sm font-medium text-foreground transition-colors hover:border-muted-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            重试
          </button>
        }
      />
    )
  }

  const { c_metrics: m, interactions_detail: d, cost, health, trends, generated_at } = data
  const newUsers14dTotal = trends.new_users_14d.reduce(
    (acc, p) => (p.value !== null && Number.isFinite(p.value) ? acc + (p.value as number) : acc),
    0,
  )
  const lastRate = [...trends.fetch_success_rate_7d].reverse().find((p) => p.value !== null)?.value ?? null

  return (
    <div className="space-y-1">
      <SectionLabel note={`时区 Asia/Shanghai · 截至 ${fmtTime(generated_at)}`}>C 端指标</SectionLabel>
      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-2">
        <StatCard k="总用户数" v={fmtNum(m.total_users)}>
          <Sub>今日 <Delta n={m.new_users_today} /></Sub>
        </StatCard>
        <StatCard k="7 日新增" v={fmtNum(m.new_users_7d)}>
          <Sub>14 日累计 <span className="tabular-nums">{fmtNum(newUsers14dTotal)}</span></Sub>
        </StatCard>
        <StatCard k="7 日活跃" v={fmtNum(m.active_users_7d)} cap="口径：7 日内有读 / 点 / 藏互动">
          <Sub>1 日活跃 <span className="tabular-nums">{fmtNum(m.active_users_1d)}</span></Sub>
        </StatCard>
        <StatCard k="信息点击" v={fmtNum(m.info_click_users_7d)} unit="人" cap="口径：点过的人数，非点击次数">
          <Sub>
            <span className="tabular-nums">{fmtNum(m.info_click_items_7d)}</span> 条 · 累计{' '}
            <span className="tabular-nums">{fmtNum(m.info_click_items_total)}</span>
          </Sub>
        </StatCard>
        <StatCard k="精选点击" v={fmtNum(m.highlight_click_users_7d)} unit="人" cap="口径：点过的人数，非点击次数">
          <Sub>
            <span className="tabular-nums">{fmtNum(m.highlight_click_events_7d)}</span> 个 · 累计{' '}
            <span className="tabular-nums">{fmtNum(m.highlight_click_events_total)}</span>
          </Sub>
        </StatCard>
        <StatCard k="24h 成本" v={fmtCost(cost.embedding_cost_yuan_24h)} onClick={onOpenRuns}>
          <Sub>
            embedding <span className="tabular-nums">{fmtNum(cost.embedding_calls_24h)}</span> 次 →
          </Sub>
        </StatCard>
      </div>

      <SectionLabel>系统健康</SectionLabel>
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-5 gap-2">
        {health.signals.map((s) => (
          <HealthLight key={s.key} signal={s} onOpenRuns={onOpenRuns} />
        ))}
      </div>
      {health.incidents.length > 0 && (
        <div className="mt-2 space-y-2">
          {health.incidents.map((inc, i) => (
            <div
              key={i}
              className="flex items-baseline gap-2 rounded-md border border-border bg-card px-3 py-2 text-xs"
              style={{ borderLeft: `3px solid var(--a-${inc.severity === 'crit' ? 'crit' : 'warn'})` }}
            >
              <span className={cn('text-[11px] font-bold shrink-0', inc.severity === 'crit' ? 'a-text-crit' : 'a-text-warn')}>
                {inc.severity === 'crit' ? '✕ 异常' : '▲ 注意'}
              </span>
              <span className="text-foreground min-w-0">{inc.text}</span>
              {inc.link === 'runs' && (
                <button onClick={onOpenRuns} className="ml-auto shrink-0 text-primary hover:underline whitespace-nowrap">
                  查看抓取运行 →
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      <SectionLabel>趋势</SectionLabel>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
        <TrendPanel
          title="新增用户 · 14 日"
          current={fmtNum(trends.new_users_14d[trends.new_users_14d.length - 1]?.value ?? null)}
          points={trends.new_users_14d}
          variant="bar"
        />
        <TrendPanel
          title="抓取成功率 · 7 日"
          current={lastRate === null ? '—' : `${Math.round(lastRate * 100)}%`}
          points={trends.fetch_success_rate_7d.map((p) => ({ ...p }))}
          variant="line"
          fmtRange={(p) => (p.value === null ? '—' : `${Math.round((p.value as number) * 100)}%`)}
        />
      </div>

      <SectionLabel note="状态位口径 · 二期埋点后升级为事件流">互动明细</SectionLabel>
      <div className="rounded-md border border-border bg-card max-w-[560px] overflow-hidden">
        <table className="w-full text-xs">
          <tbody>
            <MiniRow label="收藏过内容的用户">
              <span className="tabular-nums">{fmtNum(d.starred_users)}</span> 人 · 收藏{' '}
              <span className="tabular-nums">{fmtNum(d.starred_total)}</span> 条
            </MiniRow>
            <MiniRow label="读过信息的用户（7 日）">
              <span className="tabular-nums">{fmtNum(d.read_users_7d)}</span> 人 ·{' '}
              <span className="tabular-nums">{fmtNum(d.read_items_7d)}</span> 条
            </MiniRow>
            <MiniRow label="最近注册">
              {d.latest_signup
                ? `${d.latest_signup.username} · ${new Date(d.latest_signup.created_at).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })}`
                : '—'}
            </MiniRow>
          </tbody>
        </table>
      </div>
    </div>
  )
}

function SectionLabel({ children, note }: { children: React.ReactNode; note?: string }) {
  return (
    <div className="mt-6 mb-2 first:mt-0 flex items-baseline gap-2">
      <span className="text-[11px] font-semibold tracking-[0.09em] uppercase text-muted-foreground">{children}</span>
      {note && <span className="text-[11px] text-muted-foreground/80">{note}</span>}
    </div>
  )
}

function StatCard({
  k,
  v,
  unit,
  cap,
  children,
  onClick,
}: {
  k: string
  v: string
  unit?: string
  cap?: string
  children?: React.ReactNode
  onClick?: () => void
}) {
  const clickable = typeof onClick === 'function'
  return (
    <div
      onClick={onClick}
      className={cn(
        'flex flex-col gap-0.5 rounded-md border border-border bg-card px-3 py-3 min-w-0',
        clickable && 'cursor-pointer hover:border-primary transition-colors',
      )}
    >
      <span className="text-[11px] font-semibold tracking-[0.05em] uppercase text-muted-foreground whitespace-nowrap">{k}</span>
      <span className="font-mono tabular-nums text-[26px] font-semibold leading-tight tracking-tight text-foreground">
        {v}
        {unit && <span className="text-[13px] font-medium text-muted-foreground ml-0.5">{unit}</span>}
      </span>
      {children}
      {cap && (
        <span className="mt-1.5 pt-1.5 border-t border-dashed border-border text-[11px] text-muted-foreground">
          {cap}
        </span>
      )}
    </div>
  )
}

function Sub({ children }: { children: React.ReactNode }) {
  return <span className="text-[11px] text-muted-foreground">{children}</span>
}

function Delta({ n }: { n: number | null }) {
  if (n === null || !Number.isFinite(n)) return <span className="text-muted-foreground">—</span>
  if (n > 0) return <span className="a-text-ok font-semibold tabular-nums">+{n}</span>
  return <span className="text-muted-foreground tabular-nums">+0</span>
}

function HealthLight({ signal, onOpenRuns }: { signal: AdminHealthSignal; onOpenRuns: () => void }) {
  const clickable = signal.link === 'runs'
  return (
    <div
      onClick={clickable ? onOpenRuns : undefined}
      className={cn(
        'flex flex-col gap-1 rounded-md border border-border bg-card px-3 py-2.5',
        clickable && 'cursor-pointer hover:border-muted-foreground transition-colors',
      )}
    >
      <div className="flex items-center gap-2">
        <span className="text-xs font-semibold text-foreground">{signal.label}</span>
        <span className={cn('a-pill ml-auto', LEVEL_PILL[signal.level])}>
          {LEVEL_SYMBOL[signal.level]} {LEVEL_TEXT[signal.level]}
        </span>
      </div>
      <span className="text-[11px] text-muted-foreground">{signal.detail}</span>
    </div>
  )
}

function TrendPanel({
  title,
  current,
  points,
  variant,
  fmtRange,
}: {
  title: string
  current: string
  points: AdminTrendPoint[]
  variant: 'bar' | 'line'
  fmtRange?: (p: AdminTrendPoint) => string
}) {
  const valid = points.filter((p) => p.value !== null)
  const first = valid[0]
  const last = valid[valid.length - 1]
  const rangeStr = (p?: AdminTrendPoint) => {
    if (!p) return ''
    const val = fmtRange ? ` · ${fmtRange(p)}` : ''
    return `${p.date.slice(5)}${val}`
  }
  return (
    <div className="rounded-md border border-border bg-card px-3 py-3">
      <div className="flex items-baseline gap-2 mb-1.5">
        <span className="text-[12px] font-semibold text-foreground">{title}</span>
        <span className="ml-auto font-mono tabular-nums text-[15px] font-semibold text-foreground">{current}</span>
      </div>
      <Sparkline points={points} variant={variant} height={48} ariaLabel={title} />
      <div className="flex justify-between text-[11px] text-muted-foreground mt-1">
        <span>{rangeStr(first)}</span>
        <span>{rangeStr(last)}</span>
      </div>
    </div>
  )
}

function MiniRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <tr className="border-t border-border first:border-t-0">
      <td className="px-3 py-2 text-foreground">{label}</td>
      <td className="px-3 py-2 text-right text-muted-foreground">{children}</td>
    </tr>
  )
}

function ErrorPanel({ title, detail, action }: { title: string; detail: string; action?: React.ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-md border border-border bg-card px-6 py-16 text-center">
      <AlertTriangle className="w-6 h-6 text-muted-foreground" />
      <div>
        <p className="text-[14px] font-semibold text-foreground">{title}</p>
        <p className="mt-1 text-xs text-muted-foreground max-w-[420px]">{detail}</p>
      </div>
      {action}
    </div>
  )
}

function OverviewSkeleton() {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-2">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="rounded-md border border-border bg-card px-3 py-3">
            <div className="h-3 w-16 rounded bg-muted animate-pulse" />
            <div className="mt-2 h-6 w-12 rounded bg-muted animate-pulse" />
          </div>
        ))}
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-5 gap-2">
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className="rounded-md border border-border bg-card px-3 py-2.5">
            <div className="h-3.5 w-full rounded bg-muted animate-pulse" />
            <div className="mt-2 h-3 w-2/3 rounded bg-muted animate-pulse" />
          </div>
        ))}
      </div>
      <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
        <Loader2 className="w-3.5 h-3.5 animate-spin" /> 加载总览…
      </div>
    </div>
  )
}
