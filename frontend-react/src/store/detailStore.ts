import { create } from 'zustand'
import { fetchFeedItem, fetchFeedItemsBundle, translateItemAsr, triggerItemAsr } from '../lib/api'
import type { FeedItem, ActionItem, TranscriptPanelState, AsrPhase, AsrStatus, AsrSegment } from '../lib/types'

// v12.3: 中文翻译子状态(独立于 asrStatus,因为翻译失败不该把 ASR 打回 failed)
export type CnStatus = 'none' | 'loading' | 'ready' | 'failed'

// B6: detailCache 每条带时间戳, TTL 过期后忽略缓存值, 强制从后端拉最新.
// Why: 后端 generate_summaries / ASR 重跑 ai_summary 后, 无 TTL 的 cache 会让用户看到陈旧数据.
export interface DetailCacheEntry {
  item: FeedItem
  cachedAt: number
}
export const DETAIL_CACHE_TTL_MS = 5 * 60 * 1000

export function readDetailCache(
  cache: Map<string, DetailCacheEntry>,
  id: string,
  now: number = Date.now(),
): FeedItem | null {
  const entry = cache.get(id)
  if (!entry) return null
  if (now - entry.cachedAt > DETAIL_CACHE_TTL_MS) return null
  return entry.item
}

// Track in-flight item-detail requests so hover prefetch and click open can
// share the same remote request instead of racing two /api/feed/item/:id calls.
const _prefetching = new Map<string, Promise<FeedItem>>()

// v12.2: 每个 item 独立的 SSE 连接, closeModal 时关闭
const _asrEventSources = new Map<string, EventSource>()

/** v12.2: 把后端 asr_status 字段映射为前端 panel 状态.
 *  v13.0: skipped_quota → 'idle'(视觉保持按钮可点),副文字由组件根据 asrSkippedQuota 调整。
 */
function mapAsrStatusToPanel(status?: AsrStatus): TranscriptPanelState {
  if (!status) return 'idle'
  if (status === 'running') return 'running'
  if (status === 'success') return 'ready'
  if (status === 'failed_empty') return 'empty'
  if (status === 'skipped_quota') return 'idle'
  return 'failed'
}

/**
 * v12.3 B3 fix: 有 asr_text 就应该显示 ready 态,不论 asr_status。
 * 处理场景:ASR 写完 DB 后 translate 阶段仍在跑(asr_status=running),
 * 重开弹窗时应直接看到 transcript 而不是空白 running 态。
 */
function inferPanelState(item: {
  asr_text?: string | null
  asr_status?: AsrStatus | null
}): TranscriptPanelState {
  if (item.asr_text && item.asr_text.length > 0) return 'ready'
  return mapAsrStatusToPanel(item.asr_status ?? undefined)
}

function patchItemAsrCache(
  state: Pick<DetailState, 'itemDetail' | 'detailCache'>,
  itemId: string,
  patch: Partial<FeedItem>,
): { itemDetail: FeedItem | null; detailCache: Map<string, DetailCacheEntry> } {
  const isCurrentItem = state.itemDetail?.id === itemId
  const itemDetail: FeedItem | null = isCurrentItem
    ? ({ ...state.itemDetail, ...patch } as FeedItem)
    : state.itemDetail
  const detailCache = new Map(state.detailCache)
  const cached = detailCache.get(itemId)
  const patchedCached: FeedItem | null = isCurrentItem
    ? itemDetail
    : (cached?.item ? ({ ...cached.item, ...patch } as FeedItem) : null)
  if (patchedCached) {
    detailCache.set(itemId, { item: patchedCached, cachedAt: Date.now() })
  }
  return { itemDetail, detailCache }
}

