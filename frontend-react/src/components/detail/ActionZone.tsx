import { useState, useEffect, useRef, useCallback } from 'react'
import { Plus, ArrowRight } from 'lucide-react'
import { fetchActionsByItem, generateActionFromItem } from '../../lib/api'
import { navigateToActionCard } from '../../lib/actionNavigation'
import { useDetailStore } from '../../store/detailStore'
import { requireAuth } from '../shared/AuthGate'
import { TypewriterLine } from '../shared/TypewriterLine'
import { ActionGenHint } from '../shared/ActionGenHint'
import type { SSEEvent } from '../../lib/api'
import type { ActionItem } from '../../lib/types'

interface ActionZoneProps {
  itemId: string
  onActionCountChange?: (count: number) => void
}

type GenState = 'idle' | 'form' | 'generating' | 'done' | 'error'

// TypewriterLine moved to ../shared/TypewriterLine for reuse by cluster action UX.

export function ActionZone({ itemId, onActionCountChange }: ActionZoneProps) {
  const [actions, setActions] = useState<ActionItem[]>([])
  const [loading, setLoading] = useState(true)

  // Generation state
  const [genState, setGenState] = useState<GenState>('idle')
  const [userHint, setUserHint] = useState('')
  const [thinkingLines, setThinkingLines] = useState<Array<{ text: string; ai?: boolean; stage?: number; divider?: boolean }>>([])
  const [visibleCount, setVisibleCount] = useState(0)
  const [errorMsg, setErrorMsg] = useState('')
  const abortRef = useRef<AbortController | null>(null)
  const streamRef = useRef<HTMLDivElement>(null)
  // Track genState in a ref so SSE callbacks always see the latest value
  const genStateRef = useRef<GenState>('idle')
  genStateRef.current = genState

  // Advance to the next line: called when the current TypewriterLine finishes,
  // or immediately for divider lines (they don't animate).
  const advanceLine = useCallback(() => {
    setVisibleCount((c) => {
      const next = c + 1
      // Auto-advance past divider lines (they render instantly)
      const nextLine = thinkingLines[next]
      if (nextLine?.divider) {
        // Use setTimeout to advance past divider on next tick
        setTimeout(() => setVisibleCount((cc) => cc + 1), 60)
      }
      return next
    })
  }, [thinkingLines])

  // When new lines arrive and nothing is visible yet, start revealing
  useEffect(() => {
    if (thinkingLines.length > 0 && visibleCount === 0) {
      setVisibleCount(1)
    }
  }, [thinkingLines.length, visibleCount])

  const loadActions = useCallback(() => {
    fetchActionsByItem(itemId)
      .then((res) => {
        const list = (res.actions || []).map((a: ActionItem) => ({
          ...a,
          steps: typeof a.steps === 'string' ? JSON.parse(a.steps) : a.steps,
        }))
        setActions(list)
        onActionCountChange?.(list.length)
        // Sync to detailStore so the "行动点" section in DetailPanel updates
        useDetailStore.getState().setItemActions(list)
      })
      .catch(() => setActions([]))
      .finally(() => setLoading(false))
  }, [itemId, onActionCountChange])

  useEffect(() => {
    setLoading(true)
    loadActions()
  }, [loadActions])

  // Auto-scroll thinking stream — track both new lines and visible count
  const userScrolledUp = useRef(false)
  useEffect(() => {
    if (!streamRef.current) return
    const el = streamRef.current
    // If user hasn't manually scrolled up, keep at bottom
    if (!userScrolledUp.current) {
      el.scrollTop = el.scrollHeight
    }
  }, [thinkingLines, visibleCount])

  // Detect manual scroll-up so we don't fight the user
  useEffect(() => {
    const el = streamRef.current
    if (!el) return
    const handleScroll = () => {
      const nearBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 40
      userScrolledUp.current = !nearBottom
    }
    el.addEventListener('scroll', handleScroll, { passive: true })
    return () => el.removeEventListener('scroll', handleScroll)
  }, [genState])

  const handleShowForm = () => {
    setGenState('form')
    setUserHint('')
  }

  const handleGenerate = () => {
    if (!requireAuth('生成行动')) return
    setGenState('generating')
    setThinkingLines([])
    setVisibleCount(0)
    setErrorMsg('')

    let lastStage = 0

    const controller = generateActionFromItem(
      itemId,
      { userHint: userHint.trim() || undefined },
      // onEvent
      (evt: SSEEvent) => {
        if (evt.type === 'thinking' || evt.type === 'thinking-ai') {
          const stageIdx = evt.stage ?? 0
          setThinkingLines((prev) => {
            const lines = [...prev]
            if (stageIdx !== lastStage) lastStage = stageIdx
            lines.push({ text: evt.text || evt.data, ai: evt.type === 'thinking-ai', stage: stageIdx })
            return lines
          })
        } else if (evt.type === 'result') {
          const hasAction = evt.action != null
          if (!hasAction) {
            // AI decided not to generate — show reason to user
            const reason = (evt as unknown as Record<string, unknown>).reason as string || '评分未达阈值，未生成行动点'
            setErrorMsg(reason)
            setGenState('error')
          } else {
            // Action created — reload list
            setTimeout(() => {
              fetchActionsByItem(itemId)
                .then((res) => {
                  const list = (res.actions || []).map((a: ActionItem) => ({
                    ...a,
                    steps: typeof a.steps === 'string' ? JSON.parse(a.steps) : a.steps,
                  }))
                  setActions(list)
                  onActionCountChange?.(list.length)
                  useDetailStore.getState().setItemActions(list)
                })
                .finally(() => setGenState('idle'))
            }, 500)
          }
          abortRef.current?.abort()
        } else if (evt.type === 'error') {
          setErrorMsg(evt.message || '生成失败')
          setGenState('error')
        }
      },
      // onDone — flush
      () => {
        if (genStateRef.current !== 'error' && genStateRef.current !== 'idle') {
          setTimeout(() => {
            fetchActionsByItem(itemId)
              .then((res) => {
                const list = (res.actions || []).map((a: ActionItem) => ({
                  ...a,
                  steps: typeof a.steps === 'string' ? JSON.parse(a.steps) : a.steps,
                }))
                setActions(list)
                onActionCountChange?.(list.length)
                useDetailStore.getState().setItemActions(list)
              })
              .finally(() => setGenState('idle'))
          }, 500)
        }
      },
      // onError
      (err) => {
        setErrorMsg(err.message)
        setGenState('error')
      },
    )

    abortRef.current = controller
  }

  const handleCancel = () => {
    abortRef.current?.abort()
    setGenState('idle')
  }

  const navigateToAction = useCallback((actionId: string) => {
    navigateToActionCard(actionId)
  }, [])

  if (loading) {
    return (
      <div className="mt-2 animate-zone-in">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-sm font-medium text-muted-foreground">行动点</span>
        </div>
        <div className="space-y-2">
          {[1, 2].map((i) => (
            <div key={i} className="h-8 rounded bg-muted animate-skeleton" />
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="mt-2 animate-zone-in">
      {/* Header — 品牌色小标题 + 醒目 CTA(v21.0 redesign) */}
      <div className="flex items-center justify-between mb-3">
        <h3 className="reading-section leading-none text-[var(--brand)]">行动点</h3>
        {genState === 'idle' && (
          <button
            onClick={handleShowForm}
            className="inline-flex items-center gap-1.5 rounded-[8px] border border-[var(--brand-border)] px-3.5 py-2 text-[13px] font-semibold text-[var(--brand)] transition-colors hover:bg-[var(--brand-soft)]"
          >
            <Plus className="w-3.5 h-3.5" />
            {actions.length > 0 ? '新建行动点' : '生成行动点'}
          </button>
        )}
      </div>

      {/* Pre-generation form — 纸面弹窗风格 */}
      {genState === 'form' && (
        <div className="rounded-[10px] border border-[var(--modal-border-soft)] bg-[var(--modal-surface-soft)] p-3.5 mb-3">
          <ActionGenHint />
          <p className="font-event-title text-[14px] font-semibold text-[var(--modal-text)] mb-1">补充说明（可选）</p>
          <p className="font-event-title text-[13px] text-[var(--modal-text-muted)] mb-2.5">AI 会自动判定行动类型;想指定方向就写一句,例如"帮我做个原型 / 我想写篇文章"。</p>
          <textarea
            value={userHint}
            onChange={(e) => setUserHint(e.target.value)}
            placeholder="告诉 AI 你关注的方向或想要的产出..."
            className="w-full font-event-title text-[14px] bg-[var(--modal-surface)] border border-[var(--modal-border-soft)] rounded-[8px] px-3 py-2.5 mb-3 resize-none text-[var(--modal-text)] placeholder:text-[var(--modal-text-faint)] focus:outline-none focus:ring-1 focus:ring-[var(--brand-border)]"
            rows={2}
          />
          <div className="flex justify-end gap-2">
            <button
              onClick={() => setGenState('idle')}
              className="text-[13px] text-[var(--modal-text-muted)] hover:text-[var(--modal-text)] px-3 py-1.5 rounded-[7px] hover:bg-[var(--modal-hover)] transition-colors"
            >
              取消
            </button>
            <button
              onClick={handleGenerate}
              className="inline-flex items-center gap-1.5 rounded-[8px] border border-[var(--brand-border)] px-4 py-2 text-[13px] font-semibold text-[var(--brand)] transition-colors hover:bg-[var(--brand-soft)]"
            >
              开始生成
            </button>
          </div>
        </div>
      )}

      {/* Generating: 暖色分析面板(v2 §13.2)—— 去阶段标签,人话标题 */}
      {genState === 'generating' && (
        <div className="rounded-[10px] border border-[var(--modal-border-soft)] bg-[var(--modal-surface)] p-3.5 mb-3">
          <div className="flex items-center gap-2 mb-2.5">
            <span className="inline-flex items-center gap-2 text-[13px] font-semibold text-[var(--brand)]">
              正在分析这条信息
              <span className="inline-flex gap-1" aria-hidden="true">
                <span className="analyze-dot" /><span className="analyze-dot" /><span className="analyze-dot" />
              </span>
            </span>
            <button
              onClick={handleCancel}
              className="ml-auto reading-caption text-[var(--modal-text-muted)] hover:text-[var(--modal-text)]"
            >
              取消
            </button>
          </div>
          <div
            ref={streamRef}
            className="max-h-[200px] overflow-y-auto scrollbar-hide space-y-0.5 rounded-[8px] bg-[var(--action-code-bg)] px-3.5 py-3 font-mono text-[12px] leading-relaxed text-[var(--action-code-faint)]"
          >
            {thinkingLines.slice(0, visibleCount).map((line, i) => {
              const isLastLine = i === visibleCount - 1
              return (
                <div key={i} className={line.ai ? 'text-[var(--action-code-text)]' : 'text-[var(--action-code-faint)]'}>
                  <TypewriterLine
                    text={line.text || ''}
                    speed={25}
                    isLast={isLastLine}
                    onComplete={isLastLine ? advanceLine : undefined}
                    flush={(genState as GenState) === 'done' || (genState as GenState) === 'idle' || (genState as GenState) === 'error'}
                  />
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Error state */}
      {genState === 'error' && (
        <div className="rounded-lg border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950 p-3 mb-3">
          <p className="text-sm text-red-600 dark:text-red-400">{errorMsg}</p>
          <button
            onClick={() => setGenState('idle')}
            className="text-sm text-primary hover:underline mt-2"
          >
            关闭
          </button>
        </div>
      )}

      {/* v2 §14.3(T7): 已生成行动点原位展示标题 + steps,点击进行动详情 */}
      {actions.length > 0 && (
        <div className="space-y-1.5">
          {actions.map((action) => {
            const steps = Array.isArray(action.steps) ? action.steps.filter(Boolean) : []
            return (
              <button
                key={action.id}
                onClick={() => navigateToAction(action.id)}
                className="w-full text-left rounded-[8px] border border-[var(--modal-border-soft)] bg-[var(--modal-surface-soft)] py-2.5 px-3 hover:border-[var(--brand-border)] hover:bg-[var(--modal-hover-soft)] transition-colors group"
              >
                <div className="flex items-center gap-2">
                  <ArrowRight className="w-3.5 h-3.5 text-[var(--modal-text-faint)] group-hover:text-[var(--brand)] shrink-0" />
                  <span className="min-w-0 flex-1 truncate font-event-title text-[14px] font-semibold text-[var(--modal-text)] group-hover:text-[var(--brand)]">{action.title}</span>
                </div>
                {steps.length > 0 && (
                  <ul className="mt-1.5 ml-[22px] space-y-1">
                    {steps.slice(0, 3).map((step, i) => (
                      <li key={i} className="flex min-w-0 items-start gap-2 font-event-title text-[13px] leading-relaxed text-[var(--modal-text-muted)]">
                        <span aria-hidden="true" className="mt-[0.6em] h-1 w-1 shrink-0 rounded-full bg-[var(--brand)] opacity-70" />
                        <span className="min-w-0 line-clamp-1">{step}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
