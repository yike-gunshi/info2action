import { useMemo } from 'react'
import { create } from 'zustand'
import { toast } from 'sonner'
import type { FeedItem, ClassificationConfig, FeedSection, InfoReadModelCursor } from '../lib/types'
import { triggerFetchAll, fetchFetchStatus, fetchFeedSections, fetchFeedPlatforms, type FetchProgress, type FetchProgressStage } from '../lib/api'
// Search is now server-side — no need for useUIStore

interface FeedState {
  // Pre-grouped data by ai_category (from /api/feed/sections)
  sectionItems: Map<string, FeedItem[]>
  // Real total counts per category (from server)
  catCounts: Record<string, number>
  sectionReadModelVersionId: string | null
  sectionNextCursors: Record<string, InfoReadModelCursor | null>
  // Server-side search results (replaces sectionItems when active)
  searchResults: Map<string, FeedItem[]> | null
  searchTotal: number
  searchCatCounts: Record<string, number>
  searchPlatformSectionItems: Map<string, FeedItem[]> | null
  searchPlatformCounts: Record<string, number>
  searchSourceCounts: Record<string, Record<string, number>>
  searchPlatformCategoryCounts: Record<string, Record<string, number>>
  searchPlatformLoading: boolean
  // BF-0704-6: 搜索降级/失败时为 true,UI 显示提示而非静默
  searchDegraded: boolean
  isSearching: boolean
  // Classification config
  classification: ClassificationConfig | null
  // Loading state
  isLoading: boolean
  loadError: string | null
  // Pre-grouped data by platform (from /api/feed/platforms)
  platformSectionItems: Map<string, FeedItem[]>
  platformCounts: Record<string, number>
  sourceCounts: Record<string, Record<string, number>>
  // v16.0 W4.T9: 每 platform 的 L1 分布 {platform: {l1_id: count}} (W3.T7 后端按 7 天窗 + display_visibility 聚合)
  platformCategoryCounts: Record<string, Record<string, number>>
  platformSectionsLoaded: boolean
  platformReadModelVersionId: string | null
  platformNextCursors: Record<string, InfoReadModelCursor | null>
  // v16.0 W4.T9: 每 platform 当前选中的 L1 pill (null = 「全部」),用于 ChannelsView 状态持久化(切走再回保留选择)
  selectedCategory: Record<string, string | null>
  clickedAtById: Record<string, string>
  // Global fetch state
  isFetching: boolean
  fetchProgress: FetchProgress | null

  // Actions
  setSections: (
    sections: Record<string, FeedItem[]>,
    catCounts?: Record<string, number>,
    readModelVersionId?: string | null,
    sectionNextCursors?: Record<string, InfoReadModelCursor | null>,
  ) => void
  setPlatformSections: (
    sections: Record<string, FeedItem[]>,
    platformCounts?: Record<string, number>,
    sourceCounts?: Record<string, Record<string, number>>,
    categoryCounts?: Record<string, Record<string, number>>,
    readModelVersionId?: string | null,
    platformNextCursors?: Record<string, InfoReadModelCursor | null>,
  ) => void
  /** v16.0 W4.T9: 设置某 platform 当前选中的 L1 pill (null = 「全部」) */
  setSelectedCategory: (platform: string, category: string | null) => void
  setClassification: (clf: ClassificationConfig) => void
  setIsLoading: (loading: boolean) => void
  setLoadError: (error: string | null) => void
  toggleStar: (itemId: string) => void
  markClicked: (itemId: string) => void
  // v12.2: Twitter 视频 ASR 完成后回写 ai_summary, 列表卡片摘要同步刷新
  updateItemSummary: (itemId: string, newSummary: string) => void
  appendCategoryItems: (category: string, items: FeedItem[]) => void
  appendPlatformItems: (platform: string, items: FeedItem[]) => void
  ensurePlatformSections: () => Promise<void>
  serverSearch: (query: string) => void
  clearSearch: () => void
  startFetch: () => void
  initFetchStatus: () => void
}

/** Fallback ordering for flat mixed-item views that do not have a server page order. */
function sortItems(a: FeedItem, b: FeedItem): number {
  const aRank = a.ranking_score ?? 0
  const bRank = b.ranking_score ?? 0
  if (aRank !== bRank) return bRank - aRank
  const aTime = new Date(a.fetched_at).getTime()
  const bTime = new Date(b.fetched_at).getTime()
  return bTime - aTime
}

