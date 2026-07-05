import { describe, it, expect, beforeEach } from 'vitest'
import { useActionStore } from '../actionStore'
import type { ActionItem } from '../../lib/types'

function makeAction(overrides: Partial<ActionItem> = {}): ActionItem {
  return {
    id: 'act-1',
    title: 'Test Action',
    type: 'research',
    status: 'pending',
    created_at: '2026-03-28T10:00:00Z',
    ...overrides,
  }
}

describe('actionStore', () => {
  beforeEach(() => {
    useActionStore.setState({
      actions: [],
      counts: {},
      directions: [],
      isLoading: false,
      focusedActionId: null,
    })
  })

  it('setActionsResponse sets all data', () => {
    const resp = {
      actions: [makeAction({ id: '1' }), makeAction({ id: '2' })],
      counts: { pending: 2 },
      directions: [{ slug: 'ai', label: 'AI', count: 2 }],
    }

    useActionStore.getState().setActionsResponse(resp)
    const state = useActionStore.getState()
    expect(state.actions.length).toBe(2)
    expect(state.counts).toEqual({ pending: 2 })
    expect(state.directions).toEqual([{ slug: 'ai', label: 'AI', count: 2 }])
  })

  it('updateAction updates specified action', () => {
    useActionStore.setState({ actions: [makeAction({ id: '1', status: 'pending' })] })

    useActionStore.getState().updateAction('1', { status: 'done' })
    expect(useActionStore.getState().actions[0].status).toBe('done')
  })

  it('updateAction does not affect other actions', () => {
    useActionStore.setState({
      actions: [
        makeAction({ id: '1', status: 'pending' }),
        makeAction({ id: '2', status: 'pending' }),
      ],
    })

    useActionStore.getState().updateAction('1', { status: 'done' })
    expect(useActionStore.getState().actions[1].status).toBe('pending')
  })

  it('addAction prepends to list', () => {
    useActionStore.setState({ actions: [makeAction({ id: '1' })] })

    useActionStore.getState().addAction(makeAction({ id: '2', title: 'New' }))
    const actions = useActionStore.getState().actions
    expect(actions.length).toBe(2)
    expect(actions[0].id).toBe('2')
  })

  it('removeAction removes by id', () => {
    useActionStore.setState({
      actions: [makeAction({ id: '1' }), makeAction({ id: '2' })],
    })

    useActionStore.getState().removeAction('1')
    const actions = useActionStore.getState().actions
    expect(actions.length).toBe(1)
    expect(actions[0].id).toBe('2')
  })

  it('setFocusedActionId stores navigation target', () => {
    useActionStore.getState().setFocusedActionId('act-42')
    expect(useActionStore.getState().focusedActionId).toBe('act-42')

    useActionStore.getState().setFocusedActionId(null)
    expect(useActionStore.getState().focusedActionId).toBeNull()
  })
})