function restoreFailedAsrStartState(
  state: DetailState,
  itemId: string,
  previousDetailItem: FeedItem | null,
  previousCacheEntry: DetailCacheEntry | undefined,
  asrError: string,
): Partial<DetailState> {
  const detailCache = new Map(state.detailCache)
  if (previousCacheEntry) {
    detailCache.set(itemId, previousCacheEntry)
  } else {
    detailCache.delete(itemId)
  }
  const previousItem = previousDetailItem ?? previousCacheEntry?.item ?? null
  return {
    itemDetail: state.itemDetail?.id === itemId ? previousDetailItem : state.itemDetail,
    detailCache,
    asrStatus: 'failed',
    asrRawStatus: previousItem?.asr_status ?? null,
    asrError,
    asrProgress: null,
  }
}

function asrFieldsFromItem(item: FeedItem): Partial<DetailState> {
  return {
    asrStatus: inferPanelState(item),
    asrText: item.asr_text ?? null,
    asrDurationSec: item.asr_duration_sec ?? null,
    asrError: item.asr_failed_reason ?? null,
    asrSegments: item.asr_segments ?? null,
    asrTextCn: item.asr_text_cn ?? null,
    asrSegmentsCn: item.asr_segments_cn ?? null,
    asrCnStatus: item.asr_text_cn
      ? 'ready'
      : (item.asr_status === 'success' ? 'loading' : 'none'),
    asrCostYuan: item.asr_cost_yuan ?? null,
    asrRawStatus: item.asr_status ?? null,
  }
}

function patchFreshDetailItem(
  state: DetailState,
  itemId: string,
  fresh: FeedItem,
): Partial<DetailState> {
  const detailCache = new Map(state.detailCache)
  detailCache.set(itemId, { item: fresh, cachedAt: Date.now() })
  const isCurrentItem = isCurrentDetailItem(state, itemId)
  return {
    detailCache,
    itemDetail: isCurrentItem ? fresh : state.itemDetail,
    isLoading: isCurrentItem ? false : state.isLoading,
    ...(isCurrentItem ? asrFieldsFromItem(fresh) : {}),
  }
}

function isCurrentDetailItem(
  state: Pick<DetailState, 'itemDetail' | 'modalStack'>,
  itemId: string,
): boolean {
  const top = state.modalStack[state.modalStack.length - 1]
  return state.itemDetail?.id === itemId || (top?.type === 'item' && top.id === itemId)
}

interface AsrProgress {
  phase: AsrPhase
  message: string
  percent: number
  startedAt: number  // epoch ms,用于"已用 N 秒"计算
}

interface DetailState {
  // Navigation stack
  modalStack: Array<{ type: 'item' | 'action'; id: string }>
  // Current detail data
  itemDetail: FeedItem | null
  itemActions: ActionItem[]
  actionDetail: ActionItem | null
  // Cache
  detailCache: Map<string, DetailCacheEntry>
  // Loading state
  isLoading: boolean
  loadError: string | null

  // v12.2: ASR state (per open item)
  asrStatus: TranscriptPanelState
  asrText: string | null
  asrDurationSec: number | null
  asrProgress: AsrProgress | null
  asrError: string | null
  asrRetryCount: number
  asrSummaryUpdated: boolean   // 刚触发 SummaryUpdatedBadge 显示

  // v12.3: 视频 ASR 体验增强
  asrSegments: AsrSegment[] | null
  asrTextCn: string | null
  asrSegmentsCn: (string | null)[] | null  // 方案 B:逐段翻译结果,长度 == asrSegments
  asrCnStatus: CnStatus        // 中文翻译 Tab 状态(独立于 asrStatus)
  asrCostYuan: number | null
  asrCurrentTimeMs: number     // 视频当前播放毫秒(timeupdate 驱动,联动高亮)
  asrAutoFollow: boolean       // 自动跟随开关(默认 true,用户手动滚动 5s 抑制)

  // v13.0: 原始后端 asr_status,前端 idle 态要区分 null(全新) vs skipped_quota(配额跳过)
  asrRawStatus: AsrStatus | null

