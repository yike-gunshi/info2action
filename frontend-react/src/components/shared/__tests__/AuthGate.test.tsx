import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { MouseEvent } from 'react'
import { toast } from 'sonner'
import { requireAuth } from '../AuthGate'
import { useAuthStore } from '../../../store/authStore'

vi.mock('sonner', () => ({
  toast: {
    info: vi.fn(),
  },
}))

describe('requireAuth', () => {
  beforeEach(() => {
    window.location.hash = ''
    useAuthStore.setState({ user: null, isLoading: false, isChecked: true })
    vi.clearAllMocks()
  })

  it('未登录时点击去登录会先执行关闭回调再跳转登录页', () => {
    const onLoginClick = vi.fn()

    expect(requireAuth('收藏', { onLoginClick })).toBe(false)

    const [, options] = vi.mocked(toast.info).mock.calls[0]
    const action = options?.action as { onClick: (event: MouseEvent<HTMLButtonElement>) => void }
    action.onClick({} as MouseEvent<HTMLButtonElement>)

    expect(onLoginClick).toHaveBeenCalledTimes(1)
    expect(window.location.hash).toBe('#login')
  })

  it('已登录时直接放行且不弹 toast', () => {
    useAuthStore.setState({
      user: {
        id: 'u1',
        username: 'demo',
        email: 'demo@example.com',
        role: 'user',
      },
    })

    expect(requireAuth('收藏')).toBe(true)
    expect(toast.info).not.toHaveBeenCalled()
  })
})
