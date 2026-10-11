import { NotebookText } from 'lucide-react'
import { useChangelog } from '../../hooks/useChangelog'
import { Tooltip } from '../shared/Tooltip'

export function ChangelogButton() {
  const { hasUnread } = useChangelog()

  return (
    <Tooltip content="更新日志">
      <a
        href="#changelog"
        aria-label={hasUnread ? '更新日志，有未读更新' : '更新日志'}
        aria-current={window.location.hash === '#changelog' ? 'page' : undefined}
        className="relative inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-[4px] text-muted-foreground transition-colors hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background sm:h-9 sm:w-9"
        data-testid="topbar-changelog"
      >
        <NotebookText className="h-[19px] w-[19px]" strokeWidth={1.6} aria-hidden="true" />
        {hasUnread && (
          <span
            className="absolute right-[8px] top-[8px] h-[5px] w-[5px] rounded-full bg-[var(--brand)] ring-2 ring-background sm:right-[5px] sm:top-[5px]"
            aria-hidden="true"
            data-testid="changelog-unread-dot"
          />
        )}
      </a>
    </Tooltip>
  )
}
