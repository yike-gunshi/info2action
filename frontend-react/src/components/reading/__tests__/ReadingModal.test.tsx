import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { ReadingModalError, ReadingModalShell } from '../ReadingModal'

function TestShell({ panelTestId = 'test-panel' }: { panelTestId?: string }) {
  return (
    <ReadingModalShell
      ariaLabelledby={`${panelTestId}-title`}
      isClosing={false}
      onClose={() => {}}
      panelTestId={panelTestId}
    >
      <h2 id={`${panelTestId}-title`}>测试弹窗</h2>
      <button type="button">第一个按钮</button>
      <button type="button">最后一个按钮</button>
    </ReadingModalShell>
  )
}

describe('ReadingModal shared states', () => {
  afterEach(() => {
    cleanup()
    document.documentElement.removeAttribute('style')
  })

  it('focuses the panel first, keeps the Tab loop inside, and restores previous focus', () => {
    const trigger = document.createElement('button')
    document.body.appendChild(trigger)
    trigger.focus()

    const { unmount } = render(<TestShell />)

    const dialog = screen.getByRole('dialog')
    const first = screen.getByRole('button', { name: '第一个按钮' })
    const last = screen.getByRole('button', { name: '最后一个按钮' })
    expect(dialog).toHaveFocus()
    expect(first).not.toHaveFocus()

    fireEvent.keyDown(dialog, { key: 'Tab' })
    expect(first).toHaveFocus()
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true })
    expect(last).toHaveFocus()

    dialog.focus()
    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true })
    expect(last).toHaveFocus()

    unmount()
    expect(trigger).toHaveFocus()
    trigger.remove()
  })

  it('reference-counts the html scroll lock and restores every inline style after the last modal closes', () => {
    const html = document.documentElement
    html.style.overflow = 'scroll'
    html.style.scrollbarGutter = 'stable'
    html.style.paddingRight = '7px'
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
    Object.defineProperty(html, 'clientWidth', { configurable: true, value: 1425 })

    const first = render(<TestShell panelTestId="first-panel" />)
    expect(html.style.overflow).toBe('hidden')
    expect(html.style.scrollbarGutter).toBe('auto')
    expect(html.style.paddingRight).toBe('15px')

    const second = render(<TestShell panelTestId="second-panel" />)
    first.unmount()
    expect(html.style.overflow).toBe('hidden')
    expect(html.style.scrollbarGutter).toBe('auto')
    expect(html.style.paddingRight).toBe('15px')

    second.unmount()
    expect(html.style.overflow).toBe('scroll')
    expect(html.style.scrollbarGutter).toBe('stable')
    expect(html.style.paddingRight).toBe('7px')
  })

  it('keeps a usable header and invokes retry from the human-readable error state', () => {
    const retry = vi.fn()
    const close = vi.fn()
    render(
      <ReadingModalShell
        ariaLabelledby="test-modal-title"
        isClosing={false}
        onClose={close}
        panelTestId="test-panel"
      >
        <ReadingModalError
          headerTestId="test-header"
          message="加载失败，请重试"
          onClose={close}
          onRetry={retry}
          testId="test-error"
          title="阅读详情"
          titleId="test-modal-title"
        />
      </ReadingModalShell>,
    )

    expect(screen.getByRole('dialog')).toHaveAttribute('aria-labelledby', 'test-modal-title')
    expect(screen.getByRole('button', { name: '关闭' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(retry).toHaveBeenCalledTimes(1)
  })
})
