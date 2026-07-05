import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { SubmitPanel } from '../SubmitPanel'
import { submitRecordsStorageKey } from '../../../lib/submitRecords'

describe('SubmitPanel storage', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  afterEach(() => {
    cleanup()
  })

  it('namespaces submit records by current user id', () => {
    expect(submitRecordsStorageKey('user-1')).toBe('submit_records:user-1')
    expect(submitRecordsStorageKey('user-2')).toBe('submit_records:user-2')
    expect(submitRecordsStorageKey(null)).toBe('submit_records:anon')
    expect(submitRecordsStorageKey('user-1')).not.toBe(submitRecordsStorageKey('user-2'))
  })

  it('opens as a light link popover with warm token classes and empty state', () => {
    render(<SubmitPanel />)

    fireEvent.click(screen.getByLabelText('提交链接'))

    const popover = screen.getByTestId('submit-panel-popover')
    expect(popover.className).toContain('w-[380px]')
    expect(popover.className).toContain('rounded-[6px]')
    expect(popover.className).toContain('border-border/90')
    expect(popover.className).toContain('shadow-[0_10px_30px_rgba(26,25,23,0.08)]')
    expect(popover.className).not.toContain('rounded-xl')
    expect(popover.className).not.toContain('shadow-prominent')

    const input = screen.getByRole('textbox')
    expect(input).toHaveAttribute('placeholder', '粘贴链接...')
    expect(input.className).toContain('h-10')
    expect(input.className).toContain('focus:border-[var(--brand)]')
    expect(input.className).not.toContain('focus:ring-ring')

    const submit = within(popover).getByRole('button', { name: '提交' })
    expect(submit).toBeDisabled()
    expect(submit.className).toContain('bg-[var(--brand)]')
    expect(submit.className).toContain('disabled:bg-muted')
    expect(screen.getByText('暂无提交记录')).toBeInTheDocument()
  })

  it('keeps submit button state fixed while typing a URL', () => {
    render(<SubmitPanel />)

    fireEvent.click(screen.getByLabelText('提交链接'))
    const input = screen.getByRole('textbox')
    const submit = screen.getByRole('button', { name: '提交' })

    expect(submit).toBeDisabled()
    fireEvent.change(input, { target: { value: 'https://example.com/ai-agent-report' } })

    expect(submit).not.toBeDisabled()
    expect(submit.className).toContain('h-10')
    expect(submit.className).toContain('min-w-[58px]')
  })

  it('renders compact warm status rows from local history', () => {
    const longRemoteError = 'Remote DB connection/query failed: null value in column "platform" of relation "items" violates not-null constraint'
    window.localStorage.setItem(submitRecordsStorageKey(null), JSON.stringify([
      {
        url: 'https://example.com/ai-agent-report',
        title: 'AI Agent 生态盘点',
        status: 'duplicate',
        itemId: 'item-1',
        submittedAt: new Date(Date.now() - 120_000).toISOString(),
      },
      {
        url: 'https://invalid-link.example.com/page',
        status: 'error',
        error: longRemoteError,
        submittedAt: new Date(Date.now() - 480_000).toISOString(),
      },
    ]))

    render(<SubmitPanel />)

    fireEvent.click(screen.getByLabelText('提交链接'))

    const popover = screen.getByTestId('submit-panel-popover')
    expect(within(popover).getByText('AI Agent 生态盘点')).toBeInTheDocument()
    expect(within(popover).getByText('已存在')).toBeInTheDocument()
    expect(within(popover).getByText('https://invalid-link.example.com/page')).toBeInTheDocument()
    expect(within(popover).getByText(longRemoteError)).toBeInTheDocument()
    expect(within(popover).getByText('已存在').className).toContain('text-emerald')
    expect(within(popover).getByText(longRemoteError).className).toContain('text-destructive')
    expect(within(popover).getByText(longRemoteError).className).toContain('truncate')
    expect(within(popover).getByText(longRemoteError).className).not.toContain('shrink-0')
    expect(popover.querySelector('.overflow-x-hidden')).not.toBeNull()
  })
})
