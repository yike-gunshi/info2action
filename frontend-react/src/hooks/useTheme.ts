import { useState, useEffect, useCallback } from 'react'
import type { ThemeMode } from '../lib/types'

const STORAGE_KEY = 'i2a_theme'

function getUrlThemeOverride(): ThemeMode | null {
  const theme = new URLSearchParams(window.location.search).get('theme')
  return theme === 'light' || theme === 'dark' ? theme : null
}

function getSystemTheme(): 'light' | 'dark' {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function applyTheme(mode: ThemeMode) {
  document.documentElement.classList.toggle('dark', mode === 'dark')
  document.documentElement.setAttribute('data-theme', mode)
}

export function useTheme() {
  const [mode, setMode] = useState<ThemeMode>(() => {
    const override = getUrlThemeOverride()
    if (override) return override
    try {
      const stored = localStorage.getItem(STORAGE_KEY)
      if (stored === 'light' || stored === 'dark') return stored
    } catch { /* Storage is optional; public pages still use the system theme. */ }
    // Migrate 'auto' or missing → resolve to system preference
    return getSystemTheme()
  })

  useEffect(() => {
    applyTheme(mode)
    if (getUrlThemeOverride()) return
    try {
      localStorage.setItem(STORAGE_KEY, mode)
    } catch { /* Keep the selected theme for this page session. */ }
  }, [mode])

  const toggle = useCallback(() => {
    setMode((prev) => (prev === 'light' ? 'dark' : 'light'))
  }, [])

  return { mode, setMode, toggle }
}