function mapWithUpdatedItem(
  sectionItems: Map<string, FeedItem[]>,
  itemId: string,
  updater: (item: FeedItem) => FeedItem,
): Map<string, FeedItem[]> {
  // FE-1(B7): 只重建包含该 item 的 section,其余复用原数组引用——
  // 原实现对每个 section 都 map 出新数组,点一张卡导致全部 section 的
  // visibleItems memo 失效 → 全页重渲染。
  let changed = false
  const newMap = new Map<string, FeedItem[]>()
  for (const [key, items] of sectionItems) {
    if (items.some((it) => it.id === itemId)) {
      newMap.set(key, items.map((it) => (it.id === itemId ? updater(it) : it)))
      changed = true
    } else {
      newMap.set(key, items)
    }
  }
  return changed ? newMap : sectionItems
}

let _pollTimer: ReturnType<typeof setInterval> | null = null

const TUTORIAL_HINTS = [
  '教程',
  '指南',
  'guide',
  'how to',
  '入门',
  '保姆级',
  'cookbook',
  'best practice',
  'best practices',
  '实战',
  '从零',
  '案例',
  'case',
  'cases',
  '提示词',
  'prompt',
  'prompts',
  '实测',
  '效果对比',
  '正面pk',
  '玩法',
]

const PRODUCT_HINTS = [
  '官方发布',
  '产品发布',
  '新品发布',
  '正式发布',
  '正式上线',
  'official launch',
  'launches',
  'beta',
  'preview',
  '新功能',
  '功能更新',
  '正式版',
  '产品分析',
  '产品测评',
  '竞品分析',
]

const MODEL_HINTS = [
  'benchmark',
  'benchmarks',
  'eval',
  'evaluation',
  'cost analysis',
  '成本分析',
  '模型发布',
  'world model',
  'image model',
  'sota',
  '论文',
]

const DEV_TOOL_HINTS = [
  'mcp',
  'sdk',
  'api',
  'worker',
  'workers',
  'plugin',
  '插件',
  '框架',
  'agent',
  'cli',
  'devtools',
  'github',
  '开源',
  'skill',
  'skills',
  'cursor',
  'claude code',
]

const PRODUCT_EXCLUSION_HINTS = [
  'github',
  'repository',
  'repo',
  'cli',
  'sdk',
  'api',
  '自行部署',
  '开源',
  'prompt',
  '提示词',
  'case',
  '案例',
  '实测',
]

const STARTING_FETCH_PROGRESS: FetchProgress = {
  mode: 'global',
  stages: [
    { id: 'source_fetch', name: '抓取来源', status: 'running', platform: '全部平台', percent: 0 },
    { id: 'ingest', name: '入库处理', status: 'pending', platform: '全部平台', percent: 35 },
    { id: 'ai_enrich', name: 'AI 统一理解', status: 'pending', platform: '全部平台', percent: 50 },
    { id: 'event_cluster', name: '事件聚合', status: 'pending', platform: '全部平台', percent: 80 },
  ],
  current_stage: 0,
  total_new: 0,
  platform: '全部平台',
  percent: 0,
  result_status: 'running',
}

function cloneStartingFetchProgress(): FetchProgress {
  return {
    ...STARTING_FETCH_PROGRESS,
    stages: STARTING_FETCH_PROGRESS.stages.map((stage) => ({ ...stage })),
  }
}

function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.max(0, Math.min(100, Math.round(value)))
}

function stageLabel(stage?: FetchProgressStage): string {
  const id = stage?.id
  const name = stage?.name ?? ''
  if (id === 'ingest' || name.includes('入库')) return '入库中'
  if (id === 'ai_enrich' || name.includes('AI')) return 'AI 总结中'
  if (id === 'event_cluster' || name.includes('事件聚合')) return '事件聚合中'
  return '抓取中'
}

function fallbackStagePercent(stage?: FetchProgressStage, currentStage = 0): number {
  const id = stage?.id
  const name = stage?.name ?? ''
  if (id === 'event_cluster' || name.includes('事件聚合')) return 85
  if (id === 'ai_enrich' || name.includes('AI')) return 60
  if (id === 'ingest' || name.includes('入库')) return 45
  if (currentStage >= 3) return 60
  if (currentStage >= 2) return 45
  return 20
}

