import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type TouchEvent as ReactTouchEvent,
} from 'react'
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  MouseSensor,
  TouchSensor,
  pointerWithin,
  rectIntersection,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type Announcements,
  type CollisionDetection,
  type DragEndEvent,
  type DragStartEvent,
  type KeyboardCoordinateGetter,
  type Over,
  type ScreenReaderInstructions,
} from '@dnd-kit/core'
import { CalendarDays, ChevronDown, MoreHorizontal } from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '../../lib/utils'
import { useActionStore } from '../../store/actionStore'
import { useDetailStore } from '../../store/detailStore'
import { useAuthStore } from '../../store/authStore'
import { fetchActionsBoard, markActionDone, dismissAction, dispatchAction, updateAction, type ActionsBoardResponse } from '../../lib/api'
import type { ActionBoardDirection, ActionItem, ActionStatus } from '../../lib/types'
import {
  BOARD_SCREEN_READER_INSTRUCTIONS,
  BOARD_SORT_NOTICE,
  actionLaneKey,
  createBoardCoordinateGetter,
  dragCancelAnnouncement,
  dragEndAnnouncement,
  dragOverAnnouncement,
  dragStartAnnouncement,
  isLaneKey,
  moveFailedAnnouncement,
  patchBoardSnapshotStatus,
  resolveDropStatus,
  shiftLaneMetaCounts,
  type BoardSnapshot,
  type LaneKey,
  type LaneLoadMeta,
} from './dndBoard'

type DateFilter = 'all' | 'today' | 'week'

const ACTION_LANES: Array<{ key: LaneKey; label: string; emptyLabel: string }> = [
  { key: 'pending', label: '待处理', emptyLabel: '暂无待处理行动' },
  { key: 'in_progress', label: '执行中', emptyLabel: '暂无执行中行动' },
  { key: 'done', label: '已完成', emptyLabel: '暂无已完成行动' },
]

const DATE_LABELS: Record<DateFilter, string> = {
  all: '全部',
  today: '今天',
  week: '本周',
}

const CARDS_PER_PAGE = 20
/** 21.4: 拖拽期间及落下后 2s 内冻结 board refetch(防分页窗口吞掉刚拖的卡)。 */
const POST_DROP_REFETCH_FREEZE_MS = 2000
/** 落下后卡片「墨迹落定」背景淡出时长。 */
const CARD_SETTLE_MS = 600
/** 拖拽刚结束的窗口内抑制卡片 click(同卡位微拖不应误开弹窗)。 */
const CLICK_SUPPRESS_AFTER_DRAG_MS = 250

type ActiveDrag = { action: ActionItem; sourceLane: LaneKey }
type MoveCommand = { toStatus: ActionStatus; api: 'update' | 'done'; undoable: boolean }

/** 「···」菜单与其弹层不参与拖拽激活(调研 §⑥-2:事件 target 判断)。 */
const stopDragActivation = {
  onPointerDown: (event: ReactPointerEvent) => event.stopPropagation(),
  onMouseDown: (event: MouseEvent) => event.stopPropagation(),
  onTouchStart: (event: ReactTouchEvent) => event.stopPropagation(),
}

