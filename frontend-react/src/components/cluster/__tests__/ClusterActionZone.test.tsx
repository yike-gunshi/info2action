import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ClusterActionZone } from '../ClusterActionZone'
import type { ClusterAction } from '../../../lib/types'

const mocks = vi.hoisted(() => ({
  openAction: vi.fn(),
  closeModal: vi.fn(),
  startGenerate: vi.fn(),
  cancelGenerate: vi.fn(),
  loadActions: vi.fn(),
  resetGenerate: vi.fn(),
  state: {} as Record<string, unknown>,
}))

vi.mock('../../../store/detailStore', () => ({
  useDetailStore: {
    getState: () => ({
      openAction: mocks.openAction,
      closeModal: mocks.closeModal,
    }),
  },
}))

vi.mock('../../../store/clusterDetailStore', () => ({
  useClusterDetailStore: (selector: (s: unknown) => unknown) => selector(mocks.state),
}))

vi.mock('../../shared/AuthGate', () => ({
  requireAuth: () => true,
}))

function makeAction(overrides: Partial<ClusterAction> = {}): ClusterAction {
  return {
    id: 'act-1',
    title: '梳理事件影响面',
    action_type: 'investigate',
    prompt: '1. 盘点事件背景\n2. 对比多源差异\n3. 输出是否需要跟进的判断标准',
    priority: 'medium',
    status: 'pending',
    cluster_version: 3,
    is_stale: 0,
    reason: 'fallback: LLM 输出未能解析为 action JSON',
    ...overrides,
  }
}

function setStoreState(overrides: Partial<Record<string, unknown>> = {}) {
  mocks.state = {
    startGenerate: mocks.startGenerate,
    cancelGenerate: mocks.cancelGenerate,
    generating: false,
    generateStages: [0, 0, 0, 0],
    generateThinkingLines: [],
    generateAction: null,
    generateError: null,
    actions: [],
    loadActions: mocks.loadActions,
    resetGenerate: mocks.resetGenerate,
    ...overrides,
  }
}

describe('ClusterActionZone', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.location.hash = ''
    Object.defineProperty(window, 'scrollTo', {
      configurable: true,
      value: vi.fn(),
    })
    setStoreState()
  })

  afterEach(cleanup)

  it('生成结果卡展示行动点(steps)和 fallback 提示,并可打开行动详情', async () => {
    const user = userEvent.setup()
    setStoreState({ generateAction: makeAction({ steps: ['盘点事件背景', '对比各来源差异', '输出决策建议'] }) })

    render(<ClusterActionZone clusterId={42} />)

    await screen.findByTestId('cluster-action-result')
    // v21.0: 结果卡展示 steps 行动点,而非原始 prompt
    expect(screen.getByTestId('cluster-action-steps')).toHaveTextContent('盘点事件背景')
    expect(screen.queryByTestId('cluster-action-prompt')).toBeNull()
    expect(screen.getByText(/保守兜底/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '查看行动详情' }))
    expect(mocks.openAction).toHaveBeenCalledWith('act-1')
  })

  it('已生成行动点列表可点击跳转到行动页目标卡片', async () => {
    const user = userEvent.setup()
    setStoreState({
      actions: [
        makeAction({
          id: 'act-existing',
          title: '评估产品机会',
          reason: '多源报道显示值得跟进',
        }),
      ],
    })

    render(<ClusterActionZone clusterId={42} />)

    await user.click(screen.getByRole('button', { name: /打开行动点: 评估产品机会/ }))

    expect(mocks.closeModal).toHaveBeenCalled()
    expect(window.location.hash).toBe('#v=actions&a=act-existing')
  })
})