function currentFetchStage(progress: FetchProgress): FetchProgressStage | undefined {
  const running = progress.stages.find((stage) => stage.status === 'running')
  if (running) return running
  return progress.stages[progress.current_stage] ?? progress.stages[0]
}

export function formatFetchProgressLabel(progress: FetchProgress | null | undefined): string {
  if (!progress) return '抓取中 · 全部平台 · 0%'
  const stage = currentFetchStage(progress)
  const label = stageLabel(stage)
  const platform = stage?.platform || progress.platform || '全部平台'
  const percent = clampPercent(stage?.percent ?? progress.percent ?? fallbackStagePercent(stage, progress.current_stage))
  return `${label} · ${platform} · ${percent}%`
}

function includesAny(text: string, phrases: string[]): boolean {
  return phrases.some((phrase) => text.includes(phrase))
}

function countKeywordHits(text: string, keywords: string[] | undefined): number {
  if (!keywords?.length) return 0
  return keywords.reduce((count, keyword) => (text.includes(keyword.toLowerCase()) ? count + 1 : count), 0)
}

function pollUntilDone(set: (partial: Partial<FeedState>) => void, get: () => FeedState) {
  if (_pollTimer) return
  _pollTimer = setInterval(async () => {
    try {
      const status = await fetchFetchStatus()
      set({ fetchProgress: status.progress ?? get().fetchProgress })
      if (!status.running) {
        if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null }
        set({ isFetching: false, fetchProgress: status.progress ?? get().fetchProgress })

        // BF-0420-10: 按 progress 细分反馈
        const progress = status.progress
        const failedStage = progress?.stages.find((s) => s.status === 'failed')
        const warningStage = progress?.stages.find((s) => s.status === 'warning')
        const newCount = progress?.total_new ?? 0
        const resultStatus = progress?.result_status

        if (resultStatus === 'failed' || (!resultStatus && failedStage)) {
          toast.error(`抓取失败:${failedStage?.name || progress?.message || '未知阶段'}`)
        } else if (resultStatus === 'partial' || warningStage || failedStage) {
          const message = progress?.message || failedStage?.message || warningStage?.message || (newCount > 0 ? `已入库 ${newCount} 条` : '部分阶段未完成')
          toast.info(`抓取部分完成 · ${message}`)
        } else if (newCount > 0) {
          toast.success(`抓取完成 · 新增 ${newCount} 条`)
        } else {
          toast.info('抓取完成,暂无新内容')
        }

        // Refresh all feed surfaces affected by a global fetch; one surface
        // failing should not suppress the other surface's successful refresh.
        try {
          const requests = [
            fetchFeedSections(),
            get().platformSectionsLoaded ? fetchFeedPlatforms() : Promise.resolve(null),
          ] as const
          const [feedRes, platRes] = await Promise.allSettled(requests)
          if (feedRes.status === 'fulfilled') {
            get().setSections(
              feedRes.value.sections,
              feedRes.value.cat_counts,
              feedRes.value.read_model_version_id ?? null,
              feedRes.value.section_next_cursors,
            )
          }
          if (platRes.status === 'fulfilled' && platRes.value) {
            get().setPlatformSections(
              platRes.value.sections,
              platRes.value.platform_counts,
              platRes.value.source_counts,
              platRes.value.category_counts,
              platRes.value.read_model_version_id ?? null,
              platRes.value.platform_next_cursors,
            )
          }
        } catch { /* ignore */ }
      }
    } catch {
      // Network error — keep polling
    }
  }, 3000)
}

let _searchTimer: ReturnType<typeof setTimeout> | null = null
let _searchSeq = 0
let _platformSectionsPromise: Promise<void> | null = null

