import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { CardsPane } from '../CardsPane'
import { useAiSearchStore } from '../../../store/aiSearchStore'
import type { AiSearchItem } from '../../../lib/api/aiSearch'

const { isDesktop, keepFocusVisible } = vi.hoisted(() => ({ isDesktop: vi.fn(() => true), keepFocusVisible: vi.fn() }))
vi.mock('../aiSearchScroll', async (importOriginal) => ({
  ...await importOriginal<typeof import('../aiSearchScroll')>(),
  isDesktopAiSearch: isDesktop,
  keepFocusVisible,
}))

const items = ['First card', 'Second card', 'Third card'].map((title, index) => ({
  id: ['a', 'b', 'c'][index], title, platform: 'twitter', fetched_at: '2026-09-20T12:00:00Z',
})) as AiSearchItem[]

function mockGeometry(container: HTMLElement) {
  const order = () => Array.from(container.querySelectorAll<HTMLElement>('[data-ai-card-id]'))
  // The container also moves during the commit: viewport coordinates alone
  // would overcompensate by this movement.
  const top = () => order()[0].dataset.aiCardId === 'c' ? 120 : 100
  Object.defineProperties(container, {
    clientHeight: { configurable: true, value: 300 },
    clientTop: { configurable: true, value: 2 },
  })
  container.scrollTop = 100
  vi.spyOn(container, 'getBoundingClientRect').mockImplementation(() => ({ top: top(), bottom: top() + 304 }) as DOMRect)
  for (const card of order()) {
    vi.spyOn(card, 'getBoundingClientRect').mockImplementation(() => {
      const cardTop = top() + container.clientTop + order().indexOf(card) * 200 - container.scrollTop
      return { top: cardTop, bottom: cardTop + 80 } as DOMRect
    })
  }
}

describe('CardsPane', () => {
  it('exposes the labelled citations container as a region', () => {
    render(<CardsPane />)
    expect(screen.getByRole('region', { name: '引用内容' })).toHaveAttribute('data-ai-scroll', 'cards')
  })
  beforeEach(() => {
    isDesktop.mockReturnValue(true)
    keepFocusVisible.mockClear()
    useAiSearchStore.getState().reset()
    useAiSearchStore.setState({ items, cardOrder: ['a', 'b', 'c'] })
    vi.stubGlobal('scrollBy', vi.fn())
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('adopts API citation numbers while compensating the visible anchor on completion', () => {
    render(<CardsPane />)
    const container = screen.getByLabelText('引用内容')
    mockGeometry(container)
    const anchor = document.getElementById('ai-card-b')!
    const relativeTop = () => anchor.getBoundingClientRect().top - container.getBoundingClientRect().top - container.clientTop
    expect(relativeTop()).toBe(100)
    act(() => useAiSearchStore.setState({ items: [items[2], items[0], items[1]], cardOrder: ['c', 'a', 'b'] }))
    expect(screen.getAllByRole('button').map((element) => element.getAttribute('aria-label'))).toEqual(['[1] X Third card', '[2] X First card', '[3] X Second card'])
    expect(document.getElementById('ai-card-c')).toHaveTextContent('[1]')
    expect(document.getElementById('ai-card-a')).toHaveTextContent('[2]')
    expect(document.getElementById('ai-card-b')).toHaveTextContent('[3]')
    expect(container.scrollTop).toBe(300)
    expect(relativeTop()).toBe(100)
    expect(window.scrollBy).not.toHaveBeenCalled()
  })

  it('highlights only the selected card and removes its highlight after 1.5 seconds', () => {
    vi.useFakeTimers()
    render(<CardsPane />)
    act(() => useAiSearchStore.getState().highlight('b'))
    expect(document.getElementById('ai-card-b')).toHaveClass('ai-citation-card-highlighted')
    expect(document.getElementById('ai-card-a')).not.toHaveClass('ai-citation-card-highlighted')
    act(() => vi.advanceTimersByTime(1500))
    expect(document.getElementById('ai-card-b')).not.toHaveClass('ai-citation-card-highlighted')
  })

  it('ignores a card inside the viewport but above the visible container boundary', () => {
    render(<CardsPane />)
    const container = screen.getByLabelText('引用内容')
    mockGeometry(container)
    // Card a is at 2..82, inside the window but above this container's top (102).
    // Its new position changes by 400; the actually visible card b stays put.
    act(() => useAiSearchStore.setState({ cardOrder: ['c', 'b', 'a'] }))
    expect(container.scrollTop).toBe(100)
    expect(window.scrollBy).not.toHaveBeenCalled()
  })

  it('does not shift scrolling for a different result set', () => {
    render(<CardsPane />)
    const container = screen.getByLabelText('引用内容')
    mockGeometry(container)
    act(() => useAiSearchStore.setState({ items: [{ ...items[0], id: 'new', title: 'New query' }], cardOrder: ['new'] }))
    expect(container.scrollTop).toBe(100)
    expect(window.scrollBy).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: '[1] X New query' })).toBeInTheDocument()
  })

  it('does not anchor another job that happens to return the same candidate IDs', () => {
    useAiSearchStore.setState({ jobId: 'old-job' })
    render(<CardsPane />)
    const container = screen.getByLabelText('引用内容')
    mockGeometry(container)
    act(() => useAiSearchStore.setState({ jobId: 'new-job', cardOrder: ['c', 'a', 'b'] }))
    expect(container.scrollTop).toBe(100)
    expect(window.scrollBy).not.toHaveBeenCalled()
  })

  it('discards a pending anchor if another job arrives in the same render batch', () => {
    useAiSearchStore.setState({ jobId: 'old-job' })
    render(<CardsPane />)
    const container = screen.getByLabelText('引用内容')
    mockGeometry(container)
    act(() => {
      useAiSearchStore.setState({ cardOrder: ['c', 'a', 'b'] })
      useAiSearchStore.setState({ jobId: 'new-job', cardOrder: ['c', 'b', 'a'] })
    })
    expect(container.scrollTop).toBe(100)
    expect(window.scrollBy).not.toHaveBeenCalled()
  })

  it('keeps the narrow layout in normal page flow without reorder compensation', () => {
    isDesktop.mockReturnValue(false)
    render(<CardsPane />)
    const container = screen.getByLabelText('引用内容')
    mockGeometry(container)
    expect(container).toHaveClass('ai-search-cards')
    expect(container).toHaveAttribute('data-ai-scroll', 'cards')
    act(() => useAiSearchStore.setState({ cardOrder: ['c', 'a', 'b'] }))
    expect(container.scrollTop).toBe(100)
    expect(window.scrollBy).not.toHaveBeenCalled()
  })
})
