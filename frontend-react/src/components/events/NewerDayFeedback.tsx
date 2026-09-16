import { useEffect } from 'react'
import { Check, CircleAlert, Loader2 } from 'lucide-react'
import { useEventsStore } from '../../store/eventsStore'

/** Request feedback only: it neither reserves layout space nor controls scrolling. */
export function NewerDayFeedback({ onRetry }: { onRetry: () => void }) {
  const feedback = useEventsStore((state) => state.newerFeedback)
  const clearFeedback = useEventsStore((state) => state.clearNewerFeedback)

  useEffect(() => {
    if (feedback?.status !== 'success') return
    const timer = window.setTimeout(() => clearFeedback(feedback.requestId), 1500)
    return () => window.clearTimeout(timer)
  }, [clearFeedback, feedback])

  if (!feedback) return null
  const [, month, day] = feedback.targetDate.split('-')
  const dateLabel = `${Number(month)}月${Number(day)}日`

  return (
    <div className="highlights-newer-feedback" data-testid="highlights-newer-feedback">
      <div
        className="highlights-newer-status"
        data-testid="highlights-newer-status"
        data-state={feedback.status}
        role={feedback.status === 'error' ? 'alert' : 'status'}
        aria-live={feedback.status === 'error' ? 'assertive' : 'polite'}
        aria-atomic="true"
      >
        {feedback.status === 'loading' ? (
          <>
            <Loader2 size={16} className="shrink-0 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            <span>正在加载 {dateLabel}…</span>
          </>
        ) : feedback.status === 'success' ? (
          <>
            <Check size={16} className="shrink-0" aria-hidden="true" />
            <span>已加载 {dateLabel} · {feedback.count} 条，向上查看</span>
          </>
        ) : (
          <>
            <CircleAlert size={16} className="shrink-0" aria-hidden="true" />
            <span className="min-w-0 break-words">
              <span className="block font-medium">{dateLabel}加载失败</span>
              <span className="block text-foreground">{feedback.message}</span>
            </span>
            <button
              type="button"
              aria-label={`重试加载 ${dateLabel}`}
              className="min-h-11 min-w-11 shrink-0 rounded px-2 underline underline-offset-4 hover:bg-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--brand)]"
              onClick={onRetry}
            >重试</button>
          </>
        )}
      </div>
    </div>
  )
}
