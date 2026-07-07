import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

import { OverviewTab } from '../OverviewTab'

vi.mock('../../../lib/api', () => ({
  getAdminConsoleSummary: vi.fn(),
}))

import { getAdminConsoleSummary, type AdminConsoleSummary } from '../../../lib/api'

const fullSummary: AdminConsoleSummary = {
  available: true,
  generated_at: '2026-07-05T21:04:00+08:00',
  c_metrics: {
    total_users: 3,
    new_users_today: 0,
    new_users_7d: 1,
    active_users_1d: 1,
    active_users_7d: 2,
    info_click_users_7d: 2,
    info_click_items_7d: 34,
    info_click_items_total: 61,
    highlight_click_users_7d: 1,
    highlight_click_events_7d: 6,
    highlight_click_events_total: 9,
  },
  interactions_detail: {
    starred_users: 2,
    starred_total: 11,
    read_users_7d: 2,
    read_items_7d: 210,
    latest_signup: { username: 'probe_openreg_0704', created_at: '2026-07-04T12:00:00+08:00' },
  },
  cost: { embedding_cost_yuan_24h: 0.1143, embedding_calls_24h: 96 },
  health: {
    signals: [
      { key: 'pipeline', level: 'ok', label: '抓取 Pipeline', detail: 'run #3494 success · 2h 前', link: 'runs' },
      { key: 'freshness', level: 'warn', label: '平台新鲜度', detail: 'xiaohongshu 26h 前', link: 'runs' },
      { key: 'embedding', level: 'unknown', label: 'Embedding', detail: '24h 无调用', link: 'runs' },
      { key: 'remote_db', level: 'ok', label: '远程 DB', detail: '可达 · 128ms', link: null },
      { key: 'disk', level: 'ok', label: '磁盘', detail: '已用 62% · DB 1.8 GB', link: null },
    ],
    incidents: [{ severity: 'warn', text: 'xiaohongshu 最近抓取 26h 前（阈值 24h）', link: 'runs' }],
  },
  trends: {
    new_users_14d: Array.from({ length: 14 }, (_, i) => ({ date: `2026-06-${22 + i}`.slice(0, 10), value: i === 13 ? 1 : 0 })),
    fetch_success_rate_7d: Array.from({ length: 7 }, (_, i) => ({ date: `2026-06-${29 + i}`, value: 1 })),
  },
}

describe('OverviewTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })
  afterEach(() => {
    cleanup()
  })

  it('渲染 C 端指标卡、健康灯与口径标注', async () => {
    vi.mocked(getAdminConsoleSummary).mockResolvedValue(fullSummary)
    render(<OverviewTab reloadSignal={0} onOpenRuns={() => {}} />)

    expect(await screen.findByText('总用户数')).toBeInTheDocument()
    // 互动卡口径标注必须存在（不伪装成点击次数）
    expect(screen.getAllByText('口径：点过的人数，非点击次数').length).toBeGreaterThanOrEqual(2)
    // 健康灯：ok/warn/unknown 三态文案（ok 有 3 个信号）
    expect(screen.getAllByText(/正常/).length).toBeGreaterThanOrEqual(3)
    expect(screen.getAllByText(/注意/).length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText(/未知/)).toBeInTheDocument()
    // 异常摘要
    expect(screen.getByText(/xiaohongshu 最近抓取 26h 前/)).toBeInTheDocument()
  })

  it('降级态：available:false 显示需连接远程数据源', async () => {
    vi.mocked(getAdminConsoleSummary).mockResolvedValue({ available: false, reason: 'remote_required' })
    render(<OverviewTab reloadSignal={0} onOpenRuns={() => {}} />)

    expect(await screen.findByText('总览需连接远程数据源')).toBeInTheDocument()
  })

  it('错误态：接口抛错显示重试', async () => {
    vi.mocked(getAdminConsoleSummary).mockRejectedValue(new Error('boom'))
    render(<OverviewTab reloadSignal={0} onOpenRuns={() => {}} />)

    expect(await screen.findByText('总览加载失败')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('重试')).toBeInTheDocument())
  })

  it('未知值以 — 呈现，不伪装成 0', async () => {
    vi.mocked(getAdminConsoleSummary).mockResolvedValue({
      ...fullSummary,
      c_metrics: { ...fullSummary.c_metrics, total_users: null },
    })
    render(<OverviewTab reloadSignal={0} onOpenRuns={() => {}} />)

    expect(await screen.findByText('总用户数')).toBeInTheDocument()
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })
})
