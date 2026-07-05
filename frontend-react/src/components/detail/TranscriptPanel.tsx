/**
 * v20.7 ASR 内嵌边框态 (DetailPanel 子组件)
 *
 * ASR 作为视频的附属转写区，固定在视频正下方：
 * - idle/running: 约 68px 的纯边框条，只保留核心状态和操作。
 * - ready: 约 300px 的纯边框阅读框，内部滚动。
 * - 不展示金额、ETA、进度条；工具按钮使用中性按钮；当前段使用橙红高亮。
 */
import React, { useEffect, useMemo, useRef, useState } from 'react'
import { AudioLines, Copy, Check, AlertCircle, Music, RotateCw, Download, ChevronDown, Crosshair } from 'lucide-react'
import { toast } from 'sonner'

import { cn } from '../../lib/utils'
import { useDetailStore } from '../../store/detailStore'
import { requireAuth } from '../shared/AuthGate'
import type { AsrSegment, FeedItem, TranscriptPanelState } from '../../lib/types'

interface Props {
  itemId: string
  item?: FeedItem
}

/** 派发到 VideoPlayer / YoutubePlayer 的 seek 事件。 */
function dispatchSeek(itemId: string, ms: number) {
  window.dispatchEvent(new CustomEvent('asr:seek', { detail: { itemId, ms } }))
}

function panelStateFromItem(item?: FeedItem | null): TranscriptPanelState | null {
  if (!item) return null
  if (item.asr_text && item.asr_text.length > 0) return 'ready'
  if (!item.asr_status) return null
  if (item.asr_status === 'running') return 'running'
  if (item.asr_status === 'success') return 'ready'
  if (item.asr_status === 'failed_empty') return 'empty'
  if (item.asr_status === 'skipped_quota') return 'idle'
  return 'failed'
}

