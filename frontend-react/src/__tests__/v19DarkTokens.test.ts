import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const globalsCss = readFileSync('src/globals.css', 'utf8')
const darkBlock = globalsCss.match(/\.dark\s*\{(?<body>[\s\S]*?)\n\s{2}\}/)?.groups?.body ?? ''
// 亮色段 = .dark 块之前的全部（含两个 :root 块）
const lightSection = globalsCss.split('.dark')[0]

function token(name: string): string | null {
  const match = darkBlock.match(new RegExp(`${name}:\\s*([^;]+);`))
  return match?.[1]?.trim() ?? null
}

function lightToken(name: string): string | null {
  const match = lightSection.match(new RegExp(`${name}:\\s*([^;]+);`))
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

  // v24.0 §21.6: 亮色 accent 家族统一映射暖铜 --brand（与暗色/admin v23.1 同源），靛蓝全站退役
  it('maps light accent family to the warm brand tokens (v24 §21.6)', () => {
    expect(lightToken('--brand')).toBe('#C65A1E')
    expect(lightToken('--primary')).toBe('var(--brand)')
    expect(lightToken('--primary-foreground')).toBe('var(--brand-foreground)')
    expect(lightToken('--accent')).toBe('var(--brand-soft)')
    expect(lightToken('--accent-foreground')).toBe('var(--brand)')
    expect(lightToken('--ring')).toBe('var(--brand)')
    expect(lightToken('--badge-official-bg')).toBe('var(--brand-soft)')
    expect(lightToken('--badge-official-fg')).toBe('var(--brand)')
  })

  it('retires indigo everywhere in globals.css (v24 §21.6)', () => {
    // 靛蓝 hex 拆开拼接,避免测试文件自身撞 §21.6 的 grep 断言 gate
    const retiredIndigo = ['#4F52', 'E4'].join('')
    const retiredIndigoSoft = ['#EEEC', 'FF'].join('')
    expect(globalsCss.toUpperCase()).not.toContain(retiredIndigo)
    expect(globalsCss.toUpperCase()).not.toContain(retiredIndigoSoft)
    // 幽灵 token --warn 与死代码 --terminal-* 一并退役
    expect(globalsCss).not.toContain('--warn:')
    expect(globalsCss).not.toContain('--terminal-')
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
