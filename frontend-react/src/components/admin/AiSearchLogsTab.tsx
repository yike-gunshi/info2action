import { Fragment, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import { getAiSearchJob, getAiSearchJobs } from '../../lib/api/admin'
import type { AiSearchJobDetail, AiSearchJobsResponse, AiSearchJobStatus, AiSearchJobUsage } from '../../lib/api/admin'
import { renderMarkdownLite } from '../../lib/markdown-lite'

const PAGE_SIZE = 50
const STATUS_LABELS: Record<AiSearchJobStatus, string> = {
  retrieving: '找资料', insufficient: '资料不足',
  queued: '排队', running: '运行', done: '完成', failed: '失败', unavailable: '不可用',
}
const NUMBER_CELL = 'px-3 py-2 text-right font-mono tabular-nums whitespace-nowrap'
const LABEL = 'text-[11px] font-semibold uppercase tracking-[0.09em] text-muted-foreground'
const BUTTON = 'rounded-[4px] border border-border px-2 py-1 disabled:opacity-40'

export function AiSearchLogsTab({ reloadSignal = 0 }: { reloadSignal?: number }) {
  const [page, setPage] = useState(1)
  const [retry, setRetry] = useState(0)
  const [data, setData] = useState<AiSearchJobsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [unavailable, setUnavailable] = useState(false)
  const [expandedId, setExpandedId] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setLoading(true)
    setError(null)
    setUnavailable(false)
    setExpandedId(null)
    setData(null)
    getAiSearchJobs({ limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE })
      .then((result) => { if (active) setData(result) })
      .catch((reason: unknown) => {
        if (!active) return
        if (typeof reason === 'object' && reason !== null && 'status' in reason && Number(reason.status) === 501) {
          setUnavailable(true)
        } else setError(errorMessage(reason))
      })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [page, retry, reloadSignal])

  const totalPages = Math.max(1, Math.ceil((data?.total ?? 0) / PAGE_SIZE))
  const jobs = [...(data?.jobs ?? [])].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))

  return (
    <div className="min-w-0 space-y-3">
      {data ? (
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 border-y border-border bg-card px-3 py-3">
          <div>
            <p className={LABEL}>今日全站（不含管理员）</p>
            <p className="font-mono text-[26px] font-semibold tabular-nums">{data.quota.used_day} / {data.quota.cap_day ?? '不限'}</p>
          </div>
          <div>
            <p className={LABEL}>本月</p>
            <p className="font-mono text-[26px] font-semibold tabular-nums">{data.quota.used_month}</p>
          </div>
          <p className="text-[11px] text-muted-foreground">每人每天 {data.quota.user_day ?? '不限'} · 并发 {data.quota.concurrency}</p>
          <p className="ml-auto text-[11px] text-muted-foreground">
            {data.params.model} · max_tokens {data.params.max_tokens} · {data.params.reasoning_effort} · sim≥{data.params.sim_min} · 质量分门槛 {data.params.quality_min ?? '关'}
          </p>
        </div>
      ) : null}
      {unavailable ? <StatePanel>该视图仅在生产数据模式可用</StatePanel>
        : loading ? <StatePanel>加载中…</StatePanel>
        : error ? <InlineError title="AI 搜索日志加载失败" detail={error} onRetry={() => setRetry((value) => value + 1)} />
        : jobs.length === 0 ? <StatePanel>暂无 AI 搜索任务</StatePanel>
        : (
          <div className="overflow-x-auto border border-border bg-card">
            <table className="w-full min-w-[1120px] text-[13px]">
              <thead className="border-b border-border text-[11px] text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left">时间</th>
                  <th className="px-3 py-2 text-left">查询</th>
                  <th className="px-3 py-2 text-left">状态</th>
                  {['词法/向量命中', '候选', '耗时', 'token（in / out（思考））', '积分（估）'].map((label) => <th key={label} className="px-3 py-2 text-right">{label}</th>)}
                  <th className="px-3 py-2 text-left">错误</th>
                </tr>
              </thead>
              <tbody>
                {jobs.map((job) => {
                  const retrievalIncomplete = job.status === 'retrieving'
                    || (job.status === 'failed' && (job.error === 'retrieval_failed' || job.error === 'retrieval_timeout'))
                  return (
                  <Fragment key={job.id}>
                    <tr className="cursor-pointer border-b border-border hover:bg-muted/30" onClick={() => setExpandedId((current) => current === job.id ? null : job.id)}>
                      <td className="whitespace-nowrap px-3 py-2 font-mono tabular-nums">{formatTime(job.created_at)}</td>
                      <td className="max-w-[300px] px-3 py-2">
                        <button type="button" title={job.query} aria-expanded={expandedId === job.id} aria-controls={`ai-search-detail-${job.id}`} className="block w-full text-left" onClick={(event) => { event.stopPropagation(); setExpandedId((current) => current === job.id ? null : job.id) }}>
                          <span className="line-clamp-1">{job.query}</span>
                        </button>
                      </td>
                      <td className="whitespace-nowrap px-3 py-2">{STATUS_LABELS[job.status] ?? '—'}</td>
                      <td className={NUMBER_CELL}>
                        {job.lexical_hits ?? '—'} / {!retrievalIncomplete && job.vector_ok ? (job.vector_hits ?? '—') : '—'}
                        {!retrievalIncomplete && !job.vector_ok ? <span className="ml-1 text-[11px] text-muted-foreground">向量失败</span> : null}
                      </td>
                      <td className={NUMBER_CELL}>{job.candidate_count ?? '—'}</td>
                      <td className={NUMBER_CELL}>{formatElapsed(job.elapsed_ms)}</td>
                      <td className={NUMBER_CELL}>{formatUsage(job.usage)}</td>
                      <td className={NUMBER_CELL}>{job.points_estimate ?? '—'}</td>
                      <td className="max-w-[200px] px-3 py-2"><span title={job.error ?? undefined} className="line-clamp-1">{job.error ?? '—'}</span></td>
                    </tr>
                    {expandedId === job.id ? (
                      <tr><td colSpan={9} id={`ai-search-detail-${job.id}`} className="border-b border-border px-4 py-4"><JobDetail key={job.id} id={job.id} /></td></tr>
                    ) : null}
                  </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      {!loading && data ? (
        <div className="flex flex-wrap items-center gap-3 border-t border-border pt-2 text-[12px] text-muted-foreground">
          <span>共 {data.total} 条 · 第 {page}/{totalPages} 页 · 每页 {PAGE_SIZE} 条</span>
          <span>排序：创建时间倒序</span>
          {totalPages > 1 ? <div className="ml-auto flex gap-2">
            <button type="button" disabled={page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))} className={BUTTON}>上一页</button>
            <button type="button" disabled={page >= totalPages} onClick={() => setPage((value) => Math.min(totalPages, value + 1))} className={BUTTON}>下一页</button>
          </div> : null}
        </div>
      ) : null}
    </div>
  )
}

function JobDetail({ id }: { id: string }) {
  const [data, setData] = useState<AiSearchJobDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    let active = true
    setError(null)
    setData(null)
    getAiSearchJob(id)
      .then((result) => { if (active) setData(result) })
      .catch((reason: unknown) => { if (active) setError(errorMessage(reason)) })
    return () => { active = false }
  }, [id, retry])

  if (error) return <InlineError title="任务详情加载失败" detail={error} onRetry={() => setRetry((value) => value + 1)} />
  if (!data) return <p className="text-[13px] text-muted-foreground" role="status">加载中…</p>
  const savedContent = typeof data.result === 'object' && data.result !== null && 'content_md_final' in data.result
    ? data.result.content_md_final : null
  const contentMd = data.content_md_final ?? (typeof savedContent === 'string' ? savedContent : null)
  return (
    <div className="space-y-4">
      {data.error ? <p className="whitespace-pre-wrap break-words text-[12px] text-destructive">错误：{data.error}</p> : null}
      <RetrievalBlock retrieval={data.retrieval} />
      <RawBlock label="原始输入" text={data.input_text} />
      <RawBlock label="模型原始 JSON" text={data.result == null ? null : JSON.stringify(data.result, null, 2)} />
      <section aria-label="后处理正文">
        <h3 className={LABEL}>后处理正文</h3>
        <div className="mt-2 max-w-[720px] font-event-title text-[16px] leading-[1.72]">{contentMd ? renderMarkdownLite(contentMd) : '—'}</div>
      </section>
      <div className="space-y-2 break-words font-mono text-[12px] tabular-nums text-muted-foreground">
        <p>cited_ids：{data.cited_ids?.length ?? 0} · {data.cited_ids?.join(' · ') || '—'}</p>
        <p>candidate_meta（前 10 条 · id · sim · hs10 · tier）：{data.candidate_meta?.slice(0, 10).map((candidate) => `${candidate.id} · ${candidate.sim ?? '—'} · ${candidate.hs10 ?? '—'} · ${candidate.tier ?? '—'}`).join('；') || '—'}</p>
        <p>request_params：{JSON.stringify(data.request_params)}</p>
        <p>usage（in / out（思考））：{formatUsage(data.usage)} · 积分（估）：{data.points_estimate ?? '—'} · 耗时：{formatElapsed(data.elapsed_ms)}</p>
        <p>(in×6.9 + cached×1.7 + out×24)/10000</p>
        <p>model：{data.model ?? '—'} · prompt_version：{data.prompt_version ?? '—'} · query_lexical：{data.query_lexical || '—'}</p>
      </div>
    </div>
  )
}

function RetrievalBlock({ retrieval }: { retrieval: AiSearchJobDetail['retrieval'] }) {
  const record = retrievalObject(retrieval)
  const errors = [record.error, record.similarity_error].filter(error => error != null && error !== '')
  const understanding = retrievalObject(record.understanding)
  const previewTitles = retrievalStrings(record.preview_titles)
  const legs = retrievalObjects(record.legs)
  const dfCap = formatRetrievalNumber(record.df_cap, undefined, '上限')
  const rerank = retrievalObject(record.rerank)
  const distribution = retrievalObject(rerank.score_distribution)
  const failedAttempts = retrievalObjects(rerank.attempts).filter(attempt => attempt.error != null
    && (typeof attempt.error !== 'string' || attempt.error.trim().length > 0))
  const selection = retrievalObject(record.selection)
  const timings = Object.entries(retrievalObject(record.timings_s))
  const usage = retrievalObjects(record.usage)
  return (
    <section aria-label="找资料" className="grid gap-2 break-words font-mono text-xs tabular-nums text-muted-foreground">
      <h3 className={LABEL}>找资料</h3>
      {errors.map((error, index) => <p key={index}>错误：{formatRetrievalText(error)}</p>)}
      <p>拆问题：版本 {formatRetrievalText(understanding.prompt_version)} · 模型 {formatRetrievalText(understanding.model)}{understanding.fallback === true ? ` · 兜底：${formatRetrievalText(understanding.error)}` : ''} · 字段兜底：{formatRetrievalList(understanding.field_fallbacks)} · 耗时 {formatRetrievalNumber(understanding.elapsed_s, 1)} 秒</p>
      <div className="grid gap-2">
        <p>粗查标题：{Array.isArray(record.preview_titles) ? previewTitles.length : '—'} 条</p>
        <ul className="grid gap-2">{previewTitles.length ? previewTitles.map((title, index) => <li key={index}>{title}</li>) : <li>—</li>}</ul>
        <p>粗查错误：{formatRetrievalText(record.preview_error)}</p>
      </div>
      <p>关键词：{formatRetrievalList(record.keywords)}</p>
      <p>检索句：{formatRetrievalList(record.semantic_queries, '；')}</p>
      <p>检索范围：近{formatRetrievalNumber(record.window_days)}天 · 向量可用：{formatRetrievalBoolean(record.vector_ok)}{record.vector_ok === false ? ` · 错误：${formatRetrievalText(record.embedding_error)}` : ''}</p>
      <div className="overflow-x-auto">
        <table aria-label="检索各路" className="w-full text-left">
          <thead><tr>{['类型', '查询', '命中', '取用', '是否跳过', '跳过原因', '错误', '耗时（秒）'].map((label) => <th key={label} className="px-3 py-2">{label}</th>)}</tr></thead>
          <tbody>{legs.length ? legs.map((leg, index) => <tr key={index}>
            <td className="px-3 py-2">{leg.type === 'lexical' ? '全文' : leg.type === 'vector' ? '向量' : '—'}</td>
            <td className="px-3 py-2">{formatRetrievalText(leg.query)}</td>
            <td className={NUMBER_CELL}>{leg.hits_capped === true ? `> ${dfCap}` : formatRetrievalNumber(leg.hits)}</td>
            <td className={NUMBER_CELL}>{formatRetrievalNumber(leg.taken)}</td>
            <td className="px-3 py-2">{formatRetrievalBoolean(leg.skipped)}</td>
            <td className="px-3 py-2">{leg.skip_reason === 'df_cap' ? '泛词跳过' : formatRetrievalText(leg.skip_reason)}</td>
            <td className="px-3 py-2">{formatRetrievalText(leg.error)}</td>
            <td className={NUMBER_CELL}>{formatRetrievalNumber(leg.elapsed_s, 1)}</td>
          </tr>) : <tr><td colSpan={8} className="px-3 py-2">—</td></tr>}</tbody>
        </table>
      </div>
      <p>合并 {formatRetrievalNumber(record.merged_count)} · 时间过滤后 {formatRetrievalNumber(record.filtered_count)}</p>
      <p>重排：模型 {formatRetrievalText(rerank.model)} · 兜底 {formatRetrievalBoolean(rerank.fallback)} · 共 {formatRetrievalNumber(rerank.count)} 条 · ≥ {rerank.threshold == null ? '阈值' : formatRetrievalNumber(rerank.threshold)}：{formatRetrievalNumber(rerank.above_threshold)} 条 · 判资料不足：{formatRetrievalBoolean(rerank.threshold_applied)}</p>
      <p>重排分数分布：min {formatRetrievalNumber(distribution.min, 2)} · max {formatRetrievalNumber(distribution.max, 2)} · mean {formatRetrievalNumber(distribution.mean, 2)}</p>
      <p>分桶：{Object.entries(retrievalObject(distribution.buckets)).map(([bucket, count]) => `${bucket}：${formatRetrievalNumber(count)}`).join(' · ') || '—'}</p>
      <div className="grid gap-2"><p>重排尝试错误：</p>{failedAttempts.length ? failedAttempts.map((attempt, index) => <p key={index}>{formatRetrievalText(attempt.model)}：{formatRetrievalText(attempt.error)}</p>) : <p>—</p>}</div>
      <p>取用与截断：前 {formatRetrievalNumber(selection.top_n_count)} 条 · 截掉 {formatRetrievalNumber(selection.dropped_count)} 条 · 最终 {formatRetrievalNumber(selection.final_count)} 条 · 估算 token {formatRetrievalNumber(selection.estimated_tokens)} / 上限 {formatRetrievalNumber(selection.input_max_tokens)} · 取不到正文长度 {Array.isArray(selection.unavailable_ids) ? retrievalStrings(selection.unavailable_ids).length : '—'} 条</p>
      <section aria-label="各步耗时" className="grid gap-2"><h4 className={LABEL}>各步耗时</h4>{timings.length ? timings.map(([stage, seconds]) => <p key={stage}>{stage}：{formatRetrievalNumber(seconds, 1)} 秒</p>) : <p>—</p>}</section>
      <section aria-label="各步用量" className="grid gap-2"><h4 className={LABEL}>各步用量</h4>{usage.length ? usage.map((entry, index) => <p key={index}>{formatRetrievalText(entry.stage)} · {formatRetrievalText(entry.model)} · 用量 {entry.usage && typeof entry.usage === 'object' && !Array.isArray(entry.usage) ? JSON.stringify(entry.usage) : '—'}</p>) : <p>—</p>}</section>
    </section>
  )
}

function retrievalObject(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}
function retrievalObjects(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(entry => entry && typeof entry === 'object') : []
}
function retrievalStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter(entry => typeof entry === 'string') : []
}
function formatRetrievalText(value: unknown) {
  return typeof value === 'string' || (typeof value === 'number' && Number.isFinite(value)) ? value : '—'
}
function formatRetrievalNumber(value: unknown, digits?: number, fallback = '—') {
  return typeof value === 'number' && Number.isFinite(value) ? (digits == null ? value : value.toFixed(digits)) : fallback
}
function formatRetrievalBoolean(value: unknown) {
  return typeof value === 'boolean' ? (value ? '是' : '否') : '—'
}
function formatRetrievalList(value: unknown, separator = ' · ') {
  return retrievalStrings(value).join(separator) || '—'
}

