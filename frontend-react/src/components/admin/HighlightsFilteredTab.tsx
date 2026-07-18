import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent, MouseEvent as ReactMouseEvent, ReactNode } from 'react'
import { AlertTriangle, Loader2, RefreshCw, Search } from 'lucide-react'

import {
  getAdminHighlightsFunnel,
  getAdminHighlightsFunnelRows,
  setAdminHighlightOverride,
  submitFeedback,
} from '../../lib/api'
import type {
  AdminHighlightsClusterMember,
  AdminHighlightsClusterRow,
  AdminHighlightsDays,
  AdminHighlightsDims,
  AdminHighlightsDisplay,
  AdminHighlightsFunnelResponse,
  AdminHighlightsFunnelRowsResponse,
  AdminHighlightsItemFeedbackKind,
  AdminHighlightsItemRow,
  AdminHighlightsManualDisplay,
  AdminHighlightsRowFeedback,
  AdminHighlightsStage,
} from '../../lib/api'
import { cn, safeExternalUrl } from '../../lib/utils'
import { requireAuth } from '../shared/AuthGate'
import { Tooltip } from '../shared/Tooltip'

type HighlightsFilteredTabProps = { reloadSignal: number }

type EditorAction = AdminHighlightsItemFeedbackKind | 'force_show' | 'force_hide'
type FeedbackEditor = { key: string; action: EditorAction } | null
type ClusterOverrideState = {
  manual_display: AdminHighlightsManualDisplay
  feedback: AdminHighlightsRowFeedback
}

const PAGE_SIZE_OPTIONS = [20, 50, 100] as const
type PageSize = typeof PAGE_SIZE_OPTIONS[number]
const PAGE_SIZE_STORAGE_KEY = 'admin-highlights-funnel:page-size'
const COLUMN_WIDTH_STORAGE_PREFIX = 'admin-highlights-funnel:column-width:'
const REFRESH_MINUTES = 10
const EMPTY_DIMS: AdminHighlightsDims = {
  authority: null,
  substance: null,
  novelty: null,
  timeliness: null,
  audience_fit: null,
}
const TAGS = [
  ['', '全部'],
  ['products', '产品'],
  ['efficiency_tools', '效率工具'],
  ['coding', 'coding'],
  ['models', '模型'],
  ['tech', '技术洞察'],
  ['tutorials', '教程'],
  ['industry', '行业'],
  ['startup', '创业'],
  ['other', '其他'],
] as const
const PANORAMA_COLUMNS = [
  { id: 'time', label: '时间', tip: '簇内最新成员入库时间', defaultWidth: 82, minWidth: 74 },
  { id: 'item_title', label: 'item（标题 · 来源）', tip: '窗口内簇成员，按 score10 降序', defaultWidth: 235, minWidth: 160 },
  { id: 'item_score', label: 'item 分', tip: '0-10 原值，保留一位小数', defaultWidth: 62, minWidth: 62, className: 'text-right' },
  { id: 'item_blocked', label: 'item 拦截', tip: 'veto 或未过打分闸原因', defaultWidth: 145, minWidth: 110 },
  { id: 'item_feedback', label: 'item 反馈', tip: '收录/排除是 item 级纯标注', defaultWidth: 155, minWidth: 140 },
  { id: 'cluster_title', label: '簇标题', tip: '点击打开事件簇详情', defaultWidth: 180, minWidth: 150 },
  { id: 'cluster_score', label: '簇分', tip: '簇内最高 score10；前台外显分 = ×10', defaultWidth: 65, minWidth: 65, className: 'text-right' },
  { id: 'cluster_blocked', label: '簇拦截', tip: '行级 stage 与 5 站口径同源', defaultWidth: 175, minWidth: 130 },
  { id: 'display', label: '展示', tip: '当前生产展示判定', defaultWidth: 72, minWidth: 72, className: 'text-center' },
  { id: 'cluster_feedback', label: '簇反馈', tip: '展示/不展示会 override 并同步记标注', defaultWidth: 190, minWidth: 170 },
] as const
type PanoramaColumn = typeof PANORAMA_COLUMNS[number]
type PanoramaColumnId = PanoramaColumn['id']
type PanoramaColumnWidths = Record<PanoramaColumnId, number>

