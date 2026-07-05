import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useClusterDetailStore } from '../clusterDetailStore'
import type { ClusterBundleResponse, ClusterDetail, ClusterEvent, ClusterSourcesResponse } from '../../lib/types'

vi.mock('../../lib/api', () => ({
  fetchClusterBundle: vi.fn(),
  fetchClusterDetail: vi.fn(),
  fetchClusterSources: vi.fn(),
  clickCluster: vi.fn(),
  setClusterStar: vi.fn(),
  fetchClusterActions: vi.fn(),
  generateClusterAction: vi.fn(() => ({ abort: vi.fn() })),
}))

import {
  fetchClusterBundle,
  fetchClusterDetail,
  fetchClusterSources,
  clickCluster,
  setClusterStar,
  fetchClusterActions,
  generateClusterAction,
} from '../../lib/api'

function makeDetail(overrides: Partial<ClusterDetail> = {}): ClusterDetail {
  return {
    id: 42,
    ai_title: 'Test Cluster',
    ai_summary: 'summary',
    ai_key_points: ['kp1', 'kp2'],
    doc_count: 6,
    unique_source_count: 6,
    platforms: ['twitter'],
    first_doc_at: '2026-04-23T09:00:00Z',
    last_doc_at: '2026-04-23T09:30:00Z',
    cover_url: null,
    live_version: 3,
    user_last_seen_version: 1,
    is_visible_in_feed: true,
    ...overrides,
  }
}

function makeEvent(overrides: Partial<ClusterEvent> = {}): ClusterEvent {
  return {
    id: 42,
    ai_title: 'Preview Cluster',
    ai_summary: 'preview summary',
    doc_count: 3,
    unique_source_count: 2,
    platforms: ['rss'],
    first_doc_at: '2026-04-23T09:00:00Z',
    last_doc_at: '2026-04-23T09:30:00Z',
    cover_url: '/images/events/preview.jpg',
    has_update: false,
    live_version: 7,
    last_seen_version: null,
    ...overrides,
  }
}

const emptySources: ClusterSourcesResponse = { sources: [], next_cursor: null }
const emptyBundle = (cluster: ClusterDetail = makeDetail()): ClusterBundleResponse => ({
  cluster,
  sources: [],
  sources_next_cursor: null,
})

