/**
 * v19 HighlightsView — Image2 约束化「精选」tab。
 *
 * 01-highlights-v2 锁定为开放式编辑流：无外层卡片、自然页面滚动。
 * 精选页保留轻量 L1 分类切换，用于快速收拢模型、评测等阅读场景。
 */
import { LatestEvents } from '../events/LatestEvents'
import { HighlightsFilterTabs } from './HighlightsFilterTabs'
import { HighlightsDateDirectory } from './HighlightsDateDirectory'
import { useHighlightsDateNavigation } from '../../hooks/useHighlightsDateNavigation'
import { useUIStore } from '../../store/uiStore'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import * as Popover from '@radix-ui/react-popover'
import { ChevronDown } from 'lucide-react'

export function HighlightsView() {
  const root = useRef<HTMLDivElement>(null)
  const { activeDate, selectDate, backToLatest } = useHighlightsDateNavigation(root)
  const [desktop, setDesktop] = useState(() => window.matchMedia?.('(min-width: 1280px)').matches ?? window.innerWidth >= 1280)
  const [menuOpen, setMenuOpen] = useState(false)
  const highlightsVisible = useUIStore((s) => s.l1 === 'highlights')
  useLayoutEffect(() => {
    if (!highlightsVisible) return
    document.documentElement.classList.add('highlights-no-scrollbar')
    return () => document.documentElement.classList.remove('highlights-no-scrollbar')
  }, [highlightsVisible])
  useLayoutEffect(() => {
    const shell = root.current
    if (!desktop || !highlightsVisible || !shell) return
    let frame = 0
    let observedLabel: HTMLElement | null = null
    let observedFooter: HTMLElement | null = null
    const measure = () => {
      const group = shell.querySelector<HTMLElement>('[data-testid="event-date-group"]')
      const heading = group?.querySelector<HTMLElement>('[data-testid="event-date-heading"]')
      const label = heading?.querySelector<HTMLElement>('[data-testid="event-date-label"]')
      const footer = shell.querySelector<HTMLElement>('.highlights-date-footer')
      if (footer !== observedFooter) {
        if (observedFooter) resize?.unobserve(observedFooter)
        if (footer) resize?.observe(footer)
        observedFooter = footer
      }
      if (!group || !heading || !label || !footer) return
      if (label !== observedLabel) {
        if (observedLabel) resize?.unobserve(observedLabel)
        resize?.observe(label)
        observedLabel = label
      }
      const groupRect = group.getBoundingClientRect()
      const labelRect = label.getBoundingClientRect()
      // Keep the rail aligned with the date text in the section's normal flow.
      const labelCenter = labelRect.top - heading.getBoundingClientRect().top + labelRect.height / 2
      const values = {
        '--highlights-first-group-top': groupRect.top - shell.getBoundingClientRect().top,
        '--highlights-label-center': labelCenter,
        '--highlights-first-label-y': groupRect.top + window.scrollY + labelCenter,
        '--highlights-date-footer-height': footer.getBoundingClientRect().height,
      }
      for (const [name, value] of Object.entries(values)) {
        const pixels = `${value}px`
        if (shell.style.getPropertyValue(name) !== pixels) shell.style.setProperty(name, pixels)
      }
    }
    const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(measure) }
    const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule)
    const content = shell.querySelector('[data-testid="highlights-content"]')
    if (content) resize?.observe(content)
    const mutations = new MutationObserver(schedule)
    mutations.observe(shell, { childList: true, subtree: true })
    window.addEventListener('resize', schedule)
    measure()
    return () => {
      cancelAnimationFrame(frame)
      resize?.disconnect()
      mutations.disconnect()
      window.removeEventListener('resize', schedule)
    }
  }, [desktop, highlightsVisible])
  useEffect(() => {
    const media = window.matchMedia?.('(min-width: 1280px)')
    const update = () => { setDesktop(media?.matches ?? window.innerWidth >= 1280); setMenuOpen(false) }
    media?.addEventListener('change', update)
    return () => media?.removeEventListener('change', update)
  }, [])
  useEffect(() => {
    if (!highlightsVisible) setMenuOpen(false)
    const onVisibility = () => { if (document.visibilityState === 'hidden') setMenuOpen(false) }
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [highlightsVisible])
  const directory = <HighlightsDateDirectory activeDate={activeDate} onSelectDate={(date, keyboard) => { setMenuOpen(false); selectDate(date, keyboard) }} onBackToLatest={(keyboard) => { setMenuOpen(false); backToLatest(keyboard) }} />
  const dateMenu = desktop ? null : (
    <Popover.Root open={menuOpen} onOpenChange={setMenuOpen}>
      <Popover.Trigger asChild>
        <button type="button" data-date-navigation className="highlights-date-trigger" aria-label="选择日期">日期<ChevronDown size={14} aria-hidden="true" /></button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content align="end" sideOffset={4} className="highlights-date-popover" data-date-navigation aria-label="选择浏览日期">
          {directory}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
  return (
    <div ref={root} className="highlights-view mx-auto max-w-[1040px] px-5 pb-5 pt-0 sm:px-6 sm:pb-6 sm:pt-0 xl:max-w-[1136px] xl:px-0" data-testid="highlights-view-shell">
      {desktop ? <aside className="highlights-date-rail">{directory}</aside> : null}
      <div className="min-w-0" data-testid="highlights-content">
        <HighlightsFilterTabs dateControl={dateMenu} />
        <LatestEvents variant="page" showEmptyState />
      </div>
    </div>
  )
}