export function TranscriptPanel({ itemId, item }: Props): React.ReactElement {
  const status = useDetailStore((s) => s.asrStatus)
  const rawStatus = useDetailStore((s) => s.asrRawStatus)
  const asrText = useDetailStore((s) => s.asrText)
  const asrSegments = useDetailStore((s) => s.asrSegments)
  const asrSegmentsCn = useDetailStore((s) => s.asrSegmentsCn)
  const durationSec = useDetailStore((s) => s.asrDurationSec)
  const currentTimeMs = useDetailStore((s) => s.asrCurrentTimeMs)
  const autoFollow = useDetailStore((s) => s.asrAutoFollow)
  const error = useDetailStore((s) => s.asrError)
  const retryCount = useDetailStore((s) => s.asrRetryCount)
  const startAsr = useDetailStore((s) => s.startAsr)
  const retryAsr = useDetailStore((s) => s.retryAsr)
  const toggleAutoFollow = useDetailStore((s) => s.toggleAsrAutoFollow)
  const hydrateFromItem = useDetailStore((s) => s.hydrateFromItem)
  const storeItemId = useDetailStore((s) => s.itemDetail?.id ?? null)

  const itemSnapshotState = item?.id === itemId ? panelStateFromItem(item) : null
  const shouldUseItemSnapshot = !!itemSnapshotState
    && (storeItemId !== itemId || (status === 'idle' && (!!item?.asr_text || !!item?.asr_status)))

  const displayStatus = shouldUseItemSnapshot ? itemSnapshotState : status
  const displayRawStatus = shouldUseItemSnapshot ? (item?.asr_status ?? null) : rawStatus
  const displayAsrText = shouldUseItemSnapshot ? (item?.asr_text ?? null) : asrText
  const displayAsrSegments = shouldUseItemSnapshot ? (item?.asr_segments ?? null) : asrSegments
  const displayAsrSegmentsCn = shouldUseItemSnapshot ? (item?.asr_segments_cn ?? null) : asrSegmentsCn
  const displayDurationSec = shouldUseItemSnapshot ? (item?.asr_duration_sec ?? null) : durationSec

  useEffect(() => {
    if (!item || item.id !== itemId) return
    if (!item.asr_text && !item.asr_status) return
    if (storeItemId === itemId && rawStatus === (item.asr_status ?? null) && asrText === (item.asr_text ?? null)) return
    hydrateFromItem(item)
  }, [asrText, hydrateFromItem, item, itemId, rawStatus, storeItemId])

  const containerClass = cn(
    'w-full mb-4 overflow-hidden rounded-[6px] border border-[var(--modal-border)] bg-[var(--modal-surface-soft)] text-[var(--modal-text)] shadow-none',
    'transition-colors duration-[180ms] ease-out',
    displayStatus === 'ready'
      ? 'flex h-[300px] max-h-[42vh] min-h-[220px] flex-col px-3'
      : 'flex min-h-[68px] items-center px-4 py-3',
    displayStatus === 'failed' && 'border-[var(--modal-danger-border)] bg-[var(--modal-danger-surface)]',
    displayStatus === 'empty' && 'bg-[var(--modal-surface)]',
  )

  return (
    <div
      role="region"
      aria-label="视频转写"
      aria-live="polite"
      className={containerClass}
      data-asr-panel
      data-asr-status={displayStatus}
      data-asr-raw-status={displayRawStatus ?? 'null'}
    >
      {displayStatus === 'idle' && (
        <IdleView
          rawStatus={displayRawStatus}
          onStart={() => { if (requireAuth('AI 转写')) startAsr(itemId) }}
        />
      )}
      {displayStatus === 'running' && <RunningView />}
      {displayStatus === 'ready' && (
        <ReadyView
          itemId={itemId}
          asrText={displayAsrText}
          asrSegments={displayAsrSegments}
          asrSegmentsCn={displayAsrSegmentsCn}
          durationSec={displayDurationSec}
          currentTimeMs={currentTimeMs}
          autoFollow={autoFollow}
          onToggleAutoFollow={toggleAutoFollow}
        />
      )}
      {displayStatus === 'failed' && (
        <FailedView error={error} canRetry={retryCount < 1} onRetry={() => retryAsr(itemId)} />
      )}
      {displayStatus === 'empty' && <EmptyView />}
    </div>
  )
}

function IdleView({
  rawStatus,
  onStart,
}: {
  rawStatus: string | null
  onStart: () => void
}): React.ReactElement {
  const isQuotaExhausted = rawStatus === 'skipped_quota'
  return (
    <div className="flex w-full items-center justify-between gap-3" data-idle-variant={isQuotaExhausted ? 'quota' : 'default'}>
      <div className="flex min-w-0 items-center gap-2.5">
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[5px] border border-[var(--modal-border-soft)] bg-[var(--modal-surface)] text-[var(--brand)]">
          <AudioLines className="h-4 w-4" />
        </span>
        <div className="min-w-0">
          <div className="font-event-title text-[15px] leading-tight text-[var(--modal-text-soft)]">AI 视频转写</div>
          {isQuotaExhausted && (
            <div className="mt-1 text-[12px] leading-tight text-[var(--modal-text-faint)]" data-testid="idle-subtext">
              今日 ASR 配额已用尽
            </div>
          )}
        </div>
      </div>
      <button
        onClick={onStart}
        aria-label="开始 AI 转写"
        className={cn(
          'inline-flex h-9 shrink-0 items-center gap-1.5 rounded-[5px] bg-[var(--brand)] px-3.5 text-[13px] font-medium text-[var(--brand-foreground)]',
          'transition-colors duration-150 ease-out hover:bg-[var(--brand)] active:bg-[var(--brand)]',
          'disabled:cursor-not-allowed disabled:opacity-50',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand)] focus-visible:ring-offset-2',
        )}
      >
        <AudioLines className="h-3.5 w-3.5" />
        开始转写
      </button>
    </div>
  )
}

