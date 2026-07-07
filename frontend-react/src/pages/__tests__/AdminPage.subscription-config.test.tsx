import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { toast } from 'sonner'

import { AdminPage } from '../AdminPage'

vi.mock('../../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/api')>()
  return {
    ...actual,
    getAdminOverview: vi.fn(),
    getFetchRun: vi.fn(),
    getFetchRuns: vi.fn(),
    getFetchRunItems: vi.fn(),
    getEmbeddingUsage: vi.fn(),
    getInviteCodes: vi.fn(),
    createInviteCodes: vi.fn(),
    deleteInviteCode: vi.fn(),
    getAdminSources: vi.fn(),
    getAdminSourceAlgoParams: vi.fn(),
    reconcileLingowhaleSources: vi.fn(),
    validateAdminSource: vi.fn(),
    createAdminSource: vi.fn(),
    updateAdminSourceAlgoParams: vi.fn(),
  }
})

vi.mock('sonner', () => ({
  toast: {
    error: vi.fn(),
    success: vi.fn(),
  },
}))

import {
  createAdminSource,
  getAdminOverview,
  getAdminSourceAlgoParams,
  getAdminSources,
  getEmbeddingUsage,
  reconcileLingowhaleSources,
  updateAdminSourceAlgoParams,
  validateAdminSource,
  type AdminSource,
  type AdminSourceGroup,
  type AdminSourceReconcileResponse,
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

const defaultAlgoParams = {
  hackernews_count: 30,
  github_trending_count: 20,
  twitter_following_count: 50,
  twitter_for_you_count: 40,
  bilibili_hot_count: 25,
  bilibili_rank_count: 20,
  bilibili_videos_per_up: 3,
}

function adminOverview() {
  return {
    codes: [],
    users: [],
    fetch_runs: { runs: [], limit: 20, offset: 0 },
    embedding_usage: emptyEmbeddingUsage,
  }
}

function source(overrides: Partial<AdminSource> & Pick<AdminSource, 'id' | 'platform' | 'source_key' | 'display_name'>): AdminSource {
  return {
    status: 'active',
    config_json: null,
    origin: 'seed',
    validated_at: null,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    health: {
      last_fetched_at: null,
      inserted_7d: null,
      consecutive_failures: 0,
    },
    ...overrides,
  }
}

function sourceGroups(): AdminSourceGroup[] {
  return [
    {
      platform: 'wechat_mp',
      sources: [
        source({
          id: 1,
          platform: 'wechat_mp',
          source_key: 'mp-machine',
          display_name: '机器之心',
          health: {
            last_fetched_at: '2026-07-05T12:00:00Z',
            inserted_7d: 8,
            consecutive_failures: 0,
          },
        }),
        source({
          id: 4,
          platform: 'wechat_mp',
          source_key: 'paused-mp',
          display_name: '暂停公众号',
          status: 'paused',
          health: {
            last_fetched_at: '2026-07-04T12:00:00Z',
            inserted_7d: 2,
            consecutive_failures: 0,
          },
        }),
      ],
    },
    {
      platform: 'reddit',
      sources: [
        source({
          id: 2,
          platform: 'reddit',
          source_key: 'OpenAI',
          display_name: 'r/OpenAI',
          health: {
            last_fetched_at: null,
            inserted_7d: 0,
            consecutive_failures: 1,
          },
        }),
      ],
    },
    {
      platform: 'bilibili_up',
      sources: [
        source({
          id: 3,
          platform: 'bilibili_up',
          source_key: '12345',
          display_name: '影视飓风',
          status: 'not_fetched',
        }),
      ],
    },
  ]
}

function mockSubscriptionData(groups = sourceGroups(), reconcile: AdminSourceReconcileResponse = { missing: [], imported: [], note: null }) {
  vi.mocked(getAdminSources).mockResolvedValue({ groups, total: groups.reduce((sum, group) => sum + group.sources.length, 0) })
  vi.mocked(getAdminSourceAlgoParams).mockResolvedValue({ params: defaultAlgoParams })
  vi.mocked(reconcileLingowhaleSources).mockResolvedValue(reconcile)
}

async function openSubscriptionTab() {
  render(<AdminPage />)
  await screen.findByText('管理面板')
  fireEvent.click(screen.getByRole('button', { name: '订阅配置' }))
  return screen.findByText('名单型信源总览')
}

describe('AdminPage subscription config', () => {
  beforeEach(() => {
    vi.mocked(getAdminOverview).mockResolvedValue(adminOverview())
    vi.mocked(getEmbeddingUsage).mockResolvedValue(emptyEmbeddingUsage)
    mockSubscriptionData()
    Object.defineProperty(window, 'confirm', {
      configurable: true,
      value: vi.fn(() => true),
    })
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.clearAllMocks()
  })

  it('renders grouped sources with health columns and explicit not_fetched label', async () => {
    await openSubscriptionTab()

    expect(screen.getByText('公众号')).toBeInTheDocument()
    expect(screen.getByText('Reddit')).toBeInTheDocument()
    expect(screen.getByText('B站')).toBeInTheDocument()

    expect(within(screen.getByTestId('source-row-1')).getByText('机器之心')).toBeInTheDocument()
    expect(within(screen.getByTestId('source-row-1')).getByText('正常')).toBeInTheDocument()
    expect(within(screen.getByTestId('source-row-1')).getByText('8')).toBeInTheDocument()

    expect(within(screen.getByTestId('source-row-2')).getByText('异常 · 7 日 0 产出')).toBeInTheDocument()
    expect(within(screen.getByTestId('source-row-2')).getByText('0')).toBeInTheDocument()

    expect(within(screen.getByTestId('source-row-3')).getByText('配置存在但未在抓')).toBeInTheDocument()
    expect(within(screen.getByTestId('source-row-3')).getAllByText('—').length).toBeGreaterThan(0)
  })

  it('calls source status and delete endpoints from row actions', async () => {
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = String(input)
      if (init?.method === 'PATCH' && url === '/api/admin/sources/1') {
        return new Response(JSON.stringify({ ok: true, source: source({ id: 1, platform: 'wechat_mp', source_key: 'mp-machine', display_name: '机器之心', status: 'paused' }) }))
      }
      if (init?.method === 'PATCH' && url === '/api/admin/sources/4') {
        return new Response(JSON.stringify({ ok: true, source: source({ id: 4, platform: 'wechat_mp', source_key: 'paused-mp', display_name: '暂停公众号', status: 'active' }) }))
      }
      if (init?.method === 'DELETE' && url === '/api/admin/sources/1') {
        return new Response(JSON.stringify({ ok: true, source: source({ id: 1, platform: 'wechat_mp', source_key: 'mp-machine', display_name: '机器之心', status: 'deleted' }) }))
      }
      return new Response(JSON.stringify({ error: 'unexpected request' }), { status: 500 })
    })

    await openSubscriptionTab()

    fireEvent.click(within(screen.getByTestId('source-row-1')).getByRole('button', { name: '停用' }))
    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith('/api/admin/sources/1', expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ status: 'paused' }),
      }))
    })

    fireEvent.click(within(screen.getByTestId('source-row-4')).getByRole('button', { name: '启用' }))
    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith('/api/admin/sources/4', expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ status: 'active' }),
      }))
    })

    fireEvent.click(within(screen.getByTestId('source-row-1')).getByRole('button', { name: '删除' }))
    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith('/api/admin/sources/1', expect.objectContaining({ method: 'DELETE' }))
    })
  })

  it('runs add wizard validate preview create flow', async () => {
    vi.mocked(validateAdminSource).mockResolvedValue({
      status: 'ok',
      platform: 'rss',
      source_key: 'https://example.com/feed.xml',
      display_name: 'Example Feed',
      preview: [
        { title: 'Preview 1', url: 'https://example.com/1', published_at: '2026-07-05T10:00:00Z', summary: 'one' },
        { title: 'Preview 2', url: 'https://example.com/2', published_at: '2026-07-05T09:00:00Z', summary: 'two' },
        { title: 'Preview 3', url: 'https://example.com/3', published_at: '2026-07-05T08:00:00Z', summary: 'three' },
      ],
    })
    vi.mocked(createAdminSource).mockResolvedValue({
      ok: true,
      source: source({ id: 9, platform: 'rss', source_key: 'https://example.com/feed.xml', display_name: 'Example Feed' }),
    })

    await openSubscriptionTab()
    fireEvent.click(screen.getByRole('button', { name: '添加信源' }))
    fireEvent.click(screen.getByRole('radio', { name: /^RSS/ }))
    fireEvent.click(screen.getByRole('button', { name: '下一步' }))
    fireEvent.change(screen.getByLabelText('source_key'), { target: { value: 'https://example.com/feed.xml' } })
    fireEvent.click(screen.getByRole('button', { name: '校验' }))

    expect(await screen.findByText('Preview 1')).toBeInTheDocument()
    expect(validateAdminSource).toHaveBeenCalledWith({ platform: 'rss', source_key: 'https://example.com/feed.xml' })

    fireEvent.click(screen.getByRole('button', { name: '确认入库' }))
    await waitFor(() => {
      expect(createAdminSource).toHaveBeenCalledWith(expect.objectContaining({
        platform: 'rss',
        source_key: 'https://example.com/feed.xml',
        display_name: 'Example Feed',
        status: 'active',
      }))
    })
    expect(await screen.findByText('Example Feed')).toBeInTheDocument()
  })

  it('keeps source_key input for whitelist errors and shows deferred platform guidance inline', async () => {
    vi.mocked(validateAdminSource)
      .mockRejectedValueOnce(new Error('source_key must be a valid subreddit name'))
      .mockResolvedValueOnce({
        status: 'deferred',
        platform: 'x_user',
        source_key: 'openai',
        reason: 'X validation requires the local twitter CLI session.',
        preview: [],
      })

    await openSubscriptionTab()
    fireEvent.click(screen.getByRole('button', { name: '添加信源' }))
    fireEvent.click(screen.getByRole('radio', { name: /Reddit/ }))
    fireEvent.click(screen.getByRole('button', { name: '下一步' }))
    fireEvent.change(screen.getByLabelText('source_key'), { target: { value: 'bad sub' } })
    fireEvent.click(screen.getByRole('button', { name: '校验' }))

    expect(await screen.findByText('source_key must be a valid subreddit name')).toBeInTheDocument()
    expect(screen.getByLabelText('source_key')).toHaveValue('bad sub')
    expect(toast.error).not.toHaveBeenCalledWith('source_key must be a valid subreddit name')

    fireEvent.click(screen.getByRole('button', { name: '上一步' }))
    fireEvent.click(screen.getByRole('radio', { name: /X/ }))
    fireEvent.click(screen.getByRole('button', { name: '下一步' }))
    fireEvent.change(screen.getByLabelText('source_key'), { target: { value: 'openai' } })
    fireEvent.click(screen.getByRole('button', { name: '校验' }))

    expect(await screen.findByText('X validation requires the local twitter CLI session.')).toBeInTheDocument()
    expect(screen.getByText('后端 deferred')).toBeInTheDocument()
  })

  it('validates and saves algorithm source parameters', async () => {
    await openSubscriptionTab()

    expect(screen.getByText('算法源没有名单，不能添加名字')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Hacker News 数量'), { target: { value: '0' } })
    fireEvent.click(screen.getByRole('button', { name: '保存参数' }))
    expect(await screen.findByText('hackernews_count 范围 1-500')).toBeInTheDocument()
    expect(updateAdminSourceAlgoParams).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText('Hacker News 数量'), { target: { value: '80' } })
    vi.mocked(updateAdminSourceAlgoParams).mockResolvedValue({ params: { ...defaultAlgoParams, hackernews_count: 80 }, ok: true })
    fireEvent.click(screen.getByRole('button', { name: '保存参数' }))

    await waitFor(() => {
      expect(updateAdminSourceAlgoParams).toHaveBeenCalledWith({ ...defaultAlgoParams, hackernews_count: 80 })
    })
  })

  it('shows reconcile banner only when Lingowhale has missing managed subscriptions', async () => {
    mockSubscriptionData(sourceGroups(), {
      missing: [{ platform: 'wechat_mp', source_key: 'new-mp', display_name: '新公众号' }],
      imported: [],
      note: null,
    })

    await openSubscriptionTab()

    expect(screen.getByText('语鲸侧有 1 个未纳管订阅')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看并导入' }))
    expect(screen.getByText('新公众号')).toBeInTheDocument()

    cleanup()
    vi.clearAllMocks()
    vi.mocked(getAdminOverview).mockResolvedValue(adminOverview())
    vi.mocked(getEmbeddingUsage).mockResolvedValue(emptyEmbeddingUsage)
    mockSubscriptionData(sourceGroups(), { missing: [], imported: [], note: 'data/lingowhale/groups.json does not exist' })

    await openSubscriptionTab()
    expect(screen.queryByText(/语鲸侧有/)).not.toBeInTheDocument()
  })
})