export function HighlightsFilteredTab({ reloadSignal }: HighlightsFilteredTabProps) {
  const [days, setDays] = useState<AdminHighlightsDays>(1)
  const [view, setView] = useState<'panorama' | 'anomaly'>('panorama')
  const [display, setDisplay] = useState<AdminHighlightsDisplay>('all')
  const [stage, setStage] = useState<AdminHighlightsStage | ''>('')
  const [tag, setTag] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState<PageSize>(readPageSize)
  const [searchDraft, setSearchDraft] = useState('')
  const [query, setQuery] = useState('')
  const [funnel, setFunnel] = useState<AdminHighlightsFunnelResponse | null>(null)
  const [rows, setRows] = useState<AdminHighlightsFunnelRowsResponse | null>(null)
  const [funnelLoading, setFunnelLoading] = useState(true)
  const [rowsLoading, setRowsLoading] = useState(true)
  const [funnelError, setFunnelError] = useState<string | null>(null)
  const [rowsError, setRowsError] = useState<string | null>(null)
  const [unavailable, setUnavailable] = useState(false)
  const [itemFeedback, setItemFeedback] = useState<Record<string, AdminHighlightsRowFeedback>>({})
  const [clusterOverrides, setClusterOverrides] = useState<Record<number, ClusterOverrideState>>({})
  const [feedbackErrors, setFeedbackErrors] = useState<Record<string, string>>({})
  const [editor, setEditor] = useState<FeedbackEditor>(null)
  const [note, setNote] = useState('')
  const [pendingKey, setPendingKey] = useState<string | null>(null)
  const funnelRequest = useRef(0)
  const rowsRequest = useRef(0)

  const loadFunnel = useCallback(async () => {
    const version = ++funnelRequest.current
    setFunnelLoading(true)
    setFunnelError(null)
    try {
      const result = await getAdminHighlightsFunnel({ days, q: query, tag })
      if (version === funnelRequest.current) setFunnel(result)
    } catch (error) {
      if (version !== funnelRequest.current) return
      setFunnel(null)
      setFunnelError(errorMessage(error))
    } finally {
      if (version === funnelRequest.current) setFunnelLoading(false)
    }
  }, [days, query, tag])

  const loadRows = useCallback(async () => {
    const version = ++rowsRequest.current
    setRowsLoading(true)
    setRowsError(null)
    try {
      const result = await getAdminHighlightsFunnelRows({
        view,
        days,
        q: query,
        tag,
        display,
        stage,
        page,
        limit: pageSize,
      })
      if (version !== rowsRequest.current) return
      setRows(result)
      setUnavailable(false)
    } catch (error) {
      if (version !== rowsRequest.current) return
      setRows(null)
      if (errorStatus(error) === 501) setUnavailable(true)
      else setRowsError(errorMessage(error))
    } finally {
      if (version === rowsRequest.current) setRowsLoading(false)
    }
  }, [days, display, page, pageSize, query, stage, tag, view])

  useEffect(() => {
    void loadFunnel()
    return () => { funnelRequest.current += 1 }
  }, [loadFunnel, reloadSignal])

  useEffect(() => {
    void loadRows()
    return () => { rowsRequest.current += 1 }
  }, [loadRows, reloadSignal])

  function resetPage() {
    setPage(1)
    setEditor(null)
    setNote('')
  }

  function selectStage(next: AdminHighlightsStage) {
    setView('panorama')
    setStage(next)
    setDisplay('all')
    resetPage()
  }

  function selectDisplay(next: AdminHighlightsDisplay) {
    setView('panorama')
    setDisplay(next)
    setStage('')
    resetPage()
  }

  function selectTag(next: string) {
    setView('panorama')
    setTag(next)
    resetPage()
  }

  function selectPageSize(next: string) {
    const value = Number(next) as PageSize
    if (!PAGE_SIZE_OPTIONS.includes(value)) return
    setPageSize(value)
    setStoredValue(PAGE_SIZE_STORAGE_KEY, String(value))
    resetPage()
  }

  function runSearch(event?: FormEvent) {
    event?.preventDefault()
    setQuery(searchDraft.trim())
    resetPage()
  }

  function openEditor(key: string, action: EditorAction) {
    if (pendingKey || !requireAuth('反馈')) return
    setEditor({ key, action })
    setNote('')
    setFeedbackErrors((current) => ({ ...current, [key]: '' }))
  }

  function closeEditor() {
    setEditor(null)
    setNote('')
  }

  async function updateItem(member: AdminHighlightsClusterMember, action: AdminHighlightsItemFeedbackKind, text?: string) {
    const key = itemKey(member.id)
    if (pendingKey || !requireAuth('反馈')) return
    const previous = itemFeedback[key] ?? member.feedback
    const optimistic = previous.kind === action
      ? { kind: null, note: null }
      : { kind: action, note: text || null }
    setItemFeedback((current) => ({ ...current, [key]: optimistic }))
    setFeedbackErrors((current) => ({ ...current, [key]: '' }))
    setPendingKey(key)
    try {
      const result = await submitFeedback(member.id, action, text)
      setItemFeedback((current) => ({
        ...current,
        [key]: result.active ? { kind: action, note: text || null } : { kind: null, note: null },
      }))
      closeEditor()
    } catch {
      setItemFeedback((current) => ({ ...current, [key]: previous }))
      setFeedbackErrors((current) => ({ ...current, [key]: '反馈失败，请重试' }))
    } finally {
      setPendingKey(null)
    }
  }

  async function updateCluster(row: AdminHighlightsClusterRow, action: 'force_show' | 'force_hide' | 'clear', text?: string) {
    const key = clusterKey(row.id)
    if (pendingKey || !requireAuth('反馈')) return
    const previous = clusterOverrides[row.id] ?? {
      manual_display: row.manual_display,
      feedback: row.feedback,
    }
    const optimisticManual = action === 'clear' ? null : action
    setClusterOverrides((current) => ({
      ...current,
      [row.id]: {
        manual_display: optimisticManual,
        feedback: action === 'clear'
          ? { kind: null, note: null }
          : { kind: action === 'force_show' ? 'should_feature' : 'irrelevant', note: text || null },
      },
    }))
    setFeedbackErrors((current) => ({ ...current, [key]: '' }))
    setPendingKey(key)
    try {
      const result = await setAdminHighlightOverride(row.id, action, text)
      setClusterOverrides((current) => ({
        ...current,
        [row.id]: {
          manual_display: result.manual_display,
          feedback: { kind: result.feedback_kind, note: result.feedback_note },
        },
      }))
      closeEditor()
    } catch {
      setClusterOverrides((current) => ({ ...current, [row.id]: previous }))
      setFeedbackErrors((current) => ({ ...current, [key]: '反馈失败，请重试' }))
    } finally {
      setPendingKey(null)
    }
  }

  const totalPages = Math.max(1, Math.ceil((rows?.total ?? 0) / pageSize))

  return (
    <div className="min-w-0 space-y-3">
      <FunnelStrip
        data={funnel}
        loading={funnelLoading}
        stage={stage}
        display={display}
        anomaly={view === 'anomaly'}
        onStage={selectStage}
        onDisplayed={() => selectDisplay('shown')}
        onAnomaly={() => { setView('anomaly'); setStage(''); resetPage() }}
      />

      {funnelError ? <InlineError title="漏斗计数加载失败" detail={funnelError} onRetry={() => void loadFunnel()} /> : null}

      <div className="space-y-2 border-y border-border bg-card px-3 py-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="w-9 text-[11px] text-muted-foreground">时间</span>
          <PillGroup
            options={([1, 3, 7] as const).map((value) => ({ value, label: `${value} 天` }))}
            value={days}
            onChange={(value) => { setDays(value); resetPage() }}
          />
          <span className="ml-2 text-[11px] text-muted-foreground">展示</span>
          <PillGroup
            options={[
              { value: 'all' as const, label: '全部' },
              { value: 'shown' as const, label: '已展示' },
              { value: 'hidden' as const, label: '未展示' },
            ]}
            value={display}
            onChange={selectDisplay}
          />
          <form className="ml-auto min-w-[240px] flex-1 sm:max-w-[360px]" role="search" onSubmit={runSearch}>
            <label className="relative block">
              <span className="sr-only">搜索全景表</span>
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <input
                type="search"
                aria-label="搜索全景表"
                placeholder="搜标题，回车筛选"
                value={searchDraft}
                onChange={(event) => {
                  setSearchDraft(event.target.value)
                  if (!event.target.value) { setQuery(''); resetPage() }
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault()
                    runSearch()
                  }
                }}
                className="h-8 w-full rounded-[4px] border border-border bg-background pl-8 pr-3 text-[13px] outline-none focus:border-primary"
              />
            </label>
          </form>
        </div>
        <div className="flex items-center gap-2 overflow-x-auto">
          <span className="w-9 shrink-0 text-[11px] text-muted-foreground">标签</span>
          {TAGS.map(([value, label]) => (
            <button
              key={value || 'all'}
              type="button"
              aria-pressed={tag === value}
              onClick={() => selectTag(value)}
              className={pillClass(tag === value)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {unavailable ? (
        <StatePanel>该视图仅在生产数据模式可用</StatePanel>
      ) : rowsLoading ? (
        <StatePanel><span className="inline-flex items-center gap-2"><Loader2 className="h-4 w-4 animate-spin" />加载全景表…</span></StatePanel>
      ) : rowsError ? (
        <InlineError title="漏斗列表加载失败" detail={rowsError} onRetry={() => void loadRows()} />
      ) : !rows || rows.items.length === 0 ? (
        <StatePanel>
          {query
            ? '没有匹配的条目。搜不到通常意味着它未入库——请检查信源池'
            : view === 'anomaly' ? `近 ${days} 天没有异常` : `当前筛选下近 ${days} 天没有内容`}
        </StatePanel>
      ) : rows.granularity === 'item' ? (
        <AnomalyTable rows={rows.items} />
      ) : (
        <PanoramaTable
          rows={rows.items}
          displayThreshold={rows.display_threshold}
          itemFeedback={itemFeedback}
          clusterOverrides={clusterOverrides}
          editor={editor}
          note={note}
          pendingKey={pendingKey}
          errors={feedbackErrors}
          onOpenEditor={openEditor}
          onNote={setNote}
          onCancel={closeEditor}
          onItemSubmit={(member, action, text) => void updateItem(member, action, text)}
          onClusterSubmit={(row, action, text) => void updateCluster(row, action, text)}
        />
      )}

      {!rowsLoading && !rowsError && !unavailable && rows?.granularity === 'cluster' ? (
        <div className="flex flex-wrap items-center gap-3 border-t border-border pt-2 text-[12px] text-muted-foreground">
          <span>{`共 ${rows.total} 簇 · 第 ${rows.page}/${totalPages} 页 · 每页 ${pageSize} 簇`}</span>
          <label className="inline-flex items-center gap-1.5">
            每页
            <select aria-label="每页条数" value={pageSize} onChange={(event) => selectPageSize(event.target.value)} className="h-7 rounded-[4px] border border-border bg-background px-2 text-foreground">
              {PAGE_SIZE_OPTIONS.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
          </label>
          <span>排序：簇内最新成员时间倒序</span>
          {totalPages > 1 ? (
            <div className="ml-auto flex gap-2">
              <button type="button" disabled={page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))} className="rounded-[4px] border border-border px-2 py-1 disabled:opacity-40">上一页</button>
              <button type="button" disabled={page >= totalPages} onClick={() => setPage((value) => Math.min(totalPages, value + 1))} className="rounded-[4px] border border-border px-2 py-1 disabled:opacity-40">下一页</button>
            </div>
          ) : null}
        </div>
      ) : null}
      <p className="text-[11px] text-muted-foreground">判定按当前规则现算，历史条目可能与当时判定不一致</p>
    </div>
  )
}

function FunnelStrip({
  data,
  loading,
  stage,
  display,
  anomaly,
  onStage,
  onDisplayed,
  onAnomaly,
}: {
  data: AdminHighlightsFunnelResponse | null
  loading: boolean
  stage: AdminHighlightsStage | ''
  display: AdminHighlightsDisplay
  anomaly: boolean
  onStage: (stage: AdminHighlightsStage) => void
  onDisplayed: () => void
  onAnomaly: () => void
}) {
  const stations = new Map(data?.stations.map((entry) => [entry.key, entry.count]) ?? [])
  const diffs = new Map(data?.diffs.map((entry) => [entry.key, entry.count]) ?? [])
  const count = (value?: number) => loading ? '—' : String(value ?? '—')
  const readOnly = (label: string, value?: number) => (
    <Tooltip content="本站粒度只作口径观测，不作为筛选入口">
      <div tabIndex={0} aria-label={`${label} ${count(value)}`} className="px-1 py-1 text-center focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]">
        <div className="font-mono text-[20px] leading-5 tabular-nums text-foreground">{count(value)}</div>
        <div className="mt-1 text-[11px] text-muted-foreground">{label}</div>
      </div>
    </Tooltip>
  )
  const blocked = (key: 'scoring' | 'summary' | 'display', nextStage: AdminHighlightsStage) => (
    <Tooltip content={key === 'scoring' ? '计数为 item 数；点击筛出含 drop 成员的簇' : '点击在同一张表内筛出该阶段'}>
      <button
        type="button"
        aria-label={`被拦 ${count(diffs.get(key))}`}
        aria-pressed={stage === nextStage}
        onClick={() => onStage(nextStage)}
        className={cn('self-center rounded-[4px] border px-2 py-1 font-mono text-[11px] tabular-nums', stage === nextStage ? 'border-[var(--a-crit)] a-pill-crit' : 'border-transparent a-pill-crit')}
      >
        被拦 {count(diffs.get(key))}
      </button>
    </Tooltip>
  )
  return (
    <div className="overflow-x-auto border-y border-border bg-card px-3 py-2" aria-label="5 站计数条">
      <div className="flex min-w-max items-center gap-2">
        {readOnly('入库', stations.get('ingested'))}<Flow />
        {blocked('scoring', 'blocked_scoring')}<Flow />
        {readOnly('打分通过', stations.get('scored'))}<Flow />
        {readOnly('聚类', stations.get('clustered'))}<Flow />
        {blocked('summary', 'blocked_summary')}<Flow />
        {readOnly('总结闸', stations.get('summarized'))}<Flow />
        {blocked('display', 'blocked_display')}<Flow />
        <button type="button" aria-label={`已展示 ${count(stations.get('displayed'))}`} aria-pressed={display === 'shown' && !stage} onClick={onDisplayed} className={cn('px-1 py-1 text-center', display === 'shown' && !stage && 'bg-[var(--brand-soft)]')}>
          <div className="font-mono text-[20px] leading-5 tabular-nums">{count(stations.get('displayed'))}</div>
          <div className="mt-1 text-[11px] text-muted-foreground">已展示</div>
        </button>
        {data?.gate_disabled ? <span className="text-[11px] text-muted-foreground">展示闸未启用</span> : null}
        <button type="button" aria-label={`异常 ${count(data?.anomalies_count)}`} aria-pressed={anomaly} onClick={onAnomaly} className={cn('ml-3 rounded-[4px] border border-border px-2 py-1 text-[12px]', anomaly && 'border-[var(--a-warn)] a-pill-warn')}>
          异常 <span className="font-mono tabular-nums">{count(data?.anomalies_count)}</span>
        </button>
      </div>
    </div>
  )
}

function PanoramaTable({
  rows,
  displayThreshold,
  itemFeedback,
  clusterOverrides,
  editor,
  note,
  pendingKey,
  errors,
  onOpenEditor,
  onNote,
  onCancel,
  onItemSubmit,
  onClusterSubmit,
}: {
  rows: AdminHighlightsClusterRow[]
  displayThreshold: number | null
  itemFeedback: Record<string, AdminHighlightsRowFeedback>
  clusterOverrides: Record<number, ClusterOverrideState>
  editor: FeedbackEditor
  note: string
  pendingKey: string | null
  errors: Record<string, string>
  onOpenEditor: (key: string, action: EditorAction) => void
  onNote: (value: string) => void
  onCancel: () => void
  onItemSubmit: (member: AdminHighlightsClusterMember, action: AdminHighlightsItemFeedbackKind, note?: string) => void
  onClusterSubmit: (row: AdminHighlightsClusterRow, action: 'force_show' | 'force_hide' | 'clear', note?: string) => void
}) {
  const [columnWidths, setColumnWidths] = useState<PanoramaColumnWidths>(readColumnWidths)
  const tableWidth = PANORAMA_COLUMNS.reduce((total, column) => total + columnWidths[column.id], 0)

  function startColumnResize(column: PanoramaColumn, event: ReactMouseEvent<HTMLElement>) {
    event.preventDefault()
    const startX = event.clientX
    const startWidth = columnWidths[column.id]
    let nextWidth = startWidth
    const move = (moveEvent: MouseEvent) => {
      nextWidth = Math.max(column.minWidth, startWidth + moveEvent.clientX - startX)
      setColumnWidths((current) => ({ ...current, [column.id]: nextWidth }))
    }
    const stop = () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', stop)
      setStoredValue(`${COLUMN_WIDTH_STORAGE_PREFIX}${column.id}`, String(nextWidth))
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', stop)
  }

  function resetColumnWidth(column: PanoramaColumn) {
    setColumnWidths((current) => ({ ...current, [column.id]: column.defaultWidth }))
    setStoredValue(`${COLUMN_WIDTH_STORAGE_PREFIX}${column.id}`, String(column.defaultWidth))
  }

  return (
    <div data-testid="funnel-panorama-scroll" className="max-w-full overflow-x-auto border border-border bg-card [scrollbar-gutter:stable]">
      <table aria-label="精选漏斗全景表" className="table-fixed text-[12px]" style={{ width: tableWidth, minWidth: '100%' }}>
        <colgroup>
          {PANORAMA_COLUMNS.map((column) => <col key={column.id} data-column-id={column.id} style={{ width: columnWidths[column.id] }} />)}
        </colgroup>
        <thead>
          <tr className="border-b border-border text-left font-semibold text-muted-foreground">
            {PANORAMA_COLUMNS.map((column) => (
              <HeaderCell
                key={column.id}
                column={column}
                width={columnWidths[column.id]}
                onResizeStart={startColumnResize}
                onReset={resetColumnWidth}
              />
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const members = row.members.length ? row.members : [emptyMember(row.id)]
            const override = clusterOverrides[row.id] ?? { manual_display: row.manual_display, feedback: row.feedback }
            return members.map((member, index) => {
              const itemState = itemFeedback[itemKey(member.id)] ?? member.feedback
              const clusterStateKey = clusterKey(row.id)
              return (
                <tr key={`${row.id}-${member.id}`} data-testid={`funnel-item-row-${member.id}`} className={cn('align-top', index === 0 ? 'border-t-2 border-border' : 'border-t border-border')}>
                  {index === 0 ? <td data-testid={`cluster-time-${row.id}`} rowSpan={members.length} className="px-2 py-2.5 font-mono tabular-nums text-muted-foreground">{formatTime(row.latest_at)}</td> : null}
                  <td className="px-2 py-2.5">
                    <ItemTitle member={member} />
                  </td>
                  <td className={cn('px-2 py-2.5 text-right font-mono tabular-nums', scoreClass(member.score10))}>{formatScore(member.score10)}</td>
                  <td data-testid="item-block-reason" className="px-2 py-2.5">{itemBlockedReason(member)}</td>
                  <td className="px-2 py-2.5">
                    <ItemFeedbackControls
                      member={member}
                      feedback={itemState}
                      editor={editor}
                      note={note}
                      pending={pendingKey === itemKey(member.id)}
                      error={errors[itemKey(member.id)]}
                      onOpen={onOpenEditor}
                      onNote={onNote}
                      onCancel={onCancel}
                      onSubmit={onItemSubmit}
                    />
                  </td>
                  {index === 0 ? (
                    <>
                      <td data-testid={`cluster-title-${row.id}`} rowSpan={members.length} className="border-l border-border bg-[color-mix(in_srgb,var(--background)_45%,transparent)] px-2 py-2.5">
                        <a href={`#cluster=${row.id}`} target="_blank" rel="noopener noreferrer" aria-label={`打开事件簇：${row.title || '未命名事件簇'}`} className="text-left font-semibold hover:text-primary hover:underline">{row.title || '—'}</a>
                        <div><span className="mt-1 inline-block rounded-[4px] bg-[var(--a-unknown-soft)] px-1.5 text-[10.5px] text-[var(--a-unknown)]">{categoryLabel(row.dominant_category)}</span></div>
                      </td>
                      <td rowSpan={members.length} className="bg-[color-mix(in_srgb,var(--background)_45%,transparent)] px-2 py-2.5 text-right">
                        <ClusterScore row={row} />
                      </td>
                      <td rowSpan={members.length} className="bg-[color-mix(in_srgb,var(--background)_45%,transparent)] px-2 py-2.5">
                        <ClusterReason row={row} threshold={displayThreshold} />
                      </td>
                      <td rowSpan={members.length} className="bg-[color-mix(in_srgb,var(--background)_45%,transparent)] px-2 py-2.5 text-center text-[15px]">
                        <DisplayState row={row} manual={override.manual_display} />
                      </td>
                      <td data-testid={`cluster-feedback-${row.id}`} rowSpan={members.length} className="bg-[color-mix(in_srgb,var(--background)_45%,transparent)] px-2 py-2.5">
                        <ClusterFeedbackControls
                          row={row}
                          state={override}
                          editor={editor}
                          note={note}
                          pending={pendingKey === clusterStateKey}
                          error={errors[clusterStateKey]}
                          onOpen={onOpenEditor}
                          onNote={onNote}
                          onCancel={onCancel}
                          onSubmit={onClusterSubmit}
                        />
                      </td>
                    </>
                  ) : null}
                </tr>
              )
            })
          })}
        </tbody>
      </table>
    </div>
  )
}

function ItemFeedbackControls({ member, feedback, editor, note, pending, error, onOpen, onNote, onCancel, onSubmit }: {
  member: AdminHighlightsClusterMember
  feedback: AdminHighlightsRowFeedback
  editor: FeedbackEditor
  note: string
  pending: boolean
  error?: string
  onOpen: (key: string, action: EditorAction) => void
  onNote: (value: string) => void
  onCancel: () => void
  onSubmit: (member: AdminHighlightsClusterMember, action: AdminHighlightsItemFeedbackKind, note?: string) => void
}) {
  const key = itemKey(member.id)
  const action = editor?.key === key && (editor.action === 'should_feature' || editor.action === 'should_drop') ? editor.action : null
  const button = (kind: AdminHighlightsItemFeedbackKind, label: string) => {
    const active = feedback.kind === kind
    return <button type="button" aria-pressed={active} disabled={pending} onClick={() => active ? onSubmit(member, kind) : onOpen(key, kind)} className={feedbackButtonClass(active, kind === 'should_feature')}>{active ? `✓ ${label}` : label}</button>
  }
  return (
    <div className="space-y-1.5">
      <div className="flex gap-1.5">{button('should_feature', '收录')}{button('should_drop', '排除')}</div>
      {action ? <FeedbackInput ariaLabel="item 反馈备注" value={note} pending={pending} onNote={onNote} onCancel={onCancel} onSubmit={() => onSubmit(member, action, note || undefined)} /> : null}
      {feedback.note ? <p className="text-[10.5px] text-muted-foreground">{feedback.note}</p> : null}
      {error ? <p className="text-[11px] a-text-crit">{error}</p> : null}
    </div>
  )
}

function ClusterFeedbackControls({ row, state, editor, note, pending, error, onOpen, onNote, onCancel, onSubmit }: {
  row: AdminHighlightsClusterRow
  state: ClusterOverrideState
  editor: FeedbackEditor
  note: string
  pending: boolean
  error?: string
  onOpen: (key: string, action: EditorAction) => void
  onNote: (value: string) => void
  onCancel: () => void
  onSubmit: (row: AdminHighlightsClusterRow, action: 'force_show' | 'force_hide' | 'clear', note?: string) => void
}) {
  const key = clusterKey(row.id)
  const action = editor?.key === key && (editor.action === 'force_show' || editor.action === 'force_hide') ? editor.action : null
  const button = (kind: 'force_show' | 'force_hide', label: string) => {
    const active = state.manual_display === kind
    return <button type="button" aria-pressed={active} disabled={pending} onClick={() => active ? onSubmit(row, 'clear') : onOpen(key, kind)} className={feedbackButtonClass(active, kind === 'force_show')}>{active ? `✓ ${label}` : label}</button>
  }
  return (
    <div className="space-y-1.5">
      <div className="flex gap-1.5">{button('force_show', '展示')}{button('force_hide', '不展示')}</div>
      {action ? <FeedbackInput ariaLabel="簇反馈备注" value={note} pending={pending} onNote={onNote} onCancel={onCancel} onSubmit={() => onSubmit(row, action, note || undefined)} /> : null}
      {state.manual_display === 'force_show' ? <p className="text-[10.5px] a-text-ok">{`已强制展示，已记标注；${REFRESH_MINUTES} 分钟内生效`}</p> : null}
      {state.manual_display === 'force_hide' ? <p className="text-[10.5px] a-text-crit">已强制下架，已记标注</p> : null}
      {state.feedback.note ? <p className="text-[10.5px] text-muted-foreground">{state.feedback.note}</p> : null}
      {error ? <p className="text-[11px] a-text-crit">{error}</p> : null}
    </div>
  )
}

function FeedbackInput({ ariaLabel, value, pending, onNote, onCancel, onSubmit }: { ariaLabel: string; value: string; pending: boolean; onNote: (value: string) => void; onCancel: () => void; onSubmit: () => void }) {
  return <input autoFocus type="text" aria-label={ariaLabel} placeholder="可选：补充判断依据" maxLength={500} value={value} disabled={pending} onChange={(event) => onNote(event.target.value.slice(0, 500))} onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); onSubmit() } else if (event.key === 'Escape') { event.preventDefault(); onCancel() } }} className="h-7 w-full rounded-[4px] border border-border bg-background px-2 text-[11px] outline-none focus:border-primary" />
}

