import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { PLATFORM_ORDER } from '../../lib/platforms'
import { cn, platformName } from '../../lib/utils'
import { useFeedStore } from '../../store/feedStore'
import { useUIStore } from '../../store/uiStore'
import { InfoGroupByToggle, type InfoGroupBy } from './InfoGroupByToggle'

const HIDDEN_CATEGORY_IDS = new Set(['other', '_uncategorized'])

interface SidebarGroup {
  key: string
  label: string
}

const FALLBACK_CATEGORY_GROUPS: SidebarGroup[] = [
  { key: 'products', label: '产品' },
  { key: 'efficiency_tools', label: '工具' },
  { key: 'coding', label: 'Coding' },
  { key: 'skill', label: 'Skill' },
  { key: 'models', label: '模型' },
  { key: 'eval', label: '评测' },
  { key: 'tech', label: '技术' },
  { key: 'tutorials', label: '教程' },
  { key: 'industry', label: '行业' },
  { key: 'content_creation', label: '创作' },
  { key: 'investment', label: '投资' },
  { key: 'events', label: '事件' },
  { key: 'startup', label: '创业' },
]

export interface InfoSidebarProps {
  groupBy: InfoGroupBy
  onGroupByChange: (next: InfoGroupBy) => void
  disabled?: boolean
}

function getStickyAnchorOffset(): number {
  const topbar = document.querySelector<HTMLElement>('[data-testid="topbar"]')
  const subbar = document.querySelector<HTMLElement>('[data-testid="info-subbar"]')
  const subbarBottom = subbar?.getBoundingClientRect().bottom
  if (subbarBottom != null && subbarBottom > 0) {
    return subbarBottom
  }
  const topbarHeight = topbar?.getBoundingClientRect().height || (window.innerWidth < 640 ? 84 : 52)
  const subbarHeight = subbar?.getBoundingClientRect().height || 49
  return topbarHeight + subbarHeight
}

function scrollSectionToStickyTop(key: string, behavior: ScrollBehavior): boolean {
  const section = document.getElementById(`s-${key}`)
  if (!section) return false
  const stickyOffset = getStickyAnchorOffset()
  const top = section.getBoundingClientRect().top + window.scrollY - stickyOffset
  window.scrollTo({ top: Math.max(0, top), behavior })
  return true
}

const INFO_SECTION_SCROLL_SETTLE_DELAYS_MS = [160, 480, 900]
const INFO_SECTION_SCROLL_LOCK_MS = 1250

function useActiveInfoSection(sectionKeys: string[]): [string | null, (key: string) => void] {
  const firstKey = sectionKeys[0] ?? null
  const [active, setActive] = useState<string | null>(firstKey)
  const keysRef = useRef(sectionKeys)
  const scrollingRef = useRef(false)
  const fallbackTimerRef = useRef<ReturnType<typeof setTimeout>>()
  const settleTimerRefs = useRef<ReturnType<typeof setTimeout>[]>([])
  keysRef.current = sectionKeys

  const clearSettleTimers = useCallback(() => {
    for (const timer of settleTimerRefs.current) {
      clearTimeout(timer)
    }
    settleTimerRefs.current = []
  }, [])

  const resolveActiveFromScroll = useCallback(() => {
    const keys = keysRef.current
    if (keys.length === 0) {
      setActive(null)
      return
    }
    const anchorY = getStickyAnchorOffset() + 2
    let nextActive = keys[0]
    for (const key of keys) {
      const el = document.getElementById(`s-${key}`)
      if (!el) continue
      const rect = el.getBoundingClientRect()
      if (rect.top <= anchorY) {
        nextActive = key
      } else {
        break
      }
    }
    setActive((current) => (current === nextActive ? current : nextActive))
  }, [])

  useEffect(() => {
    setActive((current) => (current && sectionKeys.includes(current) ? current : firstKey))
  }, [firstKey, sectionKeys])

  const scrollTo = useCallback((key: string) => {
    if (fallbackTimerRef.current) clearTimeout(fallbackTimerRef.current)
    clearSettleTimers()
    scrollingRef.current = true
    setActive(key)
    const didScroll = scrollSectionToStickyTop(key, 'smooth')
    if (didScroll) {
      settleTimerRefs.current = INFO_SECTION_SCROLL_SETTLE_DELAYS_MS.map((delay) => (
        setTimeout(() => {
          if (!scrollingRef.current) return
          scrollSectionToStickyTop(key, 'auto')
          setActive(key)
        }, delay)
      ))
    }
    fallbackTimerRef.current = setTimeout(() => {
      scrollingRef.current = false
      clearSettleTimers()
      if (didScroll) {
        scrollSectionToStickyTop(key, 'auto')
        setActive(key)
        window.requestAnimationFrame(resolveActiveFromScroll)
      } else {
        resolveActiveFromScroll()
      }
    }, INFO_SECTION_SCROLL_LOCK_MS)
  }, [clearSettleTimers, resolveActiveFromScroll])

  const l1Active = useUIStore((s) => s.l1 === 'info')

  useEffect(() => {
    if (sectionKeys.length === 0) return
    // FE-8(Wave C): info tab 隐藏(display:none 常驻)时不挂滚动监听——
    // 原先在精选页滚动时也逐 section 做 gBCR,白耗帧预算
    if (!l1Active) return
    let raf = 0
    const schedule = () => {
      if (scrollingRef.current || raf) return
      raf = window.requestAnimationFrame(() => {
        raf = 0
        resolveActiveFromScroll()
      })
    }
    schedule()
    window.addEventListener('scroll', schedule, { passive: true })
    window.addEventListener('resize', schedule)
    return () => {
      window.removeEventListener('scroll', schedule)
      window.removeEventListener('resize', schedule)
      if (raf) window.cancelAnimationFrame(raf)
    }
  }, [sectionKeys, resolveActiveFromScroll, l1Active])

  useEffect(() => () => {
    if (fallbackTimerRef.current) clearTimeout(fallbackTimerRef.current)
    clearSettleTimers()
  }, [clearSettleTimers])

  return [active ?? firstKey, scrollTo]
}

