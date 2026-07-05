import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { navigateToActionCard } from '../actionNavigation'
import { useActionStore } from '../../store/actionStore'
import { useUIStore } from '../../store/uiStore'

describe('navigateToActionCard', () => {
  beforeEach(() => {
    window.location.hash = '#cluster=1326&d=item-1&s=feed'
    vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
    useActionStore.setState({
      actions: [],
      counts: {},
      directions: [],
      isLoading: false,
      focusedActionId: null,
    })
    useUIStore.setState({
      // v18.0 nav-merge: 'recommend' → 'info'（fixture 用任一非 actions 即可）
      l1: 'info',
      expandedKey: 'feed',
      searchQuery: '',
      theme: 'light',
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('navigates to a clean actions hash and focuses the target action', () => {
    navigateToActionCard(' action-42 ')

    expect(window.location.hash).toBe('#v=actions&a=action-42')
    expect(useUIStore.getState().l1).toBe('actions')
    expect(useUIStore.getState().expandedKey).toBeNull()
    expect(useActionStore.getState().focusedActionId).toBe('action-42')
    expect(window.scrollTo).toHaveBeenCalledWith({ top: 0 })
  })
})