export function ActionsView() {
  const actions = useActionStore((s) => s.actions)
  const counts = useActionStore((s) => s.counts)
  const directions = useActionStore((s) => s.directions)
  const setActionsResponse = useActionStore((s) => s.setActionsResponse)
  const updateActionInStore = useActionStore((s) => s.updateAction)
  const setIsLoading = useActionStore((s) => s.setIsLoading)
  const isLoading = useActionStore((s) => s.isLoading)
  const focusedActionId = useActionStore((s) => s.focusedActionId)
  const [dateFilter, setDateFilter] = useState<DateFilter>('all')
  const [loadError, setLoadError] = useState<string | null>(null)
  const [laneMeta, setLaneMeta] = useState<Record<LaneKey, LaneLoadMeta>>(emptyLaneMeta())
  const [loadingMoreLane, setLoadingMoreLane] = useState<LaneKey | null>(null)
  const [boardRevision, setBoardRevision] = useState(0)
  const [activeDrag, setActiveDrag] = useState<ActiveDrag | null>(null)
  const [settlingActionId, setSettlingActionId] = useState<string | null>(null)
  const [liveMessage, setLiveMessage] = useState('')
  const prefersReducedMotion = usePrefersReducedMotion()
  const boardCacheRef = useRef<Record<string, BoardSnapshot>>({})
  const requestSeqRef = useRef(0)
  const boardQueryKeyRef = useRef('')
  const activeDragRef = useRef<ActiveDrag | null>(null)
  /** 播报专用快照:onDragEnd 时 store 可能已被乐观更新,源泳道以拾起时为准。 */
  const announceDragRef = useRef<{ title: string; sourceLane: LaneKey | null }>({ title: '', sourceLane: null })
  const dragLifecycleRef = useRef<{ dragging: boolean; freezeUntil: number; pendingMutation: boolean; flushTimer: number | null }>({
    dragging: false,
    freezeUntil: 0,
    pendingMutation: false,
    flushTimer: null,
  })
  const deferredBoardRef = useRef<{ key: string; snapshot: BoardSnapshot } | null>(null)
  const dropOutcomeRef = useRef<'drop' | 'cancel'>('drop')
  const sortNoticeShownRef = useRef(false)
  const lastDragEndAtRef = useRef(0)
  const settleTimerRef = useRef<number | null>(null)

  const boardQuery = useMemo(() => ({
    date_filter: dateFilter === 'all' ? undefined : dateFilter,
    limit_per_direction: CARDS_PER_PAGE,
  }), [dateFilter])
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
    boardQueryKeyRef.current = boardQueryKey
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
        if (dragLifecycleRef.current.dragging) {
          // 拖拽期间冻结外部数据落地:暂存,dragEnd 后再 apply(期间的乐观补丁会同步进暂存快照)。
          deferredBoardRef.current = { key: boardQueryKey, snapshot }
          return
        }
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

  useEffect(() => () => {
    if (settleTimerRef.current != null) window.clearTimeout(settleTimerRef.current)
    const lifecycle = dragLifecycleRef.current
    if (lifecycle.flushTimer != null) window.clearTimeout(lifecycle.flushTimer)
  }, [])

  const visibleActions = useMemo(() => {
    return actions.filter((action) => (
      actionLaneKey(action.status) != null &&
      isInsideDateFilter(action, dateFilter)
    ))
  }, [actions, dateFilter])

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
      return
    }
    window.setTimeout(() => {
      const el = document.getElementById(`action-card-${focusedActionId}`)
      el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 80)
  }, [focusedActionId, actions, visibleActions, isLoading])

  const flushBoardRefetch = useCallback(() => {
    const lifecycle = dragLifecycleRef.current
    lifecycle.pendingMutation = false
    if (lifecycle.flushTimer != null) {
      window.clearTimeout(lifecycle.flushTimer)
      lifecycle.flushTimer = null
    }
    boardCacheRef.current = {}
    setBoardRevision((current) => current + 1)
  }, [])

  /** 菜单等常规 mutation 仍走全量重拉;但拖拽进行中/落下后冻结窗口内推迟执行(21.4 硬断言)。 */
  const handleActionMutation = useCallback(() => {
    const lifecycle = dragLifecycleRef.current
    if (lifecycle.dragging) {
      lifecycle.pendingMutation = true
      return
    }
    const wait = lifecycle.freezeUntil - Date.now()
    if (wait > 0) {
      lifecycle.pendingMutation = true
      if (lifecycle.flushTimer == null) {
        lifecycle.flushTimer = window.setTimeout(() => {
          lifecycle.flushTimer = null
          flushBoardRefetch()
        }, wait)
      }
      return
    }
    flushBoardRefetch()
  }, [flushBoardRefetch])

  /** 乐观移动:store 先动(counts 即时 ±1)、泳道头计数 ±1、缓存快照就地修补——不整板重拉。 */
  const applyOptimisticMove = useCallback((actionId: string, fromStatus: ActionStatus, toStatus: ActionStatus) => {
    updateActionInStore(String(actionId), { status: toStatus })
    setLaneMeta((prev) => shiftLaneMetaCounts(prev, actionLaneKey(fromStatus), actionLaneKey(toStatus)))
    for (const key of Object.keys(boardCacheRef.current)) {
      boardCacheRef.current[key] = patchBoardSnapshotStatus(boardCacheRef.current[key], String(actionId), fromStatus, toStatus)
    }
    if (deferredBoardRef.current) {
      deferredBoardRef.current = {
        ...deferredBoardRef.current,
        snapshot: patchBoardSnapshotStatus(deferredBoardRef.current.snapshot, String(actionId), fromStatus, toStatus),
      }
    }
  }, [updateActionInStore])

  const markSettling = useCallback((actionId: string) => {
    if (settleTimerRef.current != null) window.clearTimeout(settleTimerRef.current)
    setSettlingActionId(String(actionId))
    settleTimerRef.current = window.setTimeout(() => {
      settleTimerRef.current = null
      setSettlingActionId(null)
    }, CARD_SETTLE_MS)
  }, [])

  /** 落子提交:快照旧态 → 乐观更新 → API 后发;失败回滚 + 重试;拖入已完成配 5s 撤销。 */
  async function commitMove(action: ActionItem, fromStatus: ActionStatus, command: MoveCommand): Promise<void> {
    const id = String(action.id)
    applyOptimisticMove(id, fromStatus, command.toStatus)
    markSettling(id)
    let undoToastId: string | number | undefined
    if (command.undoable) {
      undoToastId = toast('已完成', {
        duration: 5000,
        action: {
          label: '撤销',
          onClick: () => {
            void commitMove(action, command.toStatus, { toStatus: fromStatus, api: 'update', undoable: false })
          },
        },
      })
    }
    try {
      if (command.api === 'done') {
        await markActionDone(id)
      } else {
        await updateAction(id, { status: command.toStatus })
      }
      // 成功即结束:不清缓存、不 boardRevision++,缓存快照已在 applyOptimisticMove 修补。
    } catch (err) {
      console.error('[actions] drag move failed:', err)
      if (undoToastId != null) toast.dismiss(undoToastId)
      applyOptimisticMove(id, command.toStatus, fromStatus)
      setSettlingActionId(null)
      setLiveMessage(moveFailedAnnouncement(action.title, actionLaneKey(fromStatus)))
      toast.error('移动失败，已恢复原位置', {
        action: {
          label: '重试',
          onClick: () => {
            void commitMove(action, fromStatus, command)
          },
        },
      })
    }
  }

  function finishDragLifecycle() {
    const lifecycle = dragLifecycleRef.current
    lifecycle.dragging = false
    lifecycle.freezeUntil = Date.now() + POST_DROP_REFETCH_FREEZE_MS
    const deferred = deferredBoardRef.current
    if (deferred) {
      deferredBoardRef.current = null
      boardCacheRef.current[deferred.key] = deferred.snapshot
      if (deferred.key === boardQueryKeyRef.current) applyBoardSnapshot(deferred.snapshot)
    }
    if (lifecycle.pendingMutation && lifecycle.flushTimer == null) {
      lifecycle.flushTimer = window.setTimeout(() => {
        lifecycle.flushTimer = null
        flushBoardRefetch()
      }, POST_DROP_REFETCH_FREEZE_MS)
    }
    lastDragEndAtRef.current = Date.now()
  }

  function handleDragStart({ active }: DragStartEvent) {
    const action = useActionStore.getState().actions.find((a) => String(a.id) === String(active.id))
    const sourceLane = action ? actionLaneKey(action.status) : null
    sortNoticeShownRef.current = false
    setLiveMessage('')
    if (!action || !sourceLane) return
    const drag: ActiveDrag = { action, sourceLane }
    activeDragRef.current = drag
    announceDragRef.current = { title: action.title, sourceLane }
    dragLifecycleRef.current.dragging = true
    setActiveDrag(drag)
  }

  function handleDragEnd({ over, activatorEvent }: DragEndEvent) {
    const drag = activeDragRef.current
    const targetLane = laneKeyOfOver(over)
    dropOutcomeRef.current = drag != null && targetLane != null && targetLane !== drag.sourceLane ? 'drop' : 'cancel'
    finishDragLifecycle()
    activeDragRef.current = null
    setActiveDrag(null)
    if (!drag) return
    const resolution = resolveDropStatus(drag.action.status, targetLane)
    if (resolution.kind === 'move') {
      void commitMove(drag.action, drag.action.status, {
        toStatus: resolution.toStatus,
        api: resolution.api,
        undoable: resolution.toStatus === 'done',
      })
    }
    // 键盘落下后焦点跟随卡片到新泳道(跨泳道重挂后手动 focus 回卡片)。
    if (activatorEvent instanceof KeyboardEvent) {
      window.requestAnimationFrame(() => {
        document.getElementById(`action-card-${drag.action.id}`)?.focus()
      })
    }
  }

  function handleDragCancel() {
    dropOutcomeRef.current = 'cancel'
    finishDragLifecycle()
    activeDragRef.current = null
    setActiveDrag(null)
  }

  const boardCoordinateGetter = useMemo(() => createBoardCoordinateGetter({
    getSourceLane: () => activeDragRef.current?.sourceLane ?? null,
    onVerticalArrow: () => {
      if (sortNoticeShownRef.current) return
      sortNoticeShownRef.current = true
      setLiveMessage(BOARD_SORT_NOTICE)
    },
  }) as KeyboardCoordinateGetter, [])

  const sensors = useSensors(
    // 指针 8px 内=点击(开弹窗语义不变);触屏长按 250ms/tolerance 8px。
    // 用 MouseSensor 而非 PointerSensor:触屏上 pointerdown 先于 touchstart 触发,
    // PointerSensor 会抢占激活权饿死 TouchSensor,长按拖拽将失效(dnd-kit activator 单捕获机制)。
    useSensor(MouseSensor, { activationConstraint: { distance: 8 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: boardCoordinateGetter,
      // Space 拾起(Enter 让位给「开详情」);Enter/Space 落下;Esc 取消。
      keyboardCodes: { start: ['Space'], cancel: ['Escape'], end: ['Enter', 'Space'] },
    }),
  )

  const boardCollisionDetection = useCallback<CollisionDetection>((args) => {
    const within = pointerWithin(args)
    return within.length > 0 ? within : rectIntersection(args)
  }, [])

  const announcements = useMemo<Announcements>(() => ({
    onDragStart: ({ active }) => {
      const action = useActionStore.getState().actions.find((a) => String(a.id) === String(active.id))
      if (!action) return undefined
      return dragStartAnnouncement(action.title, actionLaneKey(action.status))
    },
    onDragOver: ({ over }) => dragOverAnnouncement(laneKeyOfOver(over ?? null)),
    onDragEnd: ({ over }) => dragEndAnnouncement(
      announceDragRef.current.title,
      announceDragRef.current.sourceLane,
      laneKeyOfOver(over ?? null),
    ),
    onDragCancel: () => dragCancelAnnouncement(announceDragRef.current.title, announceDragRef.current.sourceLane),
  }), [])

  const screenReaderInstructions = useMemo<ScreenReaderInstructions>(() => ({
    draggable: BOARD_SCREEN_READER_INSTRUCTIONS,
  }), [])

  const shouldSuppressCardClick = useCallback(() => (
    activeDragRef.current != null ||
    Date.now() - lastDragEndAtRef.current < CLICK_SUPPRESS_AFTER_DRAG_MS
  ), [])

  return (
    <div data-testid="actions-view-shell" className="mx-auto w-full max-w-[1200px] px-4 pb-10">
      <ActionFilterSubbar
        dateFilter={dateFilter}
        onDateChange={setDateFilter}
      />

      {loadError && (
        <div className="mb-5 rounded-[4px] border border-[var(--brand-border)] bg-[var(--brand-soft)] px-4 py-3 text-sm text-[var(--brand)]">
          行动数据暂时不可用：{loadError}
        </div>
      )}

      {isLoading && actions.length === 0 ? (
        <ActionsBoardSkeleton />
      ) : (
        <DndContext
          sensors={sensors}
          collisionDetection={boardCollisionDetection}
          autoScroll={{ threshold: { x: 0.2, y: 0.15 } }}
          accessibility={{ announcements, screenReaderInstructions }}
          onDragStart={handleDragStart}
          onDragEnd={handleDragEnd}
          onDragCancel={handleDragCancel}
        >
          <div
            data-testid="actions-lane-grid"
            data-board-dragging={activeDrag ? 'true' : undefined}
            className="grid grid-cols-1 gap-4 lg:grid-cols-3 lg:items-start xl:gap-5"
          >
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
                  dragSourceLane={activeDrag?.sourceLane ?? null}
                  isBoardDragging={activeDrag != null}
                  settlingActionId={settlingActionId}
                  shouldSuppressCardClick={shouldSuppressCardClick}
                />
              )
            })}
          </div>
          <DragOverlay
            dropAnimation={prefersReducedMotion ? null : {
              duration: dropOutcomeRef.current === 'cancel' ? 180 : 200,
              easing: 'ease-out',
            }}
          >
            {activeDrag ? <ActionDragOverlayCard action={activeDrag.action} /> : null}
          </DragOverlay>
        </DndContext>
      )}

      <div className="sr-only" role="status" aria-live="assertive" data-testid="actions-dnd-live">
        {liveMessage}
      </div>
    </div>
  )
}

