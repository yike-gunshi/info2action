import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { LoginPage } from '../LoginPage'
import { useUIStore } from '../../store/uiStore'

describe('LoginPage v19 Quiet Gate', () => {
  afterEach(() => {
    cleanup()
  })

  it('uses the editorial auth shell without the old i2a badge', () => {
    render(<LoginPage />)

    const topbarLogo = screen.getByTestId('login-topbar-logo')
    expect(topbarLogo).toHaveAttribute('href', '#')
    expect(topbarLogo).toHaveAccessibleName('返回精选')
    expect(topbarLogo).toHaveTextContent('info2act')

    const shell = screen.getByTestId('login-quiet-gate')
    expect(shell.className).toContain('max-w-[340px]')
    expect(shell.className).toContain('rounded-[6px]')
    expect(shell.className).toContain('border')
    expect(shell.className).toContain('shadow-none')

    const wordmark = screen.getByTestId('login-wordmark')
    expect(wordmark).toHaveTextContent('info2act')
    expect(wordmark.className).toContain('font-brand')
    expect(wordmark.className).toContain('text-[36px]')
    expect(wordmark.querySelector('.brand-wordmark__two')).toHaveTextContent('2')
    expect(screen.queryByText('i2a')).not.toBeInTheDocument()
  })

  it('lets visitors return from login to the public highlights tab', () => {
    window.location.hash = '#login'
    useUIStore.setState({ l1: 'actions' })
    render(<LoginPage />)

    fireEvent.click(screen.getByTestId('login-topbar-logo'))

    expect(useUIStore.getState().l1).toBe('highlights')
    expect(window.location.hash).toBe('')
  })

  it('uses warm brand tokens for the primary action and links', () => {
    render(<LoginPage />)

    expect(screen.getByTestId('login-submit').className).toContain('bg-[var(--brand)]')
    expect(screen.getByRole('link', { name: '注册' }).className).toContain('text-[var(--brand)]')
    expect(screen.getByRole('link', { name: '忘记密码？' }).className).toContain('hover:text-[var(--brand)]')
  })

  it('keeps fields accessible by their visible labels', () => {
    render(<LoginPage />)

    expect(screen.getByLabelText('邮箱或用户名')).toHaveAttribute('id', 'login-identifier')
    expect(screen.getByLabelText('密码')).toHaveAttribute('id', 'login-password')
    expect(screen.getByRole('button', { name: '显示密码' })).not.toHaveAttribute('tabindex', '-1')
  })
})
