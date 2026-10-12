import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AiSearchLogsTab } from '../AiSearchLogsTab'
import { getAiSearchJob, getAiSearchJobs } from '../../../lib/api/admin'
import type { AiSearchJobDetail, AiSearchJobRow, AiSearchJobsResponse } from '../../../lib/api/admin'

vi.mock('../../../lib/api/admin', () => ({ getAiSearchJob: vi.fn(), getAiSearchJobs: vi.fn() }))

const job: AiSearchJobRow = {
  id: 'job-1', created_at: '2026-09-19T08:00:00Z', started_at: null, finished_at: null,
  status: 'done', query: '怎么使用 seedance 2.5', lexical_hits: 795, vector_hits: 300,
  vector_ok: true, candidate_count: 100, elapsed_ms: 492000,
  usage: { prompt_tokens: 120000, completion_tokens: 8000, completion_tokens_details: { reasoning_tokens: 3000 } },
  points_estimate: 102, error: null,
}
const response: AiSearchJobsResponse = {
  jobs: [job], total: 1, offset: 0, limit: 50,
  quota: { used_day: 3, used_month: 37, cap_day: 10, user_day: 5, concurrency: 2 },
  params: { model: 'glm-5.3', max_tokens: 131072, reasoning_effort: 'high', sim_min: 0.45, quality_min: null, window_days: 90 },
}
const detail: AiSearchJobDetail = {
  ...job, query_lexical: 'seedance', input_text: '完整输入正文', result: { content_md: '模型输出' },
  content_md_final: '**整理结果**', cited_ids: ['item-1'],
  candidate_meta: Array.from({ length: 11 }, (_, i) => ({ id: `candidate-${i + 1}`, sim: 0.8, hs10: 9, tier: 'content' })),
  request_params: { max_tokens: 131072 }, model: 'glm-5.3', prompt_version: 'v5.1',
}
const copy = vi.fn()
beforeEach(() => {
  vi.mocked(getAiSearchJobs).mockResolvedValue(response)
  vi.mocked(getAiSearchJob).mockResolvedValue(detail)
  copy.mockResolvedValue(undefined)
  vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: copy } })
})
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })

