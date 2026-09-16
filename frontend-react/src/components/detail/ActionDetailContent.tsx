import { useState } from 'react'
import { CheckCircle2, Copy, ExternalLink, Send } from 'lucide-react'
import { toast } from 'sonner'
import { useDetailStore } from '../../store/detailStore'
import { useClusterDetailStore } from '../../store/clusterDetailStore'
import { useAuthStore } from '../../store/authStore'
import { dispatchAction } from '../../lib/api'
import { cn, eventPlatformName, platformClass, stripMd } from '../../lib/utils'
import { renderMarkdownInline } from '../../lib/markdown-lite'
import { buildInfoItemHref } from '../../lib/itemDeepLink'
import type { ActionItem, ActionSourceItem } from '../../lib/types'
import { PlatformBrandIcon } from '../shared/PlatformIcon'
import { copyTextToClipboard } from './detailShared'

/** Parse a reason array: proper JSON, or best-effort salvage of legacy Python-repr
 *  (`[{'label': '..', 'text': '..'}]` — BF-0706-#3 历史脏数据;后端已修正新写入)。*/
function parseReasonArray(s: string): Array<{ label?: string; text?: string }> | null {
  try {
    const p = JSON.parse(s) as Array<{ label?: string; text?: string }>
    if (Array.isArray(p) && p.length > 0 && p[0].text) return p
  } catch { /* not JSON — try repr salvage below */ }
  if (/^\[\s*\{\s*'/.test(s)) {
    try {
      const p = JSON.parse(s.replace(/'/g, '"')) as Array<{ label?: string; text?: string }>
      if (Array.isArray(p) && p.length > 0 && p[0].text) return p
    } catch { /* salvage failed — fall through to raw */ }
  }
  return null
}

/** Parse reasoning: JSON array of {label, text} or plain string */
function ReasoningBlock({ text }: { text: string }) {
  const normalized = text.trim()
  if (normalized.startsWith('[')) {
    const parsed = parseReasonArray(normalized)
    if (parsed) {
      return (
        <div className="space-y-2">
          {parsed.map((item, i) => (
            <div key={i}>
              {item.label && <span className="font-semibold text-[var(--modal-text)]">{item.label}：</span>}
              {renderMarkdownInline(item.text || '')}
            </div>
          ))}
        </div>
      )
    }
  }
  return <p>{renderMarkdownInline(normalized)}</p>
}

export function ActionDetailContent({
  action,
  onPatchAction,
}: {
  action: ActionItem
  onPatchAction: (patch: Partial<ActionItem>) => void
}) {
  const actionPointItems = getActionPointItems(action)
  const sourceItems = getActionSourceItems(action)
  const decisionReason = action.ai_reasoning || action.reason || action.decision_brief || ''
  // v21.0 (模块 D): 顺序 理由 → 行动点 → 执行 → 关联信息。
  const showExecution = action.status === 'pending' || action.status === 'confirmed' || action.status === 'dispatched'

  return (
    <div className="pt-7">
      {decisionReason.trim() && (
        <section data-testid="action-modal-reason" className="mb-6">
          <h3 className="reading-section mb-3 leading-none text-[var(--brand)]">
            为什么做
          </h3>
          <div className="reading-body">
            <ReasoningBlock text={decisionReason} />
          </div>
        </section>
      )}

      {actionPointItems.length > 0 && (
        <section
          data-testid="action-modal-points"
          className={cn('mb-6', decisionReason.trim() && 'border-t border-[var(--modal-divider)] pt-4')}
        >
          <h3 className="reading-section mb-3 leading-none text-[var(--brand)]">
            行动点
          </h3>
          <ul className="reading-bullet space-y-2.5">
            {actionPointItems.map((item) => (
              <li key={item} className="flex min-w-0 items-start gap-3">
                <span
                  aria-hidden="true"
                  className="mt-[0.72em] h-1.5 w-1.5 shrink-0 rounded-full bg-[var(--brand)]"
                />
                <span className="min-w-0">{renderMarkdownInline(item)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {showExecution && (
        <section data-testid="action-modal-execution" className="mb-6 border-t border-[var(--modal-divider)] pt-4">
          <h3 className="reading-section mb-3 leading-none text-[var(--brand)]">
            执行
          </h3>
          <ActionExecutionBlock action={action} onPatchAction={onPatchAction} />
        </section>
      )}

      {sourceItems.length > 0 && (
        <section data-testid="action-modal-sources" className="border-t border-[var(--modal-divider)] pt-4">
          <h3 className="reading-section mb-3 leading-none text-[var(--brand)]">
            关联信息
          </h3>
          <div className="space-y-2">
            {sourceItems.map((source) => (
              <ActionSourceRow
                key={source.id}
                source={source}
                clusterId={action.source_type === 'cluster' && action.source_id != null ? Number(action.source_id) : null}
              />
            ))}
          </div>
        </section>
      )}
    </div>
  )
}

function ActionExecutionBlock({
  action,
  onPatchAction,
}: {
  action: ActionItem
  onPatchAction: (patch: Partial<ActionItem>) => void
}) {
  const canDispatch = useAuthStore((s) => s.user?.has_discord_token ?? false)
  const [copied, setCopied] = useState(false)
  const [busy, setBusy] = useState(false)
  const prompt = action.prompt || ''

  // v2 §13.3: 只复制 prompt(不再包命令)。
  const doCopy = async () => {
    try {
      await copyTextToClipboard(prompt)
      setCopied(true)
      toast.success('已复制,粘贴到本地 Agent 新会话执行')
    } catch {
      toast.error('复制失败')
    }
  }

  // v2 §13.4: 派发 Discord 算真实送出 → 自动置执行中;复制不改状态。
  const doDispatch = async () => {
    if (busy) return
    setBusy(true)
    try {
      await dispatchAction(action.id)
      onPatchAction({ status: 'dispatched' })
      toast.success('已派发到 Discord,进入执行中')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '派发失败')
    } finally {
      setBusy(false)
    }
  }

  const goSettings = () => {
    useDetailStore.getState().closeModal()
    window.location.hash = 'settings'
  }

  // 跟踪类无可执行命令,只作看板留存。
  const actionType = (action.action_type ?? action.type) as string
  if (actionType === 'track') {
    return (
      <div
        data-testid="action-execution"
        className="rounded-[8px] border border-[var(--modal-border-soft)] bg-[var(--modal-surface-soft)] px-3.5 py-3 reading-body text-[13.5px] leading-relaxed text-[var(--modal-text-muted)]"
      >
        这是一条<span className="font-semibold text-[var(--modal-text)]">跟踪项</span>,已留在行动看板。出现相关进展时,再把它转成可执行的行动。
      </div>
    )
  }

  return (
    <div data-testid="action-execution" className="space-y-3">
      <p className="reading-caption text-[var(--modal-text-muted)]">
        复制下面的指令,粘贴到你的 Claude Code / Codex 新会话执行。
      </p>
      {/* prompt 代码块 + 右上角单复制按钮 */}
      <div className="relative">
        <pre
          data-testid="exec-command"
          className="max-h-[220px] overflow-auto scrollbar-hide rounded-[8px] bg-[var(--action-code-bg)] px-3.5 py-3 pr-16 font-mono text-[12px] leading-relaxed text-[var(--action-code-text)] whitespace-pre-wrap break-words"
        >
          {prompt}
        </pre>
        <button
          type="button"
          data-testid="exec-copy-prompt"
          onClick={doCopy}
          aria-label="复制指令"
          className="absolute right-2 top-2 inline-flex items-center gap-1 rounded-[6px] border border-[rgba(255,255,255,0.16)] bg-[rgba(255,255,255,0.10)] px-2 py-1 text-[11px] font-medium text-[var(--action-code-text)] transition-colors hover:bg-[rgba(255,255,255,0.2)]"
        >
          {copied ? <CheckCircle2 className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      {/* Discord — 次级路径 */}
      <div className="flex items-center gap-2 border-t border-[var(--modal-divider)] pt-3">
        {canDispatch ? (
          <button
            type="button"
            data-testid="exec-dispatch"
            disabled={busy || action.status !== 'pending'}
            onClick={doDispatch}
            className="inline-flex items-center gap-1.5 rounded-[7px] border border-[var(--modal-border-soft)] px-3 py-1.5 text-[13px] font-medium text-[var(--modal-text-muted)] transition-colors hover:text-[var(--modal-text)] disabled:cursor-not-allowed disabled:opacity-60"
          >
            <Send className="h-3.5 w-3.5" />
            {action.status === 'dispatched' ? '已派发到 Discord' : '派发到 Discord'}
          </button>
        ) : (
          <span data-testid="exec-discord-unconfigured" className="reading-caption text-[var(--modal-text-muted)]">
            或派发到 Discord —
            <button type="button" onClick={goSettings} className="ml-1 font-semibold text-[var(--brand)] hover:underline">
              去设置
            </button>
          </span>
        )}
      </div>
    </div>
  )
}

function ActionSourceRow({ source, clusterId }: { source: ActionSourceItem; clusterId?: number | null }) {
  const openClusterModal = useClusterDetailStore((s) => s.openModal)
  const platform = source.platform || 'rss'
  const platformLabel = eventPlatformName(platform)
  const displayTitle = getActionSourceDisplayTitle(source)
  const label = normalizeActionSourceLabel(displayTitle)
  // v2 §14 BF-0706-2(#3): 事件类行动的关联信息 → 打开事件弹窗;信息类 → 信息弹窗深链
  const isCluster = clusterId != null && Number.isFinite(clusterId)
  const href = buildInfoItemHref(source.id)
  if (!displayTitle) return null

  const rowClass = cn(
    'group block w-full rounded-[7px] border border-[var(--modal-border-soft)] bg-[var(--modal-surface-soft)] px-3 py-2.5 text-left transition-colors',
    'hover:border-[var(--brand-border)] hover:bg-[var(--modal-hover-soft)]',
    'focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--modal-surface)]',
  )

  const inner = (
      <div className="flex min-w-0 items-center gap-2.5">
        <span
          className={cn(
            'inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-[5px] text-[10px] font-bold leading-none',
            platformClass(platform),
          )}
          title={platformLabel}
          aria-hidden="true"
        >
          <PlatformBrandIcon platform={platform} className="h-3.5 w-3.5" />
        </span>
        <span
          data-testid="action-modal-source-title"
          className="min-w-0 flex-1 truncate font-event-title text-[14px] font-semibold leading-[1.45] text-[var(--modal-text)] [&_strong]:font-bold"
          title={label}
        >
          {renderMarkdownInline(displayTitle)}
        </span>
        <span data-testid="action-modal-source-platform" className="shrink-0 text-[12px] leading-none text-[var(--modal-text-faint)]">
          {platformLabel}
        </span>
        <ExternalLink className="h-3.5 w-3.5 shrink-0 text-[var(--modal-text-faint)] transition-colors group-hover:text-[var(--brand)]" />
      </div>
  )

  if (isCluster) {
    return (
      <button
        type="button"
        onClick={() => { void openClusterModal(clusterId as number) }}
        data-testid="action-modal-source-row"
        className={rowClass}
        aria-label={`打开关联事件: ${label}`}
        title="打开关联事件"
      >
        {inner}
      </button>
    )
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      data-testid="action-modal-source-row"
      className={rowClass}
      aria-label={`打开关联信息: ${label}`}
      title="打开关联信息"
    >
      {inner}
    </a>
  )
}

function getActionPointItems(action: ActionItem): string[] {
  const stepItems = formatActionPointValue((action as ActionItem & { steps?: unknown }).steps)
  if (stepItems.length > 0) return stepItems
  const promptItems = formatActionPointText(action.prompt)
  if (promptItems.length > 0) return promptItems
  return formatActionPointText(action.expectation)
}

function formatActionPointValue(value?: unknown): string[] {
  if (Array.isArray(value)) return formatActionPointLines(value.map(String))
  if (typeof value === 'string') return formatActionPointText(value)
  return []
}

function formatActionPointText(text?: string): string[] {
  if (!text) return []
  const trimmed = text.trim()
  if (!trimmed) return []
  if (!trimmed.startsWith('[')) return formatActionPointLines(trimmed.split('\n'))
  try {
    const parsed = JSON.parse(trimmed) as unknown
    if (!Array.isArray(parsed)) return formatActionPointLines(trimmed.split('\n'))
    return formatActionPointLines(parsed.map((item) => {
      if (typeof item === 'string') return item
      if (!item || typeof item !== 'object') return ''
      const record = item as { text?: string; label?: string }
      return record.text || record.label || ''
    }))
  } catch {
    return formatActionPointLines(trimmed.split('\n'))
  }
}

function formatActionPointLines(lines?: string[]): string[] {
  return (lines ?? [])
    .map((line) => line.replace(/^\s*(?:[-*•]|\d+[.)、]|[一二三四五六七八九十]+[、.])\s*/, '').trim())
    .filter(Boolean)
    .filter((line) => !/^(行动步骤|具体步骤|步骤|完成标准|目标)[:：]?$/.test(line))
}

function getActionSourceDisplayTitle(source: ActionSourceItem): string {
  const title = source.title?.trim() || ''
  if (title && !isActionSourceUrlTitle(title)) return title

  const summary = source.ai_summary?.trim()
  if (summary) return summary

  return `关联信息 #${source.id.slice(0, 8)}`
}

function isActionSourceUrlTitle(title: string): boolean {
  const trimmed = title.trim()
  if (!trimmed) return false
  if (/^(https?:\/\/|www\.)\S+$/i.test(trimmed)) return true
  try {
    const url = new URL(trimmed)
    return url.protocol === 'http:' || url.protocol === 'https:'
  } catch {
    return false
  }
}

function normalizeActionSourceLabel(text: string): string {
  return stripMd(text).replace(/\s+/g, ' ').trim() || '关联信息'
}

function getActionSourceItems(action: ActionItem): ActionSourceItem[] {
  if (action.source_items?.length) return action.source_items
  return []
}
