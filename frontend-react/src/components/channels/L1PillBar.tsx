import { useMemo } from 'react'
import { InfoSectionPillBar } from '../shared/InfoSectionPillBar'

// BF-0512-6: 「未分类」L1 pill 占位符（与后端 db.py UNCATEGORIZED_SENTINEL 对齐）
// NULL ai_categories 历史 item 在「全部」可见但不归属任何 L1，归到此 pill 末尾
export const UNCATEGORIZED_SENTINEL = '__uncategorized__'
export const UNCATEGORIZED_LABEL = '未分类'

/** v16.0 W4.T10: L1 分类 pill bar.
 *
 *  ChannelsView 中以 L1 维度过滤的 section (GitHub / Reddit / RSS / HackerNews / WayToAGI / Manual)
 *  使用本组件代替 source pill bar。第一个 pill 永远是「全部」(对应 selectedCategory === null),
 *  后续 pill 按 categoryCounts 数量降序排列,空类自动隐藏。
 *
 *  视觉与 ChannelsView source pill 完全一致(同样 className),保证两阵营 pill 切换无视觉跳跃。
 */
export interface L1PillBarProps {
  /** 当前 section 的 platform id, 仅用于 key/data attr (本组件不依赖) */
  platform: string
  /** DOM section key；默认等于 platform，对应 ChannelsView 的 `s-${platform}`。 */
  sectionKey?: string
  /** {l1_id: count} from feedStore.platformCategoryCounts[platform] */
  categoryCounts: Record<string, number>
  /** L1 id → 显示名 map (来自 classification.categories[].id/name) */
  categoryLabels: Record<string, string>
  /**
   * BF-0512-5: L1 显示顺序数组 (来自 classification.categories.map(c => c.id))
   * 与推荐页 L1 顺序保持一致；不在 order 中的 L1 fallback 到末尾按 cnt DESC 排
   * 缺省时（空数组）回退到 cnt DESC 旧行为，保证向后兼容
   */
  categoryOrder?: string[]
  /** 当前选中的 L1, null = 「全部」 */
  selectedCategory: string | null
  /** 切换 pill 回调, 传 null 表示选中「全部」 */
  onSelect: (category: string | null) => void
}

export function L1PillBar({
  platform,
  sectionKey = platform,
  categoryCounts,
  categoryLabels,
  categoryOrder,
  selectedCategory,
  onSelect,
}: L1PillBarProps) {
  // BF-0512-5: 排序按 categoryOrder（推荐页 L1 顺序）；缺省回退到 cnt DESC
  // 用户决策：所有遵守 L1 分类的频道 pill 排序应跟推荐页一致，避免心智跳跃
  // BF-0512-6: 「未分类」pill 强制排在最末（不参与 categoryOrder）
  const sortedCategories = useMemo(() => {
    const nonEmpty = Object.entries(categoryCounts).filter(([, count]) => count > 0)
    // BF-0512-6: 拆出 「未分类」单独处理，保证它永远在最末
    const uncategorized = nonEmpty.filter(([id]) => id === UNCATEGORIZED_SENTINEL)
    const regular = nonEmpty.filter(([id]) => id !== UNCATEGORIZED_SENTINEL)

    let sortedRegular: typeof regular
    if (!categoryOrder || categoryOrder.length === 0) {
      // 兜底：无 order → cnt DESC（向后兼容旧调用 / classification 未加载场景）
      sortedRegular = regular.sort((a, b) => b[1] - a[1])
    } else {
      // 按 categoryOrder 索引排；不在 order 里的 L1 放最后按 cnt DESC
      const orderIndex = new Map(categoryOrder.map((id, i) => [id, i]))
      sortedRegular = regular.sort((a, b) => {
        const ia = orderIndex.has(a[0]) ? orderIndex.get(a[0])! : Infinity
        const ib = orderIndex.has(b[0]) ? orderIndex.get(b[0])! : Infinity
        if (ia !== ib) return ia - ib
        return b[1] - a[1]  // tie-breaker: 数量大的在前
      })
    }
    return [...sortedRegular, ...uncategorized]
  }, [categoryCounts, categoryOrder])

  // 只有「全部」一个 pill 时不渲染(没有过滤选择)
  if (sortedCategories.length === 0) return null

  return (
    <InfoSectionPillBar
      sectionKey={sectionKey}
      items={[
        { key: null, label: '全部' },
        ...sortedCategories.map(([id, count]) => {
          const isUncategorized = id === UNCATEGORIZED_SENTINEL
          const label = isUncategorized
            ? UNCATEGORIZED_LABEL
            : (categoryLabels[id] || id)
          return {
            key: id,
            label,
            title: isUncategorized
              ? `${count} 条历史 item 还没跑 AI 分类（含 v4.0+ 之前 / enrich 失败的）。可在「全部」pill 看到。`
              : `${count} 条`,
          }
        }),
      ]}
      activeKey={selectedCategory}
      onSelect={onSelect}
      data-testid={`info-section-pill-bar-${platform}`}
    />
  )
}