export const useFeedStore = create<FeedState>((set, get) => ({
  sectionItems: new Map(),
  catCounts: {},
  sectionReadModelVersionId: null,
  sectionNextCursors: {},
  platformSectionItems: new Map(),
  platformCounts: {},
  sourceCounts: {},
  platformCategoryCounts: {},
  platformSectionsLoaded: false,
  platformReadModelVersionId: null,
  platformNextCursors: {},
  selectedCategory: {},
  clickedAtById: {},
  searchResults: null,
  searchTotal: 0,
  searchCatCounts: {},
  searchPlatformSectionItems: null,
  searchPlatformCounts: {},
  searchSourceCounts: {},
  searchPlatformCategoryCounts: {},
  searchPlatformLoading: false,
  isSearching: false,
  searchDegraded: false,
  classification: null,
  isLoading: true,
  loadError: null,
  isFetching: false,
  fetchProgress: null,

  setSections: (sections, catCounts, readModelVersionId, sectionNextCursors) => {
    const map = new Map<string, FeedItem[]>()
    for (const [key, items] of Object.entries(sections)) {
      map.set(key, [...items])
    }
    set({
      sectionItems: map,
      loadError: null,
      sectionReadModelVersionId: readModelVersionId ?? null,
      sectionNextCursors: sectionNextCursors ?? {},
      ...(catCounts ? { catCounts } : {}),
    })
  },

  setPlatformSections: (sections, platformCounts, sourceCounts, categoryCounts, readModelVersionId, platformNextCursors) => {
    const map = new Map<string, FeedItem[]>()
    for (const [key, items] of Object.entries(sections)) {
      map.set(key, [...items])
    }
    set({
      platformSectionItems: map,
      ...(platformCounts ? { platformCounts } : {}),
      ...(sourceCounts ? { sourceCounts } : {}),
      ...(categoryCounts ? { platformCategoryCounts: categoryCounts } : {}),
      platformSectionsLoaded: true,
      platformReadModelVersionId: readModelVersionId ?? null,
      platformNextCursors: platformNextCursors ?? {},
    })
  },

  // v16.0 W4.T9: pill 切换不需要重新拉,但每 platform 选择独立隔离
  setSelectedCategory: (platform, category) => set((state) => ({
    selectedCategory: { ...state.selectedCategory, [platform]: category },
  })),

  setClassification: (classification) => set({ classification }),
  setIsLoading: (isLoading) => set({ isLoading }),
  setLoadError: (loadError) => set({ loadError }),

  toggleStar: (itemId) => set((state) => ({
    sectionItems: mapWithUpdatedItem(state.sectionItems, itemId, (it) => ({
      ...it,
      starred_at: it.starred_at ? undefined : new Date().toISOString(),
    })),
  })),

  markClicked: (itemId) => set((state) => {
    const clickedAt = state.clickedAtById[itemId] || new Date().toISOString()
    const updater = (it: FeedItem) => ({
      ...it,
      clicked_at: it.clicked_at || clickedAt,
    })
    return {
      clickedAtById: { ...state.clickedAtById, [itemId]: clickedAt },
      sectionItems: mapWithUpdatedItem(state.sectionItems, itemId, updater),
      platformSectionItems: mapWithUpdatedItem(state.platformSectionItems, itemId, updater),
      searchResults: state.searchResults
        ? mapWithUpdatedItem(state.searchResults, itemId, updater)
        : null,
    }
  }),

  // v12.2: ASR 链路完成后, 同步刷新列表卡片的 ai_summary
  updateItemSummary: (itemId, newSummary) => set((state) => ({
    sectionItems: mapWithUpdatedItem(state.sectionItems, itemId, (it) => ({
      ...it,
      ai_summary: newSummary,
      asr_status: 'success' as const,
    })),
    platformSectionItems: mapWithUpdatedItem(state.platformSectionItems, itemId, (it) => ({
      ...it,
      ai_summary: newSummary,
      asr_status: 'success' as const,
    })),
  })),

  appendCategoryItems: (category, newItems) => set((state) => {
    const map = new Map(state.searchResults ?? state.sectionItems)
    const existing = map.get(category) || []
    const existingIds = new Set(existing.map((it) => it.id))
    const deduped = newItems.filter((it) => !existingIds.has(it.id))
    map.set(category, [...existing, ...deduped])
    return state.searchResults ? { searchResults: map } : { sectionItems: map }
  }),

  appendPlatformItems: (platform, newItems) => set((state) => {
    const map = new Map(state.searchPlatformSectionItems ?? state.platformSectionItems)
    const existing = map.get(platform) || []
    const existingIds = new Set(existing.map((it) => it.id))
    const deduped = newItems.filter((it) => !existingIds.has(it.id))
    map.set(platform, [...existing, ...deduped])
    return state.searchPlatformSectionItems ? { searchPlatformSectionItems: map } : { platformSectionItems: map }
  }),

  ensurePlatformSections: () => {
    const current = get()
    if (current.platformSectionsLoaded && current.platformSectionItems.size > 0) {
      return Promise.resolve()
    }
    if (_platformSectionsPromise) return _platformSectionsPromise

    _platformSectionsPromise = fetchFeedPlatforms()
      .then((platRes) => {
        if (platRes.degraded) {
          throw new Error(platRes.degraded_reason || platRes.fallback_reason || 'platform sections degraded')
        }
        get().setPlatformSections(
          platRes.sections,
          platRes.platform_counts,
          platRes.source_counts,
          platRes.category_counts,
          platRes.read_model_version_id ?? null,
          platRes.platform_next_cursors,
        )
        set({ loadError: null })
      })
      .finally(() => {
        _platformSectionsPromise = null
      })
    return _platformSectionsPromise
  },

  serverSearch: (query) => {
    if (_searchTimer) clearTimeout(_searchTimer)
    const q = query.trim()
    const seq = ++_searchSeq
    if (!q) {
      set({
        searchResults: null,
        searchTotal: 0,
        searchCatCounts: {},
        searchPlatformSectionItems: null,
        searchPlatformCounts: {},
        searchSourceCounts: {},
        searchPlatformCategoryCounts: {},
        searchPlatformLoading: false,
        isSearching: false,
        searchDegraded: false,
      })
      return
    }
    set({
      isSearching: true,
      searchPlatformSectionItems: null,
      searchPlatformCounts: {},
      searchSourceCounts: {},
      searchPlatformCategoryCounts: {},
      searchPlatformLoading: true,
    })
    _searchTimer = setTimeout(async () => {
      try {
        const res = await fetchFeedSections({ search: q })
        if (seq !== _searchSeq) return
        if (res.degraded) {
          // BF-0704-6: 保留旧结果的同时显式暴露降级,UI 提示"搜索暂时不可用"
          set({ isSearching: false, searchPlatformLoading: false, searchDegraded: true })
          return
        }
        const map = new Map<string, FeedItem[]>()
        for (const [cat, items] of Object.entries(res.sections)) {
          map.set(cat, [...items])
        }
        set({
          searchResults: map,
          searchTotal: res.total,
          searchCatCounts: res.cat_counts ?? {},
          isSearching: false,
          searchDegraded: false,
        })
        fetchFeedPlatforms({ search: q })
          .then((platRes) => {
            if (seq !== _searchSeq) return
            if (platRes.degraded) {
              set({ searchPlatformSectionItems: null, searchPlatformLoading: false })
              return
            }
            const platformMap = new Map<string, FeedItem[]>()
            for (const [platform, items] of Object.entries(platRes.sections)) {
              platformMap.set(platform, [...items])
            }
            set({
              searchPlatformSectionItems: platformMap,
              searchPlatformCounts: platRes.platform_counts ?? {},
              searchSourceCounts: platRes.source_counts ?? {},
              searchPlatformCategoryCounts: platRes.category_counts ?? {},
              searchPlatformLoading: false,
            })
          })
          .catch(() => {
            if (seq === _searchSeq) set({ searchPlatformSectionItems: null, searchPlatformLoading: false })
          })
      } catch {
        if (seq !== _searchSeq) return
        set({ isSearching: false, searchPlatformLoading: false, searchDegraded: true })
      }
    }, 300)
  },

  clearSearch: () => {
    _searchSeq += 1
    if (_searchTimer) clearTimeout(_searchTimer)
    set({
      searchResults: null,
      searchTotal: 0,
      searchCatCounts: {},
      searchPlatformSectionItems: null,
      searchPlatformCounts: {},
      searchSourceCounts: {},
      searchPlatformCategoryCounts: {},
      searchPlatformLoading: false,
      isSearching: false,
      searchDegraded: false,
    })
  },

  startFetch: async () => {
    if (get().isFetching) {
      // BF-0420-10 rev2: 前端自身 isFetching guard 也要给反馈,
      // 否则用户连点第二次会"没反应"(后端都没调到)
      toast.info('抓取已在进行中,继续等待')
      return
    }
    set({ isFetching: true, fetchProgress: cloneStartingFetchProgress() })
    try {
      const res = await triggerFetchAll()
      // BF-0420-10: 后端可能返 {ok:false, msg:'Fetch already running'}
      if (!res.ok) {
        if (res.msg?.toLowerCase().includes('already running')) {
          toast.info('抓取已在进行中,继续等待')
        } else {
          toast.error(res.msg || '抓取请求失败')
          set({ isFetching: false, fetchProgress: null })
          return
        }
      } else {
        toast.info('开始抓取…')
      }
      pollUntilDone(set, get)
    } catch (e) {
      set({ isFetching: false, fetchProgress: null })
      const msg = e instanceof Error ? e.message : '抓取请求失败'
      toast.error(msg)
    }
  },

  initFetchStatus: async () => {
    try {
      const status = await fetchFetchStatus()
      if (status.running) {
        set({ isFetching: true, fetchProgress: status.progress ?? cloneStartingFetchProgress() })
        pollUntilDone(set, get)
      } else {
        set({ fetchProgress: status.progress ?? null })
      }
    } catch { /* ignore */ }
  },
}))

