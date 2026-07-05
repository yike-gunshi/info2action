/**
 * v15.0 ClusterActionZone — cluster 弹窗 / 落地页 内的「生成行动点」体验。
 *
 * BF-0424-CLUSTER-SSE: 复用 v10.1 ActionZone (DetailPanel) 同款打字机 + 4-stage UX,
 * 但 source = cluster 而非 doc item。事件流由 useClusterDetailStore.startGenerate 驱动:
 *   thinking → stage(active) → thinking-ai → stage(done) → result → done
 *
 * Fast Refresh 硬约束：本文件只导出 ClusterActionZone (其他 helper local-only).
 */
import { useEffect, useRef, useState, useCallback } from 'react'
import { Plus, ArrowRight } from 'lucide-react'
import { cn } from '../../lib/utils'
import { navigateToActionCard } from '../../lib/actionNavigation'
import { useClusterDetailStore } from '../../store/clusterDetailStore'
import { useDetailStore } from '../../store/detailStore'
import { requireAuth } from '../shared/AuthGate'
import { TypewriterLine } from '../shared/TypewriterLine'

const STAGE_LABELS = ['内容分析', '组装事件上下文', 'AI 综合', '整理结果']
const ACTION_TYPES = [
  { key: 'investigate', label: '调研验证' },
  { key: 'implement', label: '动手做' },
  { key: 'content', label: '创作内容' },
]

function actionTypeLabel(type: string) {
  return ACTION_TYPES.find((item) => item.key === type)?.label || type
}

function promptPreview(prompt?: string) {
  const text = (prompt || '').trim()
  if (text.length <= 260) return text
  return `${text.slice(0, 260).trim()}...`
}

type GenState = 'idle' | 'form' | 'generating' | 'done' | 'error'

interface ClusterActionZoneProps {
  clusterId: number
  showExistingActions?: boolean
}

