/**
 * v18.0 Spec-2.5（rev1, 2026-05-15）：信息 tab「按分类」视角。
 *
 * 用户流程：用户在信息 tab 顶部 segment toggle 选「按分类」→ 内容区切换到本组件
 * → 调用 `/api/feed/sections` 拉数据 → 按 ai_category L1 分类聚合 sections
 * → 复用 FeedSection 组件渲染（3 列瀑布流 + ~700px 折叠 + 渐变蒙版 + 加载更多）。
 *
 * Section 顺序 = `useSectionItems` 内置 classification.priority 排序（v15+ 分类
 * 体系 v4.0），section 标题 = 分类中文名（产品 / 工具 / 技术 / 资讯 等）。
 *
 * 异常态：
 *   - 2.5.E1：fetch 中显示 LoadingSkeleton，segment toggle 由父组件 disable
 *   - 2.5.E2：fetch 失败显示「分类视角加载失败，请重试」+ 重试按钮，
 *           segment toggle 保持「按分类」高亮态不回退
 *
 * 加载策略：组件 mount 时若 sectionItems 为空 → 触发 fetchFeedSections。
 * 重新 mount（用户切走再回来）→ 已有数据立即渲染，不刷数据，避免抖动。
 */
import { useEffect, useRef, useState, useCallback } from 'react'
import { Loader2 } from 'lucide-react'
import { fetchFeedSections } from '../../lib/api'
import { useFeedStore, useSectionItems } from '../../store/feedStore'
import { useUIStore } from '../../store/uiStore'
import { FeedSection } from '../feed/FeedSection'
import { cn } from '../../lib/utils'

/** 内联 skeleton（沿用 App.tsx 中 LoadingSkeleton 同款样式，避免跨组件导出耦合） */
function InfoCategorySkeleton({ embedded = false }: { embedded?: boolean }) {
  return (
    <div
      className={cn(embedded ? 'py-0' : 'px-4 py-4 max-w-[1200px] mx-auto')}
      data-testid="info-category-skeleton"
    >
      <div className="mb-4 space-y-2">
        {[1, 2, 3].map((i) => (
          <div key={i} className="h-6 bg-muted rounded animate-skeleton" style={{ width: `${70 + i * 10}%` }} />
        ))}
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {Array.from({ length: 9 }).map((_, i) => (
          <div key={i} className="h-48 bg-muted rounded-lg animate-skeleton" />
        ))}
      </div>
    </div>
  )
}

export interface InfoCategoryViewProps {
  /** 由父组件 InfoView 透传的 onLoadingChange 回调，用于联动 segment toggle disable 状态（Spec-2.5.E1） */
  onLoadingChange?: (loading: boolean) => void
  embedded?: boolean
}

export function InfoCategoryView({ onLoadingChange, embedded = false }: InfoCategoryViewProps = {}) {
  const sections = useSectionItems()
  const setSections = useFeedStore((s) => s.setSections)
  const isSearching = useFeedStore((s) => s.searchResults !== null)
  const searchDegraded = useFeedStore((s) => s.searchDegraded)
  // rev3: 搜索请求进行中(含输入防抖),用于加载状态条与旧内容压暗
  const searchLoading = useFeedStore((s) => s.isSearching)
  const searchQuery = useUIStore((s) => s.searchQuery)
  const [loading, setLoading] = useState<boolean>(sections.length === 0)
  const [error, setError] = useState<string | null>(null)
  const loadAttemptedRef = useRef(false)

  const loadSections = useCallback(async () => {
    setLoading(true)
    setError(null)
    onLoadingChange?.(true)
    try {
      const res = await fetchFeedSections()
      setSections(
        res.sections,
        res.cat_counts,
        res.read_model_version_id ?? null,
        res.section_next_cursors,
      )
    } catch (err) {
      // Spec-2.5.E2：fetch 失败 → 显示重试按钮；不回退视角
      console.error('[InfoCategoryView] fetchFeedSections failed', err)
      setError('分类视角加载失败，请重试')
    } finally {
      setLoading(false)
      onLoadingChange?.(false)
    }
  }, [setSections, onLoadingChange])

  // Mount 时若已有数据则跳过；空才拉
  useEffect(() => {
    if (sections.length === 0 && !error && !loadAttemptedRef.current) {
      loadAttemptedRef.current = true
      loadSections()
    } else {
      onLoadingChange?.(false)
    }
  }, [error, loadSections, onLoadingChange, sections.length])

  if (loading && sections.length === 0) {
    return <InfoCategorySkeleton embedded={embedded} />
  }

  if (error && sections.length === 0) {
    return (
      <div className={cn(embedded ? 'py-12' : 'max-w-[1200px] mx-auto px-4 py-12', 'text-center')}>
        <p className="text-muted-foreground mb-4">{error}</p>
        <button
          type="button"
          onClick={loadSections}
          className="px-5 py-2 text-sm font-medium text-foreground bg-card border border-border hover:border-warm-400 shadow-subtle hover:shadow-medium rounded-full transition-all cursor-pointer"
        >
          重试
        </button>
      </div>
    )
  }

  // BF-0704-6: 搜索降级(后端超时等)必须显式提示;保留当前可见内容
  const degradedBanner = searchDegraded && searchQuery ? (
    <div
      data-testid="info-search-degraded-hint"
      className="mb-4 rounded-md border border-border bg-muted px-4 py-2 text-[13px] text-muted-foreground"
    >
      搜索暂时不可用，请稍后重试
    </div>
  ) : null

  // Search active but no section has any matching item → render an empty-state
  // banner instead of returning nothing (which would render a blank page).
  if (isSearching && sections.length === 0) {
    return (
      <div
        className={cn(embedded ? 'py-12' : 'max-w-[1200px] mx-auto px-4 py-12', 'text-center')}
        data-testid="info-category-search-empty"
      >
        {degradedBanner ?? (
          <p className="text-muted-foreground">
            {searchQuery ? <>未找到与「{searchQuery}」相关的内容</> : '未找到相关内容'}
          </p>
        )}
      </div>
    )
  }

  return (
    <div className={cn(embedded ? 'py-0' : 'max-w-[1200px] mx-auto px-4 py-4')}>
      {/* BF-0704-6 rev3: 搜索加载状态条(不随旧内容压暗) */}
      {searchLoading && searchQuery ? (
        <div
          data-testid="info-search-loading"
          className="mb-4 flex items-center gap-2 rounded-md border border-border bg-muted px-4 py-2 text-[13px] text-muted-foreground"
        >
          <Loader2 size={14} className="animate-spin" aria-hidden="true" />
          正在搜索 “{searchQuery}”…
        </div>
      ) : null}
      {degradedBanner}
      <div
        data-testid="info-category-view"
        className={cn(searchLoading && searchQuery && 'opacity-50 pointer-events-none transition-opacity')}
      >
        {sections.map((section) => (
          <FeedSection
            key={section.key}
            section={section}
            showHeader
            showSubcategoryFilters
          />
        ))}
      </div>
    </div>
  )
}
