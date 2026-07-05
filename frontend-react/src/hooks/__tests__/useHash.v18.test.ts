import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mapLegacyL1, isLegacyFullscreenView, applyLegacyHashRedirect } from '../useHash'

beforeEach(() => {
  window.location.hash = ''
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('v18.0 mapLegacyL1', () => {
  it('recommend → info', () => {
    expect(mapLegacyL1('recommend')).toBe('info')
  })
  it('channels → info', () => {
    expect(mapLegacyL1('channels')).toBe('info')
  })
  it('已是 info → info（幂等）', () => {
    expect(mapLegacyL1('info')).toBe('info')
  })
  it('highlights / actions 不变', () => {
    expect(mapLegacyL1('highlights')).toBe('highlights')
    expect(mapLegacyL1('actions')).toBe('actions')
  })
  it('未知值 fallback highlights', () => {
    expect(mapLegacyL1('foo')).toBe('highlights')
    expect(mapLegacyL1(null)).toBe('highlights')
    expect(mapLegacyL1('')).toBe('highlights')
  })
  it('starred / history 不在白名单（应走全屏路由）', () => {
    // mapLegacyL1 只返回 L1View 三件套；starred/history 由 isLegacyFullscreenView 处理
    expect(mapLegacyL1('starred')).toBe('highlights')
    expect(mapLegacyL1('history')).toBe('highlights')
  })
})

describe('v18.0 isLegacyFullscreenView', () => {
  it('starred / history 返 true', () => {
    expect(isLegacyFullscreenView('starred')).toBe('starred')
    expect(isLegacyFullscreenView('history')).toBe('history')
  })
  it('其他值返 null', () => {
    expect(isLegacyFullscreenView('recommend')).toBeNull()
    expect(isLegacyFullscreenView('channels')).toBeNull()
    expect(isLegacyFullscreenView('info')).toBeNull()
    expect(isLegacyFullscreenView('highlights')).toBeNull()
    expect(isLegacyFullscreenView(null)).toBeNull()
  })
})

describe('legacy #item redirect', () => {
  it('#item= 深链迁移为信息页 item 弹窗深链', () => {
    window.location.hash = '#item=my%20item%2F1'

    expect(applyLegacyHashRedirect()).toBe(true)
    expect(window.location.hash).toBe('#v=info&d=my%20item%2F1')
  })
})
