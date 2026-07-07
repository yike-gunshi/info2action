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
import { Plus, ArrowRight, RotateCcw } from 'lucide-react'
import { toast } from 'sonner'
import { deleteAction } from '../../lib/api'
import { navigateToActionCard } from '../../lib/actionNavigation'
import { useClusterDetailStore } from '../../store/clusterDetailStore'
import { requireAuth } from '../shared/AuthGate'
import { TypewriterLine } from '../shared/TypewriterLine'
import { ActionGenHint } from '../shared/ActionGenHint'


// v21.0: 行动类型现由 AI 自动判定,这里仅作徽章标签映射(4 类)。
const ACTION_TYPE_LABELS: Record<string, string> = {
  investigate: '调研',
  implement: '实践',
  content: '创作',
  track: '跟踪',
}

function actionTypeLabel(type: string) {
  return ACTION_TYPE_LABELS[type] || type
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
  const thinkingLines = useClusterDetailStore((s) => s.generateThinkingLines)
  const generatedAction = useClusterDetailStore((s) => s.generateAction)
  const generateError = useClusterDetailStore((s) => s.generateError)
  const actions = useClusterDetailStore((s) => s.actions)
  const loadActions = useClusterDetailStore((s) => s.loadActions)
  const resetGenerate = useClusterDetailStore((s) => s.resetGenerate)

  const [genState, setGenState] = useState<GenState>('idle')
  const [userHint, setUserHint] = useState('')
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
    setUserHint('')
    setVisibleCount(0)
  }

  const handleGenerate = () => {
    if (!requireAuth('生成行动')) return
    resetGenerate()
    setVisibleCount(0)
    startGenerate(clusterId, userHint.trim() || undefined)
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

  // v2 §14.3(T3): 重新生成 = 作废当前已生成行动(删库)+ 重开表单。
  const handleRegenerate = async () => {
    const current = generatedAction
    resetGenerate()
    setVisibleCount(0)
    setGenState('form')
    setUserHint('')
    if (current?.id) {
      try {
        await deleteAction(current.id)
        loadActions(clusterId)
      } catch {
        toast.error('作废旧行动点失败,可到行动页手动删除')
      }
    }
  }

  // v21.0 (模块 C): 「查看行动详情」统一走 navigateToActionCard —— 切行动 Tab + 高亮 + 开弹窗。
  const openActionDetail = useCallback((actionId: string) => {
    navigateToActionCard(actionId)
  }, [])

  return (
    <div className="w-full mt-2">
      {/* Header — 品牌色小标题 + 醒目 CTA(v21.0 redesign) */}
      <div className="flex items-center justify-between mb-3">
        <h3 className="reading-section leading-none text-[var(--brand)]">行动点</h3>
        {genState === 'idle' && !generatedAction && (
          <button
            type="button"
            onClick={handleShowForm}
            data-testid="cluster-action-trigger"
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
          <p className="font-event-title text-[13px] text-[var(--modal-text-muted)] mb-2.5">AI 会自动判定行动类型;想指定角度就写一句。</p>
          <textarea
            value={userHint}
            onChange={(e) => setUserHint(e.target.value)}
            placeholder="补充说明（可选）：告诉 AI 你关注的角度..."
            className="w-full font-event-title text-[14px] bg-[var(--modal-surface)] border border-[var(--modal-border-soft)] rounded-[8px] px-3 py-2.5 mb-3 resize-none text-[var(--modal-text)] placeholder:text-[var(--modal-text-faint)] focus:outline-none focus:ring-1 focus:ring-[var(--brand-border)]"
            rows={2}
          />
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setGenState('idle')}
              className="text-[13px] text-[var(--modal-text-muted)] hover:text-[var(--modal-text)] px-3 py-1.5 rounded-[7px] hover:bg-[var(--modal-hover)] transition-colors"
            >
              取消
            </button>
            <button
              type="button"
              onClick={handleGenerate}
              data-testid="cluster-action-start"
              className="inline-flex items-center gap-1.5 rounded-[8px] border border-[var(--brand-border)] px-4 py-2 text-[13px] font-semibold text-[var(--brand)] transition-colors hover:bg-[var(--brand-soft)]"
            >
              开始生成
            </button>
          </div>
        </div>
      )}

      {/* Generating: 暖色分析面板(v2 §13.2)—— 去阶段标签,人话标题 */}
      {(genState === 'generating' || (genState === 'done' && thinkingLines.length > 0)) && (
        <div
          className="rounded-[10px] border border-[var(--modal-border-soft)] bg-[var(--modal-surface)] p-3.5 mb-3"
          data-testid="cluster-action-stream"
        >
          <div className="flex items-center gap-2 mb-2.5">
            <span className="inline-flex items-center gap-2 text-[13px] font-semibold text-[var(--brand)]">
              正在分析这个事件
              <span className="inline-flex gap-1" aria-hidden="true">
                <span className="analyze-dot" /><span className="analyze-dot" /><span className="analyze-dot" />
              </span>
            </span>
            {generating && (
              <button
                type="button"
                onClick={handleCancel}
                className="ml-auto reading-caption text-[var(--modal-text-muted)] hover:text-[var(--modal-text)]"
              >
                取消
              </button>
            )}
          </div>
          <div
            ref={streamRef}
            className="max-h-[220px] overflow-y-auto scrollbar-hide space-y-0.5 rounded-[8px] bg-[var(--action-code-bg)] px-3.5 py-3 font-mono text-[12px] leading-relaxed"
          >
            {thinkingLines.slice(0, visibleCount).map((line, i) => {
              const isLastLine = i === visibleCount - 1
              return (
                <div
                  key={i}
                  className={line.ai ? 'text-[var(--action-code-text)]' : 'text-[var(--action-code-faint)]'}
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

      {/* Result card after done — 纸面弹窗风格,展示 steps 行动点 */}
      {genState === 'done' && generatedAction && (
        <div
          className="rounded-[10px] border border-[var(--modal-border-soft)] bg-[var(--modal-surface-soft)] p-3.5 mb-3"
          data-testid="cluster-action-result"
        >
          <div className="flex items-start gap-2">
            <span className="shrink-0 rounded-[5px] bg-[var(--brand-soft)] px-2 py-0.5 text-[11px] font-semibold text-[var(--brand)]">
              {actionTypeLabel(generatedAction.action_type)}
            </span>
            <h3 className="flex-1 font-event-title text-[15px] font-semibold leading-snug text-[var(--modal-text)]">
              {generatedAction.title}
            </h3>
          </div>
          {generatedAction.reason?.toLowerCase().includes('fallback') && (
            <p className="mt-2 rounded-md bg-amber-bg px-2.5 py-1.5 text-[12px] font-medium text-amber">
              保守兜底：AI 输出未解析成功，已基于事件摘要生成可继续编辑的行动点。
            </p>
          )}
          {generatedAction.reason && (
            <p className="mt-2 font-event-title text-[13.5px] leading-relaxed text-[var(--modal-text-muted)]">
              {generatedAction.reason}
            </p>
          )}
          {generatedAction.steps && generatedAction.steps.length > 0 && (
            <ul className="mt-3 space-y-1.5" data-testid="cluster-action-steps">
              {generatedAction.steps.map((step, i) => (
                <li key={i} className="flex min-w-0 items-start gap-2 font-event-title text-[13.5px] leading-relaxed text-[var(--modal-text)]">
                  <span aria-hidden="true" className="mt-[0.6em] h-1.5 w-1.5 shrink-0 rounded-full bg-[var(--brand)]" />
                  <span className="min-w-0">{step}</span>
                </li>
              ))}
            </ul>
          )}
          <div className="mt-3.5 flex items-center gap-2.5">
            <button
              type="button"
              onClick={() => openActionDetail(generatedAction.id)}
              className="inline-flex items-center gap-1.5 rounded-[8px] border border-[var(--brand-border)] px-3.5 py-2 text-[13px] font-semibold text-[var(--brand)] transition-colors hover:bg-[var(--brand-soft)]"
            >
              <ArrowRight className="h-3.5 w-3.5" />
              查看行动详情
            </button>
            <button
              type="button"
              data-testid="cluster-action-regenerate"
              onClick={handleRegenerate}
              className="inline-flex items-center gap-1.5 rounded-[8px] border border-[var(--modal-border-soft)] px-3.5 py-2 text-[13px] font-semibold text-[var(--modal-text-muted)] transition-colors hover:border-[var(--brand-border)] hover:text-[var(--brand)]"
            >
              <RotateCcw className="h-3.5 w-3.5" />
              重新生成
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
        <div className="space-y-0.5" data-testid="cluster-action-list">
          {actions.map((action) => (
            <button
              key={action.id}
              type="button"
              aria-label={`打开行动点: ${action.title}`}
              onClick={() => navigateToActionCard(action.id)}
              className="w-full flex items-center gap-2 text-left text-[14px] text-[var(--modal-text)] hover:text-[var(--brand)] py-2 px-2.5 -mx-2.5 rounded-[7px] hover:bg-[var(--modal-hover)] transition-colors group"
            >
              <ArrowRight className="w-3.5 h-3.5 text-[var(--modal-text-faint)] group-hover:text-[var(--brand)] shrink-0" />
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
