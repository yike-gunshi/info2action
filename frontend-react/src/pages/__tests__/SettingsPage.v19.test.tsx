import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { SettingsPage } from '../SettingsPage'
import { useAuthStore } from '../../store/authStore'
import { getUserProfile, getUserSettings, updateUserProfile, updateUserSettings } from '../../lib/api'

vi.mock('../../components/layout/TopBar', () => ({
  TopBar: ({ activeL1 }: { activeL1: string | null }) => (
    <header data-testid="mock-topbar" data-active-l1={String(activeL1)} />
  ),
}))

vi.mock('../../lib/api', () => ({
  getUserSettings: vi.fn(),
  updateUserSettings: vi.fn(),
  getUserProfile: vi.fn(),
  updateUserProfile: vi.fn(),
}))

const mockGetUserSettings = getUserSettings as unknown as ReturnType<typeof vi.fn>
const mockGetUserProfile = getUserProfile as unknown as ReturnType<typeof vi.fn>
const mockUpdateUserSettings = updateUserSettings as unknown as ReturnType<typeof vi.fn>
const mockUpdateUserProfile = updateUserProfile as unknown as ReturnType<typeof vi.fn>

describe('SettingsPage v19 utility shell', () => {
  beforeEach(() => {
    useAuthStore.setState({
      user: {
        id: 'u1',
        username: 'yike',
        email: 'yike@example.com',
        role: 'user',
        onboarding_completed: true,
      },
      isLoading: false,
      isChecked: true,
    })
    mockGetUserSettings.mockResolvedValue({
      discord_bot_token: 'abcd...wxyz',
      has_discord_token: true,
    })
    mockGetUserProfile.mockResolvedValue({
      profile: {
        role: 'pm',
        interests: ['ai-agents', 'prompt-eng'],
        tools: ['cursor', 'claude-code'],
      },
      onboarding_completed: true,
    })
    mockUpdateUserSettings.mockResolvedValue({ ok: true })
    mockUpdateUserProfile.mockResolvedValue({ ok: true, profile: null })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('复用 utility TopBar，并移除旧紫色、大圆角设置页语言', async () => {
    render(<SettingsPage />)

    expect(screen.getByTestId('mock-topbar')).toHaveAttribute('data-active-l1', 'null')
    expect(await screen.findByText('产品经理')).toBeInTheDocument()

    const accountSection = screen.getByRole('heading', { name: '账号信息' }).closest('section')
    expect(accountSection?.className).toContain('rounded-[4px]')
    expect(accountSection?.className).toContain('border-border')
    expect(accountSection?.className).not.toContain('rounded-xl')

    const edit = screen.getByRole('button', { name: '编辑' })
    expect(edit.className).toContain('text-[var(--brand)]')
    expect(edit.className).toContain('hover:bg-[var(--brand-soft)]')
    expect(edit.className).not.toContain('text-primary')

    const interest = screen.getByText('AI Agent')
    expect(interest.className).toContain('bg-[var(--brand-soft)]')
    expect(interest.className).toContain('text-[var(--brand)]')
    expect(interest.className).toContain('rounded-[4px]')
    expect(interest.className).not.toContain('rounded-full')

    const save = screen.getByRole('button', { name: '保存' })
    expect(save.className).toContain('bg-[var(--brand)]')
    expect(save.className).toContain('rounded-[4px]')
    expect(save.className).not.toContain('bg-primary')
    expect(save.className).not.toContain('rounded-[10px]')
  })
})
