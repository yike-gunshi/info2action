import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { UpdatePulseBadge } from '../UpdatePulseBadge'

describe('UpdatePulseBadge', () => {
  afterEach(cleanup)

  it('active=false 时不渲染任何 DOM', () => {
    const { container } = render(<UpdatePulseBadge active={false} />)
    expect(container.firstChild).toBeNull()
  })

  it('active=true 时渲染有更新胶囊 + aria-label', () => {
    render(<UpdatePulseBadge active={true} />)
    const badge = screen.getByRole('img', { name: '有更新' })
    const dot = badge.querySelector('[aria-hidden="true"]')
    expect(badge).toBeInTheDocument()
    expect(badge).toHaveTextContent('有更新')
    expect(dot).toHaveStyle({ backgroundColor: 'var(--pulse-update)' })
  })

  it('不渲染 NEW 文字、不带 emoji 装饰（反 AI 模板纪律）', () => {
    render(<UpdatePulseBadge active={true} />)
    expect(screen.queryByText(/NEW/i)).toBeNull()
    expect(screen.getByText('有更新')).toBeInTheDocument()
    expect(screen.queryByText(/✨|🎉|🔔/)).toBeNull()
  })
})