function ClusterScore({ row }: { row: AdminHighlightsClusterRow }) {
  const dims = row.deciding_item.dims || EMPTY_DIMS
  const labels: Array<[string, number | null]> = [
    ['权威', dims.authority], ['实质', dims.substance], ['新颖', dims.novelty], ['时效', dims.timeliness], ['受众', dims.audience_fit],
  ]
  const inputs = row.score_inputs || {}
  return (
    <Tooltip variant="rich" content={(
      <div className="w-[270px] space-y-2 text-left">
        <div><p className="text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">定分 item</p><p className="font-semibold">{row.deciding_item.title || '—'}</p></div>
        <div className="space-y-1">{labels.map(([label, value]) => <div key={label} className="grid grid-cols-[38px_1fr_14px] items-center gap-2"><span className="text-[11px]">{label}{' '}</span><span className="h-1.5 overflow-hidden rounded-full bg-secondary"><span className="block h-full bg-primary" style={{ width: `${Math.max(0, Math.min(3, value ?? 0)) / 3 * 100}%` }} /></span><span className="font-mono text-[11px]">{formatDim(value)}</span></div>)}</div>
        <p className="border-t border-border pt-2 text-[11px] text-[var(--ink-2)]">{row.deciding_item.reason || '无 LLM reason'}</p>
        <div className="flex flex-wrap gap-x-3 gap-y-1 font-mono text-[10.5px] text-muted-foreground"><span>max_q {formatInput(inputs.max_q)}</span><span>avg_q {formatInput(inputs.avg_q)}</span><span>过闸成员 {formatInput(inputs.scored_include_count)}</span><span>独立源 {formatInput(inputs.unique_source_count)}</span></div>
      </div>
    )}>
      <button type="button" aria-label={`簇分 ${formatScore(row.max_flag_score10)}，查看明细`} className={cn('border-b border-dotted border-[var(--brand-border)] font-mono tabular-nums', scoreClass(row.max_flag_score10))}>{formatScore(row.max_flag_score10)}</button>
    </Tooltip>
  )
}

