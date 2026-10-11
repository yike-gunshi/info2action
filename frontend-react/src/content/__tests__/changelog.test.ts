import { describe, expect, it } from 'vitest'
import { CHANGELOG_RELEASES } from '../changelog'

describe('public changelog content', () => {
  it('contains identifiable releases with useful user-facing changes', () => {
    expect(CHANGELOG_RELEASES.length).toBeGreaterThan(0)
    expect(new Set(CHANGELOG_RELEASES.map(({ id }) => id)).size).toBe(CHANGELOG_RELEASES.length)

    for (const release of CHANGELOG_RELEASES) {
      expect(release.id.trim()).not.toBe('')
      expect(release.title.trim()).not.toBe('')
      expect(release.publishedAt).toMatch(/^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$/)
      expect(Number.isFinite(Date.parse(release.publishedAt))).toBe(true)
      expect(release.changes.length).toBeGreaterThan(0)
      for (const change of release.changes) {
        expect(['feature', 'improvement', 'fix']).toContain(change.kind)
        expect(change.title.trim()).not.toBe('')
        expect(change.description.trim()).not.toBe('')
      }
    }
  })
})
