import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { CalendarDays, ChevronDown, MoreHorizontal } from 'lucide-react'
import { toast } from 'sonner'
import { cn, actionTypeName } from '../../lib/utils'
import { useActionStore } from '../../store/actionStore'
import { useDetailStore } from '../../store/detailStore'
import { useAuthStore } from '../../store/authStore'
import { fetchActionsBoard, markActionDone, dismissAction, dispatchAction, updateActionPriority, updateAction, type ActionsBoardResponse } from '../../lib/api'
import type { ActionBoardDirection, ActionItem, ActionPriority, ActionStatus } from '../../lib/types'

type LaneKey = 'pending' | 'in_progress' | 'done'
type DateFilter = 'all' | 'today' | 'week'
type LaneLoadMeta = { count: number; hasMore: boolean; nextOffset: number | null }
type BoardSnapshot = {
  actions: ActionItem[]
  counts: Record<string, number>
  directions: Array<{ slug: string; label: string; count: number }>
  laneMeta: Record<LaneKey, LaneLoadMeta>
}

const ACTION_LANES: Array<{ key: LaneKey; label: string; emptyLabel: string }> = [
  { key: 'pending', label: '待处理', emptyLabel: '暂无待处理行动' },
  { key: 'in_progress', label: '执行中', emptyLabel: '暂无执行中行动' },
  { key: 'done', label: '已完成', emptyLabel: '暂无已完成行动' },
]

const ACTION_STATUS_LABELS: Record<ActionStatus, string> = {
  pending: '待处理',
  confirmed: '执行中',
  executing: '执行中',
  dispatched: '执行中',
  done: '已完成',
  failed: '失败',
  dismissed: '已忽略',
  ignored: '已忽略',
}

const STATUS_COLORS: Record<ActionStatus, string> = {
  pending: 'bg-[var(--brand-soft)] text-[var(--brand)]',
  confirmed: 'bg-[var(--brand-soft)] text-[var(--brand)]',
  executing: 'bg-[var(--brand-soft)] text-[var(--brand)]',
  dispatched: 'bg-[var(--brand-soft)] text-[var(--brand)]',
  done: 'bg-emerald-bg text-emerald',
  failed: 'bg-red-50 text-destructive dark:bg-red-950',
  dismissed: 'bg-warm-200 text-warm-500',
  ignored: 'bg-warm-200 text-warm-500',
}

const DATE_LABELS: Record<DateFilter, string> = {
  all: '全部',
  today: '今天',
  week: '本周',
}

const PRIORITY_FILTERS: ActionPriority[] = ['P0', 'P1', 'P2']
const CARDS_PER_PAGE = 20

const FALLBACK_DIRECTION_LABELS: Record<string, string> = {
  implementation: '实施',
  implement: '实施',
  investing: '投资',
  investment: '投资',
  research: '投资',
  content: '内容',
}

