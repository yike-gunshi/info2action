import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { HighlightsFilteredTab } from '../HighlightsFilteredTab'
import { useAuthStore } from '../../../store/authStore'

vi.mock('../../../lib/api', () => ({
  getAdminHighlightsFunnel: vi.fn(),
  getAdminHighlightsFunnelRows: vi.fn(),
  setAdminHighlightOverride: vi.fn(),
  submitFeedback: vi.fn(),
  setClusterFeedback: vi.fn(),
  fetchClusterBundle: vi.fn(),
  fetchClusterDetail: vi.fn(),
  fetchClusterSources: vi.fn(),
  clickCluster: vi.fn(),
  setClusterStar: vi.fn(),
  fetchClusterActions: vi.fn(),
  generateClusterAction: vi.fn(),
}))

vi.mock('sonner', () => ({
  toast: { error: vi.fn(), info: vi.fn(), success: vi.fn() },
}))

import {
  getAdminHighlightsFunnel,
  getAdminHighlightsFunnelRows,
  setAdminHighlightOverride,
  submitFeedback,
} from '../../../lib/api'

const funnelResponse = {
  stations: [
    { key: 'ingested' as const, count: 412 },
    { key: 'scored' as const, count: 96 },
    { key: 'clustered' as const, count: 61 },
    { key: 'summarized' as const, count: 38 },
    { key: 'displayed' as const, count: 21 },
  ],
  diffs: [
    { key: 'scoring' as const, count: 316 },
    { key: 'summary' as const, count: 23 },
    { key: 'display' as const, count: 17 },
  ],
  anomalies_count: 3,
  gate_disabled: false,
}

const dims = { authority: 3, substance: 2, novelty: 3, timeliness: 2, audience_fit: 2 }

const clusterRow = {
  id: 42,
  latest_at: '2026-07-17T09:42:00Z',
  title: 'Claude 5 系列发布',
  dominant_category: 'models',
  max_flag_score10: 9.2,
  score_inputs: { max_q: 0.92, avg_q: 0.71, scored_include_count: 2, unique_source_count: 3 },
  deciding_item: { id: 'item-1', title: 'Introducing Claude 5', dims, reason: '官方一手发布' },
  stage: 'displayed' as const,
  blocked_reason: null,
  displayed: true,
  manual_display: null,
  feedback: { kind: null, note: null },
  members: [
    {
      id: 'item-1', title: 'Introducing Claude 5', url: 'https://example.com/1',
      platform: 'official', source: 'anthropic', author_name: 'Anthropic', fetched_at: '2026-07-17T09:42:00Z',
      verdict: 'featured', score10: 9.2, dims, veto: null, uncertainty: null,
      reason: '官方一手发布', feedback: { kind: null, note: null },
    },
    {
      id: 'item-2', title: '普通人如何抓住 Claude 5 红利', url: 'https://example.com/2',
      platform: 'wechat_mp', source: 'ai-note', author_name: 'AI掘金笔记', fetched_at: '2026-07-17T08:00:00Z',
      verdict: 'drop', score10: 3.1, dims: { ...dims, authority: 1 }, veto: 'marketing', uncertainty: null,
      reason: '营销通稿', feedback: { kind: null, note: null },
    },
  ],
}

const panoramaResponse = {
  granularity: 'cluster' as const,
  items: [clusterRow],
  total: 1,
  page: 1,
  gate_disabled: false,
  display_threshold: 6.5,
}

function mockRows() {
  vi.mocked(getAdminHighlightsFunnelRows).mockImplementation(async ({ view, q }) => {
    if (view === 'anomaly') {
      return {
        granularity: 'item', items: [], total: 0, page: 1,
        gate_disabled: false, display_threshold: 6.5,
      }
    }
    if (q) return { ...panoramaResponse, items: [], total: 0 }
    return panoramaResponse
  })
}