export function InfoSidebar({ groupBy, onGroupByChange, disabled = false }: InfoSidebarProps) {
  const classification = useFeedStore((s) => s.classification)
  const navScrollRef = useRef<HTMLElement>(null)
  const [scrollState, setScrollState] = useState({
    hasOverflow: false,
    canLeft: false,
    canRight: false,
  })

  const groups = useMemo<SidebarGroup[]>(() => {
    if (groupBy === 'platform') {
      return PLATFORM_ORDER.map((key) => ({ key, label: platformName(key) }))
    }
    if (!classification) return FALLBACK_CATEGORY_GROUPS
    return classification.categories
      .filter((cat) => cat.visible && !HIDDEN_CATEGORY_IDS.has(cat.id))
      .sort((a, b) => (a.priority ?? 99) - (b.priority ?? 99))
      .map((cat) => ({ key: cat.id, label: cat.name }))
  }, [classification, groupBy])

  const keys = useMemo(() => groups.map((group) => group.key), [groups])
  const [active, scrollTo] = useActiveInfoSection(keys)
  const updateScrollState = useCallback(() => {
    const el = navScrollRef.current
    if (!el) return
    const maxScrollLeft = Math.max(0, el.scrollWidth - el.clientWidth)
    const hasOverflow = el.scrollWidth > el.clientWidth + 2
    setScrollState({
      hasOverflow,
      canLeft: hasOverflow && el.scrollLeft > 2,
      canRight: hasOverflow && el.scrollLeft < maxScrollLeft - 2,
    })
  }, [])
  const scrollGroups = useCallback((direction: 'left' | 'right') => {
    navScrollRef.current?.scrollBy({
      left: direction === 'left' ? -240 : 240,
      behavior: 'smooth',
    })
    window.setTimeout(updateScrollState, 240)
  }, [updateScrollState])

  useEffect(() => {
    updateScrollState()
    const el = navScrollRef.current
    if (!el) return
    el.addEventListener('scroll', updateScrollState, { passive: true })
    window.addEventListener('resize', updateScrollState)
    return () => {
      el.removeEventListener('scroll', updateScrollState)
      window.removeEventListener('resize', updateScrollState)
    }
  }, [groups, updateScrollState])

  useEffect(() => {
    const raf = window.requestAnimationFrame(updateScrollState)
    return () => window.cancelAnimationFrame(raf)
  }, [groups, updateScrollState])

  return (
    <aside
      className="sticky top-[84px] z-20 -mx-4 mb-0 h-10 bg-background px-4 py-0 sm:top-[52px]"
      aria-label="信息分类导航"
      data-testid="info-subbar"
    >
      <div
        className="mx-auto flex h-10 w-full max-w-[1168px] items-center justify-center border-b border-border/70 sm:px-1"
        data-testid="info-subbar-inner"
      >
        <div
          className="group/l2 flex h-10 w-full min-w-0 items-center justify-center gap-6 sm:gap-8"
          data-testid="info-l2-rail"
        >
          <div className="min-w-0 shrink-0" data-testid="info-groupby-row">
            <InfoGroupByToggle
              groupBy={groupBy}
              onChange={onGroupByChange}
              disabled={disabled}
            />
          </div>

          <span
            className="select-none font-event-title text-[16px] font-medium text-muted-foreground/45"
            aria-hidden="true"
            data-testid="info-l2-divider"
          >
            |
          </span>

          <div
            className="relative flex h-10 min-w-0 max-w-full items-center"
            data-testid="info-group-nav-shell"
          >
            {/* v24 §21.3 导航增强：溢出渐隐从 group-hover 改常显（触屏无 hover） */}
            {scrollState.hasOverflow && (
              <>
                <span
                  className={cn(
                    'pointer-events-none absolute inset-y-0 left-0 z-10 w-8 bg-gradient-to-r from-background to-transparent transition-opacity duration-150',
                    scrollState.canLeft ? 'opacity-100' : 'opacity-0',
                  )}
                  aria-hidden="true"
                  data-testid="info-group-fade-left"
                />
                <span
                  className={cn(
                    'pointer-events-none absolute inset-y-0 right-0 z-10 w-8 bg-gradient-to-l from-background to-transparent transition-opacity duration-150',
                    scrollState.canRight ? 'opacity-100' : 'opacity-0',
                  )}
                  aria-hidden="true"
                  data-testid="info-group-fade-right"
                />
                <button
                  type="button"
                  onClick={() => scrollGroups('left')}
                  className={cn(
                    'absolute left-0 top-1/2 z-20 hidden h-10 w-5 -translate-y-1/2 items-center justify-center bg-transparent px-0 text-muted-foreground/55 transition-[color,opacity] duration-150 hover:text-muted-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background sm:inline-flex',
                    scrollState.canLeft
                      ? 'opacity-80 hover:opacity-100 focus:opacity-100'
                      : 'pointer-events-none opacity-0',
                  )}
                  aria-label="向左滚动分组"
                  disabled={!scrollState.canLeft}
                  data-testid="info-group-chevron-left"
                >
                  <ChevronLeft className="h-4 w-4" aria-hidden="true" />
                </button>
                <button
                  type="button"
                  onClick={() => scrollGroups('right')}
                  className={cn(
                    'absolute right-0 top-1/2 z-20 hidden h-10 w-5 -translate-y-1/2 items-center justify-center bg-transparent px-0 text-muted-foreground/55 transition-[color,opacity] duration-150 hover:text-muted-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background sm:inline-flex',
                    scrollState.canRight
                      ? 'opacity-80 hover:opacity-100 focus:opacity-100'
                      : 'pointer-events-none opacity-0',
                  )}
                  aria-label="向右滚动分组"
                  disabled={!scrollState.canRight}
                  data-testid="info-group-chevron-right"
                >
                  <ChevronRight className="h-4 w-4" aria-hidden="true" />
                </button>
              </>
            )}
            <nav
              ref={navScrollRef}
              className={cn(
                'flex h-10 min-w-0 max-w-full items-center gap-6 overflow-x-auto scrollbar-hide sm:gap-8',
                scrollState.hasOverflow ? 'justify-start' : 'justify-center',
              )}
              aria-label="信息分组"
              data-testid="info-group-nav"
            >
              {groups.map((group) => {
                const selected = active === group.key
                return (
                  <button
                    key={group.key}
                    type="button"
                    onClick={() => scrollTo(group.key)}
                    className={cn(
                      'relative flex h-full shrink-0 items-center border-b-2 px-0.5 text-left font-event-title text-[16px] font-medium tracking-normal transition-colors',
                      'focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background',
                      selected
                        ? 'border-[var(--brand)] text-[var(--brand)]'
                        : 'border-transparent text-muted-foreground hover:text-foreground',
                    )}
                    aria-current={selected ? 'true' : undefined}
                    data-testid={`info-group-${group.key}`}
                  >
                    <span className="whitespace-nowrap">{group.label}</span>
                  </button>
                )
              })}
            </nav>
          </div>
        </div>
      </div>
    </aside>
  )
}
