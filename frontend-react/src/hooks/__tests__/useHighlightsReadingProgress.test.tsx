import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import { useRef } from 'react'
import { useHighlightsReadingProgress } from '../useHighlightsReadingProgress'
import { useAuthStore } from '../../store/authStore'
import { putHighlightsReadingProgress } from '../../lib/api'

vi.mock('../../lib/api', () => ({ putHighlightsReadingProgress: vi.fn(), getHighlightsReadingProgress: vi.fn().mockResolvedValue({ progress: null }) }))

class Observer {
  static instance: Observer
  constructor(private callback: IntersectionObserverCallback) { Observer.instance = this }
  observe() { this.callback([{ isIntersecting: true, intersectionRatio: 1, target: document.querySelector('[data-cluster-id]')! } as IntersectionObserverEntry], this as unknown as IntersectionObserver) }
  emit(entries: Array<Pick<IntersectionObserverEntry, 'isIntersecting' | 'intersectionRatio' | 'target'>>) {
    this.callback(entries as IntersectionObserverEntry[], this as unknown as IntersectionObserver)
  }
  disconnect() {}
}

function Subject() { const ref = useRef<HTMLDivElement>(null); useHighlightsReadingProgress(ref, '7'); return <div ref={ref}><div data-cluster-id="7" /></div> }

describe('useHighlightsReadingProgress', () => {
  beforeEach(() => { vi.useFakeTimers(); vi.stubGlobal('IntersectionObserver', Observer); localStorage.clear(); vi.mocked(putHighlightsReadingProgress).mockReset() })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })
  it('登录保存位置而不触发已读 store', () => {
    useAuthStore.setState({ user: { id: 'u', username: 'u', email: 'u@test', role: 'user' } })
    render(<Subject />); vi.advanceTimersByTime(500)
    expect(putHighlightsReadingProgress).toHaveBeenCalledWith(7, false)
  })
  it('匿名只写本机 localStorage', () => {
    useAuthStore.setState({ user: null }); render(<Subject />); vi.advanceTimersByTime(500)
    expect(putHighlightsReadingProgress).not.toHaveBeenCalled()
    expect(JSON.parse(localStorage.getItem('highlights-reading-progress') || '{}').cluster_id).toBe(7)
  })
  it('pagehide 在防抖完成前也使用 keepalive 提交当前锚点', () => {
    useAuthStore.setState({ user: { id: 'u', username: 'u', email: 'u@test', role: 'user' } })
    render(<Subject />)
    window.dispatchEvent(new Event('pagehide'))
    expect(putHighlightsReadingProgress).toHaveBeenCalledWith(7, true)
  })
  it('多张卡片同时可见时始终保存最靠上的一张', () => {
    useAuthStore.setState({ user: { id: 'u', username: 'u', email: 'u@test', role: 'user' } })
    render(<Subject />)
    const first = document.querySelector('[data-cluster-id="7"]')!
    const second = document.createElement('div')
    second.dataset.clusterId = '8'
    vi.spyOn(first, 'getBoundingClientRect').mockReturnValue({ top: 10 } as DOMRect)
    vi.spyOn(second, 'getBoundingClientRect').mockReturnValue({ top: 100 } as DOMRect)

    Observer.instance.emit([
      { isIntersecting: true, intersectionRatio: 1, target: first },
      { isIntersecting: true, intersectionRatio: 1, target: second },
    ])
    vi.advanceTimersByTime(500)
    expect(putHighlightsReadingProgress).toHaveBeenLastCalledWith(7, false)

    Observer.instance.emit([{ isIntersecting: false, intersectionRatio: 0, target: first }])
    vi.advanceTimersByTime(500)
    expect(putHighlightsReadingProgress).toHaveBeenLastCalledWith(8, false)
  })

  it('顶部卡片只要部分可见也会保存，避免高卡片永远无法达到全量可见', () => {
    useAuthStore.setState({ user: { id: 'u', username: 'u', email: 'u@test', role: 'user' } })
    render(<Subject />)
    const first = document.querySelector('[data-cluster-id="7"]')!

    Observer.instance.emit([
      { isIntersecting: true, intersectionRatio: 0.25, target: first },
    ])
    vi.advanceTimersByTime(500)

    expect(putHighlightsReadingProgress).toHaveBeenLastCalledWith(7, false)
  })
})
