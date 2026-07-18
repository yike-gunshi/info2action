/**
 * v24.0 模块 21.4: 行动 tab 拖拽看板 — 组件级测试。
 * jsdom 无布局(rect 全 0),按社区定式(dnd-kit issue #261)走 KeyboardSensor +
 * mock getBoundingClientRect 驱动完整「拾起-移动-落下」;指针流只在真浏览器验收。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { toast } from 'sonner'
import { ActionsView } from '../ActionsView'
import { useActionStore } from '../../../store/actionStore'
import { useAuthStore } from '../../../store/authStore'
import { useDetailStore } from '../../../store/detailStore'
import type { ActionItem } from '../../../lib/types'
import { fetchAction, fetchActionsBoard, markActionDone, updateAction } from '../../../lib/api'
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

vi.mock('sonner', () => {
  const toastFn = Object.assign(vi.fn(() => 'undo-toast-id'), {
    error: vi.fn(),
    dismiss: vi.fn(),
    success: vi.fn(),
  })
  return { toast: toastFn }
})

const mockFetchAction = fetchAction as unknown as ReturnType<typeof vi.fn>
const mockFetchActionsBoard = fetchActionsBoard as unknown as ReturnType<typeof vi.fn>
const mockMarkActionDone = markActionDone as unknown as ReturnType<typeof vi.fn>
const mockUpdateAction = updateAction as unknown as ReturnType<typeof vi.fn>
const toastMock = toast as unknown as ReturnType<typeof vi.fn> & {
  error: ReturnType<typeof vi.fn>
  dismiss: ReturnType<typeof vi.fn>
}

// ---- 几何 mock: 三泳道横排,卡片与 DragOverlay 都给真实尺寸 ----
// 必须挂在原型上:DragOverlay 是拖拽开始后新挂载的 portal 节点,实例级 mock 覆盖不到,
// 而 dnd-kit 在有 overlay 时用 overlay rect 作 collisionRect(否则全 0,碰撞永远为空)。
const LANE_X: Record<string, number> = { pending: 0, in_progress: 340, done: 680 }

function rectOf(x: number, y: number, width: number, height: number): DOMRect {
  return {
    x, y, left: x, top: y, right: x + width, bottom: y + height, width, height,
    toJSON: () => ({}),
  } as DOMRect
}

function boardAwareRect(element: Element): DOMRect {
  const testId = element.getAttribute?.('data-testid')
  if (testId === 'action-lane') {
    const key = element.getAttribute('data-lane') ?? 'pending'
    return rectOf(LANE_X[key] ?? 0, 0, 320, 600)
  }
  if (testId === 'action-card' || testId === 'action-drag-overlay') {
    const laneKey = element.closest('[data-testid="action-lane"]')?.getAttribute('data-lane') ?? 'pending'
    return rectOf((LANE_X[laneKey] ?? 0) + 10, 60, 300, 140)
  }
  // dnd-kit DragOverlay 容器(其唯一子元素是我们的 overlay 卡片)
  if (element.firstElementChild?.getAttribute?.('data-testid') === 'action-drag-overlay') {
    return rectOf(10, 60, 300, 140)
  }
  return rectOf(0, 0, 0, 0)
}

const originalGetBoundingClientRect = Element.prototype.getBoundingClientRect

// ---- jsdom DOM API 补齐(dnd-kit 依赖) ----
beforeEach(() => {
  Object.defineProperty(Element.prototype, 'scrollIntoView', {
    value: vi.fn(),
    writable: true,
    configurable: true,
  })
  Element.prototype.getBoundingClientRect = function getBoundingClientRectMock() {
    return boardAwareRect(this)
  }
  if (!Element.prototype.animate) {
    Object.defineProperty(Element.prototype, 'animate', {
      value: function animateStub() {
        const animation = { onfinish: null as null | (() => void), cancel() {}, finish() {} }
        queueMicrotask(() => animation.onfinish?.())
        return animation
      },
      writable: true,
      configurable: true,
    })
  }
})

afterEach(() => {
  Element.prototype.getBoundingClientRect = originalGetBoundingClientRect
})

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
          makeAction({ id: 'act-1', status: 'pending', title: '验证行动卡片的克制视觉' }),
          makeAction({ id: 'act-4', status: 'pending', title: '评估 Sakana Fugu 集成价值' }),
        ],
      },
      {
        slug: 'in_progress',
        label: '执行中',
        count: 1,
        has_more: false,
        next_offset: null,
        items: [
          makeAction({ id: 'act-2', status: 'confirmed', title: '判断投资线索优先级' }),
        ],
      },
      { slug: 'done', label: '已完成', count: 0, has_more: false, next_offset: null, items: [] },
    ],
    meta: { limit_per_direction: 20, offset: 0, degraded: false },
  }
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function flushDnd(ms = 40) {
  await act(async () => {
    await sleep(ms)
  })
}

function getLane(key: 'pending' | 'in_progress' | 'done'): HTMLElement {
  const lane = screen.getAllByTestId('action-lane').find((el) => el.getAttribute('data-lane') === key)
  expect(lane).toBeTruthy()
  return lane as HTMLElement
}

function getCard(title: string): HTMLElement {
  const card = screen.getByText(title).closest('[data-testid="action-card"]')
  expect(card).toBeTruthy()
  return card as HTMLElement
}

async function pickUp(card: HTMLElement) {
  card.focus()
  fireEvent.keyDown(card, { key: ' ', code: 'Space' })
  await flushDnd() // KeyboardSensor 在 setTimeout 后挂 keydown 监听 + 泳道测量
}

async function arrow(card: HTMLElement, code: 'ArrowLeft' | 'ArrowRight' | 'ArrowUp' | 'ArrowDown') {
  fireEvent.keyDown(card, { key: code, code })
  await flushDnd(20)
}

async function dropWith(card: HTMLElement, code: 'Enter' | 'Space' | 'Escape') {
  fireEvent.keyDown(card, { key: code === 'Space' ? ' ' : code, code })
  await flushDnd()
}

async function renderBoard() {
  render(<ActionsView />)
  await waitFor(() => expect(screen.getAllByTestId('action-card')).toHaveLength(3))
}

describe('ActionsView v24 dnd board (21.4)', () => {
  beforeEach(() => {
    mockFetchAction.mockImplementation((id: string) => Promise.resolve(makeAction({ id })))
    mockFetchActionsBoard.mockResolvedValue(boardResponse())
    mockUpdateAction.mockResolvedValue({ ok: true })
    mockMarkActionDone.mockResolvedValue({ ok: true })
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

  it('键盘流 待处理→执行中: Space 拾起 → → Enter 落下,落 confirmed 且不整板重拉(冻结 refetch 硬断言)', async () => {
    await renderBoard()
    expect(mockFetchActionsBoard).toHaveBeenCalledTimes(1)

    const card = getCard('验证行动卡片的克制视觉')
    await pickUp(card)

    // 拾起态: 源卡 opacity-40 + DragOverlay 同卡克隆(倾斜层)
    expect(card).toHaveAttribute('data-drag-state', 'source')
    expect(card.className).toContain('opacity-40')
    const overlay = screen.getByTestId('action-drag-overlay')
    expect(overlay.className).toContain('action-drag-overlay')
    expect(overlay.className).toContain('shadow-medium')
    expect(overlay.className).toContain('border-[var(--brand-border)]')
    expect(within(overlay).getByText('验证行动卡片的克制视觉')).toBeInTheDocument()

    await arrow(card, 'ArrowRight')

    // 目标泳道整体高亮,源泳道不高亮
    await waitFor(() => expect(getLane('in_progress')).toHaveAttribute('data-drop-target', 'true'))
    expect(getLane('in_progress').className).toContain('bg-[var(--brand-soft)]')
    expect(getLane('pending')).not.toHaveAttribute('data-drop-target')
    expect(getLane('pending').className).not.toContain('bg-[var(--brand-soft)]')

    await dropWith(card, 'Enter')

    await waitFor(() => expect(mockUpdateAction).toHaveBeenCalledWith('act-1', { status: 'confirmed' }))
    expect(useActionStore.getState().actions.find((a) => a.id === 'act-1')?.status).toBe('confirmed')
    await waitFor(() => expect(within(getLane('in_progress')).getByText('验证行动卡片的克制视觉')).toBeInTheDocument())

    // 泳道计数即时 ±1(泳道头 = laneMeta 驱动)
    const laneCounts = screen.getAllByTestId('action-lane-count').map((el) => el.textContent)
    expect(laneCounts).toEqual(['1', '2', '0'])

    // 落定标记(600ms 一次性墨迹落定)
    expect(getCard('验证行动卡片的克制视觉')).toHaveAttribute('data-drag-state', 'settling')

    // 硬断言: 成功不触发整板重拉,泳道间移动不出成功 toast
    await flushDnd(20)
    expect(mockFetchActionsBoard).toHaveBeenCalledTimes(1)
    expect(toastMock).not.toHaveBeenCalled()
    expect(toastMock.error).not.toHaveBeenCalled()

    // aria-live 播报(dnd-kit 内建 live region,Atlassian 三要素文案)
    expect(await screen.findByText('已将『验证行动卡片的克制视觉』从「待处理」移动到「执行中」。')).toBeInTheDocument()
  })

  it('拖入「已完成」走 markActionDone + 5s 撤销 toast,撤销回快照状态', async () => {
    await renderBoard()
    const card = getCard('验证行动卡片的克制视觉')
    await pickUp(card)
    await arrow(card, 'ArrowRight')
    await arrow(card, 'ArrowRight')

    // 空的已完成泳道在悬停时换「放到这里」文案
    await waitFor(() => expect(getLane('done')).toHaveAttribute('data-drop-target', 'true'))
    expect(within(getLane('done')).getByText('放到这里')).toBeInTheDocument()

    await dropWith(card, 'Space')

    await waitFor(() => expect(mockMarkActionDone).toHaveBeenCalledWith('act-1'))
    expect(mockUpdateAction).not.toHaveBeenCalled()
    await waitFor(() => expect(within(getLane('done')).getByText('验证行动卡片的克制视觉')).toBeInTheDocument())

    expect(toastMock).toHaveBeenCalledTimes(1)
    const [message, options] = toastMock.mock.calls[0]
    expect(message).toBe('已完成')
    expect(options.duration).toBe(5000)
    expect(options.action.label).toBe('撤销')

    // 撤销 = updateAction 回快照状态(含 store 回滚),依旧不整板重拉
    await act(async () => {
      options.action.onClick()
      await sleep(0)
    })
    await waitFor(() => expect(mockUpdateAction).toHaveBeenCalledWith('act-1', { status: 'pending' }))
    expect(useActionStore.getState().actions.find((a) => a.id === 'act-1')?.status).toBe('pending')
    await waitFor(() => expect(within(getLane('pending')).getByText('验证行动卡片的克制视觉')).toBeInTheDocument())
    expect(mockFetchActionsBoard).toHaveBeenCalledTimes(1)
  })

  it('API 失败: 回滚快照 + error toast(重试) + aria-live 失败播报', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    mockUpdateAction.mockRejectedValueOnce(new Error('boom'))
    await renderBoard()
    const card = getCard('验证行动卡片的克制视觉')
    await pickUp(card)
    await arrow(card, 'ArrowRight')
    await dropWith(card, 'Enter')

    await waitFor(() => expect(toastMock.error).toHaveBeenCalledWith('移动失败，已恢复原位置', expect.objectContaining({
      action: expect.objectContaining({ label: '重试' }),
    })))
    // 回滚: store 状态、泳道位置、计数全部还原
    expect(useActionStore.getState().actions.find((a) => a.id === 'act-1')?.status).toBe('pending')
    expect(within(getLane('pending')).getByText('验证行动卡片的克制视觉')).toBeInTheDocument()
    expect(screen.getAllByTestId('action-lane-count').map((el) => el.textContent)).toEqual(['2', '1', '0'])
    expect(screen.getByTestId('actions-dnd-live')).toHaveTextContent('移动失败，『验证行动卡片的克制视觉』已恢复到「待处理」泳道。')
    expect(mockFetchActionsBoard).toHaveBeenCalledTimes(1)

    // 重试 = 重发同一移动
    const [, errorOptions] = toastMock.error.mock.calls[0]
    await act(async () => {
      errorOptions.action.onClick()
      await sleep(0)
    })
    await waitFor(() => expect(mockUpdateAction).toHaveBeenLastCalledWith('act-1', { status: 'confirmed' }))
    expect(useActionStore.getState().actions.find((a) => a.id === 'act-1')?.status).toBe('confirmed')
    consoleError.mockRestore()
  })

  it('Esc 取消: 无 API 调用、卡片留在原泳道、播报取消', async () => {
    await renderBoard()
    const card = getCard('验证行动卡片的克制视觉')
    await pickUp(card)
    await arrow(card, 'ArrowRight')
    await dropWith(card, 'Escape')

    expect(mockUpdateAction).not.toHaveBeenCalled()
    expect(mockMarkActionDone).not.toHaveBeenCalled()
    expect(within(getLane('pending')).getByText('验证行动卡片的克制视觉')).toBeInTheDocument()
    expect(await screen.findByText('已取消移动，『验证行动卡片的克制视觉』仍在「待处理」泳道。')).toBeInTheDocument()
    expect(mockFetchActionsBoard).toHaveBeenCalledTimes(1)
  })

  it('同泳道落下 = 无操作(不发 API、不出 toast)', async () => {
    await renderBoard()
    const card = getCard('验证行动卡片的克制视觉')
    await pickUp(card)
    await dropWith(card, 'Enter')

    expect(mockUpdateAction).not.toHaveBeenCalled()
    expect(mockMarkActionDone).not.toHaveBeenCalled()
    expect(toastMock).not.toHaveBeenCalled()
    expect(within(getLane('pending')).getByText('验证行动卡片的克制视觉')).toBeInTheDocument()
  })

  it('↑/↓ 播报「本看板按时间自动排序」提示且不移动', async () => {
    await renderBoard()
    const card = getCard('验证行动卡片的克制视觉')
    await pickUp(card)
    await arrow(card, 'ArrowUp')

    expect(screen.getByTestId('actions-dnd-live')).toHaveTextContent('本看板按时间自动排序，无需上下移动')

    await dropWith(card, 'Enter')
    expect(mockUpdateAction).not.toHaveBeenCalled()
    expect(mockMarkActionDone).not.toHaveBeenCalled()
  })

  it('键盘替代流指引(screenReaderInstructions)以中文渲染并挂到卡片 aria-describedby', async () => {
    await renderBoard()
    // findByText: dnd-kit Accessibility 的 mounted gate 在骨架→看板二次挂载后晚一拍渲染
    const instructions = await screen.findByText(/按空格键拾起行动卡片/)
    expect(instructions).toHaveTextContent('更多操作菜单')
    const describedBy = screen.getAllByTestId('action-card')[0].getAttribute('aria-describedby')
    expect(describedBy).toBeTruthy()
    await waitFor(() => expect(document.getElementById(describedBy as string)).toBe(instructions))
  })

  it('卡片可聚焦并带 aria-roledescription;泳道 section 带中文 aria-label', async () => {
    await renderBoard()
    const card = getCard('验证行动卡片的克制视觉')
    expect(card).toHaveAttribute('tabindex', '0')
    expect(card).toHaveAttribute('aria-roledescription', '可拖拽的行动卡片')
    expect(getLane('pending')).toHaveAttribute('aria-label', '待处理泳道，共 2 条')
    expect(getLane('done')).toHaveAttribute('aria-label', '已完成泳道，共 0 条')
  })

  it('「···」菜单回归: 菜单操作仍走全量重拉(与拖拽路径的冻结互不影响)', async () => {
    await renderBoard()
    const card = getCard('验证行动卡片的克制视觉')
    fireEvent.click(within(card).getByLabelText('更多行动操作'))
    fireEvent.click(screen.getByRole('button', { name: '已完成' }))

    await waitFor(() => expect(mockMarkActionDone).toHaveBeenCalledWith('act-1'))
    await waitFor(() => expect(mockFetchActionsBoard).toHaveBeenCalledTimes(2))
    // 菜单路径不出撤销 toast(undo 仅拖入「已完成」)
    expect(toastMock).not.toHaveBeenCalled()
  })

  it('拖拽进行中其它 mutation 的 refetch 被推迟,drag 结束后 2s 冻结窗口过后才重拉', async () => {
    await renderBoard()
    const card = getCard('验证行动卡片的克制视觉')
    await pickUp(card)

    // 拖拽中在另一张卡上走菜单「已完成」
    const otherCard = getCard('判断投资线索优先级')
    fireEvent.click(within(otherCard).getByLabelText('更多行动操作'))
    fireEvent.click(screen.getByRole('button', { name: '已完成' }))
    await waitFor(() => expect(mockMarkActionDone).toHaveBeenCalledWith('act-2'))

    // 拖拽期间冻结: 不触发重拉
    await flushDnd(60)
    expect(mockFetchActionsBoard).toHaveBeenCalledTimes(1)

    await dropWith(card, 'Escape')
    // 落下后 2s 内仍冻结
    expect(mockFetchActionsBoard).toHaveBeenCalledTimes(1)
    // 冻结窗口过后补一次重拉
    await waitFor(() => expect(mockFetchActionsBoard).toHaveBeenCalledTimes(2), { timeout: 3500 })
  }, 8000)
})
