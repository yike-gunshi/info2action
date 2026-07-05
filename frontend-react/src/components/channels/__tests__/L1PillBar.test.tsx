import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { L1PillBar } from '../L1PillBar'

const LABELS = {
  coding: 'Coding',
  products: '产品',
  models: '模型',
  ai_tools: 'AI 工具',
}

describe('L1PillBar', () => {
  afterEach(() => cleanup())

  it('renders 全部 pill first followed by L1 pills sorted by count desc', () => {
    render(
      <L1PillBar
        platform="github"
        categoryCounts={{ coding: 12, products: 3, models: 25, ai_tools: 8 }}
        categoryLabels={LABELS}
        selectedCategory={null}
        onSelect={vi.fn()}
      />,
    )

    const buttons = screen.getAllByRole('button')
    // 「全部」 + 4 L1 pill
    expect(buttons).toHaveLength(5)
    expect(buttons[0]).toHaveTextContent('全部')
    // BF-0512-7: pill 纯 label 跟推荐页一致；cnt 移到 hover title attribute
    // count desc: models(25) → coding(12) → ai_tools(8) → products(3)
    expect(buttons[1].textContent?.trim()).toBe('模型')
    expect(buttons[1]).toHaveAttribute('title', '25 条')
    expect(buttons[2].textContent?.trim()).toBe('Coding')
    expect(buttons[2]).toHaveAttribute('title', '12 条')
    expect(buttons[3].textContent?.trim()).toBe('AI 工具')
    expect(buttons[4].textContent?.trim()).toBe('产品')
    // 反向断言: pill 文本不含数字（BF-0512-7 核心）
    for (const btn of buttons.slice(1)) {
      expect(btn.textContent?.trim()).not.toMatch(/\d/)
    }
  })

  it('hides L1 pills with count === 0', () => {
    render(
      <L1PillBar
        platform="reddit"
        categoryCounts={{ coding: 5, products: 0, models: 0, ai_tools: 3 }}
        categoryLabels={LABELS}
        selectedCategory={null}
        onSelect={vi.fn()}
      />,
    )

    const buttons = screen.getAllByRole('button')
    expect(buttons).toHaveLength(3) // 全部 + coding + ai_tools
    expect(screen.queryByText(/产品/)).toBeNull()
    expect(screen.queryByText(/^模型/)).toBeNull()
  })

  it('clicking an L1 pill triggers onSelect with the category id', async () => {
    const onSelect = vi.fn()
    const user = userEvent.setup()
    render(
      <L1PillBar
        platform="rss"
        categoryCounts={{ coding: 12, products: 3 }}
        categoryLabels={LABELS}
        selectedCategory={null}
        onSelect={onSelect}
      />,
    )

    await user.click(screen.getByText('Coding'))
    expect(onSelect).toHaveBeenCalledWith('coding')
  })

  it('clicking the currently selected L1 pill toggles back to null', async () => {
    const onSelect = vi.fn()
    const user = userEvent.setup()
    render(
      <L1PillBar
        platform="hackernews"
        categoryCounts={{ coding: 12, products: 3 }}
        categoryLabels={LABELS}
        selectedCategory="coding"
        onSelect={onSelect}
      />,
    )

    await user.click(screen.getByText('Coding'))
    expect(onSelect).toHaveBeenCalledWith(null)
  })

  it('clicking 全部 pill triggers onSelect(null)', async () => {
    const onSelect = vi.fn()
    const user = userEvent.setup()
    render(
      <L1PillBar
        platform="manual"
        categoryCounts={{ coding: 12 }}
        categoryLabels={LABELS}
        selectedCategory="coding"
        onSelect={onSelect}
      />,
    )

    await user.click(screen.getByText('全部'))
    expect(onSelect).toHaveBeenCalledWith(null)
  })

  it('「全部」 pill is highlighted when selectedCategory is null', () => {
    render(
      <L1PillBar
        platform="waytoagi"
        categoryCounts={{ coding: 12, products: 3 }}
        categoryLabels={LABELS}
        selectedCategory={null}
        onSelect={vi.fn()}
      />,
    )

    const allBtn = screen.getByText('全部')
    expect(allBtn.className).toContain('border-[var(--brand)]')
    expect(allBtn.className).toContain('text-[var(--brand)]')
    expect(allBtn.className).not.toContain('rounded-full')
    expect(allBtn.className).not.toContain('bg-foreground')
    expect(screen.getByTestId('info-section-pill-bar-waytoagi').className).not.toContain('sticky')
  })

  it('renders nothing when no L1 has positive count', () => {
    const { container } = render(
      <L1PillBar
        platform="github"
        categoryCounts={{ coding: 0, products: 0 }}
        categoryLabels={LABELS}
        selectedCategory={null}
        onSelect={vi.fn()}
      />,
    )
    expect(container.firstChild).toBeNull()
  })

  // BF-0512-5: pill 排序按 categoryOrder（推荐页 L1 顺序）
  it('orders L1 pills by categoryOrder when provided (matches recommend view)', () => {
    render(
      <L1PillBar
        platform="github"
        categoryCounts={{ coding: 12, products: 3, models: 25, ai_tools: 8 }}
        categoryLabels={LABELS}
        // 推荐页顺序: products → coding → models → ai_tools
        categoryOrder={['products', 'coding', 'models', 'ai_tools']}
        selectedCategory={null}
        onSelect={vi.fn()}
      />,
    )
    const buttons = screen.getAllByRole('button')
    expect(buttons).toHaveLength(5)
    expect(buttons[0]).toHaveTextContent('全部')
    // 跟 categoryOrder 顺序一致，不是 cnt DESC
    expect(buttons[1].textContent?.trim()).toBe('产品')
    expect(buttons[2].textContent?.trim()).toBe('Coding')
    expect(buttons[3].textContent?.trim()).toBe('模型')
    expect(buttons[4].textContent?.trim()).toBe('AI 工具')
  })

  // BF-0512-5: categoryOrder 缺省 → 兜底回 cnt DESC
  it('falls back to count desc when categoryOrder is empty (backward compat)', () => {
    render(
      <L1PillBar
        platform="github"
        categoryCounts={{ coding: 12, products: 3, models: 25 }}
        categoryLabels={LABELS}
        categoryOrder={[]}
        selectedCategory={null}
        onSelect={vi.fn()}
      />,
    )
    const buttons = screen.getAllByRole('button')
    expect(buttons[1].textContent?.trim()).toBe('模型')
    expect(buttons[2].textContent?.trim()).toBe('Coding')
    expect(buttons[3].textContent?.trim()).toBe('产品')
  })

  // BF-0512-5: 不在 categoryOrder 中的 L1 fallback 到末尾按 cnt DESC
  it('puts unknown L1 (not in categoryOrder) at the end sorted by count', () => {
    render(
      <L1PillBar
        platform="reddit"
        categoryCounts={{ coding: 5, models: 10, unknown_l1: 20, weird_cat: 7 }}
        categoryLabels={{ ...LABELS, unknown_l1: '未知 1', weird_cat: '怪类' }}
        categoryOrder={['models', 'coding']}  // 只 2 个 known，2 个 unknown
        selectedCategory={null}
        onSelect={vi.fn()}
      />,
    )
    const buttons = screen.getAllByRole('button')
    expect(buttons).toHaveLength(5)
    // models 在 order 第 0 → 第 1 个
    expect(buttons[1].textContent?.trim()).toBe('模型')
    // coding 在 order 第 1 → 第 2 个
    expect(buttons[2].textContent?.trim()).toBe('Coding')
    // unknown_l1 (cnt 20) > weird_cat (cnt 7) → 末尾按 cnt DESC
    expect(buttons[3].textContent?.trim()).toBe('未知 1')
    expect(buttons[4].textContent?.trim()).toBe('怪类')
  })

  // BF-0512-7 rev2: 所有 pill（含「未分类」）纯 label 无数字，cnt 全部移到 hover tooltip
  it('renders all pills as pure label including uncategorized (cnt only in tooltip)', () => {
    render(
      <L1PillBar
        platform="github"
        categoryCounts={{ coding: 12, __uncategorized__: 321 }}
        categoryLabels={LABELS}
        selectedCategory={null}
        onSelect={vi.fn()}
      />,
    )
    const buttons = screen.getAllByRole('button')
    expect(buttons).toHaveLength(3)
    expect(buttons[0]).toHaveTextContent('全部')
    // Coding pill: 纯 label
    expect(buttons[1].textContent?.trim()).toBe('Coding')
    expect(buttons[1]).toHaveAttribute('title', '12 条')
    // 「未分类」pill: 也纯 label（rev2 取消保留数字例外）
    expect(buttons[2].textContent?.trim()).toBe('未分类')
    expect(buttons[2]).not.toHaveTextContent('321')
    // 但 tooltip 仍含 321 (兜底信息)
    expect(buttons[2]).toHaveAttribute('title', expect.stringContaining('321'))
  })

  it('falls back to the L1 id as label when categoryLabels has no entry', () => {
    render(
      <L1PillBar
        platform="github"
        categoryCounts={{ unknown_category: 5 }}
        categoryLabels={{}}
        selectedCategory={null}
        onSelect={vi.fn()}
      />,
    )
    expect(screen.getByText(/unknown_category/)).toBeTruthy()
  })
})