  // Actions
  openItem: (id: string) => void
  openAction: (id: string) => void
  prefetchItem: (id: string) => void
  prefetchItems: (ids: string[]) => void
  goBack: () => void
  closeModal: () => void
  setItemDetail: (item: FeedItem) => void
  setItemActions: (actions: ActionItem[]) => void
  /** BF-0420-6: 从 FeedItem 的 asr_* 字段 hydrate store 的 asr 状态机。
   * 详情内容渲染时调用,确保已有 ASR 的视频立即 ready 态(不出现"开始 AI 转写"空态按钮) */
  hydrateFromItem: (item: FeedItem) => void
  setActionDetail: (action: ActionItem) => void
  cacheDetail: (id: string, item: FeedItem) => void
  setIsLoading: (loading: boolean) => void
  toggleItemStar: () => void

  // v12.2 ASR actions
  startAsr: (itemId: string, skipTranscript?: boolean) => Promise<void>
  retryAsr: (itemId: string) => Promise<void>
  retrySummary: (itemId: string) => Promise<void>
  clearSummaryBadge: () => void
  _connectAsrStream: (itemId: string) => void

  // v12.3 actions
  setAsrCurrentTimeMs: (ms: number) => void
  toggleAsrAutoFollow: () => void
  retryTranslate: (itemId: string) => Promise<void>
}