function RawBlock({ label, text }: { label: string; text: string | null }) {
  const [expanded, setExpanded] = useState(false)
  const [copied, setCopied] = useState(false)
  const [copyError, setCopyError] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout>>()
  useEffect(() => () => { clearTimeout(timer.current) }, [])
  async function copy() {
    try {
      await navigator.clipboard.writeText(text ?? '')
      setCopied(true)
      setCopyError(false)
      clearTimeout(timer.current)
      timer.current = setTimeout(() => setCopied(false), 1500)
    } catch { setCopyError(true) }
  }
  return (
    <section aria-label={label} className="border border-border px-3 py-2">
      <div className="flex items-center gap-3">
        <h3 className={LABEL}>{label}</h3>
        <button type="button" className="text-[12px] text-muted-foreground" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
          {expanded ? '收起' : `展开 ${((text?.length ?? 0) / 10000).toFixed(1)} 万字`}
        </button>
        <button type="button" onClick={() => void copy()} disabled={text == null} className={`${BUTTON} ml-auto text-[12px]`}>{copied ? '已复制' : '复制'}</button>
      </div>
      {copyError ? <p role="alert" className="mt-2 text-[12px] text-destructive">复制失败，请重试或展开后手动复制</p> : null}
      {expanded ? <pre className="mt-2 font-mono text-[12px] whitespace-pre-wrap max-h-[60vh] overflow-auto">{text ?? '—'}</pre> : null}
    </section>
  )
}