function RunningView(): React.ReactElement {
  return (
    <div className="flex w-full items-center justify-between gap-3" data-testid="asr-running-inline">
      <div className="flex min-w-0 items-center gap-2.5">
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[5px] border border-[var(--modal-border-soft)] bg-[var(--modal-surface)] text-[var(--brand)]">
          <AudioLines className="h-4 w-4" />
        </span>
        <div className="min-w-0">
          <div className="font-event-title text-[15px] leading-tight text-[var(--modal-text-soft)]">AI 视频转写</div>
          <div className="mt-1 text-[12px] leading-tight text-[var(--modal-text-faint)]">正在转写中</div>
        </div>
      </div>
      <button
        type="button"
        disabled
        aria-label="转写中"
        className="inline-flex h-9 shrink-0 cursor-not-allowed items-center gap-1.5 rounded-[5px] border border-[var(--modal-border)] bg-transparent px-3 text-[13px] font-medium text-[var(--modal-text-muted)] opacity-85"
      >
        <RotateCw className="h-3.5 w-3.5 animate-spin" />
        转写中
      </button>
    </div>
  )
}

function ReadyView({
  itemId,
  asrText,
  asrSegments,
  asrSegmentsCn,
  durationSec,
  currentTimeMs,
  autoFollow,
  onToggleAutoFollow,
}: {
  itemId: string
  asrText: string | null
  asrSegments: AsrSegment[] | null
  asrSegmentsCn: (string | null)[] | null
  durationSec: number | null
  currentTimeMs: number
  autoFollow: boolean
  onToggleAutoFollow: () => void
}): React.ReactElement {
  const [copied, setCopied] = useState(false)
  const [srtMenuOpen, setSrtMenuOpen] = useState(false)

  const hasSegments = Array.isArray(asrSegments) && asrSegments.length > 0
  const hasBilingual = hasSegments
    && Array.isArray(asrSegmentsCn)
    && asrSegmentsCn.length === asrSegments!.length
  const charCount = asrText?.length ?? 0
  const mins = durationSec ? Math.round(durationSec / 60) : 0

  const currentIdx = useMemo(() => {
    if (!hasSegments) return -1
    return findCurrentSegment(asrSegments!, currentTimeMs)
  }, [hasSegments, asrSegments, currentTimeMs])

  const lastUserScrollRef = useRef(0)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const onUserScroll = () => { lastUserScrollRef.current = Date.now() }

  useEffect(() => {
    if (!autoFollow || currentIdx < 0 || !scrollRef.current) return
    const since = Date.now() - lastUserScrollRef.current
    if (since < 5000) return
    scrollSegmentToCenter(scrollRef.current, currentIdx, 'smooth')
  }, [currentIdx, autoFollow])

  const onCopy = async () => {
    if (!asrText) return
    try {
      await navigator.clipboard.writeText(asrText)
      setCopied(true)
      toast.success(`已复制 ${asrText.length.toLocaleString()} 字到剪贴板`)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error('复制失败,请手动选中内容复制')
    }
  }

  const onSeekToSeg = (startMs: number) => {
    dispatchSeek(itemId, startMs)
  }

  const onFocusCurrent = () => {
    if (currentIdx < 0 || !scrollRef.current) return
    scrollSegmentToCenter(scrollRef.current, currentIdx, 'smooth')
  }

  const doSrtDownload = (bilingual: boolean) => {
    if (!hasSegments) return
    const srt = bilingual && hasBilingual
      ? segmentsToBilingualSrt(asrSegments!, asrSegmentsCn!)
      : segmentsToSrt(asrSegments!)
    const blob = new Blob([srt], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = bilingual && hasBilingual
      ? `transcript-${itemId}.bilingual.srt`
      : `transcript-${itemId}.en.srt`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    setSrtMenuOpen(false)
  }

  const onExportSrtClick = () => {
    if (hasBilingual) {
      setSrtMenuOpen((v) => !v)
    } else {
      doSrtDownload(false)
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-ready-variant={hasBilingual ? 'bilingual' : 'mono'}>
      <div className="flex h-11 flex-shrink-0 items-center gap-3 border-b border-[var(--modal-divider)]">
        <div className="min-w-0 flex-1 truncate text-[12px] leading-none text-[var(--modal-text-muted)] tabular-nums">
          📝 {mins} min · {charCount.toLocaleString()} 字
          {hasBilingual && <span> · 双语</span>}
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {hasSegments && (
            <>
              <label className="inline-flex h-7 items-center gap-1.5 rounded-[4px] px-1.5 text-[12px] font-medium text-[var(--modal-text-muted)]">
                <input
                  type="checkbox"
                  checked={autoFollow}
                  onChange={onToggleAutoFollow}
                  className="h-3 w-3 rounded border-[var(--modal-border)] accent-[var(--brand)]"
                />
                自动跟随
              </label>
              <button
                onClick={onFocusCurrent}
                aria-label="聚焦当前播放段"
                disabled={currentIdx < 0}
                className={cn(
                  'inline-flex h-7 items-center gap-1 rounded-[4px] border border-[var(--modal-border)] bg-transparent px-2 text-[12px] font-medium text-[var(--modal-text-muted)]',
                  'transition-colors duration-150 hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)]',
                  'disabled:cursor-not-allowed disabled:opacity-45 disabled:hover:bg-transparent disabled:hover:text-[var(--modal-text-muted)]',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand)] focus-visible:ring-offset-2',
                )}
                data-testid="asr-focus-button"
              >
                <Crosshair className="h-3.5 w-3.5" />
                聚焦
              </button>
            </>
          )}
          <button
            onClick={onCopy}
            aria-label="复制转写文本"
            disabled={!asrText}
            className={cn(
              'inline-flex h-7 items-center gap-1 rounded-[4px] border border-[var(--modal-border)] bg-transparent px-2 text-[12px] font-medium text-[var(--modal-text-muted)]',
              'transition-colors duration-150 hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)]',
              'disabled:cursor-not-allowed disabled:opacity-45 disabled:hover:bg-transparent disabled:hover:text-[var(--modal-text-muted)]',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand)] focus-visible:ring-offset-2',
            )}
            data-testid="asr-copy-button"
          >
            {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
            {copied ? '已复制' : '复制'}
          </button>
          {hasSegments && (
            <div className="relative">
              <button
                onClick={onExportSrtClick}
                aria-label={hasBilingual ? '导出 SRT 字幕(支持中英双语)' : '导出 SRT 字幕文件'}
                aria-haspopup={hasBilingual ? 'menu' : undefined}
                aria-expanded={hasBilingual ? srtMenuOpen : undefined}
                className={cn(
                  'inline-flex h-7 items-center gap-1 rounded-[4px] border border-[var(--modal-border)] bg-transparent px-2 text-[12px] font-medium text-[var(--modal-text-muted)]',
                  'transition-colors duration-150 hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)]',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand)] focus-visible:ring-offset-2',
                )}
                data-testid="asr-srt-button"
              >
                <Download className="h-3.5 w-3.5" />
                SRT
                {hasBilingual && <ChevronDown className="h-3 w-3" />}
              </button>
              {srtMenuOpen && hasBilingual && (
                <div
                  role="menu"
                  className="absolute right-0 top-full z-10 mt-1 w-36 rounded-[5px] border border-[var(--modal-border)] bg-[var(--modal-surface-soft)] py-1 shadow-none"
                >
                  <button
                    role="menuitem"
                    onClick={() => doSrtDownload(false)}
                    className="w-full px-3 py-1.5 text-left text-[12px] text-[var(--modal-text-muted)] hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)]"
                  >
                    英文 SRT
                  </button>
                  <button
                    role="menuitem"
                    onClick={() => doSrtDownload(true)}
                    className="w-full px-3 py-1.5 text-left text-[12px] text-[var(--modal-text-muted)] hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)]"
                  >
                    中英双语 SRT
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      <div
        ref={scrollRef}
        onScroll={onUserScroll}
        className="relative min-h-0 flex-1 overflow-y-auto py-2 font-event-title text-[14px] text-[var(--modal-text-muted)] scrollbar-thin"
        style={{ lineHeight: 1.6, scrollbarWidth: 'thin' }}
      >
        {hasSegments ? (
          <SegmentBilingualList
            segments={asrSegments!}
            segmentsCn={asrSegmentsCn}
            currentIdx={currentIdx}
            onSeek={onSeekToSeg}
          />
        ) : (
          <div data-fallback="no-segments">
            {asrText?.split('\n').map((para, i) => (
              <p key={i} className="mb-2 text-[14px] leading-[1.62]">{para}</p>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function SegmentBilingualList({
  segments,
  segmentsCn,
  currentIdx,
  onSeek,
}: {
  segments: AsrSegment[]
  segmentsCn: (string | null)[] | null
  currentIdx: number
  onSeek: (startMs: number) => void
}): React.ReactElement {
  const hasCn = Array.isArray(segmentsCn) && segmentsCn.length === segments.length
  return (
    <div className="py-1">
      {segments.map((seg, i) => {
        const isCurrent = i === currentIdx
        const cnText = hasCn ? (segmentsCn![i] || '') : ''
        return (
          <button
            key={i}
            type="button"
            data-seg-idx={i}
            data-seg-start={seg.start_ms}
            data-seg-end={seg.end_ms}
            onClick={() => onSeek(seg.start_ms)}
            className={cn(
              'block w-full rounded-[5px] px-2 py-2 text-left transition-colors duration-150',
              isCurrent ? 'bg-[var(--modal-current-bg)]' : 'hover:bg-[var(--modal-current-hover)]',
            )}
          >
            <div className="flex items-baseline gap-2">
              <span
                aria-hidden
                className={cn(
                  'shrink-0 w-[52px] font-mono text-[12px] tabular-nums',
                  isCurrent ? 'text-[var(--brand)]' : 'text-[var(--modal-text-faint)]',
                )}
              >
                {formatTimestamp(seg.start_ms)}
              </span>
              <span
                className={cn(
                  'text-[14px] leading-[1.62]',
                  isCurrent ? 'font-medium text-[var(--brand)]' : 'text-[var(--modal-text-muted)]',
                )}
                data-lang="en"
              >
                {seg.text}
              </span>
            </div>
            {cnText && (
              <div className="mt-0.5 flex items-baseline gap-2">
                <span className="shrink-0 w-[52px]" aria-hidden />
                <span
                  className={cn(
                    'text-[14px] leading-[1.62]',
                    isCurrent ? 'text-[var(--modal-current-text)]' : 'text-[var(--modal-text-muted)]',
                  )}
                  data-lang="zh"
                >
                  {cnText}
                </span>
              </div>
            )}
          </button>
        )
      })}
    </div>
  )
}

function FailedView({
  error,
  canRetry,
  onRetry,
}: {
  error: string | null
  canRetry: boolean
  onRetry: () => void
}): React.ReactElement {
  const needLogin = error === '登录后可用 AI 转写'
  if (needLogin) {
    return (
      <div className="flex w-full items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5 text-[13px] text-[var(--modal-text-muted)]">
          <AudioLines className="h-4 w-4 shrink-0 text-[var(--brand)]" />
          <span>登录后可用 AI 转写</span>
        </div>
        <button
          onClick={() => { window.location.hash = 'login' }}
          className={cn(
            'inline-flex h-9 shrink-0 items-center gap-1 rounded-[5px] bg-[var(--brand)] px-3.5 text-[13px] font-medium text-[var(--brand-foreground)]',
            'transition-colors duration-150 hover:bg-[var(--brand)]',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand)] focus-visible:ring-offset-2',
          )}
        >
          去登录
        </button>
      </div>
    )
  }
  return (
    <div className="flex w-full items-center justify-between gap-3">
      <div className="flex min-w-0 items-center gap-2.5">
        <AlertCircle className="h-4 w-4 shrink-0 text-[var(--modal-danger)]" />
        <div className="min-w-0">
          <div className="text-[13px] font-medium text-[var(--modal-danger)]">转写失败</div>
          <div className="mt-1 truncate text-[12px] text-[var(--modal-text-faint)]">
            {error || '未知错误'}
          </div>
        </div>
      </div>
      {canRetry ? (
        <button
          onClick={onRetry}
          className={cn(
            'inline-flex h-9 shrink-0 items-center gap-1 rounded-[5px] border border-[var(--modal-danger-border)] bg-transparent px-3 text-[13px] font-medium text-[var(--modal-danger)]',
            'transition-colors duration-150 hover:bg-[var(--modal-danger-hover)]',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--modal-danger)] focus-visible:ring-offset-2',
          )}
        >
          重试转写
        </button>
      ) : (
        <div className="shrink-0 text-[12px] text-[var(--modal-text-faint)]">
          重试失败,关闭弹窗后重开可再重试
        </div>
      )}
    </div>
  )
}

function EmptyView(): React.ReactElement {
  return (
    <div className="flex w-full items-center gap-2.5">
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[5px] border border-[var(--modal-border-soft)] bg-[var(--modal-surface)] text-[var(--modal-text-faint)]">
        <Music className="h-4 w-4" />
      </span>
      <div className="font-event-title text-[15px] text-[var(--modal-text-muted)]">
        视频无语音内容
      </div>
    </div>
  )
}

/** 二分查找:从 segments 里找到包含 currentTimeMs 的段 index。返回 -1 表示无匹配。 */
function findCurrentSegment(segments: AsrSegment[], ms: number): number {
  if (!segments.length) return -1
  let lo = 0
  let hi = segments.length - 1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    const s = segments[mid]
    if (ms < s.start_ms) hi = mid - 1
    else if (ms >= s.end_ms) lo = mid + 1
    else return mid
  }
  if (ms > segments[segments.length - 1].end_ms) return segments.length - 1
  return Math.max(0, lo - 1)
}

function scrollSegmentToCenter(container: HTMLElement, segIdx: number, behavior: ScrollBehavior) {
  const el = container.querySelector<HTMLElement>(`[data-seg-idx="${segIdx}"]`)
  if (!el) return
  const elRect = el.getBoundingClientRect()
  const cRect = container.getBoundingClientRect()
  const offsetWithinContainer = (elRect.top - cRect.top) + container.scrollTop
  const target = offsetWithinContainer - (container.clientHeight - el.offsetHeight) / 2
  const top = Math.max(0, target)
  if (typeof container.scrollTo === 'function') {
    container.scrollTo({ top, behavior })
  } else {
    container.scrollTop = top
  }
}

function formatTimestamp(ms: number): string {
  const total = Math.floor(ms / 1000)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
  return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
}

function toSrtStamp(ms: number): string {
  const total = Math.floor(ms / 1000)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const msPart = ms % 1000
  return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')},${msPart.toString().padStart(3, '0')}`
}

function segmentsToSrt(segments: AsrSegment[]): string {
  return segments
    .map((seg, i) => `${i + 1}\n${toSrtStamp(seg.start_ms)} --> ${toSrtStamp(seg.end_ms)}\n${seg.text}\n`)
    .join('\n')
}

function segmentsToBilingualSrt(segments: AsrSegment[], segmentsCn: (string | null)[]): string {
  return segments
    .map((seg, i) => {
      const cn = segmentsCn[i] || ''
      const text = cn ? `${seg.text}\n${cn}` : seg.text
      return `${i + 1}\n${toSrtStamp(seg.start_ms)} --> ${toSrtStamp(seg.end_ms)}\n${text}\n`
    })
    .join('\n')
}
