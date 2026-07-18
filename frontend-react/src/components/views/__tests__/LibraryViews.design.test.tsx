import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { fetchLibrary } from '../../../lib/api'
import type { FeedItem, LibraryEntry, LibraryResponse } from '../../../lib/types'
import { StarredView } from '../StarredView'
import { HistoryView } from '../HistoryView'

vi.mock('../../../lib/api', () => ({
  fetchLibrary: vi.fn(),
}))

vi.mock('../../feed/InfoCard', () => ({
  InfoCard: ({ item }: { item: FeedItem }) => <article data-testid="mock-info-card">{item.title}</article>,
}))

vi.mock('../../events/EventLibraryCard', () => ({
  EventLibraryCard: ({ entry }: { entry: LibraryEntry }) => <article data-testid="mock-event-card">{entry.id}</article>,
}))

const mockFetchLibrary = fetchLibrary as unknown as ReturnType<typeof vi.fn>

function itemEntry(id: string, platform: string): LibraryEntry {
  const now = new Date().toISOString()
  return {
    id,
    type: 'item',
    occurred_at: now,
    item: {
      id,
      platform,
      title: `${platform} item`,
      fetched_at: now,
      referenced_urls: [],
    } as FeedItem,
  }
}

function libraryResponse(view: 'history' | 'starred'): LibraryResponse {
  return {
    entries: [
      itemEntry(`${view}-twitter`, 'twitter'),
      itemEntry(`${view}-github`, 'github'),
    ],
    total: 2,
    offset: 0,
    limit: 100,
    view,
  }
}

function deferredLibraryResponse() {
  let resolve!: (value: LibraryResponse) => void
  const promise = new Promise<LibraryResponse>((res) => { resolve = res })
  return { promise, resolve }
}

describe('utility library pages design tokens', () => {
  beforeEach(() => {
    mockFetchLibrary.mockImplementation(({ view }: { view: 'history' | 'starred' }) =>
      Promise.resolve(libraryResponse(view)),
    )
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('加载态和完成态使用同一页面外壳 padding，避免标题纵向跳动', async () => {
    const starred = deferredLibraryResponse()
    mockFetchLibrary.mockReturnValue(starred.promise)

    const { unmount } = render(<StarredView />)
    const starredTitle = screen.getByRole('heading', { name: '我的收藏' })
    expect(starredTitle.parentElement?.parentElement).toHaveClass('px-4', 'py-4')

    starred.resolve(libraryResponse('starred'))
    await screen.findByTestId('starred-platform-filter')
    expect(starredTitle.parentElement?.parentElement).toHaveClass('px-4', 'py-4')

    unmount()
    const history = deferredLibraryResponse()
    mockFetchLibrary.mockReturnValue(history.promise)

    render(<HistoryView />)
    const historyTitle = screen.getByRole('heading', { name: '浏览历史' })
    expect(historyTitle.parentElement?.parentElement).toHaveClass('px-4', 'py-4')

    history.resolve(libraryResponse('history'))
    await screen.findByTestId('history-platform-filter')
    expect(historyTitle.parentElement?.parentElement).toHaveClass('px-4', 'py-4')
  })

  it('收藏页使用 section-local underline filter，不回到黑底圆 pill', async () => {
    render(<StarredView />)

    const filter = await screen.findByTestId('starred-platform-filter')
    expect(filter.className).toContain('border-b')
    expect(filter.className).toContain('bg-background')

    const all = screen.getByRole('button', { name: '全部' })
    expect(all.className).toContain('border-b-2')
    expect(all.className).toContain('border-[var(--brand)]')
    expect(all.className).toContain('font-event-title')  // v24.2: 同 pill 对齐 topbar
    expect(all.className).not.toContain('rounded-full')
    expect(all.className).not.toContain('bg-foreground')

    const sectionHeading = screen.getByRole('heading', { name: '今天' })
    expect(sectionHeading.className).toContain('font-display')
    expect(sectionHeading.className).toContain('text-[22px]')
  })

  it('浏览历史页的 filter 与日期 section 跟收藏页保持同款 token', async () => {
    const user = userEvent.setup()
    render(<HistoryView />)

    const filter = await screen.findByTestId('history-platform-filter')
    expect(filter.className).toContain('border-b')

    const github = screen.getByRole('button', { name: 'GitHub' })
    expect(github.className).toContain('border-transparent')
    await user.click(github)
    expect(github.className).toContain('border-[var(--brand)]')
    expect(github.className).toContain('text-[var(--brand)]')

    const sectionHeading = screen.getByRole('heading', { name: '今天' })
    expect(sectionHeading.className).toContain('font-display')
    expect(screen.getByText('1 条')).toHaveClass('font-body-cjk', 'text-[13px]')
  })
})
