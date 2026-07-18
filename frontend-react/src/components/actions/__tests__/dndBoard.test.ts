import { describe, it, expect, vi } from 'vitest'
import type { ActionItem, ActionStatus } from '../../../lib/types'
import {
  BOARD_SCREEN_READER_INSTRUCTIONS,
  BOARD_SORT_NOTICE,
  LANE_ORDER,
  actionLaneKey,
  createBoardCoordinateGetter,
  dragCancelAnnouncement,
  dragEndAnnouncement,
  dragOverAnnouncement,
  dragStartAnnouncement,
  laneCenterCoordinates,
  moveFailedAnnouncement,
  nextLaneForKey,
  patchBoardSnapshotStatus,
  resolveDropStatus,
  shiftLaneMetaCounts,
  shiftStatusCounts,
  type BoardSnapshot,
} from '../dndBoard'

function makeAction(overrides: Partial<ActionItem> = {}): ActionItem {
  return {
    id: 'act-1',
    title: '示例行动',
    type: 'implementation',
    status: 'pending',
    priority: 'P1',
    created_at: '2026-05-18T08:00:00Z',
    ...overrides,
  } as ActionItem
}

describe('resolveDropStatus — lane→status 映射表 (21.4 状态映射决议)', () => {
  it('待处理 → 执行中 一律落 confirmed (updateAction)', () => {
    expect(resolveDropStatus('pending', 'in_progress')).toEqual({ kind: 'move', toStatus: 'confirmed', api: 'update' })
  })

  it('执行中三个子状态 → 待处理 都落 pending (updateAction)', () => {
    for (const status of ['confirmed', 'executing', 'dispatched'] as ActionStatus[]) {
      expect(resolveDropStatus(status, 'pending')).toEqual({ kind: 'move', toStatus: 'pending', api: 'update' })
    }
  })

  it('任何泳道 → 已完成 都走 markActionDone,不分当前子状态', () => {
    for (const status of ['pending', 'confirmed', 'executing', 'dispatched'] as ActionStatus[]) {
      expect(resolveDropStatus(status, 'done')).toEqual({ kind: 'move', toStatus: 'done', api: 'done' })
    }
  })

  it('已完成可拖回 待处理/执行中', () => {
    expect(resolveDropStatus('done', 'pending')).toEqual({ kind: 'move', toStatus: 'pending', api: 'update' })
    expect(resolveDropStatus('done', 'in_progress')).toEqual({ kind: 'move', toStatus: 'confirmed', api: 'update' })
  })

  it('同泳道 = 无操作(执行中泳道内部拖动不改子状态)', () => {
    expect(resolveDropStatus('pending', 'pending')).toEqual({ kind: 'noop' })
    expect(resolveDropStatus('confirmed', 'in_progress')).toEqual({ kind: 'noop' })
    expect(resolveDropStatus('executing', 'in_progress')).toEqual({ kind: 'noop' })
    expect(resolveDropStatus('dispatched', 'in_progress')).toEqual({ kind: 'noop' })
    expect(resolveDropStatus('done', 'done')).toEqual({ kind: 'noop' })
  })

  it('无效落点(null)与不上板状态(dismissed/failed/ignored)防御为 noop', () => {
    expect(resolveDropStatus('pending', null)).toEqual({ kind: 'noop' })
    expect(resolveDropStatus('pending', undefined)).toEqual({ kind: 'noop' })
    for (const status of ['dismissed', 'failed', 'ignored'] as ActionStatus[]) {
      expect(resolveDropStatus(status, 'done')).toEqual({ kind: 'noop' })
    }
  })
})

describe('nextLaneForKey — 泳道级键盘移动,到端不循环', () => {
  it('ArrowRight 依次 pending→in_progress→done,尾端返回 null', () => {
    expect(nextLaneForKey('ArrowRight', 'pending')).toBe('in_progress')
    expect(nextLaneForKey('ArrowRight', 'in_progress')).toBe('done')
    expect(nextLaneForKey('ArrowRight', 'done')).toBeNull()
  })

  it('ArrowLeft 依次 done→in_progress→pending,首端返回 null', () => {
    expect(nextLaneForKey('ArrowLeft', 'done')).toBe('in_progress')
    expect(nextLaneForKey('ArrowLeft', 'in_progress')).toBe('pending')
    expect(nextLaneForKey('ArrowLeft', 'pending')).toBeNull()
  })

  it('其它按键不移动', () => {
    expect(nextLaneForKey('ArrowUp', 'pending')).toBeNull()
    expect(nextLaneForKey('KeyA', 'pending')).toBeNull()
  })
})