function ClusterReason({ row, threshold }: { row: AdminHighlightsClusterRow; threshold: number | null }) {
  if (row.stage === 'pending') return <span className="inline-block rounded-[4px] px-1.5 py-0.5 a-pill-unknown">⏳ 处理中 · 打分中</span>
  if (row.blocked_reason === 'awaiting_why_read') return <span className="inline-block rounded-[4px] px-1.5 py-0.5 a-pill-unknown">⏳ 处理中 · why_read 生成中</span>
  if (row.stage === 'displayed') return <span className="text-muted-foreground">—</span>
  let text = row.blocked_reason || '总结闸未通过'
  if (row.blocked_reason === 'below_threshold') text = `score ${formatScore(row.max_flag_score10)} < 展示线 ${formatScore(threshold)}`
  else if (row.blocked_reason === 'all_members_dropped' || row.blocked_reason === 'summary_gate_filtered') text = '无达标成员'
  else if (row.blocked_reason === 'manual_hide') text = '人工下架'
  return <span className="inline-block rounded-[4px] px-1.5 py-0.5 a-pill-warn">{text}</span>
}

function DisplayState({ row, manual }: { row: AdminHighlightsClusterRow; manual: AdminHighlightsManualDisplay }) {
  const waiting = row.blocked_reason === 'awaiting_why_read'
  const shown = manual === 'force_hide' ? false : manual === 'force_show' && !waiting ? true : row.displayed
  return <span>{shown ? '✅' : '❌'}{manual ? <small className="mt-1 block text-[10px] font-semibold text-primary">{manual === 'force_show' ? '人工强制' : '人工下架'}</small> : null}</span>
}