describe('AI 搜索日志', () => {
  it('读取后台实际记录的秒级每路耗时、最终取用、截断、泛词、粗查与分步用量', async () => {
    vi.mocked(getAiSearchJob).mockResolvedValue({ ...detail, retrieval: {
      preview_titles: ['Muse 的原文', 'Cue 的原文'], keywords: ['Muse', 'Agent'], semantic_queries: ['Muse 如何使用'],
      legs: [{ type: 'lexical', query: 'Agent', hits: 3000, taken: 0, skipped: true, skip_reason: 'df_cap', error: null, elapsed_s: 0.3 },
        { type: 'vector', query: 'Muse 如何使用', hits: 10, taken: 10, skipped: false, error: null, elapsed_s: 0 }],
      merged_count: 10, rerank: { model: 'voyageai/rerank-2.5', above_threshold: 2 },
      selection: { top_n_count: 10, dropped_count: 2, retained_count: 8, final_count: 5 },
      timings_s: { understanding: 8, lexical: 0.3, vector: 0, rerank: 1, total: 9.3 },
      usage: [{ stage: 'understanding', model: 'MiniMax-M3', usage: { input_tokens: 80, output_tokens: 70, cost: 0.01 } }],
    } })
    render(<AiSearchLogsTab />)
    fireEvent.click(await screen.findByRole('button', { name: job.query }))
    const region = within(await screen.findByRole('region', { name: '找资料' }))
    expect(region.getByText('粗查标题：2 条')).toBeInTheDocument()
    expect(region.getAllByRole('listitem').map((item) => item.textContent)).toEqual(['Muse 的原文', 'Cue 的原文'])
    expect(region.getByText('取用与截断：前 10 条 · 截掉 2 条 · 最终 5 条 · 估算 token — / 上限 — · 取不到正文长度 — 条')).toBeInTheDocument()
    expect(region.getByText('重排：模型 voyageai/rerank-2.5 · 兜底 — · 共 — 条 · ≥ 阈值：2 条 · 判资料不足：—')).toBeInTheDocument()
    const rows = within(region.getByRole('table')).getAllByRole('row')
    expect(within(rows[1]).getAllByRole('cell')[5]).toHaveTextContent('泛词跳过')
    expect(within(rows[1]).getAllByRole('cell')[7]).toHaveTextContent('0.3')
    expect(within(rows[2]).getAllByRole('cell')[7]).toHaveTextContent('0.0')
    const timings = within(region.getByRole('region', { name: '各步耗时' }))
    for (const line of ['understanding：8.0 秒', 'lexical：0.3 秒', 'vector：0.0 秒', 'rerank：1.0 秒', 'total：9.3 秒']) expect(timings.getByText(line)).toBeInTheDocument()
    expect(within(region.getByRole('region', { name: '各步用量' })).getByText('understanding · MiniMax-M3 · 用量 {"input_tokens":80,"output_tokens":70,"cost":0.01}')).toBeInTheDocument()
  })
  it('renders the Markdown saved inside result when a failed job has no top-level final content', async () => {
    vi.mocked(getAiSearchJob).mockResolvedValue({ ...detail, status: 'failed', content_md_final: null,
      result: { content_md_final: '**兜底保存的正文**\n\n<script>保留为纯文本</script>' } })
    render(<AiSearchLogsTab />)
    fireEvent.click(await screen.findByRole('button', { name: job.query }))
    const region = await screen.findByRole('region', { name: '后处理正文' })
    expect(within(region).getByText('兜底保存的正文').tagName).toBe('STRONG')
    expect(region).toHaveTextContent('<script>保留为纯文本</script>')
    expect(region.querySelector('script')).toBeNull()
  })

  it.each([
    { content: '顶层正文', result: { content_md_final: '兜底正文' }, expected: '顶层正文' },
    { content: '', result: { content_md_final: '兜底正文' }, expected: '—' },
    { content: null, result: null, expected: '—' },
    { content: null, result: '未知结果结构', expected: '—' },
    { content: null, result: { content_md_final: 42 }, expected: '—' },
    { content: null, result: { content_md_final: null }, expected: '—' },
  ])('prefers top-level final content with nullish fallback and handles absent or malformed saved content: %j', async ({ content, result, expected }) => {
    vi.mocked(getAiSearchJob).mockResolvedValue({ ...detail, content_md_final: content, result })
    render(<AiSearchLogsTab />)
    fireEvent.click(await screen.findByRole('button', { name: job.query }))
    const region = await screen.findByRole('region', { name: '后处理正文' })
    expect(region.querySelector('div')?.textContent).toBe(expected)
    expect(within(region).queryByText('兜底正文')).not.toBeInTheDocument()
  })
  it.each([
    { usage: { completion_tokens_details: { reasoning_tokens: 0 }, reasoning_tokens: 99 }, expected: 0 },
    { usage: { completion_tokens_details: { reasoning_tokens: 123 }, reasoning_tokens: 99 }, expected: 123 },
    { usage: { reasoning_tokens: 42 }, expected: 42 },
    { usage: { completion_tokens_details: null, reasoning_tokens: 42 }, expected: 42 },
  ])('uses provider reasoning details in the row and expanded detail, with legacy fallback: %j', async ({ usage, expected }) => {
    const providerUsage = { prompt_tokens: 120000, completion_tokens: 8000, ...usage }
    vi.mocked(getAiSearchJobs).mockResolvedValue({ ...response, jobs: [{ ...job, usage: providerUsage }] })
    vi.mocked(getAiSearchJob).mockResolvedValue({ ...detail, usage: providerUsage })
    render(<AiSearchLogsTab />)
    expect(await screen.findByText(`120000 / 8000（${expected}）`)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: job.query }))
    expect(await screen.findByText(/usage（in \/ out（思考））/)).toHaveTextContent(`120000 / 8000（${expected}）`)
  })
  it('呈现配额、只读参数和紧凑表格，保留精确数字与估算标注', async () => {
    render(<AiSearchLogsTab />)
    expect(await screen.findByText('3 / 10')).toHaveClass('text-[26px]', 'font-mono', 'tabular-nums')
    expect(screen.getByText('今日全站（不含管理员）')).toHaveClass('text-[11px]')
    expect(screen.getByText('本月')).toHaveClass('text-[11px]')
    expect(screen.getByText('37')).toHaveClass('text-[26px]', 'font-mono', 'tabular-nums')
    expect(screen.getByText('每人每天 5 · 并发 2')).toBeInTheDocument()
    expect(screen.getByText(/glm-5.3 · max_tokens 131072 · high · sim≥0.45 · 质量分门槛 关/)).toBeInTheDocument()
    expect(screen.getByText('795 / 300')).toBeInTheDocument()
    expect(screen.getByText('8 分 12 秒')).toBeInTheDocument()
    expect(screen.getByText('120000 / 8000（3000）')).toBeInTheDocument()
    expect(screen.getByText('积分（估）')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: job.query })).toHaveAttribute('title', job.query)
    expect(screen.queryByRole('button', { name: /删除|重跑/ })).not.toBeInTheDocument()
    expect(getAiSearchJob).not.toHaveBeenCalled()
  })

  it('不限与零额度、零质量门槛不混淆', async () => {
    vi.mocked(getAiSearchJobs).mockResolvedValueOnce({ ...response, quota: { ...response.quota, cap_day: null, user_day: null } })
    const { rerender } = render(<AiSearchLogsTab />)
    expect(await screen.findByText('3 / 不限')).toBeInTheDocument()
    expect(screen.getByText('每人每天 不限 · 并发 2')).toBeInTheDocument()
    vi.mocked(getAiSearchJobs).mockResolvedValueOnce({ ...response, quota: { ...response.quota, cap_day: 0, user_day: 0, used_month: 0 }, params: { ...response.params, quality_min: 0 } })
    rerender(<AiSearchLogsTab reloadSignal={1} />)
    expect(await screen.findByText('3 / 0')).toBeInTheDocument()
    expect(screen.getByText('每人每天 0 · 并发 2')).toBeInTheDocument()
    expect(screen.getByText('0')).toHaveClass('text-[26px]', 'font-mono', 'tabular-nums')
    expect(screen.getByText(/质量分门槛 0/)).toBeInTheDocument()
  })

  it('七种状态均有文字，向量失败显示 —，未知用量不伪装成零，按创建时间倒序', async () => {
    const statuses = ['retrieving', 'insufficient', 'queued', 'running', 'done', 'failed', 'unavailable'] as const
    vi.mocked(getAiSearchJobs).mockResolvedValue({ ...response, jobs: statuses.map((status, i) => ({
      ...job, id: `job-${i}`, query: `查询-${i}`, status, created_at: `2026-09-19T0${i}:00:00Z`,
      vector_ok: false, usage: null, elapsed_ms: null, points_estimate: null,
    })) })
    render(<AiSearchLogsTab />)
    await screen.findByText('排队')
    for (const label of ['找资料', '资料不足', '排队', '运行', '完成', '失败', '不可用']) expect(screen.getByText(label)).toBeInTheDocument()
    expect(screen.getAllByText('向量失败')).toHaveLength(6)
    expect(screen.getAllByText('— / —（—）')).toHaveLength(7)
    const retrievingRow = screen.getByRole('button', { name: '查询-0' }).closest('tr')!
    expect(retrievingRow).not.toHaveTextContent('向量失败')
    expect(within(retrievingRow).getAllByRole('cell')[3]).toHaveTextContent('795 / —')
    const first = screen.getAllByRole('row')[1]
    expect(first).toHaveTextContent('查询-6')
    expect(first).toHaveTextContent('795 / —')
  })

  it.each([
    { status: 'retrieving', error: null },
    { status: 'failed', error: 'retrieval_failed' },
    { status: 'failed', error: 'retrieval_timeout' },
  ] as const)('找资料未完成时不标向量失败：$status / $error', async ({ status, error }) => {
    vi.mocked(getAiSearchJobs).mockResolvedValue({ ...response, jobs: [{ ...job, status, error, vector_ok: false }] })
    render(<AiSearchLogsTab />)
    const row = (await screen.findByRole('button', { name: job.query })).closest('tr')!
    expect(within(row).getAllByRole('cell')[3].textContent).toBe('795 / —')
    expect(within(row).queryByText('向量失败')).not.toBeInTheDocument()
  })

  it('找资料中即使旧记录 vector_ok 为 true，向量命中仍显示 —', async () => {
    vi.mocked(getAiSearchJobs).mockResolvedValue({ ...response, jobs: [{ ...job, status: 'retrieving' }] })
    render(<AiSearchLogsTab />)
    const row = (await screen.findByRole('button', { name: job.query })).closest('tr')!
    expect(within(row).getAllByRole('cell')[3].textContent).toBe('795 / —')
    expect(within(row).queryByText('向量失败')).not.toBeInTheDocument()
  })

  it.each(['internal', 'timeout', null])('正常失败保留向量失败标签：%s', async error => {
    vi.mocked(getAiSearchJobs).mockResolvedValue({ ...response, jobs: [{ ...job, status: 'failed', error, vector_ok: false }] })
    render(<AiSearchLogsTab />)
    const row = (await screen.findByRole('button', { name: job.query })).closest('tr')!
    expect(within(row).getAllByRole('cell')[3]).toHaveTextContent('795 / —')
    expect(within(row).getByText('向量失败')).toBeInTheDocument()
  })

  it('完整真实记录按顺序显示拆问题、范围、检索路、重排、截断及各步用量', async () => {
    vi.mocked(getAiSearchJob).mockResolvedValue({ ...detail, retrieval: {
      understanding: { prompt_version: 'query-v1', model: 'MiniMax-M3', fallback: false, field_fallbacks: ['semantic_queries', 'time_window_days'], elapsed_s: 12.345 },
      preview_titles: ['Muse 产品介绍', 'Cue 使用体验'], keywords: ['Agent', 'Muse'], semantic_queries: ['Muse 有什么特点？', 'Agent 如何协作？'],
      window_days: 30, vector_ok: false, embedding_error: 'embedding failed',
      legs: [
        { type: 'lexical', query: 'Muse', hits: 42, taken: 40, skipped: false, error: null, elapsed_s: 0.34 },
        { type: 'lexical', query: 'Agent', hits: 3000, taken: 0, skipped: true, skip_reason: 'df_cap', error: null, elapsed_s: 0.12 },
        { type: 'vector', query: 'Muse 有什么特点？', hits: 0, taken: 0, skipped: false, error: 'vector timeout', elapsed_s: 6.04 },
      ], merged_count: 42, filtered_count: 40,
      rerank: { model: 'cohere/rerank-4-pro', count: 40, fallback: true, threshold: 0.7, above_threshold: 8, threshold_applied: false,
        score_distribution: { min: 0, max: 0.91234, mean: 0.52345, buckets: { '0.0-0.1': 0, '0.7-0.8': 8 } },
        attempts: [{ model: 'voyageai/rerank-2.5', error: 'rerank 429' }, { model: 'voyageai/rerank-2.5', error: 'rerank 503' }, { model: 'cohere/rerank-4-pro', error: null }] },
      selection: { top_n_count: 40, dropped_count: 5, final_count: 35, estimated_tokens: 280000, input_max_tokens: 300000, unavailable_ids: ['item-1'] },
      timings_s: { embedding_original: 0.045, understanding: 12.345, lexical: 0.359, rerank: 2.304, selection: 0, total: 15.456 },
      usage: [{ stage: 'embedding_original', usage: { prompt_tokens: 23 } }, { stage: 'understanding', model: 'MiniMax-M3', usage: { input_tokens: 120, output_tokens: 70 } }, { stage: 'rerank', model: 'cohere/rerank-4-pro', usage: { prompt_tokens: 540 } }],
    } })
    render(<AiSearchLogsTab />)
    fireEvent.click(await screen.findByRole('button', { name: job.query }))
    const block = await screen.findByRole('region', { name: '找资料' })
    const region = within(block)
    for (const line of [
      '拆问题：版本 query-v1 · 模型 MiniMax-M3 · 字段兜底：semantic_queries · time_window_days · 耗时 12.3 秒',
      '粗查标题：2 条', '关键词：Agent · Muse', '检索句：Muse 有什么特点？；Agent 如何协作？',
      '检索范围：近30天 · 向量可用：否 · 错误：embedding failed', '合并 42 · 时间过滤后 40',
      '重排：模型 cohere/rerank-4-pro · 兜底 是 · 共 40 条 · ≥ 0.7：8 条 · 判资料不足：否',
      '重排分数分布：min 0.00 · max 0.91 · mean 0.52', '分桶：0.0-0.1：0 · 0.7-0.8：8',
      'voyageai/rerank-2.5：rerank 429', 'voyageai/rerank-2.5：rerank 503',
      '取用与截断：前 40 条 · 截掉 5 条 · 最终 35 条 · 估算 token 280000 / 上限 300000 · 取不到正文长度 1 条',
    ]) expect(region.getByText(line)).toBeInTheDocument()
    expect(region.queryByText(/^兜底：/)).not.toBeInTheDocument()
    expect(region.getAllByRole('listitem').map((item) => item.textContent)).toEqual(['Muse 产品介绍', 'Cue 使用体验'])
    const rows = within(region.getByRole('table')).getAllByRole('row')
    expect(within(rows[1]).getAllByRole('cell').map((cell) => cell.textContent)).toEqual(['全文', 'Muse', '42', '40', '否', '—', '—', '0.3'])
    expect(within(rows[2]).getAllByRole('cell').map((cell) => cell.textContent)).toEqual(['全文', 'Agent', '3000', '0', '是', '泛词跳过', '—', '0.1'])
    expect(within(rows[3]).getAllByRole('cell').map((cell) => cell.textContent)).toEqual(['向量', 'Muse 有什么特点？', '0', '0', '否', '—', 'vector timeout', '6.0'])
    const timings = within(region.getByRole('region', { name: '各步耗时' }))
    for (const line of ['embedding_original：0.0 秒', 'understanding：12.3 秒', 'lexical：0.4 秒', 'rerank：2.3 秒', 'selection：0.0 秒', 'total：15.5 秒']) expect(timings.getByText(line)).toBeInTheDocument()
    expect(Array.from(region.getByRole('region', { name: '各步用量' }).querySelectorAll('p'), (row) => row.textContent)).toEqual([
      'embedding_original · — · 用量 {"prompt_tokens":23}', 'understanding · MiniMax-M3 · 用量 {"input_tokens":120,"output_tokens":70}', 'rerank · cohere/rerank-4-pro · 用量 {"prompt_tokens":540}',
    ])
    const order = ['拆问题：', '粗查标题：', '关键词：', '检索范围：', '合并 ', '重排：', '取用与截断：', '各步耗时', '各步用量'].map((label) => (block.textContent ?? '').indexOf(label))
    expect(order).toEqual([...order].sort((a, b) => a - b))
    expect(block).not.toHaveTextContent('费用')
  })

  it.each([undefined, null, {}, { understanding: {}, legs: [{}], rerank: {}, selection: {}, usage: [{}] }])('真实记录缺任意字段时显示 —，不推断阈值或零计数：%j', async (retrieval) => {
    vi.mocked(getAiSearchJob).mockResolvedValue({ ...detail, retrieval })
    render(<AiSearchLogsTab />)
    fireEvent.click(await screen.findByRole('button', { name: job.query }))
    const region = within(await screen.findByRole('region', { name: '找资料' }))
    for (const line of ['拆问题：版本 — · 模型 — · 字段兜底：— · 耗时 — 秒', '粗查标题：— 条', '关键词：—', '检索句：—', '检索范围：近—天 · 向量可用：—', '合并 — · 时间过滤后 —', '重排：模型 — · 兜底 — · 共 — 条 · ≥ 阈值：— 条 · 判资料不足：—', '重排分数分布：min — · max — · mean —', '分桶：—', '取用与截断：前 — 条 · 截掉 — 条 · 最终 — 条 · 估算 token — / 上限 — · 取不到正文长度 — 条']) expect(region.getByText(line)).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '后处理正文' })).toHaveTextContent('整理结果')
  })

  it.each([
    { legs: [null, 'bad', 3] },
    { legs: {}, rerank: { attempts: {} } },
    { keywords: 'Muse', semantic_queries: {}, understanding: { field_fallbacks: 'model' }, preview_titles: 'title' },
    { usage: [null, 'bad', 3], rerank: { attempts: [null, 'bad', 3] } },
    { understanding: 'bad', rerank: 'bad', selection: 'bad', timings_s: 'bad' },
    { rerank: { score_distribution: { buckets: 'bad' } }, usage: {} },
    'bad',
  ])('畸形列表或容器不会使 admin 页崩溃：%j', async retrieval => {
    vi.mocked(getAiSearchJob).mockResolvedValue({ ...detail, retrieval: retrieval as unknown as AiSearchJobDetail['retrieval'] })
    render(<AiSearchLogsTab />)
    fireEvent.click(await screen.findByRole('button', { name: job.query }))
    const region = within(await screen.findByRole('region', { name: '找资料' }))
    expect(region.getByText('关键词：—')).toBeInTheDocument()
    expect(region.getByText('检索句：—')).toBeInTheDocument()
    expect(within(region.getByRole('table')).getAllByRole('cell').map(cell => cell.textContent)).toEqual(['—'])
    expect(region.getByText('分桶：—')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '后处理正文' })).toHaveTextContent('整理结果')
  })

  it('过滤混合列表并规整对象文本、错误布尔值与非有限数字', async () => {
    const retrieval = {
      understanding: { prompt_version: {}, model: 0, fallback: true, error: {}, field_fallbacks: ['semantic_queries', null, 7], elapsed_s: '1' },
      preview_titles: ['有效标题', null, {}, 7], preview_error: {}, keywords: ['Muse', null, {}, 7], semantic_queries: ['有效检索句', null, 7],
      window_days: '90', vector_ok: 'false', merged_count: Infinity, filtered_count: NaN,
      legs: [null, 7, { type: 'lexical', query: {}, hits: Infinity, taken: '12', skipped: 'false', skip_reason: {}, error: {}, elapsed_s: '1' }],
      rerank: { model: {}, fallback: 'false', count: Infinity, threshold: NaN, above_threshold: '3', threshold_applied: {},
        score_distribution: { min: {}, max: Infinity, mean: '0.5', buckets: { invalid: {}, string: '3', zero: 0 } },
        attempts: [null, 'bad', { model: {}, error: {} }, { model: 7, error: 9 }] },
      selection: { top_n_count: {}, dropped_count: Infinity, final_count: '5', estimated_tokens: NaN, input_max_tokens: -Infinity, unavailable_ids: {} },
      timings_s: { total: '1' }, usage: [null, 7, { stage: {}, model: {}, usage: 'bad' }],
    }
    vi.mocked(getAiSearchJob).mockResolvedValue({ ...detail, retrieval: retrieval as unknown as AiSearchJobDetail['retrieval'] })
    render(<AiSearchLogsTab />)
    fireEvent.click(await screen.findByRole('button', { name: job.query }))
    const region = within(await screen.findByRole('region', { name: '找资料' }))
    for (const line of [
      '拆问题：版本 — · 模型 0 · 兜底：— · 字段兜底：semantic_queries · 耗时 — 秒',
      '粗查标题：1 条', '粗查错误：—', '关键词：Muse', '检索句：有效检索句', '检索范围：近—天 · 向量可用：—',
      '合并 — · 时间过滤后 —', '重排：模型 — · 兜底 — · 共 — 条 · ≥ —：— 条 · 判资料不足：—',
      '重排分数分布：min — · max — · mean —', '分桶：invalid：— · string：— · zero：0', '—：—', '7：9',
      '取用与截断：前 — 条 · 截掉 — 条 · 最终 — 条 · 估算 token — / 上限 — · 取不到正文长度 — 条',
    ]) expect(region.getByText(line)).toBeInTheDocument()
    expect(region.getAllByRole('listitem').map(item => item.textContent)).toEqual(['有效标题'])
    const row = within(region.getByRole('table')).getAllByRole('row')[1]
    expect(within(row).getAllByRole('cell').map(cell => cell.textContent)).toEqual(['全文', '—', '—', '—', '—', '—', '—', '—'])
    expect(within(region.getByRole('region', { name: '各步耗时' })).getByText('total：— 秒')).toBeInTheDocument()
    expect(within(region.getByRole('region', { name: '各步用量' })).getByText('— · — · 用量 —')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '后处理正文' })).toHaveTextContent('整理结果')
  })

  it.each([
    { df_cap: 1500, expected: '> 1500' },
    { df_cap: undefined, expected: '> 上限' },
  ])('找资料错误置顶，截到上限的命中显示 $expected', async ({ df_cap, expected }) => {
    vi.mocked(getAiSearchJob).mockResolvedValue({ ...detail, retrieval: {
      error: 'retrieval_failed', similarity_error: 'similarity timeout', df_cap,
      legs: [{ type: 'lexical', query: 'Agent', hits: 1501, hits_capped: true }, { type: 'lexical', query: 'Muse', hits: 42, hits_capped: false }],
    } })
    render(<AiSearchLogsTab />)
    fireEvent.click(await screen.findByRole('button', { name: job.query }))
    const block = await screen.findByRole('region', { name: '找资料' })
    const region = within(block)
    expect(region.getByText('错误：retrieval_failed')).toBeInTheDocument()
    expect(region.getByText('错误：similarity timeout')).toBeInTheDocument()
    expect(Array.from(block.querySelectorAll('p')).slice(0, 2).map(row => row.textContent)).toEqual(['错误：retrieval_failed', '错误：similarity timeout'])
    const rows = within(region.getByRole('table')).getAllByRole('row')
    expect(within(rows[1]).getAllByRole('cell')[2].textContent).toBe(expected)
    expect(within(rows[2]).getAllByRole('cell')[2].textContent).toBe('42')
  })

  it('失败部分记录展示拆问题兜底、粗查错误和单路错误，之后的字段安全缺省', async () => {
    vi.mocked(getAiSearchJob).mockResolvedValue({ ...detail, status: 'failed', retrieval: {
      preview_titles: [], preview_error: 'preview metadata timeout', understanding: { prompt_version: 'query-v1', model: 'MiniMax-M3', fallback: true, error: 'understanding timeout', field_fallbacks: [], elapsed_s: 15 },
      window_days: 90, vector_ok: false, legs: [{ type: 'lexical', query: 'Muse', hits: 0, taken: 0, skipped: false, error: 'SQL timeout', elapsed_s: 6 }], timings_s: { understanding: 15, lexical: 6 },
    } })
    render(<AiSearchLogsTab />)
    fireEvent.click(await screen.findByRole('button', { name: job.query }))
    const region = within(await screen.findByRole('region', { name: '找资料' }))
    for (const line of ['拆问题：版本 query-v1 · 模型 MiniMax-M3 · 兜底：understanding timeout · 字段兜底：— · 耗时 15.0 秒', '粗查标题：0 条', '粗查错误：preview metadata timeout', '检索范围：近90天 · 向量可用：否 · 错误：—', 'SQL timeout', '重排：模型 — · 兜底 — · 共 — 条 · ≥ 阈值：— 条 · 判资料不足：—']) expect(region.getByText(line)).toBeInTheDocument()
    expect(within(region.getByRole('region', { name: '各步耗时' })).getByText('lexical：6.0 秒')).toBeInTheDocument()
    expect(within(region.getByRole('region', { name: '各步用量' })).getByText('—')).toBeInTheDocument()
  })

  it('所有实际零值保留，阈值读取 0 而不改用固定值', async () => {
    vi.mocked(getAiSearchJob).mockResolvedValue({ ...detail, retrieval: {
      understanding: { elapsed_s: 0 }, preview_titles: [], window_days: 0, vector_ok: true,
      legs: [{ type: 'vector', hits: 0, taken: 0, skipped: false, elapsed_s: 0 }], merged_count: 0, filtered_count: 0,
      rerank: { fallback: false, count: 0, threshold: 0, above_threshold: 0, threshold_applied: true, score_distribution: { min: 0, max: 0, mean: 0, buckets: { '0.0-0.1': 0 } }, attempts: [{ model: 'ignored', error: '' }, { error: 'missing model error' }] },
      selection: { top_n_count: 0, dropped_count: 0, final_count: 0, estimated_tokens: 0, input_max_tokens: 0, unavailable_ids: [] }, timings_s: { total: 0 }, usage: [{ stage: 'embedding_original', usage: { prompt_tokens: 0 } }, {}],
    } })
    render(<AiSearchLogsTab />)
    fireEvent.click(await screen.findByRole('button', { name: job.query }))
    const region = within(await screen.findByRole('region', { name: '找资料' }))
    for (const line of ['拆问题：版本 — · 模型 — · 字段兜底：— · 耗时 0.0 秒', '检索范围：近0天 · 向量可用：是', '合并 0 · 时间过滤后 0', '重排：模型 — · 兜底 否 · 共 0 条 · ≥ 0：0 条 · 判资料不足：是', '重排分数分布：min 0.00 · max 0.00 · mean 0.00', '分桶：0.0-0.1：0', '—：missing model error', '取用与截断：前 0 条 · 截掉 0 条 · 最终 0 条 · 估算 token 0 / 上限 0 · 取不到正文长度 0 条']) expect(region.getByText(line)).toBeInTheDocument()
    expect(region.queryByText(/ignored/)).not.toBeInTheDocument()
    expect(within(region.getByRole('region', { name: '各步耗时' })).getByText('total：0.0 秒')).toBeInTheDocument()
    const usage = within(region.getByRole('region', { name: '各步用量' }))
    expect(usage.getByText('embedding_original · — · 用量 {"prompt_tokens":0}')).toBeInTheDocument()
    expect(usage.getByText('— · — · 用量 —')).toBeInTheDocument()
  })

  it('展开才加载详情，原始块默认收起，可展开与复制，并渲染 Markdown', async () => {
    let resolve!: (value: AiSearchJobDetail) => void
    vi.mocked(getAiSearchJob).mockReturnValue(new Promise((done) => { resolve = done }))
    render(<AiSearchLogsTab />)
    fireEvent.click(await screen.findByRole('button', { name: job.query }))
    expect(screen.getByRole('status')).toHaveTextContent('加载中…')
    expect(getAiSearchJob).toHaveBeenCalledWith('job-1')
    await act(async () => resolve(detail))
    const input = within(screen.getByRole('region', { name: '原始输入' }))
    const raw = within(screen.getByRole('region', { name: '模型原始 JSON' }))
    expect(screen.queryByText('完整输入正文')).not.toBeInTheDocument()
    expect(screen.queryByText(/"content_md":/)).not.toBeInTheDocument()
    fireEvent.click(input.getByRole('button', { name: '展开 0.0 万字' }))
    expect(input.getByText('完整输入正文')).toHaveClass('max-h-[60vh]', 'whitespace-pre-wrap')
    vi.useFakeTimers()
    await act(async () => fireEvent.click(input.getByRole('button', { name: '复制' })))
    expect(copy).toHaveBeenCalledWith('完整输入正文')
    expect(input.getByRole('button', { name: '已复制' })).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(1500) })
    expect(input.getByRole('button', { name: '复制' })).toBeInTheDocument()
    vi.useRealTimers()
    fireEvent.click(input.getByRole('button', { name: '收起' }))
    expect(screen.queryByText('完整输入正文')).not.toBeInTheDocument()
    fireEvent.click(raw.getByRole('button', { name: /展开/ }))
    expect(raw.getByText(/"content_md": "模型输出"/)).toBeInTheDocument()
    expect(screen.getByText('整理结果').tagName).toBe('STRONG')
    expect(screen.getByText(/cited_ids：1/)).toHaveTextContent('item-1')
    expect(screen.getByText(/candidate_meta/)).toHaveTextContent('candidate-10')
    expect(screen.getByText(/candidate_meta/)).not.toHaveTextContent('candidate-11')
    expect(screen.getByText('(in×6.9 + cached×1.7 + out×24)/10000')).toBeInTheDocument()
  })

  it('切换行只显示当前详情，并忽略前一行延迟返回的结果', async () => {
    vi.mocked(getAiSearchJobs).mockResolvedValue({ ...response, jobs: [job, { ...job, id: 'job-2', query: '第二个查询' }] })
    let resolve!: (value: AiSearchJobDetail) => void
    vi.mocked(getAiSearchJob).mockReturnValueOnce(new Promise((done) => { resolve = done })).mockResolvedValueOnce({ ...detail, id: 'job-2', content_md_final: '第二个结果' })
    render(<AiSearchLogsTab />)
    fireEvent.click(await screen.findByRole('button', { name: job.query }))
    fireEvent.click(screen.getByRole('button', { name: '第二个查询' }))
    expect(await screen.findByText('第二个结果')).toBeInTheDocument()
    await act(async () => resolve(detail))
    expect(screen.queryByText('整理结果')).not.toBeInTheDocument()
    expect(screen.getAllByRole('region', { name: '原始输入' })).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: '第二个查询' }))
    expect(screen.queryByText('第二个结果')).not.toBeInTheDocument()
  })

  it('空态与非 remote 降级', async () => {
    vi.mocked(getAiSearchJobs).mockResolvedValueOnce({ ...response, jobs: [], total: 0 })
    const { rerender } = render(<AiSearchLogsTab />)
    expect(await screen.findByText('暂无 AI 搜索任务')).toBeInTheDocument()
    vi.mocked(getAiSearchJobs).mockRejectedValueOnce(Object.assign(new Error('remote required'), { status: 501 }))
    rerender(<AiSearchLogsTab reloadSignal={1} />)
    expect(await screen.findByText('该视图仅在生产数据模式可用')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('列表错误可重试，分页使用 50 条 offset 并能返回上一页', async () => {
    vi.mocked(getAiSearchJobs).mockRejectedValueOnce(new Error('network error')).mockResolvedValue({ ...response, total: 51 })
    render(<AiSearchLogsTab />)
    expect(await screen.findByRole('alert')).toHaveTextContent('network error')
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByText('3 / 10')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    await waitFor(() => expect(getAiSearchJobs).toHaveBeenLastCalledWith({ limit: 50, offset: 50 }))
    expect(await screen.findByText('共 51 条 · 第 2/2 页 · 每页 50 条')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '上一页' }))
    await waitFor(() => expect(getAiSearchJobs).toHaveBeenLastCalledWith({ limit: 50, offset: 0 }))
  })

  it('功能关闭错误显示中文提示', async () => {
    vi.mocked(getAiSearchJobs).mockRejectedValueOnce(Object.assign(new Error('ai_search_disabled'), { status: 404 }))
    render(<AiSearchLogsTab />)
    expect(await screen.findByRole('alert')).toHaveTextContent('AI 搜索功能已关闭')
    expect(screen.queryByText('ai_search_disabled')).not.toBeInTheDocument()
  })

  it('详情请求失败可重试，复制失败不会误报成功', async () => {
    vi.mocked(getAiSearchJob).mockRejectedValueOnce(new Error('detail failed')).mockResolvedValueOnce({ ...detail, error: 'timeout 原文' })
    copy.mockRejectedValueOnce(new Error('denied'))
    render(<AiSearchLogsTab />)
    fireEvent.click(await screen.findByRole('button', { name: job.query }))
    expect(await screen.findByRole('alert')).toHaveTextContent('detail failed')
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByText('错误：timeout 原文')).toBeInTheDocument()
    fireEvent.click(within(screen.getByRole('region', { name: '原始输入' })).getByRole('button', { name: '复制' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('复制失败')
    expect(screen.queryByText('已复制')).not.toBeInTheDocument()
  })
})
