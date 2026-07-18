import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

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
  getAdminSources: vi.fn(),
  validateAdminSource: vi.fn(),
  createAdminSource: vi.fn(),
  updateAdminSource: vi.fn(),
  deleteAdminSource: vi.fn(),
  reconcileLingowhaleSources: vi.fn(),
  getAdminSourceAlgoParams: vi.fn(),
  updateAdminSourceAlgoParams: vi.fn(),
  searchWechatSources: vi.fn(),
  syncAdminXList: vi.fn(),
  getAdminConsoleSummary: vi.fn(),
  getAdminHighlightsFunnel: vi.fn(),
  getAdminHighlightsFunnelRows: vi.fn(),
}))

vi.mock('sonner', () => ({
  toast: { error: vi.fn(), success: vi.fn() },
}))

import {
  getAdminConsoleSummary,
  getAdminHighlightsFunnel,
  getAdminHighlightsFunnelRows,
  getAdminOverview,
  getEmbeddingUsage,
} from '../../lib/api'

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

describe('AdminPage 精选漏斗 tab', () => {
  beforeEach(() => {
    window.location.hash = 'admin'
    vi.mocked(getAdminOverview).mockResolvedValue({
      codes: [],
      users: [],
      fetch_runs: { runs: [], limit: 20, offset: 0 },
      embedding_usage: emptyEmbeddingUsage,
    })
    vi.mocked(getEmbeddingUsage).mockResolvedValue(emptyEmbeddingUsage)
    vi.mocked(getAdminConsoleSummary).mockResolvedValue({ available: false, reason: 'remote_required' })
    const unavailable = Object.assign(new Error('remote required'), { status: 501 })
    vi.mocked(getAdminHighlightsFunnel).mockRejectedValue(unavailable)
    vi.mocked(getAdminHighlightsFunnelRows).mockRejectedValue(unavailable)
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    window.location.hash = ''
  })

  it('从总览切换到第五 tab 并渲染 filtered 子路由', async () => {
    const { container } = render(<AdminPage />)

    fireEvent.click(screen.getByRole('button', { name: '精选漏斗' }))

    await waitFor(() => expect(window.location.hash).toBe('#admin/filtered'))
    expect(await screen.findByText('该视图仅在生产数据模式可用')).toBeInTheDocument()
    expect(container.querySelector('main')).toHaveClass('w-full', 'max-w-none')
  })
})
