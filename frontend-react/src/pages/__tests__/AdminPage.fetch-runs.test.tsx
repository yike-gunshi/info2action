import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { toast } from 'sonner'

import { AdminPage } from '../AdminPage'

vi.mock('../../lib/api', () => ({
  getAdminOverview: vi.fn(),
  getInviteCodes: vi.fn(),
  createInviteCodes: vi.fn(),
  deleteInviteCode: vi.fn(),
  getUsers: vi.fn(),
  getFetchRuns: vi.fn(),
  getFetchRun: vi.fn(),
  getFetchRunItems: vi.fn(),
  getEmbeddingUsage: vi.fn(),
}))

vi.mock('sonner', () => ({
  toast: {
    error: vi.fn(),
    success: vi.fn(),
  },
}))

import {
  createInviteCodes,
  getAdminOverview,
  getEmbeddingUsage,
  getFetchRun,
  getFetchRunItems,
  getFetchRuns,
  getInviteCodes,
  type InviteCode,
} from '../../lib/api'

const run = {
  id: 7,
  started_at: '2026-05-12T10:00:00',
  finished_at: '2026-05-12T10:02:00',
  status: 'done',
  duration_sec: 120,
  total_new_items: 1,
  audit: {
    version: 'v15.2',
    new_items_count: 1,
    stage_durations_sec: { source_fetch: 1.2, ingest: 0.4 },
    platform_source_counts: [{ platform: 'twitter', source: 'following', count: 1 }],
    pill_counts: [{ pill: 'products', count: 1 }],
    ai_summary: { summarized: 1, failed: 0, pending: 0 },
    event_cluster: { clustered_items: 1, touched_clusters: 1, published_clusters: 1 },
  },
}

const emptyEmbeddingUsage = {
  hours: 24,
  limit: 50,
  summary: {
    total_calls: 0,
    success_calls: 0,
    failed_calls: 0,
    input_count: 0,
    input_chars: 0,
    input_bytes: 0,
    estimated_tokens_attempted: 0,
    estimated_tokens_success: 0,
    output_count: 0,
    estimated_cost_yuan_success: 0,
    estimated_cost_yuan_all: 0,
  },
  by_source: [],
  by_run: [],
  logs: [],
}

const inviteBase: Omit<InviteCode, 'code' | 'max_uses' | 'used_count'> = {
  created_by: 'admin-id',
  used_by: null,
  expires_at: null,
  created_at: '2026-05-26T10:00:00',
}

function adminOverviewFor(runs = [run], limit = 20, codes: InviteCode[] = []) {
  return {
    codes,
    users: [],
    fetch_runs: { runs, limit, offset: 0 },
    embedding_usage: emptyEmbeddingUsage,
  }
}

