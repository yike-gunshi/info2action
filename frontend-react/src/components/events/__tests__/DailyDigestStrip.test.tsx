import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DailyDigestStrip } from '../DailyDigestStrip'
import { useDailyDigestStore } from '../../../store/dailyDigestStore'
import { useEventsStore } from '../../../store/eventsStore'
import { useClusterDetailStore } from '../../../store/clusterDetailStore'
import * as api from '../../../lib/api'
import { FALLBACK_EVENT_CATEGORY_OPTIONS } from '../../../lib/eventCategories'
import type { ClusterEvent, DailyDigest, DailyDigestsResponse } from '../../../lib/types'

function makeEvent(id: number): ClusterEvent {
  return {
    id,
    ai_title: `事件 ${id}`,
    doc_count: 3,
    unique_source_count: 3,
    first_doc_at: '2026-08-02T09:00:00Z',
    last_doc_at: '2026-08-02T09:30:00Z',
    platforms: ['x'],
    cover_url: null,
    has_update: false,
    live_version: 1,
  }
}

function makeDigest(entries: DailyDigest['entries'], date = '2026-08-02'): DailyDigest {
  return {
    date,
    status: 'rolling',
    entries,
    updated_at: '2026-08-02T03:00:00Z',
  }
}

function categorizedDigest(): DailyDigest {
  return makeDigest([
    { rank: 6, cluster_id: 106, title: '产品乙', source_count: 3, links: [], category: 'products' },
    { rank: 2, cluster_id: 102, title: '产品甲', source_count: 3, links: [], category: 'products' },
    { rank: 1, cluster_id: 101, title: '模型甲', source_count: 3, links: [], category: 'models' },
    { rank: 3, cluster_id: 103, title: '技术甲', source_count: 3, links: [], category: 'tech' },
    { rank: 4, cluster_id: 104, title: '工具甲', source_count: 3, links: [], category: 'efficiency_tools' },
    { rank: 5, cluster_id: 105, title: '行业甲', source_count: 3, links: [], category: 'industry' },
    { rank: 7, cluster_id: 107, title: '无法归类快照', source_count: 3, links: [], category: null },
    { rank: 8, cluster_id: 108, title: '旧接口快照', source_count: 3, links: [] },
  ])
}

function selectCategories(...categories: string[]) {
  act(() => { useEventsStore.setState({ filters: { categories } }) })
}

function visibleRows() {
  return screen.queryAllByTestId('daily-digest-entry').map((row) => row.textContent)
}

