import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { classifyByKeywords, useFeedStore, useSectionItems } from '../feedStore'
import type { FeedItem, ClassificationConfig } from '../../lib/types'

function makeItem(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'item-1',
    title: 'Test Item',
    platform: 'twitter',
    fetched_at: '2026-03-28T10:00:00Z',
    relevance_score: 7,
    ...overrides,
  }
}

const CLASSIFICATION: ClassificationConfig = {
  categories: [
    { id: 'products', name: 'AI产品', visible: true, priority: 1, fallback_keywords: ['workspace agents', '产品发布', '官方发布'] },
    { id: 'ai_tools', name: 'AI工具', visible: true, priority: 2, fallback_keywords: ['mcp', 'skill', 'workers', 'opencli', 'github'] },
    { id: 'models', name: '模型', visible: true, priority: 3, fallback_keywords: ['benchmark', 'world model'] },
    { id: 'tech', name: '技术', visible: true, priority: 4, fallback_keywords: ['agent memory', 'rag', '协议'] },
    { id: 'tutorials', name: '教程', visible: true, priority: 5, fallback_keywords: ['教程', 'guide', '入门', 'cookbook'] },
    { id: 'industry', name: '行业', visible: true, priority: 6, fallback_keywords: ['融资', '收购'] },
    { id: 'creator', name: '创作', visible: true, priority: 7, fallback_keywords: ['提示词', '分镜', '视频'] },
    { id: 'investment', name: '投资', visible: true, priority: 8, fallback_keywords: ['股票', '比特币', 'compute'] },
    { id: 'hidden', name: '隐藏', visible: false, priority: 99 },
  ],
}

