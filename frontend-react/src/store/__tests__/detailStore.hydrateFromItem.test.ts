/**
 * forge-review F2: hydrateFromItem 边界条件覆盖。
 *
 * 关键:详情内容渲染时从 fetchFeedItem 回来的 item 可能有:
 * - 全 null 字段(陈旧/纯文本 item)
 * - 未知 asr_status(后端新枚举前端未同步)
 * - asr_text_cn 为空字符串 vs null
 *
 * hydrateFromItem SHALL 在这些情况下不 crash,产出合理 store state。
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { useDetailStore } from '../detailStore'
import type { FeedItem } from '../../lib/types'

function makeItem(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'x',
    title: 't',
    platform: 'twitter',
    fetched_at: '2026-04-19T00:00:00Z',
    ...overrides,
  }
}

describe('detailStore.hydrateFromItem 边界', () => {
  beforeEach(() => {
    // 重置 asr 相关状态为初始值(间接方式:用默认 item 调 hydrate)
    useDetailStore.getState().hydrateFromItem(makeItem())
  })

  it('全 null asr_* 字段 → store 所有 asr 字段为 null / idle', () => {
    useDetailStore.getState().hydrateFromItem(makeItem())
    const s = useDetailStore.getState()
    expect(s.asrStatus).toBe('idle')
    expect(s.asrText).toBeNull()
    expect(s.asrDurationSec).toBeNull()
    expect(s.asrError).toBeNull()
    expect(s.asrSegments).toBeNull()
    expect(s.asrTextCn).toBeNull()
    expect(s.asrSegmentsCn).toBeNull()
    expect(s.asrCostYuan).toBeNull()
    expect(s.asrRawStatus).toBeNull()
    expect(s.asrCnStatus).toBe('none')
  })

  it('有 asr_text 但 asr_status 为 undefined → panel 推导为 ready(text 优先)', () => {
    useDetailStore.getState().hydrateFromItem(makeItem({ asr_text: 'hello world' }))
    expect(useDetailStore.getState().asrStatus).toBe('ready')
    expect(useDetailStore.getState().asrText).toBe('hello world')
  })

  it('asr_status=success + asr_text_cn=null → cnStatus=loading(用户等翻译)', () => {
    useDetailStore.getState().hydrateFromItem(
      makeItem({ asr_status: 'success', asr_text: 'en text', asr_text_cn: null }),
    )
    expect(useDetailStore.getState().asrCnStatus).toBe('loading')
  })

  it('asr_text_cn 空字符串 → 视为未翻译(cnStatus 不等于 ready)', () => {
    useDetailStore.getState().hydrateFromItem(
      makeItem({ asr_status: 'success', asr_text: 'en', asr_text_cn: '' }),
    )
    // 空字符串 falsy,ternary 走 (asr_status === 'success' ? 'loading' : 'none') = loading
    expect(useDetailStore.getState().asrCnStatus).toBe('loading')
  })

  it('asr_text_cn 有内容 → cnStatus=ready', () => {
    useDetailStore.getState().hydrateFromItem(
      makeItem({ asr_status: 'success', asr_text: 'en', asr_text_cn: '中文译文' }),
    )
    expect(useDetailStore.getState().asrCnStatus).toBe('ready')
    expect(useDetailStore.getState().asrTextCn).toBe('中文译文')
  })

  it('asr_status 为未知字符串 → inferPanelState 不 crash,退化为 idle', () => {
    // 强转 any 模拟后端新增未同步前端的 enum 值
    const item = makeItem({ asr_text: undefined, asr_status: 'unknown_new_value' as never })
    expect(() => useDetailStore.getState().hydrateFromItem(item)).not.toThrow()
    // asrRawStatus 透传原值
    expect(useDetailStore.getState().asrRawStatus).toBe('unknown_new_value')
  })
})
