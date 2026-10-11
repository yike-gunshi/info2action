import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CitationCard } from '../CitationCard'
import { useDetailStore } from '../../../store/detailStore'
import { useFeedStore } from '../../../store/feedStore'
import type { AiSearchItem } from '../../../lib/api/aiSearch'

const { fetchFeedItem } = vi.hoisted(() => ({ fetchFeedItem: vi.fn() }))
vi.mock('../../../lib/api', () => ({
  fetchFeedItem,
  fetchFeedItemsBundle: vi.fn(),
  translateItemAsr: vi.fn(),
  triggerItemAsr: vi.fn(),
}))

const item: AiSearchItem = {
  id: 'source-1', title: '紧凑卡展示两行标题', platform: 'wechat_mp',
  author_name: '公众号 · 硅星人Pro', published_at: '2026-09-08T12:00:00Z', fetched_at: '2026-09-20T12:00:00Z',
  ai_summary: '这段摘要不应该出现在引用卡中', cover_url: 'https://example.com/cover.jpg',
}

describe('CitationCard', () => {
  it('includes the citation number, source and title in its accessible name', () => {
    render(<CitationCard item={item} number={30} />)
    expect(screen.getByRole('button')).toHaveAccessibleName('[30] 公众号 · 硅星人Pro 紧凑卡展示两行标题')
  })
  beforeEach(() => {
    useDetailStore.getState().closeModal()
    useDetailStore.setState({ detailCache: new Map() })
    useFeedStore.setState({ clickedAtById: {} })
    fetchFeedItem.mockReset().mockResolvedValue(item)
    window.history.replaceState(null, '', '#ai-search?job=job-123')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders the citation at the start of the source line, a compact date and title without a summary', () => {
    const { container } = render(<CitationCard item={item} number={30} />)
    const source = container.querySelector('.ai-citation-card-source')!
    expect(source).toHaveTextContent('[30]公众号 · 硅星人Pro09/08')
    expect(source.querySelector('.ai-citation-card-origin')?.firstElementChild).toHaveTextContent('[30]')
    expect(screen.getByRole('button', { name: `[30] 公众号 · 硅星人Pro ${item.title}` })).toHaveTextContent(item.title)
    expect(screen.queryByText(item.ai_summary!)).not.toBeInTheDocument()
    expect(container.querySelector('img')).toHaveAttribute('loading', 'lazy')
  })

  it('opens and closes the existing detail modal without replacing the job hash', async () => {
    render(<CitationCard item={item} number={1} />)
    await userEvent.click(screen.getByRole('button', { name: `[1] 公众号 · 硅星人Pro ${item.title}` }))
    expect(useDetailStore.getState().modalStack).toEqual([{ type: 'item', id: item.id }])
    expect(fetchFeedItem).toHaveBeenCalledWith(item.id)
    expect(window.location.hash).toBe('#ai-search?job=job-123')
    act(() => useDetailStore.getState().closeModal())
    expect(useDetailStore.getState().modalStack).toEqual([])
    expect(window.location.hash).toBe('#ai-search?job=job-123')
  })

  it('supports keyboard opening through the native card button', async () => {
    const user = userEvent.setup()
    render(<CitationCard item={item} number={1} />)
    await user.tab()
    expect(screen.getByRole('button', { name: `[1] 公众号 · 硅星人Pro ${item.title}` })).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(useDetailStore.getState().modalStack).toEqual([{ type: 'item', id: item.id }])
  })

  it('reflects both server and locally observed read state without dimming the entire card', () => {
    const { rerender } = render(<CitationCard item={item} number={1} />)
    const card = screen.getByRole('button', { name: `[1] 公众号 · 硅星人Pro ${item.title}` })
    expect(card).toHaveAttribute('data-read', 'false')
    act(() => useFeedStore.getState().markClicked(item.id))
    expect(card).toHaveAttribute('data-read', 'true')
    act(() => useFeedStore.setState({ clickedAtById: {} }))
    rerender(<CitationCard item={{ ...item, clicked_at: '2026-09-20T12:00:00Z' }} number={1} />)
    expect(card).toHaveAttribute('data-read', 'true')
  })

  it('leaves no thumbnail placeholder for absent or failed images and retries a changed source', () => {
    const { container, rerender } = render(<CitationCard item={{ ...item, cover_url: undefined }} number={1} />)
    expect(container.querySelector('img')).not.toBeInTheDocument()
    expect(container.querySelector('.ai-citation-card-thumbnail')).not.toBeInTheDocument()
    rerender(<CitationCard item={item} number={1} />)
    fireEvent.error(container.querySelector('img')!)
    expect(container.querySelector('.ai-citation-card-thumbnail')).not.toBeInTheDocument()
    rerender(<CitationCard item={{ ...item, cover_url: 'https://example.com/replacement.jpg' }} number={1} />)
    expect(container.querySelector('img')).toHaveAttribute('src', 'https://example.com/replacement.jpg')
  })

  it.each([
    { cover_url: undefined, thumbnail: 'https://example.com/thumbnail.jpg', media_json: ['https://example.com/media.jpg'], expected: 'https://example.com/thumbnail.jpg' },
    { cover_url: undefined, thumbnail: undefined, media_json: ['https://example.com/media.jpg'], expected: 'https://example.com/media.jpg' },
    { cover_url: undefined, thumbnail: undefined, media_json: [{ url: 'https://example.com/media-object.jpg' }], expected: 'https://example.com/media-object.jpg' },
    { platform: 'twitter', cover_url: 'https://pbs.twimg.com/photo.jpg', expected: '/api/media/twitter-photo/source-1/0.jpg' },
    { platform: 'twitter', media_json: [{ type: 'video', url: 'https://example.com/video.mp4' }], expected: '/api/media/twitter-poster/source-1.jpg' },
  ])('preserves the usable image source fallback: $expected', ({ expected, ...overrides }) => {
    const { container } = render(<CitationCard item={{ ...item, ...overrides } as AiSearchItem} number={1} />)
    expect(container.querySelector('img')).toHaveAttribute('src', expected)
  })

  it('never attempts to display a non-Twitter video URL as a thumbnail', () => {
    const videoMedia = [{ type: 'video', url: 'https://example.com/video.mp4' }]
    const { container } = render(<CitationCard item={{ ...item, cover_url: undefined, media_json: videoMedia }} number={1} />)
    expect(container.querySelector('img')).not.toBeInTheDocument()
  })

  it('uses fetched time when published time is absent and handles unavailable dates', () => {
    const { container, rerender } = render(<CitationCard item={{ ...item, published_at: undefined }} number={1} />)
    expect(container.querySelector('time')).toHaveTextContent('09/20')
    rerender(<CitationCard item={{ ...item, published_at: 'invalid-date' }} number={1} />)
    expect(container.querySelector('time')).toHaveTextContent('—')
  })
})
