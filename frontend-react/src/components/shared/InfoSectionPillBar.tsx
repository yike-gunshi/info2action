import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { cn } from '../../lib/utils'

export interface InfoSectionPillItem {
  key: string | null
  label: string
  title?: string
}

export interface InfoSectionPillRow {
  items: InfoSectionPillItem[]
  activeKey: string | null
  onSelect: (nextKey: string | null) => void
  prefix?: string
  ariaLabel?: string
}

export interface InfoSectionPillBarProps {
  sectionKey: string
  items: InfoSectionPillItem[]
  activeKey: string | null
  onSelect: (nextKey: string | null) => void
  nestedRows?: InfoSectionPillRow[]
  className?: string
  'data-testid'?: string
}

const DESKTOP_TOPBAR_FALLBACK = 52
const MOBILE_TOPBAR_FALLBACK = 84
const SUBBAR_FALLBACK = 49

export function getInfoSectionStickyTop(): number {
  if (typeof window === 'undefined') return DESKTOP_TOPBAR_FALLBACK + SUBBAR_FALLBACK
  const topbar = document.querySelector<HTMLElement>('[data-testid="topbar"]')
  const subbar = document.querySelector<HTMLElement>('[data-testid="info-subbar"]')
  const subbarBottom = subbar?.getBoundingClientRect().bottom
  if (subbarBottom != null && subbarBottom > 0) {
    return subbarBottom
  }
  const topbarHeight = topbar?.getBoundingClientRect().height
    ?? (window.innerWidth < 640 ? MOBILE_TOPBAR_FALLBACK : DESKTOP_TOPBAR_FALLBACK)
  const subbarHeight = subbar?.getBoundingClientRect().height ?? SUBBAR_FALLBACK
  return topbarHeight + subbarHeight
}

export function scrollInfoSectionToTop(sectionKey: string): void {
  if (typeof window === 'undefined') return
  const section = document.getElementById(`s-${sectionKey}`)
  if (!section) return
  const stickyTop = getInfoSectionStickyTop()
  const top = section.getBoundingClientRect().top + window.scrollY - stickyTop
  window.scrollTo({ top: Math.max(0, top), behavior: 'smooth' })
}

