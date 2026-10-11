import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { ChangelogPage } from '../ChangelogPage'
import { useChangelog } from '../../hooks/useChangelog'

vi.mock('../../hooks/useChangelog', () => ({ useChangelog: vi.fn() }))
vi.mock('../../components/layout/TopBar', () => ({ TopBar: () => <header data-testid="topbar" /> }))

beforeEach(() => {
  vi.stubGlobal('scrollY', 0)
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
})
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('changelog content presentation', () => {
  it('renders an empty state without recording a view', () => {
    const markSeen = vi.fn()
    vi.mocked(useChangelog).mockReturnValue({ releases: [], hasUnread: false, markSeen })
    render(<ChangelogPage />)
    expect(screen.getByText('还没有更新日志')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '返回精选' })).toHaveAttribute('href', '#v=highlights')
    expect(markSeen).not.toHaveBeenCalled()
  })

  it('shows readable release content, escapes text and only renders internal action links', () => {
    const markSeen = vi.fn()
    vi.mocked(useChangelog).mockReturnValue({ hasUnread: true, markSeen, releases: [{
      id: 'release', publishedAt: '2026-09-21T00:00:00Z', title: '近期改进', changes: [
        { kind: 'feature', title: '发现新内容', description: '<script>unsafe()</script>', action: { label: '查看信息', href: '#v=info' } },
        { kind: 'fix', title: '阅读修复', description: '体验更稳定', action: { label: '不安全链接', href: 'javascript:alert(1)' } },
      ],
    }] })
    const { container } = render(<ChangelogPage />)
    expect(screen.getByRole('heading', { name: '近期改进' })).toBeInTheDocument()
    expect(screen.getByText('新功能')).toBeInTheDocument()
    expect(screen.getByText('问题修复')).toBeInTheDocument()
    expect(screen.getByText('<script>unsafe()</script>')).toBeInTheDocument()
    expect(container.querySelector('script')).toBeNull()
    expect(screen.getByRole('link', { name: /查看信息/ })).toHaveAttribute('href', '#v=info')
    expect(screen.queryByRole('link', { name: '不安全链接' })).not.toBeInTheDocument()
    expect(container.querySelector('time')).toHaveAttribute('datetime', '2026-09-21T00:00:00Z')
    expect(markSeen).toHaveBeenCalledOnce()
  })

  it('renders the full date once per release and displays GIFs directly with reserved dimensions', () => {
    vi.mocked(useChangelog).mockReturnValue({ hasUnread: false, markSeen: vi.fn(), releases: [{
      id: 'media', publishedAt: '2026-07-06T00:00:00Z', title: '行动点', changes: [{
        kind: 'feature', title: '生成行动点', description: '从内容生成步骤。',
        media: [{ src: '/demo.gif', alt: '生成行动点演示', width: 2048, height: 1298 }],
      }],
    }] })
    const { container } = render(<ChangelogPage />)
    const row = container.querySelector('[data-release-row="media"]')!
    expect(row.querySelectorAll('time')).toHaveLength(1)
    expect(row.querySelector('time')).toHaveTextContent('2026.07.06')
    expect(row.querySelector('article')?.textContent).not.toContain('2026.07.06')
    const gif = screen.getByRole('img', { name: '生成行动点演示' })
    expect(gif).toHaveAttribute('src', '/demo.gif')
    expect(gif).toHaveAttribute('width', '2048')
    expect(gif).toHaveAttribute('height', '1298')
    expect(gif).toHaveAttribute('loading', 'lazy')
    expect(container.querySelector('figcaption')).toBeNull()
    expect(screen.queryByRole('button', { name: '播放演示' })).not.toBeInTheDocument()
  })

  it('positions a selected date below both sticky headers, respects reduced motion and preserves the route', () => {
    vi.mocked(useChangelog).mockReturnValue({ hasUnread: false, markSeen: vi.fn(), releases: [{
      id: 'history', publishedAt: '2026-07-06T00:00:00Z', title: '历史更新',
      changes: [{ kind: 'feature', title: '回看', description: '内容' }],
    }] })
    vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: true }))
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      const height = this.dataset.testid === 'topbar' ? 84 : this.classList.contains('cl-heading-row') ? 66 : 40
      const top = this.dataset.releaseRow ? 500 : 0
      return { x: 0, y: top, top, bottom: top + height, left: 0, right: 390, width: 390, height, toJSON: () => ({}) }
    })
    const hash = window.location.hash
    const { container } = render(<ChangelogPage />)
    screen.getByTestId('changelog-page').style.setProperty('--cl-mobile-head-pad', '16px')
    const menu = container.querySelector('details')!
    menu.open = true
    fireEvent.click(menu.querySelector('button')!, { detail: 0 })
    expect(menu.open).toBe(false)
    expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 334, behavior: 'instant' })
    expect(screen.getByRole('heading', { name: '历史更新' })).toHaveFocus()
    expect(window.location.hash).toBe(hash)
  })

  it('closes the mobile date menu with Escape and restores focus to its trigger', () => {
    vi.mocked(useChangelog).mockReturnValue({ hasUnread: false, markSeen: vi.fn(), releases: [{
      id: 'one', publishedAt: '2026-09-21T00:00:00Z', title: '一次更新',
      changes: [{ kind: 'feature', title: '功能', description: '内容' }],
    }] })
    const { container } = render(<ChangelogPage />)
    const menu = container.querySelector('details')!
    menu.open = true
    fireEvent.keyDown(menu.querySelector('button')!, { key: 'Escape' })
    expect(menu.open).toBe(false)
    expect(menu.querySelector('summary')).toHaveFocus()
  })
})
