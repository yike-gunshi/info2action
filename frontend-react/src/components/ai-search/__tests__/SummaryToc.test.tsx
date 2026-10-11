import { useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { SummaryPane } from '../SummaryPane'
import { SummaryToc } from '../SummaryToc'
import { useAiSearchStore } from '../../../store/aiSearchStore'

const markdown = '**开篇**\n\n正文里的**强调**。\n\n- **列表加粗**\n\n**中段**\n\n中段正文\n\n**第三节**\n\n后段正文\n\n**结尾**'
let reduced = false

function Harness() {
  const paneRef = useRef<HTMLDivElement>(null)
  return (
    <div className="ai-search-page">
      <header data-testid="topbar"><input aria-label="查询" /></header>
      <div ref={paneRef} data-ai-scroll="summary"><SummaryPane /></div>
      <SummaryToc paneRef={paneRef} />
      <div data-ai-scroll="cards" data-testid="cards" />
    </div>
  )
}

beforeEach(() => {
  reduced = false
  document.documentElement.style.setProperty('--ai-toc-bp', '1500px')
  vi.stubGlobal('innerWidth', 1600)
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({ matches: query.includes('reduce') ? reduced : true })))
  useAiSearchStore.getState().reset()
  useAiSearchStore.setState({ jobId: 'job-1', summary: { ...useAiSearchStore.getState().summary, status: 'done', contentMd: markdown } })
})
afterEach(() => {
  cleanup()
  useAiSearchStore.getState().reset()
  document.documentElement.style.removeProperty('--ai-toc-bp')
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function geometry() {
  const pane = document.querySelector<HTMLElement>('[data-ai-scroll="summary"]')!
  pane.style.setProperty('--ai-pane-fade', '32px')
  Object.defineProperties(pane, { clientHeight: { value: 400 }, scrollHeight: { value: 1000 } })
  vi.spyOn(pane, 'getBoundingClientRect').mockReturnValue({ top: 100 } as DOMRect)
  const offsets = [80, 500, 900, 950]
  screen.getAllByRole('heading', { level: 4 }).forEach((heading, index) => {
    // offsetTop deliberately disagrees with container coordinates: a positioned
    // ancestor must not make later headings jump to the wrong location.
    Object.defineProperty(heading, 'offsetTop', { value: 7 })
    vi.spyOn(heading, 'getBoundingClientRect').mockImplementation(() => ({ top: 100 + offsets[index] - pane.scrollTop }) as DOMRect)
  })
  pane.scrollTo = vi.fn((options: ScrollToOptions) => {
    pane.scrollTop = Math.min(options.top ?? 0, 600)
    fireEvent.scroll(pane)
  }) as typeof pane.scrollTo
  fireEvent.scroll(pane)
  return pane
}

describe('SummaryToc', () => {
  it('uses only rendered standalone bold headings and preserves their document order', () => {
    render(<Harness />)
    const toc = screen.getByRole('navigation', { name: '目录' })
    expect(within(toc).getAllByRole('button').map(button => button.textContent)).toEqual(['开篇', '中段', '第三节', '结尾'])
    expect(within(toc).queryByText('强调')).not.toBeInTheDocument()
    expect(within(toc).queryByText('列表加粗')).not.toBeInTheDocument()
  })

  it('does not render below the token breakpoint and updates on resize, including equality', () => {
    vi.stubGlobal('innerWidth', 1499)
    render(<Harness />)
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
    vi.stubGlobal('innerWidth', 1500)
    fireEvent.resize(window)
    expect(screen.getByRole('navigation')).toBeInTheDocument()
    document.documentElement.style.setProperty('--ai-toc-bp', '1700px')
    fireEvent.resize(window)
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
    vi.stubGlobal('innerWidth', 1700)
    fireEvent.resize(window)
    expect(screen.getByRole('navigation')).toBeInTheDocument()
  })

  it('locks the clicked heading through smooth scroll and bottom detection until a real wheel input', () => {
    render(<Harness />)
    const pane = geometry()
    const third = screen.getByRole('button', { name: '第三节' })
    const last = screen.getByRole('button', { name: '结尾' })
    fireEvent.click(third)
    expect(pane.scrollTo).toHaveBeenCalledWith({ top: 860, behavior: 'smooth' })
    expect(pane.scrollTop).toBe(600)
    expect(third).toHaveAttribute('aria-current', 'location')
    fireEvent.scroll(pane)
    expect(third).toHaveAttribute('aria-current', 'location')
    expect(last).not.toHaveAttribute('aria-current')
    fireEvent.wheel(screen.getByTestId('topbar'))
    fireEvent.scroll(pane)
    expect(last).toHaveAttribute('aria-current', 'location')
    expect(third).not.toHaveAttribute('aria-current')
  })

  it.each(['touchstart', 'keydown'])('unlocks on user %s input', event => {
    render(<Harness />)
    const pane = geometry()
    fireEvent.click(screen.getByRole('button', { name: '中段' }))
    pane.scrollTop = 600
    if (event === 'touchstart') fireEvent.touchStart(pane)
    else fireEvent.keyDown(pane, { key: 'PageDown' })
    fireEvent.scroll(pane)
    expect(screen.getByRole('button', { name: '结尾' })).toHaveAttribute('aria-current', 'location')
  })

  it('keeps the lock during card scrolling, browser zoom, typing, and unrelated keyboard input', () => {
    render(<Harness />)
    const pane = geometry()
    const third = screen.getByRole('button', { name: '第三节' })
    fireEvent.click(third)
    fireEvent.wheel(screen.getByTestId('cards'))
    fireEvent.wheel(pane, { ctrlKey: true })
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'ArrowDown' })
    fireEvent.keyDown(pane, { key: 'Tab' })
    fireEvent.scroll(pane)
    expect(third).toHaveAttribute('aria-current', 'location')
  })

  it('respects reduced motion and uses coordinates relative to the current scroll position', () => {
    reduced = true
    render(<Harness />)
    const pane = geometry()
    pane.scrollTop = 200
    fireEvent.click(screen.getByRole('button', { name: '中段' }))
    expect(pane.scrollTo).toHaveBeenCalledWith({ top: 460, behavior: 'auto' })
  })

  it('rebuilds headings and clears selection when a different job is loaded', () => {
    render(<Harness />)
    const pane = geometry()
    fireEvent.click(screen.getByRole('button', { name: '第三节' }))
    act(() => useAiSearchStore.setState({ jobId: 'job-2', summary: { ...useAiSearchStore.getState().summary, contentMd: '**新的标题**\n\n正文' } }))
    const toc = screen.getByRole('navigation')
    expect(within(toc).getAllByRole('button')).toHaveLength(1)
    expect(within(toc).getByRole('button', { name: '新的标题' })).toHaveAttribute('aria-current', 'location')
    expect(pane).not.toHaveTextContent('第三节')
  })

  it.each(['running', 'failed', 'done'] as const)('omits navigation for %s without rendered headings', status => {
    useAiSearchStore.setState({ summary: { ...useAiSearchStore.getState().summary, status, contentMd: '只有正文' } })
    render(<Harness />)
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })
})
