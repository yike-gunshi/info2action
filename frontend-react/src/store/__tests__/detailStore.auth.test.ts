import { describe, it, expect, beforeEach, vi } from 'vitest'
import { useAuthStore } from '../authStore'
import { useDetailStore } from '../detailStore'
import type { FeedItem } from '../../lib/types'

class FakeEventSource {
  static instances: FakeEventSource[] = []
  url: string
  listeners = new Map<string, Array<(event: MessageEvent) => void>>()
  close = vi.fn()

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, callback: (event: MessageEvent) => void) {
    const list = this.listeners.get(type) ?? []
    list.push(callback)
    this.listeners.set(type, list)
  }

  emit(type: string, payload: unknown = {}) {
    const event = { data: JSON.stringify(payload) } as MessageEvent
    for (const callback of this.listeners.get(type) ?? []) {
      callback(event)
    }
  }
}

function jsonResponse(status: number, body: unknown = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

function mockFetchSequence(responses: Response[]) {
  const calls: string[] = []
  const queue = [...responses]
  vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL) => {
    calls.push(String(url))
    const next = queue.shift()
    if (!next) throw new Error('fetch mock exhausted')
    return next
  }))
  return { calls }
}

function flushPromises() {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  FakeEventSource.instances = []
  useAuthStore.setState({
    user: { id: 'u1', username: 'alice', email: 'alice@test.local', role: 'user' },
    isLoading: false,
    isChecked: true,
  })
  useDetailStore.setState({
    modalStack: [],
    itemDetail: null,
    itemActions: [],
    actionDetail: null,
    detailCache: new Map(),
    isLoading: false,
    asrStatus: 'idle',
    asrRawStatus: null,
    asrText: null,
    asrDurationSec: null,
    asrSegments: null,
    asrTextCn: null,
    asrSegmentsCn: null,
    asrCnStatus: 'none',
    asrCostYuan: null,
    asrProgress: null,
    asrError: null,
  })
})