describe('AdminPage fetch runs', () => {
  beforeEach(() => {
    vi.mocked(getAdminOverview).mockResolvedValue(adminOverviewFor())
    vi.mocked(getFetchRun).mockResolvedValue({ run })
    vi.mocked(getFetchRuns).mockResolvedValue({ runs: [], limit: 50, offset: 1 })
    vi.mocked(getEmbeddingUsage).mockResolvedValue(emptyEmbeddingUsage)
    vi.mocked(getFetchRunItems).mockResolvedValue({
      run_id: 7,
      platform: 'twitter',
      source_name: 'following',
      total: 1,
      limit: 50,
      offset: 0,
      items: [
        {
          id: 'new-item',
          title: 'New title from this run',
          platform: 'twitter',
          source: 'following',
          pill: 'products',
          ai_status: 'summarized',
          cluster_status: 'clustered',
          created_at: '2026-05-12T10:01:00',
        },
      ],
    })
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('shows fetch-run source drilldown and links titles to item detail', async () => {
    render(<AdminPage />)

    expect(await screen.findByText('抓取运行')).toBeInTheDocument()
    expect(screen.getByText('#7')).toBeInTheDocument()
    expect(within(screen.getByTestId('run-row-7')).getByText('1/1')).toBeInTheDocument()
    expect(screen.getByText('following')).toBeInTheDocument()

    fireEvent.click(screen.getByTitle('查看标题'))

    await waitFor(() => {
      expect(getFetchRunItems).toHaveBeenCalledWith(7, {
        platform: 'twitter',
        source: 'following',
        limit: 50,
      })
    })
    const link = await screen.findByRole('link', { name: 'New title from this run' })
    expect(link).toHaveAttribute('href', '#v=info&d=new-item')
    expect(screen.getByText('summarized')).toBeInTheDocument()
    expect(screen.getByText('clustered')).toBeInTheDocument()
  })

  it('hydrates selected run detail back into the run ledger row', async () => {
    const overviewRun = {
      ...run,
      id: 1349,
      total_new_items: 0,
      audit: {
        ...run.audit,
        new_items_count: 0,
        platform_source_counts: [],
        ai_summary: { summarized: 0, failed: 0, pending: 0 },
        event_cluster: { clustered_items: 0, touched_clusters: 0, published_clusters: 0 },
      },
    }
    const detailRun = {
      ...overviewRun,
      total_new_items: 15,
      audit: {
        ...overviewRun.audit,
        new_items_count: 15,
        platform_source_counts: [{ platform: 'twitter', source: 'following', count: 15 }],
        ai_summary: { summarized: 15, failed: 0, pending: 0 },
        event_cluster: { clustered_items: 15, touched_clusters: 4, published_clusters: 3 },
      },
    }

    vi.mocked(getAdminOverview).mockResolvedValueOnce(adminOverviewFor([overviewRun]))
    vi.mocked(getFetchRun).mockResolvedValueOnce({ run: detailRun })

    render(<AdminPage />)

    const row = await screen.findByTestId('run-row-1349')
    await waitFor(() => {
      expect(within(row).getByText('15')).toBeInTheDocument()
    })
    expect(within(row).getByText('15/15')).toBeInTheDocument()
    expect(within(row).getByText('3')).toBeInTheDocument()
    expect(screen.getAllByText('本轮新增入库').length).toBeGreaterThan(0)
  })

  it('loads older fetch runs when the run ledger scrolls near the bottom', async () => {
    const olderRun = {
      ...run,
      id: 6,
      started_at: '2026-05-12T09:30:00',
    }
    vi.mocked(getAdminOverview).mockResolvedValueOnce(adminOverviewFor([run], 1))
    vi.mocked(getFetchRuns).mockResolvedValueOnce({ runs: [olderRun], limit: 50, offset: 1 })

    render(<AdminPage />)

    const scroller = await screen.findByTestId('run-ledger-scroll')
    Object.defineProperty(scroller, 'scrollHeight', { value: 1000, configurable: true })
    Object.defineProperty(scroller, 'clientHeight', { value: 500, configurable: true })
    Object.defineProperty(scroller, 'scrollTop', { value: 380, configurable: true })
    fireEvent.scroll(scroller)

    await waitFor(() => {
      expect(getFetchRuns).toHaveBeenCalledWith({ limit: 50, offset: 1 })
    })
    expect(await screen.findByText('#6')).toBeInTheDocument()
  })

  it('generates invite codes with selected count and max uses', async () => {
    vi.mocked(createInviteCodes).mockResolvedValue({ codes: ['AAA11111', 'BBB22222', 'CCC33333'] })
    vi.mocked(getInviteCodes).mockResolvedValue({
      codes: [
        { ...inviteBase, code: 'AAA11111', max_uses: 2, used_count: 0 },
        { ...inviteBase, code: 'BBB22222', max_uses: 2, used_count: 0 },
        { ...inviteBase, code: 'CCC33333', max_uses: 2, used_count: 0 },
      ],
    })

    render(<AdminPage />)

    expect(await screen.findByText('权限管理')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('生成数量'), { target: { value: '3' } })
    fireEvent.change(screen.getByLabelText('每个码可用'), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: '生成' }))

    await waitFor(() => {
      expect(createInviteCodes).toHaveBeenCalledWith(3, 2)
    })
    expect(await screen.findByText('AAA11111')).toBeInTheDocument()
    expect(screen.getAllByText('0/2')).toHaveLength(3)
  })

  it('shows reusable invite capacity states and copies only unused active codes', async () => {
    vi.mocked(getAdminOverview).mockResolvedValue(adminOverviewFor([run], 20, [
      { ...inviteBase, code: 'UNUSED01', max_uses: 100, used_count: 0 },
      { ...inviteBase, code: 'PARTIAL1', max_uses: 100, used_count: 3 },
      { ...inviteBase, code: 'FULL0001', max_uses: 100, used_count: 100 },
      { ...inviteBase, code: 'EXPIRED1', max_uses: 1, used_count: 0, expires_at: '2000-01-01T00:00:00' },
    ]))

    render(<AdminPage />)

    expect(await screen.findByText('UNUSED01')).toBeInTheDocument()
    expect(screen.getByText('0/100')).toBeInTheDocument()
    expect(screen.getByText('3/100')).toBeInTheDocument()
    expect(screen.getByText('100/100')).toBeInTheDocument()
    expect(screen.getByText('未使用')).toBeInTheDocument()
    expect(screen.getByText('可用')).toBeInTheDocument()
    expect(screen.getByText('已用完')).toBeInTheDocument()
    expect(screen.getByText('已过期')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '复制未使用码' }))

    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith('UNUSED01')
    })
    expect(toast.success).toHaveBeenCalledWith('已复制 1 个未使用邀请码')
  })
})
