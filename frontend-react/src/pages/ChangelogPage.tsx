import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ArrowLeft, ArrowUpRight, NotebookText } from 'lucide-react'
import { TopBar } from '../components/layout/TopBar'
import { useChangelog } from '../hooks/useChangelog'
import { useDetailStore } from '../store/detailStore'
import { useClusterDetailStore } from '../store/clusterDetailStore'
import type { ChangelogRelease } from '../content/changelog'
import '../styles/product-changelog.tokens.css'
import '../styles/product-changelog.css'

const KIND_LABELS = { feature: '新功能', improvement: '体验优化', fix: '问题修复' }
const displayDate = (release: ChangelogRelease) => new Date(release.publishedAt).toISOString().slice(0, 10).replace(/-/g, '.')
const sectionId = (release: ChangelogRelease) => `changelog-release-${release.id}`

function readingLine(page: HTMLElement) {
  const topbar = page.parentElement?.querySelector<HTMLElement>('[data-testid="topbar"]')
  const mobile = page.classList.contains('mob')
  const heading = mobile ? page.querySelector<HTMLElement>('.cl-heading-row') : null
  const styles = getComputedStyle(page)
  const gap = parseFloat(styles.getPropertyValue(mobile ? '--cl-mobile-head-pad' : '--cl-page-pad')) || 0
  return (topbar?.getBoundingClientRect().height ?? 0) + (heading?.getBoundingClientRect().height ?? 0) + gap
}

