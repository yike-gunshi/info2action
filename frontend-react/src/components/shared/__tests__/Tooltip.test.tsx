import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Tooltip } from '../Tooltip'

describe('Tooltip', () => {
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('hover 150ms 后出现，离开立即隐藏', () => {
    vi.useFakeTimers()
    render(
      <Tooltip content="字段解释">
        <button type="button">分数</button>
      </Tooltip>,
    )

    fireEvent.mouseEnter(screen.getByRole('button', { name: '分数' }))
    act(() => vi.advanceTimersByTime(149))
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    act(() => vi.advanceTimersByTime(1))
    expect(screen.getByRole('tooltip')).toHaveTextContent('字段解释')

    fireEvent.mouseLeave(screen.getByRole('button', { name: '分数' }))
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('focus 触发并通过 aria-describedby 关联，靠近上边缘时翻转到底部', () => {
    render(
      <Tooltip content="五维满分均为 3">
        <button type="button">五维</button>
      </Tooltip>,
    )
    const trigger = screen.getByRole('button', { name: '五维' })
    vi.spyOn(trigger, 'getBoundingClientRect').mockReturnValue({
      x: 20, y: 2, top: 2, left: 20, right: 60, bottom: 22, width: 40, height: 20,
      toJSON: () => ({}),
    })

    fireEvent.focus(trigger)

    const tooltip = screen.getByRole('tooltip')
    expect(trigger).toHaveAttribute('aria-describedby', tooltip.id)
    expect(tooltip).toHaveAttribute('data-side', 'bottom')
    fireEvent.blur(trigger)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('移动端长按触发，点击别处关闭', () => {
    vi.useFakeTimers()
    render(
      <div>
        <Tooltip content="长按解释">
          <button type="button">终审</button>
        </Tooltip>
        <button type="button">别处</button>
      </div>,
    )

    fireEvent.pointerDown(screen.getByRole('button', { name: '终审' }), { pointerType: 'touch' })
    act(() => vi.advanceTimersByTime(500))
    expect(screen.getByRole('tooltip')).toHaveTextContent('长按解释')
    fireEvent.pointerDown(screen.getByRole('button', { name: '别处' }), { pointerType: 'touch' })
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('rich 变体用白卡和 brand border 渲染明细', () => {
    render(
      <Tooltip variant="rich" content={<span>定分 item · max_q 0.92</span>}>
        <button type="button">簇分 9.2</button>
      </Tooltip>,
    )

    fireEvent.focus(screen.getByRole('button', { name: '簇分 9.2' }))
    const tooltip = screen.getByRole('tooltip')
    expect(tooltip).toHaveTextContent('定分 item · max_q 0.92')
    expect(tooltip).toHaveClass('bg-card', 'border-[var(--brand-border)]', 'max-w-[300px]')
  })
})
