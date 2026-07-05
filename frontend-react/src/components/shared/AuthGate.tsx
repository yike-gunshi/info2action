import { useAuthStore } from '../../store/authStore'
import { toast } from 'sonner'

/**
 * Check if user is logged in before performing an action.
 * If not logged in, show a toast and redirect to login.
 * Returns true if authenticated, false otherwise.
 */
export function requireAuth(actionLabel?: string, options?: { onLoginClick?: () => void }): boolean {
  const user = useAuthStore.getState().user
  if (user) return true

  toast.info(actionLabel ? `${actionLabel}需要登录` : '请先登录', {
    action: {
      label: '去登录',
      onClick: () => {
        options?.onLoginClick?.()
        window.location.hash = 'login'
      },
    },
  })
  return false
}
