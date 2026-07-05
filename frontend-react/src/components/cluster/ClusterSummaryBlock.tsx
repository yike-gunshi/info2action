/**
 * v15.1 ClusterSummaryBlock — 事件 summary 双段视觉
 *
 * Stage 4 prompt 输出 summary = 【精华速览】+【全文拆解】两段
 * （V2.3 §7 / feature-spec R6.1）。本组件：
 * 1. 先解析双段
 * 2. 双段都有 → 渲染两个独立区块（速览段 + 全文拆解段）
 * 3. 只有一段或无 markers → 兼容性退化为 v15.0 平铺 markdown 渲染（R6.2）
 *
 * 视觉上复用单 doc 详情页（DetailPanel / ItemRightPanel）的 ✦ AI 速览 +
 * 圆点 key_points 排版语言；这里在视觉细节上保持一致以让用户在 cluster
 * 弹窗里有"和单 doc 一样"的体验（V2 决策 #9）。
 *
 * Fast Refresh 硬约束（feedback_react_fast_refresh_no_mixed_export）:
 * 本文件只导出 ClusterSummaryBlock 一个组件。解析函数在
 * lib/cluster-summary-parser.ts，KeyPoint 是文件内部组件不 export。
 */
import { renderMarkdownInline, renderMarkdownLite } from '../../lib/markdown-lite'
import { parseClusterSummary } from '../../lib/cluster-summary-parser'
import { cn } from '../../lib/utils'

type KeyPointItem = string | { title: string; points: string[] }

interface ClusterSummaryBlockProps {
  summary?: string | null
  keyPoints?: KeyPointItem[] | null
  className?: string
  surface?: 'card' | 'plain'
  mode?: 'modal' | 'detail-page'
}

function KeyPoint({ point }: { point: string }) {
  return (
    <li className="text-[16px] text-foreground leading-[1.7] flex gap-2.5">
      <span
        className="mt-[9px] w-1.5 h-1.5 rounded-full flex-shrink-0"
        style={{ backgroundColor: 'var(--primary)' }}
        aria-hidden="true"
      />
      <span className="flex-1">{renderMarkdownInline(point)}</span>
    </li>
  )
}

function KeyPointGroup({ group }: { group: { title: string; points: string[] } }) {
  return (
    <li className="space-y-2">
      <div className="text-[16px] text-foreground leading-[1.7] font-semibold">
        {renderMarkdownInline(group.title)}
      </div>
      <ul className="space-y-2">
        {group.points.map((point, idx) => (
          <KeyPoint key={idx} point={point} />
        ))}
      </ul>
    </li>
  )
}

export function ClusterSummaryBlock({
  summary,
  keyPoints,
  className,
  surface = 'card',
  mode = 'modal',
}: ClusterSummaryBlockProps) {
  const hasSummary = !!summary
  const hasPoints = !!keyPoints?.length
  if (!hasSummary && !hasPoints) return null

  const parts = parseClusterSummary(summary)
  const isDetailPage = mode === 'detail-page'

  return (
    <section
      className={cn(
        surface === 'card'
          ? 'ai-summary-signal rounded-[8px] p-4'
          : 'relative',
        className,
      )}
      data-testid="cluster-summary-block"
    >
      {parts.hasDualSections ? (
        <>
          {/* 双段视觉 — 精华速览 */}
          <div
            className={cn(
              isDetailPage ? 'reading-body' : 'text-[16px] text-foreground leading-[1.7]',
            )}
            data-testid="cluster-speed-review"
          >
            {isDetailPage ? (
              <h2 className="reading-section mb-3 leading-none text-[var(--brand)]">
                精华速览
              </h2>
            ) : (
              <span className="text-primary font-semibold mr-1">✦ AI 速览</span>
            )}
            <div className={cn('mt-1 space-y-2', isDetailPage && 'space-y-3')}>
              {renderMarkdownLite(parts.speedReview || '')}
            </div>
          </div>
          {/* 双段视觉 — 全文拆解
              v5b: 删除 "✦ 全文拆解" 标题，prompt 内已含加粗小节标题，保留 border-t 分割线 */}
          <div
            className={cn(
              isDetailPage
                ? 'reading-bullet mt-6 border-t border-border pt-5'
                : 'mt-4 pt-3 border-t border-primary/10 text-[16px] text-foreground leading-[1.7]',
            )}
            data-testid="cluster-full-breakdown"
          >
            {isDetailPage && (
              <h2 className="reading-section mb-3 leading-none text-[var(--brand)]">
                全文拆解
              </h2>
            )}
            <div className={cn('space-y-2', isDetailPage && 'space-y-3')}>
              {renderMarkdownLite(parts.fullBreakdown || '')}
            </div>
          </div>
        </>
      ) : (
        // 兼容性:单段或缺 markers → v15.0 平铺降级(R6.2)
        // BF-0428-2: 单段降级路径不再独立标 ✦ AI 速览 + 全文拆解,
        // 全部内容平铺即可,与单 doc DetailPanel 行为对齐
        hasSummary && (
          <div
            className={cn(
              isDetailPage ? 'reading-body' : 'text-[16px] text-foreground leading-[1.7]',
            )}
            data-testid="cluster-summary-flat"
          >
            {isDetailPage ? (
              <h2 className="reading-section mb-3 leading-none text-[var(--brand)]">
                精华速览
              </h2>
            ) : (
              <span className="text-primary font-semibold mr-1">✦ AI 速览</span>
            )}
            <div className={cn('mt-1 space-y-2', isDetailPage && 'space-y-3')}>
              {renderMarkdownLite(parts.speedReview || parts.fullBreakdown || summary || '')}
            </div>
          </div>
        )
      )}

      {/* BF-0428-2: cluster 弹窗下不再单独渲染 keyPoints 紫点列表(避免与
          【全文拆解】内容重复 + 视觉粘连"上详下简"问题)。
          只在没有 summary 时(纯 keyPoints 兜底)才渲染,这是历史 V14.0 单 doc
          fallback 路径,cluster 实际不会走到这里 */}
      {hasPoints && !hasSummary && (
        <ul className="space-y-2" data-testid="cluster-key-points">
          {keyPoints!.map((point, idx) => (
            typeof point === 'string'
              ? <KeyPoint key={idx} point={point} />
              : <KeyPointGroup key={idx} group={point} />
          ))}
        </ul>
      )}
    </section>
  )
}
