/**
 * v15.0 ClusterRightPanel — cluster 落地页右栏 (DESIGN.md §15.10)
 *
 * AI 摘要 + 关键要点 + 行动点区。
 */
import type { ClusterDetail, ClusterSource, ClusterAction } from '../../lib/types'
import { navigateToActionCard } from '../../lib/actionNavigation'
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

function actionTypeLabel(type: string) {
  if (type === 'investigate') return '调研验证'
  if (type === 'implement') return '动手做'
  if (type === 'content') return '创作内容'
  if (type === 'track') return '跟踪关注'
  return type
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

      {/* Stale 提示 */}
      {showActions && staleActions.length > 0 && (
        <div
          className="p-2.5 text-[12px] flex items-start gap-2"
          style={{
            background: 'rgba(245, 158, 11, 0.12)',
            borderRadius: 6,
            color: 'var(--foreground)',
          }}
        >
          <span style={{ color: 'var(--pulse-update)' }}>⚠️</span>
          <span>此事件已有更新，建议重新生成行动点</span>
        </div>
      )}

      {/* Actions list */}
      {showActions && actions.length > 0 && (
        <div>
          <div className="text-[13px] font-semibold text-warm-900 mb-2">已生成的行动点</div>
          <ul className="space-y-2">
            {actions.map((a) => {
              const isStale = a.cluster_version != null && a.cluster_version < cluster.live_version
              const prompt = a.prompt?.trim()
              const reason = a.reason?.trim()
              return (
                <li
                  key={a.id}
                  className="text-[12px] p-3 rounded-lg border border-warm-300/80 bg-warm-50/70"
                  style={{ opacity: isStale ? 0.5 : 1 }}
                >
                  <div className="flex items-start gap-2">
                    <span className="shrink-0 rounded bg-accent px-1.5 py-0.5 text-[10px] font-semibold text-primary">
                      {actionTypeLabel(a.action_type)}
                    </span>
                    <div className="min-w-0 flex-1">
                      <button
                        type="button"
                        onClick={() => navigateToActionCard(a.id)}
                        className="block w-full truncate text-left font-medium text-foreground leading-snug hover:text-primary transition-colors"
                      >
                        {a.title}
                      </button>
                      <div className="mt-1 text-muted-foreground">
                        v{a.cluster_version ?? '?'}
                        {isStale && ' · 已过期'}
                      </div>
                    </div>
                  </div>
                  {prompt && (
                    <div className="mt-2">
                      <div className="text-[11px] font-medium text-muted-foreground">行动内容</div>
                      <p className="mt-1 text-[12px] leading-relaxed text-foreground/80">{prompt}</p>
                    </div>
                  )}
                  {reason && (
                    <div className="mt-2">
                      <div className="text-[11px] font-medium text-muted-foreground">生成依据</div>
                      <p className="mt-1 text-[12px] leading-relaxed text-muted-foreground">{reason}</p>
                    </div>
                  )}
                  {!prompt && !reason && (
                    <div className="mt-2 text-[12px] leading-relaxed text-muted-foreground">
                      暂无详细内容，可重新生成一个更完整的行动点。
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        </div>
      )}

      {/* Generate CTA */}
      {showActions && <ClusterActionZone clusterId={cluster.id} showExistingActions={false} />}
    </aside>
  )
}
