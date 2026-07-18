/**
 * ClusterRightPanel — cluster 落地页右栏 (DESIGN.md §21.5, v24.0 报纸内页)
 *
 * AI 摘要(reading-* 阅读层) + stale 提示 + 行动区。
 * v24.0 §21.5-②: 删除 v15 手写行动列表(12px 正文+靛蓝 badge),
 * 复用已达标的 ClusterActionZone(含存量行动列表 + 生成流)。
 */
import { TriangleAlert } from 'lucide-react'
import type { ClusterDetail, ClusterSource, ClusterAction } from '../../lib/types'
import { cn } from '../../lib/utils'
import { ClusterActionZone } from './ClusterActionZone'
import { ClusterSummaryBlock } from './ClusterSummaryBlock'

interface ClusterRightPanelProps {
  cluster: ClusterDetail
  sources: ClusterSource[]
  actions: ClusterAction[]
  className?: string
  showActions?: boolean
}

export function ClusterRightPanel({ cluster, actions, className, showActions = false }: ClusterRightPanelProps) {
  // 老 action stale 检测
  const staleActions = actions.filter((a) => a.cluster_version != null && a.cluster_version < cluster.live_version)

  return (
    <aside className={cn(
      'space-y-6',
      className,
    )}>
      <ClusterSummaryBlock
        summary={cluster.ai_summary}
        keyPoints={cluster.ai_key_points}
        surface="plain"
        mode="detail-page"
      />

      {/* Stale 提示 — v24.0 §21.5-④: emoji ⚠️ + 硬编码琥珀底 → lucide + score 语义 token */}
      {showActions && staleActions.length > 0 && (
        <div
          className="flex items-start gap-2 rounded-[4px] bg-[var(--score-high-bg)] px-3 py-2.5 text-[13px] leading-relaxed text-foreground"
          data-testid="cluster-stale-note"
        >
          <TriangleAlert className="mt-[3px] h-3.5 w-3.5 shrink-0 text-[var(--score-high)]" aria-hidden="true" />
          <span>此事件已有更新，建议重新生成行动点</span>
        </div>
      )}

      {/* 行动区 — 存量列表 + 生成 CTA 统一走 ClusterActionZone */}
      {showActions && <ClusterActionZone clusterId={cluster.id} />}
    </aside>
  )
}