export function ActionsView() {
  const actions = useActionStore((s) => s.actions)
  const counts = useActionStore((s) => s.counts)
  const directions = useActionStore((s) => s.directions)
  const setActionsResponse = useActionStore((s) => s.setActionsResponse)
  const setIsLoading = useActionStore((s) => s.setIsLoading)
  const isLoading = useActionStore((s) => s.isLoading)
  const focusedActionId = useActionStore((s) => s.focusedActionId)
  const [dateFilter, setDateFilter] = useState<DateFilter>('all')
  const [priorityFilter, setPriorityFilter] = useState<ActionPriority | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [laneMeta, setLaneMeta] = useState<Record<LaneKey, LaneLoadMeta>>(emptyLaneMeta())
  const [loadingMoreLane, setLoadingMoreLane] = useState<LaneKey | null>(null)
  const [boardRevision, setBoardRevision] = useState(0)
  const boardCacheRef = useRef<Record<string, BoardSnapshot>>({})
  const requestSeqRef = useRef(0)

  const boardQuery = useMemo(() => ({
    date_filter: dateFilter === 'all' ? undefined : dateFilter,
    priority: priorityFilter ?? undefined,
    limit_per_direction: CARDS_PER_PAGE,
  }), [dateFilter, priorityFilter])
  const boardQueryKey = useMemo(() => actionBoardQueryKey(boardQuery), [boardQuery])

  const applyBoardSnapshot = useCallback((snapshot: BoardSnapshot) => {
    setActionsResponse({
      actions: snapshot.actions,
      counts: snapshot.counts,
      directions: snapshot.directions,
    })
    setLaneMeta(snapshot.laneMeta)
  }, [setActionsResponse])

  useEffect(() => {
    const requestSeq = requestSeqRef.current + 1
    requestSeqRef.current = requestSeq
    const cached = boardCacheRef.current[boardQueryKey]
    setIsLoading(true)
    setLoadError(null)
    if (cached) {
      applyBoardSnapshot(cached)
    } else {
      setActionsResponse({ actions: [], counts: {}, directions: [] })
      setLaneMeta(emptyLaneMeta())
    }
    fetchActionsBoard(boardQuery)
      .then((resp) => {
        if (requestSeq !== requestSeqRef.current) return
        const snapshot = snapshotFromBoardResponse(resp)
        boardCacheRef.current[boardQueryKey] = snapshot
        applyBoardSnapshot(snapshot)
      })
      .catch((err) => {
        if (requestSeq === requestSeqRef.current) {
          setLoadError(err instanceof Error ? err.message : '行动数据加载失败')
        }
      })
      .finally(() => {
        if (requestSeq === requestSeqRef.current) setIsLoading(false)
      })
  }, [applyBoardSnapshot, boardQuery, boardQueryKey, boardRevision, setActionsResponse, setIsLoading])

  const visibleActions = useMemo(() => {
    return actions.filter((action) => (
      actionLaneKey(action.status) != null &&
      isInsideDateFilter(action, dateFilter) &&
      matchesPriorityFilter(action, priorityFilter)
    ))
  }, [actions, dateFilter, priorityFilter])

  const groupedByLane = useMemo(() => {
    const groups = laneGroups()
    for (const action of visibleActions) {
      const lane = actionLaneKey(action.status)
      if (!lane) continue
      groups[lane].push(action)
    }
    for (const lane of ACTION_LANES) {
      groups[lane.key].sort(sortActionsByCreatedDesc)
    }
    return groups
  }, [visibleActions])

  const handleLoadMoreLane = useCallback(async (lane: LaneKey): Promise<boolean> => {
    const meta = laneMeta[lane]
    if (!meta?.hasMore || loadingMoreLane) return false
    const requestSeq = requestSeqRef.current
    setLoadingMoreLane(lane)
    setLoadError(null)
    try {
      const loadedInLane = visibleActions.filter((action) => actionLaneKey(action.status) === lane).length
      const resp = await fetchActionsBoard({
        ...boardQuery,
        status: lane,
        offset: meta.nextOffset ?? loadedInLane,
        limit_per_direction: CARDS_PER_PAGE,
      })
      if (requestSeq !== requestSeqRef.current) return false
      const incoming = flattenActionsBoard(resp)
      const seen = new Set(actions.map((action) => String(action.id)))
      const appendedActions = incoming.filter((action) => !seen.has(String(action.id)))
      const nextActions = [...actions, ...appendedActions]
      const nextDirections = mergeDirections(directions, normalizeBoardDirections(resp.directions))
      const nextLaneMeta = { ...laneMeta, ...buildLaneMeta(resp.directions) }
      setActionsResponse({
        actions: nextActions,
        counts,
        directions: nextDirections,
      })
      setLaneMeta(nextLaneMeta)
      boardCacheRef.current[boardQueryKey] = {
        actions: nextActions,
        counts,
        directions: nextDirections,
        laneMeta: nextLaneMeta,
      }
      return appendedActions.length > 0
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '行动数据加载失败')
      return false
    } finally {
      setLoadingMoreLane(null)
    }
  }, [actions, boardQuery, boardQueryKey, counts, directions, laneMeta, loadingMoreLane, setActionsResponse, visibleActions])

  useEffect(() => {
    if (!focusedActionId || isLoading || actions.length === 0) return
    const target = actions.find((a) => String(a.id) === String(focusedActionId))
    if (!target) return
    const visible = visibleActions.some((a) => String(a.id) === String(focusedActionId))
    if (!visible) {
      setDateFilter('all')
      setPriorityFilter(null)
      return
    }
    window.setTimeout(() => {
      const el = document.getElementById(`action-card-${focusedActionId}`)
      el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 80)
  }, [focusedActionId, actions, visibleActions, isLoading])

  const handlePriorityToggle = (priority: ActionPriority) => {
    setPriorityFilter((current) => (current === priority ? null : priority))
  }

  const handleActionMutation = useCallback(() => {
    boardCacheRef.current = {}
    setBoardRevision((current) => current + 1)
  }, [])

  return (
    <div data-testid="actions-view-shell" className="mx-auto w-full max-w-[1200px] px-4 pb-10">
      <ActionFilterSubbar
        dateFilter={dateFilter}
        priorityFilter={priorityFilter}
        onDateChange={setDateFilter}
        onPriorityToggle={handlePriorityToggle}
      />

      {loadError && (
        <div className="mb-5 rounded-[4px] border border-[var(--brand-border)] bg-[var(--brand-soft)] px-4 py-3 text-sm text-[var(--brand)]">
          行动数据暂时不可用：{loadError}
        </div>
      )}

      {isLoading && actions.length === 0 ? (
        <ActionsBoardSkeleton />
      ) : (
        <div data-testid="actions-lane-grid" className="grid grid-cols-1 gap-4 lg:grid-cols-3 lg:items-start xl:gap-5">
          {ACTION_LANES.map((lane) => {
            const items = groupedByLane[lane.key]
            const totalCount = Math.max(laneMeta[lane.key]?.count ?? 0, items.length)
            return (
              <ActionLane
                key={lane.key}
                lane={lane}
                items={items}
                totalCount={totalCount}
                hasMoreFromServer={laneMeta[lane.key]?.hasMore ?? false}
                isLoadingMore={loadingMoreLane === lane.key}
                onLoadMore={() => handleLoadMoreLane(lane.key)}
                onActionMutation={handleActionMutation}
              />
            )
          })}
        </div>
      )}
    </div>
  )
}

