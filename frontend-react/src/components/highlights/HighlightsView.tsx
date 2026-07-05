/**
 * v19 HighlightsView — Image2 约束化「精选」tab。
 *
 * 01-highlights-v2 锁定为开放式编辑流：无外层卡片、自然页面滚动。
 * 精选页保留轻量 L1 分类切换，用于快速收拢模型、评测等阅读场景。
 */
import { LatestEvents } from '../events/LatestEvents'
import { HighlightsFilterTabs } from './HighlightsFilterTabs'

export function HighlightsView() {
  return (
    <div className="mx-auto max-w-[1040px] px-5 pb-5 pt-0 sm:px-6 sm:pb-6 sm:pt-0 xl:px-0" data-testid="highlights-view-shell">
      <HighlightsFilterTabs />
      <LatestEvents variant="page" showEmptyState />
    </div>
  )
}
