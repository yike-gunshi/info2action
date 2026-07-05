import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'

import { ItemLeftPanel } from '../ItemLeftPanel'
import type { FeedItem } from '../../../lib/types'

// Mock heavy children — we only care about ItemLeftPanel composition.
vi.mock('../../detail/VideoPlayer', () => ({
  VideoPlayer: ({ mp4Url, itemId }: { mp4Url: string; itemId: string }) => (
    <div data-testid="video-player" data-mp4={mp4Url} data-item-id={itemId} />
  ),
}))
vi.mock('../../detail/YoutubePlayer', () => ({
  YoutubePlayer: ({ videoId, itemId }: { videoId: string; itemId: string }) => (
    <div data-testid="youtube-player" data-video-id={videoId} data-item-id={itemId} />
  ),
}))
vi.mock('../../detail/TranscriptPanel', () => ({
  TranscriptPanel: ({ itemId }: { itemId: string }) => (
    <div data-testid="transcript-panel" data-item-id={itemId} />
  ),
}))

function makeItem(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'tw_123',
    title: '默认标题',
    platform: 'twitter',
    fetched_at: '2026-04-19T10:00:00Z',
    ...overrides,
  }
}

describe('ItemLeftPanel', () => {
  afterEach(() => cleanup())

  it('渲染 item.title 为 <h1>', () => {
    render(<ItemLeftPanel item={makeItem({ title: 'Hello World 这是一个测试标题' })} />)
    const heading = screen.getByRole('heading', { level: 1 })
    expect(heading).toHaveTextContent('Hello World 这是一个测试标题')
  })

  it('showHeader=false 时不重复渲染标题和作者头部,只保留正文阅读区', () => {
    render(
      <ItemLeftPanel
        item={makeItem({
          title: '吸顶栏已经承载的标题',
          author_name: '培风客',
          content: '正文从这里开始。',
        })}
        showHeader={false}
      />,
    )

    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument()
    expect(screen.queryByText('培风客')).not.toBeInTheDocument()
    const bodyText = screen.getByText('正文从这里开始。')
    expect(bodyText).toBeInTheDocument()
    expect(bodyText.closest('div')).toHaveClass('text-[16px]', 'text-foreground')
  })

  it('plain surface 使用事件详情页阅读字体,用于和右侧 AI 总结对齐', () => {
    render(
      <ItemLeftPanel
        item={makeItem({
          title: '不应重复出现的标题',
          content: '原文阅读区使用杂志风正文。',
        })}
        showHeader={false}
        surface="plain"
        truncateContent={false}
      />,
    )

    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument()
    expect(screen.getByTestId('item-left-body-text')).toHaveClass(
      'font-event-title',
      'text-[16px]',
      'leading-[1.82]',
      'tracking-[0]',
      'text-[#3F3A34]',
    )
  })

  it('media_json 含 video 类型时渲染 VideoPlayer,不渲染 image', () => {
    const item = makeItem({
      media_json: [{ type: 'video', url: 'https://video.twimg.com/ext/foo.mp4' } as unknown as { url?: string }],
      cover_url: 'https://example.com/poster.jpg',
    })
    render(<ItemLeftPanel item={item} />)
    const video = screen.getByTestId('video-player')
    expect(video).toBeInTheDocument()
    expect(video.getAttribute('data-mp4')).toBe('https://video.twimg.com/ext/foo.mp4')
    expect(video.getAttribute('data-item-id')).toBe('tw_123')
    // 视频帖不应同时渲染图片
    expect(document.querySelectorAll('img').length).toBe(0)
    // Twitter + 视频 → 渲染 TranscriptPanel
    expect(screen.getByTestId('transcript-panel')).toBeInTheDocument()
  })

  it('纯文本 item 不渲染任何视频或图片容器,也不渲染 TranscriptPanel', () => {
    const item = makeItem({
      content: '这是一段纯文本的推文,没有图也没有视频。',
      // 不给 media_json / cover_url / thumbnail
    })
    render(<ItemLeftPanel item={item} />)

    expect(screen.queryByTestId('video-player')).not.toBeInTheDocument()
    expect(screen.queryByTestId('youtube-player')).not.toBeInTheDocument()
    expect(screen.queryByTestId('transcript-panel')).not.toBeInTheDocument()
    // DOM 中不应出现 <img> 或 <video>
    expect(document.querySelectorAll('img').length).toBe(0)
    expect(document.querySelectorAll('video').length).toBe(0)
    // 正文仍被渲染
    const bodyText = screen.getByText(/这是一段纯文本的推文/)
    expect(bodyText).toBeInTheDocument()
    expect(bodyText.closest('div')).toHaveClass('text-[16px]', 'text-foreground')
  })

  it('YouTube 平台渲染 YoutubePlayer 并显示 TranscriptPanel', () => {
    const item = makeItem({
      id: 'yt_abc123XYZ',
      platform: 'youtube',
      title: 'YouTube 视频标题',
    })
    render(<ItemLeftPanel item={item} />)
    const yt = screen.getByTestId('youtube-player')
    expect(yt.getAttribute('data-video-id')).toBe('abc123XYZ')
    expect(screen.getByTestId('transcript-panel')).toBeInTheDocument()
    // 不应该同时渲染 VideoPlayer
    expect(screen.queryByTestId('video-player')).not.toBeInTheDocument()
  })
})