describe('HighlightsFilteredTab 全景表', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
    useAuthStore.setState({
      user: { id: 'admin-1', username: 'admin', email: 'admin@example.com', role: 'admin' },
      isLoading: false,
      isChecked: true,
    })
    vi.mocked(getAdminHighlightsFunnel).mockResolvedValue(funnelResponse)
    mockRows()
  })

  afterEach(cleanup)

  it('默认渲染 10 列、全平铺 item 和簇级 rowspan', async () => {
    render(<HighlightsFilteredTab reloadSignal={0} />)

    const table = await screen.findByRole('table', { name: '精选漏斗全景表' })
    expect(within(table).getAllByRole('columnheader')).toHaveLength(10)
    expect(screen.getAllByTestId(/funnel-item-row-/)).toHaveLength(2)
    expect(screen.getByTestId('cluster-time-42')).toHaveAttribute('rowspan', '2')
    expect(screen.getByTestId('cluster-title-42')).toHaveAttribute('rowspan', '2')
    expect(getAdminHighlightsFunnelRows).toHaveBeenCalledWith({
      view: 'panorama', days: 1, q: '', tag: '', display: 'all', stage: '', page: 1, limit: 20,
    })
    expect(screen.queryByRole('button', { name: '入库 412' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('入库 412')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '被拦 316' })).toBeInTheDocument()

    const clusterLink = screen.getByRole('link', { name: '打开事件簇：Claude 5 系列发布' })
    expect(clusterLink).toHaveAttribute('href', '#cluster=42')
    expect(clusterLink).toHaveAttribute('target', '_blank')
    expect(clusterLink).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('全景表使用可达横向滚动层并用 colgroup 对齐 10 列', async () => {
    render(<HighlightsFilteredTab reloadSignal={0} />)

    const table = await screen.findByRole('table', { name: '精选漏斗全景表' })
    const scroll = screen.getByTestId('funnel-panorama-scroll')
    expect(scroll).toHaveClass('max-w-full', 'overflow-x-auto')
    expect(scroll).toContainElement(table)
    expect(table.querySelectorAll('colgroup col')).toHaveLength(10)
    expect(within(table).getByRole('columnheader', { name: /簇反馈/ })).toBeVisible()
  })

  it('拖动列头右缘调整列宽并按列 id 持久化', async () => {
    const { unmount } = render(<HighlightsFilteredTab reloadSignal={0} />)
    const table = await screen.findByRole('table', { name: '精选漏斗全景表' })
    const handle = within(table).getByRole('separator', { name: '调整 item（标题 · 来源）列宽' })

    fireEvent.mouseDown(handle, { clientX: 200 })
    fireEvent.mouseMove(window, { clientX: 240 })
    fireEvent.mouseUp(window)

    expect(table.querySelector('col[data-column-id="item_title"]')).toHaveStyle({ width: '275px' })
    expect(window.localStorage.getItem('admin-highlights-funnel:column-width:item_title')).toBe('275')

    unmount()
    render(<HighlightsFilteredTab reloadSignal={0} />)
    const restored = await screen.findByRole('table', { name: '精选漏斗全景表' })
    expect(restored.querySelector('col[data-column-id="item_title"]')).toHaveStyle({ width: '275px' })
  })

  it('每页默认 20，可切换到 100 并回到第 1 页持久化', async () => {
    vi.mocked(getAdminHighlightsFunnelRows).mockResolvedValue({ ...panoramaResponse, total: 120 })
    const { unmount } = render(<HighlightsFilteredTab reloadSignal={0} />)
    await screen.findByText('Claude 5 系列发布')

    expect(screen.getByRole('combobox', { name: '每页条数' })).toHaveValue('20')
    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    await waitFor(() => expect(getAdminHighlightsFunnelRows).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 2, limit: 20 }),
    ))

    fireEvent.change(screen.getByRole('combobox', { name: '每页条数' }), { target: { value: '100' } })
    await waitFor(() => expect(getAdminHighlightsFunnelRows).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 1, limit: 100 }),
    ))
    expect(window.localStorage.getItem('admin-highlights-funnel:page-size')).toBe('100')
    expect(screen.getByText(/每页 100 簇/)).toBeInTheDocument()

    unmount()
    render(<HighlightsFilteredTab reloadSignal={0} />)
    expect(await screen.findByRole('combobox', { name: '每页条数' })).toHaveValue('100')
    await waitFor(() => expect(getAdminHighlightsFunnelRows).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 1, limit: 100 }),
    ))
  })

  it('1/3/7 天、展示、标签、搜索可正交叠加', async () => {
    render(<HighlightsFilteredTab reloadSignal={0} />)
    await screen.findByText('Claude 5 系列发布')

    fireEvent.click(screen.getByRole('button', { name: '3 天' }))
    fireEvent.click(screen.getByRole('button', { name: '已展示' }))
    fireEvent.click(screen.getByRole('button', { name: '效率工具' }))
    const search = screen.getByRole('searchbox', { name: '搜索全景表' })
    fireEvent.change(search, { target: { value: 'Agent' } })
    fireEvent.keyDown(search, { key: 'Enter' })

    await waitFor(() => expect(getAdminHighlightsFunnelRows).toHaveBeenLastCalledWith({
      view: 'panorama', days: 3, q: 'Agent', tag: 'efficiency_tools',
      display: 'shown', stage: '', page: 1, limit: 20,
    }))
    await waitFor(() => expect(getAdminHighlightsFunnel).toHaveBeenLastCalledWith({
      days: 3, q: 'Agent', tag: 'efficiency_tools',
    }))
  })

  it('计数条只让 3 个被拦、已展示和异常成为快捷筛选', async () => {
    render(<HighlightsFilteredTab reloadSignal={0} />)
    await screen.findByText('Claude 5 系列发布')

    fireEvent.click(screen.getByRole('button', { name: '被拦 316' }))
    await waitFor(() => expect(getAdminHighlightsFunnelRows).toHaveBeenLastCalledWith(
      expect.objectContaining({ view: 'panorama', stage: 'blocked_scoring' }),
    ))
    fireEvent.click(screen.getByRole('button', { name: '已展示 21' }))
    await waitFor(() => expect(getAdminHighlightsFunnelRows).toHaveBeenLastCalledWith(
      expect.objectContaining({ view: 'panorama', display: 'shown', stage: '' }),
    ))
    fireEvent.click(screen.getByRole('button', { name: '异常 3' }))
    await waitFor(() => expect(getAdminHighlightsFunnelRows).toHaveBeenLastCalledWith(
      expect.objectContaining({ view: 'anomaly' }),
    ))
  })

  it('簇分 rich hover 明细包含定分 item、五维、reason 和 score_inputs', async () => {
    render(<HighlightsFilteredTab reloadSignal={0} />)
    const score = await screen.findByRole('button', { name: '簇分 9.2，查看明细' })
    fireEvent.focus(score)

    const tip = screen.getByRole('tooltip')
    expect(tip).toHaveTextContent('定分 item')
    expect(tip).toHaveTextContent('Introducing Claude 5')
    expect(tip).toHaveTextContent('权威 3')
    expect(tip).toHaveTextContent('官方一手发布')
    expect(tip).toHaveTextContent('max_q 0.92')
    expect(tip).toHaveTextContent('过闸成员 2')
  })

  it('待 why_read 和 pending 均显示中性 ⏳ 样式', async () => {
    vi.mocked(getAdminHighlightsFunnelRows).mockResolvedValue({
      ...panoramaResponse,
      items: [
        { ...clusterRow, id: 43, stage: 'blocked_display', blocked_reason: 'awaiting_why_read', displayed: false },
        { ...clusterRow, id: 44, stage: 'pending', blocked_reason: 'pending_scoring', displayed: false },
      ],
      total: 2,
    })
    render(<HighlightsFilteredTab reloadSignal={0} />)

    expect(await screen.findByText('⏳ 处理中 · why_read 生成中')).toHaveClass('a-pill-unknown')
    expect(screen.getByText('⏳ 处理中 · 打分中')).toHaveClass('a-pill-unknown')
  })

  it('簇级展示反馈走 override，再点激活态发 clear', async () => {
    vi.mocked(setAdminHighlightOverride)
      .mockResolvedValueOnce({
        ok: true, manual_display: 'force_show', manual_display_at: '2026-07-17T10:00:00Z',
        feedback_kind: 'should_feature', feedback_note: '架构级进展',
      })
      .mockResolvedValueOnce({
        ok: true, manual_display: null, manual_display_at: null,
        feedback_kind: null, feedback_note: null,
      })
    render(<HighlightsFilteredTab reloadSignal={0} />)
    const cell = await screen.findByTestId('cluster-feedback-42')

    fireEvent.click(within(cell).getByRole('button', { name: '展示' }))
    const input = within(cell).getByRole('textbox', { name: '簇反馈备注' })
    fireEvent.change(input, { target: { value: '架构级进展' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    expect(await within(cell).findByText('已强制展示，已记标注；10 分钟内生效')).toBeInTheDocument()
    expect(setAdminHighlightOverride).toHaveBeenCalledWith(42, 'force_show', '架构级进展')
    fireEvent.click(within(cell).getByRole('button', { name: '✓ 展示' }))
    await waitFor(() => expect(setAdminHighlightOverride).toHaveBeenLastCalledWith(42, 'clear', undefined))
  })

  it('item 收录/排除走 should_feature/should_drop，不改簇 override', async () => {
    vi.mocked(submitFeedback).mockResolvedValue({ ok: true, active: true })
    render(<HighlightsFilteredTab reloadSignal={0} />)
    const row = await screen.findByTestId('funnel-item-row-item-2')

    fireEvent.click(within(row).getByRole('button', { name: '排除' }))
    const input = within(row).getByRole('textbox', { name: 'item 反馈备注' })
    fireEvent.change(input, { target: { value: '营销内容' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    expect(await within(row).findByRole('button', { name: '✓ 排除' })).toBeInTheDocument()
    expect(submitFeedback).toHaveBeenCalledWith('item-2', 'should_drop', '营销内容')
    expect(setAdminHighlightOverride).not.toHaveBeenCalled()
  })

  it('veto 映射快照与后端四值对齐，未知值原样输出', async () => {
    const vetoes = ['marketing', 'rumor_unverified', 'flamewar', 'engagement_bait', 'future_veto']
    vi.mocked(getAdminHighlightsFunnelRows).mockResolvedValue({
      ...panoramaResponse,
      items: [{
        ...clusterRow,
        members: vetoes.map((veto, index) => ({
          ...clusterRow.members[0], id: `veto-${index}`, title: `veto-${index}`, veto,
        })),
      }],
    })
    render(<HighlightsFilteredTab reloadSignal={0} />)
    await screen.findByText('veto-0')

    const labels = vetoes.map((_, index) => within(screen.getByTestId(`funnel-item-row-veto-${index}`)).getByTestId('item-block-reason').textContent)
    expect(labels).toMatchInlineSnapshot(`
      [
        "veto · 营销通稿",
        "veto · 传闻未证实",
        "veto · 引战",
        "veto · 互动诱饵",
        "veto · future_veto",
      ]
    `)
  })

  it.each([
    {
      name: 'featured + veto none 显示为空原因',
      member: {
        ...clusterRow.members[0], id: 'featured-veto-none', title: 'featured veto none',
        veto: 'none', reason: null,
      },
      expected: '—',
    },
    {
      name: 'drop + veto none 回退到分数原因',
      member: {
        ...clusterRow.members[1], id: 'drop-veto-none', title: 'drop veto none',
        veto: 'none', score10: 3.1,
      },
      expected: 'score 3.1 未过打分闸',
    },
  ])('$name', async ({ member, expected }) => {
    vi.mocked(getAdminHighlightsFunnelRows).mockResolvedValue({
      ...panoramaResponse,
      items: [{
        ...clusterRow,
        members: [member],
      }],
    })
    render(<HighlightsFilteredTab reloadSignal={0} />)
    await screen.findByText(member.title)

    const reason = within(screen.getByTestId(`funnel-item-row-${member.id}`)).getByTestId('item-block-reason')
    expect(reason).toHaveTextContent(expected)
    expect(screen.queryByText('veto · none')).not.toBeInTheDocument()
  })

  it('item 无 veto/分数时回退显示 LLM reason', async () => {
    vi.mocked(getAdminHighlightsFunnelRows).mockResolvedValue({
      ...panoramaResponse,
      items: [{
        ...clusterRow,
        members: [{
          ...clusterRow.members[0], id: 'reason-fallback', verdict: 'drop',
          score10: null, veto: null, reason: '证据不足，暂不收录',
        }],
      }],
    })
    render(<HighlightsFilteredTab reloadSignal={0} />)

    const row = await screen.findByTestId('funnel-item-row-reason-fallback')
    expect(within(row).getByTestId('item-block-reason')).toHaveTextContent('证据不足，暂不收录')
  })

  it('提交失败行内报错并回滚激活态', async () => {
    vi.mocked(setAdminHighlightOverride).mockRejectedValue(new Error('boom'))
    render(<HighlightsFilteredTab reloadSignal={0} />)
    const cell = await screen.findByTestId('cluster-feedback-42')

    fireEvent.click(within(cell).getByRole('button', { name: '不展示' }))
    fireEvent.keyDown(within(cell).getByRole('textbox', { name: '簇反馈备注' }), { key: 'Enter' })

    expect(await within(cell).findByText('反馈失败，请重试')).toBeInTheDocument()
    expect(within(cell).getByRole('button', { name: '不展示' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('搜索空态与 non-remote 降级态保留边界教育', async () => {
    render(<HighlightsFilteredTab reloadSignal={0} />)
    await screen.findByText('Claude 5 系列发布')
    const search = screen.getByRole('searchbox', { name: '搜索全景表' })
    fireEvent.change(search, { target: { value: '不存在' } })
    fireEvent.keyDown(search, { key: 'Enter' })
    expect(await screen.findByText('没有匹配的条目。搜不到通常意味着它未入库——请检查信源池')).toBeInTheDocument()

    const unavailable = Object.assign(new Error('remote required'), { status: 501 })
    vi.mocked(getAdminHighlightsFunnelRows).mockRejectedValue(unavailable)
    fireEvent.click(screen.getByRole('button', { name: '7 天' }))
    expect(await screen.findByText('该视图仅在生产数据模式可用')).toBeInTheDocument()
  })
})
