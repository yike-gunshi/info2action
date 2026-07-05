/**
 * v15.1 cluster-summary-parser 单测
 *
 * 覆盖：
 * - 完整双段（精华速览 + 全文拆解）→ hasDualSections=true，两段拆出
 * - 只有【精华速览】 → speedReview，hasDualSections=false（用于退化平铺）
 * - 只有【全文拆解】 → fullBreakdown，hasDualSections=false
 * - 都不存在 → speedReview=整段（兼容旧 prompt 平铺输出）
 * - 空 / null / undefined → 三段都空
 */
import { describe, it, expect } from 'vitest'
import { parseClusterBreakdownSections, parseClusterSummary } from '../cluster-summary-parser'

describe('parseClusterSummary', () => {
  it('双段命中时拆分 speedReview 和 fullBreakdown', () => {
    const summary =
      '【精华速览】\nAnthropic 发布 Claude 4.7 Max。\n\n' +
      '【全文拆解】\n1. 能力变化\n- 上下文 200k\n2. 来源差异\n- 官博给出价格细则'
    const r = parseClusterSummary(summary)
    expect(r.hasDualSections).toBe(true)
    expect(r.speedReview).toContain('Anthropic 发布 Claude 4.7 Max')
    expect(r.fullBreakdown).toContain('1. 能力变化')
    expect(r.fullBreakdown).toContain('2. 来源差异')
    // markers 本身不应该混进段内容
    expect(r.speedReview).not.toContain('【精华速览】')
    expect(r.fullBreakdown).not.toContain('【全文拆解】')
  })

  it('只有【精华速览】marker → speedReview 命中，hasDualSections=false', () => {
    const summary = '【精华速览】OpenAI 推出新模型，定价 $20/月。多源报道证实。'
    const r = parseClusterSummary(summary)
    expect(r.hasDualSections).toBe(false)
    expect(r.speedReview).toContain('OpenAI 推出新模型')
    expect(r.fullBreakdown).toBe(null)
  })

  it('只有【全文拆解】marker → fullBreakdown 命中', () => {
    const summary = '【全文拆解】\n1. 能力变化\n- 长上下文'
    const r = parseClusterSummary(summary)
    expect(r.hasDualSections).toBe(false)
    expect(r.speedReview).toBe(null)
    expect(r.fullBreakdown).toContain('1. 能力变化')
  })

  it('无速览 marker 但有【全文拆解】时, marker 前文本作为速览', () => {
    const summary =
      '《穿着Prada的恶魔2》在亚洲上映后引发争议。\n\n' +
      '【全文拆解】\n上映与争议背景\n- 多地观众反馈'
    const r = parseClusterSummary(summary)
    expect(r.speedReview).toBe('《穿着Prada的恶魔2》在亚洲上映后引发争议。')
    expect(r.fullBreakdown).toContain('上映与争议背景')
    expect(r.hasDualSections).toBe(true)
  })

  it('兼容【全文速览】marker', () => {
    const summary = '【全文速览】OpenAI 发布新功能。\n\n【全文拆解】\n1. 背景'
    const r = parseClusterSummary(summary)
    expect(r.speedReview).toBe('OpenAI 发布新功能。')
    expect(r.fullBreakdown).toContain('1. 背景')
    expect(r.hasDualSections).toBe(true)
  })

  it('无 markers（旧版 v15.0 平铺）→ 整段 fall back 到 speedReview', () => {
    const summary = 'Anthropic 发布 Claude 4.7 Max，多源报道集中在能力边界。'
    const r = parseClusterSummary(summary)
    expect(r.hasDualSections).toBe(false)
    expect(r.speedReview).toBe(summary)
    expect(r.fullBreakdown).toBe(null)
  })

  it('空字符串 / null / undefined → 三段都空', () => {
    expect(parseClusterSummary('')).toEqual({
      speedReview: null,
      fullBreakdown: null,
      hasDualSections: false,
    })
    expect(parseClusterSummary(null)).toEqual({
      speedReview: null,
      fullBreakdown: null,
      hasDualSections: false,
    })
    expect(parseClusterSummary(undefined)).toEqual({
      speedReview: null,
      fullBreakdown: null,
      hasDualSections: false,
    })
  })

  it('双段顺序异常（拆解 marker 在前）→ 不当作双段，整段当 speedReview', () => {
    // 防御性：如果 LLM 输出顺序倒了，不能误判为双段
    const summary = '【全文拆解】\n1. 主题\n\n【精华速览】 概述'
    const r = parseClusterSummary(summary)
    // 精华速览 marker 在拆解 marker 之后 → speedIdx > breakdownIdx → 不进入双段分支
    expect(r.hasDualSections).toBe(false)
  })
})

describe('parseClusterBreakdownSections', () => {
  it('把编号标题和 bullet 解析为纵向分点 section', () => {
    const sections = parseClusterBreakdownSections(
      '1. 困境现状\n' +
        '- 初期用户热情高涨，日活与互动频繁\n' +
        '- 使用一段时间后，互动频率显著下降\n\n' +
        '2. 依恋理论视角\n' +
        '- AI 通过“稳定可达”进入依恋体系\n' +
        '- 情绪回应一致，带来被理解的体验',
    )

    expect(sections).toEqual([
      {
        title: '困境现状',
        points: ['初期用户热情高涨，日活与互动频繁', '使用一段时间后，互动频率显著下降'],
      },
      {
        title: '依恋理论视角',
        points: ['AI 通过“稳定可达”进入依恋体系', '情绪回应一致，带来被理解的体验'],
      },
    ])
  })

  it('兼容 01 / 02 科技杂志编号格式', () => {
    const sections = parseClusterBreakdownSections(
      '01 困境现状\n• 初期用户热情高涨\n\n02 核心悖论\n• 过度确定性降低互动的不确定性',
    )

    expect(sections).toHaveLength(2)
    expect(sections[0].title).toBe('困境现状')
    expect(sections[0].points).toEqual(['初期用户热情高涨'])
    expect(sections[1].title).toBe('核心悖论')
    expect(sections[1].points).toEqual(['过度确定性降低互动的不确定性'])
  })

  it('兼容真实 cluster 的标题行 + bullet 拆解格式', () => {
    const sections = parseClusterBreakdownSections(
      '**困境现状：用户“失宠”非技术问题**\n' +
        '- 行业将 AI 陪伴产品使用率下降归因于新鲜感消退\n' +
        '- 深层原因指向用户心理机制与产品设计逻辑\n\n' +
        '**依恋理论视角：AI 如何成为“安全基地”**\n' +
        '- 依恋理论由鲍尔比提出\n' +
        '- 核心是个体与重要他人形成稳定心理联结\n\n' +
        '**核心悖论：安全感消解互动动力**\n' +
        '- 过度确定性让关系趋于可预测',
    )

    expect(sections).toHaveLength(3)
    expect(sections[0].title).toBe('困境现状：用户“失宠”非技术问题')
    expect(sections[1].title).toBe('依恋理论视角：AI 如何成为“安全基地”')
    expect(sections[2].points).toEqual(['过度确定性让关系趋于可预测'])
  })

  it('没有可识别编号标题时返回空数组,由 UI 降级为轻量正文', () => {
    expect(parseClusterBreakdownSections('上映与争议背景\n- 多地观众反馈')).toEqual([])
    expect(parseClusterBreakdownSections(null)).toEqual([])
  })
})