describe('DailyDigestStrip', () => {
  const originalOpenModal = useClusterDetailStore.getState().openModal
  const originalCloseModal = useClusterDetailStore.getState().closeModal

  beforeEach(() => {
    useDailyDigestStore.getState().reset()
    useEventsStore.getState().reset()
    useClusterDetailStore.setState({
      modalState: 'closed',
      modalClusterId: null,
      cluster: null,
      error: null,
      openModal: vi.fn().mockResolvedValue(undefined),
      closeModal: vi.fn(),
    })
  })

  afterEach(() => {
    cleanup()
    useDailyDigestStore.getState().reset()
    useEventsStore.getState().reset()
    useClusterDetailStore.setState({ openModal: originalOpenModal, closeModal: originalCloseModal })
    vi.restoreAllMocks()
  })

  it('直接按 rank 升序展示要点，不显示每日要点标签、空标题行或信源数', () => {
    useDailyDigestStore.setState({
      digestsByDate: {
        '2026-08-02': makeDigest([
          { rank: 2, cluster_id: 102, title: '第二条', source_count: 8, links: [] },
          { rank: 1, cluster_id: 101, title: '第一条', source_count: 3, links: [] },
        ]),
      },
    })

    render(<DailyDigestStrip date="2026-08-02" />)

    const strip = screen.getByTestId('daily-digest-strip')
    expect(strip).not.toHaveTextContent('每日要点')
    expect(strip).not.toHaveTextContent('✦')
    expect(strip.firstElementChild?.tagName).toBe('OL')
    expect(strip).not.toHaveTextContent('2 条')
    expect(screen.getAllByTestId('daily-digest-entry').map((row) => row.textContent)).toEqual([
      expect.stringContaining('01第一条'),
      expect.stringContaining('02第二条'),
    ])
    expect(strip).not.toHaveTextContent('3 个信源')
    expect(strip).not.toHaveTextContent('8 个信源')
  })

  it('暖纸外框无额外左缩进，保留内部留白与序号列', () => {
    useDailyDigestStore.setState({
      digestsByDate: {
        '2026-08-02': makeDigest([
          { rank: 1, cluster_id: 101, title: '第一条', source_count: 3, links: [] },
          { rank: 2, cluster_id: 102, title: '第二条', source_count: 8, links: [] },
        ]),
      },
    })

    render(<DailyDigestStrip date="2026-08-02" />)

    const strip = screen.getByTestId('daily-digest-strip')
    expect(strip.className).toContain('bg-[color-mix(in_srgb,var(--brand)_7%,var(--background))]')
    expect(strip.className).toContain('rounded-[4px]')
    expect(strip.className).toContain('px-5')
    expect(strip.className).toContain('py-4')
    expect(strip.className).not.toContain('sm:ml-4')
    expect(strip.className).not.toContain('border-y')
    expect(strip.className).not.toContain('border-foreground')
    expect(strip.className).not.toContain('shadow')

    const rows = screen.getAllByTestId('daily-digest-entry')
    rows.forEach((row) => {
      expect(row.parentElement?.className).not.toContain('border')
      expect(row.className).toContain('sm:grid-cols-[32px_minmax(0,1fr)]')
      expect(row.className).toContain('sm:gap-3')
    })
    const title = rows[0].querySelector('.font-event-title') as HTMLElement
    expect(title.className).toContain('text-[14px]')
    expect(title.className).toContain('sm:text-[17px]')
    expect(title.className).toContain('line-clamp-2')
    expect(title.className).toContain('sm:line-clamp-1')
  })

  it('无该日快照或 entries 为空时零渲染', () => {
    const { container, rerender } = render(<DailyDigestStrip date="2026-08-02" />)
    expect(container).toBeEmptyDOMElement()

    act(() => {
      useDailyDigestStore.setState({ digestsByDate: { '2026-08-02': makeDigest([]) } })
    })
    rerender(<DailyDigestStrip date="2026-08-02" />)
    expect(container).toBeEmptyDOMElement()
  })

  it('全部 8 条切产品后仅显示 2 条并连续编号，切回全部恢复原顺序与编号', () => {
    const digest = categorizedDigest()
    useDailyDigestStore.setState({ digestsByDate: { [digest.date]: digest } })
    render(<DailyDigestStrip date={digest.date} />)
    const allRows = [
      '01模型甲', '02产品甲', '03技术甲', '04工具甲',
      '05行业甲', '06产品乙', '07无法归类快照', '08旧接口快照',
    ]
    expect(visibleRows()).toEqual(allRows)

    selectCategories('products')
    expect(visibleRows()).toEqual(['01产品甲', '02产品乙'])

    selectCategories()
    expect(visibleRows()).toEqual(allRows)
    expect(useDailyDigestStore.getState().digestsByDate[digest.date]).toBe(digest)
    expect(digest.entries.map((entry) => entry.rank)).toEqual([6, 2, 1, 3, 4, 5, 7, 8])
  })

  it.each(FALLBACK_EVENT_CATEGORY_OPTIONS)('$label 分类只展示接口主分类匹配的要点，排除其他分类和缺分类条目', ({ id, label }) => {
    const digest = makeDigest([
      ...FALLBACK_EVENT_CATEGORY_OPTIONS.map((category, index) => ({
        rank: index + 1, cluster_id: index + 1, title: category.label,
        source_count: 3, links: [], category: category.id,
      })),
      { rank: 14, cluster_id: 14, title: '无法归类', source_count: 3, links: [], category: null },
      { rank: 15, cluster_id: 15, title: '旧响应', source_count: 3, links: [] },
    ])
    useDailyDigestStore.setState({ digestsByDate: { [digest.date]: digest } })
    selectCategories(id)
    render(<DailyDigestStrip date={digest.date} />)

    expect(visibleRows()).toEqual([`01${label}`])
  })

  it('无匹配分类隐藏整个要点块，多分类沿用 OR 语义和原排序', () => {
    const digest = categorizedDigest()
    useDailyDigestStore.setState({ digestsByDate: { [digest.date]: digest } })
    selectCategories('coding')
    const { container } = render(<DailyDigestStrip date={digest.date} />)
    expect(container).toBeEmptyDOMElement()

    selectCategories('products', 'models')
    expect(visibleRows()).toEqual(['01模型甲', '02产品甲', '03产品乙'])
  })

  it('匹配卡片尚未加载也展示要点，分页加载与主分类不同的卡片不改变要点筛选', () => {
    const digest = categorizedDigest()
    useDailyDigestStore.setState({ digestsByDate: { [digest.date]: digest } })
    selectCategories('products')
    render(<DailyDigestStrip date={digest.date} />)
    expect(visibleRows()).toEqual(['01产品甲', '02产品乙'])

    act(() => {
      useEventsStore.setState({
        events: [{ ...makeEvent(101), category: 'products' }, { ...makeEvent(102), category: 'models' }],
        cursor: 2,
      })
    })
    expect(visibleRows()).toEqual(['01产品甲', '02产品乙'])

    act(() => { useEventsStore.setState({ events: [makeEvent(106)], cursor: 3 }) })
    expect(visibleRows()).toEqual(['01产品甲', '02产品乙'])
  })

  it('切换今天与历史日期时按各日主分类过滤，不混用日期快照', () => {
    const today = categorizedDigest()
    const historical = makeDigest([
      { rank: 4, cluster_id: 201, title: '历史产品', source_count: 2, links: [], category: 'products' },
      { rank: 1, cluster_id: 202, title: '历史模型', source_count: 2, links: [], category: 'models' },
    ], '2026-06-01')
    useDailyDigestStore.setState({ digestsByDate: { [today.date]: today, [historical.date]: historical } })
    selectCategories('products')
    const { rerender } = render(<DailyDigestStrip date={today.date} />)

    rerender(<DailyDigestStrip date={historical.date} />)
    expect(visibleRows()).toEqual(['01历史产品'])
    selectCategories('models')
    expect(visibleRows()).toEqual(['01历史模型'])
    rerender(<DailyDigestStrip date={today.date} />)
    expect(visibleRows()).toEqual(['01模型甲'])
  })

  it('日期缓存复用且刷新失败保留已筛选内容，重试后使用更新分类', async () => {
    const digest = categorizedDigest()
    const reclassified = {
      ...digest,
      entries: digest.entries.map((entry) => entry.cluster_id === 102 ? { ...entry, category: 'models' } : entry),
    }
    const fetchDigests = vi.spyOn(api, 'fetchDailyDigests')
      .mockResolvedValueOnce({ digests: [digest] })
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({ digests: [reclassified] })
    selectCategories('products')
    render(<DailyDigestStrip date={digest.date} />)
    await act(async () => { await useDailyDigestStore.getState().loadRange(digest.date, digest.date) })
    expect(visibleRows()).toEqual(['01产品甲', '02产品乙'])

    selectCategories('models')
    expect(visibleRows()).toEqual(['01模型甲'])
    selectCategories('products')
    await act(async () => { await useDailyDigestStore.getState().loadRange(digest.date, digest.date) })
    expect(fetchDigests).toHaveBeenCalledTimes(1)

    await act(async () => { await useDailyDigestStore.getState().loadRange(digest.date, digest.date, true) })
    expect(useDailyDigestStore.getState().error).toBe('offline')
    expect(visibleRows()).toEqual(['01产品甲', '02产品乙'])

    await act(async () => { await useDailyDigestStore.getState().loadRange(digest.date, digest.date, true) })
    expect(fetchDigests).toHaveBeenCalledTimes(3)
    expect(useDailyDigestStore.getState().error).toBeNull()
    expect(visibleRows()).toEqual(['01产品乙'])
    selectCategories('models')
    expect(visibleRows()).toEqual(['01模型甲', '02产品甲'])
  })

  it('请求中快速切换分类，返回后使用最新选择而非发请求时的分类', async () => {
    let resolve!: (response: DailyDigestsResponse) => void
    const fetchDigests = vi.spyOn(api, 'fetchDailyDigests').mockReturnValue(
      new Promise<DailyDigestsResponse>((resolvePromise) => { resolve = resolvePromise }),
    )
    const digest = categorizedDigest()
    selectCategories('products')
    render(<DailyDigestStrip date={digest.date} />)
    let loading!: Promise<void>
    act(() => { loading = useDailyDigestStore.getState().loadRange(digest.date, digest.date) })

    selectCategories('models')
    selectCategories()
    selectCategories('models')
    expect(screen.queryByTestId('daily-digest-strip')).toBeNull()
    await act(async () => { resolve({ digests: [digest] }); await loading })
    expect(visibleRows()).toEqual(['01模型甲'])
    expect(fetchDigests).toHaveBeenCalledTimes(1)
    selectCategories()
    expect(visibleRows()).toHaveLength(8)
  })

  it('点击窗内簇复用 EventCard 的详情弹窗路径并传 preview', async () => {
    const event = makeEvent(101)
    const openModal = vi.fn().mockResolvedValue(undefined)
    useEventsStore.setState({ events: [event] })
    useClusterDetailStore.setState({ openModal })
    useDailyDigestStore.setState({
      digestsByDate: {
        '2026-08-02': makeDigest([
          { rank: 1, cluster_id: 101, title: '第一条', source_count: 3, links: [], category: 'products' },
        ]),
      },
    })

    selectCategories('products')
    render(<DailyDigestStrip date="2026-08-02" />)
    await userEvent.click(screen.getByRole('button', { name: /第一条/ }))

    expect(openModal).toHaveBeenCalledWith(101, event)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it.each([null, 'products'])('详情不可得时关闭错误态并展示快照兜底浮层与新标签原文链接（分类 %s）', async (category) => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    const closeModal = vi.fn()
    const openModal = vi.fn(async () => {
      useClusterDetailStore.setState({ modalState: 'error', modalClusterId: 999, error: 'Not found' })
    })
    useClusterDetailStore.setState({ openModal, closeModal })
    useDailyDigestStore.setState({
      digestsByDate: {
        '2026-08-02': makeDigest([
          {
            rank: 1,
            cluster_id: 999,
            title: '历史快照标题',
            source_count: 3,
            links: [{ label: '原文 A', url: 'https://example.com/a' }],
            category,
          },
        ]),
      },
    })

    selectCategories(...(category ? [category] : []))
    render(<DailyDigestStrip date="2026-08-02" />)
    await userEvent.click(screen.getByRole('button', { name: /历史快照标题/ }))

    const dialog = await screen.findByRole('dialog')
    expect(closeModal).toHaveBeenCalled()
    expect(dialog).toHaveTextContent('历史快照标题')
    expect(dialog).toHaveTextContent('3 个信源')
    expect(screen.getByRole('link', { name: '原文 A' })).toHaveAttribute('target', '_blank')
    expect(screen.getByRole('link', { name: '原文 A' })).toHaveAttribute('rel', 'noreferrer')
    expect(consoleError).not.toHaveBeenCalled()
    consoleError.mockRestore()
  })
})
