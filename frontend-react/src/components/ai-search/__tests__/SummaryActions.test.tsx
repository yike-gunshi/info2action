import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { SummaryActions } from '../SummaryActions'

// 复制与分享的结果走页面级 toast（与「收藏」同一个），不再写进搜索框下方的提示行。
const toastSuccess = vi.fn()
const toastError = vi.fn()
vi.mock('sonner', () => ({ toast: {
  success: (...a: unknown[]) => toastSuccess(...a),
  error: (...a: unknown[]) => toastError(...a),
} }))
import { useAiSearchStore } from '../../../store/aiSearchStore'
import type { AiSearchItem } from '../../../lib/api/aiSearch'

const items: AiSearchItem[] = [
  {
    id: 'first', title: '第一篇', url: 'https://example.com/first', author_name: '作者一',
    platform: 'web', source: '来源一', published_at: '2026-09-19T08:00:00Z',
    created_at: '2026-09-19T08:30:00Z', fetched_at: '2026-09-19T09:00:00Z',
    content: '不能复制的全文', description: '不能复制的描述', ai_summary: '不能复制的来源摘要',
    detail_json: { full_text: '不能复制的详情' }, asr_text: '不能复制的转写',
  },
  {
    id: 'second', title: '第二篇', url: 'https://example.com/second', author_name: '作者二',
    platform: 'web', published_at: '2026-09-20T08:00:00Z', fetched_at: '2026-09-20T09:00:00Z',
  },
]

