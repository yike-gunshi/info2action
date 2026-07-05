/**
 * v15.0 cluster 详情 store — 弹窗 + 落地页共用。
 *
 * 职责：
 *   - 当前查看的 cluster 详情 + sources（弹窗 / 落地页共用）
 *   - 自动 click 打点（弹窗打开时调 POST /api/clusters/:id/click）
 *   - merged_into redirect 处理（落地页 useEffect 监听 redirect_to）
 *   - actions 列表 + SSE 流式生成行动点
 *
 * Fast Refresh 硬约束：本文件只导出 useClusterDetailStore + ClusterModalState 类型。
 */
import { create } from 'zustand'
import type { ClusterDetail, ClusterSource, ClusterAction, ClusterBundleResponse, ClusterEvent } from '../lib/types'
import {
  fetchClusterBundle,
  fetchClusterDetail,
  fetchClusterSources,
  clickCluster,
  setClusterStar,
  fetchClusterActions,
  generateClusterAction,
} from '../lib/api'

export type ClusterModalState = 'closed' | 'loading' | 'open' | 'redirecting' | 'error'

const CLUSTER_BUNDLE_CACHE_TTL_MS = 5 * 60 * 1000
const CLUSTER_MODAL_LOADING_DELAY_MS = 450
const _bundleCache = new Map<number, { bundle: ClusterBundleResponse; cachedAt: number }>()
const _bundleInFlight = new Map<number, Promise<ClusterBundleResponse>>()
let _openModalSeq = 0

function readBundleCache(clusterId: number): ClusterBundleResponse | null {
  const cached = _bundleCache.get(clusterId)
  if (!cached) return null
  if (Date.now() - cached.cachedAt > CLUSTER_BUNDLE_CACHE_TTL_MS) {
    _bundleCache.delete(clusterId)
    return null
  }
  return cached.bundle
}

function cacheBundle(clusterId: number, bundle: ClusterBundleResponse) {
  _bundleCache.set(clusterId, { bundle, cachedAt: Date.now() })
}

function eventPreviewToDetail(event: ClusterEvent): ClusterDetail {
  return {
    id: event.id,
    ai_title: event.ai_title,
    ai_summary: event.ai_summary ?? null,
    ai_key_points: [],
    doc_count: event.doc_count,
    unique_source_count: event.unique_source_count,
    platforms: event.platforms,
    category: event.category,
    first_doc_at: event.first_doc_at,
    last_doc_at: event.last_doc_at,
    cover_url: event.cover_url,
    media_urls: event.cover_url ? [event.cover_url] : [],
    live_version: event.live_version,
    user_last_seen_version: event.last_seen_version ?? null,
    viewer_status: {
      clicked_at: null,
      starred_at: null,
      last_seen_version: event.last_seen_version ?? null,
    },
    is_visible_in_feed: true,
  }
}

/** 一行 thinking 流：text + 是否来自 AI thinking-ai 通道 + 所在 stage */
export interface ClusterThinkingLine {
  text: string
  ai?: boolean
  stage?: number
}

interface ClusterDetailState {
  // 弹窗状态
  modalState: ClusterModalState
  modalClusterId: number | null
  cluster: ClusterDetail | null
  sources: ClusterSource[]
  sourcesCursor: number | null
  sourcesLoading: boolean
  actions: ClusterAction[]
  /** redirect_to 时落地页 useEffect 拿来更新 URL */
  redirectTo: number | null
  error: string | null

  // SSE 行动点
  generating: boolean
  generateController: AbortController | null
  /** 4-stage 进度: 0=pending, 1=active, 2=done */
  generateStages: number[]
  /** 完整 thinking 行列表（含 thinking + thinking-ai） — UI 用这个驱动打字机 */
  generateThinkingLines: ClusterThinkingLine[]
  /** 兼容旧 store 字段 — 仅 thinking 文本 string[]，外部消费方少 */
  generateThinking: string[]
  /** result 事件解析出的完整 action（含 prompt / reason / type） */
  generateAction: ClusterAction | null
  /** done 事件简要 payload — 兼容旧字段 */
  generateDone: { actionId: string; title: string } | null
  generateError: string | null