describe('createBoardCoordinateGetter — 直接跳到相邻泳道 rect 中心', () => {
  const laneRects = new Map([
    ['pending', { top: 0, left: 0, width: 320, height: 600 }],
    ['in_progress', { top: 0, left: 340, width: 320, height: 600 }],
    ['done', { top: 0, left: 680, width: 320, height: 600 }],
  ])
  const collisionRect = { top: 60, left: 10, width: 300, height: 140 }

  function makeArgs(over: string | null) {
    return {
      currentCoordinates: { x: collisionRect.left, y: collisionRect.top },
      context: {
        over: over ? { id: over } : null,
        collisionRect,
        droppableRects: laneRects,
      },
    }
  }

  function keyEvent(code: string): KeyboardEvent {
    return { code, preventDefault: vi.fn() } as unknown as KeyboardEvent
  }

  it('ArrowRight 从 over 泳道跳到下一泳道中心(x 按泳道中心减半卡宽)', () => {
    const getter = createBoardCoordinateGetter({ getSourceLane: () => 'pending' })
    const event = keyEvent('ArrowRight')
    const coords = getter(event, makeArgs('pending'))
    expect(coords).toEqual(laneCenterCoordinates(laneRects.get('in_progress')!, collisionRect))
    expect(coords!.x).toBe(340 + 160 - 150)
    expect(event.preventDefault).toHaveBeenCalled()
  })

  it('over 缺失时回退到 getSourceLane(拖拽起点泳道)', () => {
    const getter = createBoardCoordinateGetter({ getSourceLane: () => 'in_progress' })
    const coords = getter(keyEvent('ArrowLeft'), makeArgs(null))
    expect(coords).toEqual(laneCenterCoordinates(laneRects.get('pending')!, collisionRect))
  })

  it('尾端 ArrowRight 不移动(不循环)', () => {
    const getter = createBoardCoordinateGetter({ getSourceLane: () => 'done' })
    expect(getter(keyEvent('ArrowRight'), makeArgs('done'))).toBeUndefined()
  })

  it('↑/↓ 无操作但 preventDefault 并触发排序提示回调', () => {
    const onVerticalArrow = vi.fn()
    const getter = createBoardCoordinateGetter({ getSourceLane: () => 'pending', onVerticalArrow })
    const event = keyEvent('ArrowUp')
    expect(getter(event, makeArgs('pending'))).toBeUndefined()
    expect(event.preventDefault).toHaveBeenCalled()
    expect(onVerticalArrow).toHaveBeenCalledTimes(1)
    expect(getter(keyEvent('ArrowDown'), makeArgs('pending'))).toBeUndefined()
    expect(onVerticalArrow).toHaveBeenCalledTimes(2)
    expect(BOARD_SORT_NOTICE).toContain('自动排序')
  })

  it('LANE_ORDER 恒为 待处理→执行中→已完成', () => {
    expect(LANE_ORDER).toEqual(['pending', 'in_progress', 'done'])
  })
})

