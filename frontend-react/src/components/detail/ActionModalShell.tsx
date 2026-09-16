import { useCallback, useEffect, useState } from 'react'
import { ArrowLeft, Ban, CalendarDays, RotateCcw, X } from 'lucide-react'
import { toast } from 'sonner'
import { useDetailStore } from '../../store/detailStore'
import { useActionStore } from '../../store/actionStore'
import { fetchAction, setActionStatus } from '../../lib/api'
import { cn } from '../../lib/utils'
import type { ActionItem, ActionStatus } from '../../lib/types'
import { ActionDetailContent } from './ActionDetailContent'
import { paperSurfaceStyle } from './detailShared'

function hasCompleteActionDetailPayload(action: ActionItem | null): boolean {
  if (!action) return false
  const maybeWithSteps = action as ActionItem & { steps?: unknown }
  return (
    Array.isArray(maybeWithSteps.steps) &&
    Array.isArray(action.source_items) &&
    typeof action.source_item_count === 'number'
  )
}

export function ActionModalShell({
  actionId,
  canGoBack,
  goBack,
  handleClose,
}: {
  actionId: string
  canGoBack: boolean
  goBack: () => void
  handleClose: () => void
}) {
  const updateActionInStore = useActionStore((s) => s.updateAction)
  const detailAction = useDetailStore((s) => (s.actionDetail?.id === actionId ? s.actionDetail : null))
  const setActionDetail = useDetailStore((s) => s.setActionDetail)
  const [fetchedAction, setFetchedAction] = useState<ActionItem | null>(null)
  const [fetchError, setFetchError] = useState(false)

  const action = fetchedAction ?? (hasCompleteActionDetailPayload(detailAction) ? detailAction : null)

  useEffect(() => {
    let cancelled = false
    setFetchedAction(null)
    setFetchError(false)
    if (hasCompleteActionDetailPayload(detailAction)) {
      return () => {
        cancelled = true
      }
    }
    fetchAction(String(actionId))
      .then((data) => {
        if (!cancelled && data) {
          setFetchedAction(data)
          setActionDetail(data)
          updateActionInStore(data.id, data)
        }
        if (!cancelled && !data) setFetchError(true)
      })
      .catch(() => {
        if (!cancelled) setFetchError(true)
      })
    return () => {
      cancelled = true
    }
  }, [actionId, detailAction, setActionDetail, updateActionInStore])

  const patchAction = useCallback((patch: Partial<ActionItem>) => {
    setFetchedAction((prev) => ({ ...(prev ?? action), ...patch }) as ActionItem)
    if (action) setActionDetail({ ...action, ...patch } as ActionItem)
    updateActionInStore(actionId, patch)
  }, [action, actionId, setActionDetail, updateActionInStore])

  if (!action && fetchError) {
    return (
      <div className="flex min-h-[220px] flex-1 items-center justify-center px-6 py-12 text-sm text-destructive">
        行动点未找到或加载失败
      </div>
    )
  }

  if (!action) return <ActionModalLoading />

  return (
    <>
      <ActionModalHeader
        action={action}
        canGoBack={canGoBack}
        goBack={goBack}
        handleClose={handleClose}
      />
      <ActionStatusStepper action={action} onPatchAction={patchAction} />
      <div className="flex-1 overflow-y-auto px-6 pb-7 pt-0 sm:px-10 sm:pb-8">
        <ActionDetailContent action={action} onPatchAction={patchAction} />
      </div>
    </>
  )
}

function ActionModalHeader({
  action,
  canGoBack,
  goBack,
  handleClose,
}: {
  action: ActionItem
  canGoBack: boolean
  goBack: () => void
  handleClose: () => void
}) {
  return (
    <header
      data-testid="detail-modal-header"
      className="shrink-0 border-b border-[var(--modal-divider)] bg-[var(--modal-surface)] px-6 py-5 sm:px-10"
      style={paperSurfaceStyle}
    >
      <div className="flex items-start gap-4">
        {canGoBack && (
          <button
            type="button"
            onClick={goBack}
            aria-label="返回上一条"
            title="返回"
            className="mt-0.5 relative flex h-7 w-7 shrink-0 items-center justify-center rounded-[5px] text-[var(--modal-text-faint)] before:absolute before:-inset-2 before:content-[''] transition-colors hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)]"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
          </button>
        )}

        <div className="min-w-0 flex-1">
          <h2
            id="detail-modal-title"
            data-testid="action-modal-title"
            className="reading-title line-clamp-2"
            title={action.title}
          >
            {action.title}
          </h2>
          <div
            data-testid="action-modal-meta"
            className="reading-meta mt-3 flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1"
          >
            <span className="inline-flex min-w-0 items-center gap-1.5 text-[var(--modal-text-muted)]">
              <CalendarDays className="h-3.5 w-3.5 shrink-0" />
              <time className="font-mono tabular-nums" dateTime={action.created_at}>
                {formatActionAbsoluteTime(action.created_at)}
              </time>
            </span>
          </div>
        </div>

        <div className="flex w-8 shrink-0 items-center justify-end" data-testid="detail-header-actions">
          <button
            type="button"
            onClick={handleClose}
            aria-label="关闭"
            title="关闭"
            className="relative flex h-8 w-8 items-center justify-center rounded-[5px] before:absolute before:-inset-1.5 before:content-[''] border border-[var(--modal-border-soft)] bg-[var(--modal-surface-soft)] text-[var(--modal-text-faint)] transition-colors hover:border-[var(--brand-border)] hover:bg-[var(--modal-hover-soft)] hover:text-[var(--modal-text)]"
          >
            <X className="h-5 w-5" />
          </button>
        </div>
      </div>
    </header>
  )
}

