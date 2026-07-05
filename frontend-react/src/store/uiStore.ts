import { create } from 'zustand'
import type { L1View } from '../lib/types'

interface UIState {
  l1: L1View
  expandedKey: string | null
  searchQuery: string
  theme: 'light' | 'dark'

  setL1: (l1: L1View) => void
  setExpandedKey: (key: string | null) => void
  setSearchQuery: (query: string) => void
  setTheme: (theme: 'light' | 'dark') => void
  toggleTheme: () => void
}

export const useUIStore = create<UIState>((set) => ({
  // v17.0: 默认进站 tab = highlights（推翻原 recommend 默认）
  l1: 'highlights',
  expandedKey: null,
  searchQuery: '',
  theme: 'light',

  setL1: (l1) => set({ l1 }),
  setExpandedKey: (expandedKey) => set({ expandedKey }),
  setSearchQuery: (searchQuery) => set({ searchQuery }),
  setTheme: (theme) => set({ theme }),
  toggleTheme: () => set((state) => ({ theme: state.theme === 'light' ? 'dark' : 'light' })),
}))
