import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode, UIEvent } from 'react'
import { Activity, AlertTriangle, ArrowLeft, CheckCircle2, ChevronRight, Clock3, Copy, Database, Loader2, MoreHorizontal, Plus, RefreshCw, Search, Settings2, Trash2, X } from 'lucide-react'
import { toast } from 'sonner'

import { cn } from '../lib/utils'
import { buildInfoItemHash } from '../lib/itemDeepLink'
import { HighlightsFilteredTab } from '../components/admin/HighlightsFilteredTab'
import { OverviewTab } from '../components/admin/OverviewTab'
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
import {
  getAdminSources,
  createAdminSource,
  validateAdminSource,
  updateAdminSource,
  deleteAdminSource,
  reconcileLingowhaleSources,
  getAdminSourceAlgoParams,
  updateAdminSourceAlgoParams,
  searchWechatSources,
  syncAdminXList,
} from '../lib/api'
import type {
  AdminSource,
  AdminSourceGroup,
  AdminSourceAlgoParams,
  AdminSourcePreviewItem,
  AdminSourceReconcileResponse,
  AdminSourceValidateResponse,
  AdminWechatSearchChannel,
  AdminXListStatus,
  AdminXRunSummary,
} from '../lib/api'

type AdminTab = 'overview' | 'runs' | 'subscriptions' | 'access' | 'filtered'
const ADMIN_TABS: { key: AdminTab; label: string }[] = [
  { key: 'overview', label: '总览' },
  { key: 'runs', label: '抓取运行' },
  { key: 'subscriptions', label: '订阅配置' },
  { key: 'access', label: '用户与权限' },
  { key: 'filtered', label: '精选漏斗' },
]

type SourceWizardStep = 1 | 2 | 3

type WizardPlatform = 'wechat_mp' | 'x_user' | 'rss' | 'reddit' | 'github_repo' | 'bilibili_up'

const SOURCE_PLATFORM_OPTIONS: Array<{
  value: WizardPlatform
  label: string
  helper: string
  disabled?: boolean
}> = [
  { value: 'wechat_mp', label: '公众号', helper: '推荐走语鲸：在语鲸 App 关注该公众号后，回总览区点「对账导入」一键纳管；此处粘贴 RSS URL 仅用于语鲸没有的号' },
  { value: 'x_user', label: 'X', helper: '只输入 handle，不含 @，最长 15 位' },
  { value: 'rss', label: 'RSS', helper: '输入 http(s) feed URL' },
  { value: 'reddit', label: 'Reddit', helper: '输入 subreddit 名，不含 r/' },
  { value: 'github_repo', label: 'GitHub', helper: '输入 owner/repo' },
  { value: 'bilibili_up', label: 'B站', helper: '抓取管线未接入', disabled: true },
]

const ALGO_PARAM_SPECS: Array<{
  key: keyof AdminSourceAlgoParams
  label: string
  min: number
  max: number
  note: string
}> = [
  { key: 'hackernews_count', label: 'Hacker News 数量', min: 1, max: 500, note: 'HN top 抓取条数' },
  { key: 'github_trending_count', label: 'GitHub Trending 数量', min: 1, max: 500, note: 'trending 仓库条数' },
  { key: 'bilibili_hot_count', label: 'B站热门数量', min: 1, max: 500, note: '热门榜抓取条数' },
  { key: 'bilibili_rank_count', label: 'B站排行数量', min: 1, max: 500, note: '排行榜抓取条数' },
  { key: 'bilibili_videos_per_up', label: 'B站每 UP 视频数', min: 1, max: 100, note: '单个 UP 视频上限' },
]

