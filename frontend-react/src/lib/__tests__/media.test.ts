import { describe, expect, it } from 'vitest'
import { proxiedImageUrl } from '../media'

describe('proxiedImageUrl', () => {
  it('keeps same-origin and data/blob images unchanged', () => {
    expect(proxiedImageUrl('/images/a.jpg')).toBe('/images/a.jpg')
    expect(proxiedImageUrl('data:image/png;base64,abc')).toBe('data:image/png;base64,abc')
    expect(proxiedImageUrl('blob:http://localhost/x')).toBe('blob:http://localhost/x')
  })

  it('routes external http images through same-origin proxy', () => {
    expect(proxiedImageUrl('https://example.com/a b.jpg')).toBe(
      '/api/media/image-proxy?url=https%3A%2F%2Fexample.com%2Fa%20b.jpg',
    )
  })
})