function ItemTitle({ member }: { member: AdminHighlightsClusterMember }) {
  const external = safeExternalUrl(member.url)
  return <div><div className="font-medium">{external ? <a href={external} target="_blank" rel="noopener noreferrer" className="underline decoration-dotted underline-offset-4 hover:text-primary">{member.title || '—'}</a> : member.title || '—'}</div><div className="mt-1 text-[10.5px] text-muted-foreground">{[member.platform, member.author_name || member.source].filter(Boolean).join(' · ') || '—'}</div></div>
}

function AnomalyTable({ rows }: { rows: AdminHighlightsItemRow[] }) {
  return <div className="overflow-x-auto border border-border bg-card"><table aria-label="精选漏斗异常表" className="w-full min-w-[820px] text-[12px]"><thead><tr className="border-b border-border text-left text-muted-foreground"><th className="px-3 py-2">时间</th><th className="px-3 py-2">标题</th><th className="px-3 py-2">卡在哪步</th><th className="px-3 py-2">错误摘要</th></tr></thead><tbody>{rows.map((row) => <tr key={row.id} className="border-t border-border"><td className="px-3 py-2 font-mono">{formatTime(row.ingested_at)}</td><td className="px-3 py-2">{row.title || '—'}</td><td className="px-3 py-2">{row.stuck_at || '—'}</td><td className="px-3 py-2">{row.error_summary || row.reason || '—'}</td></tr>)}</tbody></table></div>
}

