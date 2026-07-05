import { cleanup, render, waitFor } from '@testing-library/react'
import { createElement } from 'react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { parseClusterHash, useHash } from '../useHash'
import { useDetailStore } from '../../store/detailStore'
import { useUIStore } from '../../store/uiStore'

function HashHarness() {
  useHash()
  return null
}

beforeEach(() => {
  window.location.hash = ''
  useUIStore.setState({ l1: 'highlights', expandedKey: null, searchQuery: '', theme: 'light' })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('parseClusterHash', () => {
  it('hash 为 #cluster=42 时返回 42', () => {
    window.location.hash = '#cluster=42'
    expect(parseClusterHash()).toBe(42)
  })

  it('hash 为 #cluster=999 时返回 999', () => {
    window.location.hash = '#cluster=999'
    expect(parseClusterHash()).toBe(999)
  })

  it('hash 为 #item=42 时不返回 cluster id', () => {
    window.location.hash = '#item=42'
    expect(parseClusterHash()).toBe(null)
  })

  it('hash 为 #v=recommend 时不返回(URLSearchParams 模式)', () => {
    window.location.hash = '#v=recommend'
    expect(parseClusterHash()).toBe(null)
  })

  it('hash 空时返回 null', () => {
    window.location.hash = ''
    expect(parseClusterHash()).toBe(null)
  })

  it('hash 为 #cluster= 空值时返回 null', () => {
    window.location.hash = '#cluster='
    expect(parseClusterHash()).toBe(null)
  })

  it('hash 为 #cluster=abc 非数字时返回 null', () => {
    window.location.hash = '#cluster=abc'
    expect(parseClusterHash()).toBe(null)
  })

  it('hash 为 #cluster=-5 负数时返回 null', () => {
    window.location.hash = '#cluster=-5'
    expect(parseClusterHash()).toBe(null)
  })
})

describe('useHash item detail deep link', () => {
  it('直接访问 #v=info&d= 时切到信息页并打开 item 弹窗', async () => {
    const openItemSpy = vi.spyOn(useDetailStore.getState(), 'openItem').mockImplementation(() => {})
    window.location.hash = '#v=info&d=deep-item'

    render(createElement(HashHarness))

    await waitFor(() => expect(openItemSpy).toHaveBeenCalledWith('deep-item'))
    expect(useUIStore.getState().l1).toBe('info')
  })
})
