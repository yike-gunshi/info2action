import { useState, useEffect, useRef, useCallback } from 'react'
import { useSectionItems, usePlatformSections } from '../../store/feedStore'
import { PLATFORM_ORDER } from '../../lib/platforms'
import { platformName, cn } from '../../lib/utils'

/**
 * L2 anchor pills — scroll-to-section navigation with active tracking.
 *
 * v18.0 nav-merge §6: 3 tab 模式下整体不再渲染。
 * - 信息 tab 内部已有 source pill + L1 pill 内嵌实现（ChannelsView），不依赖外层
 * - CategoryPills / PlatformPills 子组件保留作为 v18.1 死代码清理候选
 *
 * 历史：v17.0 之前 recommend tab 用 CategoryPills，channels tab 用 PlatformPills。
 */
export function L2Pills() {
  // v18.0: 3 tab 模式下永不渲染
  return null
}

// 标记 unused-but-kept 子组件依赖，防止 noUnusedLocals 报错（保留代码作为
// v18.1 死代码清理候选）
const _v18_unused = { CategoryPills, PlatformPills }
void _v18_unused

/**
 * Track which section is currently in view.
 * - Near top (scrollY < 250): returns null → "全部" is active
 * - During programmatic scroll (pill click): suppressed for 600ms to prevent flicker
 * - Otherwise: IntersectionObserver determines active section
 */
function useActiveSection(sectionKeys: string[]): [string | null, (key: string | null) => void] {
  const [active, setActive] = useState<string | null>(null)
  const [nearTop, setNearTop] = useState(true)
  const keysRef = useRef(sectionKeys)
  keysRef.current = sectionKeys

  // Suppress observer during programmatic scroll
  const scrollingRef = useRef(false)

  const scrollTargetRef = useRef<string | null>(null)
  const fallbackTimerRef = useRef<ReturnType<typeof setTimeout>>()
  const scrollEndHandlerRef = useRef<(() => void) | null>(null)

  const scrollTo = useCallback((key: string | null) => {
    // Clean up previous scroll listener if still pending
    if (scrollEndHandlerRef.current) {
      window.removeEventListener('scrollend', scrollEndHandlerRef.current)
      scrollEndHandlerRef.current = null
    }
    if (fallbackTimerRef.current) clearTimeout(fallbackTimerRef.current)

    scrollingRef.current = true
    scrollTargetRef.current = key
    setActive(key)

    if (key === null) {
      window.scrollTo({ top: 0, behavior: 'smooth' })
    } else {
      document.getElementById(`s-${key}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }

    // Use scrollend event to detect when smooth scroll finishes
    const onScrollEnd = () => {
      if (fallbackTimerRef.current) clearTimeout(fallbackTimerRef.current)
      scrollEndHandlerRef.current = null
      scrollingRef.current = false
      setActive(scrollTargetRef.current)
    }
    scrollEndHandlerRef.current = onScrollEnd
    window.addEventListener('scrollend', onScrollEnd, { once: true })

    // Fallback: if scrollend never fires (older browsers), unlock after 2s
    fallbackTimerRef.current = setTimeout(() => {
      if (scrollEndHandlerRef.current) {
        window.removeEventListener('scrollend', scrollEndHandlerRef.current)
        scrollEndHandlerRef.current = null
      }
      scrollingRef.current = false
      setActive(scrollTargetRef.current)
    }, 2000)
  }, [])

  // Track scroll position — near top means "全部"
  useEffect(() => {
    const check = () => setNearTop(window.scrollY < 250)
    check()
    window.addEventListener('scroll', check, { passive: true })
    return () => window.removeEventListener('scroll', check)
  }, [])

  // IntersectionObserver for section tracking
  useEffect(() => {
    if (sectionKeys.length === 0) return

    const visibleMap = new Map<string, boolean>()

    const observer = new IntersectionObserver(
      (observations) => {
        if (scrollingRef.current) {
          // During programmatic scroll: still update map but don't setActive
          for (const entry of observations) {
            visibleMap.set(entry.target.id, entry.isIntersecting)
          }
          return
        }

        for (const entry of observations) {
          visibleMap.set(entry.target.id, entry.isIntersecting)
        }
        for (const key of keysRef.current) {
          if (visibleMap.get(`s-${key}`)) {
            setActive(key)
            return
          }
        }
        setActive(null)
      },
      { rootMargin: '-140px 0px -40% 0px' },
    )

    for (const key of sectionKeys) {
      const el = document.getElementById(`s-${key}`)
      if (el) observer.observe(el)
    }

    return () => observer.disconnect()
  }, [sectionKeys.join(',')])

  // When near top and not explicitly scrolling, default to first section
  const firstKey = keysRef.current[0] ?? null
  const result = (nearTop && !scrollingRef.current) ? firstKey : (active ?? firstKey)
  return [result, scrollTo]
}

function CategoryPills() {
  const sections = useSectionItems()
  const keys = sections.map((s) => s.key)
  const [active, scrollTo] = useActiveSection(keys)

  if (sections.length === 0) return null

  return (
    <PillBar>
      {sections.map((sec) => (
        <Pill
          key={sec.key}
          label={sec.label}
          tooltip={`${sec.label}: ${sec.count} 条`}
          isActive={active === sec.key}
          onClick={() => scrollTo(sec.key)}
        />
      ))}
    </PillBar>
  )
}

function PlatformPills() {
  const sections = usePlatformSections()
  const sorted = [...sections]
    .filter((s) => PLATFORM_ORDER.includes(s.key))
    .sort((a, b) => PLATFORM_ORDER.indexOf(a.key) - PLATFORM_ORDER.indexOf(b.key))
  const keys = sorted.map((s) => s.key)
  const [active, scrollTo] = useActiveSection(keys)

  if (sorted.length === 0) return null

  return (
    <PillBar>
      {sorted.map((sec) => (
        <Pill
          key={sec.key}
          label={platformName(sec.key)}
          tooltip={`${platformName(sec.key)}: ${sec.count} 条`}
          isActive={active === sec.key}
          onClick={() => scrollTo(sec.key)}
        />
      ))}
    </PillBar>
  )
}

function PillBar({ children }: { children: React.ReactNode }) {
  return (
    <div className="sticky top-14 z-[99] flex items-center gap-1.5 px-4 py-2 bg-card border-b border-border overflow-x-auto scrollbar-hide">
      {children}
    </div>
  )
}

function Pill({
  label,
  tooltip,
  isActive,
  onClick,
}: {
  label: string
  tooltip: string
  isActive: boolean
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'flex-shrink-0 px-3 py-1.5 text-[13px] font-medium rounded-full whitespace-nowrap cursor-pointer transition-colors duration-150',
        isActive
          ? 'bg-foreground text-background'
          : 'text-muted-foreground hover:text-foreground hover:bg-muted',
      )}
      title={tooltip}
    >
      {label}
    </button>
  )
}
