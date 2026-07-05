import { describe, expect, it } from 'vitest'
import {
  buildInfoItemHash,
  buildInfoItemHref,
  buildInfoItemShareUrl,
  parseLegacyItemHash,
} from '../itemDeepLink'

describe('itemDeepLink', () => {
  it('builds canonical info item modal deep links', () => {
    expect(buildInfoItemHash('my item/1')).toBe('v=info&d=my%20item%2F1')
    expect(buildInfoItemHref('my item/1')).toBe('/#v=info&d=my%20item%2F1')
    expect(buildInfoItemShareUrl('my item/1')).toBe('https://www.info2act.com#v=info&d=my%20item%2F1')
  })

  it('parses legacy #item links', () => {
    expect(parseLegacyItemHash('#item=my%20item%2F1')).toBe('my item/1')
    expect(parseLegacyItemHash('item=abc')).toBe('abc')
    expect(parseLegacyItemHash('v=info&d=abc')).toBeNull()
  })
})