// --- Client-side keyword classification fallback ---
export function classifyByKeywords(item: FeedItem, categories: ClassificationConfig['categories']): string | null {
  const titleText = (item.title || '').toLowerCase()
  const bodyText = ((item.content || '') + (item.description || '') + (item.ai_summary || '')).toLowerCase()
  const text = `${titleText}\n${bodyText}`
  const platform = (item.platform || '').toLowerCase()

  const toolsCategory = categories.find((cat) => cat.id === 'ai_tools')
  if (toolsCategory && platform === 'github') {
    return toolsCategory.id
  }

  const tutorialCategory = categories.find((cat) => cat.id === 'tutorials')
  if (tutorialCategory && includesAny(text, TUTORIAL_HINTS)) {
    return tutorialCategory.id
  }

  const modelsCategory = categories.find((cat) => cat.id === 'models')
  if (modelsCategory && includesAny(text, MODEL_HINTS)) {
    return modelsCategory.id
  }

  const productsCategory = categories.find((cat) => cat.id === 'products')
  const hasProductSignal = includesAny(text, PRODUCT_HINTS)
  const hasDevToolSignal = includesAny(text, DEV_TOOL_HINTS)
  const hasProductExclusionSignal = includesAny(text, PRODUCT_EXCLUSION_HINTS)
  if (productsCategory && toolsCategory && hasProductSignal) {
    if (hasDevToolSignal) return toolsCategory.id
    if (!hasProductExclusionSignal && platform !== 'reddit') return productsCategory.id
  }

  let bestCategory: string | null = null
  let bestScore = 0
  let bestPriority = Number.POSITIVE_INFINITY

  for (const cat of categories) {
    const score = countKeywordHits(text, cat.fallback_keywords)
    if (score === 0) continue
    const priority = cat.priority ?? 99
    if (score > bestScore || (score === bestScore && priority < bestPriority)) {
      bestCategory = cat.id
      bestScore = score
      bestPriority = priority
    }
  }

  return bestCategory
}