// Match the existing admin error, empty-state and pagination patterns without changing shared files.
function InlineError({ title, detail, onRetry }: { title: string; detail: string; onRetry: () => void }) {
  return <div role="alert" className="flex items-center gap-3 border border-border bg-card px-4 py-3 text-[13px]"><AlertTriangle className="h-4 w-4 a-text-crit" /><div><p className="font-semibold">{title}</p><p className="text-[12px] text-muted-foreground">{detail}</p></div><button type="button" onClick={onRetry} className="ml-auto inline-flex items-center gap-1 rounded-[4px] border border-border px-2.5 py-1.5"><RefreshCw className="h-3.5 w-3.5" />重试</button></div>
}
function StatePanel({ children }: { children: ReactNode }) {
  return <div className="flex min-h-40 items-center justify-center border border-border bg-card px-6 py-12 text-center text-[13px] text-muted-foreground">{children}</div>
}
function errorMessage(error: unknown) {
  const message = error instanceof Error ? error.message : '加载失败'
  return message === 'ai_search_disabled' ? 'AI 搜索功能已关闭' : message
}
function formatUsage(usage: AiSearchJobUsage | null) {
  return `${usage?.prompt_tokens ?? '—'} / ${usage?.completion_tokens ?? '—'}（${usage?.completion_tokens_details?.reasoning_tokens ?? usage?.reasoning_tokens ?? '—'}）`
}
function formatElapsed(ms: number | null) {
  if (ms == null || !Number.isFinite(ms)) return '—'
  const seconds = Math.floor(ms / 1000)
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}
function formatTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const pad = (part: number) => String(part).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
