import { Fragment, useEffect, useRef, useState } from 'react'
import type { ReactNode, UIEvent } from 'react'
import { Activity, ArrowLeft, ChevronRight, Clock3, Copy, Database, Loader2, Plus, Trash2 } from 'lucide-react'
import { toast } from 'sonner'

import { cn } from '../lib/utils'
import { buildInfoItemHash } from '../lib/itemDeepLink'
import {
  createInviteCodes,
  deleteInviteCode,
  getAdminOverview,
  getEmbeddingUsage,
  getFetchRun,
  getFetchRunItems,
  getFetchRuns,
  getInviteCodes,
  type AdminUser,
  type EmbeddingUsageResponse,
  type FetchRunDistribution,
  type FetchRunItem,
  type FetchRunSummary,
  type InviteCode,
} from '../lib/api'

const RUN_PAGE_SIZE = 50
const INVITE_COUNT_PRESETS = [
  { label: '10 个一次性码', count: 10, maxUses: 1 },
  { label: '50 个一次性码', count: 50, maxUses: 1 },
  { label: '1 个 100 次场景码', count: 1, maxUses: 100 },
]

type InviteCodeStatus = {
  kind: 'unused' | 'available' | 'exhausted' | 'expired'
  label: string
  cls: string
}

function mergeRunDetail(runs: FetchRunSummary[], detail: FetchRunSummary) {
  let didReplace = false
  const merged = runs.map((run) => {
    if (run.id !== detail.id) return run
    didReplace = true
    return detail
  })
  return didReplace ? merged : [detail, ...runs]
}

function appendRuns(runs: FetchRunSummary[], nextRuns: FetchRunSummary[]) {
  const seen = new Set(runs.map((run) => run.id))
  return [...runs, ...nextRuns.filter((run) => !seen.has(run.id))]
}

function getRunNewItems(run: FetchRunSummary) {
  return run.audit?.new_items_count ?? run.total_new_items
}

function getRunAiLabel(run: FetchRunSummary) {
  const ai = run.audit?.ai_summary
  const newItems = getRunNewItems(run)
  if (!ai || ai.summarized === null || ai.summarized === undefined || newItems === null || newItems === undefined) {
    return '—'
  }
  return `${ai.summarized}/${newItems}`
}

function getRunPublishedEvents(run: FetchRunSummary) {
  return run.audit?.event_cluster?.published_clusters
}

function isInviteExpired(c: InviteCode) {
  if (!c.expires_at) return false
  const expiresAt = new Date(c.expires_at).getTime()
  return Number.isFinite(expiresAt) && expiresAt < Date.now()
}

function isUnusedActiveInvite(c: InviteCode) {
  return c.used_count === 0 && !isInviteExpired(c)
}

async function writeClipboardText(text: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.left = '-9999px'
  document.body.appendChild(textarea)
  textarea.select()
  const copied = document.execCommand('copy')
  document.body.removeChild(textarea)
  if (!copied) throw new Error('copy failed')
}