describe('SummaryActions', () => {
  const writeText = vi.fn<[string], Promise<void>>()
  const feedback = vi.fn()
  beforeEach(() => { toastSuccess.mockClear(); toastError.mockClear() })
  const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')

  beforeEach(() => {
    vi.clearAllMocks()
    writeText.mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    window.history.replaceState(null, '', '/app/?view=reader#ai-search')
    useAiSearchStore.getState().reset()
    useAiSearchStore.setState({
      jobId: 'job-1', phase: 'results', items: [items[1], items[0]], cardOrder: ['second', 'first'],
      summary: {
        ...useAiSearchStore.getState().summary,
        status: 'done', contentMd: '**结论**\n\n正文 [1][2]，再次 [1]，无效 [0][99]。', citedIds: ['second', 'first'],
      },
    })
  })

  afterEach(() => {
    cleanup()
    if (originalClipboard) Object.defineProperty(navigator, 'clipboard', originalClipboard)
    else Reflect.deleteProperty(navigator, 'clipboard')
    window.history.replaceState(null, '', '/')
  })

  it('copies stored Markdown unchanged with references using the same first-appearance numbers', async () => {
    render(<SummaryActions onFeedback={feedback} />)
    fireEvent.click(screen.getByRole('button', { name: '复制' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('已复制正文与参考信息'))

    const copied = writeText.mock.calls[0][0]
    const body = useAiSearchStore.getState().summary.contentMd!
    expect(copied.startsWith(`${body}\n\n`)).toBe(true)
    const references = JSON.parse(copied.slice(copied.indexOf('\n[') + 1))
    expect(references.map((reference: { n: number }) => reference.n)).toEqual([1, 2])
    expect(references[0]).toMatchObject({
      n: 1, title: '第二篇', url: 'https://example.com/second', author_name: '作者二',
      published_at: '2026-09-20T08:00:00Z', fetched_at: '2026-09-20T09:00:00Z',
    })
    expect(references[1]).toMatchObject({
      n: 2, title: '第一篇', url: 'https://example.com/first', author_name: '作者一',
      published_at: '2026-09-19T08:00:00Z', created_at: '2026-09-19T08:30:00Z', fetched_at: '2026-09-19T09:00:00Z',
    })
    expect(Object.keys(references[1]).sort()).toEqual([
      'n', 'title', 'url', 'author_name', 'platform', 'source', 'published_at', 'created_at', 'fetched_at',
    ].sort())
    expect(copied).not.toMatch(/不能复制|content|description|ai_summary|detail_json|asr_text/)
  })

  it('keeps missing metadata explicit and skips unknown citation numbers', async () => {
    useAiSearchStore.setState({ items: [{ id: 'first' }] as AiSearchItem[] })
    render(<SummaryActions onFeedback={feedback} />)
    fireEvent.click(screen.getByRole('button', { name: '复制' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('已复制正文与参考信息'))
    const copied = writeText.mock.calls[0][0]
    expect(JSON.parse(copied.slice(copied.indexOf('\n[') + 1))).toEqual([{
      n: 1, title: null, url: null, author_name: null, platform: null, source: null,
      published_at: null, created_at: null, fetched_at: null,
    }])
  })

  it('copies a directly loadable encoded job URL, keeping the deployment path but dropping the query', async () => {
    useAiSearchStore.setState({ jobId: 'job/空 格?&' })
    const href = window.location.href
    render(<SummaryActions onFeedback={feedback} />)
    fireEvent.click(screen.getByRole('button', { name: '分享' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('分享链接已复制'))

    const shared = new URL(writeText.mock.calls[0][0])
    expect(shared.origin).toBe(window.location.origin)
    expect(shared.pathname).toBe('/app/')
    // The query string must NOT survive: the backend accepts `?token=<shared secret>`
    // as authentication, so carrying it would hand that credential to the recipient.
    expect(shared.search).toBe('')
    expect(shared.hash).toBe(`#ai-search?job=${encodeURIComponent('job/空 格?&')}`)
    expect(window.location.href).toBe(href)
  })

  it('never leaks an auth token from the address bar into the shared link', async () => {
    window.history.replaceState({}, '', '/app/?token=SECRET&view=reader#ai-search')
    useAiSearchStore.setState({ jobId: 'job-1' })
    render(<SummaryActions onFeedback={feedback} />)
    fireEvent.click(screen.getByRole('button', { name: '分享' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('分享链接已复制'))
    expect(writeText.mock.calls[0][0]).not.toContain('SECRET')
    expect(writeText.mock.calls[0][0]).not.toContain('token')
  })

  it.each([
    ['复制', '复制失败，请重试'],
    ['分享', '分享链接复制失败，请重试'],
  ])('reports a rejected clipboard write for %s and allows retry', async (name, message) => {
    writeText.mockRejectedValueOnce(new Error('Permission denied'))
    render(<SummaryActions onFeedback={feedback} />)
    fireEvent.click(screen.getByRole('button', { name }))
    await waitFor(() => expect(toastError).toHaveBeenCalledWith(message))
    expect(screen.getByRole('button', { name })).not.toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name }))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(2))
  })

  it('reports clipboard unavailability without throwing', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
    render(<SummaryActions onFeedback={feedback} />)
    fireEvent.click(screen.getByRole('button', { name: '复制' }))
    await waitFor(() => expect(toastError).toHaveBeenCalledWith('复制失败，请重试'))
  })

  it('disables actions whose required content is absent', () => {
    useAiSearchStore.setState({ jobId: null, summary: { ...useAiSearchStore.getState().summary, contentMd: null } })
    render(<SummaryActions onFeedback={feedback} />)
    expect(screen.getByRole('button', { name: '复制' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '分享' })).toBeDisabled()
    expect(writeText).not.toHaveBeenCalled()
  })

  it.each(['resolve', 'reject'] as const)('drops stale %s feedback when the job changes during a clipboard operation', async outcome => {
    let resolve!: () => void
    let reject!: (error: Error) => void
    writeText.mockImplementationOnce(() => new Promise<void>((done, fail) => { resolve = done; reject = fail }))
    render(<SummaryActions onFeedback={feedback} />)
    fireEvent.click(screen.getByRole('button', { name: '复制' }))
    expect(screen.getByRole('button', { name: '分享' })).toBeDisabled()
    feedback.mockClear()

    act(() => useAiSearchStore.setState({ jobId: 'job-2' }))
    expect(screen.getByRole('button', { name: '分享' })).not.toBeDisabled()
    await act(async () => { if (outcome === 'resolve') resolve(); else reject(new Error('stale operation')) })
    expect(feedback).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '分享' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('分享链接已复制'))
    expect(writeText.mock.calls[1][0]).toContain('#ai-search?job=job-2')
  })

  it('drops feedback after unmount', async () => {
    let resolve!: () => void
    writeText.mockImplementationOnce(() => new Promise<void>(done => { resolve = done }))
    const { unmount } = render(<SummaryActions onFeedback={feedback} />)
    fireEvent.click(screen.getByRole('button', { name: '分享' }))
    feedback.mockClear()
    unmount()
    await act(async () => resolve())
    expect(feedback).not.toHaveBeenCalled()
  })
})
