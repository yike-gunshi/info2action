import { useEffect, useLayoutEffect, useRef } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { ArrowLeft, X } from 'lucide-react'
import { cn } from '../../lib/utils'
import { useBodyScrollLock } from '../../hooks/useBodyScrollLock'
import { useScrollbarReveal } from '../../hooks/useScrollbarReveal'
import { paperSurfaceStyle } from '../detail/detailShared'

const PANEL_CLASSES = 'modal-viewport-panel flex w-[calc(100vw-24px)] max-w-[720px] flex-col overflow-hidden rounded-[10px] border border-[var(--modal-border)] bg-[var(--modal-surface)] text-[var(--modal-text)] shadow-[var(--modal-shadow)] focus:outline-none'

export const readingModalFooterButtonClass = 'flex h-12 w-full items-center justify-center gap-1.5 text-[13px] font-medium text-[var(--modal-text-muted)] transition-colors hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-inset disabled:cursor-not-allowed disabled:opacity-60'

export function ReadingModalShell({
  ariaLabelledby,
  children,
  className,
  dataAttributes,
  escapeDisabled = false,
  isClosing,
  onClose,
  panelStyle = paperSurfaceStyle,
  panelTestId,
}: {
  ariaLabelledby: string
  children: ReactNode
  className?: string
  dataAttributes?: Record<string, string>
  escapeDisabled?: boolean
  isClosing: boolean
  onClose: () => void
  panelStyle?: CSSProperties
  panelTestId: string
}) {
  const panelRef = useRef<HTMLDivElement>(null)
  useBodyScrollLock(true, { compensateGutter: true })

  useEffect(() => {
    if (escapeDisabled) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [escapeDisabled, onClose])

  useEffect(() => {
    const panel = panelRef.current
    if (!panel) return
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    panel.focus({ preventScroll: true })

    const trapFocus = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return
      const elements = Array.from(panel.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'))
      if (elements.length === 0) {
        event.preventDefault()
        panel.focus()
        return
      }
      const first = elements[0]
      const last = elements[elements.length - 1]
      if (document.activeElement === panel) {
        event.preventDefault()
        const target = event.shiftKey ? last : first
        target.focus()
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    panel.addEventListener('keydown', trapFocus)
    return () => {
      panel.removeEventListener('keydown', trapFocus)
      previousFocus?.focus({ preventScroll: true })
    }
  }, [])

  return (
    <div
      data-testid="reading-modal-overlay"
      className={cn(
        'reading-modal-overlay modal-viewport-shell fixed inset-0 z-[900] flex bg-black/60',
        isClosing ? 'animate-backdrop-out' : 'animate-backdrop-in',
      )}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={ariaLabelledby}
        tabIndex={-1}
        data-testid={panelTestId}
        data-modal-theme="editorial"
        {...dataAttributes}
        className={cn(PANEL_CLASSES, isClosing ? 'animate-modal-out' : 'animate-modal-in', className)}
        style={panelStyle}
        onClick={(event) => event.stopPropagation()}
      >
        {children}
      </div>
    </div>
  )
}

export function ReadingModalHeader({
  canGoBack = false,
  closeLabel = '关闭',
  headerTestId,
  loading = false,
  meta,
  metaTestId,
  onBack,
  onClose,
  title,
  titleId,
  titleTestId,
}: {
  canGoBack?: boolean
  closeLabel?: string
  headerTestId: string
  loading?: boolean
  meta?: ReactNode
  metaTestId?: string
  onBack?: () => void
  onClose: () => void
  title: string
  titleId: string
  titleTestId?: string
}) {
  return (
    <header
      data-testid={headerTestId}
      className="shrink-0 bg-[var(--modal-surface)] px-4 py-5 sm:px-10"
      style={paperSurfaceStyle}
    >
      <div className="flex items-start gap-4">
        {canGoBack && onBack && (
          <button
            type="button"
            onClick={onBack}
            aria-label="返回上一条"
            title="返回"
            className="relative mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-[5px] text-[var(--modal-text-faint)] transition-colors before:absolute before:-inset-2 before:content-[''] hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-inset"
          >
            <ArrowLeft className="h-5 w-5" />
          </button>
        )}

        <div className="min-w-0 flex-1">
          {loading ? (
            <>
              <h2 id={titleId} className="sr-only">{title}</h2>
              <div className="space-y-2" aria-hidden="true">
                <div className="h-5 w-3/4 animate-skeleton rounded bg-[var(--modal-divider)]" />
                <div className="h-5 w-1/2 animate-skeleton rounded bg-[var(--modal-divider)]" />
              </div>
            </>
          ) : (
            <h2
              id={titleId}
              data-testid={titleTestId}
              className="reading-title line-clamp-2"
              title={title}
            >
              <span className="line-clamp-2">{title}</span>
            </h2>
          )}
          {loading ? (
            <div className="mt-2 h-4 w-2/5 animate-skeleton rounded bg-[var(--modal-hover)]" aria-hidden="true" />
          ) : meta ? (
            <div
              data-testid={metaTestId}
              className="reading-meta mt-2 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1"
            >
              {meta}
            </div>
          ) : null}
        </div>

        <div className="flex w-8 shrink-0 items-center justify-end" data-testid="detail-header-actions">
          <button
            type="button"
            onClick={onClose}
            aria-label={closeLabel}
            title={closeLabel}
            className="relative flex h-7 w-7 items-center justify-center rounded-[5px] text-[var(--modal-text-faint)] transition-colors before:absolute before:-inset-2 before:content-[''] hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-inset"
          >
            <X className="h-5 w-5" />
          </button>
        </div>
      </div>
    </header>
  )
}

export function ReadingModalBody({
  children,
  className,
  resetKey,
  testId,
}: {
  children: ReactNode
  className?: string
  resetKey?: string | number
  testId?: string
}) {
  const scrollRef = useRef<HTMLDivElement>(null)
  useScrollbarReveal(scrollRef)

  useLayoutEffect(() => {
    const element = scrollRef.current
    if (!element || resetKey === undefined) return
    element.setAttribute('data-scrollbar-programmatic', '')
    if (typeof element.scrollTo === 'function') element.scrollTo({ top: 0 })
    else element.scrollTop = 0
    const frame = requestAnimationFrame(() => element.removeAttribute('data-scrollbar-programmatic'))
    return () => cancelAnimationFrame(frame)
  }, [resetKey])

  return (
    <div
      ref={scrollRef}
      data-testid={testId}
      className={cn('reveal-scroll reading-modal-scroll flex-1 overflow-y-auto pb-7 pt-0 sm:pb-8', className)}
    >
      {children}
    </div>
  )
}

export function ReadingModalFooter({
  children,
  className,
  testId,
}: {
  children: ReactNode
  className?: string
  testId: string
}) {
  return (
    <div
      className={cn('modal-safe-footer grid flex-shrink-0 overflow-hidden border-t border-[var(--modal-border-soft)] bg-[var(--modal-surface)]', className)}
      data-testid={testId}
      style={paperSurfaceStyle}
    >
      {children}
    </div>
  )
}

export function ReadingModalLoading({
  headerTestId,
  onClose,
  testId,
  title,
  titleId,
}: {
  headerTestId: string
  onClose: () => void
  testId: string
  title: string
  titleId: string
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid={testId} aria-label="加载中">
      <ReadingModalHeader
        headerTestId={headerTestId}
        loading
        onClose={onClose}
        title={title}
        titleId={titleId}
      />
      <ReadingModalBody>
        <div className="space-y-5 py-5" aria-hidden="true">
          {[0, 1, 2].map((index) => (
            <div key={index} className="space-y-2.5">
              <div className="h-4 w-full animate-skeleton rounded bg-[var(--modal-hover)]" />
              <div className="h-4 w-11/12 animate-skeleton rounded bg-[var(--modal-hover)]" />
              <div className="h-4 w-4/5 animate-skeleton rounded bg-[var(--modal-hover)]" />
            </div>
          ))}
        </div>
      </ReadingModalBody>
    </div>
  )
}

export function ReadingModalError({
  headerTestId,
  message,
  onClose,
  onRetry,
  testId,
  title,
  titleId,
}: {
  headerTestId: string
  message: string
  onClose: () => void
  onRetry: () => void
  testId: string
  title: string
  titleId: string
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <ReadingModalHeader
        headerTestId={headerTestId}
        onClose={onClose}
        title={title}
        titleId={titleId}
      />
      <ReadingModalBody className="flex min-h-[220px] items-center justify-center">
        <div className="flex flex-col items-center gap-3 py-12 text-center" data-testid={testId}>
          <p className="text-[14px] text-[var(--modal-text-muted)]">{message}</p>
          <button
            type="button"
            onClick={onRetry}
            className="rounded-[4px] border border-[var(--modal-border)] bg-[var(--modal-surface)] px-4 py-2 text-[13px] font-medium text-[var(--modal-text)] transition-colors hover:border-[var(--brand-border)]"
          >
            重试
          </button>
        </div>
      </ReadingModalBody>
    </div>
  )
}
