/**
 * v18.0 Spec-2.5 rev2：InfoView 集成测试 — groupBy 状态 + localStorage 持久化。
 *
 * 覆盖：
 *   - 默认进入「类型」（无 localStorage / 2026-05-23 信息页分类优先）
 *   - localStorage='category' 时进入「类型」（持久化恢复 / §2.5.2 / .7）
 *   - 未打新默认版本标记的旧 localStorage 会迁移回「类型」
 *   - 切换写入 localStorage（§2.5.3 / .4）
 *   - localStorage 不可用时 setItem 抛异常 → 不传播（§2.5.E3）
 *   - localStorage 中含无效值（'invalid' / null / 'undefined'）时 fallback 到 'category'
 *   - 内容区随 groupBy 切换 ChannelsView / InfoCategoryView
 *
 * 2026-05-20 主要变化：
 *   - localStorage 无值 / 异常 / 无效值时 fallback 默认回到 'category'
 *   - role='tab' 改为 role='button'（pill 风格不再用 tablist 语义）
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('../../channels/ChannelsView', () => ({
  ChannelsView: () => <div data-testid="channels-view">platform-mode</div>,
}))

vi.mock('../InfoCategoryView', () => {
  const Mock = ({ onLoadingChange }: { onLoadingChange?: (b: boolean) => void }) => {
    // 模拟 mount 后 loading 一闪即过；setTimeout 0 避免渲染过程中 setState
    if (onLoadingChange) setTimeout(() => onLoadingChange(false), 0)
    return <div data-testid="info-category-view">category-mode</div>
  }
  return { InfoCategoryView: Mock }
})

import { InfoView } from '../InfoView'

const LS_KEY = 'info_tab_group_by'
const LS_DEFAULT_REV_KEY = 'info_tab_group_by_default_rev'
const DEFAULT_REV = '2026-05-23-category-v1'

describe('InfoView (Spec-2.5 rev3)', () => {
  beforeEach(() => {
    localStorage.clear()
  })
  afterEach(() => cleanup())

  it('无 localStorage 时默认进入「类型」（2026-05-23 信息页分类优先）', () => {
    render(<InfoView />)
    expect(screen.getByTestId('info-category-view')).toBeInTheDocument()
    expect(screen.queryByTestId('channels-view')).toBeNull()
    const toggle = screen.getByRole('button', { name: '类型' })
    expect(toggle).toHaveAttribute('aria-pressed', 'true')
  })

  it('信息页内容区和 L2 tab 保持 12px 呼吸间距', () => {
    render(<InfoView />)
    const shell = screen.getByTestId('info-view-shell')
    expect(shell.className).toContain('max-w-[1360px]')
    expect(shell.className).toContain('pt-0')
    expect(shell.className).not.toContain('pt-4')
    expect(screen.getByTestId('info-subbar')).toBeInTheDocument()
    expect(screen.getByTestId('info-view-content').className).toContain('pt-3')
  })

  it('localStorage=category 时进入「类型」（持久化恢复 §2.5.2 / .7）', () => {
    localStorage.setItem(LS_KEY, 'category')
    localStorage.setItem(LS_DEFAULT_REV_KEY, DEFAULT_REV)
    render(<InfoView />)
    expect(screen.getByTestId('info-category-view')).toBeInTheDocument()
    expect(screen.queryByTestId('channels-view')).toBeNull()
    const toggle = screen.getByRole('button', { name: '类型' })
    expect(toggle).toHaveAttribute('aria-pressed', 'true')
  })

  it('旧默认 localStorage=platform 且未打版本标记时迁移回「类型」', () => {
    localStorage.setItem(LS_KEY, 'platform')
    render(<InfoView />)
    expect(screen.getByTestId('info-category-view')).toBeInTheDocument()
    expect(localStorage.getItem(LS_DEFAULT_REV_KEY)).toBe(DEFAULT_REV)
    expect(localStorage.getItem(LS_KEY)).toBe('category')
  })

  it('点击「类型」切换内容区 + 写入 localStorage（§2.5.3）', async () => {
    localStorage.setItem(LS_KEY, 'platform')
    localStorage.setItem(LS_DEFAULT_REV_KEY, DEFAULT_REV)
    render(<InfoView />)
    expect(screen.getByTestId('channels-view')).toBeInTheDocument()
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '类型' }))
    expect(screen.getByTestId('info-category-view')).toBeInTheDocument()
    expect(screen.queryByTestId('channels-view')).toBeNull()
    expect(localStorage.getItem(LS_KEY)).toBe('category')
  })

  it('点击「来源」从 category 切回 platform + 写入 localStorage（§2.5.4）', async () => {
    localStorage.setItem(LS_KEY, 'category')
    localStorage.setItem(LS_DEFAULT_REV_KEY, DEFAULT_REV)
    render(<InfoView />)
    expect(screen.getByTestId('info-category-view')).toBeInTheDocument()
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '来源' }))
    expect(screen.getByTestId('channels-view')).toBeInTheDocument()
    expect(localStorage.getItem(LS_KEY)).toBe('platform')
  })

  it('localStorage 含无效值「invalid」时 fallback 到默认 category', () => {
    localStorage.setItem(LS_KEY, 'invalid')
    render(<InfoView />)
    expect(screen.getByTestId('info-category-view')).toBeInTheDocument()
    const toggle = screen.getByRole('button', { name: '类型' })
    expect(toggle).toHaveAttribute('aria-pressed', 'true')
  })

  it('localStorage 含「undefined」字符串时 fallback 到默认 category（异常输入防御）', () => {
    localStorage.setItem(LS_KEY, 'undefined')
    render(<InfoView />)
    expect(screen.getByTestId('info-category-view')).toBeInTheDocument()
  })

  it('localStorage.setItem 抛异常时不传播错误，UI 仍切换（§2.5.E3 隐私模式）', async () => {
    localStorage.setItem(LS_KEY, 'platform')
    localStorage.setItem(LS_DEFAULT_REV_KEY, DEFAULT_REV)
    const original = Storage.prototype.setItem
    Storage.prototype.setItem = vi.fn(() => {
      throw new DOMException('QuotaExceededError')
    })
    try {
      render(<InfoView />)
      const user = userEvent.setup()
      await user.click(screen.getByRole('button', { name: '类型' }))
      expect(screen.getByTestId('info-category-view')).toBeInTheDocument()
    } finally {
      Storage.prototype.setItem = original
    }
  })

  it('localStorage.getItem 抛异常时 fallback 到默认 category（§2.5.E3）', () => {
    const original = Storage.prototype.getItem
    Storage.prototype.getItem = vi.fn(() => {
      throw new DOMException('SecurityError')
    })
    try {
      render(<InfoView />)
      expect(screen.getByTestId('info-category-view')).toBeInTheDocument()
    } finally {
      Storage.prototype.getItem = original
    }
  })
})