function laneKeyOfOver(over: Over | null | undefined): LaneKey | null {
  if (over == null) return null
  const id = String(over.id)
  return isLaneKey(id) ? id : null
}

/** reduced-motion 下 dropAnimation 置 null、去倾斜缩放(颜色状态保留,见 globals.css)。 */
function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  ))
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const query = window.matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = () => setReduced(query.matches)
    query.addEventListener?.('change', onChange)
    return () => query.removeEventListener?.('change', onChange)
  }, [])
  return reduced
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
  limit_per_direction?: number
}): string {
  return [
    `date:${query.date_filter ?? 'all'}`,
    `limit:${query.limit_per_direction ?? CARDS_PER_PAGE}`,
  ].join('|')
}

function ActionFilterSubbar({
  dateFilter,
  onDateChange,
}: {
  dateFilter: DateFilter
  onDateChange: (filter: DateFilter) => void
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

// v24.0 §21.6 骨架统一: 硬编码米色(#EFE8DE 系,暗色下发亮)删除,
// 统一走 animate-skeleton + muted token(亮暗自适应);brand-soft 占位块保留(已是 token)。
function ActionsBoardSkeleton() {
  return (
    <div data-testid="actions-board-skeleton" className="grid grid-cols-1 gap-4 lg:grid-cols-3 lg:items-start xl:gap-5">
      {ACTION_LANES.map((lane) => (
        <section key={lane.key} data-testid="action-lane-skeleton" className="min-w-0 rounded-[4px] border border-border bg-transparent p-3">
          <div className="mb-3 flex items-baseline gap-2 px-1">
            <h3 className="font-event-title text-[22px] font-bold leading-none text-foreground">{lane.label}</h3>
            <span className="h-4 w-8 animate-skeleton rounded-[3px] bg-muted" />
          </div>
          <div className="space-y-3">
            {Array.from({ length: 3 }).map((_, index) => (
              <div key={index} className="rounded-[4px] border border-border bg-card p-4">
                <div className="mb-3 flex items-start justify-between gap-3">
                  <span className="h-5 w-4/5 animate-skeleton rounded-[3px] bg-muted" />
                  <span className="h-7 w-7 shrink-0 animate-skeleton rounded-[4px] bg-muted" />
                </div>
                <div className="space-y-2">
                  <span className="block h-3.5 w-full animate-skeleton rounded-[3px] bg-muted" />
                  <span className="block h-3.5 w-3/4 animate-skeleton rounded-[3px] bg-muted" />
                  <span className="block h-3.5 w-5/6 animate-skeleton rounded-[3px] bg-muted" />
                </div>
                <div className="mt-4 flex items-center gap-2">
                  <span className="h-7 w-16 animate-pulse rounded-[4px] bg-[var(--brand-soft)]" />
                  <span className="h-7 w-20 animate-skeleton rounded-[4px] bg-muted" />
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
  dragSourceLane,
  isBoardDragging,
  settlingActionId,
  shouldSuppressCardClick,
}: {
  lane: { key: LaneKey; label: string; emptyLabel: string }
  items: ActionItem[]
  totalCount: number
  hasMoreFromServer: boolean
  isLoadingMore: boolean
  onLoadMore: () => Promise<boolean>
  onActionMutation: () => void
  dragSourceLane: LaneKey | null
  isBoardDragging: boolean
  settlingActionId: string | null
  shouldSuppressCardClick: () => boolean
}) {
  const remainingCount = Math.max(0, totalCount - items.length)
  const { setNodeRef, isOver } = useDroppable({ id: lane.key })
  // 21.4: 目标泳道整体高亮;源泳道自身不高亮(放回=无操作)。
  const isDropTarget = isOver && dragSourceLane != null && dragSourceLane !== lane.key

  return (
    <section
      ref={setNodeRef}
      data-testid="action-lane"
      data-lane={lane.key}
      data-drop-target={isDropTarget ? 'true' : undefined}
      aria-label={`${lane.label}泳道，共 ${totalCount} 条`}
      className={cn(
        'min-w-0 rounded-[4px] border bg-transparent p-3 transition-colors duration-[180ms] ease-out',
        isDropTarget ? 'border-[var(--brand-border)] bg-[var(--brand-soft)]' : 'border-border',
      )}
    >
      <div className="mb-3 flex items-baseline gap-2 px-1">
        <h3 className="font-event-title text-[22px] font-bold leading-none text-foreground">{lane.label}</h3>
        <span data-testid="action-lane-count" className="font-body-cjk text-[14px] font-normal text-muted-foreground">{totalCount}</span>
      </div>

      {items.length > 0 ? (
        <div className="space-y-3">
          {items.map((action) => (
            <ActionCard
              key={action.id}
              action={action}
              onActionMutation={onActionMutation}
              isSettling={settlingActionId != null && String(action.id) === settlingActionId}
              shouldSuppressClick={shouldSuppressCardClick}
            />
          ))}
        </div>
      ) : (
        <div
          data-testid="action-lane-empty"
          className={cn(
            'rounded-[4px] border border-dashed px-3 py-8 text-center transition-colors duration-[180ms] ease-out',
            isDropTarget ? 'border-[var(--brand-border)] bg-transparent' : 'border-border/80 bg-background/40',
          )}
        >
          <p className={cn('text-sm', isDropTarget ? 'text-[var(--brand)]' : 'text-muted-foreground')}>
            {isDropTarget ? '放到这里' : lane.emptyLabel}
          </p>
        </div>
      )}

      {hasMoreFromServer && (
        <div className="mt-4 flex justify-center">
          <button
            onClick={() => void onLoadMore()}
            disabled={isLoadingMore}
            className={cn(
              // v24.0 §21.6: 阴影胶囊 → hairline 边框按钮(对齐 SectionFront expand 样式)
              'mx-auto flex cursor-pointer items-center gap-1.5 rounded-[4px] border border-border bg-card px-5 py-2 text-sm font-medium text-muted-foreground transition-colors hover:border-[var(--brand-border)] hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60',
              // 拖动中「展开更多」不做投放障碍(调研 §⑥-4b)
              isBoardDragging && 'pointer-events-none',
            )}
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

/** 卡片正文(标题行/行动点/来源行),ActionCard 与 DragOverlay 共用同一份渲染。 */
function ActionCardContent({ action, moreButton }: { action: ActionItem; moreButton: ReactNode }) {
  const actionPointItems = getActionPointItems(action)
  const sourceLabel = formatActionSource(action)
  const createdAtLabel = formatActionDate(action.created_at)

  return (
    <>
      <div className="flex items-start justify-between gap-3">
        <h4
          className={cn(
            'line-clamp-2 font-event-title text-[20px] font-semibold leading-[1.36] tracking-normal',
            action.status === 'done' ? 'text-muted-foreground line-through' : 'text-foreground',
          )}
        >
          {action.title}
        </h4>
        {moreButton}
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
              <span className="line-clamp-2 min-w-0">{item}</span>
            </li>
          ))}
        </ul>
      )}

      {/* BF-0706-6: 删类型/方向/优先级/状态徽章(状态与列头重复,优先级已下线);
          卡片聚焦行动点内容。仅保留 来自N条 + 时间。 */}
      <div data-testid="action-card-footer" className="flex flex-wrap items-center gap-x-3 gap-y-1 pt-1 text-[13px] text-[#6B6259]">
        {sourceLabel && <span>来自 {sourceLabel}</span>}
        <span data-testid="action-card-created-at" className="inline-flex min-w-0 items-center gap-1.5 font-mono tabular-nums">
          <CalendarDays className="h-3.5 w-3.5 shrink-0" />
          {createdAtLabel}
        </span>
      </div>
    </>
  )
}

function ActionCard({
  action,
  onActionMutation,
  isSettling,
  shouldSuppressClick,
}: {
  action: ActionItem
  onActionMutation: () => void
  isSettling: boolean
  shouldSuppressClick: () => boolean
}) {
  const openAction = useDetailStore((s) => s.openAction)
  const setActionDetail = useDetailStore((s) => s.setActionDetail)
  const updateActionInStore = useActionStore((s) => s.updateAction)
  const focusedActionId = useActionStore((s) => s.focusedActionId)
  const canDispatch = useAuthStore((s) => s.user?.has_discord_token ?? false)
  const [showMenu, setShowMenu] = useState(false)
  const isFocused = String(action.id) === String(focusedActionId || '')
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, isDragging } = useDraggable({
    id: String(action.id),
    data: { title: action.title, lane: actionLaneKey(action.status) },
    attributes: { roleDescription: '可拖拽的行动卡片' },
  })

  // 激活节点=卡片本体:菜单等子元素上的 Space 不会拾起卡片(KeyboardSensor activator 会校验 target)。
  const setCardRef = (node: HTMLElement | null) => {
    setNodeRef(node)
    setActivatorNodeRef(node)
  }

  const handleClick = () => {
    if (shouldSuppressClick()) return
    if (hasCompleteActionDetailPayload(action)) {
      setActionDetail(action)
      updateActionInStore(action.id, action)
    }
    openAction(action.id)
  }

  // Enter=开详情(与点击对齐,补键盘可达);拖拽中 Enter 由 KeyboardSensor 作「落下」,不开弹窗。
  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    listeners?.onKeyDown?.(event)
    if (event.key !== 'Enter') return
    if (isDragging || event.defaultPrevented) return
    if (event.target !== event.currentTarget) return
    handleClick()
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

  return (
    <div
      ref={setCardRef}
      id={`action-card-${action.id}`}
      data-testid="action-card"
      data-drag-state={isDragging ? 'source' : isSettling ? 'settling' : 'idle'}
      onClick={handleClick}
      {...attributes}
      {...listeners}
      onKeyDown={handleKeyDown}
      className={cn(
        'group relative flex cursor-grab select-none touch-manipulation flex-col gap-3 rounded-[4px] border border-border bg-card p-4 shadow-none transition-colors hover:border-[var(--brand-border)] hover:bg-[#FFFCF8] dark:hover:bg-muted/70',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background',
        // 拾起后源卡原位降透明、不塌陷(强化「排序固定、只是换泳道」语义)
        isDragging && 'opacity-40',
        isSettling && 'action-card-settling',
        isFocused && 'border-[var(--brand-border)] ring-2 ring-[var(--brand-border)]',
      )}
    >
      <ActionCardContent
        action={action}
        moreButton={(
          <button
            type="button"
            aria-label="更多行动操作"
            onClick={(e) => { e.stopPropagation(); setShowMenu((v) => !v) }}
            {...stopDragActivation}
            className="relative -mr-1 flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-[4px] text-[#4F4A43] opacity-80 transition-colors hover:bg-muted hover:text-foreground"
          >
            <MoreHorizontal className="h-4 w-4" />
          </button>
        )}
      />

      {showMenu && (
        <>
          <div className="fixed inset-0 z-10" {...stopDragActivation} onClick={(e) => { e.stopPropagation(); setShowMenu(false) }} />
          <div className="absolute z-20 mt-2 min-w-[148px] self-end rounded-[4px] border border-border bg-card py-1 shadow-medium" {...stopDragActivation}>
            <button onClick={(e) => handleStatusChange(e, 'done')} className="w-full px-3 py-1.5 text-left text-sm hover:bg-muted">已完成</button>
            <button onClick={(e) => canDispatch ? handleStatusChange(e, 'dispatched') : e.stopPropagation()} disabled={!canDispatch} className={cn('w-full px-3 py-1.5 text-left text-sm', canDispatch ? 'hover:bg-muted' : 'cursor-not-allowed opacity-40')}>派发</button>
            <button onClick={(e) => handleStatusChange(e, 'dismissed')} className="w-full px-3 py-1.5 text-left text-sm hover:bg-muted">忽略</button>
            <button onClick={(e) => handleStatusChange(e, 'pending')} className="w-full px-3 py-1.5 text-left text-sm hover:bg-muted">恢复待处理</button>
          </div>
        </>
      )}
    </div>
  )
}

/** DragOverlay 悬浮卡:同一张卡的克隆,倾斜 1.5° + 放大 1.02 + brand 边框 + shadow-medium
 *  (悬浮层=系统唯一合法投影场景)。lift 150ms 见 globals.css .action-drag-overlay。 */
function ActionDragOverlayCard({ action }: { action: ActionItem }) {
  return (
    <div
      data-testid="action-drag-overlay"
      className="action-drag-overlay flex cursor-grabbing flex-col gap-3 rounded-[4px] border border-[var(--brand-border)] bg-[#FFFCF8] p-4 shadow-medium dark:bg-muted"
    >
      <ActionCardContent
        action={action}
        moreButton={(
          <span
            aria-hidden="true"
            className="relative -mr-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-[4px] text-[#4F4A43] opacity-80"
          >
            <MoreHorizontal className="h-4 w-4" />
          </span>
        )}
      />
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

function sortActionsByCreatedDesc(a: ActionItem, b: ActionItem): number {
  return new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
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
