/**
 * v24.0 模块 21.4: 行动 tab 拖拽看板 — 纯逻辑层。
 * 全部为无 DOM 依赖的纯函数，供 ActionsView 接线与单测复用。
 * 语义(硬约束): 拖拽只改泳道、不改序(顺序恒 created_at desc);
 * 拖入「执行中」一律落 confirmed(executing/dispatched 是动作结果态,不由拖拽进入)。
 */
import type { ActionItem, ActionStatus } from '../../lib/types'

export type LaneKey = 'pending' | 'in_progress' | 'done'
export type LaneLoadMeta = { count: number; hasMore: boolean; nextOffset: number | null }
export type BoardSnapshot = {
  actions: ActionItem[]
  counts: Record<string, number>
  directions: Array<{ slug: string; label: string; count: number }>
  laneMeta: Record<LaneKey, LaneLoadMeta>
}

export const LANE_ORDER: LaneKey[] = ['pending', 'in_progress', 'done']

export const LANE_LABELS: Record<LaneKey, string> = {
  pending: '待处理',
  in_progress: '执行中',
  done: '已完成',
}

export function isLaneKey(value: unknown): value is LaneKey {
  return value === 'pending' || value === 'in_progress' || value === 'done'
}

export function actionLaneKey(status: ActionStatus): LaneKey | null {
  if (status === 'pending') return 'pending'
  if (status === 'confirmed' || status === 'executing' || status === 'dispatched') return 'in_progress'
  if (status === 'done') return 'done'
  return null
}

/** onDragEnd 状态映射决议(21.4):
 *  →执行中 = confirmed;→待处理 = pending;→已完成 = markActionDone;同泳道/无效落点 = 无操作。 */
export type DropResolution =
  | { kind: 'noop' }
  | { kind: 'move'; toStatus: 'pending' | 'confirmed' | 'done'; api: 'update' | 'done' }

export function resolveDropStatus(
  sourceStatus: ActionStatus,
  targetLane: LaneKey | null | undefined,
): DropResolution {
  const sourceLane = actionLaneKey(sourceStatus)
  // dismissed/failed/ignored 不在看板上,防御性 no-op
  if (!sourceLane || !targetLane || !isLaneKey(targetLane)) return { kind: 'noop' }
  if (sourceLane === targetLane) return { kind: 'noop' }
  if (targetLane === 'pending') return { kind: 'move', toStatus: 'pending', api: 'update' }
  if (targetLane === 'in_progress') return { kind: 'move', toStatus: 'confirmed', api: 'update' }
  return { kind: 'move', toStatus: 'done', api: 'done' }
}

/** 键盘拖拽:←/→ 在三泳道间移动,到端不循环;↑/↓ 无操作(固定排序)。 */
export function nextLaneForKey(code: string, currentLane: LaneKey): LaneKey | null {
  const index = LANE_ORDER.indexOf(currentLane)
  if (index < 0) return null
  if (code === 'ArrowLeft') return index > 0 ? LANE_ORDER[index - 1] : null
  if (code === 'ArrowRight') return index < LANE_ORDER.length - 1 ? LANE_ORDER[index + 1] : null
  return null
}

type RectLike = { top: number; left: number; width: number; height: number }

/** 泳道级 coordinateGetter 的纯几何核:把卡片(collisionRect)对齐到目标泳道 rect 中心。 */
export function laneCenterCoordinates(
  laneRect: RectLike,
  collisionRect: { width: number; height: number } | null | undefined,
): { x: number; y: number } {
  const width = collisionRect?.width ?? 0
  const height = collisionRect?.height ?? 0
  return {
    x: laneRect.left + laneRect.width / 2 - width / 2,
    y: Math.max(laneRect.top, laneRect.top + laneRect.height / 2 - height / 2),
  }
}

export const BOARD_SORT_NOTICE = '本看板按时间自动排序，无需上下移动'

/** 键盘 coordinateGetter 工厂(泳道级移动,不是 25px 平移)。
 *  依赖通过参数注入,便于单测:getSourceLane 提供拖拽起点泳道,onVerticalArrow 播报固定排序提示。 */
export function createBoardCoordinateGetter(deps: {
  getSourceLane: () => LaneKey | null
  onVerticalArrow?: () => void
}) {
  return (
    event: KeyboardEvent,
    args: {
      currentCoordinates: { x: number; y: number }
      context: {
        over?: { id: string | number } | null
        collisionRect?: RectLike | null
        droppableRects: { get: (id: string) => RectLike | undefined }
      }
    },
  ): { x: number; y: number } | undefined => {
    const { code } = event
    if (code === 'ArrowUp' || code === 'ArrowDown') {
      event.preventDefault()
      deps.onVerticalArrow?.()
      return undefined
    }
    if (code !== 'ArrowLeft' && code !== 'ArrowRight') return undefined
    event.preventDefault()
    const { over, collisionRect, droppableRects } = args.context
    const overLane = over != null && isLaneKey(String(over.id)) ? (String(over.id) as LaneKey) : null
    const currentLane = overLane ?? deps.getSourceLane()
    if (!currentLane) return undefined
    const targetLane = nextLaneForKey(code, currentLane)
    if (!targetLane) return undefined
    const laneRect = droppableRects.get(targetLane)
    if (!laneRect) return undefined
    return laneCenterCoordinates(laneRect, collisionRect)
  }
}

