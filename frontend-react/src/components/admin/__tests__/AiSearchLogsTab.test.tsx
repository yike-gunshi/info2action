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

  it('五种状态均有文字，向量失败显示 —，未知用量不伪装成零，按创建时间倒序', async () => {
    const statuses = ['queued', 'running', 'done', 'failed', 'unavailable'] as const
    vi.mocked(getAiSearchJobs).mockResolvedValue({ ...response, jobs: statuses.map((status, i) => ({
      ...job, id: `job-${i}`, query: `查询-${i}`, status, created_at: `2026-09-19T0${i}:00:00Z`,
      vector_ok: false, usage: null, elapsed_ms: null, points_estimate: null,
    })) })
    render(<AiSearchLogsTab />)
    await screen.findByText('排队')
    for (const label of ['排队', '运行', '完成', '失败', '不可用']) expect(screen.getByText(label)).toBeInTheDocument()
    expect(screen.getAllByText('向量失败')).toHaveLength(5)
    expect(screen.getAllByText('— / —（—）')).toHaveLength(5)
    const first = screen.getAllByRole('row')[1]
    expect(first).toHaveTextContent('查询-4')
    expect(first).toHaveTextContent('795 / —')
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