function snapshotFromBoardResponse(resp: ActionsBoardResponse): BoardSnapshot {
  return {
    actions: flattenActionsBoard(resp),
    counts: resp.counts,
    directions: normalizeBoardDirections(resp.directions),
    laneMeta: { ...emptyLaneMeta(), ...buildLaneMeta(resp.directions) },
  }
}

function actionBoardQueryKey(query: {
  date_filter?: DateFilter
  priority?: ActionPriority
  limit_per_direction?: number
}): string {
  return [
    `date:${query.date_filter ?? 'all'}`,
    `priority:${query.priority ?? 'all'}`,
    `limit:${query.limit_per_direction ?? CARDS_PER_PAGE}`,
  ].join('|')
}

function ActionFilterSubbar({
  dateFilter,
  priorityFilter,
  onDateChange,
  onPriorityToggle,
}: {
  dateFilter: DateFilter
  priorityFilter: ActionPriority | null
  onDateChange: (filter: DateFilter) => void
  onPriorityToggle: (priority: ActionPriority) => void
}) {
  const tabClassName = (selected: boolean) => cn(
    'relative flex h-10 shrink-0 items-center border-b-2 px-0.5 font-event-title text-[16px] font-medium tracking-normal transition-colors',
    'focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background',
    selected
      ? 'border-[var(--brand)] text-[var(--brand)]'
      : 'border-transparent text-muted-foreground hover:text-foreground',
  )

  return (
    <nav
      aria-label="行动二级筛选"
      className="sticky top-[84px] z-20 -mx-4 mb-6 h-10 overflow-x-auto bg-background px-4 scrollbar-hide max-sm:top-[84px] sm:top-[52px]"
      data-testid="actions-filter-row"
    >
      <div className="mx-auto flex h-10 w-full max-w-[1168px] items-center justify-center border-b border-border/70 sm:px-1" data-testid="actions-l2-tabs">
        <div className="flex h-10 w-full min-w-max items-center justify-center gap-6 sm:gap-8">
          {(Object.keys(DATE_LABELS) as DateFilter[]).map((date) => (
            <button
              key={date}
              type="button"
              role="tab"
              aria-selected={dateFilter === date}
              onClick={() => onDateChange(date)}
              className={tabClassName(dateFilter === date)}
              data-testid={`actions-date-tab-${date}`}
            >
              {DATE_LABELS[date]}
            </button>
          ))}
          <span className="select-none font-event-title text-[16px] font-medium text-muted-foreground/45" aria-hidden="true" data-testid="actions-l2-divider">
            |
          </span>
          {PRIORITY_FILTERS.map((priority) => (
            <button
              key={priority}
              type="button"
              aria-pressed={priorityFilter === priority}
              onClick={() => onPriorityToggle(priority)}
              className={tabClassName(priorityFilter === priority)}
              data-testid={`actions-priority-tab-${priority}`}
            >
              {priority}
            </button>
          ))}
        </div>
      </div>
    </nav>
  )
}

