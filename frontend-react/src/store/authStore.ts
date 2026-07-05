import { create } from 'zustand'

export interface AuthUser {
  id: string
  username: string
  email: string
  role: 'admin' | 'user'
  has_discord_token?: boolean
  onboarding_completed?: boolean
}

interface AuthState {
  user: AuthUser | null
  isLoading: boolean   // true while checkAuth() is in flight
  isChecked: boolean   // true after first checkAuth() completes

  setUser: (user: AuthUser | null) => void
  setLoading: (v: boolean) => void
  setChecked: (v: boolean) => void
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  isLoading: true,
  isChecked: false,

  setUser: (user) => set({ user }),
  setLoading: (isLoading) => set({ isLoading }),
  setChecked: (isChecked) => set({ isChecked }),
}))

// Derived selectors
export const useIsLoggedIn = () => useAuthStore((s) => !!s.user)
export const useIsAdmin = () => useAuthStore((s) => s.user?.role === 'admin')
