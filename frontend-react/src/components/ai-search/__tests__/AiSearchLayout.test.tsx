import { readFileSync } from 'node:fs'
import { parse, type Root } from 'postcss'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { AiSearchPage } from '../AiSearchPage'
import { useAiSearchStore } from '../../../store/aiSearchStore'
import { useBodyScrollLock } from '../../../hooks/useBodyScrollLock'
import { ReadingModalShell } from '../../reading/ReadingModal'
import type { AiSearchItem } from '../../../lib/api/aiSearch'

vi.mock('../../layout/TopBar', () => ({ TopBar: () => <header data-testid="topbar">TopBar</header> }))

let desktop = true
let mediaChange: (() => void) | undefined
let reduced = false
const items: AiSearchItem[] = [{ id: 'one', title: '来源一', platform: 'rss', fetched_at: '' }]
const layoutCss = readFileSync('src/components/ai-search/ai-search.css', 'utf8')
const cardsCss = readFileSync('src/components/ai-search/CitationCard.css', 'utf8')
const tocCss = readFileSync('src/components/ai-search/SummaryToc.css', 'utf8')
const summaryCss = readFileSync('src/components/ai-search/SummaryPane.css', 'utf8')

function declarations(styles: Root, selector: string, media?: string) {
  const result: Record<string, string> = {}
  styles.walkRules(selector, rule => {
    const context = rule.parent?.type === 'atrule' ? rule.parent.params : undefined
    if (context !== media) return
    rule.walkDecls(({ prop, value }) => { result[prop] = value })
  })
  return result
}

beforeEach(() => {
  desktop = true
  reduced = false
  mediaChange = undefined
  document.documentElement.style.setProperty('--ai-toc-bp', '1500px')
  vi.stubGlobal('innerWidth', 1760)
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
    get matches() { return query.includes('reduce') ? reduced : query.includes('min-width') ? desktop : false },
    addEventListener: (_: string, fn: () => void) => { mediaChange = fn },
    removeEventListener: vi.fn(),
  })))
  window.history.replaceState(null, '', '/#ai-search')
  useAiSearchStore.getState().reset()
  useAiSearchStore.setState({ unavailable: false })
})
afterEach(() => {
  cleanup()
  useAiSearchStore.getState().reset()
  document.documentElement.style.removeProperty('overflow')
  document.documentElement.style.removeProperty('scrollbar-gutter')
  document.documentElement.style.removeProperty('--ai-toc-bp')
  document.documentElement.style.removeProperty('padding-right')
  document.documentElement.style.removeProperty('color')
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function results(contentMd = '正文 [1]') {
  act(() => useAiSearchStore.setState({ phase: 'results', query: '测试问题', items, cardOrder: ['one'],
    summary: { ...useAiSearchStore.getState().summary, status: 'done', contentMd } }))
  const pane = document.querySelector<HTMLElement>('[data-ai-scroll="summary"]')!
  const cards = screen.getByLabelText('引用内容')
  return { pane, cards }
}

function wheel(target: Element, deltaY = 80, options: WheelEventInit = {}) {
  const event = new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY, ...options })
  fireEvent(target, event)
  return event
}