export function ChangelogPage() {
  const { releases, markSeen } = useChangelog()
  const latest = releases[0]
  const pageRef = useRef<HTMLElement>(null)
  const menuRef = useRef<HTMLDetailsElement>(null)
  const [activeId, setActiveId] = useState<string | undefined>(latest?.id)

  useLayoutEffect(() => {
    useDetailStore.getState().closeModal()
    useClusterDetailStore.getState().closeModal()
    window.scrollTo({ top: 0 })
  }, [])

  useEffect(() => {
    if (latest) markSeen()
  }, [latest, markSeen])

  useLayoutEffect(() => {
    const page = pageRef.current
    if (!page) return
    const topbar = page.parentElement?.querySelector<HTMLElement>('[data-testid="topbar"]')
    const rows = [...page.querySelectorAll<HTMLElement>('[data-release-row]')]
    let frame = 0
    const updateActive = () => {
      frame = 0
      let active = rows[0]
      const line = readingLine(page) + 2
      for (const row of rows) {
        if (row.getBoundingClientRect().top <= line) active = row
        else break
      }
      if (window.scrollY > 0 && window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2) {
        active = rows[rows.length - 1]
      }
      setActiveId(active?.dataset.releaseRow)
    }
    const schedule = () => { if (!frame) frame = requestAnimationFrame(updateActive) }
    const measure = () => {
      const styles = getComputedStyle(page)
      const width = page.clientWidth
      const mobile = width < (parseFloat(styles.getPropertyValue('--cl-mobile-below')) || 640)
      page.classList.toggle('mob', mobile)
      page.classList.toggle('cl-narrow', !mobile && width < (parseFloat(styles.getPropertyValue('--cl-narrow-below')) || 900))
      page.style.setProperty('--cl-topbar-height', `${topbar?.getBoundingClientRect().height ?? 0}px`)
      if (!mobile && menuRef.current) menuRef.current.open = false
      schedule()
    }
    const observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(measure)
    observer?.observe(page)
    if (topbar) observer?.observe(topbar)
    measure()
    window.addEventListener('resize', measure)
    window.addEventListener('scroll', schedule, { passive: true })
    const closeOutside = (event: PointerEvent) => {
      const menu = menuRef.current
      if (menu?.open && event.target instanceof Node && !menu.contains(event.target)) menu.open = false
    }
    document.addEventListener('pointerdown', closeOutside)
    return () => {
      observer?.disconnect()
      cancelAnimationFrame(frame)
      window.removeEventListener('resize', measure)
      window.removeEventListener('scroll', schedule)
      document.removeEventListener('pointerdown', closeOutside)
    }
  }, [releases])

  const selectRelease = (release: ChangelogRelease, keyboard: boolean) => {
    const page = pageRef.current
    const article = document.getElementById(sectionId(release))
    const row = article?.closest<HTMLElement>('[data-release-row]')
    if (!page || !row) return
    if (menuRef.current) menuRef.current.open = false
    if (keyboard) article?.querySelector<HTMLElement>('h2')?.focus({ preventScroll: true })
    window.scrollTo({
      top: Math.max(0, row.getBoundingClientRect().top + window.scrollY - readingLine(page)),
      behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
    })
  }

  const dateButton = (release: ChangelogRelease, inMenu = false) => (
    <button
      type="button"
      className="cl-date"
      aria-label={displayDate(release)}
      aria-current={activeId === release.id ? 'date' : undefined}
      aria-controls={sectionId(release)}
      title={release.title}
      onClick={(event) => selectRelease(release, event.detail === 0)}
    >
      <span className="cl-dot" aria-hidden="true" />
      {inMenu ? <span>{displayDate(release)}</span> : (
        <time className="cl-date-label" dateTime={release.publishedAt} aria-hidden="true">{displayDate(release)}</time>
      )}
    </button>
  )

  return (
    <div className="min-h-screen bg-background text-foreground">
      <TopBar activeL1={null} showSearch={false} />
      <main ref={pageRef} className="cl-page cl-no-glow" data-state={releases.length ? 'done' : 'empty'} data-testid="changelog-page">
        <div className="cl-document">
          <div className="cl-heading-row">
            <div className="cl-heading">
              <h1 className="cl-page-title">更新日志</h1>
              {releases.length > 0 && (
                <details ref={menuRef} className="cl-menu" onKeyDown={(event) => {
                  if (event.key === 'Escape') {
                    event.preventDefault()
                    event.currentTarget.open = false
                    event.currentTarget.querySelector('summary')?.focus()
                  }
                }}>
                  <summary className="cl-menu-trigger" aria-label="选择更新日期">日期</summary>
                  <nav className="cl-date-menu" aria-label="更新日志日期导航">
                    {releases.map((release) => <div key={release.id}>{dateButton(release, true)}</div>)}
                  </nav>
                </details>
              )}
            </div>
          </div>

          {releases.length ? (
            <div className="cl-list">
              {releases.map((release) => (
                <div key={release.id} className="cl-row" data-release-row={release.id}>
                  <div className="cl-date-cell">{dateButton(release)}</div>
                  <article className="cl-article" id={sectionId(release)} aria-labelledby={`${sectionId(release)}-title`}>
                    <h2 className="cl-release-title" id={`${sectionId(release)}-title`} tabIndex={-1}>{release.title}</h2>
                    <ul className="cl-changes">
                      {release.changes.map((change, index) => (
                        <li key={index} className="cl-change">
                          <div className="cl-change-title">
                            <span className="cl-kind">{KIND_LABELS[change.kind]}</span>
                            <h3 className="cl-entry-title">{change.title}</h3>
                          </div>
                          <p className="cl-copy">{change.description}</p>
                          {change.media?.map((media) => (
                            <figure className="cl-media" key={media.src}>
                              <img src={media.src} alt={media.alt} width={media.width} height={media.height} loading="lazy" decoding="async" referrerPolicy="no-referrer" />
                            </figure>
                          ))}
                          {change.action && /^#[^\s#]+$/.test(change.action.href) && (
                            <a href={change.action.href} className="cl-action">
                              {change.action.label}<ArrowUpRight className="h-4 w-4" aria-hidden="true" />
                            </a>
                          )}
                        </li>
                      ))}
                    </ul>
                  </article>
                </div>
              ))}
            </div>
          ) : (
            <div className="cl-empty">
              <NotebookText className="mb-5 h-7 w-7 text-muted-foreground" strokeWidth={1.4} aria-hidden="true" />
              <h2 className="cl-release-title">还没有更新日志</h2>
              <p className="cl-copy">发布后的更新会显示在这里。</p>
              <a href="#v=highlights" className="cl-action"><ArrowLeft className="h-4 w-4" aria-hidden="true" />返回精选</a>
            </div>
          )}
        </div>
      </main>
    </div>
  )
}