export function ClusterActionZone({ clusterId, showExistingActions = true }: ClusterActionZoneProps) {
  const startGenerate = useClusterDetailStore((s) => s.startGenerate)
  const cancelGenerate = useClusterDetailStore((s) => s.cancelGenerate)
  const generating = useClusterDetailStore((s) => s.generating)
  const stages = useClusterDetailStore((s) => s.generateStages)
  const thinkingLines = useClusterDetailStore((s) => s.generateThinkingLines)
  const generatedAction = useClusterDetailStore((s) => s.generateAction)
  const generateError = useClusterDetailStore((s) => s.generateError)
  const actions = useClusterDetailStore((s) => s.actions)
  const loadActions = useClusterDetailStore((s) => s.loadActions)
  const resetGenerate = useClusterDetailStore((s) => s.resetGenerate)

  const [genState, setGenState] = useState<GenState>('idle')
  const [userHint, setUserHint] = useState('')
  const [selectedType, setSelectedType] = useState('investigate')
  const [visibleCount, setVisibleCount] = useState(0)
  const streamRef = useRef<HTMLDivElement>(null)

  // Sync local UI state from store transitions
  useEffect(() => {
    if (generating) {
      setGenState('generating')
    } else if (generateError) {
      setGenState('error')
    } else if (generatedAction) {
      setGenState('done')
    }
  }, [generating, generateError, generatedAction])

  // Initial load of existing actions for this cluster
  useEffect(() => {
    loadActions(clusterId)
  }, [clusterId, loadActions])

  // Reveal lines progressively (matches ActionZone)
  useEffect(() => {
    if (thinkingLines.length > 0 && visibleCount === 0) {
      setVisibleCount(1)
    }
  }, [thinkingLines.length, visibleCount])

  const advanceLine = useCallback(() => {
    setVisibleCount((c) => c + 1)
  }, [])

  // Auto-scroll thinking stream
  const userScrolledUp = useRef(false)
  useEffect(() => {
    if (!streamRef.current) return
    const el = streamRef.current
    if (!userScrolledUp.current) {
      el.scrollTop = el.scrollHeight
    }
  }, [thinkingLines, visibleCount])

  const handleShowForm = () => {
    if (!requireAuth('生成行动')) return
    resetGenerate()
    setGenState('form')
    setSelectedType('investigate')
    setUserHint('')
    setVisibleCount(0)
  }

  const handleGenerate = () => {
    if (!requireAuth('生成行动')) return
    resetGenerate()
    setVisibleCount(0)
    startGenerate(clusterId, userHint.trim() || undefined, selectedType)
  }

  const handleCancel = () => {
    cancelGenerate()
    setGenState('idle')
  }

  const handleClose = () => {
    resetGenerate()
    setGenState('idle')
    setVisibleCount(0)
  }

  const openActionDetail = useCallback((actionId: string) => {
    useDetailStore.getState().openAction(actionId)
  }, [])

  const lastStageLabel = (() => {
    const idx = stages.findIndex((s) => s === 1)
    return idx >= 0 ? STAGE_LABELS[idx] : STAGE_LABELS[3]
  })()

  return (
    <div className="w-full mt-2">
      {/* Header */}
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-medium text-muted-foreground">行动点</span>
        {genState === 'idle' && !generatedAction && (
          <button
            type="button"
            onClick={handleShowForm}
            className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors"
            data-testid="cluster-action-trigger"
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
                type="button"
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
            placeholder="补充说明（可选）：告诉 AI 你关注的角度..."
            className="w-full text-sm bg-muted border border-input rounded-md px-2.5 py-2 mb-3 resize-none focus:outline-none focus:ring-1 focus:ring-ring"
            rows={2}
          />
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setGenState('idle')}
              className="text-sm text-muted-foreground hover:text-foreground px-3 py-1 rounded-md hover:bg-muted transition-colors"
            >
              取消
            </button>
            <button
              type="button"
              onClick={handleGenerate}
              data-testid="cluster-action-start"
              className="text-sm font-semibold text-primary-foreground bg-primary px-4 py-1.5 rounded-md hover:bg-primary/90 transition-colors"
            >
              开始生成
            </button>
          </div>
        </div>
      )}

      {/* Generating: thinking stream + 4 stage labels */}
      {(genState === 'generating' || (genState === 'done' && thinkingLines.length > 0)) && (
        <div
          className="rounded-lg p-3 mb-3"
          style={{ background: 'var(--terminal-bg, #1f1f1f)' }}
          data-testid="cluster-action-stream"
        >
          <div className="flex items-center gap-2 mb-2">
            <span
              className="text-xs font-mono opacity-60"
              style={{ color: 'var(--terminal-text, #d4d4d4)' }}
            >
              {lastStageLabel}
            </span>
            {/* stage bar */}
            <div className="flex gap-1 ml-2">
              {STAGE_LABELS.map((label, idx) => (
                <span
                  key={label}
                  title={label}
                  data-testid={`cluster-stage-${idx}-${stages[idx] === 2 ? 'done' : stages[idx] === 1 ? 'active' : 'pending'}`}
                  className={cn(
                    'inline-block rounded-full',
                    stages[idx] === 2
                      ? 'bg-green-500'
                      : stages[idx] === 1
                        ? 'bg-yellow-400 animate-pulse'
                        : 'bg-gray-500',
                  )}
                  style={{ width: 6, height: 6 }}
                />
              ))}
            </div>
            {generating && (
              <button
                type="button"
                onClick={handleCancel}
                className="ml-auto text-xs text-warm-500 hover:text-foreground"
              >
                取消
              </button>
            )}
          </div>
          <div
            ref={streamRef}
            className="max-h-[220px] overflow-y-auto scrollbar-hide space-y-0.5"
          >
            {thinkingLines.slice(0, visibleCount).map((line, i) => {
              const isLastLine = i === visibleCount - 1
              return (
                <div
                  key={i}
                  className={cn(
                    'text-xs font-mono leading-relaxed',
                    line.ai ? 'text-[var(--terminal-text-ai,#7fffd4)]' : 'text-[var(--terminal-text,#d4d4d4)]',
                  )}
                >
                  <TypewriterLine
                    text={line.text || ''}
                    speed={20}
                    isLast={isLastLine}
                    onComplete={isLastLine ? advanceLine : undefined}
                    flush={!generating}
                  />
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Result card after done */}
      {genState === 'done' && generatedAction && (
        <div
          className="rounded-lg border border-border bg-card p-3 mb-3"
          data-testid="cluster-action-result"
        >
          <div className="flex items-start gap-2">
            <span
              className="px-1.5 py-0.5 rounded text-[10px] font-bold shrink-0"
              style={{
                background: 'var(--accent)',
                color: 'var(--accent-foreground)',
              }}
            >
              {actionTypeLabel(generatedAction.action_type)}
            </span>
            <h3 className="flex-1 text-sm font-semibold text-foreground leading-snug">
              {generatedAction.title}
            </h3>
          </div>
          {generatedAction.reason?.toLowerCase().includes('fallback') && (
            <p className="mt-2 rounded-md bg-amber-bg px-2.5 py-1.5 text-[12px] font-medium text-amber">
              保守兜底：AI 输出未解析成功，已基于事件摘要生成可继续编辑的行动点。
            </p>
          )}
          {generatedAction.reason && (
            <p className="mt-2 text-[12px] text-muted-foreground leading-relaxed">
              {generatedAction.reason}
            </p>
          )}
          {generatedAction.prompt && (
            <div
              className="mt-3 rounded-md border border-border bg-muted/60 p-2.5"
              data-testid="cluster-action-prompt"
            >
              <div className="mb-1 text-[11px] font-semibold text-muted-foreground">行动内容</div>
              <p className="whitespace-pre-line text-[12px] leading-relaxed text-foreground">
                {promptPreview(generatedAction.prompt)}
              </p>
            </div>
          )}
          <div className="mt-3 flex items-center gap-2">
            <button
              type="button"
              onClick={() => openActionDetail(generatedAction.id)}
              className="text-xs font-medium text-primary px-2 py-1 rounded hover:bg-accent"
            >
              查看行动详情
            </button>
            <button
              type="button"
              onClick={handleClose}
              className="text-xs text-muted-foreground hover:text-foreground px-2 py-1 rounded hover:bg-muted"
            >
              收起
            </button>
            <button
              type="button"
              onClick={handleShowForm}
              className="text-xs text-primary hover:underline px-2 py-1"
            >
              再生成一个
            </button>
          </div>
        </div>
      )}

      {/* Error state */}
      {genState === 'error' && (
        <div className="rounded-lg border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950 p-3 mb-3">
          <p className="text-sm text-red-600 dark:text-red-400">{generateError}</p>
          <button
            type="button"
            onClick={handleClose}
            className="text-sm text-primary hover:underline mt-2"
          >
            关闭
          </button>
        </div>
      )}

      {/* Existing actions list */}
      {showExistingActions && actions.length > 0 && genState === 'idle' && (
        <div className="space-y-1" data-testid="cluster-action-list">
          {actions.map((action) => (
            <button
              key={action.id}
              type="button"
              aria-label={`打开行动点: ${action.title}`}
              onClick={() => navigateToActionCard(action.id)}
              className="w-full flex items-center gap-2 text-left text-sm text-foreground hover:text-primary py-1.5 px-2 -mx-2 rounded-md hover:bg-muted transition-colors group"
            >
              <ArrowRight className="w-3.5 h-3.5 text-warm-400 group-hover:text-primary shrink-0" />
              <span className="truncate flex-1">{action.title}</span>
              {action.is_stale ? (
                <span
                  className="text-[10px] px-1.5 py-0.5 rounded"
                  style={{ background: 'var(--warn, #f59e0b)', color: '#fff' }}
                  title="此事件已更新，行动点可能过时"
                >
                  陈旧
                </span>
              ) : null}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
