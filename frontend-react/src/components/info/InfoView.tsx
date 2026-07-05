/**
 * v18.0 nav-merge: 信息 tab 主容器组件
 *
 * rev0（2026-05-15 初次实现）：仅是 ChannelsView 的别名导出。
 * rev1（2026-05-15 用户反馈触发，PRD §Spec-2.5）：升级为真实容器组件，
 * 顶部新增 InfoGroupByToggle 切换「按频道 / 按分类」两个视角，
 * 内容区根据 groupBy 状态条件渲染对应子视图：
 *   - groupBy === 'platform' → ChannelsView（按平台 section + 子源 pill + L1 pill）
 *   - groupBy === 'category' → InfoCategoryView（按 ai_category L1 分类 sections）
 *
 * 用户偏好持久化到 localStorage（key=`info_tab_group_by`，值=platform|category）：
 *   - 默认 'category'：2026-05-23 信息页恢复左侧「类型」优先，避免进入信息页时默认落到来源
 *   - 首次加载新基线时会把旧默认 platform 重置为 category；之后用户手动切换继续持久化
 *   - 切换时立即写入 localStorage（§Spec-2.5.3 / .4）
 *   - 重新进入 tab 时读取并恢复（§Spec-2.5.7）
 *   - localStorage 不可用 / 隐私模式 → 写入失败不抛异常，本会话内切换有效（§Spec-2.5.E3）
 *   - 老 localStorage 字段名兼容：v18.0 首次发布无历史 key（§Spec-2.5.E4）
 */
import { useState, useCallback, useEffect } from 'react'
import { ChannelsView } from '../channels/ChannelsView'
import type { InfoGroupBy } from './InfoGroupByToggle'
import { InfoCategoryView } from './InfoCategoryView'
import { InfoSidebar } from './InfoSidebar'
import { useFeedStore } from '../../store/feedStore'

const LS_KEY = 'info_tab_group_by'
const LS_DEFAULT_REV_KEY = 'info_tab_group_by_default_rev'
const DEFAULT_REV = '2026-05-23-category-v1'
const VALID_VALUES: ReadonlyArray<InfoGroupBy> = ['platform', 'category']

/** 从 localStorage 读取 groupBy 偏好；异常态（§2.5.E3）→ 默认 'category' */
function readGroupByFromLocalStorage(): InfoGroupBy {
  try {
    const defaultRev = localStorage.getItem(LS_DEFAULT_REV_KEY)
    if (defaultRev !== DEFAULT_REV) {
      return 'category'
    }
    const raw = localStorage.getItem(LS_KEY)
    if (raw && (VALID_VALUES as ReadonlyArray<string>).includes(raw)) {
      return raw as InfoGroupBy
    }
  } catch {
    // localStorage 不可用 / 隐私模式 / 阻止访问 → 静默 fallback
  }
  return 'category'
}

/** 持久化 groupBy 偏好；异常态（§2.5.E3）→ 静默吞错，不影响 UI 切换 */
function persistGroupByToLocalStorage(value: InfoGroupBy): void {
  try {
    localStorage.setItem(LS_DEFAULT_REV_KEY, DEFAULT_REV)
    localStorage.setItem(LS_KEY, value)
  } catch {
    // QuotaExceededError / SecurityError → 静默吞错
  }
}

/** 标记 2026-05-23 默认视角迁移，避免旧默认 platform 永久覆盖分类优先基线。 */
function persistDefaultRevision(): void {
  try {
    if (localStorage.getItem(LS_DEFAULT_REV_KEY) === DEFAULT_REV) return
    localStorage.setItem(LS_DEFAULT_REV_KEY, DEFAULT_REV)
    localStorage.setItem(LS_KEY, 'category')
  } catch {
    // localStorage 不可用时仅使用本轮内存状态
  }
}

export function InfoView() {
  // 初始读取 localStorage（同步，避免首屏闪一下默认态再切换）
  // §Spec-2.5.2 持久化恢复后立即高亮正确 segment
  const [groupBy, setGroupBy] = useState<InfoGroupBy>(() => readGroupByFromLocalStorage())
  const ensurePlatformSections = useFeedStore((s) => s.ensurePlatformSections)

  // 切走再回来时强制重新读 localStorage（其他 tab 改了偏好的兼容场景）
  useEffect(() => {
    persistDefaultRevision()
    const next = readGroupByFromLocalStorage()
    if (next !== groupBy) {
      setGroupBy(next)
    }
    // 仅 mount 时读一次；运行时切换走 onChange
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleChange = useCallback((next: InfoGroupBy) => {
    setGroupBy(next)
    persistGroupByToLocalStorage(next)
  }, [])

  useEffect(() => {
    if (groupBy !== 'category') return
    const timer = window.setTimeout(() => {
      ensurePlatformSections().catch(() => {
        // Background prewarm only. ChannelsView owns the visible error state.
      })
    }, 700)
    return () => window.clearTimeout(timer)
  }, [ensurePlatformSections, groupBy])

  return (
    <div className="mx-auto max-w-[1360px] px-4 pt-0" data-testid="info-view-shell">
      <InfoSidebar
        groupBy={groupBy}
        onGroupByChange={handleChange}
      />
      <div className="min-w-0 pt-3" data-testid="info-view-content">
        {groupBy === 'platform' ? (
          <ChannelsView embedded />
        ) : (
          <InfoCategoryView embedded />
        )}
      </div>
    </div>
  )
}
