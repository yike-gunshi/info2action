/**
 * v5b ClusterSummaryBlock 视觉测试
 *
 * 设计:
 * - v5b summary 可能是【精华速览】+【全文拆解】双段
 * - summary 存在时，以 summary 为准，不再重复渲染 keyPoints
 * - 只有无 summary 时，keyPoints 作为历史兜底渲染
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { ClusterSummaryBlock } from '../ClusterSummaryBlock'

const NEW_SUMMARY = 'Anthropic 发布 **Claude 4.7 Max**,上下文 **200k**,定价不变。多源报道证实新模型在长上下文和代码生成上显著提升。'

// BF-0428-2 期间生成的旧 cluster summary 形态(双段 markers)
const LEGACY_DUAL_SUMMARY =
  '【精华速览】\nAnthropic 发布 Claude 4.7 Max,上下文 200k,定价不变。\n\n' +
  '【全文拆解】\n- 长上下文 200k,代码跑分超 4.6\n' +
  '- 官博定价细则、社区主要测代码生成'

describe('ClusterSummaryBlock (v5b)', () => {
  afterEach(cleanup)

  it('单段 summary + keyPoints → 渲染平铺速览,不重复渲染 keyPoints', () => {
    render(
      <ClusterSummaryBlock
        summary={NEW_SUMMARY}
        keyPoints={['要点 A', '要点 B', '要点 C']}
      />
    )
    expect(screen.getByTestId('cluster-summary-flat')).toBeInTheDocument()
    expect(screen.getByText(/Anthropic 发布/)).toBeInTheDocument()
    expect(screen.queryByTestId('cluster-key-points')).toBeNull()
    expect(screen.queryByTestId('cluster-full-breakdown')).toBeNull()
  })

  it('双段 markers → 渲染速览段 + 全文拆解段', () => {
    render(<ClusterSummaryBlock summary={LEGACY_DUAL_SUMMARY} keyPoints={['kp1']} />)
    const speed = screen.getByTestId('cluster-speed-review')
    const breakdown = screen.getByTestId('cluster-full-breakdown')
    expect(speed).toBeInTheDocument()
    expect(breakdown).toBeInTheDocument()
    expect(speed).toHaveClass('text-[16px]', 'leading-[1.7]')
    expect(breakdown).toHaveClass('text-[16px]', 'leading-[1.7]')
    expect(speed.textContent).not.toContain('【精华速览】')
    expect(speed.textContent).toContain('Anthropic 发布')
    expect(speed.textContent).not.toContain('长上下文 200k,代码跑分')
    expect(breakdown.textContent).toContain('长上下文 200k,代码跑分')
    expect(breakdown.textContent).toContain('官博定价细则')
    expect(screen.queryByTestId('cluster-key-points')).toBeNull()
  })

  it('双段 summary 的速览与全文拆解之间有分割线', () => {
    const { container } = render(
      <ClusterSummaryBlock summary={LEGACY_DUAL_SUMMARY} keyPoints={['kp1', 'kp2']} />
    )
    const dividers = container.querySelectorAll('.border-t.border-primary\\/10')
    expect(dividers.length).toBeGreaterThanOrEqual(1)
  })

  it('只有 summary 无 keyPoints → 不渲染分割线和列表', () => {
    const { container } = render(<ClusterSummaryBlock summary={NEW_SUMMARY} />)
    expect(screen.getByTestId('cluster-summary-flat')).toHaveClass('text-[16px]', 'leading-[1.7]')
    expect(screen.queryByTestId('cluster-key-points')).toBeNull()
    const dividers = container.querySelectorAll('.border-t.border-primary\\/10')
    expect(dividers.length).toBe(0)
  })

  it('只有 keyPoints 无 summary(V14.0 fallback)→ 不渲染速览,只渲染紫点列表', () => {
    render(<ClusterSummaryBlock summary="" keyPoints={['kp1', 'kp2', 'kp3']} />)
    expect(screen.queryByTestId('cluster-speed-review')).toBeNull()
    expect(screen.getByTestId('cluster-key-points')).toBeInTheDocument()
    expect(screen.getByText('kp1')).toBeInTheDocument()
    const dots = screen.getByTestId('cluster-summary-block')
      .querySelectorAll('.w-1\\.5.h-1\\.5')
    expect(dots.length).toBe(3)
  })

  it('全空(无 summary 也无 keyPoints)→ 完全不渲染', () => {
    const { container } = render(<ClusterSummaryBlock summary={null} />)
    expect(container.firstChild).toBeNull()
  })

  it('keyPoints 中的 markdown bold 用 markdown-lite 渲染(剥 ** 标记 + 加粗)', () => {
    render(<ClusterSummaryBlock summary="" keyPoints={['**关键产品** 已发布']} />)
    const li = screen.getByText(/已发布/)
    // 文字本身不应含 **
    expect(li.textContent).not.toContain('**关键产品**')
    expect(li.textContent).toContain('关键产品')
  })

  it('summary 存在时,嵌套 keyPoints 不重复渲染', () => {
    render(
      <ClusterSummaryBlock
        summary={NEW_SUMMARY}
        keyPoints={[
          { title: '产品上线', points: ['HappyHorse 1.0 灰测', '阿里云百炼平台开放'] },
          { title: '模型能力', points: ['150 亿参数 Transformer', '音画一体生成'] },
        ]}
      />,
    )
    expect(screen.queryByText('产品上线')).toBeNull()
    expect(screen.queryByText('HappyHorse 1.0 灰测')).toBeNull()
  })

  it('无 summary 时,嵌套 keyPoints [{title, points: []}] → 渲染加粗小标题 + sub-紫点', () => {
    render(
      <ClusterSummaryBlock
        summary=""
        keyPoints={[
          { title: '产品上线', points: ['HappyHorse 1.0 灰测', '阿里云百炼平台开放'] },
          { title: '模型能力', points: ['150 亿参数 Transformer', '音画一体生成'] },
        ]}
      />,
    )
    expect(screen.getByText('产品上线')).toBeInTheDocument()
    expect(screen.getByText('模型能力')).toBeInTheDocument()
    expect(screen.getByText('HappyHorse 1.0 灰测')).toBeInTheDocument()
    expect(screen.getByText('150 亿参数 Transformer')).toBeInTheDocument()
    const title1 = screen.getByText('产品上线')
    const titleParent = title1.closest('div')
    expect(titleParent?.className).toContain('font-semibold')
  })

  it('无 summary 时,嵌套 + 扁平 mixed keyPoints → 都正确渲染(向后兼容)', () => {
    render(
      <ClusterSummaryBlock
        summary=""
        keyPoints={[
          '扁平要点 A(BF-0428-4 旧数据)',
          { title: '嵌套分组', points: ['sub 1', 'sub 2'] },
        ]}
      />,
    )
    expect(screen.getByText('扁平要点 A(BF-0428-4 旧数据)')).toBeInTheDocument()
    expect(screen.getByText('嵌套分组')).toBeInTheDocument()
    expect(screen.getByText('sub 1')).toBeInTheDocument()
  })

  it('BF-0428-5: 嵌套 keyPoints sub-points 数量 = sub-紫点数量', () => {
    render(
      <ClusterSummaryBlock
        summary=""
        keyPoints={[
          { title: 'A', points: ['x', 'y', 'z'] },
          { title: 'B', points: ['p', 'q'] },
        ]}
      />,
    )
    const block = screen.getByTestId('cluster-summary-block')
    // 5 个 sub-points,各 1 紫点 dot;两个 title 不带 dot → 共 5 dot
    const dots = block.querySelectorAll('.w-1\\.5.h-1\\.5')
    expect(dots.length).toBe(5)
  })
})
