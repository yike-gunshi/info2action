import { describe, it, expect, beforeEach } from 'vitest'
import { useUIStore } from '../uiStore'

describe('uiStore', () => {
  beforeEach(() => {
    // v18.0 nav-merge: 默认 l1 用 'highlights'（recommend tab 已删）
    useUIStore.setState({
      l1: 'highlights',
      expandedKey: null,
      searchQuery: '',
      theme: 'light',
    })
  })

  it('setL1 switches tab', () => {
    // v18.0 nav-merge: 'channels' 已被合并到 'info'
    useUIStore.getState().setL1('info')
    expect(useUIStore.getState().l1).toBe('info')
  })

  it('setL1 to actions', () => {
    useUIStore.getState().setL1('actions')
    expect(useUIStore.getState().l1).toBe('actions')
  })

  it('toggleTheme switches between light and dark', () => {
    expect(useUIStore.getState().theme).toBe('light')

    useUIStore.getState().toggleTheme()
    expect(useUIStore.getState().theme).toBe('dark')

    useUIStore.getState().toggleTheme()
    expect(useUIStore.getState().theme).toBe('light')
  })

  it('setExpandedKey sets and clears expanded key', () => {
    useUIStore.getState().setExpandedKey('section-ai')
    expect(useUIStore.getState().expandedKey).toBe('section-ai')

    useUIStore.getState().setExpandedKey(null)
    expect(useUIStore.getState().expandedKey).toBeNull()
  })

  it('setSearchQuery updates search query', () => {
    useUIStore.getState().setSearchQuery('claude')
    expect(useUIStore.getState().searchQuery).toBe('claude')
  })

  it('setTheme sets theme directly', () => {
    useUIStore.getState().setTheme('dark')
    expect(useUIStore.getState().theme).toBe('dark')
  })
})
