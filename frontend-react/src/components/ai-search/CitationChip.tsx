import { useAiSearchStore } from '../../store/aiSearchStore'
import { cssTimeMs, revealCitation } from './aiSearchScroll'

export function CitationChip({ n }: { n: number }) {
  const itemId = useAiSearchStore((state) => state.items[n - 1]?.id)
  const highlight = useAiSearchStore((state) => state.highlight)

  if (!itemId) return <>[{n}]</>

  return (
    <button
      type="button"
      aria-label={`查看引用 ${n}`}
      className="ai-search-chip focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]"
      onClick={event => {
        const chip = event.currentTarget
        const card = document.getElementById(`ai-card-${itemId}`)
        // 先滚，停稳了再脉冲：卡片没到位就闪，等它到位时脉冲已经结束了。
        if (card) revealCitation(card, () => {
          if (!card.isConnected) return
          const active = document.activeElement
          if ((active === chip || active === document.body) && !document.querySelector('[role="dialog"]')) {
            card.focus({ preventScroll: true })
          }
          highlight(itemId, cssTimeMs(card, '--ai-highlight-ms'))
        })
        else highlight(itemId)
      }}
    >
      {n}
    </button>
  )
}
