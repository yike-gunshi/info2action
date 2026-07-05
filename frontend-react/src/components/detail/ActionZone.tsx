import { useState, useEffect, useRef, useCallback } from 'react'
import { Plus, ArrowRight } from 'lucide-react'
import { cn } from '../../lib/utils'
import { fetchActionsByItem, generateActionFromItem } from '../../lib/api'
import { navigateToActionCard } from '../../lib/actionNavigation'
import { useDetailStore } from '../../store/detailStore'
import { requireAuth } from '../shared/AuthGate'
import { TypewriterLine } from '../shared/TypewriterLine'
import type { SSEEvent } from '../../lib/api'
import type { ActionItem } from '../../lib/types'

interface ActionZoneProps {
  itemId: string
  onActionCountChange?: (count: number) => void
}

const STAGE_LABELS = ['内容分析', '读取上下文', 'AI 评估', '整理结果']
const ACTION_TYPES = [
  { key: 'investigate', label: '调研验证' },
  { key: 'implement', label: '动手做' },
  { key: 'content', label: '创作内容' },
]

type GenState = 'idle' | 'form' | 'generating' | 'done' | 'error'

// TypewriterLine moved to ../shared/TypewriterLine for reuse by cluster action UX.

export function ActionZone({ itemId, onActionCountChange }: ActionZoneProps) {
  const [actions, setActions] = useState<ActionItem[]>([])
  const [loading, setLoading] = useState(true)

  // Generation state
  const [genState, setGenState] = useState<GenState>('idle')
  const [selectedType, setSelectedType] = useState('investigate')
  const [userHint, setUserHint] = useState('')
  const [stages, setStages] = useState([0, 0, 0, 0]) // 0=pending 1=active 2=done
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
    setSelectedType('investigate')
    setUserHint('')
  }

  const handleGenerate = () => {
    if (!requireAuth('生成行动')) return
    setGenState('generating')
    setStages([1, 0, 0, 0])
    setThinkingLines([])
    setVisibleCount(0)
    setErrorMsg('')

    let lastStage = 0

    const controller = generateActionFromItem(
      itemId,
      { actionType: selectedType, userHint: userHint.trim() || undefined },
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
        } else if (evt.type === 'stage') {
          const idx = evt.stage ?? 0
          const status = (evt as unknown as Record<string, unknown>).status as string
          if (status === 'active' && idx !== lastStage) lastStage = idx
          setStages((prev) => {
            const next = [...prev]
            if (status === 'done') {
              next[idx] = 2
              if (idx + 1 < next.length && next[idx + 1] === 0) next[idx + 1] = 1
            } else if (status === 'active') {
              next[idx] = 1
            }
            return next
          })
        } else if (evt.type === 'result') {
          setStages([2, 2, 2, 2])
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
        setStages([2, 2, 2, 2])
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
      {/* Header */}
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-medium text-muted-foreground">行动点</span>
        {genState === 'idle' && (
          <button
            onClick={handleShowForm}
            className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors"
          >
            <Plus className="w-3 h-3" />
            {actions.length > 0 ? '新建' : '生成行动点'}
          </button>
        )}
      </div>

      {/* Pre-generation form */}
      {genState === 'form' && (
        <div className="rounded-lg border border-border bg-card p-3 mb-3">
          <p className="text-sm font-medium text-foreground mb-2">选择行动类型</p>
          <div className="flex gap-2 mb-3">
            {ACTION_TYPES.map((at) => (
              <button
                key={at.key}
                onClick={() => setSelectedType(at.key)}
                className={cn(
                  'text-sm px-3 py-1.5 rounded-md border transition-colors',
                  selectedType === at.key
                    ? 'border-primary bg-accent text-primary font-semibold'
                    : 'border-border text-muted-foreground hover:border-foreground/30',
                )}
              >
                {at.label}
              </button>
            ))}
          </div>
          <textarea
            value={userHint}
            onChange={(e) => setUserHint(e.target.value)}
            placeholder="补充说明（可选）：告诉 AI 你关注的方向..."
            className="w-full text-sm bg-muted border border-input rounded-md px-2.5 py-2 mb-3 resize-none focus:outline-none focus:ring-1 focus:ring-ring"
            rows={2}
          />
          <div className="flex justify-end gap-2">
            <button
              onClick={() => setGenState('idle')}
              className="text-sm text-muted-foreground hover:text-foreground px-3 py-1 rounded-md hover:bg-muted transition-colors"
            >
              取消
            </button>
            <button
              onClick={handleGenerate}
              className="text-sm font-semibold text-primary-foreground bg-primary px-4 py-1.5 rounded-md hover:bg-primary/90 transition-colors"
            >
              开始生成
            </button>
          </div>
        </div>
      )}

      {/* Generating: thinking stream */}
      {genState === 'generating' && (
        <div className="rounded-lg bg-[var(--terminal-bg)] p-3 mb-3">
          <div className="flex items-center gap-2 mb-2">
            <span className="text-xs font-mono text-[var(--terminal-text)] opacity-60">
              {STAGE_LABELS[stages.findIndex((s) => s === 1)] || STAGE_LABELS[3]}
            </span>
            <button
              onClick={handleCancel}
              className="ml-auto text-xs text-warm-500 hover:text-foreground"
            >
              取消
            </button>
          </div>
          {/* Thinking stream */}
          <div
            ref={streamRef}
            className="max-h-[200px] overflow-y-auto scrollbar-hide space-y-0.5"
          >
            {thinkingLines.slice(0, visibleCount).map((line, i) => {
              const isLastLine = i === visibleCount - 1
              return (
                <div
                  key={i}
                  className="text-xs font-mono leading-relaxed text-[var(--terminal-text)]"
                >
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

      {/* Action title links — click to navigate to Actions tab */}
      {actions.length > 0 && (
        <div className="space-y-1">
          {actions.map((action) => (
            <button
              key={action.id}
              onClick={() => navigateToAction(action.id)}
              className="w-full flex items-center gap-2 text-left text-sm text-foreground hover:text-primary py-1.5 px-2 -mx-2 rounded-md hover:bg-muted transition-colors group"
            >
              <ArrowRight className="w-3.5 h-3.5 text-warm-400 group-hover:text-primary shrink-0" />
              <span className="truncate">{action.title}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
