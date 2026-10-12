import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { AiSearchResults } from '../AiSearchResults'
import { useAiSearchStore } from '../../../store/aiSearchStore'
import { useAuthStore } from '../../../store/authStore'

beforeEach(() => {
  useAiSearchStore.getState().reset()
  useAiSearchStore.setState({ phase: 'results', query: '任务问题', items: [
    { id: 'one', title: '原文', platform: 'rss', fetched_at: '' },
  ], summary: { ...useAiSearchStore.getState().summary, status: 'running' } })
  useAuthStore.setState({ user: null, isChecked: true, isLoading: false })
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false })))
})
afterEach(() => { cleanup(); useAiSearchStore.getState().reset(); vi.unstubAllGlobals() })

describe('AiSearchResults announcements', () => {
  it.each([
    { status: 'insufficient', error: null, message: '站内没有找到足够相关的内容，这次不计次数。换个更具体的说法试试。' },
    { status: 'failed', error: 'retrieval_failed', message: '这次没能找完资料，可以再试一次，不计次数。' },
    { status: 'failed', error: 'retrieval_timeout', message: '这次没能找完资料，可以再试一次，不计次数。' },
    { status: 'failed', error: 'quota_exhausted', message: '当前额度已用完，明天 0 点恢复。' },
    { status: 'failed', error: 'hard_timeout', message: '这次总结用时太长，没能完成，可以再试一次。' },
    { status: 'failed', error: 'parse_error', message: '这次总结没有生成成功，可以再试一次。' },
    { status: 'failed', error: 'candidates_unavailable', message: '有几条原文刚刚下线了，重新搜索一次即可。' },
    { status: 'failed', error: 'job_not_found', message: '这次的结果已失效，可以重新搜索。' },
    { status: 'unavailable', error: 'quota', message: 'AI 总结服务暂时不可用，请稍后再来。' },
    { status: 'unavailable', error: 'HTTP 503 upstream detail', message: 'AI 总结服务暂时不可用，请稍后再来。' },
    { status: 'unavailable', error: 'quota_exhausted', message: '当前额度已用完，明天 0 点恢复。' },
  ] as const)('keeps a pre-existing hidden status mounted when running becomes $status / $error', ({ status, error, message }) => {
    render(<AiSearchResults query="任务问题" />)
    const live = screen.getByRole('status', { name: 'AI 搜索任务状态' })
    expect(live).toHaveClass('sr-only')
    expect(live).toHaveAttribute('aria-live', 'polite')
    expect(live).toBeEmptyDOMElement()
    expect(screen.getByRole('region', { name: 'AI 总结' })).not.toHaveAttribute('aria-live')
    act(() => useAiSearchStore.setState({ summary: { ...useAiSearchStore.getState().summary, status, error } }))
    expect(screen.getByRole('status', { name: 'AI 搜索任务状态' })).toBe(live)
    expect(live.textContent).toBe(message)
    expect(screen.getByRole('textbox', { name: 'AI 搜索' })).toHaveValue('任务问题')
    const region = screen.getByRole('region', { name: 'AI 总结' })
    expect(region.textContent).toBe(message)
    expect(region).not.toHaveAttribute('aria-live')
    expect(region.closest('[aria-live]')).toBeNull()
  })

  it.each([0, 59999, 60000, 90000, null])('announces only the short completion sentence through the same status: elapsed=%s', elapsedMs => {
    render(<AiSearchResults query="任务问题" />)
    const live = screen.getByRole('status', { name: 'AI 搜索任务状态' })
    expect(live).toBeEmptyDOMElement()
    act(() => useAiSearchStore.setState({ summary: { ...useAiSearchStore.getState().summary,
      status: 'done', contentMd: '整篇总结正文', elapsedMs, citedCount: 2 } }))
    const elapsed = elapsedMs == null ? '—' : elapsedMs < 60000 ? '不足 1 分钟' : `${Math.round(elapsedMs / 60000)} 分`
    const message = `用时 ${elapsed} · 基于 2 条内容`
    expect(screen.getByRole('status', { name: 'AI 搜索任务状态' })).toBe(live)
    expect(live.textContent).toBe(message)
    expect(live).not.toHaveTextContent('整篇总结正文')
    expect(document.querySelector('.ai-summary-meta')?.textContent).toBe(message)
    expect(document.querySelector('.ai-summary-meta')).not.toHaveAttribute('aria-live')
    expect(screen.getAllByRole('status')).toEqual([live])
    expect(screen.getByRole('region', { name: 'AI 总结' })).not.toHaveAttribute('aria-live')
  })

  it('keeps the same status through initial job loading, a reason-only failure and another pending task', () => {
    useAiSearchStore.setState({ loadingJob: true, summary: { ...useAiSearchStore.getState().summary, status: 'none' } })
    render(<AiSearchResults query="任务问题" />)
    const live = screen.getByRole('status', { name: 'AI 搜索任务状态' })
    expect(live).toBeEmptyDOMElement()
    act(() => useAiSearchStore.setState({ loadingJob: false,
      summary: { ...useAiSearchStore.getState().summary, status: 'failed', reason: 'timeout' } }))
    expect(screen.getByRole('status', { name: 'AI 搜索任务状态' })).toBe(live)
    expect(live.textContent).toBe('这次总结用时太长，没能完成，可以再试一次。')
    act(() => useAiSearchStore.setState({ summary: { ...useAiSearchStore.getState().summary, status: 'queued' } }))
    expect(screen.getByRole('status', { name: 'AI 搜索任务状态' })).toBe(live)
    expect(live).toBeEmptyDOMElement()
  })
})