  openModal: (clusterId: number, preview?: ClusterEvent) => Promise<void>
  prefetchBundle: (clusterId: number) => void
  closeModal: () => void
  loadSources: (clusterId: number, append?: boolean) => Promise<void>
  loadActions: (clusterId: number) => Promise<void>
  toggleClusterStar: (clusterId: number) => Promise<{ ok: boolean; starred_at: string | null }>
  startGenerate: (clusterId: number, userHint?: string, actionType?: string) => void
  cancelGenerate: () => void
  /** 重置 generate-* 字段而不影响 modal/sources/actions 主流程 */
  resetGenerate: () => void
  /** 落地页用：直接拉详情，不打 click（落地页是分享场景，自己按 R3.1 触发 click） */
  loadFullPage: (clusterId: number) => Promise<void>
}

export const useClusterDetailStore = create<ClusterDetailState>((set, get) => ({
  modalState: 'closed',
  modalClusterId: null,
  cluster: null,
  sources: [],
  sourcesCursor: null,
  sourcesLoading: false,
  actions: [],
  redirectTo: null,
  error: null,
  generating: false,
  generateController: null,
  generateStages: [0, 0, 0, 0],
  generateThinkingLines: [],
  generateThinking: [],
  generateAction: null,
  generateDone: null,
  generateError: null,

  openModal: async (clusterId, preview) => {
    const openSeq = ++_openModalSeq
    const cachedBundle = readBundleCache(clusterId)
    let loadingTimer: ReturnType<typeof setTimeout> | null = null

    set({
      modalState: preview ? 'open' : 'closed',
      modalClusterId: clusterId,
      cluster: preview ? eventPreviewToDetail(preview) : null,
      sources: [],
      sourcesCursor: null,
      actions: [],
      redirectTo: null,
      error: null,
      generateStages: [0, 0, 0, 0],
      generateThinkingLines: [],
      generateThinking: [],
      generateAction: null,
      generateDone: null,
      generateError: null,
    })

    if (!cachedBundle && !preview) {
      loadingTimer = setTimeout(() => {
        const s = get()
        if (_openModalSeq === openSeq && s.modalClusterId === clusterId && s.modalState === 'closed') {
          set({ modalState: 'loading' })
        }
      }, CLUSTER_MODAL_LOADING_DELAY_MS)
    }

    // Bundle: 详情 + 第一页 sources in one request; click remains fire-and-forget.
    try {
      let bundle = cachedBundle
      if (!bundle) {
        let promise = _bundleInFlight.get(clusterId)
        if (!promise) {
          promise = fetchClusterBundle(clusterId, { page: 1, limit: 20 })
          _bundleInFlight.set(clusterId, promise)
        }
        bundle = await promise
        cacheBundle(clusterId, bundle)
        if (_bundleInFlight.get(clusterId) === promise) {
          _bundleInFlight.delete(clusterId)
        }
      }
      if (loadingTimer) clearTimeout(loadingTimer)
      if (_openModalSeq !== openSeq || get().modalClusterId !== clusterId) return

      const currentCategory = get().cluster?.category
      const detail = {
        ...bundle.cluster,
        category: bundle.cluster.category ?? currentCategory ?? null,
      }
      // 弹窗中的 redirect_to → 直接 open 新 cluster（不影响 hash）
      if (detail.redirect_to) {
        await get().openModal(detail.redirect_to)
        return
      }
      set({
        modalState: 'open',
        cluster: detail,
        sources: bundle.sources,
        sourcesCursor: bundle.sources_next_cursor,
      })
      // 异步 click 打点（不阻塞 UI；失败也不报错给用户）
      clickCluster(clusterId).catch(() => { /* swallow */ })
      // v15.1 R7.1：用户点开 cluster 弹窗时通知 eventsStore 乐观清角标
      // + fire-and-forget 后端 /seen 写入（失败不影响渲染）
      void import('./eventsStore').then(({ useEventsStore }) => {
        useEventsStore.getState().markSeen(clusterId)
      }).catch(() => { /* swallow */ })
    } catch (e) {
      if (loadingTimer) clearTimeout(loadingTimer)
      if (_openModalSeq !== openSeq || get().modalClusterId !== clusterId) return
      _bundleInFlight.delete(clusterId)
      const msg = e instanceof Error ? e.message : 'Failed to load cluster'
      set({ modalState: 'error', error: msg })
    }
  },

  prefetchBundle: (clusterId) => {
    if (readBundleCache(clusterId) || _bundleInFlight.has(clusterId)) return
    const promise = fetchClusterBundle(clusterId, { page: 1, limit: 20 })
    _bundleInFlight.set(clusterId, promise)
    promise
      .then((bundle) => cacheBundle(clusterId, bundle))
      .catch(() => {})
      .finally(() => {
        if (_bundleInFlight.get(clusterId) === promise) {
          _bundleInFlight.delete(clusterId)
        }
      })
  },

  closeModal: () => {
    _openModalSeq += 1
    const { generateController } = get()
    if (generateController) {
      generateController.abort()
    }
    set({
      modalState: 'closed',
      modalClusterId: null,
      cluster: null,
      sources: [],
      sourcesCursor: null,
      actions: [],
      redirectTo: null,
      error: null,
      generating: false,
      generateController: null,
      generateStages: [0, 0, 0, 0],
      generateThinkingLines: [],
      generateThinking: [],
      generateAction: null,
      generateDone: null,
      generateError: null,
    })
  },

  resetGenerate: () => {
    const { generateController } = get()
    if (generateController) {
      generateController.abort()
    }
    set({
      generating: false,
      generateController: null,
      generateStages: [0, 0, 0, 0],
      generateThinkingLines: [],
      generateThinking: [],
      generateAction: null,
      generateDone: null,
      generateError: null,
    })
  },

  loadSources: async (clusterId, append = false) => {
    const { sourcesCursor, sources, sourcesLoading } = get()
    if (sourcesLoading) return
    if (append && !sourcesCursor) return
    set({ sourcesLoading: true })
    try {
      const page = append ? (sourcesCursor || 1) : 1
      const res = await fetchClusterSources(clusterId, { page, limit: 20 })
      set({
        sources: append ? [...sources, ...res.sources] : res.sources,
        sourcesCursor: res.next_cursor,
        sourcesLoading: false,
      })
    } catch {
      set({ sourcesLoading: false })
    }
  },

  loadActions: async (clusterId) => {
    try {
      const res = await fetchClusterActions(clusterId)
      set({ actions: res.actions })
    } catch {
      // 拉 actions 失败不阻断主流程
    }
  },

  toggleClusterStar: async (clusterId) => {
    // 乐观更新:先翻转图标,接口失败再回滚(对齐信息弹窗手感, D3)
    const applyStarredAt = (starredAt: string | null) => {
      const current = get().cluster
      if (current?.id !== clusterId) return
      set({
        cluster: {
          ...current,
          viewer_status: {
            clicked_at: current.viewer_status?.clicked_at ?? null,
            last_seen_version: current.viewer_status?.last_seen_version ?? current.user_last_seen_version ?? null,
            starred_at: starredAt,
          },
        },
      })
    }
    const snapshot = get().cluster
    const prevStarredAt = snapshot?.id === clusterId
      ? snapshot.viewer_status?.starred_at ?? null
      : null
    applyStarredAt(prevStarredAt ? null : new Date().toISOString())
    try {
      const result = await setClusterStar(clusterId)
      _bundleCache.delete(clusterId)
      applyStarredAt(result.starred_at)
      return result
    } catch (err) {
      applyStarredAt(prevStarredAt)
      throw err
    }
  },

  startGenerate: (clusterId, userHint = '', actionType?: string) => {
    const { generating, generateController } = get()
    if (generating) {
      generateController?.abort()
    }
    set({
      generating: true,
      generateStages: [1, 0, 0, 0],
      generateThinkingLines: [],
      generateThinking: [],
      generateAction: null,
      generateDone: null,
      generateError: null,
    })
    // BF-0424-CLUSTER-SSE: 处理 v10.1 同款多事件流
    // thinking | thinking-ai | stage(active|done) | result | done | error
    const controller = generateClusterAction(
      clusterId,
      { userHint, actionType },
      (event) => {
        const { type } = event
        if (type === 'thinking' || type === 'thinking-ai') {
          const text = String(event.text || '')
          if (!text) return
          const stage = typeof event.stage === 'number' ? (event.stage as number) : undefined
          set((s) => ({
            generateThinkingLines: [
              ...s.generateThinkingLines,
              { text, ai: type === 'thinking-ai', stage },
            ],
            // 兼容字段：仅 thinking 文本（方便老 test / 老组件）
            generateThinking:
              type === 'thinking'
                ? [...s.generateThinking, text]
                : s.generateThinking,
          }))
        } else if (type === 'stage') {
          const idx = typeof event.index === 'number' ? (event.index as number) : -1
          const status = String(event.status || '')
          if (idx >= 0 && idx < 4) {
            set((s) => {
              const next = [...s.generateStages]
              if (status === 'done') {
                next[idx] = 2
                if (idx + 1 < next.length && next[idx + 1] === 0) next[idx + 1] = 1
              } else if (status === 'active') {
                next[idx] = 1
              }
              return { generateStages: next }
            })
          }
        } else if (type === 'result') {
          // result.action 是完整结构化 action
          const action = (event.action || null) as ClusterAction | null
          if (action) {
            // 后端字段同步 — 兼容字段
            set({
              generateAction: action,
              generateDone: {
                actionId: String(action.id || ''),
                title: String(action.title || ''),
              },
            })
          }
        } else if (type === 'done') {
          set((s) => {
            const final = s.generateAction
              ? s
              : {
                  ...s,
                  generateDone: s.generateDone || {
                    actionId: String(event.action_id || ''),
                    title: String(event.title || ''),
                  },
                }
            return {
              ...final,
              generateStages: [2, 2, 2, 2],
              generating: false,
              generateController: null,
            }
          })
          // 完成后刷一遍 actions 列表
          get().loadActions(clusterId)
        } else if (type === 'error') {
          const message = String(event.error || event.message || 'Generate failed')
          set({
            generateError: message,
            generating: false,
            generateController: null,
          })
        }
      },
      () => {
        // onDone (stream closed) — fallback if no explicit done event
        set((s) => ({
          generating: false,
          generateController: null,
          generateStages: s.generateAction
            ? [2, 2, 2, 2]
            : s.generateError
              ? s.generateStages
              : s.generateStages,
        }))
      },
      (err) => {
        set({
          generateError: err.message,
          generating: false,
          generateController: null,
        })
      },
    )
    set({ generateController: controller })
  },

  cancelGenerate: () => {
    const { generateController } = get()
    if (generateController) {
      generateController.abort()
    }
    set({
      generating: false,
      generateController: null,
    })
  },

  loadFullPage: async (clusterId) => {
    set({
      modalState: 'loading',
      modalClusterId: clusterId,
      cluster: null,
      sources: [],
      sourcesCursor: null,
      redirectTo: null,
      error: null,
    })
    try {
      const [detail, sourcesRes] = await Promise.all([
        fetchClusterDetail(clusterId),
        fetchClusterSources(clusterId, { page: 1, limit: 20 }),
      ])
      if (detail.redirect_to) {
        // 落地页：set redirect_to，由页面 useEffect 处理 history.replaceState
        set({
          modalState: 'redirecting',
          redirectTo: detail.redirect_to,
        })
        return
      }
      set({
        modalState: 'open',
        cluster: detail,
        sources: sourcesRes.sources,
        sourcesCursor: sourcesRes.next_cursor,
      })
      clickCluster(clusterId).catch(() => { /* swallow */ })
      // v15.1 R7.1：用户点开 cluster 弹窗时通知 eventsStore 乐观清角标
      // + fire-and-forget 后端 /seen 写入（失败不影响渲染）
      void import('./eventsStore').then(({ useEventsStore }) => {
        useEventsStore.getState().markSeen(clusterId)
      }).catch(() => { /* swallow */ })
      get().loadActions(clusterId)
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to load cluster'
      set({ modalState: 'error', error: msg })
    }
  },
}))
