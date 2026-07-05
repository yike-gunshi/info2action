import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { InfoSectionPillBar } from '../InfoSectionPillBar'

function rect(overrides: Partial<DOMRect> = {}): DOMRect {
  return {
    x: 0,
    y: 0,
    left: 0,
    right: 0,
    top: 0,
    bottom: 0,
    width: 0,
    height: 0,
    toJSON: () => ({}),
    ...overrides,
  } as DOMRect
}

describe('InfoSectionPillBar', () => {
  beforeEach(() => {
    const topbar = document.createElement('div')
    topbar.setAttribute('data-testid', 'topbar')
    topbar.getBoundingClientRect = () => rect({ height: 52, bottom: 52 })
    const subbar = document.createElement('div')
    subbar.setAttribute('data-testid', 'info-subbar')
    subbar.getBoundingClientRect = () => rect({ height: 40, top: 52, bottom: 92 })
    const section = document.createElement('section')
    section.id = 's-products'
    section.getBoundingClientRect = () => rect({ top: 420, bottom: 820, height: 400 })
    document.body.append(topbar, subbar, section)
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 120 })
  })

  afterEach(() => {
    cleanup()
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('渲染 section 内局部下划线筛选，不再作为 sticky 第三级导航', () => {
    render(
      <InfoSectionPillBar
        sectionKey="products"
        items={[
          { key: null, label: '全部' },
          { key: 'ai_video', label: 'AI 视频' },
        ]}
        activeKey={null}
        onSelect={vi.fn()}
      />,
    )

    const bar = screen.getByTestId('info-section-pill-bar')
    const allButton = screen.getByRole('button', { name: '全部' })
    const videoButton = screen.getByRole('button', { name: 'AI 视频' })
    expect(bar.className).not.toContain('sticky')
    expect(bar.className).toContain('bg-background')
    expect(bar.className).toContain('border-b')
    expect(bar.className).toContain('py-0')
    expect(bar.className).not.toContain('backdrop-blur')
    expect(bar).not.toHaveStyle({ top: '92px' })
    expect(screen.getByTestId('info-section-pill-row').className).toContain('h-10')
    expect(allButton.className).toContain('border-b-2')
    expect(allButton.className).toContain('border-[var(--brand)]')
    expect(allButton.className).toContain('text-[var(--brand)]')
    expect(allButton.className).not.toContain('rounded-full')
    expect(allButton.className).not.toContain('bg-foreground')
    expect(videoButton.className).toContain('border-transparent')
    expect(videoButton.className).toContain('text-muted-foreground')
  })

  it('点击局部 pill 时只触发 onSelect，不主动滚动页面', async () => {
    const onSelect = vi.fn()
    const scrollTo = vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
    const user = userEvent.setup()
    render(
      <InfoSectionPillBar
        sectionKey="products"
        items={[
          { key: null, label: '全部' },
          { key: 'ai_video', label: 'AI 视频' },
        ]}
        activeKey={null}
        onSelect={onSelect}
      />,
    )

    await user.click(screen.getByRole('button', { name: 'AI 视频' }))

    expect(onSelect).toHaveBeenCalledWith('ai_video')
    expect(scrollTo).not.toHaveBeenCalled()
  })

  it('内容溢出时展示和 L2tab 同款左右滑动按钮', async () => {
    render(
      <InfoSectionPillBar
        sectionKey="products"
        items={[
          { key: null, label: '全部' },
          { key: 'ai_video', label: 'AI 视频' },
          { key: 'ai_search', label: 'AI 搜索' },
          { key: 'ai_draw', label: 'AI 绘画 / 生图' },
          { key: 'ai_music', label: 'AI 音乐' },
          { key: 'ai_agent', label: 'AI Agent' },
        ]}
        activeKey={null}
        onSelect={vi.fn()}
      />,
    )

    const row = screen.getByTestId('info-section-pill-row') as HTMLDivElement
    const scrollBy = vi.fn()
    Object.defineProperty(row, 'scrollWidth', { configurable: true, value: 800 })
    Object.defineProperty(row, 'clientWidth', { configurable: true, value: 240 })
    Object.defineProperty(row, 'scrollLeft', { configurable: true, value: 0 })
    Object.defineProperty(row, 'scrollBy', { configurable: true, value: scrollBy })

    act(() => {
      window.dispatchEvent(new Event('resize'))
    })

    const right = await screen.findByTestId('info-section-pill-chevron-right-products-0')
    const left = await screen.findByTestId('info-section-pill-chevron-left-products-0')
    expect(right).toBeEnabled()
    expect(right.className).toContain('top-1/2')
    expect(right.className).toContain('-translate-y-1/2')
    expect(right.querySelector('svg')).toBeTruthy()
    expect(left).toBeDisabled()

    await userEvent.click(right)
    expect(scrollBy).toHaveBeenCalledWith({ left: 240, behavior: 'smooth' })
  })

  it('支持公众号二层 row 放在同一个局部筛选区块', () => {
    render(
      <InfoSectionPillBar
        sectionKey="products"
        items={[
          { key: null, label: '全部' },
          { key: 'AI周报', label: 'AI周报' },
        ]}
        activeKey="AI周报"
        onSelect={vi.fn()}
        nestedRows={[{
          prefix: '↳ AI周报:',
          items: [{ key: 'Alpha-公众号', label: 'Alpha', title: 'Alpha-公众号' }],
          activeKey: null,
          onSelect: vi.fn(),
        }]}
      />,
    )

    expect(screen.getAllByTestId('info-section-pill-row')).toHaveLength(2)
    expect(screen.getByText('↳ AI周报:')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Alpha' })).toHaveAttribute('title', 'Alpha-公众号')
  })
})
