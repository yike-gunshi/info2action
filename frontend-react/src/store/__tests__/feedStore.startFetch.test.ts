import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'

vi.mock('../../lib/api', () => ({
  triggerFetchAll: vi.fn(),
  fetchFetchStatus: vi.fn(),
  fetchFeedSections: vi.fn(async () => ({ sections: {}, cat_counts: {} })),
  fetchFeedPlatforms: vi.fn(async () => ({ sections: {}, platform_counts: {}, source_counts: {} })),
  fetchFeed: vi.fn(),
}))

vi.mock('sonner', () => ({
  toast: {
    info: vi.fn(),
    success: vi.fn(),
    error: vi.fn(),
  },
}))

import { formatFetchProgressLabel, useFeedStore } from '../feedStore'
import { triggerFetchAll, fetchFetchStatus, fetchFeedSections, fetchFeedPlatforms } from '../../lib/api'
import { toast } from 'sonner'

const mockTrigger = triggerFetchAll as unknown as ReturnType<typeof vi.fn>
const mockStatus = fetchFetchStatus as unknown as ReturnType<typeof vi.fn>
const mockFetchSections = fetchFeedSections as unknown as ReturnType<typeof vi.fn>
const mockFetchPlatforms = fetchFeedPlatforms as unknown as ReturnType<typeof vi.fn>
const mockToast = toast as unknown as {
  info: ReturnType<typeof vi.fn>
  success: ReturnType<typeof vi.fn>
  error: ReturnType<typeof vi.fn>
}

async function flushMicrotasks() {
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
}