/** counts 平移:镜像 actionStore.updateAction 的逻辑并补齐 in_progress 聚合键。 */
export function shiftStatusCounts(
  counts: Record<string, number>,
  fromStatus: ActionStatus,
  toStatus: ActionStatus,
): Record<string, number> {
  if (fromStatus === toStatus) return counts
  const next = { ...counts }
  if (next[fromStatus] != null) next[fromStatus] = Math.max(0, next[fromStatus] - 1)
  next[toStatus] = (next[toStatus] ?? 0) + 1
  const fromLane = actionLaneKey(fromStatus)
  const toLane = actionLaneKey(toStatus)
  if (fromLane !== toLane) {
    if (fromLane === 'in_progress' && next.in_progress != null) {
      next.in_progress = Math.max(0, next.in_progress - 1)
    }
    if (toLane === 'in_progress') {
      next.in_progress = (next.in_progress ?? 0) + 1
    }
  }
  return next
}

/** laneMeta 计数 ±1(泳道头计数即时更新的来源)。 */
export function shiftLaneMetaCounts(
  laneMeta: Record<LaneKey, LaneLoadMeta>,
  fromLane: LaneKey | null,
  toLane: LaneKey | null,
): Record<LaneKey, LaneLoadMeta> {
  if (!fromLane || !toLane || fromLane === toLane) return laneMeta
  return {
    ...laneMeta,
    [fromLane]: { ...laneMeta[fromLane], count: Math.max(0, laneMeta[fromLane].count - 1) },
    [toLane]: { ...laneMeta[toLane], count: laneMeta[toLane].count + 1 },
  }
}

/** 冻结 refetch 的配套:把某 action 的新 status 同步写进缓存快照(不整板重拉)。
 *  action 不在该快照(其它筛选窗口)时原样返回——陈旧缓存靠下次 SWR refetch 自愈。 */
export function patchBoardSnapshotStatus(
  snapshot: BoardSnapshot,
  actionId: string,
  fromStatus: ActionStatus,
  toStatus: ActionStatus,
): BoardSnapshot {
  const id = String(actionId)
  if (!snapshot.actions.some((action) => String(action.id) === id)) return snapshot
  return {
    ...snapshot,
    actions: snapshot.actions.map((action) =>
      String(action.id) === id ? { ...action, status: toStatus } : action,
    ),
    counts: shiftStatusCounts(snapshot.counts, fromStatus, toStatus),
    laneMeta: shiftLaneMetaCounts(snapshot.laneMeta, actionLaneKey(fromStatus), actionLaneKey(toStatus)),
  }
}

// ---- a11y 中文播报(调研 §⑤,Atlassian 三要素:什么东西、从哪、到哪) ----

export const BOARD_SCREEN_READER_INSTRUCTIONS =
  '按空格键拾起行动卡片；拾起后用左右方向键在待处理、执行中、已完成泳道之间移动；再按空格键放下，按 Esc 键取消。也可以通过卡片上的更多操作菜单完成同样的移动。'

function laneLabel(lane: LaneKey | null): string {
  return lane ? LANE_LABELS[lane] : '未知'
}

export function dragStartAnnouncement(title: string, sourceLane: LaneKey | null): string {
  return `已拾起行动『${title}』，当前位于「${laneLabel(sourceLane)}」泳道。`
}

export function dragOverAnnouncement(overLane: LaneKey | null): string {
  return overLane ? `已移动到「${laneLabel(overLane)}」泳道。` : '当前不在任何泳道上。'
}

export function dragEndAnnouncement(
  title: string,
  sourceLane: LaneKey | null,
  overLane: LaneKey | null,
): string {
  if (!overLane) return dragCancelAnnouncement(title, sourceLane)
  if (overLane === sourceLane) return `已放下，『${title}』仍在「${laneLabel(sourceLane)}」泳道。`
  return `已将『${title}』从「${laneLabel(sourceLane)}」移动到「${laneLabel(overLane)}」。`
}

export function dragCancelAnnouncement(title: string, sourceLane: LaneKey | null): string {
  return `已取消移动，『${title}』仍在「${laneLabel(sourceLane)}」泳道。`
}

export function moveFailedAnnouncement(title: string, sourceLane: LaneKey | null): string {
  return `移动失败，『${title}』已恢复到「${laneLabel(sourceLane)}」泳道。`
}