// v2 §13.4: 行动详情顶部独立状态区。三段式 stepper 可点切换 + 忽略/恢复。
const STATUS_STEPS: { label: string; status: 'pending' | 'confirmed' | 'done' }[] = [
  { label: '待处理', status: 'pending' },
  { label: '执行中', status: 'confirmed' },
  { label: '已完成', status: 'done' },
]

function statusStepIndex(status: ActionStatus): number {
  if (status === 'pending') return 0
  if (status === 'confirmed' || status === 'executing' || status === 'dispatched') return 1
  if (status === 'done') return 2
  return -1
}

function ActionStatusStepper({
  action,
  onPatchAction,
}: {
  action: ActionItem
  onPatchAction: (patch: Partial<ActionItem>) => void
}) {
  const updateActionInStore = useActionStore((s) => s.updateAction)
  const [busy, setBusy] = useState(false)
  const cur = statusStepIndex(action.status)
  const isDismissed = action.status === 'dismissed' || action.status === 'ignored' || action.status === 'failed'

  const apply = async (status: 'pending' | 'confirmed' | 'done' | 'dismissed', okMsg: string) => {
    if (busy) return
    setBusy(true)
    try {
      await setActionStatus(action.id, status)
      onPatchAction({ status })
      updateActionInStore(action.id, { status })
      toast.success(okMsg)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '操作失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      data-testid="action-status-stepper"
      className="flex flex-shrink-0 flex-wrap items-center gap-3 border-b border-[var(--modal-divider)] px-6 py-3 sm:px-10"
    >
      <div className="inline-flex items-center rounded-[9px] border border-[var(--modal-border-soft)] bg-[var(--modal-surface)] p-1">
        {STATUS_STEPS.map((st, i) => (
          <span key={st.status} className="inline-flex items-center">
            {i > 0 && <span className="px-0.5 text-[12px] text-[var(--modal-text-faint)]" aria-hidden="true">›</span>}
            <button
              type="button"
              data-testid={`status-step-${st.status}`}
              aria-pressed={i === cur}
              disabled={busy}
              onClick={() => apply(st.status, `已置为「${st.label}」`)}
              className={cn(
                'rounded-[6px] px-2.5 py-1 text-[12px] font-semibold transition-colors disabled:opacity-60',
                i === cur
                  ? 'bg-[var(--brand)] text-[var(--brand-foreground)]'
                  : i < cur
                    ? 'text-[var(--brand)] hover:bg-[var(--modal-hover-soft)]'
                    : 'text-[var(--modal-text-muted)] hover:text-[var(--modal-text)]',
              )}
            >
              {st.label}
            </button>
          </span>
        ))}
      </div>
      {isDismissed ? (
        <button
          type="button"
          data-testid="status-restore"
          disabled={busy}
          onClick={() => apply('pending', '已恢复待处理')}
          className="inline-flex items-center gap-1.5 reading-caption text-[var(--modal-text-muted)] hover:text-[var(--modal-text)]"
        >
          <RotateCcw className="h-3.5 w-3.5" /> 恢复待处理
        </button>
      ) : (
        <button
          type="button"
          data-testid="status-dismiss"
          disabled={busy}
          onClick={() => apply('dismissed', '已忽略')}
          className="inline-flex items-center gap-1.5 reading-caption text-[var(--modal-text-faint)] hover:text-[var(--modal-text)]"
        >
          <Ban className="h-3.5 w-3.5" /> 忽略
        </button>
      )}
    </div>
  )
}

function ActionModalLoading() {
  return (
    <div className="flex flex-1 flex-col">
      <div className="shrink-0 border-b border-[var(--modal-divider)] px-6 py-5 sm:px-10">
        <div className="h-6 w-3/4 animate-skeleton rounded bg-[var(--modal-divider)]" />
        <div className="mt-3 h-4 w-1/2 animate-skeleton rounded bg-[var(--modal-hover)]" />
      </div>
      <div className="flex-1 space-y-4 px-6 py-7 sm:px-10">
        <div className="h-4 w-20 animate-skeleton rounded bg-[var(--modal-hover)]" />
        <div className="h-4 w-full animate-skeleton rounded bg-[var(--modal-hover)]" />
        <div className="h-4 w-5/6 animate-skeleton rounded bg-[var(--modal-hover)]" />
        <div className="h-16 w-full animate-skeleton rounded bg-[var(--modal-hover)]" />
      </div>
    </div>
  )
}

function formatActionAbsoluteTime(value?: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