beforeEach(() => {
  useClusterDetailStore.getState().closeModal()
  vi.mocked(fetchClusterBundle).mockReset()
  vi.mocked(fetchClusterDetail).mockReset()
  vi.mocked(fetchClusterSources).mockReset()
  vi.mocked(clickCluster).mockReset()
  vi.mocked(setClusterStar).mockReset()
  vi.mocked(fetchClusterActions).mockReset()
  vi.mocked(generateClusterAction).mockReset()
  vi.mocked(clickCluster).mockResolvedValue({ ok: true, last_seen_version: 0 })
  vi.mocked(setClusterStar).mockResolvedValue({ ok: true, starred_at: '2026-05-25T09:00:00Z' })
  vi.mocked(fetchClusterBundle).mockResolvedValue(emptyBundle())
  vi.mocked(generateClusterAction).mockReturnValue({ abort: vi.fn() } as unknown as AbortController)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('clusterDetailStore.openModal', () => {
  it('成功时设置 modalState=open + cluster + sources', async () => {
    vi.mocked(fetchClusterBundle).mockResolvedValue(emptyBundle(makeDetail()))
    await useClusterDetailStore.getState().openModal(42)
    const s = useClusterDetailStore.getState()
    expect(s.modalState).toBe('open')
    expect(s.cluster?.id).toBe(42)
    expect(s.modalClusterId).toBe(42)
  })

  it('bundle 未返回前不立刻展示 loading,超过延迟才显示', async () => {
    vi.useFakeTimers()
    let resolveBundle!: (value: ClusterBundleResponse) => void
    vi.mocked(fetchClusterBundle).mockReturnValue(
      new Promise((resolve) => {
        resolveBundle = resolve
      }),
    )

    const opening = useClusterDetailStore.getState().openModal(777)

    expect(useClusterDetailStore.getState().modalState).toBe('closed')
    await vi.advanceTimersByTimeAsync(449)
    expect(useClusterDetailStore.getState().modalState).toBe('closed')
    await vi.advanceTimersByTimeAsync(1)
    expect(useClusterDetailStore.getState().modalState).toBe('loading')

    resolveBundle(emptyBundle(makeDetail({ id: 777 })))
    await opening

    const s = useClusterDetailStore.getState()
    expect(s.modalState).toBe('open')
    expect(s.cluster?.id).toBe(777)
  })

  it('传入事件预览时立即打开弹窗,后台 bundle 返回后再补齐 sources', async () => {
    vi.useFakeTimers()
    let resolveBundle!: (value: ClusterBundleResponse) => void
    vi.mocked(fetchClusterBundle).mockReturnValue(
      new Promise((resolve) => {
        resolveBundle = resolve
      }),
    )

    const opening = useClusterDetailStore.getState().openModal(888, makeEvent({ id: 888 }))

    let s = useClusterDetailStore.getState()
    expect(s.modalState).toBe('open')
    expect(s.cluster?.ai_title).toBe('Preview Cluster')
    expect(s.cluster?.cover_url).toBe('/images/events/preview.jpg')
    expect(s.sources).toEqual([])

    await vi.advanceTimersByTimeAsync(1000)
    expect(useClusterDetailStore.getState().modalState).toBe('open')

    resolveBundle({
      cluster: makeDetail({ id: 888, ai_title: 'Fresh Cluster' }),
      sources: [{ item_id: 'src-1', title: 'source', author: null, platform: 'rss', published_at: null, url: null, is_primary_source: 1, authority_badge: null, snippet: '' }],
      sources_next_cursor: null,
    })
    await opening

    s = useClusterDetailStore.getState()
    expect(s.cluster?.ai_title).toBe('Fresh Cluster')
    expect(s.sources).toHaveLength(1)
  })

  it('redirect_to 时自动跳转新 cluster (不影响 hash)', async () => {
    vi.mocked(fetchClusterBundle)
      .mockResolvedValueOnce(emptyBundle({ ...makeDetail({ id: 99 }), redirect_to: 100 }))
      .mockResolvedValueOnce(emptyBundle(makeDetail({ id: 100 })))
    await useClusterDetailStore.getState().openModal(99)
    expect(useClusterDetailStore.getState().cluster?.id).toBe(100)
  })

  it('error 时设置 modalState=error', async () => {
    vi.mocked(fetchClusterBundle).mockRejectedValue(new Error('Not found'))
    await useClusterDetailStore.getState().openModal(404)
    const s = useClusterDetailStore.getState()
    expect(s.modalState).toBe('error')
    expect(s.error).toBe('Not found')
  })

  it('打开后异步触发 click 打点', async () => {
    vi.mocked(fetchClusterBundle).mockResolvedValue(emptyBundle(makeDetail()))
    await useClusterDetailStore.getState().openModal(42)
    // click fire-and-forget,等微任务
    await new Promise((r) => setTimeout(r, 0))
    expect(clickCluster).toHaveBeenCalledWith(42)
  })
})

describe('clusterDetailStore.toggleClusterStar', () => {
  it('乐观更新:接口返回前图标先翻转,成功后落到服务端值(D3)', async () => {
    vi.mocked(fetchClusterBundle).mockResolvedValue(emptyBundle(makeDetail()))
    await useClusterDetailStore.getState().openModal(42)

    let resolveStar: (v: { ok: boolean; starred_at: string | null }) => void
    vi.mocked(setClusterStar).mockReturnValue(
      new Promise((resolve) => { resolveStar = resolve }) as ReturnType<typeof setClusterStar>,
    )

    const pending = useClusterDetailStore.getState().toggleClusterStar(42)
    // 接口未返回,乐观值已生效
    expect(useClusterDetailStore.getState().cluster?.viewer_status?.starred_at).toBeTruthy()

    resolveStar!({ ok: true, starred_at: '2026-05-25T09:00:00Z' })
    await pending
    expect(useClusterDetailStore.getState().cluster?.viewer_status?.starred_at).toBe('2026-05-25T09:00:00Z')
  })

  it('接口失败时回滚到原状态并抛错', async () => {
    vi.mocked(fetchClusterBundle).mockResolvedValue(emptyBundle(makeDetail()))
    await useClusterDetailStore.getState().openModal(42)
    expect(useClusterDetailStore.getState().cluster?.viewer_status?.starred_at ?? null).toBeNull()

    vi.mocked(setClusterStar).mockRejectedValue(new Error('network'))
    await expect(useClusterDetailStore.getState().toggleClusterStar(42)).rejects.toThrow('network')
    expect(useClusterDetailStore.getState().cluster?.viewer_status?.starred_at ?? null).toBeNull()
  })
})

describe('clusterDetailStore.closeModal', () => {
  it('重置所有状态', async () => {
    useClusterDetailStore.setState({
      modalState: 'open',
      modalClusterId: 5,
      cluster: makeDetail({ id: 5 }),
    })
    useClusterDetailStore.getState().closeModal()
    const s = useClusterDetailStore.getState()
    expect(s.modalState).toBe('closed')
    expect(s.modalClusterId).toBe(null)
    expect(s.cluster).toBe(null)
  })
})

describe('clusterDetailStore.loadFullPage', () => {
  it('redirect_to → modalState=redirecting + redirectTo set', async () => {
    vi.mocked(fetchClusterDetail).mockResolvedValue({ ...makeDetail({ id: 99 }), redirect_to: 100 })
    vi.mocked(fetchClusterSources).mockResolvedValue(emptySources)
    await useClusterDetailStore.getState().loadFullPage(99)
    const s = useClusterDetailStore.getState()
    expect(s.modalState).toBe('redirecting')
    expect(s.redirectTo).toBe(100)
  })

  it('正常 load → modalState=open + 触发 loadActions', async () => {
    vi.mocked(fetchClusterDetail).mockResolvedValue(makeDetail())
    vi.mocked(fetchClusterSources).mockResolvedValue(emptySources)
    vi.mocked(fetchClusterActions).mockResolvedValue({ actions: [] })
    await useClusterDetailStore.getState().loadFullPage(42)
    expect(useClusterDetailStore.getState().modalState).toBe('open')
    // loadActions fire-and-forget,等微任务
    await new Promise((r) => setTimeout(r, 0))
    expect(fetchClusterActions).toHaveBeenCalledWith(42)
  })
})

describe('clusterDetailStore.startGenerate', () => {
  it('处理 cluster SSE 的 stage / thinking-ai / result / done 事件', async () => {
    vi.mocked(fetchClusterActions).mockResolvedValue({ actions: [] })
    vi.mocked(generateClusterAction).mockImplementation(
      (_clusterId, _options, onEvent, onDone) => {
        queueMicrotask(() => {
          onEvent!({ type: 'thinking', stage: 0, text: '事件已读入' })
          onEvent!({ type: 'stage', index: 0, status: 'done' })
          onEvent!({ type: 'stage', index: 1, status: 'active' })
          onEvent!({ type: 'thinking-ai', stage: 2, text: 'AI 正在综合多源信息' })
          onEvent!({
            type: 'result',
            action: {
              id: 'act-1',
              title: '验证多源事件机会',
              action_type: 'implement',
              prompt: 'prompt',
              priority: 'high',
              status: 'pending',
              cluster_version: 3,
              is_stale: 0,
              reason: 'reason',
            },
          })
          onEvent!({ type: 'done', action_id: 'act-1', title: '验证多源事件机会' })
          onDone!()
        })
        return { abort: vi.fn() } as unknown as AbortController
      },
    )

    useClusterDetailStore.getState().startGenerate(42, '关注工程机会', 'implement')
    await new Promise((r) => setTimeout(r, 0))
    await new Promise((r) => setTimeout(r, 0))

    expect(generateClusterAction).toHaveBeenCalledWith(
      42,
      { userHint: '关注工程机会', actionType: 'implement' },
      expect.any(Function),
      expect.any(Function),
      expect.any(Function),
    )
    const s = useClusterDetailStore.getState()
    expect(s.generating).toBe(false)
    expect(s.generateStages).toEqual([2, 2, 2, 2])
    expect(s.generateThinkingLines).toEqual([
      { text: '事件已读入', ai: false, stage: 0 },
      { text: 'AI 正在综合多源信息', ai: true, stage: 2 },
    ])
    expect(s.generateThinking).toEqual(['事件已读入'])
    expect(s.generateAction?.title).toBe('验证多源事件机会')
    expect(s.generateDone).toEqual({ actionId: 'act-1', title: '验证多源事件机会' })
    expect(fetchClusterActions).toHaveBeenCalledWith(42)
  })

  it('error 事件会结束生成态并保留错误信息', async () => {
    vi.mocked(generateClusterAction).mockImplementation(
      (_clusterId, _options, onEvent) => {
        queueMicrotask(() => {
          onEvent!({ type: 'error', error: 'provider cooldown' })
        })
        return { abort: vi.fn() } as unknown as AbortController
      },
    )

    useClusterDetailStore.getState().startGenerate(42)
    await new Promise((r) => setTimeout(r, 0))

    const s = useClusterDetailStore.getState()
    expect(s.generating).toBe(false)
    expect(s.generateController).toBe(null)
    expect(s.generateError).toBe('provider cooldown')
  })
})