describe('BF-0420-10: feedStore.startFetch reacts to backend result', () => {
  beforeEach(() => {
    useFeedStore.setState({ isFetching: false, fetchProgress: null })
    mockTrigger.mockReset()
    mockStatus.mockReset()
    mockFetchSections.mockReset()
    mockFetchSections.mockResolvedValue({ sections: {}, cat_counts: {} })
    mockFetchPlatforms.mockReset()
    mockFetchPlatforms.mockResolvedValue({ sections: {}, platform_counts: {}, source_counts: {} })
    mockFetchPlatforms.mockClear()
    mockToast.info.mockReset()
    mockToast.success.mockReset()
    mockToast.error.mockReset()
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('ok=true: toast.info 开始抓取 + 完成后 toast.success 带新增条数', async () => {
    mockTrigger.mockResolvedValue({ ok: true, msg: 'Global fetch: all sources' })
    mockStatus.mockResolvedValue({
      running: false,
      finished_at: '2026-04-20T12:00:00',
      progress: { stages: [{ name: '入库处理', status: 'done' }], current_stage: 2, total_new: 7 },
    })

    const p = useFeedStore.getState().startFetch()
    await flushMicrotasks()

    expect(mockToast.info).toHaveBeenCalledWith('开始抓取…')
    expect(useFeedStore.getState().isFetching).toBe(true)

    await vi.advanceTimersByTimeAsync(3000)
    await p
    await flushMicrotasks()

    expect(mockToast.success).toHaveBeenCalledWith('抓取完成 · 新增 7 条')
    expect(useFeedStore.getState().isFetching).toBe(false)
    expect(mockFetchPlatforms).toHaveBeenCalled()
  })

  it('运行中 progress 写入 store,并格式化为 阶段 · 平台 · 百分比', async () => {
    mockTrigger.mockResolvedValue({ ok: true, msg: 'Global fetch: all sources' })
    mockStatus
      .mockResolvedValueOnce({
        running: true,
        finished_at: null,
        progress: {
          stages: [
            { id: 'ai_enrich', name: 'AI 统一理解', status: 'running', platform: 'waytoagi', percent: 60 },
          ],
          current_stage: 0,
          total_new: 0,
          platform: 'waytoagi',
          percent: 60,
          result_status: 'running',
        },
      })
      .mockResolvedValueOnce({
        running: false,
        finished_at: '2026-05-08T12:00:00',
        progress: {
          stages: [
            { id: 'ai_enrich', name: 'AI 统一理解', status: 'done', platform: 'waytoagi', percent: 80 },
          ],
          current_stage: 0,
          total_new: 3,
          result_status: 'success',
        },
      })

    const p = useFeedStore.getState().startFetch()
    await flushMicrotasks()

    await vi.advanceTimersByTimeAsync(3000)
    await flushMicrotasks()

    const progress = useFeedStore.getState().fetchProgress
    expect(formatFetchProgressLabel(progress)).toBe('AI 总结中 · waytoagi · 60%')
    expect(useFeedStore.getState().isFetching).toBe(true)

    await vi.advanceTimersByTimeAsync(3000)
    await p
    await flushMicrotasks()

    expect(useFeedStore.getState().isFetching).toBe(false)
  })

  it('全局抓取完成后即使频道刷新失败,推荐区仍会刷新', async () => {
    mockTrigger.mockResolvedValue({ ok: true, msg: 'Global fetch: all sources' })
    mockStatus.mockResolvedValue({
      running: false,
      finished_at: '2026-04-20T12:00:00',
      progress: { stages: [{ name: '入库处理', status: 'done' }], current_stage: 2, total_new: 1 },
    })
    mockFetchSections.mockResolvedValue({
      sections: {
        products: [{
          id: 'fresh-item',
          title: 'Fresh item',
          platform: 'twitter',
          fetched_at: '2026-05-07T00:00:00Z',
          ai_category: 'products',
        }],
      },
      cat_counts: { products: 1 },
    })
    mockFetchPlatforms.mockRejectedValue(new Error('platform refresh failed'))

    const p = useFeedStore.getState().startFetch()
    await flushMicrotasks()
    await vi.advanceTimersByTimeAsync(3000)
    await p
    await flushMicrotasks()

    expect(useFeedStore.getState().sectionItems.get('products')?.[0]?.id).toBe('fresh-item')
    expect(useFeedStore.getState().isFetching).toBe(false)
  })

  it('ok=true 但 total_new=0: toast.info 暂无新内容', async () => {
    mockTrigger.mockResolvedValue({ ok: true, msg: '' })
    mockStatus.mockResolvedValue({
      running: false,
      finished_at: '2026-04-20T12:00:00',
      progress: { stages: [{ name: '入库处理', status: 'done' }], current_stage: 2, total_new: 0 },
    })

    const p = useFeedStore.getState().startFetch()
    await flushMicrotasks()
    await vi.advanceTimersByTimeAsync(3000)
    await p
    await flushMicrotasks()

    expect(mockToast.info).toHaveBeenCalledWith('抓取完成,暂无新内容')
    expect(mockToast.success).not.toHaveBeenCalled()
  })

  it('任一 stage=failed: toast.error 带 stage 名', async () => {
    mockTrigger.mockResolvedValue({ ok: true, msg: '' })
    mockStatus.mockResolvedValue({
      running: false,
      finished_at: '2026-04-20T12:00:00',
      progress: {
        stages: [
          { name: '准备抓取', status: 'done' },
          { name: '执行 fetch_all.sh', status: 'failed' },
        ],
        current_stage: 1,
        total_new: 0,
      },
    })

    const p = useFeedStore.getState().startFetch()
    await flushMicrotasks()
    await vi.advanceTimersByTimeAsync(3000)
    await p
    await flushMicrotasks()

    expect(mockToast.error).toHaveBeenCalledWith('抓取失败:执行 fetch_all.sh')
  })

  it('result_status=partial 时不按失败 toast,而是提示部分完成', async () => {
    mockTrigger.mockResolvedValue({ ok: true, msg: '' })
    mockStatus.mockResolvedValue({
      running: false,
      finished_at: '2026-05-08T12:00:00',
      progress: {
        stages: [
          { name: 'AI 统一理解', status: 'failed', message: 'AI 统一理解失败' },
        ],
        current_stage: 0,
        total_new: 9,
        result_status: 'partial',
        message: '本轮部分完成，AI 总结失败',
      },
    })

    const p = useFeedStore.getState().startFetch()
    await flushMicrotasks()
    await vi.advanceTimersByTimeAsync(3000)
    await p
    await flushMicrotasks()

    expect(mockToast.info).toHaveBeenCalledWith('抓取部分完成 · 本轮部分完成，AI 总结失败')
    expect(mockToast.error).not.toHaveBeenCalled()
  })

  it('ok=false + already running: toast.info 附着 + 继续 poll', async () => {
    mockTrigger.mockResolvedValue({ ok: false, msg: 'Fetch already running' })
    mockStatus.mockResolvedValue({
      running: false,
      finished_at: '2026-04-20T12:00:00',
      progress: { stages: [], current_stage: 0, total_new: 2 },
    })

    const p = useFeedStore.getState().startFetch()
    await flushMicrotasks()

    expect(mockToast.info).toHaveBeenCalledWith('抓取已在进行中,继续等待')
    expect(useFeedStore.getState().isFetching).toBe(true)

    await vi.advanceTimersByTimeAsync(3000)
    await p
    await flushMicrotasks()

    expect(mockToast.success).toHaveBeenCalledWith('抓取完成 · 新增 2 条')
  })

  it('ok=false 其他错误: toast.error + isFetching 归 false', async () => {
    mockTrigger.mockResolvedValue({ ok: false, msg: 'Internal server glitch' })

    await useFeedStore.getState().startFetch()

    expect(mockToast.error).toHaveBeenCalledWith('Internal server glitch')
    expect(useFeedStore.getState().isFetching).toBe(false)
    expect(mockStatus).not.toHaveBeenCalled()
  })

  it('触发请求本身 throw: toast.error 带真实错误信息', async () => {
    mockTrigger.mockRejectedValue(new Error('Network unreachable'))

    await useFeedStore.getState().startFetch()

    expect(mockToast.error).toHaveBeenCalledWith('Network unreachable')
    expect(useFeedStore.getState().isFetching).toBe(false)
  })

  it('rev2: isFetching=true 时再次 startFetch 不调后端但弹 info toast', async () => {
    useFeedStore.setState({ isFetching: true })

    await useFeedStore.getState().startFetch()

    expect(mockTrigger).not.toHaveBeenCalled()
    // rev2: 前端 guard 时也要 toast,否则用户"第二次点击没反应"
    expect(mockToast.info).toHaveBeenCalledWith('抓取已在进行中,继续等待')
  })
})