describe('AI search layout and scroll lifecycle', () => {
  describe.each([false, true])('shared lock mount order: modal first=%s', modalFirst => {
    it.each([false, true])('preserves the first lock until both close and restores all inline styles: close page first=%s', closePageFirst => {
      const html = document.documentElement
      html.style.setProperty('overflow', 'scroll', 'important')
      html.style.setProperty('padding-right', '7px', 'important')
      html.style.setProperty('scrollbar-gutter', 'stable both-edges', 'important')
      html.style.setProperty('color', 'red')
      const original = html.style.cssText
      vi.spyOn(html, 'clientWidth', 'get').mockReturnValue(window.innerWidth - 15)
      const mountPage = () => render(<AiSearchPage />)
      const mountModal = () => render(
        <ReadingModalShell ariaLabelledby="shared-lock-title" isClosing={false} onClose={() => {}} panelTestId="shared-lock-panel">
          <h2 id="shared-lock-title">阅读详情</h2>
        </ReadingModalShell>,
      )
      const first = modalFirst ? mountModal() : mountPage()
      const locked = html.style.cssText
      const second = modalFirst ? mountPage() : mountModal()
      expect(html.style.cssText).toBe(locked)
      expect(html.style.paddingRight).toBe(modalFirst ? '15px' : '7px')
      expect(html.style.scrollbarGutter).toBe('auto')
      const page = modalFirst ? second : first
      const modal = modalFirst ? first : second
      ;(closePageFirst ? page : modal).unmount()
      expect(html.style.cssText).toBe(locked)
      ;(closePageFirst ? modal : page).unmount()
      expect(html.style.cssText).toBe(original)
      html.style.removeProperty('padding-right')
      html.style.removeProperty('color')
    })
  })

  it.each([
    { delta: 80, position: 0, pass: true },
    { delta: -80, position: 100, pass: true },
    { delta: 80, position: 200, pass: false },
    { delta: -80, position: 0, pass: false },
  ])('lets an unlabelled popover ancestor scroll only when it can continue: %j', ({ delta, position, pass }) => {
    render(<AiSearchPage />)
    const { pane } = results()
    const list = document.createElement('div')
    list.style.overflowY = 'auto'
    Object.defineProperties(list, { clientHeight: { value: 100 }, scrollHeight: { value: 300 } })
    list.scrollTop = position
    const row = document.createElement('button')
    const text = document.createElement('span')
    row.append(text)
    list.append(row)
    screen.getByTestId('topbar').append(list)
    expect(wheel(text, delta).defaultPrevented).toBe(!pass)
    expect(pane.scrollTop).toBe(pass ? 0 : delta)
  })

  it('checks outer scrollable ancestors when an inner list is at its boundary, including inside the article', () => {
    render(<AiSearchPage />)
    const { pane } = results()
    const outer = document.createElement('div')
    const inner = document.createElement('div')
    for (const list of [inner, outer]) {
      list.style.overflowY = 'scroll'
      Object.defineProperties(list, { clientHeight: { value: 100 }, scrollHeight: { value: 300 } })
    }
    inner.scrollTop = 200
    const child = document.createElement('span')
    inner.append(child)
    outer.append(inner)
    pane.append(outer)
    expect(wheel(child).defaultPrevented).toBe(false)
    expect(pane.scrollTop).toBe(0)
    outer.style.overflowY = 'hidden'
    expect(wheel(child).defaultPrevented).toBe(true)
    expect(pane.scrollTop).toBe(80)
  })
  it('uses the existing gray tokens for auth and quota blocked buttons', () => {
    expect(declarations(parse(layoutCss), '.ai-search-submit-text[data-blocked="true"]')).toMatchObject({
      background: 'var(--muted)', color: 'var(--muted-foreground)',
    })
  })

  it('keeps citation spacing on the unbroken group and justifies only body paragraphs and list items', () => {
    const styles = parse(layoutCss)
    expect(declarations(styles, '.ai-search-chip-glue')).toEqual({ 'white-space': 'nowrap' })
    expect(declarations(styles, '.ai-search-chip-group')).toMatchObject({
      'white-space': 'nowrap', padding: '0 var(--ai-chip-pad)', 'vertical-align': 'super',
      'font-size': 'var(--ai-chip-size)', color: 'var(--ai-chip-color)',
    })
    expect(declarations(styles, '.ai-search-chip')).toMatchObject({ padding: '0', 'vertical-align': 'baseline' })
    expect(declarations(styles, '.ai-search-chip-item + .ai-search-chip-item::before')).toMatchObject({ content: "','" })
    const body = parse(summaryCss)
    expect(declarations(body, '.ai-summary-article p,\n.ai-summary-article li')).toMatchObject({
      'text-align': 'justify', 'text-autospace': 'normal',
    })
    expect(declarations(body, '.ai-summary-article h4')).not.toHaveProperty('text-align')
    expect(declarations(body, '.ai-summary-meta')).not.toHaveProperty('text-align')
  })

  it('centers the document and fixed-width citation column together without retired spacing tokens', () => {
    render(<AiSearchPage />)
    const { pane, cards } = results()
    const group = document.querySelector('.ai-search-results')
    expect(group).not.toBeNull()
    expect(pane.closest('.ai-search-left')?.parentElement).toBe(group)
    expect(cards.parentElement).toBe(group)
    const styles = parse(layoutCss)
    expect(declarations(styles, '.ai-search-results')).toMatchObject({
      position: 'absolute', inset: '0', display: 'flex', 'justify-content': 'center',
      gap: 'var(--ai-col-gap)', padding: '0 var(--ai-shell-pad)',
    })
    expect(declarations(styles, '.ai-search-left')).toMatchObject({
      position: 'static', flex: '0 1 var(--ai-doc-max)', 'min-width': '0', padding: 'var(--ai-left-pt) 0 0',
    })
    expect(declarations(styles, '.ai-search-doc')).toMatchObject({
      width: '100%', height: '100%', display: 'flex', 'flex-direction': 'column',
    })
    expect(declarations(parse(cardsCss), '.ai-search-cards', '(min-width: 1024px)')).toMatchObject({
      position: 'static', flex: '0 0 calc(var(--ai-col-w) + var(--ai-sb-gutter))', width: 'calc(var(--ai-col-w) + var(--ai-sb-gutter))', 'margin-right': 'calc(-1 * var(--ai-sb-gutter))',
      'overflow-y': 'auto', 'scrollbar-width': 'thin', 'overscroll-behavior': 'contain',
    })
    expect(layoutCss + cardsCss).not.toMatch(/--ai-(?:col-margin|doc-pad)\b/)
  })

  it('uses transparent native thin scrollbars in both panes while keeping the toc and idle container hidden', () => {
    const styles = parse(layoutCss)
    const cards = parse(cardsCss)
    expect(declarations(styles, '.ai-search-pane')).toMatchObject({
      '--scrollbar-hide-ms': 'var(--ai-sb-hide-ms)',
      'padding-right': 'max(0px, calc(var(--ai-sb-gutter) - var(--scrollbar-actual, 0px)))',
      'scrollbar-width': 'thin', 'scrollbar-color': 'transparent transparent',
    })
    expect(declarations(cards, '.ai-search-cards')).toHaveProperty('--scrollbar-hide-ms', 'var(--ai-sb-hide-ms)')
    expect(declarations(cards, '.ai-search-cards', '(min-width: 1024px)')).toMatchObject({
      padding: '0 max(0px, calc(var(--ai-sb-gutter) - var(--scrollbar-actual, 0px))) var(--ai-col-pb) 0',
      'scrollbar-width': 'thin', 'scrollbar-color': 'transparent transparent',
    })
    expect(declarations(styles, '.ai-search-pane::-webkit-scrollbar')).toEqual({ width: 'var(--ai-sb-w)' })
    expect(declarations(cards, '.ai-search-cards::-webkit-scrollbar', '(min-width: 1024px)')).toEqual({ width: 'var(--ai-sb-w)' })
    expect(declarations(parse(tocCss), '.ai-search-toc-inner')).toHaveProperty('scrollbar-width', 'none')
    expect(declarations(styles, ".ai-search-body[data-idle='true'] .ai-search-results")).toHaveProperty('scrollbar-width', 'none')
  })

  it('attaches the reveal hook to both containers when results mount after idle', () => {
    vi.useFakeTimers()
    try {
      render(<AiSearchPage />)
      const { pane, cards } = results()
      for (const container of [pane, cards]) {
        container.style.setProperty('--scrollbar-hide-ms', '1000ms')
        fireEvent.wheel(window) // 只有真实输入之后的滚动才显示滚动条
        fireEvent.scroll(container)
        expect(container).toHaveAttribute('data-scrollbar', 'on')
      }
      vi.advanceTimersByTime(1000)
      expect(pane).not.toHaveAttribute('data-scrollbar')
      expect(cards).not.toHaveAttribute('data-scrollbar')
    } finally {
      cleanup()
      vi.useRealTimers()
    }
  })

  it('returns the result group to normal single-column flow on narrow screens', () => {
    expect(declarations(parse(layoutCss), '.ai-search-results', '(width < 1024px)')).toMatchObject({
      position: 'static', display: 'block', padding: '0', gap: '0',
    })
  })

  it('includes the visible absolute-positioned toc in centering and removes its extra padding when hidden', () => {
    render(<AiSearchPage />)
    const { cards } = results('**结论**\n\n正文 [1]')
    const group = document.querySelector('.ai-search-results')!
    const padded = '.ai-search-results:has(> .ai-search-toc)'
    expect(group.matches(padded)).toBe(true)
    expect(screen.getByRole('navigation', { name: '目录' }).parentElement).toBe(group)
    expect(cards.parentElement).toBe(group)
    expect(declarations(parse(layoutCss), padded, '(min-width: 1024px)')).toMatchObject({
      'padding-left': 'calc(var(--ai-shell-pad) + (var(--ai-toc-w) + var(--ai-toc-gap)))',
    })
    expect(declarations(parse(tocCss), '.ai-search-toc')).toMatchObject({
      position: 'absolute',
      right: 'calc(50% + (var(--ai-doc-max) + var(--ai-col-gap) + var(--ai-col-w)) / 2 + var(--ai-toc-gap) - (var(--ai-toc-w) + var(--ai-toc-gap)) / 2)',
    })
    for (const width of [1499, 768]) {
      vi.stubGlobal('innerWidth', width)
      fireEvent.resize(window)
      expect(screen.queryByRole('navigation', { name: '目录' })).not.toBeInTheDocument()
      expect(group.matches(padded)).toBe(false)
    }
    vi.stubGlobal('innerWidth', 1760)
    fireEvent.resize(window)
    expect(group.matches(padded)).toBe(true)
    results('没有目录标题的正文 [1]')
    expect(group.matches(padded)).toBe(true)
    expect(group.querySelector('.ai-search-toc')).toHaveAttribute('aria-hidden', 'true')
    expect(group.querySelector('.ai-search-toc')).toBeEmptyDOMElement()
    expect(declarations(parse(layoutCss), '.ai-search-results').padding).toBe('0 var(--ai-shell-pad)')
  })

  it.each(['loadingJob', 'submitting', 'queued', 'running'] as const)('reserves citation and wide toc skeletons during %s', stage => {
    render(<AiSearchPage />)
    act(() => useAiSearchStore.setState({
      phase: stage === 'submitting' ? 'submitting' : 'results',
      loadingJob: stage === 'loadingJob', items: [], cardOrder: [],
      summary: { ...useAiSearchStore.getState().summary, status: stage === 'queued' || stage === 'running' ? stage : 'none' },
    }))
    const cards = screen.getByLabelText('引用内容')
    const skeleton = cards.querySelector('[aria-hidden="true"]')!
    expect(skeleton.querySelectorAll('.ai-citation-card-skeleton')).toHaveLength(8)
    const toc = document.querySelector('.ai-search-toc')!
    expect(toc).toHaveAttribute('aria-hidden', 'true')
    expect(toc.querySelectorAll('.ai-search-toc-skeleton-row')).toHaveLength(6)
    expect(toc.querySelector('[aria-current]')).toBeNull()
    act(() => useAiSearchStore.setState({ loadingJob: false, phase: 'results', items, cardOrder: ['one'],
      summary: { ...useAiSearchStore.getState().summary, status: 'done', contentMd: '**结论**\n\n正文' } }))
    expect(screen.getByLabelText('引用内容')).toBe(cards)
    expect(document.querySelector('.ai-search-toc')).toBe(toc)
    expect(cards.querySelector('.ai-citation-card-skeleton')).toBeNull()
    expect(screen.getByRole('navigation', { name: '目录' })).toBe(toc)
  })

  it.each(['done', 'failed', 'unavailable'] as const)('keeps an empty wide toc placeholder for %s without headings', status => {
    render(<AiSearchPage />)
    results()
    act(() => useAiSearchStore.setState({ summary: { ...useAiSearchStore.getState().summary, status, contentMd: '只有正文' } }))
    const toc = document.querySelector('.ai-search-toc')!
    expect(toc).toHaveAttribute('aria-hidden', 'true')
    expect(toc).toBeEmptyDOMElement()
    expect(screen.getByLabelText('引用内容')).toBeInTheDocument()
  })

  it.each(['empty', 'stale'] as const)('omits both reserved columns for %s results', state => {
    render(<AiSearchPage />)
    results('**结论**\n\n正文')
    act(() => useAiSearchStore.setState({ emptyResult: state === 'empty',
      summary: { ...useAiSearchStore.getState().summary, status: 'failed', error: state === 'stale' ? 'job_not_found' : null } }))
    expect(document.querySelector('.ai-search-toc')).toBeNull()
    expect(screen.queryByLabelText('引用内容')).not.toBeInTheDocument()
    expect(document.querySelector('main')).toHaveAttribute('data-columns', 'false')
  })

  it('uses title and header tokens with wrapping instead of truncation', () => {
    const styles = parse(layoutCss)
    expect(declarations(styles, '.ai-search-qtitle')).toMatchObject({
      'font-size': 'var(--ai-qtitle-size)', 'font-weight': 'var(--ai-qtitle-weight)',
      'line-height': 'var(--ai-qtitle-lh)', color: 'var(--ai-qtitle-color)',
      'overflow-wrap': 'anywhere',
    })
    expect(declarations(styles, '.ai-search-qtitle')).not.toHaveProperty('text-overflow')
    expect(declarations(styles, '.ai-search-qtitle')).toHaveProperty('margin-bottom', 'var(--ai-qtitle-mb)')
    expect(layoutCss).not.toMatch(/ai-search-(?:qhead|again)|--ai-(?:qhead-gap|again-size|again-color)/)
    expect(declarations(styles, '.ai-search-form')).toHaveProperty('width', '100%')
    expect(declarations(styles, '.ai-search-form')).not.toHaveProperty('margin-left')
  })

  it.each(['', 'stable both-edges'])('restores the original gutter "%s" on narrow screens and route unmount', (value) => {
    const html = document.documentElement
    const priority = value ? 'important' : ''
    html.style.setProperty('scrollbar-gutter', value, priority)
    const { unmount } = render(<AiSearchPage />)
    expect(html.style.scrollbarGutter).toBe('auto')
    act(() => { desktop = false; mediaChange?.() })
    expect(html.style.scrollbarGutter).toBe(value)
    expect(html.style.getPropertyPriority('scrollbar-gutter')).toBe(priority)
    act(() => { desktop = true; mediaChange?.() })
    expect(html.style.scrollbarGutter).toBe('auto')
    unmount()
    expect(html.style.scrollbarGutter).toBe(value)
    expect(html.style.getPropertyPriority('scrollbar-gutter')).toBe(priority)
  })

  it('locks only while desktop is mounted, preserves original value and priority, and restores on resize', () => {
    document.documentElement.style.setProperty('overflow', 'auto', 'important')
    const { unmount } = render(<AiSearchPage />)
    expect(document.documentElement.style.overflow).toBe('hidden')
    act(() => { desktop = false; mediaChange?.() })
    expect(document.documentElement.style.overflow).toBe('auto')
    expect(document.documentElement.style.getPropertyPriority('overflow')).toBe('important')
    act(() => { desktop = true; mediaChange?.() })
    expect(document.documentElement.style.overflow).toBe('hidden')
    unmount()
    expect(document.documentElement.style.overflow).toBe('auto')
  })

  it('does not restore a stale lock when a detail modal outlives this route', () => {
    function ModalLock() { useBodyScrollLock(true); return null }
    const page = render(<AiSearchPage />)
    const modal = render(<ModalLock />)
    page.unmount()
    expect(document.documentElement.style.overflow).toBe('hidden')
    modal.unmount()
    expect(document.documentElement.style.overflow).toBe('')
  })

  it.each([false, true])('shares the desktop lock with the real reading shell without adding padding (close page first=%s)', (closePageFirst) => {
    const html = document.documentElement
    html.style.setProperty('scrollbar-gutter', 'stable', 'important')
    const page = render(<AiSearchPage />)
    const modal = render(
      <ReadingModalShell ariaLabelledby="reading-title" isClosing={false} onClose={() => {}} panelTestId="reading-panel">
        <h2 id="reading-title">阅读详情</h2>
      </ReadingModalShell>,
    )
    expect(html.style.overflow).toBe('hidden')
    expect(html.style.paddingRight).toBe('')
    expect(html.style.scrollbarGutter).toBe('auto')
    const firstToClose = closePageFirst ? page : modal
    const lastToClose = closePageFirst ? modal : page
    firstToClose.unmount()
    expect(html.style.overflow).toBe('hidden')
    expect(html.style.paddingRight).toBe('')
    lastToClose.unmount()
    expect(html.style.overflow).toBe('')
    expect(html.style.paddingRight).toBe('')
    expect(html.style.scrollbarGutter).toBe('stable')
    expect(html.style.getPropertyPriority('scrollbar-gutter')).toBe('important')
  })

  it('routes header, title, article, and blank-space wheels only to the article', () => {
    render(<AiSearchPage />)
    const { pane, cards } = results()
    for (const target of [screen.getByTestId('topbar'), screen.getByRole('heading', { level: 1 }), pane, document.querySelector('main')!, document.body]) {
      expect(wheel(target).defaultPrevented).toBe(true)
    }
    expect(pane.scrollTop).toBe(400)
    expect(cards.scrollTop).toBe(0)
    expect(wheel(cards).defaultPrevented).toBe(false)
    expect(pane.scrollTop).toBe(400)
  })

  it('normalizes line/page wheel deltas and leaves zoom and portalled dialogs alone', () => {
    render(<AiSearchPage />)
    const { pane } = results()
    pane.style.lineHeight = '24px'
    Object.defineProperty(pane, 'clientHeight', { value: 400 })
    wheel(screen.getByTestId('topbar'), 2, { deltaMode: WheelEvent.DOM_DELTA_LINE })
    wheel(screen.getByRole('heading', { level: 1 }), 1, { deltaMode: WheelEvent.DOM_DELTA_PAGE })
    expect(pane.scrollTop).toBe(448)
    expect(wheel(pane, 100, { ctrlKey: true }).defaultPrevented).toBe(false)
    const modal = document.createElement('div')
    modal.setAttribute('role', 'dialog')
    document.body.append(modal)
    expect(wheel(modal).defaultPrevented).toBe(false)
    expect(pane.scrollTop).toBe(448)
    modal.remove()
  })

  it('keeps the vector notice above the scroll pane and empty results left in the document', () => {
    render(<AiSearchPage />)
    act(() => useAiSearchStore.setState({ phase: 'results', emptyResult: true, vectorOk: false }))
    const notice = screen.getByText('向量检索失败，仅按关键词匹配')
    expect(notice.closest('.ai-search-controls')).not.toBeNull()
    expect(notice.closest('[data-ai-scroll]')).toBeNull()
    const pane = document.querySelector<HTMLElement>('[data-ai-scroll="summary"]')!
    expect(pane).toHaveTextContent('站内近 90 天没有找到相关内容，换个说法试试。')
    expect(within(pane).getAllByRole('button').map(button => button.textContent)).toEqual(['AI 搜索'])
    expect(screen.queryByLabelText('引用内容')).not.toBeInTheDocument()
    expect(document.querySelector('main')).toHaveAttribute('data-columns', 'false')
  })

  it('scrolls just the focused container with its accessibility margin', () => {
    render(<AiSearchPage />)
    const { pane, cards } = results()
    for (const container of [pane, cards]) {
      Object.defineProperty(container, 'clientHeight', { value: 400 })
      vi.spyOn(container, 'getBoundingClientRect').mockReturnValue({ top: 100 } as DOMRect)
      const button = within(container).getByRole('button', { name: container === pane ? '查看引用 1' : '[1] RSS 来源一' })
      button.style.scrollMarginTop = '16px'
      button.style.scrollMarginBottom = '16px'
      const bounds = vi.spyOn(button, 'getBoundingClientRect').mockReturnValue({ top: 600, bottom: 630 } as DOMRect)
      fireEvent.focus(button)
      expect(container.scrollTop).toBe(146)
      bounds.mockReturnValue({ top: 80, bottom: 110 } as DOMRect)
      fireEvent.focus(button)
      expect(container.scrollTop).toBe(110)
      bounds.mockReturnValue({ top: 100, bottom: 130 } as DOMRect)
      fireEvent.focus(button)
      expect(container.scrollTop).toBe(110)
    }
    expect(pane.scrollTop).toBe(110)
  })

  it('leaves narrow screens unlocked and native document wheel scrolling intact', () => {
    desktop = false
    vi.stubGlobal('innerWidth', 768)
    const html = document.documentElement
    html.style.setProperty('scrollbar-gutter', 'stable', 'important')
    const { unmount } = render(<AiSearchPage />)
    const { pane, cards } = results()
    expect(html.style.overflow).toBe('')
    expect(html.style.scrollbarGutter).toBe('stable')
    expect(html.style.getPropertyPriority('scrollbar-gutter')).toBe('important')
    expect(wheel(screen.getByTestId('topbar')).defaultPrevented).toBe(false)
    expect(pane.scrollTop).toBe(0)
    expect(cards.closest('.ai-search-left')).toBeNull()
    expect(cards.previousElementSibling).toHaveClass('ai-search-left')
    unmount()
    expect(html.style.scrollbarGutter).toBe('stable')
    expect(html.style.getPropertyPriority('scrollbar-gutter')).toBe('important')
  })

  it('removes the wheel interceptor when leaving the route', () => {
    const { unmount } = render(<AiSearchPage />)
    const { pane } = results()
    unmount()
    expect(wheel(document.body).defaultPrevented).toBe(false)
    expect(pane.scrollTop).toBe(0)
    expect(document.documentElement.style.overflow).toBe('')
  })

  it.each([false, true])('switches directly to the title without animation; reduced = %s', reduce => {
    reduced = reduce
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      const idle = document.querySelector('main')?.getAttribute('data-idle') === 'true'
      return { top: idle ? 250 : 84, left: idle ? 320 : 16 } as DOMRect
    })
    render(<AiSearchPage />)
    const controls = document.querySelector<HTMLElement>('.ai-search-controls')!
    controls.style.setProperty('--ai-search-move-ms', '350ms')
    const animate = vi.fn().mockReturnValue({ cancel: vi.fn() })
    controls.animate = animate
    results()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('测试问题')
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(animate).not.toHaveBeenCalled()
  })

  it('still replaces the input without animation after resizing the idle page', () => {
    let idleLeft = 320
    let idleTop = 250
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(() => {
      const idle = document.querySelector('main')?.getAttribute('data-idle') === 'true'
      return { top: idle ? idleTop : 84, left: idle ? idleLeft : 16 } as DOMRect
    })
    render(<AiSearchPage />)
    const controls = document.querySelector<HTMLElement>('.ai-search-controls')!
    controls.style.setProperty('--ai-search-move-ms', '200ms')
    const animate = vi.fn().mockReturnValue({ cancel: vi.fn() })
    controls.animate = animate
    idleLeft = 220
    idleTop = 180
    fireEvent.resize(window)
    results()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('测试问题')
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(animate).not.toHaveBeenCalled()
  })
})
