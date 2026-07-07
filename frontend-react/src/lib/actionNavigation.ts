import { useActionStore } from '../store/actionStore'
import { useDetailStore } from '../store/detailStore'
import { useUIStore } from '../store/uiStore'

export function navigateToActionCard(actionId: string) {
  const id = String(actionId || '').trim()
  if (!id || typeof window === 'undefined') return

  // v21.0 (模块 C): 切行动 Tab + 卡片高亮 + 自动打开行动弹窗。
  useDetailStore.getState().closeModal()
  useUIStore.getState().setL1('actions')
  useUIStore.getState().setExpandedKey(null)
  useActionStore.getState().setFocusedActionId(id)

  const params = new URLSearchParams()
  params.set('v', 'actions')
  params.set('a', id)
  window.location.hash = params.toString()
  window.scrollTo({ top: 0 })

  // 关闭上一个弹窗后压入行动弹窗,让"查看行动点"直接落到行动详情。
  useDetailStore.getState().openAction(id)
}
