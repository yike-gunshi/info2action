/**
 * v24.1 §21.3 修订: SectionFront = 板块壳 —— v24 板块眉（保留件）+ 回滚的瀑布流白卡身体。
 *
 * 眉（v24 保留，不随卡片回滚）：
 *   2px×26px brand 短标线压通栏 hairline + 22px/700 衬线板块名 + mono 12px 计数
 *   + 同行右侧 L2 underline-tab；桌面滚进 section 后 sticky（top-[92px]），移动端不 sticky。
 *
 * 身体（v24.1 回滚，用户实物验收定案）：
 *   JS masonry 白卡瀑布流 + 折叠夹取（~800px 上限、hasMore 时裁到最短列）
 *   + 底部渐变蒙版 + 「展开更多」hairline 按钮（v24 样式保留，非旧阴影胶囊）。
 *   masonry 视觉序 ≠ DOM/焦点序（WCAG 2.4.3）为已知取舍，记录于 DESIGN.md §21.3 v24.1 修订块。
 *
 * 「按类型」（FeedSection）与「按来源」（ChannelsView，platform 当分类）复用本组件；
 * 数据获取、筛选、分页、计数语义留在各自容器里。IO 懒渲染 + 行级 cv-auto 保留件照旧。
 */
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { cn } from '../../lib/utils'
import type { FeedItem } from '../../lib/types'
import { InfoCard } from './InfoCard'
import { Masonry } from './Masonry'

/**
 * Max collapsed height for the masonry grid.
 * We dynamically clamp this to the actual rendered content height
 * so the gradient mask never sits over empty space.
 */
const COLLAPSED_MAX = 800

export interface SectionFrontProps {
  /** DOM 锚点 id 的后缀（`s-${sectionKey}`），scroll-spy 与滚回顶部依赖它。 */
  sectionKey: string
  /** 板块名；不传 = 不渲染板块眉（Image2 嵌入态）。 */
  label?: string
  /** 板块总条数（mono 计数）。 */
  count?: number
  /** 可见条目（容器按 showCount 切好再传入）。 */
  items: FeedItem[]
  hasMore: boolean
  remaining: number
  isExpanded: boolean
  showReadState?: boolean
  onLoadMore: () => void
  onCollapse: () => void
  /** section 进入视口（IO 懒渲染激活）时回调一次，容器用于触发预取。 */
  onBecameVisible?: () => void
  /** 同行右侧 L2（underline-tab 筛选条），由容器注入。 */
  pillBar?: ReactNode
  /** 筛选请求进行中 → 轻微降透明度 + aria-busy。 */
  filterLoading?: boolean
}

