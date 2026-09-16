import { useEffect, useMemo, useRef, useState } from 'react'
import { Loader2 } from 'lucide-react'
import { useEventsStore } from '../../store/eventsStore'
import { getBrowseableHighlightDates } from '../../lib/highlightsDates'

interface HighlightsDateDirectoryProps {
  activeDate: string | null
  onSelectDate: (date: string, keyboard: boolean) => void
  onBackToLatest: (keyboard: boolean) => void
}

export function HighlightsDateDirectory({ activeDate, onSelectDate, onBackToLatest }: HighlightsDateDirectoryProps) {
  const dateCounts = useEventsStore((s) => s.dateCounts)
  const navigation = useEventsStore((s) => s.navigation)
  const timelineStartDate = useEventsStore((s) => s.timelineStartDate)
  const loading = useEventsStore((s) => s.loading)
  const filtering = useEventsStore((s) => s.filtering)
  const error = useEventsStore((s) => s.error)
  const enabled = useEventsStore((s) => s.enabled)
  const blocked = useEventsStore((s) => Boolean(s.filtering || s.searchQuery.trim() || s.searching || (!s.navigation && (s.refreshing || (s.loading && !s.events.length)))))
  const searching = useEventsStore((s) => Boolean(s.searchQuery.trim() || s.searching))
  const dates = useMemo(() => getBrowseableHighlightDates(dateCounts), [dateCounts])
  const [visibleDate, setVisibleDate] = useState<string | null>(null)
  const root = useRef<HTMLDivElement>(null)
  const list = useRef<HTMLElement>(null)
  const year = (visibleDate && dates.includes(visibleDate) ? visibleDate : dates[0])?.slice(0, 4)
  const updateYear = () => {
    const scroller = list.current
    const rowHeight = scroller?.firstElementChild instanceof HTMLElement ? scroller.firstElementChild.offsetHeight : 0
    if (!scroller || !rowHeight) return
    const index = Math.min(dates.length - 1, Math.floor(Math.max(0, scroller.scrollTop - 4) / rowHeight))
    setVisibleDate(dates[index] ?? null)
  }

  useEffect(() => {
    const element = root.current
    const scroller = list.current
    if (!element || !scroller) return
    let touchY: number | null = null
    const wheel = (event: WheelEvent) => {
      if (event.ctrlKey) return
      event.preventDefault()
      event.stopPropagation()
      scroller.scrollTop += event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? scroller.clientHeight : 1)
    }
    const touchStart = (event: TouchEvent) => { touchY = event.touches[0]?.clientY ?? null }
    const touchMove = (event: TouchEvent) => {
      const y = event.touches[0]?.clientY
      if (touchY == null || y == null) return
      const delta = touchY - y
      touchY = y
      if ((delta < 0 && scroller.scrollTop <= 0) || (delta > 0 && scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight)) event.preventDefault()
      event.stopPropagation()
    }
    element.addEventListener('wheel', wheel, { passive: false })
    element.addEventListener('touchstart', touchStart, { passive: true })
    element.addEventListener('touchmove', touchMove, { passive: false })
    return () => {
      element.removeEventListener('wheel', wheel)
      element.removeEventListener('touchstart', touchStart)
      element.removeEventListener('touchmove', touchMove)
    }
  }, [])

  useEffect(() => {
    const scroller = list.current
    const current = scroller?.querySelector<HTMLElement>('[aria-current="date"]')
    if (!scroller || !current) return
    const outer = scroller.getBoundingClientRect()
    const inner = current.getBoundingClientRect()
    if (inner.top < outer.top) scroller.scrollTop -= outer.top - inner.top
    else if (inner.bottom > outer.bottom) scroller.scrollTop += inner.bottom - outer.bottom
  }, [activeDate])

  if (enabled === false) return null
  const pending = navigation && navigation.status !== 'error'
  const failure = navigation?.status === 'error' ? navigation.error : (!dates.length ? error : null)
  return (
    <div ref={root} data-date-navigation className="highlights-date-directory" onTouchStart={(event) => event.stopPropagation()} onTouchEnd={(event) => event.stopPropagation()}>
      {year ? <div className="highlights-date-year" aria-label={`${year}年`}>{year}</div> : null}
      <nav ref={list} aria-label="精选日期导航" className="highlights-date-list" onScroll={updateYear}>
        {dates.map((date) => (
          <div key={date} className="highlights-date-row">
            <button
              type="button"
              className="highlights-date-button"
              aria-label={date}
              aria-current={activeDate === date ? 'date' : undefined}
              disabled={blocked || enabled !== true || Boolean(pending && navigation.date === date)}
              onClick={(event) => onSelectDate(date, event.detail === 0)}
            >
              <span className="highlights-date-dot" aria-hidden="true" />
              <span className="highlights-date-text" aria-hidden="true" data-label={date.slice(5).replace('-', '.')}>{date.slice(5).replace('-', '.')}</span>
              {pending && navigation.date === date ? <Loader2 size={12} className="highlights-date-spinner" aria-hidden="true" /> : null}
            </button>
          </div>
        ))}
      </nav>
      <div className="highlights-date-footer">
        <div className="highlights-date-status" role="status" aria-live="polite">
          {searching ? <p>清除搜索后按日期浏览</p> : null}
          {filtering && !searching ? <p>切换分类中…</p> : null}
          {!dates.length && !failure && !searching ? <p>{loading || enabled === null ? '加载日期…' : '暂无日期'}</p> : null}
          {pending ? <span className="sr-only">{navigation.status === 'loading' ? '正在加载' : '正在定位'}{navigation.kind === 'latest' ? '最新内容' : navigation.date}…</span> : null}
          {failure ? <><p>{failure}</p><button type="button" disabled={blocked} onClick={() => {
            if (navigation?.kind === 'date' && navigation.date) onSelectDate(navigation.date, false)
            else if (navigation?.kind === 'latest') onBackToLatest(false)
            else void useEventsStore.getState().init()
          }}>重试</button></> : null}
        </div>
        {timelineStartDate || navigation?.kind === 'latest' ? <button type="button" className="highlights-date-latest" disabled={blocked || Boolean(pending && navigation.kind === 'latest')} onClick={(event) => onBackToLatest(event.detail === 0)}>回到最新</button> : null}
      </div>
    </div>
  )
}