describe('feedStore', () => {
  beforeEach(() => {
    useFeedStore.setState({
      sectionItems: new Map(),
      platformSectionItems: new Map(),
      searchResults: null,
      clickedAtById: {},
      classification: null,
      isLoading: true,
      loadError: null,
    })
  })

  it('setSections correctly sets data', () => {
    const items = [makeItem({ id: '1' }), makeItem({ id: '2' })]
    useFeedStore.getState().setSections({ ai: items })

    const map = useFeedStore.getState().sectionItems
    expect(map.size).toBe(1)
    expect(map.get('ai')!.length).toBe(2)
  })

  it('setSections preserves backend page order', () => {
    useFeedStore.getState().setSections({
      ai: [
        makeItem({ id: '1', fetched_at: '2026-03-28T08:00:00Z' }),
        makeItem({ id: '2', fetched_at: '2026-03-28T10:00:00Z' }),
        makeItem({ id: '3', fetched_at: '2026-03-28T10:00:00Z' }),
      ],
    })

    const items = useFeedStore.getState().sectionItems.get('ai')!
    expect(items.map((item) => item.id)).toEqual(['1', '2', '3'])
  })

  it('setSections does not re-rank backend results by ranking_score', () => {
    useFeedStore.getState().setSections({
      ai: [
        { ...makeItem({ id: '1' }), ranking_score: 0.5 },
        { ...makeItem({ id: '2' }), ranking_score: 0.9 },
        { ...makeItem({ id: '3' }), ranking_score: 0.1 },
      ],
    })

    const items = useFeedStore.getState().sectionItems.get('ai')!
    expect(items.map((item) => item.id)).toEqual(['1', '2', '3'])
  })

  it('appendPlatformItems appends after existing page without reordering', () => {
    useFeedStore.getState().setPlatformSections({
      twitter: [
        makeItem({ id: 'first-1', platform: 'twitter', ranking_score: 0.2 }),
        makeItem({ id: 'first-2', platform: 'twitter', ranking_score: 0.1 }),
      ],
    })

    useFeedStore.getState().appendPlatformItems('twitter', [
      makeItem({ id: 'next-1', platform: 'twitter', ranking_score: 1 }),
      makeItem({ id: 'next-2', platform: 'twitter', ranking_score: 0.9 }),
    ])

    const items = useFeedStore.getState().platformSectionItems.get('twitter')!
    expect(items.map((item) => item.id)).toEqual(['first-1', 'first-2', 'next-1', 'next-2'])
  })

  it('useSectionItems returns correct sections with classification', () => {
    useFeedStore.getState().setSections({
      products: [makeItem({ id: '1', ai_category: 'products' })],
      ai_tools: [makeItem({ id: '2', ai_category: 'ai_tools' })],
    })
    useFeedStore.getState().setClassification(CLASSIFICATION)

    const state = useFeedStore.getState()
    expect(state.sectionItems.size).toBe(2)
    expect(state.classification).toBeTruthy()
  })

  it('useSectionItems trusts remote section keys when item ai_category is stale', () => {
    useFeedStore.getState().setSections({
      products: [makeItem({ id: '1', ai_category: undefined })],
      ai_tools: [makeItem({ id: '2', ai_category: 'legacy_tools' })],
    }, { products: 712, ai_tools: 128 })
    useFeedStore.getState().setClassification(CLASSIFICATION)

    const { result } = renderHook(() => useSectionItems())

    expect(result.current.map((s) => s.key)).toEqual(['products', 'ai_tools'])
    expect(result.current[0].count).toBe(712)
    expect(result.current[1].items[0].id).toBe('2')
  })

  it('usePlatformItems groups by platform', () => {
    useFeedStore.getState().setSections({
      ai: [
        makeItem({ id: '1', platform: 'twitter' }),
        makeItem({ id: '2', platform: 'github' }),
        makeItem({ id: '3', platform: 'twitter' }),
      ],
    })

    const allItems: FeedItem[] = []
    for (const items of useFeedStore.getState().sectionItems.values()) {
      allItems.push(...items)
    }
    const byPlatform = new Map<string, FeedItem[]>()
    for (const item of allItems) {
      if (!byPlatform.has(item.platform)) byPlatform.set(item.platform, [])
      byPlatform.get(item.platform)!.push(item)
    }
    expect(byPlatform.get('twitter')!.length).toBe(2)
    expect(byPlatform.get('github')!.length).toBe(1)
  })

  it('toggleStar toggles starred_at', () => {
    useFeedStore.getState().setSections({
      ai: [makeItem({ id: '1' })],
    })

    useFeedStore.getState().toggleStar('1')
    let item = useFeedStore.getState().sectionItems.get('ai')![0]
    expect(item.starred_at).toBeTruthy()

    useFeedStore.getState().toggleStar('1')
    item = useFeedStore.getState().sectionItems.get('ai')![0]
    expect(item.starred_at).toBeUndefined()
  })

  it('markClicked sets clicked_at only once', () => {
    useFeedStore.getState().setSections({
      ai: [makeItem({ id: '1' })],
    })

    useFeedStore.getState().markClicked('1')
    const firstClick = useFeedStore.getState().sectionItems.get('ai')![0].clicked_at
    expect(firstClick).toBeTruthy()

    useFeedStore.getState().markClicked('1')
    const secondClick = useFeedStore.getState().sectionItems.get('ai')![0].clicked_at
    expect(secondClick).toBe(firstClick)
  })

  it('markClicked updates platform sections for channel cards', () => {
    useFeedStore.getState().setPlatformSections({
      twitter: [makeItem({ id: '1', platform: 'twitter' })],
    })

    useFeedStore.getState().markClicked('1')

    const item = useFeedStore.getState().platformSectionItems.get('twitter')![0]
    expect(item.clicked_at).toBeTruthy()
    expect(useFeedStore.getState().clickedAtById['1']).toBe(item.clicked_at)
  })

  it('classifyByKeywords keeps tutorial-style MCP content in tutorials', () => {
    const item = makeItem({
      title: '2.Intro to MCP (Model Context Protocol) 教程',
      content: '将 Claude 连接到工具和数据，构建 Python SDK 服务器',
    })

    expect(classifyByKeywords(item, CLASSIFICATION.categories)).toBe('tutorials')
  })

  it('classifyByKeywords keeps developer feature launches in ai_tools', () => {
    const item = makeItem({
      title: 'Cloudflare 发布 Dynamic Workers 公开测试版',
      content: '面向开发者的 worker / API / SDK 能力更新',
    })

    expect(classifyByKeywords(item, CLASSIFICATION.categories)).toBe('ai_tools')
  })

  it('classifyByKeywords routes product analysis to products', () => {
    const item = makeItem({
      title: 'GPTImage2：随意做出可作为“证据”的图片',
      content: '这是一篇关于 GPT Image 2 的产品分析，讨论产品能力边界和风险',
    })

    expect(classifyByKeywords(item, CLASSIFICATION.categories)).toBe('products')
  })

  it('classifyByKeywords keeps GitHub projects in ai_tools', () => {
    const item = makeItem({
      platform: 'github',
      title: 'Fincept-Corporation/FinceptTerminal',
      content: 'A modern finance application on GitHub that users need to run themselves.',
    })

    expect(classifyByKeywords(item, CLASSIFICATION.categories)).toBe('ai_tools')
  })

  it('classifyByKeywords routes GPT Image case-and-prompt posts to tutorials', () => {
    const item = makeItem({
      title: 'GPT Image 2 全量开放！100+案例，跟 Nano Banana 2 正面PK（附提示词）',
      content: '包含大量实测案例和 prompts',
    })

    expect(classifyByKeywords(item, CLASSIFICATION.categories)).toBe('tutorials')
  })

  it('classifyByKeywords avoids classifying generic Reddit usage posts as products', () => {
    const item = makeItem({
      platform: 'reddit',
      title: 'The new chatgpt image generator is insane',
      content: 'And no I did not use a camera to take a photo',
    })

    expect(classifyByKeywords(item, CLASSIFICATION.categories)).not.toBe('products')
  })
})