export function SectionFront({
  sectionKey,
  label,
  count,
  items,
  hasMore,
  remaining,
  isExpanded,
  showReadState = true,
  onLoadMore,
  onCollapse,
  onBecameVisible,
  pillBar,
  filterLoading = false,
}: SectionFrontProps) {
  // 懒渲染：section 靠近视口才渲染卡片（保留件）
  const [hasBeenVisible, setHasBeenVisible] = useState(false)
  const rootRef = useRef<HTMLElement | null>(null)
  const onBecameVisibleRef = useRef(onBecameVisible)
  onBecameVisibleRef.current = onBecameVisible
  useEffect(() => {
    if (hasBeenVisible) return
    const el = rootRef.current
    if (!el || typeof IntersectionObserver === 'undefined') {
      setHasBeenVisible(true)
      onBecameVisibleRef.current?.()
      return
    }
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setHasBeenVisible(true)
          onBecameVisibleRef.current?.()
          observer.disconnect()
        }
      },
      { rootMargin: '200px 0px' },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [hasBeenVisible])

  // Masonry 测量：最短列高度 = 折叠裁切线
  const masonryInnerRef = useRef<HTMLDivElement>(null)
  const [shortestColHeight, setShortestColHeight] = useState<number | null>(null)
  useLayoutEffect(() => {
    if (!masonryInnerRef.current) return
    const container = masonryInnerRef.current.querySelector('[data-testid="masonry-columns"]')
    if (!container || container.children.length === 0) return
    const colHeights = Array.from(container.children).map((c) => (c as HTMLElement).offsetHeight)
    const shortest = Math.min(...colHeights)
    setShortestColHeight((current) => (current === shortest ? current : shortest))
  }, [items, isExpanded, hasMore])

  // Clip height: when hasMore, cut at shortest column so ALL columns have content past the line
  // Collapsed: also cap at COLLAPSED_MAX; Expanded: use shortest column directly
  const clipMaxHeight = hasMore && shortestColHeight != null && shortestColHeight > 100
    ? (isExpanded
      ? shortestColHeight - 40 // leave margin so gradient overlays actual card content
      : Math.min(shortestColHeight - 40, COLLAPSED_MAX))
    : hasMore
      ? COLLAPSED_MAX // fallback before measurement
      : undefined

  // 展开滚动途中显示固定「收起」按钮；到底或离开 section 后隐藏
  const [sectionVisible, setSectionVisible] = useState(false)
  useEffect(() => {
    if (!isExpanded || !masonryInnerRef.current) {
      setSectionVisible(false)
      return
    }
    const check = () => {
      const rect = masonryInnerRef.current!.getBoundingClientRect()
      setSectionVisible(rect.top < window.innerHeight && rect.bottom > window.innerHeight)
    }
    check()
    window.addEventListener('scroll', check, { passive: true })
    return () => window.removeEventListener('scroll', check)
  }, [isExpanded])

  // 只有新追加的卡片做入场动画
  const prevVisibleCountRef = useRef(0)
  useEffect(() => {
    prevVisibleCountRef.current = items.length
  }, [items.length])

  return (
    <section
      ref={rootRef}
      id={`s-${sectionKey}`}
      className="mb-16"
      style={{ scrollMarginTop: '120px' }}
      data-testid="section-front"
    >
      {label != null && (
        <div
          className="z-10 bg-background sm:sticky sm:top-[92px]"
          data-testid="section-front-head"
        >
          {/* 双线 motif：2px×26px brand 短标线压通栏 hairline */}
          <div className="relative h-[2px]" data-testid="section-front-rule" aria-hidden="true">
            <span className="absolute inset-x-0 top-0 h-px bg-border" />
            <span className="absolute left-0 top-0 h-[2px] w-[26px] bg-[var(--brand)]" />
          </div>
          <div className="flex flex-wrap items-center gap-x-3 pb-1.5 pt-2 sm:h-[42px] sm:flex-nowrap sm:py-0">
            <h2 className="shrink-0 font-event-title text-[22px] font-bold leading-none text-foreground">
              {label}
            </h2>
            {count != null && (
              <span className="shrink-0 font-body-cjk text-[13px] font-normal text-muted-foreground">{count} 条</span>
            )}
            {pillBar && (
              <div className="ml-auto flex w-full min-w-0 grow justify-end sm:w-auto" data-testid="section-front-l2">
                {pillBar}
              </div>
            )}
          </div>
        </div>
      )}

      {hasBeenVisible ? (
        <>
          {/* Card masonry with horizontal clip line + gradient mask */}
          <div
            className={cn(
              'relative mt-4 transition-opacity duration-150',
              hasMore && 'overflow-hidden',
              filterLoading && 'opacity-80',
            )}
            aria-busy={filterLoading || undefined}
            style={clipMaxHeight != null ? { maxHeight: `${clipMaxHeight}px` } : undefined}
            data-testid="section-front-body"
          >
            <div ref={masonryInnerRef}>
              <Masonry
                items={items}
                renderItem={(item, i) => {
                  // Only animate cards that are newly added (beyond previous count)
                  const isNew = i >= prevVisibleCountRef.current
                  const delay = isNew ? Math.min(i - prevVisibleCountRef.current, 19) * 30 : 0
                  return <InfoCard key={item.id} item={item} delay={delay} showReadState={showReadState} />
                }}
              />
            </div>

            {/* Gradient mask — horizontal cut across all columns */}
            {hasMore && clipMaxHeight != null && (
              <div className="pointer-events-none absolute bottom-0 left-0 right-0 h-24 bg-gradient-to-t from-background to-transparent" />
            )}
          </div>

          {/* 展开按钮：hairline 边框、无阴影（v24 §21.6 保留件，替换旧阴影胶囊） */}
          {hasMore && (
            <div className="mt-6 flex justify-center">
              <button
                onClick={onLoadMore}
                className="flex cursor-pointer items-center gap-1.5 rounded-[4px] border border-border bg-card px-5 py-2 text-sm font-medium text-muted-foreground transition-colors hover:border-[var(--brand-border)] hover:text-foreground"
                data-testid="section-front-expand"
              >
                展开更多
                {remaining > 0 && <span className="font-mono text-xs">· 还有 {remaining} 条</span>}
                <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
              </button>
            </div>
          )}
        </>
      ) : (
        /* IO 懒渲染占位：与瀑布流卡片同形 */
        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3" data-testid="section-front-skeleton">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="h-48 animate-skeleton rounded-[4px] bg-muted" />
          ))}
        </div>
      )}

      {/* 固定「收起」按钮 —— 仅展开滚动途中可见；收起滚回 section 顶由容器处理 */}
      {isExpanded && sectionVisible && (
        <div className="fixed bottom-6 left-1/2 z-[90] -translate-x-1/2">
          <button
            onClick={onCollapse}
            className="flex cursor-pointer items-center gap-1.5 rounded-[4px] border border-border bg-card px-5 py-2 text-sm font-medium text-foreground transition-colors hover:border-[var(--brand-border)]"
            data-testid="section-front-collapse"
          >
            收起
            <ChevronUp className="h-3.5 w-3.5 text-muted-foreground" />
          </button>
        </div>
      )}
    </section>
  )
}