describe('detailStore ASR auth handling', () => {
  it('startAsr uses refresh handling before reporting an auth failure', async () => {
    const { calls } = mockFetchSequence([
      jsonResponse(401, { error: 'expired access token' }),
      jsonResponse(200, { ok: true }),
      jsonResponse(200, { status: 'success', asr_text: 'hello', asr_duration_sec: 12 }),
    ])

    await useDetailStore.getState().startAsr('item-1')

    expect(calls).toEqual([
      '/api/items/item-1/asr',
      '/api/auth/refresh',
      '/api/items/item-1/asr',
    ])
    expect(useDetailStore.getState().asrStatus).toBe('ready')
    expect(useDetailStore.getState().asrText).toBe('hello')
  })

  it('retryTranslate uses refresh handling before updating translated text', async () => {
    useDetailStore.setState({ asrText: 'hello' })
    const { calls } = mockFetchSequence([
      jsonResponse(401, { error: 'expired access token' }),
      jsonResponse(200, { ok: true }),
      jsonResponse(200, { asr_text_cn: '你好', asr_segments_cn: [] }),
    ])

    await useDetailStore.getState().retryTranslate('item-1')

    expect(calls).toEqual([
      '/api/items/item-1/asr/translate',
      '/api/auth/refresh',
      '/api/items/item-1/asr/translate',
    ])
    expect(useDetailStore.getState().asrCnStatus).toBe('ready')
    expect(useDetailStore.getState().asrTextCn).toBe('你好')
  })

  it('startAsr marks current detail cache as running so close/reopen keeps 转写中', async () => {
    const item: FeedItem = {
      id: 'item-1',
      title: 'video item',
      platform: 'twitter',
      fetched_at: '2026-05-25T00:00:00Z',
    }
    useDetailStore.setState({
      modalStack: [{ type: 'item', id: 'item-1' }],
      itemDetail: item,
      detailCache: new Map([['item-1', { item, cachedAt: Date.now() }]]),
    })
    mockFetchSequence([
      jsonResponse(200, { status: 'running', task_id: 'item-1' }),
      jsonResponse(200, { ...item, asr_status: 'running' }),
    ])

    await useDetailStore.getState().startAsr('item-1')

    expect(useDetailStore.getState().asrStatus).toBe('running')
    expect(useDetailStore.getState().detailCache.get('item-1')?.item.asr_status).toBe('running')

    useDetailStore.getState().closeModal()
    useDetailStore.getState().openItem('item-1')

    expect(useDetailStore.getState().asrStatus).toBe('running')
    expect(useDetailStore.getState().asrRawStatus).toBe('running')
  })

  it('ASR done 事件后重新拉详情并自动刷新为完成态', async () => {
    const runningItem: FeedItem = {
      id: 'item-1',
      title: 'video item',
      platform: 'twitter',
      fetched_at: '2026-05-25T00:00:00Z',
      asr_status: 'running',
    }
    const readyItem: FeedItem = {
      ...runningItem,
      asr_status: 'success',
      asr_text: 'fresh transcript after done',
      asr_duration_sec: 80,
      asr_segments: [{ start_ms: 0, end_ms: 1000, text: 'fresh transcript after done' }],
      asr_text_cn: '完成后的转写',
      asr_segments_cn: ['完成后的转写'],
    }
    useDetailStore.setState({
      modalStack: [{ type: 'item', id: 'item-1' }],
      itemDetail: runningItem,
      asrStatus: 'running',
      asrRawStatus: 'running',
      asrCnStatus: 'loading',
      detailCache: new Map([['item-1', { item: runningItem, cachedAt: Date.now() }]]),
    })
    const { calls } = mockFetchSequence([
      jsonResponse(200, readyItem),
    ])
    vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)

    useDetailStore.getState()._connectAsrStream('item-1')
    const source = FakeEventSource.instances[0]
    expect(source.url).toBe('/api/items/item-1/asr/stream')

    source.emit('done', { status: 'success' })
    await flushPromises()

    expect(calls).toEqual(['/api/feed/item/item-1'])
    const state = useDetailStore.getState()
    expect(state.asrStatus).toBe('ready')
    expect(state.asrRawStatus).toBe('success')
    expect(state.asrText).toBe('fresh transcript after done')
    expect(state.asrCnStatus).toBe('ready')
    expect(state.itemDetail?.asr_status).toBe('success')
    expect(state.detailCache.get('item-1')?.item.asr_text).toBe('fresh transcript after done')
    expect(source.close).toHaveBeenCalled()
  })

  it('后台 ASR 事件不会污染当前打开的另一个 item 状态', () => {
    const transcribingItem: FeedItem = {
      id: 'item-1',
      title: 'background video',
      platform: 'twitter',
      fetched_at: '2026-05-25T00:00:00Z',
      asr_status: 'running',
    }
    const currentItem: FeedItem = {
      id: 'item-2',
      title: 'current video',
      platform: 'twitter',
      fetched_at: '2026-05-25T00:00:00Z',
    }
    useDetailStore.setState({
      modalStack: [{ type: 'item', id: 'item-2' }],
      itemDetail: currentItem,
      asrStatus: 'idle',
      asrText: null,
      detailCache: new Map([
        ['item-1', { item: transcribingItem, cachedAt: Date.now() }],
        ['item-2', { item: currentItem, cachedAt: Date.now() }],
      ]),
    })
    vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)

    useDetailStore.getState()._connectAsrStream('item-1')
    FakeEventSource.instances[0].emit('transcript', {
      text: 'background transcript',
      duration_sec: 9,
      segments: [{ start_ms: 0, end_ms: 1000, text: 'background transcript' }],
    })

    const state = useDetailStore.getState()
    expect(state.itemDetail?.id).toBe('item-2')
    expect(state.asrStatus).toBe('idle')
    expect(state.asrText).toBeNull()
    expect(state.detailCache.get('item-1')?.item.id).toBe('item-1')
    expect(state.detailCache.get('item-1')?.item.asr_text).toBe('background transcript')
    expect(state.detailCache.get('item-2')?.item.asr_text).toBeUndefined()
  })
})