export function AdminPage() {
  const [codes, setCodes] = useState<InviteCode[]>([])
  const [users, setUsers] = useState<AdminUser[]>([])
  const [runs, setRuns] = useState<FetchRunSummary[]>([])
  const [selectedRun, setSelectedRun] = useState<FetchRunSummary | null>(null)
  const [embeddingUsage, setEmbeddingUsage] = useState<EmbeddingUsageResponse | null>(null)
  const [drilldown, setDrilldown] = useState<FetchRunDistribution | null>(null)
  const [items, setItems] = useState<FetchRunItem[]>([])
  const [loading, setLoading] = useState(true)
  const [generating, setGenerating] = useState(false)
  const [inviteCount, setInviteCount] = useState(1)
  const [inviteMaxUses, setInviteMaxUses] = useState(1)
  const [runLoading, setRunLoading] = useState(false)
  const [itemsLoading, setItemsLoading] = useState(false)
  const [runsHasMore, setRunsHasMore] = useState(true)
  const [runsLoadingMore, setRunsLoadingMore] = useState(false)
  const runsLoadingMoreRef = useRef(false)

  useEffect(() => {
    getAdminOverview()
      .then((overview) => {
        setCodes(overview.codes)
        setUsers(overview.users)
        setRuns(overview.fetch_runs.runs)
        setRunsHasMore(overview.fetch_runs.runs.length >= overview.fetch_runs.limit)
        setEmbeddingUsage(overview.embedding_usage)

        const firstRun = overview.fetch_runs.runs[0] ?? null
        setSelectedRun(firstRun)
        if (firstRun) {
          setRunLoading(true)
          getFetchRun(firstRun.id)
            .then((res) => {
              setSelectedRun(res.run)
              setRuns((prev) => mergeRunDetail(prev, res.run))
            })
            .catch((err) => toast.error(err instanceof Error ? err.message : '加载抓取记录失败'))
            .finally(() => setRunLoading(false))
        }

        getEmbeddingUsage({ hours: 24, limit: 50 })
          .then((usage) => setEmbeddingUsage(usage))
          .catch((err) => toast.error(err instanceof Error ? err.message : '加载 Embedding 用量失败'))
      })
      .catch((err) => toast.error(err.message))
      .finally(() => setLoading(false))
  }, [])

  async function handleSelectRun(runId: number) {
    const localRun = runs.find((run) => run.id === runId)
    if (localRun) setSelectedRun(localRun)
    setRunLoading(true)
    setDrilldown(null)
    setItems([])
    try {
      const res = await getFetchRun(runId)
      setSelectedRun(res.run)
      setRuns((prev) => mergeRunDetail(prev, res.run))
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '加载抓取记录失败')
    } finally {
      setRunLoading(false)
    }
  }

  async function loadMoreRuns() {
    if (runsLoadingMoreRef.current || !runsHasMore) return
    runsLoadingMoreRef.current = true
    setRunsLoadingMore(true)
    try {
      const res = await getFetchRuns({ limit: RUN_PAGE_SIZE, offset: runs.length })
      setRuns((prev) => appendRuns(prev, res.runs))
      setRunsHasMore(res.runs.length >= res.limit)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '加载历史抓取记录失败')
    } finally {
      runsLoadingMoreRef.current = false
      setRunsLoadingMore(false)
    }
  }

  function handleRunLedgerScroll(event: UIEvent<HTMLDivElement>) {
    const el = event.currentTarget
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 160) {
      void loadMoreRuns()
    }
  }

  async function handleSourceDrilldown(row: FetchRunDistribution) {
    if (!selectedRun || !row.platform || !row.source) return
    if (drilldown?.platform === row.platform && drilldown?.source === row.source && !itemsLoading) {
      setDrilldown(null)
      setItems([])
      return
    }
    setDrilldown(row)
    setItemsLoading(true)
    setItems([])
    try {
      const res = await getFetchRunItems(selectedRun.id, {
        platform: row.platform,
        source: row.source,
        limit: 50,
      })
      setItems(res.items)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '加载标题列表失败')
    } finally {
      setItemsLoading(false)
    }
  }

  async function handleGenerate() {
    setGenerating(true)
    try {
      const res = await createInviteCodes(inviteCount, inviteMaxUses)
      toast.success(res.codes.length === 1 ? `生成邀请码: ${res.codes[0]}` : `已生成 ${res.codes.length} 个邀请码`)
      const c = await getInviteCodes()
      setCodes(c.codes)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '生成失败')
    } finally {
      setGenerating(false)
    }
  }

  async function handleDelete(code: string) {
    try {
      await deleteInviteCode(code)
      setCodes((prev) => prev.filter((c) => c.code !== code))
      toast.success('已删除')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '删除失败')
    }
  }

  async function copyCode(code: string) {
    try {
      await writeClipboardText(code)
      toast.success('已复制')
    } catch {
      toast.error('复制失败')
    }
  }

  async function copyUnusedCodes() {
    const unusedCodes = codes.filter(isUnusedActiveInvite).map((c) => c.code)
    if (unusedCodes.length === 0) {
      toast.error('没有未使用的邀请码')
      return
    }
    try {
      await writeClipboardText(unusedCodes.join('\n'))
      toast.success(`已复制 ${unusedCodes.length} 个未使用邀请码`)
    } catch {
      toast.error('复制失败')
    }
  }

  function codeStatus(c: InviteCode): InviteCodeStatus {
    if (isInviteExpired(c)) return { kind: 'expired', label: '已过期', cls: 'bg-destructive/10 text-destructive' }
    if (c.used_count >= c.max_uses) return { kind: 'exhausted', label: '已用完', cls: 'bg-muted text-muted-foreground' }
    if (c.used_count === 0) return { kind: 'unused', label: '未使用', cls: 'bg-accent text-accent-foreground' }
    return { kind: 'available', label: '可用', cls: 'bg-emerald-50 text-emerald-700' }
  }

  function formatDate(value?: string | null) {
    if (!value) return '—'
    return new Date(value).toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
  }

  function formatRunDate(value?: string | null) {
    return value ? formatDate(value) : '进行中'
  }

  function formatSeconds(value?: number | null) {
    if (value === null || value === undefined) return '—'
    if (value < 60) return `${value.toFixed(value < 10 ? 1 : 0)}s`
    const totalSeconds = Math.round(value)
    return `${Math.floor(totalSeconds / 60)}m ${totalSeconds % 60}s`
  }

  function formatNumber(value?: number | null) {
    if (value === null || value === undefined) return '—'
    return new Intl.NumberFormat('zh-CN').format(value)
  }

  function formatCost(value?: number | null) {
    const n = value ?? 0
    if (n > 0 && n < 0.0001) return '<¥0.0001'
    return `¥${n.toFixed(4)}`
  }

  function statusLabel(status: string) {
    if (status === 'done') return '完成'
    if (status === 'error') return '失败'
    if (status === 'running') return '运行中'
    return status
  }

  function statusClass(status: string) {
    if (status === 'done') return 'bg-accent text-accent-foreground'
    if (status === 'error') return 'bg-destructive/10 text-destructive'
    return 'bg-muted text-muted-foreground'
  }

  function itemStatusClass(status?: string | null) {
    if (status === 'failed' || status === 'error') return 'bg-destructive/10 text-destructive'
    if (status === 'summarized' || status === 'clustered') return 'bg-accent text-accent-foreground'
    return 'bg-muted text-muted-foreground'
  }

  const audit = selectedRun?.audit
  const ai = audit?.ai_summary
  const eventCluster = audit?.event_cluster
  const platformSourceRows = audit?.platform_source_counts ?? []
  const pillRows = audit?.pill_counts ?? []
  const stageRows = Object.entries(audit?.stage_durations_sec ?? {})
  const embSummary = embeddingUsage?.summary
  const embSourceRows = embeddingUsage?.by_source ?? []
  const embLogs = embeddingUsage?.logs ?? []
  const selectedRunNewItems = selectedRun ? getRunNewItems(selectedRun) : null
  const selectedRunAiLabel = selectedRun ? getRunAiLabel(selectedRun) : '—'
  const selectedRunPublishedEvents = selectedRun ? getRunPublishedEvents(selectedRun) : null

  if (loading) {
    return <AdminSkeleton />
  }

  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-50 flex items-center gap-3 px-4 h-14 bg-card border-b border-border">
        <a
          href="#"
          className="p-2 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
          aria-label="返回"
        >
          <ArrowLeft className="w-4 h-4" />
        </a>
        <h1 className="text-base font-semibold text-foreground">管理面板</h1>
        <div className="ml-auto hidden sm:flex items-center gap-2 text-[12px] text-muted-foreground">
          <span className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-2 py-1">
            <Activity className="w-3.5 h-3.5" />
            抓取观测台
          </span>
          <span>已加载 {runs.length} 次</span>
        </div>
      </header>

      <main className="max-w-[1280px] mx-auto px-4 py-8 space-y-6">
        <section className="bg-card border border-border rounded-lg overflow-hidden shadow-subtle">
          <div className="flex items-center justify-between gap-4 px-5 py-4">
            <div>
              <h2 className="text-[15px] font-semibold text-foreground">抓取运行</h2>
              <p className="mt-1 text-[12px] text-muted-foreground">本轮新增入库、AI 总结和事件发布的对账面板</p>
            </div>
            <div className="inline-flex items-center gap-1.5 text-[12px] text-muted-foreground">
              <Activity className="w-3.5 h-3.5" />
              已加载 {runs.length} 次
            </div>
          </div>

          {runs.length === 0 ? (
            <p className="text-sm text-muted-foreground py-10 text-center border-t border-border">暂无抓取记录</p>
          ) : (
            <div className="grid grid-cols-1 lg:grid-cols-[352px_minmax(0,1fr)] border-t border-border lg:h-[min(760px,calc(100vh-148px))] lg:min-h-[560px]">
              <div className="min-h-0 border-b border-border lg:border-b-0 lg:border-r flex flex-col">
                <div
                  data-testid="run-ledger-scroll"
                  className="overflow-auto max-h-[44vh] lg:max-h-none lg:flex-1"
                  onScroll={handleRunLedgerScroll}
                >
                  <table className="w-full table-fixed text-[12px]">
                    <thead className="sticky top-0 z-10">
                      <tr className="bg-secondary text-muted-foreground font-semibold">
                        <th className="text-left px-3 py-2.5 w-[92px]">Run</th>
                        <th className="text-left px-2 py-2.5 w-[62px]">状态</th>
                        <th className="text-right px-2 py-2.5 w-[58px]" title="本轮新增入库">新增入库</th>
                        <th className="text-right px-2 py-2.5 w-[52px]">AI</th>
                        <th className="text-right px-3 py-2.5 w-[48px]">事件</th>
                      </tr>
                    </thead>
                    <tbody>
                      {runs.map((run) => {
                        const publishedEvents = getRunPublishedEvents(run)
                        return (
                          <tr
                            key={run.id}
                            data-testid={`run-row-${run.id}`}
                            className={cn(
                              'h-14 border-t border-border hover:bg-background transition-colors cursor-pointer tabular-nums',
                              selectedRun?.id === run.id && 'bg-accent shadow-[inset_2px_0_0_var(--primary)]',
                            )}
                            onClick={() => handleSelectRun(run.id)}
                          >
                            <td className="px-3 py-2 align-middle">
                              <div className="font-semibold text-foreground">#{run.id}</div>
                              <div className="text-[11px] text-muted-foreground">{formatRunDate(run.started_at)}</div>
                            </td>
                            <td className="px-2 py-2 align-middle">
                              <span className={cn('px-1.5 py-0.5 text-[11px] font-medium rounded-md', statusClass(run.status))}>
                                {statusLabel(run.status)}
                              </span>
                            </td>
                            <td className="px-2 py-2 text-right align-middle font-semibold text-foreground">
                              {formatNumber(getRunNewItems(run))}
                            </td>
                            <td className="px-2 py-2 text-right align-middle text-muted-foreground">
                              {getRunAiLabel(run)}
                            </td>
                            <td className="px-3 py-2 text-right align-middle text-muted-foreground">
                              {formatNumber(publishedEvents)}
                            </td>
                          </tr>
                        )
                      })}
                      <tr>
                        <td colSpan={5} className="px-3 py-3 text-center text-[11px] text-muted-foreground border-t border-border">
                          {runsLoadingMore ? (
                            <span className="inline-flex items-center justify-center gap-1.5">
                              <Loader2 className="w-3 h-3 animate-spin" />
                              加载历史记录
                            </span>
                          ) : runsHasMore ? '继续下拉加载更早记录' : '已加载全部记录'}
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>

              <div className="min-w-0 min-h-0 overflow-auto">
                {runLoading ? (
                  <RunInspectorSkeleton />
                ) : selectedRun && audit ? (
                  <div className="min-h-full">
                    <div className="sticky top-0 z-20 border-b border-border bg-card px-5 py-3">
                      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <h3 className="text-[14px] font-semibold text-foreground tabular-nums">Run #{selectedRun.id}</h3>
                            <span className={cn('px-1.5 py-0.5 text-[11px] font-medium rounded-md', statusClass(selectedRun.status))}>
                              {statusLabel(selectedRun.status)}
                            </span>
                          </div>
                          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px] text-muted-foreground">
                            <span>{formatRunDate(selectedRun.started_at)}</span>
                            <span aria-hidden="true">·</span>
                            <span>{audit.result_status ? `结果 ${audit.result_status}` : '本轮详情'}</span>
                          </div>
                        </div>
                        <div className="grid grid-cols-3 gap-2 text-[12px] tabular-nums md:min-w-[300px]">
                          <RunContextStat label="入库" value={formatNumber(selectedRunNewItems)} />
                          <RunContextStat label="AI" value={selectedRunAiLabel} />
                          <RunContextStat label="事件" value={formatNumber(selectedRunPublishedEvents)} />
                        </div>
                      </div>
                    </div>

                    <div className="p-5 space-y-5">
                    <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                      <Metric label="总耗时" value={formatSeconds(selectedRun.duration_sec ?? audit.duration_sec)} icon={<Clock3 className="w-4 h-4" />} />
                      <Metric label="本轮新增入库" value={formatNumber(selectedRunNewItems)} icon={<Database className="w-4 h-4" />} />
                      <Metric label="AI 总结" value={selectedRunAiLabel} />
                      <Metric label="AI 失败" value={formatNumber(ai?.failed)} />
                      <Metric label="发布事件" value={formatNumber(eventCluster?.published_clusters)} />
                    </div>

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
                      <div className="rounded-lg border border-border bg-background p-4">
                        <h3 className="text-[13px] font-semibold text-foreground mb-2">阶段耗时</h3>
                        {stageRows.length === 0 ? (
                          <p className="text-sm text-muted-foreground py-3">暂无阶段耗时</p>
                        ) : (
                          <div className="space-y-2">
                            {stageRows.map(([stage, seconds]) => (
                              <div key={stage} className="flex items-center justify-between gap-4 text-sm tabular-nums">
                                <span className="text-muted-foreground truncate">{stage}</span>
                                <span className="font-medium text-foreground">{formatSeconds(seconds)}</span>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>

                      <div className="rounded-lg border border-border bg-background p-4">
                        <h3 className="text-[13px] font-semibold text-foreground mb-2">Pill 分布</h3>
                        {pillRows.length === 0 ? (
                          <p className="text-sm text-muted-foreground py-3">暂无 pill 数据</p>
                        ) : (
                          <div className="flex flex-wrap gap-2">
                            {pillRows.map((row) => (
                              <span
                                key={row.pill}
                                className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-card border border-border text-[12px] text-foreground tabular-nums"
                              >
                                {row.pill}
                                <b>{row.count}</b>
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>

                    <div>
                      <h3 className="text-[13px] font-semibold text-foreground mb-2">Platform / Source</h3>
                      {platformSourceRows.length === 0 ? (
                        <p className="text-sm text-muted-foreground py-3">暂无 source 新增数据</p>
                      ) : (
                        <div className="overflow-auto border border-border rounded-lg bg-card">
                          <table className="w-full text-sm">
                            <thead className="sticky top-0 z-10">
                              <tr className="bg-secondary text-muted-foreground text-[12px] font-semibold">
                                <th className="text-left px-3 py-2.5">平台</th>
                                <th className="text-left px-3 py-2.5">Source</th>
                                <th className="text-right px-3 py-2.5">本轮新增入库</th>
                                <th className="text-right px-3 py-2.5">下钻</th>
                              </tr>
                            </thead>
                            <tbody>
                              {platformSourceRows.map((row) => {
                                const selected = drilldown?.platform === row.platform && drilldown?.source === row.source
                                return (
                                  <Fragment key={`${row.platform}:${row.source}`}>
                                    <tr
                                      className={cn(
                                        'border-t border-border cursor-pointer hover:bg-background transition-colors',
                                        selected && 'bg-accent',
                                      )}
                                      onClick={() => handleSourceDrilldown(row)}
                                    >
                                      <td className="px-3 py-3 text-foreground">{row.platform}</td>
                                      <td className="px-3 py-3 text-muted-foreground">{row.source}</td>
                                      <td className="px-3 py-3 text-right font-medium text-foreground tabular-nums">{formatNumber(row.count)}</td>
                                      <td className="px-3 py-3 text-right">
                                        <button
                                          onClick={(event) => {
                                            event.stopPropagation()
                                            handleSourceDrilldown(row)
                                          }}
                                          className="inline-flex items-center justify-center p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
                                          title={selected ? '收起标题' : '查看标题'}
                                        >
                                          <ChevronRight className={cn('w-4 h-4 transition-transform', selected && 'rotate-90')} />
                                        </button>
                                      </td>
                                    </tr>
                                    {selected && (
                                      <tr className="border-t border-border bg-background">
                                        <td colSpan={4} className="p-0">
                                          {itemsLoading ? (
                                            <div className="py-8 flex justify-center">
                                              <Loader2 className="w-5 h-5 animate-spin text-primary" />
                                            </div>
                                          ) : items.length === 0 ? (
                                            <p className="px-4 py-4 text-sm text-muted-foreground">暂无本轮新增标题</p>
                                          ) : (
                                            <div className="max-h-[320px] overflow-auto border-t border-border">
                                              <table className="w-full text-sm">
                                                <thead className="sticky top-0 z-10">
                                                  <tr className="bg-secondary text-muted-foreground text-[12px] font-semibold">
                                                    <th className="text-left px-3 py-2.5">标题</th>
                                                    <th className="text-left px-3 py-2.5">Pill</th>
                                                    <th className="text-left px-3 py-2.5">AI</th>
                                                    <th className="text-left px-3 py-2.5">聚合</th>
                                                    <th className="text-left px-3 py-2.5">时间</th>
                                                  </tr>
                                                </thead>
                                                <tbody>
                                                  {items.map((item) => (
                                                    <tr key={item.id} className="border-t border-border">
                                                      <td className="px-3 py-3 max-w-[360px]">
                                                        <a
                                                          href={`#${buildInfoItemHash(item.id)}`}
                                                          className="block truncate text-foreground hover:text-primary"
                                                          title={item.title}
                                                        >
                                                          {item.title}
                                                        </a>
                                                      </td>
                                                      <td className="px-3 py-3 text-muted-foreground">{item.pill ?? '—'}</td>
                                                      <td className="px-3 py-3">
                                                        <span className={cn('px-1.5 py-0.5 rounded-md text-[11px] font-medium', itemStatusClass(item.ai_status))}>
                                                          {item.ai_status}
                                                        </span>
                                                      </td>
                                                      <td className="px-3 py-3">
                                                        <span className={cn('px-1.5 py-0.5 rounded-md text-[11px] font-medium', itemStatusClass(item.cluster_status))}>
                                                          {item.cluster_status}
                                                        </span>
                                                      </td>
                                                      <td className="px-3 py-3 text-muted-foreground">{formatDate(item.created_at ?? item.fetched_at)}</td>
                                                    </tr>
                                                  ))}
                                                </tbody>
                                              </table>
                                            </div>
                                          )}
                                        </td>
                                      </tr>
                                    )}
                                  </Fragment>
                                )
                              })}
                            </tbody>
                          </table>
                        </div>
                      )}
                    </div>

                  </div>
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground py-10 text-center">请选择抓取记录</p>
                )}
              </div>
            </div>
          )}
        </section>

        <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1.15fr)_minmax(380px,0.85fr)] gap-6 items-start">
          <section className="bg-card border border-border rounded-lg p-5 min-w-0 shadow-subtle">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-[15px] font-semibold text-foreground">Embedding 用量</h2>
              <div className="text-[12px] text-muted-foreground">最近 24 小时</div>
            </div>

            {!embeddingUsage || !embSummary ? (
              <p className="text-sm text-muted-foreground py-4 text-center">暂无 embedding 调用记录</p>
            ) : (
              <div className="space-y-5">
                <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                  <Metric label="调用" value={`${embSummary.success_calls}/${embSummary.total_calls}`} />
                  <Metric label="输入条数" value={formatNumber(embSummary.input_count)} />
                  <Metric label="估算 token" value={formatNumber(embSummary.estimated_tokens_attempted)} />
                  <Metric label="输出向量" value={formatNumber(embSummary.output_count)} />
                  <Metric label="估算费用" value={formatCost(embSummary.estimated_cost_yuan_success)} />
                </div>

                <div className="grid grid-cols-1 2xl:grid-cols-2 gap-5">
                  <div className="min-w-0">
                    <h3 className="text-[13px] font-semibold text-foreground mb-2">调用来源</h3>
                    {embSourceRows.length === 0 ? (
                      <p className="text-sm text-muted-foreground py-3">暂无来源数据</p>
                    ) : (
                      <div className="overflow-auto border border-border rounded-lg max-h-[360px]">
                        <table className="w-full min-w-[420px] text-sm">
                          <thead className="sticky top-0 z-10">
                            <tr className="bg-secondary text-muted-foreground text-[12px] font-semibold">
                              <th className="text-left px-3 py-2.5">来源</th>
                              <th className="text-left px-3 py-2.5">状态</th>
                              <th className="text-right px-3 py-2.5">token</th>
                              <th className="text-right px-3 py-2.5">费用</th>
                            </tr>
                          </thead>
                          <tbody>
                            {embSourceRows.map((row, idx) => (
                              <tr key={`${row.source}:${row.stage}:${row.status}:${idx}`} className="border-t border-border">
                                <td className="px-3 py-3">
                                  <div className="text-foreground">{row.source || 'unknown'}</div>
                                  <div className="text-[12px] text-muted-foreground">{row.stage || row.model || '—'}</div>
                                </td>
                                <td className="px-3 py-3 text-muted-foreground">{row.status}</td>
                                <td className="px-3 py-3 text-right font-medium text-foreground tabular-nums">{formatNumber(row.estimated_tokens)}</td>
                                <td className="px-3 py-3 text-right text-muted-foreground tabular-nums">{formatCost(row.estimated_cost_yuan)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>

                  <div className="min-w-0">
                    <h3 className="text-[13px] font-semibold text-foreground mb-2">最近调用</h3>
                    {embLogs.length === 0 ? (
                      <p className="text-sm text-muted-foreground py-3">暂无调用明细</p>
                    ) : (
                      <div className="overflow-auto border border-border rounded-lg max-h-[360px]">
                        <table className="w-full min-w-[520px] text-sm">
                          <thead className="sticky top-0 z-10">
                            <tr className="bg-secondary text-muted-foreground text-[12px] font-semibold">
                              <th className="text-left px-3 py-2.5">时间</th>
                              <th className="text-left px-3 py-2.5">Run</th>
                              <th className="text-right px-3 py-2.5">输入</th>
                              <th className="text-right px-3 py-2.5">token</th>
                            </tr>
                          </thead>
                          <tbody>
                            {embLogs.map((row) => (
                              <tr key={row.id} className="border-t border-border">
                                <td className="px-3 py-3">
                                  <div className="text-foreground">{formatDate(row.created_at)}</div>
                                  <div className="text-[12px] text-muted-foreground truncate max-w-[220px]" title={row.error || row.caller_file || ''}>
                                    {row.status === 'success' ? (row.caller_file || row.source || '—') : (row.error || 'failed')}
                                  </div>
                                </td>
                                <td className="px-3 py-3 text-muted-foreground">{row.run_id ? `#${row.run_id}` : '—'}</td>
                                <td className="px-3 py-3 text-right text-muted-foreground tabular-nums">{formatNumber(row.input_count)}</td>
                                <td className="px-3 py-3 text-right font-medium text-foreground tabular-nums">{formatNumber(row.estimated_tokens)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            )}
          </section>

          <section className="bg-card border border-border rounded-lg p-5 min-w-0 shadow-subtle">
            <div className="flex items-center justify-between gap-4 mb-4">
              <h2 className="text-[15px] font-semibold text-foreground">权限管理</h2>
              <button
                onClick={copyUnusedCodes}
                className={cn(
                  'inline-flex items-center gap-1.5 px-3 py-2 text-[13px] font-semibold rounded-lg transition-colors',
                  'text-muted-foreground bg-secondary hover:text-foreground hover:bg-muted',
                )}
              >
                <Copy className="w-3.5 h-3.5" />
                复制未使用码
              </button>
            </div>

            <div className="space-y-5">
              <div>
                <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] mb-3">
                  <label className="space-y-1.5">
                    <span className="text-[12px] font-medium text-muted-foreground">生成数量</span>
                    <input
                      aria-label="生成数量"
                      type="number"
                      min={1}
                      max={50}
                      value={inviteCount}
                      onChange={(event) => {
                        const value = Number.parseInt(event.target.value, 10)
                        setInviteCount(Number.isFinite(value) ? Math.min(Math.max(value, 1), 50) : 1)
                      }}
                      className="h-9 w-full rounded-md border border-border bg-background px-3 text-sm text-foreground outline-none focus:border-primary"
                    />
                  </label>
                  <label className="space-y-1.5">
                    <span className="text-[12px] font-medium text-muted-foreground">每个码可用</span>
                    <input
                      aria-label="每个码可用"
                      type="number"
                      min={1}
                      value={inviteMaxUses}
                      onChange={(event) => {
                        const value = Number.parseInt(event.target.value, 10)
                        setInviteMaxUses(Number.isFinite(value) ? Math.max(value, 1) : 1)
                      }}
                      className="h-9 w-full rounded-md border border-border bg-background px-3 text-sm text-foreground outline-none focus:border-primary"
                    />
                  </label>
                  <button
                    onClick={handleGenerate}
                    disabled={generating}
                    className={cn(
                      'inline-flex h-9 items-center justify-center gap-1.5 self-end px-3 text-[13px] font-semibold rounded-lg transition-colors',
                      'text-primary bg-accent hover:bg-primary hover:text-white',
                      'disabled:opacity-50 disabled:cursor-not-allowed',
                    )}
                  >
                    {generating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
                    生成
                  </button>
                </div>
                <div className="flex flex-wrap gap-2 mb-4">
                  {INVITE_COUNT_PRESETS.map((preset) => (
                    <button
                      key={preset.label}
                      onClick={() => {
                        setInviteCount(preset.count)
                        setInviteMaxUses(preset.maxUses)
                      }}
                      className="px-2.5 py-1.5 text-[12px] font-medium rounded-md border border-border text-muted-foreground hover:text-foreground hover:bg-secondary transition-colors"
                    >
                      {preset.label}
                    </button>
                  ))}
                </div>
                <h3 className="text-[13px] font-semibold text-foreground mb-2">邀请码</h3>
                {codes.length === 0 ? (
                  <p className="text-sm text-muted-foreground py-4 text-center">暂无邀请码</p>
                ) : (
                  <div className="overflow-auto border border-border rounded-lg max-h-[220px]">
                    <table className="w-full text-sm">
                      <thead className="sticky top-0 z-10">
                        <tr className="bg-secondary text-muted-foreground text-[12px] font-semibold">
                          <th className="text-left px-3 py-2.5">码</th>
                          <th className="text-left px-3 py-2.5">创建时间</th>
                          <th className="text-left px-3 py-2.5">使用量</th>
                          <th className="text-left px-3 py-2.5">状态</th>
                          <th className="text-right px-3 py-2.5">操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {codes.map((c) => {
                          const st = codeStatus(c)
                          return (
                            <tr key={c.code} className="border-t border-border hover:bg-background transition-colors">
                              <td className="px-3 py-3">
                                <code className="font-mono tracking-[1px] text-foreground">{c.code}</code>
                              </td>
                              <td className="px-3 py-3 text-muted-foreground">
                                {new Date(c.created_at).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}
                              </td>
                              <td className="px-3 py-3 text-muted-foreground font-mono">
                                {c.used_count}/{c.max_uses}
                              </td>
                              <td className="px-3 py-3">
                                <span className={cn('px-2 py-0.5 text-[11px] font-medium rounded-md', st.cls)}>
                                  {st.label}
                                </span>
                              </td>
                              <td className="px-3 py-3 text-right">
                                <div className="flex items-center justify-end gap-1">
                                  <button
                                    onClick={() => copyCode(c.code)}
                                    className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
                                    title="复制"
                                  >
                                    <Copy className="w-3.5 h-3.5" />
                                  </button>
                                  {st.kind === 'unused' && (
                                    <button
                                      onClick={() => handleDelete(c.code)}
                                      className="p-1.5 rounded-md text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors"
                                      title="删除"
                                    >
                                      <Trash2 className="w-3.5 h-3.5" />
                                    </button>
                                  )}
                                </div>
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              <div>
                <h3 className="text-[13px] font-semibold text-foreground mb-2">用户列表</h3>
                {users.length === 0 ? (
                  <p className="text-sm text-muted-foreground py-4 text-center">暂无用户</p>
                ) : (
                  <div className="overflow-auto border border-border rounded-lg max-h-[260px]">
                    <table className="w-full text-sm">
                      <thead className="sticky top-0 z-10">
                        <tr className="bg-secondary text-muted-foreground text-[12px] font-semibold">
                          <th className="text-left px-3 py-2.5">用户名</th>
                          <th className="text-left px-3 py-2.5">邮箱</th>
                          <th className="text-left px-3 py-2.5">角色</th>
                          <th className="text-left px-3 py-2.5">注册时间</th>
                        </tr>
                      </thead>
                      <tbody>
                        {users.map((u) => (
                          <tr key={u.id} className="border-t border-border hover:bg-background transition-colors">
                            <td className="px-3 py-3 text-foreground font-medium">{u.username}</td>
                            <td className="px-3 py-3 text-muted-foreground">{u.email}</td>
                            <td className="px-3 py-3">
                              <span className={cn(
                                'px-2 py-0.5 text-[11px] font-medium rounded-md',
                                u.role === 'admin' ? 'bg-accent text-accent-foreground' : 'bg-muted text-muted-foreground',
                              )}>
                                {u.role === 'admin' ? '管理员' : '用户'}
                              </span>
                            </td>
                            <td className="px-3 py-3 text-muted-foreground">{new Date(u.created_at).toLocaleDateString('zh-CN')}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>
          </section>
        </div>
      </main>
    </div>
  )
}

function Metric({
  label,
  value,
  icon,
}: {
  label: string
  value: string
  icon?: ReactNode
}) {
  return (
    <div className="border border-border bg-background rounded-lg px-3 py-3 min-w-0 min-h-[72px]">
      <div className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
        {icon}
        <span className="truncate">{label}</span>
      </div>
      <div className="mt-1.5 text-xl font-semibold text-foreground truncate tabular-nums">{value}</div>
    </div>
  )
}

function RunContextStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border bg-background px-2.5 py-1.5 min-w-0">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="mt-0.5 truncate font-semibold text-foreground">{value}</div>
    </div>
  )
}

function AdminSkeleton() {
  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-50 flex items-center gap-3 px-4 h-14 bg-card border-b border-border">
        <SkeletonBlock className="w-8 h-8 rounded-md" />
        <SkeletonBlock className="w-20 h-4" />
        <div className="ml-auto hidden sm:flex items-center gap-2">
          <SkeletonBlock className="w-24 h-7 rounded-md" />
          <SkeletonBlock className="w-20 h-4" />
        </div>
      </header>
      <main className="max-w-[1280px] mx-auto px-4 py-8 space-y-6">
        <section className="bg-card border border-border rounded-lg overflow-hidden shadow-subtle">
          <div className="flex items-center justify-between gap-4 px-5 py-4">
            <div className="space-y-2">
              <SkeletonBlock className="w-20 h-4" />
              <SkeletonBlock className="w-56 h-3" />
            </div>
            <SkeletonBlock className="w-24 h-4" />
          </div>
          <div className="grid grid-cols-1 lg:grid-cols-[352px_minmax(0,1fr)] border-t border-border lg:h-[min(760px,calc(100vh-148px))] lg:min-h-[560px]">
            <div className="border-b border-border lg:border-b-0 lg:border-r">
              <div className="grid grid-cols-[1fr_72px_64px] gap-3 bg-secondary px-3 py-3">
                <SkeletonBlock className="h-3" />
                <SkeletonBlock className="h-3" />
                <SkeletonBlock className="h-3" />
              </div>
              <div className="divide-y divide-border">
                {Array.from({ length: 9 }).map((_, index) => (
                  <div key={index} className="grid grid-cols-[1fr_72px_64px] gap-3 px-3 py-3.5">
                    <div className="space-y-2">
                      <SkeletonBlock className="w-16 h-4" />
                      <SkeletonBlock className="w-20 h-3" />
                    </div>
                    <SkeletonBlock className="w-11 h-5 rounded-md" />
                    <SkeletonBlock className="justify-self-end w-8 h-4" />
                  </div>
                ))}
              </div>
            </div>
            <RunInspectorSkeleton />
          </div>
        </section>
      </main>
    </div>
  )
}

function RunInspectorSkeleton() {
  return (
    <div className="min-h-full">
      <div className="border-b border-border bg-card px-5 py-3">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div className="space-y-2">
            <SkeletonBlock className="w-28 h-4" />
            <SkeletonBlock className="w-44 h-3" />
          </div>
          <div className="grid grid-cols-3 gap-2 md:min-w-[300px]">
            <SkeletonBlock className="h-12 rounded-md" />
            <SkeletonBlock className="h-12 rounded-md" />
            <SkeletonBlock className="h-12 rounded-md" />
          </div>
        </div>
      </div>
      <div className="p-5 space-y-5">
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          {Array.from({ length: 5 }).map((_, index) => (
            <SkeletonBlock key={index} className="h-[72px] rounded-lg" />
          ))}
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
          <SkeletonBlock className="h-32 rounded-lg" />
          <SkeletonBlock className="h-32 rounded-lg" />
        </div>
        <SkeletonBlock className="h-[280px] rounded-lg" />
      </div>
    </div>
  )
}

function SkeletonBlock({ className }: { className?: string }) {
  return <div className={cn('animate-pulse rounded bg-muted', className)} />
}
