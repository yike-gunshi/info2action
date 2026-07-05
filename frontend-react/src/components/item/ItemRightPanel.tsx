import { ArrowRight } from 'lucide-react'
import { useDetailStore } from '../../store/detailStore'
import { navigateToActionCard } from '../../lib/actionNavigation'
import { cn } from '../../lib/utils'
import { renderMarkdownLite, renderMarkdownInline } from '../../lib/markdown-lite'
import type { FeedItem, ActionItem } from '../../lib/types'

/** item AI 产出与互动入口面板。桌面 sticky(top-72px)/移动端 static。
 *  分区(gap-4):AI 速览 / 行动建议 / 互动数据 / 反馈+分享;三数据分区全空 → 降级文案。 */
export interface ItemRightPanelProps {
  item: FeedItem
  /** 可选:由父级传入;不传则从 detailStore.itemActions 取 */
  actions?: ActionItem[]
}

export function ItemRightPanel({ item, actions: actionsProp }: ItemRightPanelProps) {
  const storeActions = useDetailStore((s) => s.itemActions)
  const actions = actionsProp ?? storeActions

  const hasSummary = !!item.ai_summary || (item.ai_key_points && item.ai_key_points.length > 0)
  const hasActions = actions.length > 0
  const allEmpty = !hasSummary && !hasActions

  return (
    <aside
      className={cn(
        'ai-summary-signal rounded-[8px] p-6',
        'lg:sticky lg:top-[72px] lg:max-h-[calc(100vh-96px)] lg:overflow-y-auto',
        'flex flex-col gap-4',
      )}
      aria-label="AI 产出与互动"
    >
      {hasSummary && (
        <section className="relative">
          {item.ai_summary && (
            <div className="text-[16px] text-foreground leading-[1.7]">
              <div className="text-primary font-semibold mb-2">✦ AI 速览</div>
              <SummaryParagraphs text={item.ai_summary.replace(/^【精华速览】/, '').trim()} />
            </div>
          )}
          {item.ai_key_points && item.ai_key_points.length > 0 && (
            <ul className={cn('space-y-2', item.ai_summary && 'mt-3')}>
              {item.ai_key_points.map((point, i) => (
                <KeyPoint key={i} point={point} isFirst={i === 0} />
              ))}
            </ul>
          )}
        </section>
      )}

      {hasActions && (
        <section className="relative border-t border-border/70 pt-4">
          <h3 className="text-xs uppercase tracking-wide text-muted-foreground mb-2">行动建议</h3>
          <ul className="space-y-1">
            {actions.map((action) => (
              <li key={action.id}>
                <button
                  onClick={() => navigateToActionCard(String(action.id))}
                  className="w-full flex items-center gap-2 text-left text-sm text-foreground hover:text-primary py-1.5 px-2 -mx-2 rounded-md hover:bg-muted transition-colors group"
                >
                  <ArrowRight className="w-3.5 h-3.5 text-warm-400 group-hover:text-primary shrink-0" />
                  <span className="truncate">{action.title}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* BF-0420-7: 反馈/分享按钮整段阉割(用户明确要删) */}
      {/* BF-0420-8: 互动数据整段移至 ItemLeftPanel 的 Meta 块,右列只聚焦 AI 产出 */}
      {allEmpty && (
        <p className="relative text-sm text-muted-foreground leading-[1.7]">该内容尚未生成 AI 总结</p>
      )}
    </aside>
  )
}

/** 单个 ai_key_point 渲染(string 或 {title, points[]}) */
/** BF-0420-10: 列表项 bullet 放大可见 + 内容走 markdown-lite inline(**X** 变粗) + section 间距加大
 * 注:本项目 Tailwind 的 bg-primary/X slash-opacity modifier 失效(背景实际 rgba(0,0,0,0)),
 * 用 inline style + CSS var(--primary) 直接赋色。 */
function KeyPoint({ point, isFirst }: { point: string | { title: string; points: string[] }; isFirst?: boolean }) {
  if (typeof point === 'string') {
    return (
      <li className="text-[16px] text-foreground leading-[1.7] flex gap-2.5">
        <span
          className="mt-[9px] w-1.5 h-1.5 rounded-full flex-shrink-0"
          style={{ backgroundColor: 'var(--primary)' }}
        />
        <span className="flex-1">{renderMarkdownInline(point)}</span>
      </li>
    )
  }
  return (
    <li className={cn('text-[16px] leading-[1.7]', isFirst ? 'mt-0' : 'mt-4')}>
      <div className="font-semibold text-foreground mb-2">{renderMarkdownInline(point.title)}</div>
      {point.points && point.points.length > 0 && (
        <ul className="space-y-1.5">
          {point.points.map((sub, j) => (
            <li key={j} className="flex gap-2.5 text-[16px] text-foreground leading-[1.7]">
              <span
                className="mt-[9px] w-1.5 h-1.5 rounded-full flex-shrink-0"
                style={{ backgroundColor: 'var(--primary)', opacity: 0.7 }}
              />
              <span className="flex-1">{renderMarkdownInline(sub)}</span>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

// BF-0420-7: GhostButton / handleFeedback / handleShare 已删除(用户明确阉割)

/** BF-0420-3: 用 markdown-lite 渲染 AI 速览(bold/italic/code/list 内联) */
function SummaryParagraphs({ text }: { text: string }) {
  const nodes = renderMarkdownLite(text)
  if (nodes.length === 0) return null
  return <div className="space-y-2">{nodes}</div>
}

// BF-0420-8: collectMetrics / METRIC_META 已移至 ItemLeftPanel(互动数据归 Meta 块)