function HeaderCell({ column, width, onResizeStart, onReset }: {
  column: PanoramaColumn
  width: number
  onResizeStart: (column: PanoramaColumn, event: ReactMouseEvent<HTMLElement>) => void
  onReset: (column: PanoramaColumn) => void
}) {
  return (
    <th data-column-id={column.id} className={cn('relative whitespace-nowrap px-2 py-2', 'className' in column && column.className)}>
      <Tooltip content={column.tip}><button type="button" className="cursor-help text-left font-semibold underline decoration-dotted underline-offset-4">{column.label}</button></Tooltip>
      <span
        role="separator"
        aria-label={`调整 ${column.label}列宽`}
        aria-orientation="vertical"
        aria-valuemin={column.minWidth}
        aria-valuenow={width}
        onMouseDown={(event) => onResizeStart(column, event)}
        onDoubleClick={() => onReset(column)}
        className="absolute inset-y-0 right-0 z-10 w-2 cursor-col-resize select-none border-r border-transparent hover:border-primary"
      />
    </th>
  )
}

function PillGroup<T extends string | number>({ options, value, onChange }: { options: Array<{ value: T; label: string }>; value: T; onChange: (value: T) => void }) {
  return <div className="flex gap-1">{options.map((option) => <button key={option.value} type="button" aria-label={option.label} aria-pressed={value === option.value} onClick={() => onChange(option.value)} className={pillClass(value === option.value)}>{option.label}</button>)}</div>
}

