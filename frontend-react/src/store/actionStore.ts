import { create } from 'zustand'
import type { ActionItem } from '../lib/types'

interface ActionState {
  actions: ActionItem[]
  counts: Record<string, number>
  directions: Array<{ slug: string; label: string; count: number }>
  isLoading: boolean
  focusedActionId: string | null

  setActionsResponse: (resp: {
    actions: ActionItem[]
    counts: Record<string, number>
    directions: Array<{ slug: string; label: string; count: number }>
  }) => void
  updateAction: (id: string, data: Partial<ActionItem>) => void
  addAction: (action: ActionItem) => void
  removeAction: (id: string) => void
  setIsLoading: (loading: boolean) => void
  setFocusedActionId: (id: string | null) => void
}

export const useActionStore = create<ActionState>((set) => ({
  actions: [],
  counts: {},
  directions: [],
  isLoading: false,
  focusedActionId: null,

  setActionsResponse: (resp) => set({
    actions: resp.actions,
    counts: resp.counts,
    directions: resp.directions,
  }),

  updateAction: (id, data) => set((state) => {
    const oldAction = state.actions.find((a) => a.id === id)
    const actions = state.actions.map((a) => (a.id === id ? { ...a, ...data } : a))
    const counts = { ...state.counts }
    if (oldAction && data.status && data.status !== oldAction.status) {
      if (counts[oldAction.status] != null) counts[oldAction.status]--
      counts[data.status] = (counts[data.status] ?? 0) + 1
      counts.total = counts.total ?? actions.length
    }
    return { actions, counts }
  }),

  addAction: (action) => set((state) => ({
    actions: [action, ...state.actions],
  })),

  removeAction: (id) => set((state) => ({
    actions: state.actions.filter((a) => a.id !== id),
  })),

  setIsLoading: (isLoading) => set({ isLoading }),
  setFocusedActionId: (focusedActionId) => set({ focusedActionId }),
}))