describe('快照修补 — 冻结 refetch 的乐观更新与回滚', () => {
  function makeSnapshot(): BoardSnapshot {
    return {
      actions: [
        makeAction({ id: 'act-1', status: 'pending' }),
        makeAction({ id: 'act-2', status: 'confirmed' }),
      ],
      counts: { total: 3, pending: 2, confirmed: 1, in_progress: 1, done: 0 },
      directions: [],
      laneMeta: {
        pending: { count: 2, hasMore: true, nextOffset: 20 },
        in_progress: { count: 1, hasMore: false, nextOffset: null },
        done: { count: 0, hasMore: false, nextOffset: null },
      },
    }
  }

  it('shiftStatusCounts: 原子状态 ±1 并同步 in_progress 聚合键', () => {
    const counts = shiftStatusCounts({ total: 3, pending: 2, confirmed: 1, in_progress: 1, done: 0 }, 'pending', 'confirmed')
    expect(counts.pending).toBe(1)
    expect(counts.confirmed).toBe(2)
    expect(counts.in_progress).toBe(2)
    expect(counts.done).toBe(0)
    expect(counts.total).toBe(3)
  })

  it('shiftStatusCounts: 执行中内部换子状态不动 in_progress 聚合', () => {
    const counts = shiftStatusCounts({ confirmed: 1, executing: 0, in_progress: 1 }, 'confirmed', 'executing')
    expect(counts.confirmed).toBe(0)
    expect(counts.executing).toBe(1)
    expect(counts.in_progress).toBe(1)
  })

  it('shiftLaneMetaCounts: 泳道头计数 ±1 且不落负', () => {
    const meta = makeSnapshot().laneMeta
    const next = shiftLaneMetaCounts(meta, 'pending', 'done')
    expect(next.pending.count).toBe(1)
    expect(next.done.count).toBe(1)
    expect(next.in_progress.count).toBe(1)
    const clamped = shiftLaneMetaCounts(next, 'done', 'pending')
    const again = shiftLaneMetaCounts(clamped, 'done', 'pending')
    expect(again.done.count).toBe(0)
  })

  it('patchBoardSnapshotStatus: 改 status + counts + laneMeta,不动其它字段', () => {
    const snapshot = makeSnapshot()
    const patched = patchBoardSnapshotStatus(snapshot, 'act-1', 'pending', 'confirmed')
    expect(patched.actions.find((a) => a.id === 'act-1')?.status).toBe('confirmed')
    expect(patched.counts.pending).toBe(1)
    expect(patched.counts.in_progress).toBe(2)
    expect(patched.laneMeta.pending.count).toBe(1)
    expect(patched.laneMeta.in_progress.count).toBe(2)
    expect(patched.laneMeta.pending.hasMore).toBe(true)
    expect(patched.laneMeta.pending.nextOffset).toBe(20)
    // 原快照不被原地修改
    expect(snapshot.actions.find((a) => a.id === 'act-1')?.status).toBe('pending')
    expect(snapshot.laneMeta.pending.count).toBe(2)
  })

  it('patchBoardSnapshotStatus: 反向修补 = 回滚(快照对称)', () => {
    const snapshot = makeSnapshot()
    const moved = patchBoardSnapshotStatus(snapshot, 'act-1', 'pending', 'done')
    const rolled = patchBoardSnapshotStatus(moved, 'act-1', 'done', 'pending')
    expect(rolled.actions.find((a) => a.id === 'act-1')?.status).toBe('pending')
    expect(rolled.counts).toEqual(snapshot.counts)
    expect(rolled.laneMeta).toEqual(snapshot.laneMeta)
  })

  it('patchBoardSnapshotStatus: action 不在快照内时原样返回(其它筛选窗口不被污染)', () => {
    const snapshot = makeSnapshot()
    expect(patchBoardSnapshotStatus(snapshot, 'act-999', 'pending', 'done')).toBe(snapshot)
  })
})

describe('a11y 中文播报文案 (调研 §⑤ 五文案)', () => {
  it('拾起/移入/落下/取消/失败 + 指引全中文且含三要素', () => {
    expect(dragStartAnnouncement('验证卡片', 'pending')).toBe('已拾起行动『验证卡片』，当前位于「待处理」泳道。')
    expect(dragOverAnnouncement('in_progress')).toBe('已移动到「执行中」泳道。')
    expect(dragOverAnnouncement(null)).toBe('当前不在任何泳道上。')
    expect(dragEndAnnouncement('验证卡片', 'pending', 'done')).toBe('已将『验证卡片』从「待处理」移动到「已完成」。')
    expect(dragCancelAnnouncement('验证卡片', 'pending')).toBe('已取消移动，『验证卡片』仍在「待处理」泳道。')
    expect(moveFailedAnnouncement('验证卡片', 'pending')).toBe('移动失败，『验证卡片』已恢复到「待处理」泳道。')
    expect(BOARD_SCREEN_READER_INSTRUCTIONS).toContain('按空格键拾起行动卡片')
    expect(BOARD_SCREEN_READER_INSTRUCTIONS).toContain('更多操作菜单')
  })

  it('落回原泳道/无效落点的收尾播报', () => {
    expect(dragEndAnnouncement('验证卡片', 'pending', 'pending')).toBe('已放下，『验证卡片』仍在「待处理」泳道。')
    expect(dragEndAnnouncement('验证卡片', 'pending', null)).toBe('已取消移动，『验证卡片』仍在「待处理」泳道。')
  })
})

describe('actionLaneKey — 状态→泳道(21.4 归并)', () => {
  it('pending/confirmed/executing/dispatched/done 上板,其余不上板', () => {
    expect(actionLaneKey('pending')).toBe('pending')
    expect(actionLaneKey('confirmed')).toBe('in_progress')
    expect(actionLaneKey('executing')).toBe('in_progress')
    expect(actionLaneKey('dispatched')).toBe('in_progress')
    expect(actionLaneKey('done')).toBe('done')
    expect(actionLaneKey('dismissed')).toBeNull()
    expect(actionLaneKey('failed')).toBeNull()
    expect(actionLaneKey('ignored')).toBeNull()
  })
})