function InlineError({ title, detail, onRetry }: { title: string; detail: string; onRetry: () => void }) {
  return <div className="flex items-center gap-3 border border-border bg-card px-4 py-3 text-[13px]"><AlertTriangle className="h-4 w-4 a-text-crit" /><div><p className="font-semibold">{title}</p><p className="text-[12px] text-muted-foreground">{detail}</p></div><button type="button" onClick={onRetry} className="ml-auto inline-flex items-center gap-1 rounded-[4px] border border-border px-2.5 py-1.5"><RefreshCw className="h-3.5 w-3.5" />重试</button></div>
}

function StatePanel({ children }: { children: ReactNode }) {
  return <div className="flex min-h-40 items-center justify-center border border-border bg-card px-6 py-12 text-center text-[13px] text-muted-foreground">{children}</div>
}

function Flow() { return <span className="text-muted-foreground/60" aria-hidden="true">→</span> }
function itemKey(id: string) { return `item:${id}` }
function clusterKey(id: number) { return `cluster:${id}` }
function pillClass(active: boolean) { return cn('h-7 shrink-0 rounded-full px-3 text-[11.5px] transition-colors', active ? 'bg-primary font-semibold text-primary-foreground' : 'bg-secondary text-muted-foreground hover:text-foreground') }
function feedbackButtonClass(active: boolean, positive: boolean) { return cn('rounded-[6px] border px-2 py-0.5 text-[11px] disabled:opacity-50', active ? positive ? 'border-[var(--a-ok)] a-pill-ok' : 'border-[var(--a-crit)] a-pill-crit' : 'border-border bg-card text-muted-foreground hover:border-[var(--brand-border)] hover:text-primary') }
function formatScore(value: number | null) { return value === null || !Number.isFinite(value) ? '—' : value.toFixed(1) }
function formatDim(value: number | null) { return value === null || !Number.isFinite(value) ? '—' : String(value) }
function formatInput(value: unknown) { return typeof value === 'number' && Number.isFinite(value) ? String(value) : '—' }
function scoreClass(value: number | null) { if (value === null) return 'text-muted-foreground'; if (value >= 7) return 'a-text-ok'; if (value >= 5) return 'a-text-warn'; return 'a-text-crit' }
function formatTime(value: string | null) { if (!value) return '—'; const date = new Date(value); if (Number.isNaN(date.getTime())) return '—'; return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).replace(' ', '\n') }
function itemBlockedReason(member: AdminHighlightsClusterMember) { const veto = humanVeto(member.veto); if (veto) return <span className="inline-block rounded-[4px] px-1.5 py-0.5 a-pill-crit">veto · {veto}</span>; if (member.verdict === 'drop' && member.score10 !== null) return `score ${formatScore(member.score10)} 未过打分闸`; return member.reason || '—' }
function humanVeto(veto: string | null) { if (!veto || veto === 'none') return null; return ({ marketing: '营销通稿', rumor_unverified: '传闻未证实', flamewar: '引战', engagement_bait: '互动诱饵' } as Record<string, string>)[veto] ?? veto }
function categoryLabel(value: string) { return TAGS.find(([id]) => id === value)?.[1] ?? value }
function emptyMember(clusterId: number): AdminHighlightsClusterMember { return { id: `cluster-${clusterId}-empty`, title: null, url: null, platform: null, source: null, author_name: null, fetched_at: null, verdict: null, score10: null, dims: EMPTY_DIMS, veto: null, uncertainty: null, reason: null, feedback: { kind: null, note: null } } }
function errorStatus(error: unknown) { return typeof error === 'object' && error !== null && 'status' in error ? Number((error as { status?: number }).status) : null }
function errorMessage(error: unknown) { return error instanceof Error ? error.message : '加载失败' }
function readPageSize(): PageSize { const value = Number(getStoredValue(PAGE_SIZE_STORAGE_KEY)); return PAGE_SIZE_OPTIONS.includes(value as PageSize) ? value as PageSize : 20 }
function readColumnWidths(): PanoramaColumnWidths { return Object.fromEntries(PANORAMA_COLUMNS.map((column) => { const stored = Number(getStoredValue(`${COLUMN_WIDTH_STORAGE_PREFIX}${column.id}`)); return [column.id, Number.isFinite(stored) && stored >= column.minWidth ? stored : column.defaultWidth] })) as PanoramaColumnWidths }
function getStoredValue(key: string) { try { return typeof window === 'undefined' ? null : window.localStorage.getItem(key) } catch { return null } }
function setStoredValue(key: string, value: string) { try { window.localStorage.setItem(key, value) } catch { /* localStorage 不可用时仅保留当前会话状态 */ } }
