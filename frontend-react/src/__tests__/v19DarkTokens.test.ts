import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const globalsCss = readFileSync('src/globals.css', 'utf8')
const darkBlock = globalsCss.match(/\.dark\s*\{(?<body>[\s\S]*?)\n\s{2}\}/)?.groups?.body ?? ''

function token(name: string): string | null {
  const match = darkBlock.match(new RegExp(`${name}:\\s*([^;]+);`))
  return match?.[1]?.trim() ?? null
}

describe('v19 dark tokens', () => {
  it('reserves the vertical scrollbar gutter to avoid centered layout jumps', () => {
    expect(globalsCss).toContain('scrollbar-gutter: stable;')
  })

  it('allows long inline links to wrap inside fixed-width modals', () => {
    expect(globalsCss).toMatch(/\.content-inline-link\s*\{[\s\S]*overflow-wrap:\s*anywhere;/)
  })

  it('uses warm dark surfaces instead of pure black or blue-black inversion', () => {
    expect(token('--background')).toBe('#181611')
    expect(token('--card')).toBe('#211E18')
    expect(token('--border')).toBe('#39342B')
    expect(token('--background')).not.toBe('#000000')
  })

  it('keeps dark active color in the warm brand family instead of indigo', () => {
    expect(token('--brand')).toBe('#F0A273')
    expect(token('--primary')).toBe('#F0A273')
    expect(token('--accent-foreground')).toBe('#F2B287')
  })

  it('defines modal semantic tokens so dark mode can theme shared popups', () => {
    expect(globalsCss).toContain('--modal-surface:')
    expect(globalsCss).toContain('--modal-paper-texture:')
    expect(token('--modal-surface')).toBe('#211E18')
    expect(token('--modal-text')).toBe('#ECE4D8')
    expect(token('--modal-border')).toBe('#4A4034')
    expect(token('--modal-current-bg')).toBe('#3A2A21')
    expect(token('--modal-danger-surface')).toBe('#2D201B')
  })
})
