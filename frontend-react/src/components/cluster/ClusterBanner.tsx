/**
 * v15.0 ClusterBanner — item 原文面板顶部插槽 (DESIGN.md §15.11)
 *
 * 文案锁定 `【{cluster.ai_title}】`（中文方括号包裹，无前缀，无 emoji）。
 * 点击 → 当前页打开 ClusterDetailPanel（不跳路由）。
 * cluster_id IS NULL 时整个组件不渲染（feedback_dont_render_empty_placeholder）。
 */
import { useCallback } from 'react'
import { useClusterDetailStore } from '../../store/clusterDetailStore'

interface ClusterBannerProps {
  clusterId: number | null | undefined
  clusterTitle: string | null | undefined
}

export function ClusterBanner({ clusterId, clusterTitle }: ClusterBannerProps) {
  const openModal = useClusterDetailStore((s) => s.openModal)

  const handleClick = useCallback(() => {
    if (clusterId) openModal(clusterId)
  }, [clusterId, openModal])

  if (!clusterId || !clusterTitle) return null

  return (
    <div
      data-testid="cluster-banner"
      role="button"
      tabIndex={0}
      onClick={handleClick}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          handleClick()
        }
      }}
      className="cursor-pointer hover:opacity-90 transition-opacity flex items-center gap-3 px-5 py-2.5 sm:px-5"
      style={{
        background: 'var(--accent)',
      }}
      aria-label={`打开聚合视图: ${clusterTitle}`}
    >
      <span
        className="flex-1 truncate"
        style={{ fontSize: 13, fontWeight: 500, color: 'var(--accent-foreground)' }}
      >
        【{clusterTitle}】
      </span>
      <span
        className="shrink-0"
        style={{ fontSize: 12, color: 'var(--accent-foreground)', textDecoration: 'underline' }}
      >
        查看聚合视图
      </span>
    </div>
  )
}
