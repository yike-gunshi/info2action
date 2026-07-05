import { useEffect, useCallback } from 'react'
import { useUIStore } from '../store/uiStore'
import { useDetailStore } from '../store/detailStore'
import { useActionStore } from '../store/actionStore'
import { buildInfoItemHash, parseLegacyItemHash } from '../lib/itemDeepLink'
import type { L1View } from '../lib/types'

// ── v15.0 cluster route helpers ─────────────────────────────────────────────
// #cluster=:id 是事件详情落地页；#item=:id 只作为旧链接迁移到 v=info&d=:id。
// 与 useUIStore 的 URLSearchParams hash 解析策略并存（v=/s=/d=）。

/** 解析 hash 是否为 #cluster=NN 形式，返回 cluster id（数字）或 null。 */
export function parseClusterHash(): number | null {
  if (typeof window === 'undefined') return null
  const hash = window.location.hash.slice(1)
  if (!hash.startsWith('cluster=')) return null
  const raw = hash.slice('cluster='.length).trim()
  const id = parseInt(raw, 10)
  return Number.isFinite(id) && id > 0 ? id : null
}

// ── v18.0 nav-merge: 老 hash 60 天兼容 ─────────────────────────────────────
// PRD §Spec-4 锁定:
//   v=recommend|channels  → v=info（同位映射）
//   v=starred|history     → 跳全屏路由（Location 跳，不在 v= 里处理）
//   未知值                → fallback highlights（不抛异常）
//
// 既保留 60 天兼容窗口，又在缩窄 L1View 后维持类型安全。

/** 把可能的老 view 字符串映射到当前 L1View；无效值 fallback highlights。 */
export function mapLegacyL1(raw: string | null | undefined): L1View {
  if (!raw) return 'highlights'
  if (raw === 'recommend' || raw === 'channels' || raw === 'info') return 'info'
  if (raw === 'highlights' || raw === 'actions') return raw
  // starred / history 由 isLegacyFullscreenView 处理；这里 fallback
  return 'highlights'
}

/** 检查是否是应跳全屏路由的老 view（starred/history）；返回路由名或 null。 */
export function isLegacyFullscreenView(raw: string | null | undefined): 'starred' | 'history' | null {
  if (raw === 'starred' || raw === 'history') return raw
  return null
}

/** v18.0 PRD §Spec-4.7: 60 天兼容期内做 hash 静默重定向（无可见跳转闪烁）。
 *  返回 true 表示已发起重定向（caller 应 early return 等下一轮 hashchange）。
 */
export function applyLegacyHashRedirect(): boolean {
  if (typeof window === 'undefined') return false
  const raw = window.location.hash.slice(1)
  if (!raw) return false

  const legacyItemId = parseLegacyItemHash(raw)
  if (legacyItemId) {
    window.location.hash = buildInfoItemHash(legacyItemId)
    return true
  }

  // 全屏路由优先（hash 是单 token，不含 = 时）
  if (!raw.includes('=') && !raw.includes('&')) {
    const fs = isLegacyFullscreenView(raw)
    if (fs) {
      // 已经在目标 hash 上，无需跳
      return false
    }
  }

  const params = new URLSearchParams(raw)
  const view = params.get('v')
  if (!view) return false

  // starred / history → 跳到全屏 hash（不带 v= 前缀）
  const fs = isLegacyFullscreenView(view)
  if (fs) {
    // 保留 d=（item deep link 在全屏页内不可用，但保留以防上游使用）
    window.location.hash = fs
    return true
  }

  // recommend / channels → 改写 v=info 但保留其它参数（s=/d=/a=）
  if (view === 'recommend' || view === 'channels') {
    params.set('v', 'info')
    window.location.hash = params.toString()
    return true
  }

  return false
}

/** Sync URL hash ↔ navigation state */
export function useHash() {
  const setL1 = useUIStore((s) => s.setL1)
  const setExpandedKey = useUIStore((s) => s.setExpandedKey)
  const openItem = useDetailStore((s) => s.openItem)
  const setFocusedActionId = useActionStore((s) => s.setFocusedActionId)

  // Parse hash on mount and hash change
  useEffect(() => {
    function parseHash() {
      // v18.0 §Spec-4: 老 hash 静默重定向（recommend|channels → info）
      // 重定向触发新 hashchange，本轮 early return 等下一轮处理
      if (applyLegacyHashRedirect()) return

      const hash = window.location.hash.slice(1) // remove #
      if (!hash) return

      const params = new URLSearchParams(hash)
      const rawView = params.get('v')
      const section = params.get('s')
      const detail = params.get('d')
      const action = params.get('a')

      // v18.0 §Spec-1.E2: 非法 view 值 fallback highlights，不抛
      if (rawView) {
        setL1(mapLegacyL1(rawView))
      }
      if (action) {
        setL1('actions')
        setFocusedActionId(action)
      } else {
        setFocusedActionId(null)
      }
      if (section) {
        setExpandedKey(section)
        // Scroll to section element if it exists
        requestAnimationFrame(() => {
          const el = document.getElementById(`section-${section}`)
          el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
        })
      }
      if (detail) openItem(detail)
    }

    parseHash()
    window.addEventListener('hashchange', parseHash)
    return () => window.removeEventListener('hashchange', parseHash)
  }, [setL1, setExpandedKey, openItem, setFocusedActionId])

  // Update hash when state changes
  const updateHash = useCallback((params: {
    v?: L1View
    s?: string | null
    d?: string | null
  }) => {
    const current = new URLSearchParams(window.location.hash.slice(1))
    if (params.v !== undefined) {
      current.set('v', params.v)
      current.delete('a')
    }
    if (params.s !== undefined) {
      if (params.s) current.set('s', params.s)
      else current.delete('s')
    }
    if (params.d !== undefined) {
      if (params.d) current.set('d', String(params.d))
      else current.delete('d')
    }
    window.location.hash = current.toString()
  }, [])

  return { updateHash }
}
