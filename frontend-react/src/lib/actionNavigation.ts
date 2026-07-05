import { useActionStore } from '../store/actionStore'
import { useDetailStore } from '../store/detailStore'
import { useUIStore } from '../store/uiStore'

export function navigateToActionCard(actionId: string) {
  const id = String(actionId || '').trim()
  if (!id || typeof window === 'undefined') return

  useDetailStore.getState().closeModal()
  useUIStore.getState().setL1('actions')
  useUIStore.getState().setExpandedKey(null)
  useActionStore.getState().setFocusedActionId(id)

  const params = new URLSearchParams()
  params.set('v', 'actions')
  params.set('a', id)
  window.location.hash = params.toString()
  window.scrollTo({ top: 0 })
}
