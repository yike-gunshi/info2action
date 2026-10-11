import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { CitationChip } from '../CitationChip'
import { useAiSearchStore } from '../../../store/aiSearchStore'
import type { AiSearchItem } from '../../../lib/api/aiSearch'

const highlight = vi.fn()
const originalHighlight = useAiSearchStore.getState().highlight
let desktop = true
let reduced = false

beforeEach(() => {
  desktop = true
  reduced = false
  highlight.mockReset()
  useAiSearchStore.getState().reset()
  useAiSearchStore.setState({ items: [{ id: 'source' }] as AiSearchItem[], highlight })
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({ matches: query.includes('reduce') ? reduced : desktop })))
  vi.stubGlobal('scrollTo', vi.fn())
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ top: 100, bottom: 500, height: 400 } as DOMRect)
})
afterEach(() => {
  cleanup()
  useAiSearchStore.setState({ highlight: originalHighlight })
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

function fixture(top = 800, height = 100) {
  render(<>
    <div data-testid="topbar" />
    <div data-testid="article"><CitationChip n={1} /></div>
    <div data-ai-scroll="cards" data-testid="cards"><div id="ai-card-source" /></div>
  </>)
  const article = screen.getByTestId('article')
  const container = screen.getByTestId('cards')
  const card = document.getElementById('ai-card-source')!
  article.scrollTop = 245
  container.scrollTop = 200
  Object.defineProperties(container, { clientHeight: { value: 400 }, scrollHeight: { value: 2000 } })
  vi.spyOn(card, 'getBoundingClientRect').mockReturnValue({ top, bottom: top + height, height } as DOMRect)
  card.style.setProperty('--ai-highlight-ms', '2300ms')
  Object.defineProperty(container, 'scrollTo', { value: vi.fn() })
  return { article, container }
}

describe('CitationChip container navigation', () => {
  describe.each(['scrollend', 'timeout'])('delayed focus after %s', signal => {
    it.each(['dialog', 'dialog-body', 'elsewhere'])('does not reclaim focus after the user opens a dialog or moves elsewhere: %s', destination => {
      vi.useFakeTimers()
      const { container } = fixture()
      const card = document.getElementById('ai-card-source')!
      card.tabIndex = 0
      const focus = vi.spyOn(card, 'focus')
      const chip = screen.getByRole('button', { name: '查看引用 1' })
      chip.focus()
      fireEvent.click(chip)
      const other = render(destination === 'elsewhere'
        ? <button>其他操作</button>
        : <div role="dialog" aria-label="原文弹窗"><button>弹窗操作</button></div>)
      const destinationButton = screen.getByRole('button', { name: destination === 'elsewhere' ? '其他操作' : '弹窗操作' })
      destinationButton.focus()
      if (destination === 'dialog-body') destinationButton.blur()
      const expectedFocus = document.activeElement
      if (signal === 'scrollend') fireEvent(container, new Event('scrollend'))
      else act(() => vi.advanceTimersByTime(800))
      expect(focus).not.toHaveBeenCalled()
      expect(document.activeElement).toBe(expectedFocus)
      expect(highlight).toHaveBeenCalledOnce()
      other.unmount()
    })
  })

  it('does not focus behind an open dialog even if the citation still owns focus', () => {
    vi.useFakeTimers()
    const { container } = fixture()
    const card = document.getElementById('ai-card-source')!
    const focus = vi.spyOn(card, 'focus')
    const chip = screen.getByRole('button', { name: '查看引用 1' })
    chip.focus()
    fireEvent.click(chip)
    render(<div role="dialog" aria-label="原文弹窗" />)
    fireEvent(container, new Event('scrollend'))
    expect(focus).not.toHaveBeenCalled()
    expect(chip).toHaveFocus()
  })

  it('still focuses the referenced card when the body owns focus and no dialog is open', () => {
    const { container } = fixture()
    const card = document.getElementById('ai-card-source')!
    card.tabIndex = 0
    const focus = vi.spyOn(card, 'focus')
    const chip = screen.getByRole('button', { name: '查看引用 1' })
    chip.focus()
    fireEvent.click(chip)
    chip.blur()
    expect(document.body).toHaveFocus()
    fireEvent(container, new Event('scrollend'))
    expect(focus).toHaveBeenCalledWith({ preventScroll: true })
    expect(card).toHaveFocus()
  })
  it.each(['scrollend', 'timeout'])('focuses the referenced card with preventScroll only after settling: %s', signal => {
    vi.useFakeTimers()
    const { article, container } = fixture()
    const card = document.getElementById('ai-card-source')!
    card.tabIndex = 0
    const focus = vi.spyOn(card, 'focus')
    const chip = screen.getByRole('button', { name: '查看引用 1' })
    chip.focus()
    fireEvent.click(chip)
    expect(chip).toHaveFocus()
    expect(focus).not.toHaveBeenCalled()
    if (signal === 'scrollend') fireEvent(container, new Event('scrollend'))
    else act(() => vi.advanceTimersByTime(800))
    expect(focus).toHaveBeenCalledOnce()
    expect(focus).toHaveBeenCalledWith({ preventScroll: true })
    expect(card).toHaveFocus()
    expect(article.scrollTop).toBe(245)
    expect(window.scrollTo).not.toHaveBeenCalled()
    act(() => vi.advanceTimersByTime(800))
    expect(focus).toHaveBeenCalledOnce()
  })
  it.each([false, true])('centers within only the citation container; reduced motion = %s', async value => {
    reduced = value
    const { article, container } = fixture()
    fireEvent.click(screen.getByRole('button', { name: '查看引用 1' }))
    expect(container.scrollTo).toHaveBeenCalledOnce()
    expect(container.scrollTo).toHaveBeenCalledWith({ top: 750, behavior: value ? 'auto' : 'smooth' })
    expect(article.scrollTop).toBe(245)
    expect(window.scrollTo).not.toHaveBeenCalled()
    // 脉冲改为等滚动停稳再触发：卡片没到位就闪，等它到位时脉冲已经结束了
    //（用户 2026-09-22 反馈）。jsdom 不派发 scrollend，这里走超时兜底那条路。
    await waitFor(() => expect(highlight).toHaveBeenCalledWith('source', 2300), { timeout: 1200 })
  })

  it('still centres a card that is already fully visible', async () => {
    // 已可见也要滚到居中：不滚会让点击看起来毫无反应，读者无从判断引用是否已定位
    //（用户 2026-09-22 反馈）。
    const { container } = fixture(150)
    fireEvent.click(screen.getByRole('button', { name: '查看引用 1' }))
    expect(container.scrollTo).toHaveBeenCalled()
    await waitFor(() => expect(highlight).toHaveBeenCalledWith('source', 2300), { timeout: 1200 })
  })

  it.each([[0, 0], [3000, 1600]])('clamps a distant card at %s to container bounds', (top, expected) => {
    const { container } = fixture(top)
    fireEvent.click(screen.getByRole('button', { name: '查看引用 1' }))
    expect(container.scrollTo).toHaveBeenCalledWith({ top: expected, behavior: 'smooth' })
  })

  it('uses document scrolling only in the narrow-screen fallback', () => {
    desktop = false
    const { article, container } = fixture()
    vi.spyOn(screen.getByTestId('topbar'), 'getBoundingClientRect').mockReturnValue({ bottom: 84 } as DOMRect)
    vi.stubGlobal('innerHeight', 800)
    vi.stubGlobal('scrollY', 50)
    fireEvent.click(screen.getByRole('button', { name: '查看引用 1' }))
    expect(container.scrollTo).not.toHaveBeenCalled()
    expect(window.scrollTo).toHaveBeenCalledWith({ top: 458, behavior: 'smooth' })
    expect(article.scrollTop).toBe(245)
  })

  it('leaves missing sources inert', () => {
    render(<CitationChip n={9} />)
    expect(screen.getByText('[9]')).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})
