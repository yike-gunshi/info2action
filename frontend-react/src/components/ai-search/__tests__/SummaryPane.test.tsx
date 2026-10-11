import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { SummaryMeta, SummaryPane } from '../SummaryPane'
import { useAiSearchStore } from '../../../store/aiSearchStore'
import type { AiSearchItem } from '../../../lib/api/aiSearch'

const items = [{ id: 'first' }, { id: 'second' }] as AiSearchItem[]

function SummaryWithMeta() {
  return <><SummaryMeta /><SummaryPane /></>
}

describe('SummaryPane', () => {
  const highlight = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
    useAiSearchStore.getState().reset()
    useAiSearchStore.setState({ items, highlight, query: 'seedance 2.5' })
    Object.defineProperty(window, 'matchMedia', { configurable: true, value: vi.fn().mockReturnValue({ matches: false }) })
  })

  afterEach(cleanup)

  function summary(overrides: Partial<ReturnType<typeof useAiSearchStore.getState>['summary']>) {
    useAiSearchStore.setState({ summary: { ...useAiSearchStore.getState().summary, ...overrides } })
  }

  it('keeps visible completion metadata and the article outside live regions', () => {
    summary({ status: 'running' })
    render(<SummaryWithMeta />)
    const meta = document.querySelector('.ai-summary-meta')!
    const article = screen.getByRole('region', { name: 'AI 总结' })
    expect(article).not.toHaveAttribute('aria-live')
    act(() => summary({ status: 'done', contentMd: '一篇很长的总结正文', elapsedMs: 60000, citedCount: 2 }))
    expect(document.querySelector('.ai-summary-meta')).toBe(meta)
    expect(meta).not.toHaveAttribute('role')
    expect(meta).not.toHaveAttribute('aria-live')
    expect(meta).toHaveTextContent('用时 1 分 · 基于 2 条内容')
    expect(meta).not.toHaveTextContent('一篇很长的总结正文')
    expect(article).not.toHaveAttribute('aria-live')
    expect(article.closest('[aria-live]')).toBeNull()
    expect(article).toHaveTextContent('一篇很长的总结正文')
  })

  it('renders model Markdown links and automatically recognised URLs as inert text in every block', () => {
    summary({ status: 'done', contentMd: '# 官网 https://example.com/head\n\n[资源](https://example.com/resource) https://example.com/path 官网 example.com [1]\n\n- https://example.com/list\n  - https://example.com/nested\n\n1. https://example.com/ordered' })
    const { container } = render(<SummaryPane />)
    const article = container.querySelector('.ai-summary-article')!
    expect(article.querySelectorAll('a')).toHaveLength(0)
    expect(article).toHaveTextContent('[资源](https://example.com/resource)')
    expect(article).toHaveTextContent('https://example.com/path')
    expect(article).toHaveTextContent('https://example.com/nested')
    expect(screen.getByRole('button', { name: '查看引用 1' })).toBeInTheDocument()
  })

  it.each(['[3][1]', '[1] [3]', '[3]\t [1]'])('groups adjacent citations and sorts their display: %s', contentMd => {
    useAiSearchStore.setState({ items: [...items, { id: 'third' }] as AiSearchItem[] })
    summary({ status: 'done', contentMd })
    const { container } = render(<SummaryPane />)
    const groups = container.querySelectorAll('.ai-search-chip-group')
    expect(groups).toHaveLength(1)
    expect(within(groups[0] as HTMLElement).getAllByRole('button').map(button => button.textContent)).toEqual(['1', '3'])
    expect(useAiSearchStore.getState().summary.contentMd).toBe(contentMd)
  })

  it('keeps citations separated by text in individual groups, each glued to the preceding character', () => {
    summary({ status: 'done', contentMd: '正文[1]文字[2]' })
    const { container } = render(<SummaryPane />)
    const groups = container.querySelectorAll('.ai-search-chip-group')
    expect(groups).toHaveLength(2)
    expect(Array.from(groups, group => group.textContent)).toEqual(['1', '2'])
    for (const group of groups) {
      expect(group.parentElement).toHaveClass('ai-search-chip-glue')
      expect(group.previousSibling?.nodeType).toBe(Node.TEXT_NODE)
      expect(Array.from(group.previousSibling?.textContent ?? '')).toHaveLength(1)
    }
  })

  it.each([
    ['判断[1][2]', '判', '断'],
    ['结果😀[1][2]', '结果', '😀'],
  ])('moves only the preceding character into the citation wrapper: %s', (contentMd, prefix, character) => {
    summary({ status: 'done', contentMd })
    const { container } = render(<SummaryPane />)
    const glue = container.querySelector('.ai-search-chip-glue')!
    expect(glue.textContent).toBe(`${character}12`)
    expect(glue.querySelector('.ai-search-chip-group')).toHaveTextContent('12')
    expect(glue.previousSibling?.textContent).toBe(prefix)
    expect(glue.previousSibling?.textContent?.endsWith(character)).toBe(false)
  })

  it.each(['[1]', '**重点**[1]', '`代码`[1]'])('leaves a citation without preceding text unwrapped: %s', contentMd => {
    summary({ status: 'done', contentMd })
    const { container } = render(<SummaryPane />)
    expect(container.querySelector('.ai-search-chip-glue')).toBeNull()
    expect(container.querySelector('.ai-search-chip-group')).toHaveTextContent('1')
  })

  it.each([
    ['token [1]', 'token1'],
    ['token \t [1]', 'token1'],
    ['token  text [1] after  text', 'token  text1 after  text'],
  ])('removes only whitespace immediately before a citation group: %s', (contentMd, expectedText) => {
    summary({ status: 'done', contentMd })
    const { container } = render(<SummaryPane />)
    expect(container.querySelector('.ai-summary-article p')?.textContent).toBe(expectedText)
    const group = container.querySelector('.ai-search-chip-group')!
    expect(group.previousSibling?.nodeType).toBe(Node.TEXT_NODE)
    expect(group.parentElement).toHaveClass('ai-search-chip-glue')
  })

  it('highlights only the item for the second button in a sorted group', () => {
    useAiSearchStore.setState({ items: [...items, { id: 'third' }] as AiSearchItem[] })
    summary({ status: 'done', contentMd: '正文[3][1]' })
    const { container } = render(<SummaryPane />)
    const group = container.querySelector<HTMLElement>('.ai-search-chip-group')!
    const buttons = within(group).getAllByRole('button')
    expect(buttons.map(button => button.getAttribute('aria-label'))).toEqual(['查看引用 1', '查看引用 3'])
    fireEvent.click(buttons[1])
    expect(highlight).toHaveBeenCalledOnce()
    expect(highlight).toHaveBeenCalledWith('third')
  })

  it('leaves missing citations as plain text and preserves whitespace outside valid group prefixes', () => {
    summary({ status: 'done', contentMd: '正文[1] [999] [2]' })
    const { container } = render(<SummaryPane />)
    const groups = container.querySelectorAll('.ai-search-chip-group')
    expect(Array.from(groups, group => group.textContent)).toEqual(['1', '2'])
    expect(container.querySelector('.ai-summary-article p')).toHaveTextContent('[999]')
    expect(container.querySelector('.ai-summary-article p')?.textContent).toBe('正文1 [999]2')
    expect(screen.queryByRole('button', { name: '查看引用 999' })).not.toBeInTheDocument()
  })

  it('updates citation grouping when previously missing items arrive', () => {
    summary({ status: 'done', contentMd: '正文[3][1]' })
    const { container } = render(<SummaryPane />)
    expect(container.querySelector('.ai-search-chip-group')).toHaveTextContent('1')
    expect(container.querySelector('.ai-summary-article')).toHaveTextContent('[3]')
    act(() => useAiSearchStore.setState({ items: [...items, { id: 'third' }] as AiSearchItem[] }))
    expect(container.querySelectorAll('.ai-search-chip-group')).toHaveLength(1)
    expect(container.querySelector('.ai-search-chip-group')).toHaveTextContent('13')
    expect(container.querySelector('.ai-summary-article')).not.toHaveTextContent('[3]')
  })

  it('shows loading count, positive queue position and motion-safe skeleton', () => {
    summary({ status: 'queued', position: 2 })
    const { container } = render(<SummaryWithMeta />)
    expect(screen.getByText('正在整理 2 条内容 · 通常需要 8 到 15 分钟 · 排在第 2 位')).toBeInTheDocument()
    expect(container.querySelectorAll('.animate-pulse')).toHaveLength(7)
    expect(container.querySelectorAll('.motion-reduce\\:animate-none')).toHaveLength(7)
    const bars = container.querySelectorAll('.ai-summary-skeleton-bar')
    expect(bars).toHaveLength(7)
    expect(bars[3]).toHaveClass('ai-summary-skeleton-heading')
    expect(Array.from(bars, bar => (bar as HTMLElement).style.width)).toEqual(['92%', '100%', '96%', '', '88%', '100%', '70%'])
  })

  it('does not display a zero queue position', () => {
    summary({ status: 'running', position: 0 })
    render(<SummaryWithMeta />)
    expect(screen.queryByText(/排在第/)).not.toBeInTheDocument()
  })

  it('renders first-appearance citation numbers and locates the matching API item', () => {
    summary({ status: 'done', contentMd: '**结论**\n\n正文 [1][2]\n\n- 更多 [1]', citedIds: ['second', 'first'], model: 'GLM-5.3', elapsedMs: 480000, citedCount: 2 })
    useAiSearchStore.setState({ items: [items[1], items[0]], cardOrder: ['second', 'first'] })
    const { container } = render(<><SummaryWithMeta /><div id="ai-card-second" /></>)
    expect(screen.getByText('用时 8 分 · 基于 2 条内容')).toBeInTheDocument()
    expect(screen.queryByText(/GLM-5\.3/)).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 4, name: '结论' })).toBeInTheDocument()
    expect(container.querySelector('li button')).toHaveAttribute('aria-label', '查看引用 1')
    fireEvent.click(screen.getAllByRole('button', { name: '查看引用 1' })[0])
    expect(highlight).toHaveBeenCalledWith('second', expect.any(Number))
    fireEvent.click(screen.getByRole('button', { name: '查看引用 2' }))
    // 找不到卡片时只传 itemId，时长用 store 默认值（重构后不再显式传 undefined）
    expect(highlight).toHaveBeenLastCalledWith('first')
  })

  it('falls back to citedIds count and preserves clickable citations', () => {
    summary({ status: 'done', contentMd: '正文 [1]', citedIds: ['first'], elapsedMs: 90000, citedCount: null })
    render(<><SummaryWithMeta /><div id="ai-card-first" /></>)
    expect(screen.getByText('用时 2 分 · 基于 1 条内容')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看引用 1' }))
    expect(highlight).toHaveBeenCalledWith('first', expect.any(Number))
  })

  it('distinguishes standalone bold headings from inline and list emphasis', () => {
    summary({ status: 'done', contentMd: '**结论**\n\n正文里的**重点**保留在段内 [1]\n\n- **产品**：说明\n  - 二级条目\n\n1. 有序条目\n\n**其他相关内容**\n\n基于 2 条内容' })
    const { container } = render(<SummaryWithMeta />)
    expect(screen.getAllByRole('heading', { level: 4 }).map(heading => heading.textContent)).toEqual(['结论', '其他相关内容'])
    expect(screen.getByText('重点').closest('p')).toHaveTextContent('正文里的重点保留在段内')
    expect(screen.getByText('产品').closest('li')).toHaveTextContent('产品：说明')
    expect(screen.getByText('二级条目').closest('ul')?.parentElement?.tagName).toBe('LI')
    expect(screen.getByText('有序条目').closest('ol')).toBeInTheDocument()
    // 末尾那句「基于 N 条内容」与上方 meta 行重复，设计要求不渲染（用户 2026-09-21 反馈）。
    // 旧任务的 markdown 里仍存着这句，所以是渲染期过滤，不是生成期删除。
    expect(screen.queryByText('基于 2 条内容')).not.toBeInTheDocument()
    expect(document.querySelector('.ai-summary-foot')).toBeNull()
    expect(container.querySelector('.ai-summary-article .space-y-1, .ai-summary-article .pl-5')).toBeNull()
  })

  it('preserves code and inert URLs, escapes HTML, and leaves invalid citations inert', () => {
    summary({ status: 'done', contentMd: '`[1]` https://example.com/[2] <script>alert(1)</script> [0] [999]' })
    const { container } = render(<SummaryWithMeta />)
    expect(container.querySelector('code')).toHaveTextContent('[1]')
    expect(container.querySelector('code button, a button, script')).toBeNull()
    expect(container.querySelectorAll('.ai-summary-article a')).toHaveLength(0)
    expect(container.querySelector('.ai-summary-article')).toHaveTextContent('https://example.com/')
    expect(screen.queryByRole('button', { name: '查看引用 999' })).not.toBeInTheDocument()
    expect(container.textContent).toContain('<script>alert(1)</script>')
  })

  it('recognizes a paragraph containing only bold fragments separated by whitespace as a heading', () => {
    summary({ status: 'done', contentMd: '**产品** **变化**\n\n**产品** 的变化\n\n- **加粗列表项**' })
    render(<SummaryWithMeta />)
    expect(screen.getAllByRole('heading', { level: 4 }).map(heading => heading.textContent)).toEqual(['产品 变化'])
  })

  it.each([
    { state: 'failed', error: 'hard_timeout', message: '这次总结用时太长，没能完成，可以再试一次。' },
    { state: 'service', error: 'quota', message: 'AI 总结服务暂时不可用，请稍后再来。' },
    { state: 'stale', error: 'job_not_found', message: '这次的结果已失效，可以重新搜索。' },
    { state: 'empty', error: null, message: '站内近 90 天没有找到相关内容，换个说法试试。' },
  ])('renders one sentence and no buttons for $state', ({ state, error, message }) => {
    summary({ status: 'failed', error })
    useAiSearchStore.setState({ emptyResult: state === 'empty', items: [] })
    render(<SummaryWithMeta />)
    const region = screen.getByRole('region', { name: 'AI 总结' })
    expect(region).not.toHaveAttribute('aria-live')
    expect(region.textContent).toBe(message)
    expect(region.querySelectorAll('p')).toHaveLength(1)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()

    act(() => useAiSearchStore.setState({ items }))
    expect(region.textContent).toBe(message)
  })

  describe.each(['failed', 'unavailable'] as const)('%s', (status) => {
    it.each([
      ['hard_timeout', '这次总结用时太长，没能完成，可以再试一次。'],
      ['timeout', '这次总结用时太长，没能完成，可以再试一次。'],
      ['parse_error', '这次总结没有生成成功，可以再试一次。'],
      ['internal', '这次总结没有生成成功，可以再试一次。'],
      ['HTTP 503', '这次总结没有生成成功，可以再试一次。'],
      ['candidates_unavailable', '有几条原文刚刚下线了，重新搜索一次即可。'],
      ['quota', 'AI 总结服务暂时不可用，请稍后再来。'],
      ['unavailable', 'AI 总结服务暂时不可用，请稍后再来。'],
      ['quota_exhausted', '当前额度已用完，明天 0 点恢复。'],
    ])(
      'maps the failure sentence for %s',
      (error, message) => {
        summary({ status, error })
        render(<SummaryWithMeta />)
        expect(screen.getByRole('region', { name: 'AI 总结' }).textContent).toBe(status === 'unavailable' && error !== 'quota_exhausted' ? 'AI 总结服务暂时不可用，请稍后再来。' : message)
        expect(screen.queryByRole('button')).not.toBeInTheDocument()
      },
    )

    it.each([
      ['quota', 'AI 总结服务暂时不可用，请稍后再来。'],
      ['job_not_found', '这次的结果已失效，可以重新搜索。'],
    ])('supports error and reason payloads for %s', (code, message) => {
      summary({ status, error: code, reason: null })
      render(<SummaryWithMeta />)
      const region = screen.getByRole('region', { name: 'AI 总结' })
      const expected = status === 'unavailable' ? 'AI 总结服务暂时不可用，请稍后再来。' : message
      expect(region.textContent).toBe(expected)

      act(() => summary({ error: null, reason: code }))
      expect(region.textContent).toBe(expected)
      expect(screen.queryByRole('button')).not.toBeInTheDocument()
    })
  })

  it('does not expose unknown unavailable reasons from POST responses', () => {
    summary({ status: 'unavailable', reason: 'HTTP 503 upstream detail' })
    render(<SummaryWithMeta />)
    expect(screen.getByRole('region', { name: 'AI 总结' }).textContent).toBe('AI 总结服务暂时不可用，请稍后再来。')
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it.each([null, 'internal', 'unknown'])('uses the service-unavailable sentence for unavailable status with %s', error => {
    summary({ status: 'unavailable', error, reason: null })
    render(<SummaryWithMeta />)
    expect(screen.getByRole('region', { name: 'AI 总结' })).toHaveTextContent('AI 总结服务暂时不可用，请稍后再来。')
  })

  it.each([0, 59999, 60000, null])('formats elapsed time at the minute boundary: %s', elapsedMs => {
    summary({ status: 'done', contentMd: '', elapsedMs })
    render(<SummaryWithMeta />)
    const elapsed = elapsedMs === null ? '—' : elapsedMs < 60000 ? '不足 1 分钟' : '1 分'
    expect(screen.getByText(`用时 ${elapsed} · 基于 0 条内容`)).toBeInTheDocument()
    expect(screen.getByText('本次未生成正文')).toHaveClass('text-muted-foreground')
  })

  it.each(['done', 'failed'] as const)('keeps the summary region outside live regions when pending becomes %s', status => {
    summary({ status: 'running' })
    render(<SummaryWithMeta />)
    const region = screen.getByRole('region', { name: 'AI 总结' })
    expect(region).not.toHaveAttribute('aria-live')
    expect(region).toHaveAttribute('aria-busy', 'true')
    act(() => summary({ status, contentMd: '完成正文', error: 'hard_timeout' }))
    expect(screen.getByRole('region', { name: 'AI 总结' })).toBe(region)
    expect(region).not.toHaveAttribute('aria-live')
    if (status === 'done') expect(document.querySelector('.ai-summary-meta')).toHaveTextContent('用时 — · 基于 0 条内容')
    expect(region).not.toHaveAttribute('aria-busy', 'true')
    expect(region).toHaveTextContent(status === 'done' ? '完成正文' : '这次总结用时太长，没能完成，可以再试一次。')
  })

  it('取既有任务期间显示骨架，而不是空白', () => {
    // 此刻还不知道任务是完成还是进行中，但页面已确定要放一篇文章；
    // 空着会让人以为出错了（用户 2026-09-22 反馈）。
    useAiSearchStore.setState({ loadingJob: true, summary: { ...useAiSearchStore.getState().summary, status: 'none' } })
    render(<SummaryWithMeta />)
    expect(document.querySelector('.ai-summary-skeleton')).not.toBeNull()
  })
})
