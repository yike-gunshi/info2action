import { useImperativeHandle, useRef, type Ref } from 'react'
import { useAiSearchStore } from '../../store/aiSearchStore'
import { useScrollbarReveal } from '../../hooks/useScrollbarReveal'
import { AiSearchInput } from './AiSearchInput'
import { describeSummaryOutcome, SummaryMeta, SummaryPane } from './SummaryPane'
import { keepFocusVisible } from './aiSearchScroll'

export function AiSearchResults({ paneRef, onFeedback, inputRef, query }: {
  paneRef?: Ref<HTMLDivElement>
  onFeedback?: (message: string) => void
  inputRef?: Ref<HTMLTextAreaElement>
  query: string
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  useImperativeHandle(paneRef, () => containerRef.current!, [])
  useScrollbarReveal(containerRef)
  const announcement = useAiSearchStore((s) => s.loadingJob ? '' : describeSummaryOutcome(s.summary, s.emptyResult))
  const searchMode = useAiSearchStore((s) => s.phase !== 'submitting' && !s.loadingJob
    && (s.emptyResult || s.summary.status === 'insufficient' || s.summary.status === 'failed' || s.summary.status === 'unavailable'))
  return (
    <div ref={containerRef} className="ai-search-pane" data-ai-scroll="summary" onFocusCapture={keepFocusVisible}>
      <p role="status" aria-label="AI 搜索任务状态" aria-live="polite" aria-atomic="true" className="sr-only">{announcement}</p>
      {/* 完成与等待时标题属于文档；错误和资料不足在同一位置复用空态搜索框。 */}
      {searchMode ? <AiSearchInput inputRef={inputRef} /> : <h1 className="ai-search-qtitle">{query}</h1>}
      {!searchMode ? <SummaryMeta /> : null}
      <SummaryPane onFeedback={onFeedback} />
    </div>
  )
}