// Categories hidden from recommend page (noise/non-AI content)
const HIDDEN_CATEGORIES = new Set(['other', '_uncategorized'])

// --- Derived hooks ---

/** Returns the active data source: searchResults when searching, sectionItems otherwise */
function useActiveItems() {
  const sectionItems = useFeedStore((s) => s.sectionItems)
  const searchResults = useFeedStore((s) => s.searchResults)
  return searchResults ?? sectionItems
}

/** Returns sections array (from sectionItems Map + classification sorting) */
export const useSectionItems = (): FeedSection[] => {
  const activeItems = useActiveItems()
  const catCounts = useFeedStore((s) => s.catCounts)
  const searchCatCounts = useFeedStore((s) => s.searchCatCounts)
  const classification = useFeedStore((s) => s.classification)
  const isSearching = useFeedStore((s) => s.searchResults !== null)

  return useMemo(() => {
    if (!classification) {
      const sections: FeedSection[] = []
      for (const [key, items] of activeItems) {
        if (items.length > 0) {
          const serverTotal = isSearching ? (searchCatCounts[key] ?? items.length) : (catCounts[key] ?? items.length)
          sections.push({ key, label: key, items, count: serverTotal })
        }
      }
      return sections
    }

    const visible = classification.categories.filter((c) => c.visible)
    const sections = visible
      .sort((a, b) => (a.priority ?? 99) - (b.priority ?? 99))
      .map((cat) => {
        const items = activeItems.get(cat.id) ?? []
        const serverTotal = isSearching ? (searchCatCounts[cat.id] ?? items.length) : (catCounts[cat.id] ?? items.length)
        return { key: cat.id, label: cat.name, items, count: serverTotal }
      })
      .filter((s) => s.items.length > 0 && !HIDDEN_CATEGORIES.has(s.key))

    return sections
  }, [activeItems, catCounts, searchCatCounts, classification, isSearching])
}

