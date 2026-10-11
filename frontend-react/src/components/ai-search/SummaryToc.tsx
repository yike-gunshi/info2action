import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react'
import { useAiSearchStore } from '../../store/aiSearchStore'
import { keepFocusVisible } from './aiSearchScroll'
import './SummaryToc.css'

const scrollKeys = new Set(['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '])

export function SummaryToc({ paneRef, reserveSpace = false, loading = false }: {
  paneRef: RefObject<HTMLDivElement>
  reserveSpace?: boolean
  loading?: boolean
}) {
  const contentMd = useAiSearchStore(state => state.summary.contentMd)
  const status = useAiSearchStore(state => state.summary.status)
  const jobId = useAiSearchStore(state => state.jobId)
  const [wide, setWide] = useState(false)
  const [headings, setHeadings] = useState<HTMLHeadingElement[]>([])
  const [active, setActive] = useState(0)
  const locked = useRef<number | null>(null)

  useLayoutEffect(() => {
    const measure = () => {
      const breakpoint = Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--ai-toc-bp'))
      setWide(Number.isFinite(breakpoint) && window.innerWidth >= breakpoint)
    }
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [])

  useLayoutEffect(() => {
    locked.current = null
    setActive(0)
    setHeadings(wide && status === 'done'
      ? Array.from(paneRef.current?.querySelectorAll<HTMLHeadingElement>('.ai-summary-article h4') ?? [])
      : [])
  }, [wide, status, contentMd, jobId, paneRef])

  useEffect(() => {
    const pane = paneRef.current
    if (!pane || !headings.length) return
    const mark = () => {
      if (locked.current !== null) return
      const line = pane.getBoundingClientRect().top + readingInset(pane)
      let current = 0
      headings.forEach((heading, index) => {
        if (heading.getBoundingClientRect().top <= line + 1) current = index
      })
      if (pane.scrollTop + pane.clientHeight >= pane.scrollHeight - 2) current = headings.length - 1
      setActive(current)
    }
    const unlock = (event: Event) => {
      const target = event.target
      if (!(target instanceof Element)) return
      const page = pane.closest('.ai-search-page')
      if (target !== document.body && target !== document.documentElement && !page?.contains(target)) return
      if (target.closest('[data-ai-scroll="cards"], [role="dialog"], [role="menu"]')) return
      if (event instanceof WheelEvent && event.ctrlKey) return
      if (event instanceof KeyboardEvent) {
        if (!scrollKeys.has(event.key) || event.ctrlKey || event.metaKey || event.altKey || event.defaultPrevented) return
        if (target.closest('input, textarea, select, [contenteditable="true"], [role="textbox"]')) return
        if (event.key === ' ' && target.closest('button, a')) return
      }
      locked.current = null
    }
    mark()
    pane.addEventListener('scroll', mark, { passive: true })
    // Capture runs before the page's wheel router. Scroll events from our smooth
    // scroll never unlock the selected heading; only user scroll input can.
    window.addEventListener('wheel', unlock, { passive: true, capture: true })
    window.addEventListener('touchstart', unlock, { passive: true, capture: true })
    window.addEventListener('keydown', unlock)
    return () => {
      pane.removeEventListener('scroll', mark)
      window.removeEventListener('wheel', unlock, true)
      window.removeEventListener('touchstart', unlock, true)
      window.removeEventListener('keydown', unlock)
    }
  }, [headings, paneRef])

  if (!wide || (!reserveSpace && !headings.length)) return null

  if (loading) return (
    <nav className="ai-search-toc" aria-hidden="true">
      <div className="ai-search-toc-inner">
        <div className="ai-search-toc-list">
          {Array.from({ length: 6 }, (_, index) => (
            <div key={index} className="ai-search-toc-skeleton-row">
              <span className="ai-search-toc-skeleton-bar animate-pulse motion-reduce:animate-none" />
            </div>
          ))}
        </div>
      </div>
    </nav>
  )

  if (!headings.length) return <nav className="ai-search-toc" aria-hidden="true" />

  return (
    <nav className="ai-search-toc" aria-label="目录">
      <div className="ai-search-toc-inner" data-ai-scroll="toc" onFocusCapture={keepFocusVisible}>
        <div className="ai-search-toc-list">
          {headings.map((heading, index) => (
            <button
              key={index}
              type="button"
              aria-current={active === index ? 'location' : undefined}
              onClick={() => {
                const pane = paneRef.current
                if (!pane) return
                locked.current = index
                setActive(index)
                const top = heading.getBoundingClientRect().top - pane.getBoundingClientRect().top + pane.scrollTop
                pane.scrollTo({
                  top: Math.max(0, top - readingInset(pane)),
                  behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
                })
              }}
            >
              {heading.textContent}
            </button>
          ))}
        </div>
      </div>
    </nav>
  )
}

function readingInset(pane: HTMLElement): number {
  const fade = Number.parseFloat(getComputedStyle(pane).getPropertyValue('--ai-pane-fade')) || 0
  // Fixed 8px reading inset matches the skeleton's container scroll calculation.
  return fade + 8
}
