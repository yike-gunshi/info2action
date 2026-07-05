import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { ActionsView } from '../ActionsView'
import { useActionStore } from '../../../store/actionStore'
import { useAuthStore } from '../../../store/authStore'
import { useDetailStore } from '../../../store/detailStore'
import type { ActionItem } from '../../../lib/types'
import { fetchAction, fetchActionsBoard, markActionDone } from '../../../lib/api'
import type { ActionsBoardResponse } from '../../../lib/api'

vi.mock('../../../lib/api', () => ({
  fetchAction: vi.fn(),
  fetchActionsBoard: vi.fn(),
  markActionDone: vi.fn().mockResolvedValue({ ok: true }),
  dismissAction: vi.fn().mockResolvedValue({ ok: true }),
  dispatchAction: vi.fn().mockResolvedValue({ ok: true }),
  updateActionPriority: vi.fn().mockResolvedValue({ ok: true }),
  updateAction: vi.fn().mockResolvedValue({ ok: true }),
}))

const mockFetchAction = fetchAction as unknown as ReturnType<typeof vi.fn>
const mockFetchActionsBoard = fetchActionsBoard as unknown as ReturnType<typeof vi.fn>
const mockMarkActionDone = markActionDone as unknown as ReturnType<typeof vi.fn>

function makeAction(overrides: Partial<ActionItem> = {}): ActionItem {
  return {
    id: 'act-1',
    title: '验证行动卡片的克制视觉',
    type: 'implementation',
    status: 'pending',
    priority: 'P1',
    created_at: '2026-05-18T08:00:00Z',
    direction: 'implementation',
    direction_label: 'Agent 生态',
    steps: ['确认输入', '整理输出'],
    prompt: '1. 确认输入\n2. 整理输出',
    reason: '这是推理理由，不应该出现在行动卡片正文',
    ai_reasoning: '这是 AI 推理，不应该出现在行动卡片正文',
    decision_brief: '这是决策简报，不应该出现在行动卡片正文',
    source_item_ids: ['feed-item-123456'],
    source_items: [
      {
        id: 'feed-item-123456',
        platform: 'twitter',
        title: '真实来源标题',
        ai_summary: '真实来源摘要',
        url: 'https://example.com/source',
        referenced_urls: [],
      },
    ],
    source_item_count: 1,
    ...overrides,
  }
}