/** Returns items grouped by platform (flattened from active items) */
export const usePlatformItems = (): Map<string, FeedItem[]> => {
  const activeItems = useActiveItems()

  return useMemo(() => {
    const platformMap = new Map<string, FeedItem[]>()
    for (const items of activeItems.values()) {
      for (const item of items) {
        const platform = item.platform
        if (!platformMap.has(platform)) {
          platformMap.set(platform, [])
        }
        platformMap.get(platform)!.push(item)
      }
    }
    for (const [key, items] of platformMap) {
      platformMap.set(key, items.sort(sortItems))
    }
    return platformMap
  }, [activeItems])
}

/** Returns platform sections (from /api/feed/platforms, symmetric with useSectionItems).
 *  When searching, regroups searchResults by platform so the channels page also reflects search. */
export const usePlatformSections = (): FeedSection[] => {
  const platformItems = useFeedStore((s) => s.platformSectionItems)
  const platformCounts = useFeedStore((s) => s.platformCounts)
  const searchResults = useFeedStore((s) => s.searchResults)
  const searchPlatformItems = useFeedStore((s) => s.searchPlatformSectionItems)
  const searchPlatformCounts = useFeedStore((s) => s.searchPlatformCounts)

  return useMemo(() => {
    if (searchResults && searchPlatformItems) {
      const sections: FeedSection[] = []
      for (const [key, items] of searchPlatformItems) {
        if (items.length > 0) {
          sections.push({ key, label: key, items, count: searchPlatformCounts[key] ?? items.length })
        }
      }
      return sections
    }

    const sections: FeedSection[] = []
    for (const [key, items] of platformItems) {
      if (items.length > 0) {
        sections.push({ key, label: key, items, count: platformCounts[key] ?? items.length })
      }
    }
    return sections
  }, [platformItems, platformCounts, searchResults, searchPlatformItems, searchPlatformCounts])
}

/** v16.0 W4.T9: 返回某 platform 非空 L1 分类的列表(按数量降序)。
 *  数据源 = useFeedStore.platformCategoryCounts[platform] (来自 /api/feed/platforms category_counts).
 *  返回 [{id, count}] 数组,空类(count === 0)被过滤,L1PillBar 直接 map 渲染。
 *  display name 由调用方通过 classification(L1 id → name) 映射。 */
export const useVisibleCategoriesForPlatform = (
  platform: string,
): Array<{ id: string; count: number }> => {
  const counts = useFeedStore((s) => (
    s.searchResults
      ? s.searchPlatformCategoryCounts[platform]
      : s.platformCategoryCounts[platform]
  ))
  return useMemo(() => {
    if (!counts) return []
    return Object.entries(counts)
      .filter(([, count]) => count > 0)
      .map(([id, count]) => ({ id, count }))
      .sort((a, b) => b.count - a.count)
  }, [counts])
}

/** Returns flat array of all items */
export const useAllItems = (): FeedItem[] => {
  const activeItems = useActiveItems()

  return useMemo(() => {
    const all: FeedItem[] = []
    for (const items of activeItems.values()) {
      all.push(...items)
    }
    return all.sort(sortItems)
  }, [activeItems])
}

// --- Backward compatibility exports ---

/** @deprecated Use useDetailStore instead */
export const useSelectedItem = () => {
  return null
}

/** @deprecated Use useActionStore instead */
export const useActionCounts = () => {
  return { total: 0, pending: 0, dispatched: 0, done: 0, ignored: 0 }
}