function readAdminTab(): AdminTab {
  const hash = window.location.hash.slice(1) // e.g. "admin/runs"
  const sub = hash.split('?')[0].split('/')[1] as AdminTab | undefined
  if (sub === 'runs' || sub === 'subscriptions' || sub === 'access' || sub === 'filtered') return sub
  return 'overview'
}

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
  const [activeTab, setActiveTab] = useState<AdminTab>(() => readAdminTab())
  const [reloadSignal, setReloadSignal] = useState(0)

  useEffect(() => {
    const onHash = () => setActiveTab(readAdminTab())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  function navTab(tab: AdminTab) {
    window.location.hash = tab === 'overview' ? 'admin' : `admin/${tab}`
  }

  const loadAdminData = useCallback(() => {
    setLoading(true)
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

  useEffect(() => {
    loadAdminData()
  }, [loadAdminData])

  function handleRefresh() {
    loadAdminData()
    setReloadSignal((n) => n + 1)
  }

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

  return (
    <div className="admin-scope min-h-screen bg-background">
      <header className="sticky top-0 z-50 flex items-center gap-3 px-4 h-14 bg-card border-b border-border">
        <a
          href="#"
          className="p-2 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
          aria-label="返回"
        >
          <ArrowLeft className="w-4 h-4" />
        </a>
        <h1 className="text-base font-semibold text-foreground hidden sm:block">管理面板</h1>
        <nav className="flex items-stretch gap-0.5 h-full ml-1" aria-label="管理面板导航">
          {ADMIN_TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => navTab(t.key)}
              className={cn(
                'px-3 font-body-cjk text-sm border-b-2 -mb-px transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-inset',
                activeTab === t.key
                  ? 'text-foreground font-semibold border-primary'
                  : 'text-muted-foreground border-transparent hover:text-foreground',
              )}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-2 text-[12px] text-muted-foreground">
          <button
            onClick={handleRefresh}
            disabled={loading}
            className="inline-flex h-8 items-center gap-1.5 rounded-[4px] border border-border bg-card px-2.5 font-body-cjk text-sm font-medium hover:border-muted-foreground disabled:opacity-50 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            aria-label="刷新"
          >
            <RefreshCw className={cn('w-3.5 h-3.5', loading && 'animate-spin')} />
            <span className="hidden sm:inline">刷新</span>
          </button>
        </div>
      </header>

      <main className={cn('mx-auto px-4 py-8', activeTab === 'filtered' ? 'w-full max-w-none' : 'max-w-[1280px]')}>
        {activeTab === 'overview' && (
          <OverviewTab reloadSignal={reloadSignal} onOpenRuns={() => navTab('runs')} />
        )}

        {activeTab === 'subscriptions' && <SubscriptionConfigTab />}

        {activeTab === 'filtered' && <HighlightsFilteredTab reloadSignal={reloadSignal} />}

        {activeTab === 'runs' &&
          (loading ? (
            <TabLoading />
          ) : (
            <div className="space-y-6">
              <section className="bg-card border border-border rounded-md overflow-hidden">
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
                      <div className="rounded-md border border-border bg-background p-4">
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

                      <div className="rounded-md border border-border bg-background p-4">
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
                        <div className="overflow-auto border border-border rounded-md bg-card">
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

              <section className="bg-card border border-border rounded-md p-5 min-w-0">
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
                      <div className="overflow-auto border border-border rounded-md max-h-[360px]">
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
                      <div className="overflow-auto border border-border rounded-md max-h-[360px]">
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
            </div>
          ))}

        {activeTab === 'access' &&
          (loading ? (
            <TabLoading />
          ) : (
            <section className="bg-card border border-border rounded-md p-5 min-w-0">
            <div className="flex items-center justify-between gap-4 mb-4">
              <h2 className="text-[15px] font-semibold text-foreground">权限管理</h2>
              <button
                onClick={copyUnusedCodes}
                className={cn(
                  'inline-flex h-8 items-center gap-1.5 rounded-[4px] px-2.5 font-body-cjk text-sm font-medium transition-colors',
                  'text-muted-foreground bg-secondary hover:text-foreground hover:bg-muted',
                  'focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background',
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
                      'inline-flex h-9 items-center justify-center gap-1.5 self-end px-3 font-body-cjk text-sm font-medium rounded-[4px] transition-colors',
                      'text-primary bg-accent hover:bg-primary hover:text-primary-foreground',
                      'focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background',
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
                      className="px-2.5 py-1.5 font-body-cjk text-[12px] font-medium rounded-[4px] border border-border text-muted-foreground hover:text-foreground hover:bg-secondary transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                    >
                      {preset.label}
                    </button>
                  ))}
                </div>
                <h3 className="text-[13px] font-semibold text-foreground mb-2">邀请码</h3>
                {codes.length === 0 ? (
                  <p className="text-sm text-muted-foreground py-4 text-center">暂无邀请码</p>
                ) : (
                  <div className="overflow-auto border border-border rounded-md max-h-[220px]">
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
                                    className="p-1.5 rounded-[4px] text-muted-foreground hover:text-foreground hover:bg-muted transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]"
                                    title="复制"
                                  >
                                    <Copy className="w-3.5 h-3.5" />
                                  </button>
                                  {st.kind === 'unused' && (
                                    <button
                                      onClick={() => handleDelete(c.code)}
                                      className="p-1.5 rounded-[4px] text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]"
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
                  <div className="overflow-auto border border-border rounded-md max-h-[260px]">
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
          ))}
      </main>
    </div>
  )
}

function TabLoading() {
  return (
    <div className="flex items-center justify-center gap-2 py-20 text-[13px] text-muted-foreground">
      <Loader2 className="w-4 h-4 animate-spin" />
      加载中…
    </div>
  )
}

function SubscriptionConfigTab() {
  const [groups, setGroups] = useState<AdminSourceGroup[]>([])
  const [total, setTotal] = useState(0)
  const [latestXRun, setLatestXRun] = useState<AdminXRunSummary | null>(null)
  const [xList, setXList] = useState<AdminXListStatus | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [algoParams, setAlgoParams] = useState<AdminSourceAlgoParams | null>(null)
  const [algoDraft, setAlgoDraft] = useState<Record<keyof AdminSourceAlgoParams, string>>(emptyAlgoDraft)
  const [algoError, setAlgoError] = useState<string | null>(null)
  const [reconcile, setReconcile] = useState<AdminSourceReconcileResponse | null>(null)
  const [showReconcileList, setShowReconcileList] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [savingSourceId, setSavingSourceId] = useState<number | null>(null)
  const [savingAlgo, setSavingAlgo] = useState(false)
  const [importingMissing, setImportingMissing] = useState(false)
  const [syncingXList, setSyncingXList] = useState(false)
  const [wizardOpen, setWizardOpen] = useState(false)
  const [wizardPlatform, setWizardPlatform] = useState<WizardPlatform | undefined>(undefined)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const [sourceRes, algoRes, reconcileRes] = await Promise.all([
        getAdminSources(),
        getAdminSourceAlgoParams(),
        reconcileLingowhaleSources(),
      ])
      setGroups(sourceRes.groups)
      setTotal(sourceRes.total)
      setLatestXRun(sourceRes.latest_x_run ?? null)
      setXList(sourceRes.x_list ?? null)
      setAlgoParams(algoRes.params)
      setAlgoDraft(algoParamsToDraft(algoRes.params))
      setReconcile(reconcileRes)
      setShowReconcileList(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载订阅配置失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  async function refreshSourcesOnly() {
    const sourceRes = await getAdminSources()
    setGroups(sourceRes.groups)
    setTotal(sourceRes.total)
    setLatestXRun(sourceRes.latest_x_run ?? null)
    setXList(sourceRes.x_list ?? null)
  }

  async function handleSyncXList(full: boolean) {
    setSyncingXList(true)
    try {
      const status = await syncAdminXList(full)
      setXList(status)
      toast.success(`X List 已同步 ${status.synced_count} / ${status.registry_count}`)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'X List 同步失败')
    } finally {
      setSyncingXList(false)
    }
  }

  async function handleStatus(source: AdminSource, status: 'active' | 'paused') {
    setSavingSourceId(source.id)
    try {
      const res = await updateAdminSource(source.id, { status })
      setGroups((prev) => replaceSourceInGroups(prev, res.source))
      toast.success(status === 'active' ? '已启用' : '已停用')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '更新信源失败')
    } finally {
      setSavingSourceId(null)
    }
  }

  async function handleDelete(source: AdminSource) {
    if (!window.confirm(`确认删除 ${source.display_name || source.source_key}？`)) return
    setSavingSourceId(source.id)
    try {
      await deleteAdminSource(source.id)
      setGroups((prev) => removeSourceFromGroups(prev, source.id))
      setTotal((prev) => Math.max(prev - 1, 0))
      toast.success('已删除')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '删除信源失败')
    } finally {
      setSavingSourceId(null)
    }
  }

  async function handleSaveAlgo() {
    const parsed = parseAlgoDraft(algoDraft)
    if ('error' in parsed) {
      setAlgoError(parsed.error)
      return
    }
    setAlgoError(null)
    setSavingAlgo(true)
    try {
      const res = await updateAdminSourceAlgoParams(parsed.params)
      setAlgoParams(res.params)
      setAlgoDraft(algoParamsToDraft(res.params))
      toast.success('算法源参数已保存')
    } catch (err) {
      setAlgoError(err instanceof Error ? err.message : '保存算法源参数失败')
    } finally {
      setSavingAlgo(false)
    }
  }

  async function handleImportMissing() {
    const missing = reconcile?.missing ?? []
    if (missing.length === 0) return
    setImportingMissing(true)
    try {
      const res = await reconcileLingowhaleSources({ import_keys: missing.map((item) => item.source_key) })
      setReconcile(res)
      await refreshSourcesOnly()
      toast.success(`已导入 ${res.imported.length} 个订阅`)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '导入语鲸订阅失败')
    } finally {
      setImportingMissing(false)
    }
  }

  function handleCreated(source: AdminSource) {
    setGroups((prev) => appendSourceToGroups(prev, source))
    setTotal((prev) => prev + 1)
    if (source.platform === 'x_user') {
      setXList((prev) => prev ? {
        ...prev,
        registry_count: prev.registry_count + 1,
        pending_count: prev.pending_count + 1,
        pending_handles: [...prev.pending_handles, source.source_key],
      } : prev)
    }
  }

  function openWizard(platform?: WizardPlatform) {
    setWizardPlatform(platform)
    setWizardOpen(true)
  }

  const latestUpdatedAt = latestSourceUpdatedAt(groups)
  const missing = reconcile?.missing ?? []
  const attentionCount = groups.reduce(
    (count, group) => count + group.sources.filter((source) => ['error', 'warning'].includes(sourceStatusInfo(source).kind)).length,
    0,
  )
  const MODULE_ORDER = ['wechat_mp', 'x_user', 'rss', 'reddit', 'github_repo', 'bilibili_up']
  const orderedGroups = [...groups].sort(
    (a, b) => {
      const ia = MODULE_ORDER.indexOf(a.platform)
      const ib = MODULE_ORDER.indexOf(b.platform)
      return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib)
    },
  )

  if (loading) {
    return <SubscriptionSkeleton />
  }

  if (error) {
    return (
      <section className="bg-card border border-border rounded-lg p-5 shadow-subtle">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-[15px] font-semibold text-foreground">订阅配置</h2>
            <p className="mt-2 text-sm text-destructive">{error}</p>
          </div>
          <button
            type="button"
            onClick={load}
            className="inline-flex h-9 items-center justify-center rounded-lg bg-accent px-3 text-[13px] font-semibold text-primary hover:bg-primary hover:text-white"
          >
            重试
          </button>
        </div>
      </section>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <button
            type="button"
            onClick={() => openWizard()}
            className="inline-flex h-9 items-center justify-center gap-1.5 rounded-[4px] border border-border bg-card px-3 text-[13px] font-semibold text-muted-foreground transition-colors hover:border-[var(--brand-border)] hover:text-foreground"
          >
            <Plus className="w-3.5 h-3.5" />
            添加信源
          </button>
          <label className="relative block sm:w-72">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <input
              aria-label="搜索信源"
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder="搜索名称 / source key"
              className="h-9 w-full rounded-[4px] border border-border bg-card pl-9 pr-3 text-[13px] text-foreground outline-none transition-colors placeholder:text-muted-foreground focus:border-[var(--brand-border)]"
            />
          </label>
        </div>
        <div className="text-[12px] text-muted-foreground tabular-nums">
          注册表快照时间 {latestUpdatedAt ? formatAdminDate(latestUpdatedAt) : '—'} · {formatAdminNumber(total)} 个
        </div>
      </div>

      <section className="grid gap-px overflow-hidden rounded-lg border border-border bg-border shadow-subtle sm:grid-cols-2 xl:grid-cols-4">
        <SummaryMetric label="已配置" value={formatAdminNumber(total)} helper="注册表信源" />
        <SummaryMetric
          label="本轮 X 覆盖"
          value={latestXRun ? `${latestXRun.attempted} / ${latestXRun.planned}` : '—'}
          helper={latestXRun ? `成功 ${latestXRun.succeeded}（${latestXRun.no_new} 无新增） · 失败 ${latestXRun.failed} · 漏抓 ${latestXRun.missed}` : '暂无运行记录'}
          tone={latestXRun?.missed ? 'danger' : 'default'}
        />
        <SummaryMetric label="需处理" value={formatAdminNumber(attentionCount)} helper="失败或正在重试" tone={attentionCount > 0 ? 'warning' : 'default'} />
        <div className="flex min-h-[88px] items-center justify-between gap-3 bg-card px-4 py-3">
          <div>
            <div className="text-[11px] text-muted-foreground">最近完成</div>
            <div className="mt-1 text-[14px] font-semibold text-foreground tabular-nums">{formatAdminDate(latestXRun?.finished_at)}</div>
          </div>
          <button
            type="button"
            onClick={() => {
              window.location.hash = '#admin/runs'
              window.dispatchEvent(new HashChangeEvent('hashchange'))
            }}
            className="rounded-[4px] border border-border px-2.5 py-1.5 text-[12px] font-semibold text-muted-foreground hover:border-[var(--brand-border)] hover:text-foreground"
          >
            查看运行
          </button>
        </div>
      </section>

      {missing.length > 0 && (
        <section className="rounded-lg border border-amber-300/60 bg-amber-50 px-4 py-3 text-[13px] text-amber-900">
          <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
            <div className="inline-flex items-center gap-2 font-semibold">
              <AlertTriangle className="w-4 h-4" />
              <span>语鲸侧有 {missing.length} 个未纳管订阅</span>
              <span className="font-normal text-amber-800">当前不会被抓取</span>
            </div>
            <button
              type="button"
              onClick={() => setShowReconcileList((prev) => !prev)}
              className="self-start rounded-md px-2.5 py-1.5 text-[12px] font-semibold text-amber-900 hover:bg-amber-100 md:self-auto"
            >
              查看并导入
            </button>
          </div>
          {showReconcileList && (
            <div className="mt-3 rounded-lg border border-amber-300/60 bg-card p-3">
              <div className="grid gap-2 md:grid-cols-2">
                {missing.map((item) => (
                  <div key={item.source_key} className="rounded-md border border-border bg-background px-3 py-2">
                    <div className="text-[13px] font-semibold text-foreground">{item.display_name}</div>
                    <div className="mt-0.5 text-[12px] text-muted-foreground">{item.source_key}</div>
                  </div>
                ))}
              </div>
              <button
                type="button"
                onClick={handleImportMissing}
                disabled={importingMissing}
                className="mt-3 inline-flex h-8 items-center justify-center gap-1.5 rounded-md bg-accent px-3 text-[12px] font-semibold text-primary hover:bg-primary hover:text-white disabled:opacity-50"
              >
                {importingMissing && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                导入全部
              </button>
            </div>
          )}
        </section>
      )}

      {groups.length === 0 ? (
        <section className="bg-card border border-border rounded-lg px-5 py-12 text-center shadow-subtle">
          <p className="text-sm font-medium text-foreground">注册表为空</p>
          <p className="mt-1 text-[12px] text-muted-foreground">用「添加信源」逐个添加公众号 / X / RSS 等信源。</p>
        </section>
      ) : (
        <section data-testid="source-module-grid" className="grid grid-cols-1 gap-4 min-[1280px]:grid-cols-2">
          {orderedGroups.map((group) => (
            <ModulePanel
              key={group.platform}
              group={group}
              onAdd={() => openWizard(group.platform as WizardPlatform)}
              onStatus={handleStatus}
              onDelete={handleDelete}
              savingSourceId={savingSourceId}
              latestXRun={latestXRun}
              xList={xList}
              syncingXList={syncingXList}
              onSyncXList={handleSyncXList}
              searchQuery={searchQuery}
            />
          ))}
        </section>
      )}

      <section data-testid="algo-params-panel" className="bg-card border border-border rounded-lg p-5 shadow-subtle">
        <div className="flex flex-col gap-1 md:flex-row md:items-center md:justify-between">
          <div>
            <h2 className="text-[15px] font-semibold text-foreground">算法源区块</h2>
            <p className="mt-1 text-[12px] text-muted-foreground">算法源没有名单，不能添加名字</p>
          </div>
          <button
            type="button"
            onClick={handleSaveAlgo}
            disabled={savingAlgo || !algoParams}
            className="inline-flex h-9 items-center justify-center gap-1.5 rounded-lg bg-accent px-3 text-[13px] font-semibold text-primary hover:bg-primary hover:text-white disabled:opacity-50"
          >
            {savingAlgo ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Settings2 className="w-3.5 h-3.5" />}
            保存参数
          </button>
        </div>
        {algoError && <p className="mt-3 text-[12px] text-destructive">{algoError}</p>}
        <div className="mt-4 grid grid-cols-[repeat(auto-fill,minmax(230px,1fr))] gap-3">
          {ALGO_PARAM_SPECS.map((spec) => (
            <label key={spec.key} className="rounded-lg border border-border bg-background p-3">
              <span className="flex items-center justify-between gap-2">
                <span className="text-[13px] font-semibold text-foreground">{spec.label}</span>
                <span className="rounded-md bg-accent px-2 py-0.5 text-[11px] font-semibold text-primary">
                  配置生效
                </span>
              </span>
              <span className="mt-1 block text-[12px] text-muted-foreground">{spec.note}</span>
              <input
                aria-label={spec.label}
                type="number"
                min={spec.min}
                max={spec.max}
                value={algoDraft[spec.key]}
                onChange={(event) => {
                  setAlgoDraft((prev) => ({ ...prev, [spec.key]: event.target.value }))
                  setAlgoError(null)
                }}
                className="mt-3 h-9 w-full rounded-md border border-border bg-card px-3 text-sm font-semibold text-foreground tabular-nums outline-none focus:border-primary"
              />
              <span className="mt-1 block text-[11px] text-muted-foreground">范围 {spec.min}-{spec.max}</span>
            </label>
          ))}
        </div>
      </section>

      <AddSourceWizard
        open={wizardOpen}
        initialPlatform={wizardPlatform}
        onClose={() => setWizardOpen(false)}
        onCreated={handleCreated}
      />
    </div>
  )
}

type SourceFilter = 'attention' | 'waiting' | 'all' | 'paused'

function SummaryMetric({
  label,
  value,
  helper,
  tone = 'default',
}: {
  label: string
  value: string
  helper: string
  tone?: 'default' | 'warning' | 'danger'
}) {
  return (
    <div className="min-h-[88px] bg-card px-4 py-3">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className={cn(
        'mt-1 text-[20px] font-semibold tabular-nums',
        tone === 'danger' ? 'text-destructive' : tone === 'warning' ? 'text-amber-700' : 'text-foreground',
      )}>{value}</div>
      <div className="mt-0.5 text-[11px] text-muted-foreground">{helper}</div>
    </div>
  )
}

function ModulePanel({
  group,
  onAdd,
  onStatus,
  onDelete,
  savingSourceId,
  latestXRun,
  xList,
  syncingXList,
  onSyncXList,
  searchQuery,
}: {
  group: AdminSourceGroup
  onAdd: () => void
  onStatus: (source: AdminSource, status: 'active' | 'paused') => void
  onDelete: (source: AdminSource) => void
  savingSourceId: number | null
  latestXRun: AdminXRunSummary | null
  xList: AdminXListStatus | null
  syncingXList: boolean
  onSyncXList: (full: boolean) => void
  searchQuery: string
}) {
  const [filter, setFilter] = useState<SourceFilter>('attention')
  const [openMenuId, setOpenMenuId] = useState<number | null>(null)
  const [selectedSource, setSelectedSource] = useState<AdminSource | null>(null)
  const previousSourceCount = useRef(group.sources.length)
  useEffect(() => {
    if (group.sources.length > previousSourceCount.current) setFilter('all')
    previousSourceCount.current = group.sources.length
  }, [group.sources.length])
  const attentionCount = group.sources.filter(sourceNeedsAttention).length
  const waitingCount = group.sources.filter(sourceIsWaiting).length
  const pausedCount = group.sources.filter((source) => source.status === 'paused').length
  const query = searchQuery.trim().toLocaleLowerCase()
  const visibleSources = group.sources
    .filter((source) => {
      if (query) {
        return `${source.display_name} ${source.source_key}`.toLocaleLowerCase().includes(query)
      }
      if (filter === 'attention') return sourceNeedsAttention(source)
      if (filter === 'waiting') return sourceIsWaiting(source)
      if (filter === 'paused') return source.status === 'paused'
      return true
    })
    .sort((a, b) => {
      const severity = { error: 0, warning: 1, muted: 2, ok: 3 }
      const statusDelta = severity[sourceStatusInfo(a).kind] - severity[sourceStatusInfo(b).kind]
      return statusDelta || (a.display_name || a.source_key).localeCompare(b.display_name || b.source_key, 'zh-CN')
    })

  const filters: Array<{ key: SourceFilter; label: string; count: number }> = [
    { key: 'attention', label: '需处理', count: attentionCount },
    { key: 'waiting', label: '待首次验证', count: waitingCount },
    { key: 'all', label: '全部', count: group.sources.length },
    { key: 'paused', label: '已停用', count: pausedCount },
  ]

  return (
    <div
      data-testid={`module-card-${group.platform}`}
      className="flex h-[min(520px,calc(100dvh-230px))] min-h-[360px] flex-col overflow-hidden rounded-lg border border-border bg-card shadow-subtle"
    >
      <div className="flex flex-none items-start justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <h3 className="text-[14px] font-semibold text-foreground">{platformLabel(group.platform)}</h3>
            <span className="text-[11px] text-muted-foreground tabular-nums">{group.sources.length} 已配置</span>
            {attentionCount > 0 && (
              <span className="rounded-md bg-amber-50 px-2 py-0.5 text-[11px] font-semibold text-amber-700 tabular-nums">{attentionCount} 需处理</span>
            )}
          </div>
          {group.platform === 'x_user' && (
            <div className="mt-1 text-[11px] text-muted-foreground tabular-nums">
              本轮覆盖 {latestXRun ? `${latestXRun.attempted}/${latestXRun.planned}` : '待首轮验证'}
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={onAdd}
          className="inline-flex h-8 flex-none items-center gap-1 rounded-[4px] border border-border bg-card px-3 text-[12px] font-semibold text-muted-foreground transition-colors hover:border-[var(--brand-border)] hover:text-foreground"
        >
          <Plus className="h-3 w-3" />
          添加
        </button>
      </div>

      {group.platform === 'x_user' && (
        <div data-testid="x-list-status" className="flex flex-none flex-wrap items-center justify-between gap-2 border-b border-border bg-background/60 px-3 py-2">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
              <span className="font-semibold text-foreground">X List 抓取</span>
              <span className="rounded-md bg-secondary px-2 py-0.5 font-semibold text-foreground tabular-nums">
                {xList ? `${xList.synced_count} / ${xList.registry_count} 已同步` : '状态待加载'}
              </span>
              {(xList?.pending_count ?? 0) > 0 && (
                <span className="rounded-md bg-amber-50 px-2 py-0.5 font-semibold text-amber-700 tabular-nums">{xList?.pending_count} List 待同步</span>
              )}
              {(xList?.pending_count ?? 0) > 0 && (
                <span className="rounded-md bg-secondary px-2 py-0.5 font-semibold text-muted-foreground">分组搜索兜底</span>
              )}
            </div>
            <div className="mt-0.5 truncate text-[10px] text-muted-foreground tabular-nums">
              最近对账 {formatAdminDate(xList?.last_synced_at)} · {(xList?.pending_count ?? 0) > 0 ? '未同步账号仍按配置抓取' : '每轮聚合抓取'}
            </div>
            {(xList?.lists?.length ?? 0) > 0 && (
              <div className="mt-1.5 flex max-w-full flex-wrap gap-1">
                {xList?.lists?.map((item) => (
                  <a
                    key={item.key}
                    href={item.list_url}
                    target="_blank"
                    rel="noreferrer"
                    aria-label={`打开 ${item.name}`}
                    title={`${item.name} · ${item.synced_count}/${item.registry_count} 已同步`}
                    className={cn(
                      'inline-flex h-6 max-w-full items-center rounded-[4px] border px-2 text-[10px] font-semibold tabular-nums transition-colors hover:text-foreground',
                      item.pending_count > 0
                        ? 'border-amber-200 bg-amber-50 text-amber-700'
                        : 'border-border bg-card text-muted-foreground',
                    )}
                  >
                    <span className="max-w-[120px] truncate">{item.name.replace(/^i2a · /, '')}</span>
                    <span className="ml-1 flex-none">{item.synced_count}/{item.registry_count}</span>
                  </a>
                ))}
              </div>
            )}
          </div>
          <div className="flex flex-none items-center gap-1">
            {xList?.list_url && (
              <a
                href={xList.list_url}
                target="_blank"
                rel="noreferrer"
                aria-label="打开 X List"
                className="inline-flex h-8 items-center rounded-[4px] px-2.5 text-[11px] font-semibold text-muted-foreground hover:bg-secondary hover:text-foreground"
              >
                打开 List
              </a>
            )}
            <button
              type="button"
              aria-label={(xList?.pending_count ?? 0) > 0 ? `同步 ${xList?.pending_count} 个待同步账号` : '重新对账全部 X 账号'}
              onClick={() => onSyncXList((xList?.pending_count ?? 0) === 0)}
              disabled={syncingXList || !xList?.configured}
              className="inline-flex h-8 items-center gap-1 rounded-[4px] border border-border bg-card px-2.5 text-[11px] font-semibold text-muted-foreground transition-colors hover:border-[var(--brand-border)] hover:text-foreground disabled:opacity-50"
            >
              {syncingXList ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
              {(xList?.pending_count ?? 0) > 0 ? '同步' : '重新对账'}
            </button>
          </div>
        </div>
      )}

      <div className="flex flex-none gap-1 overflow-x-auto border-b border-border px-3 py-2">
        {filters.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => setFilter(item.key)}
            className={cn(
              'h-7 flex-none rounded-[4px] px-2.5 text-[11px] font-semibold transition-colors',
              !query && filter === item.key ? 'bg-secondary text-foreground' : 'text-muted-foreground hover:bg-secondary/70 hover:text-foreground',
            )}
          >
            {item.label} {item.count}
          </button>
        ))}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
        {visibleSources.length === 0 ? (
          <div className="flex min-h-40 flex-col items-center justify-center px-4 text-center">
            <CheckCircle2 className="h-5 w-5 text-muted-foreground/60" />
            <p className="mt-2 text-[13px] font-medium text-foreground">{query ? '没有匹配的信源' : filter === 'attention' ? '本轮全部信源已完成' : '当前筛选没有信源'}</p>
            {!query && filter === 'attention' && group.sources.length > 0 && (
              <button type="button" onClick={() => setFilter('all')} className="mt-2 text-[12px] font-semibold text-primary hover:underline">
                查看全部
              </button>
            )}
          </div>
        ) : visibleSources.map((source) => {
          const status = sourceStatusInfo(source)
          const paused = source.status === 'paused'
          const saving = savingSourceId === source.id
          const name = source.display_name || source.source_key
          return (
            <div
              key={source.id}
              data-testid={`source-row-${source.id}`}
              className={cn(
                'relative border-b border-border px-4 py-3 transition-colors last:border-b-0 hover:bg-background',
                status.kind === 'error' && 'border-l-2 border-l-destructive bg-destructive/[0.025]',
                paused && 'bg-muted/20',
              )}
            >
              <div className="flex min-w-0 items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                    <span title={name} className={cn('max-w-full truncate text-[13px] font-semibold', paused ? 'text-muted-foreground' : 'text-foreground')}>{name}</span>
                    <span className={cn('inline-flex flex-none rounded-md px-2 py-0.5 text-[11px] font-semibold leading-tight', status.cls)}>{status.label}</span>
                  </div>
                  <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[11px] text-muted-foreground tabular-nums">
                    <span title={source.source_key} className="max-w-[45%] truncate">{source.source_key}</span>
                    <span>·</span>
                    <span>最近成功 {formatAdminDate(source.health?.last_fetched_at)}</span>
                    <span>·</span>
                    <span>近7日 <strong className="font-semibold text-foreground">{formatAdminNumber(source.health?.inserted_7d)}</strong></span>
                  </div>
                </div>
                <button
                  type="button"
                  aria-label={`更多操作 ${name}`}
                  aria-expanded={openMenuId === source.id}
                  onClick={() => setOpenMenuId((current) => current === source.id ? null : source.id)}
                  className="inline-flex h-11 w-11 flex-none items-center justify-center rounded-md text-muted-foreground hover:bg-secondary hover:text-foreground"
                >
                  <MoreHorizontal className="h-4 w-4" />
                </button>
              </div>
              {openMenuId === source.id && (
                <div className="absolute right-4 top-12 z-20 w-36 rounded-md border border-border bg-card p-1 shadow-lg">
                  <button type="button" onClick={() => { setSelectedSource(source); setOpenMenuId(null) }} className="w-full rounded px-2.5 py-2 text-left text-[12px] font-medium text-foreground hover:bg-secondary">查看详情</button>
                  <button
                    type="button"
                    onClick={() => {
                      setOpenMenuId(null)
                      onStatus(source, source.status === 'paused' || source.status === 'broken' ? 'active' : 'paused')
                    }}
                    disabled={saving}
                    className="w-full rounded px-2.5 py-2 text-left text-[12px] font-medium text-foreground hover:bg-secondary disabled:opacity-50"
                  >
                    {source.status === 'broken' ? '重新启用' : source.status === 'paused' ? '启用' : '停用'}
                  </button>
                  <button type="button" onClick={() => { setOpenMenuId(null); onDelete(source) }} disabled={saving} className="w-full rounded px-2.5 py-2 text-left text-[12px] font-medium text-destructive hover:bg-destructive/10 disabled:opacity-50">删除</button>
                </div>
              )}
            </div>
          )
        })}
      </div>

      {selectedSource && (
        <SourceDetailDrawer source={selectedSource} onClose={() => setSelectedSource(null)} />
      )}
    </div>
  )
}

function SourceDetailDrawer({ source, onClose }: { source: AdminSource; onClose: () => void }) {
  const status = sourceStatusInfo(source)
  const attempt = source.health?.latest_attempt
  return (
    <div className="fixed inset-0 z-[80] bg-black/20" role="presentation" onMouseDown={onClose}>
      <aside className="ml-auto flex h-full w-full max-w-md flex-col border-l border-border bg-card shadow-2xl" aria-label={`${source.display_name || source.source_key} 详情`} onMouseDown={(event) => event.stopPropagation()}>
        <div className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <h3 className="truncate text-[15px] font-semibold text-foreground">{source.display_name || source.source_key}</h3>
            <p className="mt-1 truncate text-[12px] text-muted-foreground">{source.source_key}</p>
          </div>
          <button type="button" aria-label="关闭详情" onClick={onClose} className="inline-flex h-9 w-9 items-center justify-center rounded-md text-muted-foreground hover:bg-secondary hover:text-foreground"><X className="h-4 w-4" /></button>
        </div>
        <dl className="grid grid-cols-[110px_1fr] gap-x-4 gap-y-4 overflow-y-auto px-5 py-5 text-[13px]">
          <dt className="text-muted-foreground">当前状态</dt><dd><span className={cn('rounded-md px-2 py-0.5 text-[11px] font-semibold', status.cls)}>{status.label}</span></dd>
          <dt className="text-muted-foreground">最近成功</dt><dd className="text-foreground tabular-nums">{formatAdminDate(source.health?.last_fetched_at)}</dd>
          <dt className="text-muted-foreground">近7日入库</dt><dd className="text-foreground tabular-nums">{formatAdminNumber(source.health?.inserted_7d)}</dd>
          <dt className="text-muted-foreground">连续失败</dt><dd className="text-foreground tabular-nums">{formatAdminNumber(source.health?.consecutive_failures)}</dd>
          <dt className="text-muted-foreground">最近运行</dt><dd className="text-foreground tabular-nums">{attempt ? `#${attempt.run_id}` : '—'}</dd>
          <dt className="text-muted-foreground">尝试次数</dt><dd className="text-foreground tabular-nums">{formatAdminNumber(attempt?.attempts)}</dd>
          <dt className="text-muted-foreground">错误类型</dt><dd className="break-words text-foreground">{attempt?.error_code || '—'}</dd>
          <dt className="text-muted-foreground">错误详情</dt><dd className="break-words text-foreground">{attempt?.error || '—'}</dd>
        </dl>
      </aside>
    </div>
  )
}

function sourceNeedsAttention(source: AdminSource) {
  const kind = sourceStatusInfo(source).kind
  return kind === 'error' || kind === 'warning'
}

function sourceIsWaiting(source: AdminSource) {
  return source.status !== 'paused' && !source.health?.last_fetched_at && source.health?.latest_attempt?.outcome !== 'missed'
}

function AddSourceWizard({
  open,
  initialPlatform,
  onClose,
  onCreated,
}: {
  open: boolean
  initialPlatform?: WizardPlatform
  onClose: () => void
  onCreated: (source: AdminSource) => void
}) {
  const [step, setStep] = useState<SourceWizardStep>(1)
  const [platform, setPlatform] = useState<WizardPlatform>('wechat_mp')
  const [sourceKey, setSourceKey] = useState('')
  const [validation, setValidation] = useState<AdminSourceValidateResponse | null>(null)
  const [validationError, setValidationError] = useState<string | null>(null)
  const [validating, setValidating] = useState(false)
  const [creating, setCreating] = useState(false)
  // 公众号：按名字搜索选号（语鲸）
  const [wechatQuery, setWechatQuery] = useState('')
  const [wechatResults, setWechatResults] = useState<AdminWechatSearchChannel[] | null>(null)
  const [wechatSearching, setWechatSearching] = useState(false)
  const [wechatError, setWechatError] = useState<string | null>(null)
  const [picked, setPicked] = useState<AdminWechatSearchChannel | null>(null)

  useEffect(() => {
    if (!open) return
    setStep(initialPlatform ? 2 : 1)
    setPlatform(initialPlatform ?? 'wechat_mp')
    setSourceKey('')
    setValidation(null)
    setValidationError(null)
    setValidating(false)
    setCreating(false)
    setWechatQuery('')
    setWechatResults(null)
    setWechatSearching(false)
    setWechatError(null)
    setPicked(null)
  }, [open])

  if (!open) return null

  const platformOption = SOURCE_PLATFORM_OPTIONS.find((option) => option.value === platform)
  const canValidate = sourceKey.trim().length > 0 && !validating

  async function handleValidate() {
    if (!canValidate) return
    setValidating(true)
    setValidation(null)
    setValidationError(null)
    try {
      const res = await validateAdminSource({ platform, source_key: sourceKey.trim() })
      setValidation(res)
      if (res.status === 'ok' || res.status === 'empty') {
        setStep(3)
      }
    } catch (err) {
      setValidationError(err instanceof Error ? err.message : '校验失败')
    } finally {
      setValidating(false)
    }
  }

  async function handleWechatSearch() {
    const q = wechatQuery.trim()
    if (!q || wechatSearching) return
    setWechatSearching(true)
    setWechatError(null)
    setWechatResults(null)
    try {
      const res = await searchWechatSources(q)
      setWechatResults(res.channels)
    } catch (err) {
      setWechatError(err instanceof Error ? err.message : '语鲸搜索失败，请稍后重试')
    } finally {
      setWechatSearching(false)
    }
  }

  function pickWechatChannel(channel: AdminWechatSearchChannel) {
    if (channel.already_in_registry) return
    setPicked(channel)
    setSourceKey(channel.channel_id)
    setStep(3)
  }

  async function handleCreate() {
    const isWechat = platform === 'wechat_mp'
    if (isWechat) {
      if (!picked) return
    } else if (!validation || validation.status === 'deferred') {
      return
    }
    setCreating(true)
    try {
      const res = await createAdminSource(
        isWechat && picked
          ? {
              platform: 'wechat_mp',
              source_key: picked.channel_id,
              display_name: picked.name,
              status: 'active',
              config_json: { backend: 'lingowhale' },
              validated_at: new Date().toISOString(),
            }
          : {
              platform,
              source_key: sourceKey.trim(),
              display_name: validation?.display_name || sourceKey.trim(),
              status: 'active',
              validated_at: new Date().toISOString(),
            },
      )
      onCreated(res.source)
      toast.success('信源已入库')
      onClose()
    } catch (err) {
      setValidationError(err instanceof Error ? err.message : '入库失败')
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/40 p-4">
      <section
        role="dialog"
        aria-modal="true"
        aria-label="添加信源"
        className="flex max-h-[92vh] w-full max-w-[720px] flex-col overflow-hidden rounded-lg border border-border bg-card shadow-xl"
      >
        <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
          <div>
            <div className="text-[12px] font-semibold text-muted-foreground">第 {step} 步 / 共 3 步</div>
            <h2 className="mt-1 text-[15px] font-semibold text-foreground">添加信源</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground"
            aria-label="关闭"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="min-h-0 overflow-auto px-5 py-4">
          {step === 1 && (
            <div className="grid gap-3 sm:grid-cols-2">
              {SOURCE_PLATFORM_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="radio"
                  aria-checked={platform === option.value}
                  disabled={option.disabled}
                  onClick={() => setPlatform(option.value)}
                  className={cn(
                    'rounded-lg border border-border bg-background p-3 text-left transition-colors',
                    platform === option.value && 'border-primary bg-accent',
                    option.disabled && 'cursor-not-allowed opacity-50',
                  )}
                >
                  <span className="block text-[13px] font-semibold text-foreground">{option.label}</span>
                  <span className="mt-1 block text-[12px] text-muted-foreground">{option.helper}</span>
                </button>
              ))}
            </div>
          )}

          {step === 2 && platform === 'wechat_mp' && (
            <div className="space-y-4">
              <div className="flex gap-2">
                <input
                  aria-label="公众号名称"
                  value={wechatQuery}
                  onChange={(event) => setWechatQuery(event.target.value)}
                  onKeyDown={(event) => { if (event.key === 'Enter') handleWechatSearch() }}
                  className="h-10 flex-1 rounded-md border border-border bg-background px-3 text-sm text-foreground outline-none focus:border-primary"
                  placeholder="输入公众号名字，如 机器之心"
                />
                <button
                  type="button"
                  onClick={handleWechatSearch}
                  disabled={!wechatQuery.trim() || wechatSearching}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 text-[13px] font-semibold text-primary hover:bg-primary hover:text-white disabled:opacity-50"
                >
                  {wechatSearching && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                  搜索
                </button>
              </div>
              <p className="text-[12px] text-muted-foreground">经语鲸搜索公众号，选中即纳入抓取（无需订阅、无需 RSS URL）。</p>
              {wechatError && (
                <p className="rounded-md bg-destructive/10 px-3 py-2 text-[12px] font-medium text-destructive">{wechatError}</p>
              )}
              {wechatResults && wechatResults.length === 0 && !wechatSearching && (
                <p className="px-1 py-4 text-sm text-muted-foreground">没搜到匹配的公众号，换个名字试试。</p>
              )}
              {wechatResults && wechatResults.length > 0 && (
                <div className="max-h-[46vh] divide-y divide-border overflow-auto rounded-lg border border-border">
                  {wechatResults.map((ch) => (
                    <button
                      key={ch.channel_id}
                      type="button"
                      disabled={ch.already_in_registry}
                      onClick={() => pickWechatChannel(ch)}
                      className={cn(
                        'flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-background',
                        ch.already_in_registry && 'cursor-not-allowed opacity-60',
                      )}
                    >
                      {ch.avatar_url ? (
                        <img src={ch.avatar_url} alt="" className="h-9 w-9 flex-none rounded-md object-cover" />
                      ) : (
                        <div className="h-9 w-9 flex-none rounded-md bg-secondary" />
                      )}
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="truncate text-[13px] font-semibold text-foreground">{ch.name}</span>
                          {ch.already_in_registry && (
                            <span className="flex-none rounded bg-secondary px-1.5 py-0.5 text-[10px] font-semibold text-muted-foreground">已在注册表</span>
                          )}
                          {ch.has_subscribed && !ch.already_in_registry && (
                            <span className="flex-none rounded bg-accent px-1.5 py-0.5 text-[10px] font-semibold text-primary">语鲸已订阅</span>
                          )}
                        </div>
                        {ch.description && <div className="mt-0.5 line-clamp-1 text-[12px] text-muted-foreground">{ch.description}</div>}
                        <div className="mt-0.5 text-[11px] text-muted-foreground tabular-nums">7日更新 {ch.last_7d_count} · 订阅 {ch.subscriber_count}</div>
                      </div>
                      {!ch.already_in_registry && <ChevronRight className="w-4 h-4 flex-none text-muted-foreground" />}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}

          {step === 2 && platform !== 'wechat_mp' && (
            <div className="space-y-4">
              <label className="block">
                <span className="text-[12px] font-semibold text-muted-foreground">source_key</span>
                <input
                  aria-label="source_key"
                  value={sourceKey}
                  onChange={(event) => setSourceKey(event.target.value)}
                  className="mt-1.5 h-10 w-full rounded-md border border-border bg-background px-3 text-sm text-foreground outline-none focus:border-primary"
                  placeholder={platformPlaceholder(platform)}
                />
              </label>
              <p className="text-[12px] text-muted-foreground">{platformOption?.helper}</p>
              {validationError && (
                <p className="rounded-md bg-destructive/10 px-3 py-2 text-[12px] font-medium text-destructive">{validationError}</p>
              )}
              {validation?.status === 'deferred' && (
                <div className="rounded-lg border border-amber-300/60 bg-amber-50 p-3 text-[13px] text-amber-900">
                  <span className="mb-2 inline-flex rounded-md bg-amber-100 px-2 py-0.5 text-[11px] font-semibold text-amber-900">后端 deferred</span>
                  <p>{validation.reason || '该平台当前由后端异步或外部凭证路径处理。'}</p>
                </div>
              )}
            </div>
          )}

          {step === 3 && platform === 'wechat_mp' && picked && (
            <div className="space-y-4">
              <div className="flex items-center gap-3 rounded-lg border border-border bg-background p-3">
                {picked.avatar_url ? (
                  <img src={picked.avatar_url} alt="" className="h-11 w-11 flex-none rounded-md object-cover" />
                ) : (
                  <div className="h-11 w-11 flex-none rounded-md bg-secondary" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <CheckCircle2 className="w-4 h-4 flex-none text-primary" />
                    <span className="truncate text-[14px] font-semibold text-foreground">{picked.name}</span>
                  </div>
                  {picked.description && <p className="mt-1 line-clamp-2 text-[12px] text-muted-foreground">{picked.description}</p>}
                  <p className="mt-1 text-[11px] text-muted-foreground tabular-nums">7日更新 {picked.last_7d_count} · 订阅 {picked.subscriber_count} · channel {picked.channel_id}</p>
                </div>
              </div>
              <p className="text-[12px] text-muted-foreground">确认后该公众号纳入注册表，下一轮抓取即开始按名单拉取其文章。</p>
              {validationError && (
                <p className="rounded-md bg-destructive/10 px-3 py-2 text-[12px] font-medium text-destructive">{validationError}</p>
              )}
            </div>
          )}

          {step === 3 && platform !== 'wechat_mp' && validation && (
            <div className="space-y-4">
              <div className="rounded-lg border border-border bg-background p-3">
                <div className="flex items-center gap-2">
                  <CheckCircle2 className="w-4 h-4 text-primary" />
                  <span className="text-[13px] font-semibold text-foreground">{validation.display_name || sourceKey}</span>
                  <span className="rounded-md bg-accent px-2 py-0.5 text-[11px] font-semibold text-primary">{validation.status}</span>
                </div>
                {validation.warning && <p className="mt-2 text-[12px] text-amber-700">{validation.warning}</p>}
              </div>
              <div className="rounded-lg border border-border overflow-hidden">
                <div className="bg-secondary px-3 py-2 text-[12px] font-semibold text-muted-foreground">试抓预览</div>
                {validation.preview.length === 0 ? (
                  <p className="px-3 py-4 text-sm text-muted-foreground">暂无预览样本</p>
                ) : (
                  <div className="divide-y divide-border">
                    {validation.preview.slice(0, 3).map((item, index) => (
                      <PreviewRow key={`${item.url || item.title || index}`} item={item} />
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        <div className="flex items-center justify-between gap-3 border-t border-border px-5 py-4">
          <button
            type="button"
            onClick={() => {
              if (step === 1) onClose()
              else setStep((prev) => (prev === 3 ? 2 : 1))
            }}
            className="rounded-md px-3 py-2 text-[13px] font-semibold text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            {step === 1 ? '取消' : '上一步'}
          </button>
          {step === 1 && (
            <button
              type="button"
              onClick={() => setStep(2)}
              className="rounded-lg bg-accent px-3 py-2 text-[13px] font-semibold text-primary hover:bg-primary hover:text-white"
            >
              下一步
            </button>
          )}
          {step === 2 && platform !== 'wechat_mp' && (
            <button
              type="button"
              onClick={handleValidate}
              disabled={!canValidate}
              className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-[13px] font-semibold text-primary hover:bg-primary hover:text-white disabled:opacity-50"
            >
              {validating && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
              校验
            </button>
          )}
          {step === 3 && (
            <button
              type="button"
              onClick={handleCreate}
              disabled={creating}
              className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-[13px] font-semibold text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {creating && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
              确认入库
            </button>
          )}
        </div>
      </section>
    </div>
  )
}

function PreviewRow({ item }: { item: AdminSourcePreviewItem }) {
  return (
    <div className="px-3 py-2.5">
      <div className="text-[13px] font-medium text-foreground">{item.title || item.url || '未命名条目'}</div>
      <div className="mt-1 text-[11.5px] text-muted-foreground tabular-nums">{formatAdminDate(item.published_at)}</div>
      {item.summary && <div className="mt-1 line-clamp-2 text-[12px] text-muted-foreground">{item.summary}</div>}
    </div>
  )
}

function SubscriptionSkeleton() {
  return (
    <div className="space-y-6">
      <section className="bg-card border border-border rounded-lg overflow-hidden shadow-subtle">
        <div className="flex items-center justify-between gap-4 px-5 py-4">
          <div className="space-y-2">
            <SkeletonBlock className="w-28 h-4" />
            <SkeletonBlock className="w-64 h-3" />
          </div>
          <SkeletonBlock className="w-24 h-4" />
        </div>
        <div className="border-t border-border px-5 py-4 space-y-3">
          {Array.from({ length: 4 }).map((_, index) => (
            <SkeletonBlock key={index} className="h-10 rounded-md" />
          ))}
        </div>
      </section>
      <SkeletonBlock className="h-52 rounded-lg" />
    </div>
  )
}

function sourceStatusInfo(source: AdminSource): {
  label: string
  cls: string
  kind: 'ok' | 'error' | 'warning' | 'muted'
} {
  if (source.status === 'paused') return { label: '已停用', cls: 'bg-muted text-muted-foreground', kind: 'muted' }
  if (source.status === 'pending') return { label: '待放量', cls: 'bg-amber-50 text-amber-700', kind: 'warning' }
  if (source.status === 'not_fetched' && source.platform === 'bilibili_up') {
    return { label: '管线未接入', cls: 'bg-muted text-muted-foreground', kind: 'muted' }
  }
  const latestAttempt = source.health?.latest_attempt
  const lastSuccessMs = Date.parse(source.health?.last_fetched_at || '')
  const attemptFinishedMs = Date.parse(latestAttempt?.finished_at || '')
  const attempt = Number.isFinite(lastSuccessMs)
    && Number.isFinite(attemptFinishedMs)
    && lastSuccessMs > attemptFinishedMs
    ? null
    : latestAttempt
  if (attempt?.outcome === 'missed') return { label: '本轮漏抓', cls: 'bg-destructive/10 text-destructive', kind: 'error' }
  if (source.status === 'broken') return { label: '连续失败', cls: 'bg-destructive/10 text-destructive', kind: 'error' }
  if (attempt?.outcome === 'failed' || attempt?.outcome === 'interrupted') {
    return { label: '本轮失败', cls: 'bg-amber-50 text-amber-700', kind: 'warning' }
  }
  const failures = source.health?.consecutive_failures ?? 0
  if (failures > 0 || attempt?.outcome === 'retrying') {
    return { label: `抓取重试中 · ${Math.max(failures, attempt?.attempts ?? 1)}`, cls: 'bg-amber-50 text-amber-700', kind: 'warning' }
  }
  if (attempt?.outcome === 'success' && (attempt.new_count ?? 0) > 0) {
    return { label: `成功 · 新增 ${attempt.new_count}`, cls: 'bg-accent text-accent-foreground', kind: 'ok' }
  }
  if (attempt?.outcome === 'success' || attempt?.outcome === 'no_new') {
    return { label: '成功 · 无新增', cls: 'bg-secondary text-muted-foreground', kind: 'muted' }
  }
  if (!source.health?.last_fetched_at) {
    return { label: '待首次验证', cls: 'bg-muted text-muted-foreground', kind: 'muted' }
  }
  if (source.health?.inserted_7d === 0) return { label: '近7日无更新', cls: 'bg-secondary text-muted-foreground', kind: 'muted' }
  return { label: '正常', cls: 'bg-accent text-accent-foreground', kind: 'ok' }
}

function formatAdminDate(value?: string | null) {
  if (!value) return '—'
  return new Date(value).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function formatAdminNumber(value?: number | null) {
  if (value === null || value === undefined) return '—'
  return new Intl.NumberFormat('zh-CN').format(value)
}

function platformLabel(platform: string) {
  if (platform === 'wechat_mp' || platform === 'lingowhale') return '公众号'
  if (platform === 'x_user') return 'X'
  if (platform === 'rss') return 'RSS'
  if (platform === 'reddit') return 'Reddit'
  if (platform === 'github_repo') return 'GitHub'
  if (platform === 'bilibili_up') return 'B站'
  return platform
}

function platformPlaceholder(platform: WizardPlatform) {
  if (platform === 'x_user') return 'openai'
  if (platform === 'rss') return 'https://example.com/feed.xml'
  if (platform === 'reddit') return 'OpenAI'
  if (platform === 'github_repo') return 'owner/repo'
  if (platform === 'bilibili_up') return '123456'
  if (platform === 'wechat_mp') return 'https://wechat2rss.xlab.app/feed/xxx.xml'
  return 'channel_id'
}

function replaceSourceInGroups(groups: AdminSourceGroup[], source: AdminSource): AdminSourceGroup[] {
  return groups.map((group) => {
    if (group.platform !== source.platform) return group
    return {
      ...group,
      sources: group.sources.map((item) => (item.id === source.id ? source : item)),
    }
  })
}

function removeSourceFromGroups(groups: AdminSourceGroup[], sourceId: number): AdminSourceGroup[] {
  return groups
    .map((group) => ({ ...group, sources: group.sources.filter((source) => source.id !== sourceId) }))
    .filter((group) => group.sources.length > 0)
}

function appendSourceToGroups(groups: AdminSourceGroup[], source: AdminSource): AdminSourceGroup[] {
  const existing = groups.find((group) => group.platform === source.platform)
  if (!existing) return [...groups, { platform: source.platform, sources: [source] }]
  return groups.map((group) => {
    if (group.platform !== source.platform) return group
    const hasSource = group.sources.some((item) => item.id === source.id)
    return {
      ...group,
      sources: hasSource
        ? group.sources.map((item) => (item.id === source.id ? source : item))
        : [...group.sources, source],
    }
  })
}

function latestSourceUpdatedAt(groups: AdminSourceGroup[]) {
  let latest: string | null = null
  for (const group of groups) {
    for (const source of group.sources) {
      if (!latest || new Date(source.updated_at).getTime() > new Date(latest).getTime()) latest = source.updated_at
    }
  }
  return latest
}

function emptyAlgoDraft(): Record<keyof AdminSourceAlgoParams, string> {
  return ALGO_PARAM_SPECS.reduce((acc, spec) => {
    acc[spec.key] = ''
    return acc
  }, {} as Record<keyof AdminSourceAlgoParams, string>)
}

function algoParamsToDraft(params: AdminSourceAlgoParams): Record<keyof AdminSourceAlgoParams, string> {
  return ALGO_PARAM_SPECS.reduce((acc, spec) => {
    const value = params[spec.key]
    acc[spec.key] = value === null || value === undefined ? '' : String(value)
    return acc
  }, {} as Record<keyof AdminSourceAlgoParams, string>)
}

function parseAlgoDraft(draft: Record<keyof AdminSourceAlgoParams, string>):
  | { params: AdminSourceAlgoParams }
  | { error: string } {
  const params = {} as AdminSourceAlgoParams
  for (const spec of ALGO_PARAM_SPECS) {
    const value = Number.parseInt(draft[spec.key], 10)
    if (!Number.isInteger(value) || value < spec.min || value > spec.max) {
      return { error: `${spec.key} 范围 ${spec.min}-${spec.max}` }
    }
    params[spec.key] = value
  }
  return { params }
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
    <div className="border border-border bg-background rounded-md px-3 py-3 min-w-0 min-h-[72px]">
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
            <SkeletonBlock key={index} className="h-[72px] rounded-md" />
          ))}
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
          <SkeletonBlock className="h-32 rounded-md" />
          <SkeletonBlock className="h-32 rounded-md" />
        </div>
        <SkeletonBlock className="h-[280px] rounded-md" />
      </div>
    </div>
  )
}

function SkeletonBlock({ className }: { className?: string }) {
  return <div className={cn('animate-pulse rounded bg-muted', className)} />
}