export const useDetailStore = create<DetailState>((set, get) => ({
  modalStack: [],
  itemDetail: null,
  itemActions: [],
  actionDetail: null,
  detailCache: new Map(),
  isLoading: false,
  loadError: null,

  // v12.2 ASR 初始
  asrStatus: 'idle',
  asrText: null,
  asrDurationSec: null,
  asrProgress: null,
  asrError: null,
  asrRetryCount: 0,
  asrSummaryUpdated: false,

  // v12.3 初始
  asrSegments: null,
  asrTextCn: null,
  asrSegmentsCn: null,
  asrCnStatus: 'none',
  asrCostYuan: null,
  asrCurrentTimeMs: 0,
  asrAutoFollow: true,
  // v13.0
  asrRawStatus: null,

  openItem: (id) => {
    const cachedBeforeOpen = readDetailCache(get().detailCache, id)
    set((state) => {
      const cached = readDetailCache(state.detailCache, id)
      const top = state.modalStack[state.modalStack.length - 1]
      const modalStack = top?.type === 'item' && top.id === id
        ? state.modalStack
        : [...state.modalStack, { type: 'item' as const, id }]
      return {
        modalStack,
        itemDetail: cached || null,
        itemActions: [],
        isLoading: !cached,
        loadError: null,
        // v12.2: 先从 cached 初始化(可能是 stale);下面的异步 refetch 会覆盖
        // v12.3 B3: asr_text 有值直接 ready,不被 asr_status=running 阻塞
        asrStatus: cached ? inferPanelState(cached) : 'idle',
        asrText: cached?.asr_text ?? null,
        asrDurationSec: cached?.asr_duration_sec ?? null,
        asrProgress: null,
        asrError: cached?.asr_failed_reason ?? null,
        asrRetryCount: 0,
        asrSummaryUpdated: false,
        // v12.3
        asrSegments: cached?.asr_segments ?? null,
        asrTextCn: cached?.asr_text_cn ?? null,
        asrSegmentsCn: cached?.asr_segments_cn ?? null,
        asrCnStatus: cached?.asr_text_cn ? 'ready' : (cached?.asr_status === 'success' ? 'loading' : 'none'),
        asrCostYuan: cached?.asr_cost_yuan ?? null,
        asrCurrentTimeMs: 0,
        asrRawStatus: cached?.asr_status ?? null,
      }
    })
    if (cachedBeforeOpen?.asr_status === 'running' && !cachedBeforeOpen.asr_text) {
      get()._connectAsrStream(id)
    }
    // v12.2 Bug 3+5 修复: 打开弹窗总是异步从后端拉最新 item
    // (cache 即显 + 后台同步最新, Notion/Linear 模式, memory: feedback_hover_prefetch_pattern)
    let itemPromise = _prefetching.get(id)
    if (!itemPromise) {
      itemPromise = fetchFeedItem(id)
      _prefetching.set(id, itemPromise)
    }
    itemPromise.then((fresh) => {
      const curr = get()
      // 仅当仍在此 item 的弹窗里才应用更新
      const top = curr.modalStack[curr.modalStack.length - 1]
      if (!top || top.id !== id) return
      curr.cacheDetail(id, fresh)
      set({
        itemDetail: fresh,
        isLoading: false,
        asrStatus: inferPanelState(fresh),
        asrText: fresh.asr_text ?? null,
        asrDurationSec: fresh.asr_duration_sec ?? null,
        asrError: fresh.asr_failed_reason ?? null,
        // v12.3
        asrSegments: fresh.asr_segments ?? null,
        asrTextCn: fresh.asr_text_cn ?? null,
        asrSegmentsCn: fresh.asr_segments_cn ?? null,
        asrCnStatus: fresh.asr_text_cn
          ? 'ready'
          : (fresh.asr_status === 'success' ? 'loading' : 'none'),
        asrCostYuan: fresh.asr_cost_yuan ?? null,
        asrRawStatus: fresh.asr_status ?? null,
      })
      // 如果拉到发现是 running 态(比如上次未完成的任务),自动接上 SSE
      if (fresh.asr_status === 'running') {
        get()._connectAsrStream(id)
      }
    }).catch(() => {
      // UX-3(B8): 有 cache 时静默(不破坏已显示内容);无 cache 时必须复位
      // loading 并暴露错误——原实现吞错导致骨架屏永远转
      const curr = get()
      const top = curr.modalStack[curr.modalStack.length - 1]
      if (top?.type === 'item' && top.id === id && !curr.itemDetail) {
        set({ isLoading: false, loadError: '内容加载失败,请重试' })
      }
    }).finally(() => {
      if (_prefetching.get(id) === itemPromise) {
        _prefetching.delete(id)
      }
    })
  },

  openAction: (id) => set((state) => {
    const top = state.modalStack[state.modalStack.length - 1]
    if (top?.type === 'action' && top.id === id) return state
    return { modalStack: [...state.modalStack, { type: 'action' as const, id }] }
  }),

  // Prefetch on hover — fetch and cache without opening modal
  prefetchItem: (id) => {
    const { detailCache } = get()
    if (readDetailCache(detailCache, id) !== null || _prefetching.has(id)) return
    const itemPromise = fetchFeedItem(id)
    _prefetching.set(id, itemPromise)
    itemPromise
      .then((item) => {
        get().cacheDetail(id, item)
      })
      .catch(() => {})
      .finally(() => {
        if (_prefetching.get(id) === itemPromise) {
          _prefetching.delete(id)
        }
      })
  },

  prefetchItems: (ids) => {
    const unique = Array.from(new Set(ids.map((id) => String(id)).filter(Boolean))).slice(0, 30)
    const pendingIds = unique.filter((id) => {
      const { detailCache } = get()
      return readDetailCache(detailCache, id) === null && !_prefetching.has(id)
    })
    if (pendingIds.length === 0) return

    const batchPromise = fetchFeedItemsBundle(pendingIds)
    for (const id of pendingIds) {
      const itemPromise = batchPromise.then((res) => {
        const item = res.items.find((it) => it.id === id)
        if (!item) throw new Error('Item not returned')
        return item
      })
      itemPromise.catch(() => {})
      _prefetching.set(id, itemPromise)
    }
    batchPromise
      .then((res) => {
        for (const item of res.items) {
          get().cacheDetail(item.id, item)
        }
      })
      .catch(() => {})
      .finally(() => {
        for (const id of pendingIds) {
          _prefetching.delete(id)
        }
      })
  },

  goBack: () => set((state) => ({
    modalStack: state.modalStack.slice(0, -1),
  })),

  closeModal: () => {
    // v12.2: 关闭弹窗断 SSE, 但后端任务继续跑 (PRD R3.3 明确决策)
    _asrEventSources.forEach((es) => es.close())
    _asrEventSources.clear()
    set({
      modalStack: [],
      itemDetail: null,
      itemActions: [],
      actionDetail: null,
      // 重置 ASR state
      asrStatus: 'idle',
      asrText: null,
      asrDurationSec: null,
      asrProgress: null,
      asrError: null,
      asrRetryCount: 0,
      asrSummaryUpdated: false,
      // v12.3 重置
      asrSegments: null,
      asrTextCn: null,
      asrSegmentsCn: null,
      asrCnStatus: 'none',
      asrCostYuan: null,
      asrCurrentTimeMs: 0,
      asrRawStatus: null,
    })
  },

  setItemDetail: (item) => set({ itemDetail: item }),
  setItemActions: (actions) => set({ itemActions: actions }),
  // BF-0420-6:从 item 同步 asr_* 到 store。逻辑对齐 openItem 中 fetchFeedItem.then 的 hydrate。
  hydrateFromItem: (item) => {
    set(asrFieldsFromItem(item))
    if (item.asr_status === 'running' && !item.asr_text) {
      get()._connectAsrStream(item.id)
    }
  },
  setActionDetail: (action) => set({ actionDetail: action }),

  cacheDetail: (id, item) => set((state) => {
    const newCache = new Map(state.detailCache)
    newCache.set(id, { item, cachedAt: Date.now() })
    return { detailCache: newCache }
  }),

  setIsLoading: (isLoading) => set({ isLoading }),

  toggleItemStar: () => set((state) => {
    if (!state.itemDetail) return state
    return {
      itemDetail: {
        ...state.itemDetail,
        starred_at: state.itemDetail.starred_at ? undefined : new Date().toISOString(),
      },
    }
  }),

  // ── v12.2 ASR actions ────────────────────────────────────

  startAsr: async (itemId, skipTranscript = false) => {
    const before = get()
    const previousDetailItem = before.itemDetail?.id === itemId ? before.itemDetail : null
    const previousCacheEntry = before.detailCache.get(itemId)
    set((state) => {
      const patched = patchItemAsrCache(state, itemId, {
        asr_status: 'running',
        asr_failed_reason: undefined,
      })
      return {
        ...patched,
        asrStatus: 'running',
        asrRawStatus: 'running',
        asrError: null,
        asrProgress: {
          phase: 'download',
          message: skipTranscript ? '摘要更新中' : '开始转写',
          percent: skipTranscript ? 90 : 0,
          startedAt: Date.now(),
        },
      }
    })
    try {
      const data = await triggerItemAsr(itemId, skipTranscript)
      if (data.status === 'success' && data.asr_text) {
        // 缓存命中
        set((state) => {
          const patch: Partial<FeedItem> = {
            asr_status: 'success',
            asr_text: data.asr_text ?? undefined,
            asr_duration_sec: data.asr_duration_sec ?? undefined,
            asr_segments: (data.asr_segments as AsrSegment[] | null | undefined) ?? null,
            asr_text_cn: data.asr_text_cn ?? null,
            asr_segments_cn: (data.asr_segments_cn as (string | null)[] | null | undefined) ?? null,
            asr_cost_yuan: data.asr_cost_yuan ?? undefined,
          }
          const patched = patchItemAsrCache(state, itemId, patch)
          return {
            ...patched,
            asrStatus: 'ready',
            asrRawStatus: 'success',
            asrText: data.asr_text,
            asrDurationSec: data.asr_duration_sec ?? null,
            asrProgress: null,
            // v12.3
            asrSegments: (data.asr_segments as AsrSegment[] | null | undefined) ?? null,
            asrTextCn: data.asr_text_cn ?? null,
            asrSegmentsCn: (data.asr_segments_cn as (string | null)[] | null | undefined) ?? null,
            asrCnStatus: data.asr_text_cn ? 'ready' : 'loading',
            asrCostYuan: data.asr_cost_yuan ?? null,
          }
        })
        return
      }
      // running 状态, 开 SSE
      get()._connectAsrStream(itemId)
    } catch (e) {
      const err = e as Error & { status?: number; current?: number; limit?: number }
      if (err.status === 429) {
        set((state) => restoreFailedAsrStartState(state, itemId, previousDetailItem, previousCacheEntry, '队列满,等任一完成后再试'))
      } else if (err.status === 401) {
        set((state) => restoreFailedAsrStartState(state, itemId, previousDetailItem, previousCacheEntry, '登录后可用 AI 转写'))
      } else {
        set((state) => restoreFailedAsrStartState(state, itemId, previousDetailItem, previousCacheEntry, err.message ?? '未知错误'))
      }
    }
  },

  retryAsr: async (itemId) => {
    if (get().asrRetryCount >= 1) return
    set((s) => ({ asrRetryCount: s.asrRetryCount + 1 }))
    await get().startAsr(itemId, false)
  },

  retrySummary: async (itemId) => {
    await get().startAsr(itemId, true)
  },

  clearSummaryBadge: () => set({ asrSummaryUpdated: false }),

  _connectAsrStream: (itemId) => {
    if (typeof EventSource === 'undefined') return
    // 关闭之前的 SSE
    const old = _asrEventSources.get(itemId)
    if (old) old.close()

    const es = new EventSource(`/api/items/${itemId}/asr/stream`)
    _asrEventSources.set(itemId, es)

    es.addEventListener('progress', (ev) => {
      try {
        const data = JSON.parse((ev as MessageEvent).data)
        set((s) => isCurrentDetailItem(s, itemId)
          ? {
              asrProgress: {
                phase: data.phase,
                message: data.message,
                percent: data.percent,
                startedAt: s.asrProgress?.startedAt ?? Date.now(),
              },
            }
          : {})
      } catch {/* swallow parse */}
    })

    es.addEventListener('transcript', (ev) => {
      try {
        const data = JSON.parse((ev as MessageEvent).data)
        set((s) => {
          // v12.2 Bug 3+5: 同步更新 itemDetail 和 detailCache, 下次打开能复用
          // v12.3: 同步 segments / cost_yuan
          const patch: Partial<FeedItem> = {
            asr_text: data.text,
            asr_status: 'success' as const,
            asr_duration_sec: data.duration_sec,
            asr_segments: data.segments ?? null,
            asr_cost_yuan: data.cost_yuan ?? null,
          }
          const patched = patchItemAsrCache(s, itemId, patch)
          if (!isCurrentDetailItem(s, itemId)) return patched
          return {
            ...patched,
            asrStatus: 'ready',
            asrRawStatus: 'success',
            asrText: data.text,
            asrDurationSec: data.duration_sec ?? null,
            asrProgress: null,
            asrSegments: data.segments ?? null,
            asrCostYuan: data.cost_yuan ?? null,
            // 翻译还没跑 → loading;已经有(历史)→ ready;none 场景走 asrCnStatus 初值
            asrCnStatus: s.asrTextCn ? 'ready' : 'loading',
          }
        })
      } catch {/* ignore */}
    })

    es.addEventListener('transcript_cn', (ev) => {
      // v12.3 E4: 翻译完成事件(方案 B 带 segments_cn)
      try {
        const data = JSON.parse((ev as MessageEvent).data)
        set((s) => {
          const patch: Partial<FeedItem> = {
            asr_text_cn: data.text,
            asr_segments_cn: data.segments_cn ?? null,
          }
          const patched = patchItemAsrCache(s, itemId, patch)
          if (!isCurrentDetailItem(s, itemId)) return patched
          return {
            ...patched,
            asrTextCn: data.text,
            asrSegmentsCn: data.segments_cn ?? null,
            asrCnStatus: 'ready',
          }
        })
      } catch {/* ignore */}
    })

    es.addEventListener('summary_updated', (ev) => {
      try {
        const data = JSON.parse((ev as MessageEvent).data)
        set((s) => {
          // v12.2 Bug 3+5: 摘要刷新时同步 itemDetail + detailCache
          const patch: Partial<FeedItem> = {
            ai_summary: data.ai_summary,
            asr_status: 'success' as const,
          }
          const patched = patchItemAsrCache(s, itemId, patch)
          if (!isCurrentDetailItem(s, itemId)) return patched
          return {
            ...patched,
            asrRawStatus: 'success',
            asrSummaryUpdated: true,
          }
        })
        // 同步刷新 feedStore (列表卡片摘要)
        import('./feedStore').then(({ useFeedStore }) => {
          const updater = (useFeedStore.getState() as { updateItemSummary?: (id: string, s: string) => void }).updateItemSummary
          if (updater) updater(itemId, data.ai_summary)
        }).catch(() => {})
      } catch {/* ignore */}
    })

    es.addEventListener('done', () => {
      const cnStatusBeforeDone = get().asrCnStatus
      fetchFeedItem(itemId)
        .then((fresh) => {
          set((s) => {
            const patch = patchFreshDetailItem(s, itemId, fresh)
            // v12.3: 流关时若翻译仍在 loading 且最终详情没有中文,降级 failed。
            if (isCurrentDetailItem(s, itemId) && cnStatusBeforeDone === 'loading' && fresh.asr_status === 'success' && !fresh.asr_text_cn) {
              patch.asrCnStatus = 'failed'
            }
            return patch
          })
        })
        .catch(() => {
          set((s) => isCurrentDetailItem(s, itemId) && s.asrCnStatus === 'loading'
            ? { asrCnStatus: 'failed' }
            : {})
        })
      es.close()
      _asrEventSources.delete(itemId)
      // BF-0419-17: ASR 完成后广播,HealthPanel 收到后主动 refetch 配额(不用等 5min 轮询)
      window.dispatchEvent(new CustomEvent('asr:done', { detail: { itemId } }))
    })

    es.addEventListener('error', (ev) => {
      try {
        const data = (ev as MessageEvent).data ? JSON.parse((ev as MessageEvent).data) : null
        if (data && data.code === 'empty_transcript') {
          set((s) => {
            const patched = patchItemAsrCache(s, itemId, {
              asr_status: 'failed_empty',
              asr_failed_reason: data.message ?? undefined,
            })
            if (!isCurrentDetailItem(s, itemId)) return patched
            return {
              ...patched,
              asrStatus: 'empty',
              asrRawStatus: 'failed_empty',
              asrProgress: null,
              asrError: data.message ?? null,
            }
          })
        } else if (data) {
          const message = data.message ?? data.code ?? '转写失败'
          set((s) => {
            const patched = patchItemAsrCache(s, itemId, {
              asr_status: 'failed_asr',
              asr_failed_reason: message,
            })
            if (!isCurrentDetailItem(s, itemId)) return patched
            return {
              ...patched,
              asrStatus: 'failed',
              asrRawStatus: 'failed_asr',
              asrProgress: null,
              asrError: message,
            }
          })
        }
      } catch {/* EventSource 自身的 error, 不一定是服务器 error 事件 */}
    })
  },

  // ── v12.3 actions ────────────────────────────────────────

  setAsrCurrentTimeMs: (ms) => {
    // 不用 set 的 function 形式因为每 ~250ms 被调用,直接 set 即可
    set({ asrCurrentTimeMs: ms })
  },

  toggleAsrAutoFollow: () => {
    set((s) => ({ asrAutoFollow: !s.asrAutoFollow }))
  },

  retryTranslate: async (itemId) => {
    const s = get()
    if (!s.asrText) return
    set({ asrCnStatus: 'loading' })
    try {
      const data = await translateItemAsr(itemId)
      if (data.asr_text_cn) {
        set((prev) => {
          const nextItem = prev.itemDetail ? {
            ...prev.itemDetail,
            asr_text_cn: data.asr_text_cn,
            asr_segments_cn: data.asr_segments_cn ?? null,
          } : prev.itemDetail
          const newCache = new Map(prev.detailCache)
          if (nextItem) newCache.set(itemId, { item: nextItem, cachedAt: Date.now() })
          return {
            asrTextCn: data.asr_text_cn,
            asrSegmentsCn: data.asr_segments_cn ?? null,
            asrCnStatus: 'ready',
            itemDetail: nextItem,
            detailCache: newCache,
          }
        })
      } else {
        set({ asrCnStatus: 'failed' })
      }
    } catch {
      set({ asrCnStatus: 'failed' })
    }
  },
}))
