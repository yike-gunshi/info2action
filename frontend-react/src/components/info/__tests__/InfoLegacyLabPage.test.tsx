import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { fetchClassification, fetchFeedPlatforms } from '../../../lib/api'
import { InfoLegacyLabPage } from '../InfoLegacyLabPage'
import type { FeedItem } from '../../../lib/types'

vi.mock('../../../lib/api', () => ({
  fetchClassification: vi.fn(),
  fetchFeedPlatforms: vi.fn(),
  fetchFeedPlatformMore: vi.fn(),
  fetchLingowhaleGroups: vi.fn(),
}))

vi.mock('../../layout/TopBar', () => ({
  TopBar: ({ activeL1 }: { activeL1?: string | null }) => (
    <header data-testid="legacy-topbar">{activeL1}</header>
  ),
}))

function legacyItem(id: string, title: string, platform = 'github'): FeedItem {
  return {
    id,
    title,
    platform,
    fetched_at: '2026-05-18T10:00:00Z',
    published_at: '2026-05-18T09:00:00Z',
    author_name: platform === 'github' ? 'tinyhumansai' : 'lukaspetersson',
    ai_summary: `${title} 的旧版信息流摘要，用于验证早期卡片结构。`,
    ranking_score: 86,
  }
}

describe('InfoLegacyLabPage', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    })
    vi.mocked(fetchFeedPlatforms).mockResolvedValue({
      sections: {
        github: [
          legacyItem('1', 'tinyhumansai/openhuman'),
          legacyItem('2', 'draw-ui screenshot to code toolkit'),
        ],
      },
      platform_counts: { github: 2 },
      source_counts: { github: { repo: 2 } },
      category_counts: { github: { products: 2 } },
    })
    vi.mocked(fetchClassification).mockResolvedValue({
      categories: [
        { id: 'products', name: '产品', priority: 1, visible: true },
      ],
    })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('渲染 2026-05-18 频道页结构作为独立对照页', async () => {
    render(<InfoLegacyLabPage />)

    expect(screen.getByTestId('info-legacy-lab-page')).toBeInTheDocument()
    expect(screen.getByTestId('legacy-topbar')).toHaveTextContent('info')
    expect(screen.getByTestId('info-legacy-lab-channel-shell')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '来源' })).toHaveAttribute('aria-pressed', 'true')

    expect(await screen.findAllByText('tinyhumansai/openhuman')).not.toHaveLength(0)
    expect(screen.getAllByText('GitHub').length).toBeGreaterThan(0)
    expect(screen.getAllByTestId('info-card')).toHaveLength(2)

    expect(screen.queryByTestId('image2-lab-page')).not.toBeInTheDocument()
  })
})
