import { useMemo, useState } from 'react'
import { X } from 'lucide-react'
import { useClusterDetailStore } from '../../store/clusterDetailStore'
import { useDailyDigestStore } from '../../store/dailyDigestStore'
import { useEventsStore } from '../../store/eventsStore'
import type { DailyDigestEntry } from '../../lib/types'

interface DailyDigestStripProps {
  date: string
}

function SnapshotFallback({ entry, onClose }: { entry: DailyDigestEntry; onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-background/80 px-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="daily-digest-fallback-title"
      onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}
    >
      <div className="w-full max-w-lg border border-border bg-background px-6 py-5 text-foreground shadow-lg sm:px-8 sm:py-7">
        <div className="flex items-start gap-4">
          <div className="min-w-0 flex-1">
            <p className="font-mono text-[12px] text-[var(--brand)]">每日要点快照</p>
            <h2 id="daily-digest-fallback-title" className="mt-3 font-event-title text-[20px] font-medium leading-snug">
              {entry.title}
            </h2>
            <p className="mt-2 font-mono text-[12px] text-muted-foreground">{entry.source_count} 个信源</p>
          </div>
          <button
            type="button"
            aria-label="关闭快照"
            onClick={onClose}
            className="flex h-8 w-8 shrink-0 items-center justify-center border border-border text-muted-foreground transition-colors hover:text-foreground"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
        {entry.links.length > 0 && (
          <div className="mt-5 border-t border-border pt-4">
            <p className="mb-2 font-mono text-[11px] text-muted-foreground">原文链接</p>
            <ul className="space-y-2">
              {entry.links.map((link) => (
                <li key={link.url}>
                  <a
                    href={link.url}
                    target="_blank"
                    rel="noreferrer"
                    className="font-event-title text-[15px] text-foreground underline decoration-border underline-offset-4 hover:decoration-foreground"
                  >
                    {link.label || link.url}
                  </a>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  )
}

export function DailyDigestStrip({ date }: DailyDigestStripProps) {
  const digest = useDailyDigestStore((state) => state.digestsByDate[date])
  const categories = useEventsStore((state) => state.filters.categories)
  const events = useEventsStore((state) => state.events)
  const openModal = useClusterDetailStore((state) => state.openModal)
  const closeModal = useClusterDetailStore((state) => state.closeModal)
  const [fallbackEntry, setFallbackEntry] = useState<DailyDigestEntry | null>(null)
  const entries = useMemo(
    () => (digest?.entries ?? [])
      .filter((entry) => categories.length === 0 || (entry.category != null && categories.includes(entry.category)))
      .sort((left, right) => left.rank - right.rank),
    [digest?.entries, categories],
  )

  if (!digest || entries.length === 0) return null

  const openEntry = async (entry: DailyDigestEntry) => {
    const preview = events.find((event) => event.id === entry.cluster_id)
    try {
      await openModal(entry.cluster_id, preview)
      const modal = useClusterDetailStore.getState()
      if (modal.modalClusterId !== entry.cluster_id || modal.modalState !== 'error') return
    } catch {
      // The immutable snapshot remains a usable reading path when detail loading fails.
    }
    closeModal()
    setFallbackEntry(entry)
  }

  return (
    <>
      <aside data-testid="daily-digest-strip" className="rounded-[4px] bg-[color-mix(in_srgb,var(--brand)_7%,var(--background))] px-5 py-4">
        <ol>
          {entries.map((entry, index) => (
            <li key={entry.cluster_id}>
              <button
                type="button"
                data-testid="daily-digest-entry"
                onClick={() => { void openEntry(entry) }}
                className="group grid w-full grid-cols-[28px_minmax(0,1fr)] items-start gap-2 py-2 text-left sm:grid-cols-[32px_minmax(0,1fr)] sm:gap-3"
              >
                <span className="font-mono text-[12px] leading-5 tabular-nums text-[var(--brand)]">
                  {String(categories.length ? index + 1 : entry.rank).padStart(2, '0')}
                </span>
                <span className="line-clamp-2 min-w-0 font-event-title text-[14px] font-medium leading-5 text-foreground decoration-border underline-offset-4 group-hover:underline sm:line-clamp-1 sm:text-[17px] sm:leading-6">
                  {entry.title}
                </span>
              </button>
            </li>
          ))}
        </ol>
      </aside>
      {fallbackEntry && <SnapshotFallback entry={fallbackEntry} onClose={() => setFallbackEntry(null)} />}
    </>
  )
}
