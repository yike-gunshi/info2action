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
    syncAdminXList: vi.fn(),
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
  syncAdminXList,
  updateAdminSourceAlgoParams,
  validateAdminSource,
  type AdminSource,
  type AdminSourceGroup,
  type AdminSourceReconcileResponse,
  type AdminXRunSummary,
  type AdminXListStatus,
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
            last_fetched_at: '2026-07-05T12:00:00Z',
            inserted_7d: 0,
            consecutive_failures: 1,
          },
        }),
        source({
          id: 5,
          platform: 'reddit',
          source_key: 'ClaudeAI',
          display_name: 'r/ClaudeAI',
          status: 'broken',
          health: {
            last_fetched_at: '2026-07-05T12:00:00Z',
            inserted_7d: 0,
            consecutive_failures: 5,
          },
        }),
        source({
          id: 6,
          platform: 'reddit',
          source_key: 'QuietSub',
          display_name: 'r/QuietSub',
          health: {
            last_fetched_at: '2026-07-05T12:00:00Z',
            inserted_7d: 0,
            consecutive_failures: 0,
          },
        }),
      ],
    },
    {
      platform: 'x_user',
      sources: [
        source({
          id: 7,
          platform: 'x_user',
          source_key: 'openai',
          display_name: 'OpenAI',
          health: {
            last_fetched_at: '2026-07-11T01:45:00Z',
            inserted_7d: 12,
            consecutive_failures: 0,
            latest_attempt: { run_id: 77, outcome: 'success', attempts: 1, new_count: 3 },
          },
        }),
        source({
          id: 8,
          platform: 'x_user',
          source_key: 'missed_account',
          display_name: '本轮漏抓账号',
          status: 'not_fetched',
          health: {
            last_fetched_at: null,
            inserted_7d: 0,
            consecutive_failures: 0,
            latest_attempt: { run_id: 77, outcome: 'missed', attempts: 0 },
          },
        }),
        source({
          id: 9,
          platform: 'x_user',
          source_key: 'failed_account',
          display_name: '本轮失败账号',
          health: {
            last_fetched_at: '2026-07-10T01:45:00Z',
            inserted_7d: 2,
            consecutive_failures: 1,
            latest_attempt: { run_id: 77, outcome: 'failed', attempts: 3, error_code: 'rate_limited' },
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

const latestXRun: AdminXRunSummary = {
  run_id: 77,
  started_at: '2026-07-11T01:40:00Z',
  finished_at: '2026-07-11T01:45:00Z',
  planned: 3,
  attempted: 2,
  succeeded: 1,
  no_new: 1,
  failed: 1,
  missed: 1,
  mode: 'list',
  list_id: '123456',
  unmatched_posts: 0,
}

const xListStatus: AdminXListStatus = {
  configured: true,
  mode: 'list',
  list_id: '123456',
  list_url: 'https://x.com/i/lists/123456',
  registry_count: 3,
  synced_count: 2,
  pending_count: 1,
  synced_handles: ['openai', 'failed_account'],
  pending_handles: ['missed_account'],
  last_synced_at: '2026-07-11T01:35:00Z',
  last_error: null,
}

function mockSubscriptionData(
  groups = sourceGroups(),
  reconcile: AdminSourceReconcileResponse = { missing: [], imported: [], note: null },
  xRun: AdminXRunSummary | null = latestXRun,
  xList: AdminXListStatus | null = xListStatus,
) {
  vi.mocked(getAdminSources).mockResolvedValue({
    groups,
    total: groups.reduce((sum, group) => sum + group.sources.length, 0),
    latest_x_run: xRun,
    x_list: xList,
  })
  vi.mocked(getAdminSourceAlgoParams).mockResolvedValue({ params: defaultAlgoParams })
  vi.mocked(reconcileLingowhaleSources).mockResolvedValue(reconcile)
}

async function openSubscriptionTab() {
  render(<AdminPage />)
  await screen.findByText('管理面板')
  fireEvent.click(screen.getByRole('button', { name: '订阅配置' }))
  return screen.findByRole('button', { name: '添加信源' })
}

// 面板内直接展示全量列表（无弹窗），按 platform 定位面板容器
async function panelFor(platform: string) {
  return screen.findByTestId(`module-card-${platform}`)
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

  it('renders problem-first cards, run coverage, compact rows, and global search', async () => {
    await openSubscriptionTab()

    const modules = screen.getByTestId('source-module-grid')
    expect(modules).toHaveClass('min-[1280px]:grid-cols-2')
    expect(screen.getByText('本轮 X 覆盖')).toBeInTheDocument()
    expect(screen.getByText('2 / 3')).toBeInTheDocument()
    expect(screen.getByText('成功 1（1 无新增） · 失败 1 · 漏抓 1')).toBeInTheDocument()

    const wechatCard = await panelFor('wechat_mp')
    expect(within(wechatCard).queryByRole('table')).not.toBeInTheDocument()
    expect(within(wechatCard).getByText('本轮全部信源已完成')).toBeInTheDocument()
    fireEvent.click(within(wechatCard).getByRole('button', { name: /^全部 2$/ }))

    const addSourceButton = screen.getByRole('button', { name: '添加信源' })
    expect(addSourceButton).toHaveClass('border-border', 'bg-card', 'rounded-[4px]')
    expect(addSourceButton).not.toHaveClass('bg-primary', 'shadow')

    expect(within(wechatCard).getByText('公众号')).toBeInTheDocument()
    expect(within(wechatCard).getByTestId('source-row-1')).toBeInTheDocument()
    expect(within(within(wechatCard).getByTestId('source-row-1')).getByText('机器之心')).toBeInTheDocument()
    expect(within(within(wechatCard).getByTestId('source-row-1')).getByText('正常')).toBeInTheDocument()
    expect(within(within(wechatCard).getByTestId('source-row-1')).getByText('8')).toBeInTheDocument()
    expect(within(wechatCard).queryByRole('button', { name: '删除' })).not.toBeInTheDocument()

    const redditCard = await panelFor('reddit')
    expect(within(within(redditCard).getByTestId('source-row-2')).getByText('抓取重试中 · 1')).toBeInTheDocument()
    expect(within(within(redditCard).getByTestId('source-row-5')).getByText('连续失败')).toBeInTheDocument()
    expect(within(redditCard).queryByTestId('source-row-6')).not.toBeInTheDocument()
    fireEvent.click(within(redditCard).getByRole('button', { name: /^全部 3$/ }))
    expect(within(within(redditCard).getByTestId('source-row-6')).getByText('近7日无更新')).toBeInTheDocument()
    expect(within(redditCard).queryByText(/异常/)).not.toBeInTheDocument()

    const xCard = await panelFor('x_user')
    expect(within(xCard).getByText('X List 抓取')).toBeInTheDocument()
    expect(within(xCard).getByText('2 / 3 已同步')).toBeInTheDocument()
    expect(within(xCard).getByText('1 List 待同步')).toBeInTheDocument()
    expect(within(xCard).getByText('分组搜索兜底')).toBeInTheDocument()
    expect(within(xCard).getByText(/未同步账号仍按配置抓取/)).toBeInTheDocument()
    expect(within(xCard).queryByText('同步失败')).not.toBeInTheDocument()
    expect(within(xCard).getByRole('link', { name: '打开 X List' })).toHaveAttribute(
      'href',
      'https://x.com/i/lists/123456',
    )
    expect(within(xCard).getByText('本轮覆盖 2/3')).toBeInTheDocument()
    expect(within(within(xCard).getByTestId('source-row-8')).getByText('本轮漏抓')).toBeInTheDocument()
    expect(within(xCard).queryByTestId('source-row-7')).not.toBeInTheDocument()
    fireEvent.click(within(xCard).getByRole('button', { name: /^全部 3$/ }))
    expect(within(within(xCard).getByTestId('source-row-7')).getByText('成功 · 新增 3')).toBeInTheDocument()

    const biliCard = await panelFor('bilibili_up')
    fireEvent.click(within(biliCard).getByRole('button', { name: /^全部 1$/ }))
    expect(within(within(biliCard).getByTestId('source-row-3')).getByText('管线未接入')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('搜索信源'), { target: { value: '机器之心' } })
    expect(within(wechatCard).getByTestId('source-row-1')).toBeInTheDocument()
    expect(within(redditCard).getByText('没有匹配的信源')).toBeInTheDocument()
  })

  it('syncs pending registry accounts into the configured X List', async () => {
    vi.mocked(syncAdminXList).mockResolvedValue({
      ...xListStatus,
      synced_count: 3,
      pending_count: 0,
      synced_handles: ['openai', 'missed_account', 'failed_account'],
      pending_handles: [],
      last_synced_at: '2026-07-11T02:00:00Z',
      failed: [],
    })
    await openSubscriptionTab()

    const xCard = await panelFor('x_user')
    fireEvent.click(within(xCard).getByRole('button', { name: '同步 1 个待同步账号' }))

    await waitFor(() => {
      expect(syncAdminXList).toHaveBeenCalledWith(false)
      expect(within(xCard).getByText('3 / 3 已同步')).toBeInTheDocument()
    })
    expect(toast.success).toHaveBeenCalledWith('X List 已同步 3 / 3')
  })

  it('shows each configured X List with its own coverage and link', async () => {
    mockSubscriptionData(sourceGroups(), { missing: [], imported: [], note: null }, latestXRun, {
      ...xListStatus,
      list_id: null,
      list_url: null,
      lists: [
        {
          key: 'official',
          name: 'i2a · AI Official',
          list_id: '111',
          list_url: 'https://x.com/i/lists/111',
          registry_count: 1,
          synced_count: 1,
          pending_count: 0,
          synced_handles: ['openai'],
          pending_handles: [],
          last_synced_at: '2026-07-11T01:35:00Z',
          last_error: null,
        },
        {
          key: 'people',
          name: 'i2a · AI People',
          list_id: '222',
          list_url: 'https://x.com/i/lists/222',
          registry_count: 2,
          synced_count: 1,
          pending_count: 1,
          synced_handles: ['karpathy'],
          pending_handles: ['missed_account'],
          last_synced_at: '2026-07-11T01:35:00Z',
          last_error: null,
        },
      ],
    })

    await openSubscriptionTab()

    const xCard = await panelFor('x_user')
    const officialLink = within(xCard).getByRole('link', { name: '打开 i2a · AI Official' })
    const peopleLink = within(xCard).getByRole('link', { name: '打开 i2a · AI People' })
    expect(officialLink).toHaveTextContent('AI Official1/1')
    expect(peopleLink).toHaveTextContent('AI People1/2')
    expect(officialLink).toHaveAttribute(
      'href',
      'https://x.com/i/lists/111',
    )
    expect(peopleLink).toHaveAttribute(
      'href',
      'https://x.com/i/lists/222',
    )
  })

  it('does not show an older failed attempt after a newer source success', async () => {
    mockSubscriptionData([
      {
        platform: 'x_user',
        sources: [
          source({
            id: 10,
            platform: 'x_user',
            source_key: 'recovered_account',
            display_name: '已恢复账号',
            health: {
              last_fetched_at: '2026-07-11T03:00:00Z',
              inserted_7d: 2,
              consecutive_failures: 0,
              latest_attempt: {
                run_id: 77,
                outcome: 'failed',
                attempts: 1,
                error_code: 'rate_limited',
                finished_at: '2026-07-11T02:00:00Z',
              },
            },
          }),
        ],
      },
    ])
    await openSubscriptionTab()

    const xCard = await panelFor('x_user')
    fireEvent.click(within(xCard).getByRole('button', { name: /^全部 1$/ }))
    const row = within(xCard).getByTestId('source-row-10')
    expect(within(row).getByText('正常')).toBeInTheDocument()
    expect(within(row).queryByText('本轮失败')).not.toBeInTheDocument()
  })

  it('calls source status and delete endpoints from the row action menu', async () => {
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = String(input)
      if (init?.method === 'PATCH' && url === '/api/admin/sources/1') {
        return new Response(JSON.stringify({ ok: true, source: source({ id: 1, platform: 'wechat_mp', source_key: 'mp-machine', display_name: '机器之心', status: 'paused' }) }))
      }
      if (init?.method === 'PATCH' && url === '/api/admin/sources/4') {
        return new Response(JSON.stringify({ ok: true, source: source({ id: 4, platform: 'wechat_mp', source_key: 'paused-mp', display_name: '暂停公众号', status: 'active' }) }))
      }
      if (init?.method === 'PATCH' && url === '/api/admin/sources/5') {
        return new Response(JSON.stringify({ ok: true, source: source({ id: 5, platform: 'reddit', source_key: 'ClaudeAI', display_name: 'r/ClaudeAI', status: 'active' }) }))
      }
      if (init?.method === 'DELETE' && url === '/api/admin/sources/1') {
        return new Response(JSON.stringify({ ok: true, source: source({ id: 1, platform: 'wechat_mp', source_key: 'mp-machine', display_name: '机器之心', status: 'deleted' }) }))
      }
      return new Response(JSON.stringify({ error: 'unexpected request' }), { status: 500 })
    })

    await openSubscriptionTab()
    const wechatCard = await panelFor('wechat_mp')
    fireEvent.click(within(wechatCard).getByRole('button', { name: /^全部 2$/ }))

    fireEvent.click(within(screen.getByTestId('source-row-1')).getByRole('button', { name: '更多操作 机器之心' }))
    fireEvent.click(within(screen.getByTestId('source-row-1')).getByRole('button', { name: '停用' }))
    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith('/api/admin/sources/1', expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ status: 'paused' }),
      }))
    })

    fireEvent.click(within(screen.getByTestId('source-row-4')).getByRole('button', { name: '更多操作 暂停公众号' }))
    fireEvent.click(within(screen.getByTestId('source-row-4')).getByRole('button', { name: '启用' }))
    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith('/api/admin/sources/4', expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ status: 'active' }),
      }))
    })

    fireEvent.click(within(screen.getByTestId('source-row-5')).getByRole('button', { name: '更多操作 r/ClaudeAI' }))
    fireEvent.click(within(screen.getByTestId('source-row-5')).getByRole('button', { name: '重新启用' }))
    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith('/api/admin/sources/5', expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ status: 'active' }),
      }))
    })

    fireEvent.click(within(screen.getByTestId('source-row-1')).getByRole('button', { name: '更多操作 机器之心' }))
    fireEvent.click(within(screen.getByTestId('source-row-1')).getByRole('button', { name: '删除' }))
    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith('/api/admin/sources/1', expect.objectContaining({ method: 'DELETE' }))
    })
  })

  it('shows an explicit X coverage empty state before the first audited run', async () => {
    mockSubscriptionData(sourceGroups(), { missing: [], imported: [], note: null }, null)

    await openSubscriptionTab()

    const xCard = await panelFor('x_user')
    expect(within(xCard).getByText('本轮覆盖 待首轮验证')).toBeInTheDocument()
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
    const rssCard = await panelFor('rss')
    fireEvent.click(within(rssCard).getByRole('button', { name: /^全部 1$/ }))
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
    expect(screen.queryByLabelText('X Following 数量')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('X For You 数量')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Hacker News 数量'), { target: { value: '0' } })
    fireEvent.click(screen.getByRole('button', { name: '保存参数' }))
    expect(await screen.findByText('hackernews_count 范围 1-500')).toBeInTheDocument()
    expect(updateAdminSourceAlgoParams).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText('Hacker News 数量'), { target: { value: '80' } })
    vi.mocked(updateAdminSourceAlgoParams).mockResolvedValue({ params: { ...defaultAlgoParams, hackernews_count: 80 }, ok: true })
    fireEvent.click(screen.getByRole('button', { name: '保存参数' }))

    await waitFor(() => {
      expect(updateAdminSourceAlgoParams).toHaveBeenCalledWith({
        hackernews_count: 80,
        github_trending_count: 20,
        bilibili_hot_count: 25,
        bilibili_rank_count: 20,
        bilibili_videos_per_up: 3,
      })
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
