import { useState } from 'react'
import { cn, actionTypeName, actionTypeClass } from '../../lib/utils'
import { updateAction } from '../../lib/api'
import type { ActionItem } from '../../lib/types'

interface ActionCardProps {
  action: ActionItem
  delay?: number
  onUpdate: (action: ActionItem) => void
  onDelete: (id: string) => void
  onRegenerate: () => void
}

/**
 * Single action card with hover operations + accordion edit mode.
 * 变更12: Action Card design
 * 变更17: 中文类型名
 */
export function ActionCard({
  action,
  delay = 0,
  onUpdate,
  onRegenerate,
}: ActionCardProps) {
  const [isEditing, setIsEditing] = useState(false)
  const [editTitle, setEditTitle] = useState(action.title)
  const [editSteps, setEditSteps] = useState(action.steps || [])

  const statusLabel: Record<string, string> = {
    pending: '待执行',
    dispatched: '已派发',
    done: '已完成',
    ignored: '已忽略',
  }

  const statusClass: Record<string, string> = {
    pending: 'text-warm-600 bg-warm-200',
    dispatched: 'text-amber bg-amber-bg',
    done: 'text-emerald bg-emerald-bg',
    ignored: 'text-warm-500 bg-warm-100',
  }

  const handleSave = async () => {
    await updateAction(action.id, {
      title: editTitle,
      steps: editSteps,
    })
    onUpdate({ ...action, title: editTitle, steps: editSteps })
    setIsEditing(false)
  }

  const handleCancel = () => {
    setEditTitle(action.title)
    setEditSteps(action.steps || [])
    setIsEditing(false)
  }

  return (
    <div
      className={cn(
        'group bg-card border border-border rounded-lg p-3 cursor-pointer',
        'hover:border-emerald-border hover:shadow-subtle transition-all duration-150',
        'animate-blur-fade',
        isEditing && 'border-primary ring-1 ring-primary/20',
      )}
      style={{ animationDelay: `${delay}ms` }}
      onClick={() => !isEditing && setIsEditing(true)}
    >
      {/* Header: type tag + status */}
      <div className="flex items-center gap-1.5 mb-1.5">
        <span
          className={cn(
            'text-sm font-semibold px-1.5 py-0.5 rounded',
            actionTypeClass(action.type),
          )}
        >
          {actionTypeName(action.type)}
        </span>
        <span
          className={cn(
            'ml-auto text-sm font-medium px-1.5 py-0.5 rounded',
            statusClass[action.status] || statusClass.pending,
          )}
        >
          {statusLabel[action.status] || action.status}
        </span>
      </div>

      {isEditing ? (
        /* Accordion edit mode — 变更12 */
        <div className="mt-2" onClick={(e) => e.stopPropagation()}>
          <input
            type="text"
            value={editTitle}
            onChange={(e) => setEditTitle(e.target.value)}
            className="w-full text-sm font-semibold bg-muted border border-input rounded-md px-2 py-1.5 mb-2 focus:outline-none focus:ring-2 focus:ring-ring"
          />
          <div className="space-y-1 mb-2">
            {editSteps.map((step, i) => (
              <div key={i} className="flex items-start gap-1.5">
                <span className="text-sm text-warm-500 font-mono mt-1.5 w-4 text-right flex-shrink-0">
                  {i + 1}.
                </span>
                <input
                  type="text"
                  value={step}
                  onChange={(e) => {
                    const newSteps = [...editSteps]
                    newSteps[i] = e.target.value
                    setEditSteps(newSteps)
                  }}
                  className="flex-1 text-sm bg-muted border border-input rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-ring"
                />
              </div>
            ))}
            <button
              onClick={() => setEditSteps([...editSteps, ''])}
              className="text-sm text-primary hover:underline ml-6"
            >
              + 添加步骤
            </button>
          </div>
          <div className="flex justify-end gap-2">
            <button
              onClick={handleCancel}
              className="text-sm text-muted-foreground hover:text-foreground px-3 py-1 rounded-md hover:bg-muted transition-colors"
            >
              取消
            </button>
            <button
              onClick={handleSave}
              className="text-sm font-medium text-primary-foreground bg-primary px-3 py-1 rounded-md hover:bg-primary/90 transition-colors"
            >
              保存
            </button>
          </div>
        </div>
      ) : (
        /* Display mode */
        <>
          <h4 className="text-sm font-semibold text-foreground leading-snug mb-1">
            {action.title}
          </h4>
          {action.steps && action.steps.length > 0 && (
            <div className="text-sm text-muted-foreground space-y-0.5">
              {action.steps.slice(0, 3).map((step, i) => (
                <div key={i} className="flex gap-1.5">
                  <span className="text-warm-500 font-mono w-3 text-right flex-shrink-0">
                    {i + 1}.
                  </span>
                  <span className="line-clamp-1">{step}</span>
                </div>
              ))}
            </div>
          )}

          {/* Hover actions — 变更12 */}
          <div className="flex justify-end gap-3 mt-2 opacity-0 group-hover:opacity-100 transition-opacity duration-150">
            <button
              onClick={(e) => {
                e.stopPropagation()
                setIsEditing(true)
              }}
              className="text-sm font-medium text-primary hover:underline"
            >
              编辑
            </button>
            <span className="text-border text-sm">·</span>
            <button
              onClick={(e) => {
                e.stopPropagation()
                onRegenerate()
              }}
              className="text-sm font-medium text-primary hover:underline"
            >
              重新生成
            </button>
          </div>
        </>
      )}
    </div>
  )
}