function flattenActionsBoard(resp: ActionsBoardResponse): ActionItem[] {
  return (resp.directions || []).flatMap((direction) =>
    (direction.items || []).map((action) => ({
      ...action,
      steps: typeof action.steps === 'string' ? parseSteps(action.steps as unknown as string) : action.steps,
    })),
  )
}

function normalizeBoardDirections(directions: ActionBoardDirection[]): Array<{ slug: string; label: string; count: number }> {
  return (directions || []).map((direction) => ({
    slug: direction.slug,
    label: direction.label,
    count: direction.count,
  }))
}

function buildLaneMeta(directions: ActionBoardDirection[]): Partial<Record<LaneKey, LaneLoadMeta>> {
  const meta: Partial<Record<LaneKey, LaneLoadMeta>> = {}
  for (const direction of directions || []) {
    if (!isLaneKey(direction.slug)) continue
    meta[direction.slug] = {
      count: direction.count,
      hasMore: Boolean((direction as ActionBoardDirection & { has_more?: boolean }).has_more),
      nextOffset: typeof (direction as ActionBoardDirection & { next_offset?: number | null }).next_offset === 'number'
        ? (direction as ActionBoardDirection & { next_offset?: number }).next_offset ?? null
        : null,
    }
  }
  return meta
}

function mergeDirections(
  current: Array<{ slug: string; label: string; count: number }>,
  incoming: Array<{ slug: string; label: string; count: number }>,
): Array<{ slug: string; label: string; count: number }> {
  const bySlug = new Map(current.map((direction) => [direction.slug, direction]))
  for (const direction of incoming) {
    bySlug.set(direction.slug, direction)
  }
  return Array.from(bySlug.values())
}

function emptyLaneMeta(): Record<LaneKey, LaneLoadMeta> {
  return {
    pending: { count: 0, hasMore: false, nextOffset: null },
    in_progress: { count: 0, hasMore: false, nextOffset: null },
    done: { count: 0, hasMore: false, nextOffset: null },
  }
}

function laneGroups(): Record<LaneKey, ActionItem[]> {
  return {
    pending: [],
    in_progress: [],
    done: [],
  }
}

