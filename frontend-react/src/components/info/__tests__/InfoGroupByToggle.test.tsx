/**
 * v18.0 Spec-2.5 rev6：InfoGroupByToggle 组件测试（L2 静默文字切换）。
 *
 * 覆盖：
 *   - 默认渲染 2 个按钮，「来源」「类型」，组件内部不渲染分隔符
 *   - 选中态 className 包含暖橙下划线 / 暖橙文字
 *   - 未选态 className 包含 text-muted-foreground hover:text-foreground
 *   - h-10 容器 + h-full 按钮，与右侧 L2 分组导航共用底部基准线
 *   - 点击触发 onChange（PRD §Spec-2.5.3 / 2.5.4）
 *   - 重复点击同一项不触发 onChange（无效切换防抖）
 *   - disabled 状态防重复点击（PRD §Spec-2.5.E1 + disabled:opacity-50）
 *   - aria-pressed 可访问性属性
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { InfoGroupByToggle } from '../InfoGroupByToggle'

describe('InfoGroupByToggle (rev6 - quiet underline switch)', () => {
  afterEach(() => cleanup())

  it('renders 2 个按钮，「来源」「类型」，组件内部不渲染分隔符', () => {
    render(<InfoGroupByToggle groupBy="platform" onChange={vi.fn()} />)
    const sourceBtn = screen.getByRole('button', { name: '来源' })
    const typeBtn = screen.getByRole('button', { name: '类型' })
    expect(sourceBtn).toBeInTheDocument()
    expect(typeBtn).toBeInTheDocument()
    expect(screen.queryByText('｜')).toBeNull()
    expect(screen.queryByText('|')).toBeNull()
  })

  it('groupBy=platform 时「来源」aria-pressed=true，「类型」aria-pressed=false', () => {
    render(<InfoGroupByToggle groupBy="platform" onChange={vi.fn()} />)
    const sourceBtn = screen.getByRole('button', { name: '来源' })
    const typeBtn = screen.getByRole('button', { name: '类型' })
    expect(sourceBtn).toHaveAttribute('aria-pressed', 'true')
    expect(typeBtn).toHaveAttribute('aria-pressed', 'false')
  })

  it('groupBy=category 时「类型」高亮（PRD §Spec-2.5.2 持久化恢复后高亮）', () => {
    render(<InfoGroupByToggle groupBy="category" onChange={vi.fn()} />)
    const typeBtn = screen.getByRole('button', { name: '类型' })
    expect(typeBtn).toHaveAttribute('aria-pressed', 'true')
  })

  it('选中按钮 className 包含暖橙下划线 / 暖橙文字', () => {
    render(<InfoGroupByToggle groupBy="platform" onChange={vi.fn()} />)
    const sourceBtn = screen.getByRole('button', { name: '来源' })
    expect(sourceBtn.className).toContain('border-[var(--brand)]')
    expect(sourceBtn.className).toContain('text-[var(--brand)]')
    expect(sourceBtn.className).toContain('border-b-2')
    expect(sourceBtn.className).not.toContain('rounded-full')
  })

  it('未选按钮 className 包含 text-muted-foreground hover:text-foreground', () => {
    render(<InfoGroupByToggle groupBy="platform" onChange={vi.fn()} />)
    const typeBtn = screen.getByRole('button', { name: '类型' })
    expect(typeBtn.className).toContain('text-muted-foreground')
    expect(typeBtn.className).toContain('hover:text-foreground')
  })

  it('所有按钮形态使用信息页 L2 文字切换规格', () => {
    render(<InfoGroupByToggle groupBy="platform" onChange={vi.fn()} />)
    const buttons = [
      screen.getByRole('button', { name: '来源' }),
      screen.getByRole('button', { name: '类型' }),
    ]
    for (const btn of buttons) {
      expect(btn.className).toContain('h-full')
      expect(btn.className).toContain('border-b-2')
      expect(btn.className).toContain('text-[16px]')
      expect(btn.className).toContain('font-event-title')
      expect(btn.className).toContain('font-medium')
      expect(btn.className).not.toContain('rounded-full')
    }
  })

  it('点击未选中项触发 onChange(next)（PRD §Spec-2.5.3）', async () => {
    const onChange = vi.fn()
    render(<InfoGroupByToggle groupBy="platform" onChange={onChange} />)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '类型' }))
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith('category')
  })

  it('点击「来源」触发 platform 映射（PRD §Spec-2.5.4）', async () => {
    const onChange = vi.fn()
    render(<InfoGroupByToggle groupBy="category" onChange={onChange} />)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '来源' }))
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith('platform')
  })

  it('点击已选中项不触发 onChange（无效切换防抖）', async () => {
    const onChange = vi.fn()
    render(<InfoGroupByToggle groupBy="platform" onChange={onChange} />)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '来源' }))
    expect(onChange).not.toHaveBeenCalled()
  })

  it('disabled=true 时禁用所有 pill 并加 disabled:opacity-50（PRD §Spec-2.5.E1）', async () => {
    const onChange = vi.fn()
    render(<InfoGroupByToggle groupBy="platform" onChange={onChange} disabled />)
    const buttons = [
      screen.getByRole('button', { name: '来源' }),
      screen.getByRole('button', { name: '类型' }),
    ]
    for (const btn of buttons) {
      expect(btn).toBeDisabled()
      expect(btn.className).toContain('disabled:opacity-50')
    }
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '类型' }))
    expect(onChange).not.toHaveBeenCalled()
  })

  it('容器使用静默文字切换外框', () => {
    const { container } = render(<InfoGroupByToggle groupBy="platform" onChange={vi.fn()} />)
    const root = container.firstChild as HTMLElement
    expect(root.className).toContain('inline-flex')
    expect(root.className).toContain('h-10')
    expect(root.className).toContain('min-w-max')
    expect(root.className).toContain('gap-6')
    expect(root.className).toContain('sm:gap-8')
    expect(root.textContent).toContain('类型来源')
    expect(root.textContent).not.toContain('｜')
    expect(root.textContent).not.toContain('|')
    expect(root.className).not.toContain('rounded-full')
    expect(root.className).not.toContain('bg-card')
  })
})