function InfoSectionPillRowView({
  row,
  sectionKey,
  rowIndex,
}: {
  row: InfoSectionPillRow
  sectionKey: string
  rowIndex: number
}) {
  const railRef = useRef<HTMLDivElement>(null)
  const [scrollState, setScrollState] = useState({
    hasOverflow: false,
    canLeft: false,
    canRight: false,
  })

  const updateScrollState = useCallback(() => {
    const el = railRef.current
    if (!el) return
    const maxScrollLeft = Math.max(0, el.scrollWidth - el.clientWidth)
    const hasOverflow = el.scrollWidth > el.clientWidth + 2
    setScrollState({
      hasOverflow,
      canLeft: hasOverflow && el.scrollLeft > 2,
      canRight: hasOverflow && el.scrollLeft < maxScrollLeft - 2,
    })
  }, [])

  const scrollRail = useCallback((direction: 'left' | 'right') => {
    railRef.current?.scrollBy({
      left: direction === 'left' ? -240 : 240,
      behavior: 'smooth',
    })
    window.setTimeout(updateScrollState, 240)
  }, [updateScrollState])

  useEffect(() => {
    updateScrollState()
    const el = railRef.current
    if (!el) return
    el.addEventListener('scroll', updateScrollState, { passive: true })
    window.addEventListener('resize', updateScrollState)
    const raf = window.requestAnimationFrame(updateScrollState)
    return () => {
      el.removeEventListener('scroll', updateScrollState)
      window.removeEventListener('resize', updateScrollState)
      window.cancelAnimationFrame(raf)
    }
  }, [row.items, updateScrollState])

  return (
    <div
      className={cn(
        'group/section-pill relative h-10',
        rowIndex > 0 && 'border-t border-border/60',
      )}
      aria-label={row.ariaLabel}
      data-testid="info-section-pill-row-shell"
    >
      {scrollState.hasOverflow && (
        <>
          <span
            className={cn(
              'pointer-events-none absolute inset-y-0 left-0 z-10 w-8 bg-gradient-to-r from-background to-transparent opacity-0 transition-opacity duration-150',
              scrollState.canLeft && 'group-hover/section-pill:opacity-100',
            )}
            aria-hidden="true"
            data-testid={`info-section-pill-fade-left-${sectionKey}-${rowIndex}`}
          />
          <span
            className={cn(
              'pointer-events-none absolute inset-y-0 right-0 z-10 w-8 bg-gradient-to-l from-background to-transparent opacity-0 transition-opacity duration-150',
              scrollState.canRight && 'group-hover/section-pill:opacity-100',
            )}
            aria-hidden="true"
            data-testid={`info-section-pill-fade-right-${sectionKey}-${rowIndex}`}
          />
          <button
            type="button"
            onClick={() => scrollRail('left')}
            className={cn(
              'absolute left-0 top-1/2 z-20 hidden h-10 w-5 -translate-y-1/2 items-center justify-center bg-transparent px-0 text-muted-foreground/55 transition-[color,opacity] duration-150 hover:text-muted-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background sm:inline-flex',
              scrollState.canLeft
                ? 'opacity-0 group-hover/section-pill:opacity-80 focus:opacity-100'
                : 'pointer-events-none opacity-0',
            )}
            aria-label="向左滚动 section 筛选"
            disabled={!scrollState.canLeft}
            data-testid={`info-section-pill-chevron-left-${sectionKey}-${rowIndex}`}
          >
            <ChevronLeft className="h-4 w-4" aria-hidden="true" />
          </button>
          <button
            type="button"
            onClick={() => scrollRail('right')}
            className={cn(
              'absolute right-0 top-1/2 z-20 hidden h-10 w-5 -translate-y-1/2 items-center justify-center bg-transparent px-0 text-muted-foreground/55 transition-[color,opacity] duration-150 hover:text-muted-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background sm:inline-flex',
              scrollState.canRight
                ? 'opacity-0 group-hover/section-pill:opacity-80 focus:opacity-100'
                : 'pointer-events-none opacity-0',
            )}
            aria-label="向右滚动 section 筛选"
            disabled={!scrollState.canRight}
            data-testid={`info-section-pill-chevron-right-${sectionKey}-${rowIndex}`}
          >
            <ChevronRight className="h-4 w-4" aria-hidden="true" />
          </button>
        </>
      )}

      <div
        ref={railRef}
        className={cn(
          'flex h-10 flex-nowrap items-center gap-5 overflow-x-auto px-1 scrollbar-hide sm:gap-6',
          rowIndex > 0 && 'gap-4 sm:gap-5',
        )}
        data-testid="info-section-pill-row"
      >
        {row.prefix && (
          <span className="mr-1 shrink-0 whitespace-nowrap font-body-cjk text-[13px] font-medium text-muted-foreground">
            {row.prefix}
          </span>
        )}
        {row.items.map((item) => {
          const selected = row.activeKey === item.key
          const testKey = item.key ?? 'all'
          return (
            <button
              key={testKey}
              type="button"
              title={item.title}
              aria-pressed={selected}
              onClick={() => {
                const nextKey = selected ? null : item.key
                row.onSelect(nextKey)
              }}
              className={cn(
                'flex h-full shrink-0 items-center whitespace-nowrap border-b-2 px-0.5 font-body-cjk text-[14px] font-medium tracking-normal transition-colors duration-150',
                'cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background',
                selected
                  ? 'border-[var(--brand)] text-[var(--brand)]'
                  : 'border-transparent text-muted-foreground hover:text-foreground',
              )}
              data-testid={`info-section-pill-${sectionKey}-${testKey}`}
            >
              {item.label}
            </button>
          )
        })}
      </div>
    </div>
  )
}

export function InfoSectionPillBar({
  sectionKey,
  items,
  activeKey,
  onSelect,
  nestedRows,
  className,
  'data-testid': testId = 'info-section-pill-bar',
}: InfoSectionPillBarProps) {
  const rows: InfoSectionPillRow[] = [
    { items, activeKey, onSelect, ariaLabel: 'section 二级筛选' },
    ...(nestedRows ?? []),
  ].filter((row) => row.items.length > 0)

  if (rows.length === 0) return null

  return (
    <div
      className={cn(
        'mb-4 border-b border-border/70 bg-background py-0',
        className,
      )}
      data-testid={testId}
      data-section-key={sectionKey}
    >
      {rows.map((row, index) => (
        <InfoSectionPillRowView
          key={`${row.prefix ?? 'row'}-${index}`}
          row={row}
          sectionKey={sectionKey}
          rowIndex={index}
        />
      ))}
    </div>
  )
}