function boardResponse(): ActionsBoardResponse {
  return {
    counts: {
      total: 3,
      pending: 2,
      confirmed: 1,
      executing: 0,
      dispatched: 0,
      in_progress: 1,
      done: 0,
      failed: 0,
      dismissed: 0,
    },
    directions: [
      {
        slug: 'pending',
        label: '待处理',
        count: 2,
        has_more: false,
        next_offset: null,
        items: [
          makeAction({ id: 'act-1', status: 'pending', priority: 'P1', title: '验证行动卡片的克制视觉' }),
          makeAction({ id: 'act-4', status: 'pending', priority: 'P0', title: '评估 Sakana Fugu 集成价值' }),
        ],
      },
      {
        slug: 'in_progress',
        label: '执行中',
        count: 1,
        has_more: false,
        next_offset: null,
        items: [
          makeAction({
            id: 'act-2',
            status: 'confirmed',
            priority: 'P0',
            type: 'research',
            title: '判断投资线索优先级',
            steps: undefined,
            prompt: '1. 对比 Claude Code 成本\n2. 输出采用建议',
          }),
        ],
      },
      {
        slug: 'done',
        label: '已完成',
        count: 0,
        has_more: false,
        next_offset: null,
        items: [],
      },
    ],
    meta: { limit_per_direction: 20, offset: 0, degraded: false },
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

describe('ActionsView v19 visual constraints', () => {
  beforeEach(() => {
    mockFetchAction.mockImplementation((id: string) => Promise.resolve(makeAction({ id })))
    mockFetchActionsBoard.mockResolvedValue(boardResponse())
    useActionStore.setState({
      actions: [],
      counts: {},
      directions: [],
      isLoading: false,
      focusedActionId: null,
    })
    useDetailStore.setState({
      modalStack: [],
      itemDetail: null,
      itemActions: [],
      actionDetail: null,
      isLoading: false,
    })
    useAuthStore.setState({
      user: { id: 'u1', username: 'tester', email: 'tester@example.com', role: 'user', has_discord_token: true },
      isLoading: false,
      isChecked: true,
    })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('renders the shared L2 token with time plus optional priority filters and three fixed lanes', async () => {
    render(<ActionsView />)

    expect(screen.queryByText('我的行动')).toBeNull()
    expect(await screen.findByTestId('actions-filter-row')).toBeInTheDocument()
    expect(screen.getByTestId('actions-filter-row').className).toContain('sticky')
    expect(screen.getByTestId('actions-filter-row').className).toContain('sm:top-[52px]')
    expect(screen.getByTestId('actions-l2-tabs').className).toContain('max-w-[1168px]')
    expect(screen.getByTestId('actions-l2-tabs').className).toContain('border-b')
    expect(screen.getByTestId('actions-date-tab-all').className).toContain('border-[var(--brand)]')
    expect(screen.getByTestId('actions-date-tab-all').className).toContain('font-event-title')
    expect(screen.getByTestId('actions-date-tab-all').className).toContain('text-[16px]')
    expect(screen.getByTestId('actions-l2-divider')).toHaveTextContent('|')
    expect(screen.getByTestId('actions-priority-tab-P0')).toHaveTextContent('P0')
    expect(screen.getByTestId('actions-priority-tab-P1')).toHaveTextContent('P1')
    expect(screen.getByTestId('actions-priority-tab-P2')).toHaveTextContent('P2')
    expect(screen.getByTestId('actions-priority-tab-P0')).toHaveAttribute('aria-pressed', 'false')
    expect(screen.queryByTestId('actions-status-tab-all')).toBeNull()
    expect(screen.queryByTestId('action-section-pill-bar')).toBeNull()
    expect(screen.queryByRole('combobox')).toBeNull()

    const laneGrid = screen.getByTestId('actions-lane-grid')
    expect(laneGrid.className).toContain('lg:grid-cols-3')
    const lanes = screen.getAllByTestId('action-lane')
    expect(lanes).toHaveLength(3)
    expect(lanes.map((lane) => lane.getAttribute('data-lane'))).toEqual(['pending', 'in_progress', 'done'])
    expect(within(lanes[0]).getByRole('heading', { name: '待处理' })).toBeInTheDocument()
    expect(within(lanes[1]).getByRole('heading', { name: '执行中' })).toBeInTheDocument()
    expect(within(lanes[2]).getByRole('heading', { name: '已完成' })).toBeInTheDocument()
    expect(screen.getByText('暂无已完成行动')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('actions-priority-tab-P0'))
    await waitFor(() => expect(mockFetchActionsBoard).toHaveBeenLastCalledWith(expect.objectContaining({
      priority: 'P0',
      limit_per_direction: 20,
    })))

    fireEvent.click(screen.getByTestId('actions-priority-tab-P0'))
    await waitFor(() => expect(mockFetchActionsBoard).toHaveBeenLastCalledWith(expect.objectContaining({
      priority: undefined,
      limit_per_direction: 20,
    })))
  })

  it('shows lane-shaped skeletons while loading data', async () => {
    mockFetchActionsBoard.mockReturnValue(new Promise(() => {}))

    render(<ActionsView />)

    await waitFor(() => expect(screen.getAllByTestId('action-lane-skeleton')).toHaveLength(3))
    expect(screen.getByText('待处理')).toBeInTheDocument()
    expect(screen.getByText('执行中')).toBeInTheDocument()
    expect(screen.getByText('已完成')).toBeInTheDocument()
    expect(screen.queryByTestId('actions-lane-grid')).toBeNull()
  })

  it('does not keep stale lane counts under a newly selected uncached L2 priority', async () => {
    const p2Board = deferred<ActionsBoardResponse>()
    mockFetchActionsBoard.mockImplementation((params?: { priority?: string }) => {
      if (params?.priority === 'P2') return p2Board.promise
      return Promise.resolve({
        ...boardResponse(),
        counts: { total: 312, pending: 312, in_progress: 0, done: 0 },
        directions: [
          {
            slug: 'pending',
            label: '待处理',
            count: 312,
            has_more: true,
            next_offset: 20,
            items: [makeAction({ id: 'act-p1', status: 'pending', priority: 'P1', title: 'P1 旧数据' })],
          },
          { slug: 'in_progress', label: '执行中', count: 0, has_more: false, next_offset: null, items: [] },
          { slug: 'done', label: '已完成', count: 0, has_more: false, next_offset: null, items: [] },
        ],
      })
    })

    render(<ActionsView />)

    expect(await screen.findByText('P1 旧数据')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('actions-priority-tab-P2'))

    await waitFor(() => expect(screen.getByTestId('actions-priority-tab-P2')).toHaveAttribute('aria-pressed', 'true'))
    expect(screen.getByTestId('actions-board-skeleton')).toBeInTheDocument()
    expect(screen.queryByText('P1 旧数据')).toBeNull()
    expect(screen.queryByText('312')).toBeNull()

    p2Board.resolve({
      ...boardResponse(),
      counts: { total: 202, pending: 202, confirmed: 0, executing: 0, dispatched: 0, in_progress: 0, done: 0, failed: 0, dismissed: 0 },
      directions: [
        {
          slug: 'pending',
          label: '待处理',
          count: 202,
          has_more: true,
          next_offset: 20,
          items: [makeAction({ id: 'act-p2', status: 'pending', priority: 'P2', title: 'P2 新数据' })],
        },
        { slug: 'in_progress', label: '执行中', count: 0, has_more: false, next_offset: null, items: [] },
        { slug: 'done', label: '已完成', count: 0, has_more: false, next_offset: null, items: [] },
      ],
    })

    await waitFor(() => expect(screen.getByText('P2 新数据')).toBeInTheDocument())
    expect(screen.queryByText('P1 旧数据')).toBeNull()
    expect(screen.getByText('202')).toBeInTheDocument()
  })

  it('keeps action cards at 1px border, 4px radius, no shadow, and small warm status pills', async () => {
    render(<ActionsView />)

    await waitFor(() => expect(screen.getAllByTestId('action-card')).toHaveLength(3))

    for (const card of screen.getAllByTestId('action-card')) {
      expect(card.className).toContain('border')
      expect(card.className).toContain('rounded-[4px]')
      expect(card.className).toContain('p-4')
      expect(card.className).toContain('shadow-none')
      expect(card.className).not.toContain('shadow-subtle')
      expect(card.className).not.toContain('hover:-translate')
      expect(card.className).not.toContain('rounded-lg')
    }

    const statusPill = screen.getAllByTestId('action-status-pill')[0]
    expect(statusPill.className).toContain('rounded-full')
    expect(statusPill.className).toContain('text-[12px]')
    expect(statusPill.className).toContain('bg-[var(--brand-soft)]')

    const typePill = screen.getAllByTestId('action-type-pill')[0]
    expect(typePill.className).toContain('bg-[var(--brand-soft)]')
    const createdAt = screen.getAllByTestId('action-card-created-at')[0]
    expect(createdAt).toHaveTextContent('2026-05-18')
    expect(createdAt).not.toHaveTextContent('16:00')
    expect(typePill.compareDocumentPosition(createdAt) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByText('来自 16:00')).toBeNull()
    expect(screen.queryByText('2026-05-18 16:00')).toBeNull()
    expect(screen.queryByText('来自 2026-05-18 16:00')).toBeNull()
    expect(screen.getAllByText('来自 1 条信息')[0]).toBeInTheDocument()
    expect(screen.queryByText('关联信息')).toBeNull()
    expect(screen.queryByText('📄')).toBeNull()
    expect(screen.queryByText('⚡')).toBeNull()

    const confirmedCard = screen.getByText('判断投资线索优先级').closest('[data-testid="action-card"]')
    expect(confirmedCard).toBeTruthy()
    expect(within(confirmedCard as HTMLElement).getByTestId('action-status-pill')).toHaveTextContent('执行中')
    expect(screen.queryByText('已确认')).toBeNull()
    expect(screen.queryByText('已派发')).toBeNull()
  })

  it('shows concrete action steps as unordered lists instead of reasoning copy', async () => {
    render(<ActionsView />)

    await waitFor(() => expect(screen.getAllByTestId('action-card')).toHaveLength(3))

    expect(screen.queryByText('确认输入；整理输出')).toBeNull()
    const actionPointLists = screen.getAllByTestId('action-point-list')
    expect(actionPointLists.length).toBeGreaterThan(0)
    expect(actionPointLists[0].className).toContain('space-y-1.5')
    expect(actionPointLists[0].className).toContain('font-event-title')
    expect(actionPointLists[0].className).toContain('text-[15px]')
    expect(actionPointLists[0].className).toContain('leading-[1.58]')
    expect(actionPointLists[0].className).not.toContain('pl-4')
    expect(screen.getAllByTestId('action-point-dot')[0].className).toContain('rounded-full')
    expect(screen.getAllByTestId('action-point-dot')[0].className).toContain('bg-[var(--brand)]')
    expect(screen.getAllByTestId('action-point-dot')[0].className).toContain('opacity-70')
    expect(screen.getAllByText('确认输入')[0].closest('li')).toBeTruthy()
    expect(screen.getAllByText('整理输出')[0].closest('li')).toBeTruthy()
    expect(screen.getByText('对比 Claude Code 成本').closest('li')).toBeTruthy()
    expect(screen.getByText('输出采用建议').closest('li')).toBeTruthy()
    expect(screen.queryByText('这是推理理由，不应该出现在行动卡片正文')).toBeNull()
    expect(screen.queryByText('这是 AI 推理，不应该出现在行动卡片正文')).toBeNull()
    expect(screen.queryByText('这是决策简报，不应该出现在行动卡片正文')).toBeNull()
  })

  it('opens immediately when the action list already contains the complete detail read model', async () => {
    render(<ActionsView />)

    await waitFor(() => expect(screen.getAllByTestId('action-card')).toHaveLength(3))
    fireEvent.click(screen.getAllByTestId('action-card')[0])

    expect(mockFetchAction).not.toHaveBeenCalled()
    expect(useDetailStore.getState().modalStack).toEqual([{ type: 'action', id: 'act-1' }])
    expect(useDetailStore.getState().actionDetail?.source_items?.[0]?.title).toBe('真实来源标题')
    expect(screen.queryByText('加载详情...')).toBeNull()
    expect(screen.getAllByTestId('action-card')[0]).not.toHaveAttribute('data-opening')
  })

  it('loads one status lane page and reveals the appended card without a collapse button', async () => {
    mockFetchActionsBoard.mockReset()
    mockFetchActionsBoard
      .mockResolvedValueOnce({
        counts: { total: 3, pending: 3, in_progress: 0, done: 0 },
        directions: [
          {
            slug: 'pending',
            label: '待处理',
            count: 3,
            has_more: true,
            next_offset: 1,
            items: [makeAction({ id: 'act-1', status: 'pending', title: '第一条行动' })],
          },
          { slug: 'in_progress', label: '执行中', count: 0, has_more: false, next_offset: null, items: [] },
          { slug: 'done', label: '已完成', count: 0, has_more: false, next_offset: null, items: [] },
        ],
        meta: { limit_per_direction: 20, offset: 0, degraded: false },
      })
      .mockResolvedValueOnce({
        counts: { total: 3, pending: 3, in_progress: 0, done: 0 },
        directions: [
          {
            slug: 'pending',
            label: '待处理',
            count: 3,
            has_more: true,
            next_offset: 2,
            items: [makeAction({ id: 'act-2', status: 'pending', title: '第二条行动' })],
          },
        ],
        meta: { limit_per_direction: 20, offset: 1, degraded: false },
      })
      .mockResolvedValueOnce({
        counts: { total: 3, pending: 3, in_progress: 0, done: 0 },
        directions: [
          {
            slug: 'pending',
            label: '待处理',
            count: 3,
            has_more: false,
            next_offset: null,
            items: [makeAction({ id: 'act-3', status: 'pending', title: '第三条行动' })],
          },
        ],
        meta: { limit_per_direction: 20, offset: 2, degraded: false },
      })

    render(<ActionsView />)

    expect(await screen.findByText('第一条行动')).toBeInTheDocument()
    expect(screen.queryByText('第二条行动')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '展开更多 还有 2 条' }))

    await waitFor(() => expect(mockFetchActionsBoard).toHaveBeenLastCalledWith(expect.objectContaining({
      status: 'pending',
      limit_per_direction: 20,
      offset: 1,
    })))
    await waitFor(() => expect(screen.getByText('第二条行动')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: '展开更多 还有 1 条' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '展开更多 还有 1 条' }))

    await waitFor(() => expect(mockFetchActionsBoard).toHaveBeenLastCalledWith(expect.objectContaining({
      status: 'pending',
      limit_per_direction: 20,
      offset: 2,
    })))
    await waitFor(() => expect(screen.getByText('第三条行动')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /展开更多/ })).toBeNull()
    expect(screen.queryByRole('button', { name: '收起' })).toBeNull()
    expect(screen.queryByRole('button', { name: '新建行动' })).toBeNull()
  })

  it('clears the board cache and refetches after action status mutation', async () => {
    mockFetchActionsBoard.mockReset()
    mockFetchActionsBoard
      .mockResolvedValueOnce(boardResponse())
      .mockResolvedValueOnce({
        ...boardResponse(),
        counts: { total: 3, pending: 1, confirmed: 1, executing: 0, dispatched: 0, in_progress: 1, done: 1 },
        directions: [
          {
            slug: 'pending',
            label: '待处理',
            count: 1,
            has_more: false,
            next_offset: null,
            items: [makeAction({ id: 'act-4', status: 'pending', priority: 'P0', title: '评估 Sakana Fugu 集成价值' })],
          },
          {
            slug: 'in_progress',
            label: '执行中',
            count: 1,
            has_more: false,
            next_offset: null,
            items: [makeAction({ id: 'act-2', status: 'confirmed', priority: 'P0', title: '判断投资线索优先级' })],
          },
          {
            slug: 'done',
            label: '已完成',
            count: 1,
            has_more: false,
            next_offset: null,
            items: [makeAction({ id: 'act-1', status: 'done', priority: 'P1', title: '验证行动卡片的克制视觉' })],
          },
        ],
      })

    render(<ActionsView />)

    const card = (await screen.findByText('验证行动卡片的克制视觉')).closest('[data-testid="action-card"]')
    expect(card).toBeTruthy()
    fireEvent.click(within(card as HTMLElement).getByLabelText('更多行动操作'))
    fireEvent.click(screen.getByRole('button', { name: '已完成' }))

    await waitFor(() => expect(mockMarkActionDone).toHaveBeenCalledWith('act-1'))
    await waitFor(() => expect(mockFetchActionsBoard).toHaveBeenCalledTimes(2))
    const doneLane = screen.getAllByTestId('action-lane')[2]
    await waitFor(() => expect(within(doneLane).getByText('验证行动卡片的克制视觉')).toBeInTheDocument())
    const doneCard = within(doneLane).getByText('验证行动卡片的克制视觉').closest('[data-testid="action-card"]')
    expect(within(doneCard as HTMLElement).getByTestId('action-status-pill')).toHaveTextContent('已完成')
  })
})