function ActionsBoardSkeleton() {
  return (
    <div data-testid="actions-board-skeleton" className="grid grid-cols-1 gap-4 lg:grid-cols-3 lg:items-start xl:gap-5">
      {ACTION_LANES.map((lane) => (
        <section key={lane.key} data-testid="action-lane-skeleton" className="min-w-0 rounded-[4px] border border-border bg-transparent p-3">
          <div className="mb-3 flex items-baseline gap-2 px-1">
            <h3 className="font-event-title text-[22px] font-bold leading-none text-foreground">{lane.label}</h3>
            <span className="h-4 w-8 animate-pulse rounded-[3px] bg-[#EFE8DE]" />
          </div>
          <div className="space-y-3">
            {Array.from({ length: 3 }).map((_, index) => (
              <div key={index} className="rounded-[4px] border border-border bg-card p-4">
                <div className="mb-3 flex items-start justify-between gap-3">
                  <span className="h-5 w-4/5 animate-pulse rounded-[3px] bg-[#E9E1D7]" />
                  <span className="h-7 w-7 shrink-0 animate-pulse rounded-[4px] bg-[#EFE8DE]" />
                </div>
                <div className="space-y-2">
                  <span className="block h-3.5 w-full animate-pulse rounded-[3px] bg-[#EFE8DE]" />
                  <span className="block h-3.5 w-3/4 animate-pulse rounded-[3px] bg-[#EFE8DE]" />
                  <span className="block h-3.5 w-5/6 animate-pulse rounded-[3px] bg-[#EFE8DE]" />
                </div>
                <div className="mt-4 flex items-center gap-2">
                  <span className="h-7 w-16 animate-pulse rounded-[4px] bg-[var(--brand-soft)]" />
                  <span className="h-7 w-20 animate-pulse rounded-[4px] bg-[#F3EFE8]" />
                </div>
              </div>
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}

function ActionLane({
  lane,
  items,
  totalCount,
  hasMoreFromServer,
  isLoadingMore,
  onLoadMore,
  onActionMutation,
}: {
  lane: { key: LaneKey; label: string; emptyLabel: string }
  items: ActionItem[]
  totalCount: number
  hasMoreFromServer: boolean
  isLoadingMore: boolean
  onLoadMore: () => Promise<boolean>
  onActionMutation: () => void
}) {
  const remainingCount = Math.max(0, totalCount - items.length)

  return (
    <section data-testid="action-lane" data-lane={lane.key} className="min-w-0 rounded-[4px] border border-border bg-transparent p-3">
      <div className="mb-3 flex items-baseline gap-2 px-1">
        <h3 className="font-event-title text-[22px] font-bold leading-none text-foreground">{lane.label}</h3>
        <span className="font-body-cjk text-[14px] font-normal text-muted-foreground">{totalCount}</span>
      </div>

      {items.length > 0 ? (
        <div className="space-y-3">
          {items.map((action) => (
            <ActionCard key={action.id} action={action} onActionMutation={onActionMutation} />
          ))}
        </div>
      ) : (
        <div data-testid="action-lane-empty" className="rounded-[4px] border border-dashed border-border/80 bg-background/40 px-3 py-8 text-center">
          <p className="text-sm text-muted-foreground">{lane.emptyLabel}</p>
        </div>
      )}

      {hasMoreFromServer && (
        <div className="mt-4 flex justify-center">
          <button
            onClick={() => void onLoadMore()}
            disabled={isLoadingMore}
            className="mx-auto flex cursor-pointer items-center gap-1.5 rounded-full border border-border bg-card px-5 py-2 text-sm font-medium text-foreground shadow-subtle transition-all hover:border-warm-400 hover:shadow-medium disabled:cursor-not-allowed disabled:opacity-60"
          >
            {isLoadingMore ? '加载中' : '展开更多'}
            {remainingCount > 0 && (
              <span className="text-xs text-muted-foreground">还有 {remainingCount} 条</span>
            )}
            <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
          </button>
        </div>
      )}
    </section>
  )
}

function ActionCard({ action, onActionMutation }: { action: ActionItem; onActionMutation: () => void }) {
  const openAction = useDetailStore((s) => s.openAction)
  const setActionDetail = useDetailStore((s) => s.setActionDetail)
  const updateActionInStore = useActionStore((s) => s.updateAction)
  const focusedActionId = useActionStore((s) => s.focusedActionId)
  const canDispatch = useAuthStore((s) => s.user?.has_discord_token ?? false)
  const [showMenu, setShowMenu] = useState(false)
  const isFocused = String(action.id) === String(focusedActionId || '')
  const actionPointItems = getActionPointItems(action)
  const direction = normalizeDirection((action as ActionItem & { direction?: string }).direction)
  const directionLabel = FALLBACK_DIRECTION_LABELS[direction] ?? (action as ActionItem & { direction_label?: string }).direction_label
  const sourceLabel = formatActionSource(action)
  const createdAtLabel = formatActionDate(action.created_at)

  const handleClick = () => {
    if (hasCompleteActionDetailPayload(action)) {
      setActionDetail(action)
      updateActionInStore(action.id, action)
    }
    openAction(action.id)
  }

  const handleStatusChange = async (e: MouseEvent, status: ActionStatus) => {
    e.stopPropagation()
    try {
      if (status === 'done') {
        await markActionDone(action.id)
      } else if (status === 'dismissed' || status === 'ignored') {
        await dismissAction(action.id)
      } else if (status === 'dispatched') {
        await dispatchAction(action.id)
      } else {
        await updateAction(action.id, { status })
      }
      updateActionInStore(action.id, { status })
      onActionMutation()
    } catch (err) {
      console.error('[actions] status change failed:', err)
      toast.error(`操作失败: ${err instanceof Error ? err.message : '未知错误'}`)  // UX-8: 与全站 toast 一致
    }
    setShowMenu(false)
  }

  const handlePriorityChange = async (e: MouseEvent, priority: ActionPriority) => {
    e.stopPropagation()
    try {
      await updateActionPriority(action.id, priority)
      updateActionInStore(action.id, { priority })
      onActionMutation()
    } catch (err) {
      console.error('[actions] priority change failed:', err)
      toast.error(`操作失败: ${err instanceof Error ? err.message : '未知错误'}`)  // UX-8: 与全站 toast 一致
    }
    setShowMenu(false)
  }

  return (
    <div
      id={`action-card-${action.id}`}
      data-testid="action-card"
      onClick={handleClick}
      className={cn(
        'group relative flex cursor-pointer flex-col gap-3 rounded-[4px] border border-border bg-card p-4 shadow-none transition-colors hover:border-[var(--brand-border)] hover:bg-[#FFFCF8] dark:hover:bg-muted/70',
        isFocused && 'border-[var(--brand-border)] ring-2 ring-[var(--brand-border)]',
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <h4
          className={cn(
            'line-clamp-2 font-event-title text-[20px] font-semibold leading-[1.36] tracking-normal',
            action.status === 'done' ? 'text-muted-foreground line-through' : 'text-foreground',
          )}
        >
          {action.title}
        </h4>
        <button
          type="button"
          aria-label="更多行动操作"
          onClick={(e) => { e.stopPropagation(); setShowMenu((v) => !v) }}
          className="relative -mr-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-[4px] text-[#4F4A43] opacity-80 transition-colors hover:bg-muted hover:text-foreground"
        >
          <MoreHorizontal className="h-4 w-4" />
        </button>
      </div>

      {actionPointItems.length > 0 && (
        <ul
          data-testid="action-point-list"
          className="space-y-1.5 font-event-title text-[15px] leading-[1.58] text-[#4F4A43] dark:text-muted-foreground"
        >
          {actionPointItems.map((item) => (
            <li key={item} className="flex min-w-0 items-start gap-2">
              <span
                aria-hidden="true"
                data-testid="action-point-dot"
                className="mt-[0.55em] h-1.5 w-1.5 shrink-0 rounded-full bg-[var(--brand)] opacity-70"
              />
              <span className="line-clamp-1 min-w-0">{item}</span>
            </li>
          ))}
        </ul>
      )}

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-[14px] text-[#5E574F]">
        {sourceLabel && <span>来自 {sourceLabel}</span>}
        <span
          data-testid="action-status-pill"
          className={cn(
            'ml-auto inline-flex h-7 items-center rounded-full px-2.5 text-[12px] font-medium',
            STATUS_COLORS[action.status],
          )}
        >
          {ACTION_STATUS_LABELS[action.status] || action.status}
        </span>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <span
          data-testid="action-type-pill"
          className="inline-flex h-7 items-center rounded-[4px] bg-[var(--brand-soft)] px-2.5 text-[13px] font-medium text-[var(--brand)]"
        >
          {actionTypeName(action.type)}
        </span>
        {directionLabel && (
          <span className="inline-flex h-7 items-center rounded-[4px] border border-[#EFE7DE] bg-[#FBF7F1] px-2.5 text-[13px] text-[#4F4A43]">
            {directionLabel}
          </span>
        )}
        {action.priority && (
          <span className={cn(
            'inline-flex h-7 items-center rounded-[4px] px-2.5 text-[13px] font-medium',
            action.priority === 'P0'
              ? 'bg-red-50 text-destructive dark:bg-red-950'
              : action.priority === 'P1'
                ? 'bg-[var(--brand-soft)] text-[var(--brand)]'
                : 'bg-[#F3EFE8] text-[#5E574F]',
          )}>
            {action.priority}
          </span>
        )}
      </div>

      <div data-testid="action-card-footer" className="flex items-center gap-3 pt-1 text-[13px] text-[#6B6259]">
        <span data-testid="action-card-created-at" className="inline-flex min-w-0 items-center gap-1.5 font-mono tabular-nums">
          <CalendarDays className="h-3.5 w-3.5 shrink-0" />
          {createdAtLabel}
        </span>
      </div>

      {showMenu && (
        <>
          <div className="fixed inset-0 z-10" onClick={(e) => { e.stopPropagation(); setShowMenu(false) }} />
          <div className="absolute z-20 mt-2 min-w-[148px] self-end rounded-[4px] border border-border bg-card py-1 shadow-medium">
            <button onClick={(e) => handleStatusChange(e, 'done')} className="w-full px-3 py-1.5 text-left text-sm hover:bg-muted">已完成</button>
            <button onClick={(e) => canDispatch ? handleStatusChange(e, 'dispatched') : e.stopPropagation()} disabled={!canDispatch} className={cn('w-full px-3 py-1.5 text-left text-sm', canDispatch ? 'hover:bg-muted' : 'cursor-not-allowed opacity-40')}>派发</button>
            <button onClick={(e) => handleStatusChange(e, 'dismissed')} className="w-full px-3 py-1.5 text-left text-sm hover:bg-muted">忽略</button>
            <button onClick={(e) => handleStatusChange(e, 'pending')} className="w-full px-3 py-1.5 text-left text-sm hover:bg-muted">恢复待处理</button>
            <div className="my-1 border-t border-border" />
            {(['P0', 'P1', 'P2'] as ActionPriority[]).map((priority) => (
              <button key={priority} onClick={(e) => handlePriorityChange(e, priority)} className="w-full px-3 py-1.5 text-left text-sm hover:bg-muted">
                设为 {priority}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

function parseSteps(value: string): string[] {
  try {
    const parsed = JSON.parse(value)
    return Array.isArray(parsed) ? parsed.map(String).filter(Boolean) : []
  } catch {
    return []
  }
}

function isLaneKey(value: string): value is LaneKey {
  return value === 'pending' || value === 'in_progress' || value === 'done'
}

function actionLaneKey(status: ActionStatus): LaneKey | null {
  if (status === 'pending') return 'pending'
  if (status === 'confirmed' || status === 'executing' || status === 'dispatched') return 'in_progress'
  if (status === 'done') return 'done'
  return null
}

function normalizeDirection(direction: string | undefined): string {
  if (!direction) return '_uncategorized'
  if (direction === 'investment') return 'investing'
  return direction
}

function sortActionsByCreatedDesc(a: ActionItem, b: ActionItem): number {
  return new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
}

function matchesPriorityFilter(action: ActionItem, filter: ActionPriority | null): boolean {
  if (!filter) return true
  return action.priority === filter
}

function isInsideDateFilter(action: ActionItem, filter: DateFilter): boolean {
  if (filter === 'all') return true
  const now = new Date()
  const created = new Date(action.created_at)
  if (Number.isNaN(created.getTime())) return true
  if (filter === 'today') {
    return created.toDateString() === now.toDateString()
  }
  const start = new Date(now)
  start.setDate(now.getDate() - 6)
  start.setHours(0, 0, 0, 0)
  return created >= start
}

function getActionPointItems(action: ActionItem): string[] {
  const stepItems = formatActionPointLines(action.steps)
  if (stepItems.length > 0) return stepItems
  const promptItems = formatActionPointText(action.prompt)
  if (promptItems.length > 0) return promptItems
  return formatActionPointText(action.expectation)
}

function formatActionPointText(text?: string): string[] {
  if (!text) return []
  const trimmed = text.trim()
  if (!trimmed) return []
  if (!trimmed.startsWith('[')) return formatActionPointLines(trimmed.split('\n'))
  try {
    const parsed = JSON.parse(trimmed) as unknown
    if (!Array.isArray(parsed)) return formatActionPointLines(trimmed.split('\n'))
    return formatActionPointLines(parsed.map((item) => {
      if (typeof item === 'string') return item
      if (!item || typeof item !== 'object') return ''
      const record = item as { text?: string; label?: string }
      return record.text || record.label || ''
    }))
  } catch {
    return formatActionPointLines(trimmed.split('\n'))
  }
}

function formatActionPointLines(lines?: string[]): string[] {
  const cleaned = (lines ?? [])
    .map((line) => line.replace(/^\s*(?:[-*•]|\d+[.)、]|[一二三四五六七八九十]+[、.])\s*/, '').trim())
    .filter(Boolean)
    .filter((line) => !/^(行动步骤|具体步骤|步骤|完成标准|目标)[:：]?$/.test(line))
  return cleaned.slice(0, 3)
}

function hasCompleteActionDetailPayload(action: ActionItem): boolean {
  if ((action as ActionItem & { _list_payload?: boolean })._list_payload) return false
  return (
    Array.isArray(action.steps) &&
    Array.isArray(action.source_items) &&
    typeof action.source_item_count === 'number'
  )
}

function formatActionSource(action: ActionItem): string | null {
  const extended = action as ActionItem & { source_label?: string; source_title?: string; source?: string }
  if (extended.source_label) return extended.source_label
  if (extended.source_title) return extended.source_title
  if (extended.source) return extended.source
  const count = action.source_item_count ?? action.source_item_ids?.length ?? 0
  return count > 0 ? `${count} 条信息` : null
}

function formatActionDate(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}
