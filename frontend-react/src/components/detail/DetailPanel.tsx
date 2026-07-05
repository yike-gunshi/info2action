import { useState, useCallback, useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import type { CSSProperties } from 'react'
import { X, ExternalLink, ArrowLeft, ChevronDown, Share2, Bookmark, ImageOff, Ban, CalendarDays, CheckCircle2, RotateCcw, Send } from 'lucide-react'
import { toast } from 'sonner'
import { useDetailStore } from '../../store/detailStore'
import { useFeedStore } from '../../store/feedStore'
import { useActionStore } from '../../store/actionStore'
import { useAuthStore } from '../../store/authStore'
import { dismissAction, dispatchAction, fetchAction, markActionDone, setItemStatus, updateAction } from '../../lib/api'
import { cn, eventPlatformName, platformClass, relativeTime, actionTypeName, stripMd } from '../../lib/utils'
import { PlatformBrandIcon } from '../shared/PlatformIcon'
import { requireAuth } from '../shared/AuthGate'
import { VideoPlayer } from './VideoPlayer'
import { YoutubePlayer } from './YoutubePlayer'
import { renderMarkdownInline } from '../../lib/markdown-lite'
import { TranscriptPanel } from './TranscriptPanel'
import { SummaryUpdatedBadge } from './SummaryUpdatedBadge'
import { proxiedImageUrl } from '../../lib/media'
import { buildInfoItemHref, buildInfoItemShareUrl, clearItemDetailHash } from '../../lib/itemDeepLink'
import type { FeedItem, ActionItem, ActionSourceItem, ActionStatus } from '../../lib/types'

const paperSurfaceStyle: CSSProperties = {
  backgroundImage: 'var(--modal-paper-texture)',
  backgroundSize: 'auto, 6px 6px',
}

const mediaCapStyle: CSSProperties = {
  aspectRatio: '16 / 9',
  maxHeight: 'min(180px, calc((var(--app-visual-height) - 32px - var(--modal-bottom-clearance)) * 0.25))',
}

/**
 * Center Stage detail modal (680px / 85vw).
 * Uses detailStore for modal stack + item detail data.
 * Uses feedStore only for toggleStar / markClicked.
 */
export function DetailPanel() {
  // Detail store
  const modalStack = useDetailStore((s) => s.modalStack)
  const closeModal = useDetailStore((s) => s.closeModal)
  const goBack = useDetailStore((s) => s.goBack)
  const setIsLoading = useDetailStore((s) => s.setIsLoading)
  const itemDetail = useDetailStore((s) => s.itemDetail)
  const loadError = useDetailStore((s) => s.loadError)
  const toggleItemStar = useDetailStore((s) => s.toggleItemStar)
  const openItem = useDetailStore((s) => s.openItem)

  // Feed store (only star/click)
  const toggleStar = useFeedStore((s) => s.toggleStar)
  const markClicked = useFeedStore((s) => s.markClicked)

  const [lightboxSrc, setLightboxSrc] = useState<string | null>(null)
  const [lightboxImages, setLightboxImages] = useState<string[]>([])
  const [contentExpanded, setContentExpanded] = useState(false)
  const [isClosing, setIsClosing] = useState(false)
  const [starOverrides, setStarOverrides] = useState<Record<string, string | null>>({})

  const backdropRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const loadedSideDataKeyRef = useRef<string | null>(null)

  const sectionItems = useFeedStore((s) => s.sectionItems)

  const isOpen = modalStack.length > 0
  const topEntry = isOpen ? modalStack[modalStack.length - 1] : null
  const canGoBack = modalStack.length > 1

  // Fallback: look up item from feedStore list data (already loaded, has title/summary/metrics)
  const listItem = topEntry?.type === 'item' && !itemDetail
    ? (() => {
        for (const items of sectionItems.values()) {
          const found = items.find((it) => it.id === topEntry.id)
          if (found) return found
        }
        return null
      })()
    : null

  // The item to render: full detail (from API/cache) or list fallback (instant)
  const item = itemDetail || listItem
  const starOverride = item ? starOverrides[item.id] : undefined
  const displayItem = useMemo(() => {
    if (!item || starOverride === undefined) return item
    return { ...item, starred_at: starOverride ?? undefined }
  }, [item, starOverride])

  // Load item side data when modal stack changes.
  // openItem() owns the item-detail fetch so card clicks do not issue duplicate
  // /api/feed/item/:id requests before the panel can render.
  useEffect(() => {
    if (modalStack.length === 0) {
      loadedSideDataKeyRef.current = null
      return
    }
    const top = modalStack[modalStack.length - 1]
    const sideDataKey = `${top.type}:${top.id}:${modalStack.length}`
    if (loadedSideDataKeyRef.current === sideDataKey) return
    loadedSideDataKeyRef.current = sideDataKey
    if (top.type === 'action') {
      setIsLoading(false)
      return
    }
    if (top.type !== 'item') return

    const id = top.id

    markClicked(id)
    setItemStatus(id, 'clicked').catch(() => {})
  }, [modalStack, markClicked, setIsLoading])

  // Reset expand state when item changes
  useEffect(() => {
    setContentExpanded(false)
  }, [topEntry?.id])

  // Lock background scroll when modal is open. The global scrollbar gutter
  // already reserves width, so adding padding here would shift the app shell.
  useEffect(() => {
    if (!isOpen) return
    const html = document.documentElement
    const prevOverflow = html.style.overflow
    html.style.overflow = 'hidden'
    return () => {
      html.style.overflow = prevOverflow
    }
  }, [isOpen])

  const handleClose = useCallback(() => {
    setIsClosing(true)
    setTimeout(() => {
      closeModal()
      clearItemDetailHash()
      setIsClosing(false)
    }, 180)
  }, [closeModal])

  const navigateAdjacentItem = useCallback((direction: 'previous' | 'next') => {
    if (!item || topEntry?.type !== 'item') return
    const ids: string[] = []
    for (const items of sectionItems.values()) {
      for (const entry of items) ids.push(entry.id)
    }
    const currentIndex = ids.indexOf(item.id)
    if (currentIndex < 0) return
    const nextIndex = direction === 'next' ? currentIndex + 1 : currentIndex - 1
    const nextId = ids[nextIndex]
    if (!nextId) return
    openItem(nextId)
  }, [item, openItem, sectionItems, topEntry?.type])

  // Escape closes; ArrowUp/ArrowDown moves between loaded feed items without leaving the modal.
  useEffect(() => {
    if (!isOpen) return
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (lightboxSrc) {
          setLightboxSrc(null)
        } else {
          handleClose()
        }
      }
      // Lightbox left/right navigation
      if (lightboxSrc && lightboxImages.length > 1) {
        const idx = lightboxImages.indexOf(lightboxSrc)
        if (idx < 0) return
        if (e.key === 'ArrowLeft' && idx > 0) setLightboxSrc(lightboxImages[idx - 1])
        if (e.key === 'ArrowRight' && idx < lightboxImages.length - 1) setLightboxSrc(lightboxImages[idx + 1])
        return
      }
      if (!lightboxSrc && e.key === 'ArrowDown') {
        e.preventDefault()
        navigateAdjacentItem('next')
      }
      if (!lightboxSrc && e.key === 'ArrowUp') {
        e.preventDefault()
        navigateAdjacentItem('previous')
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [handleClose, isOpen, lightboxSrc, lightboxImages, navigateAdjacentItem])

  const handleStar = useCallback(async () => {
    if (!displayItem) return
    if (!requireAuth('收藏', { onLoginClick: handleClose })) return
    const wasStarred = !!displayItem.starred_at
    const nextStarredAt = wasStarred ? null : new Date().toISOString()
    setStarOverrides((prev) => ({ ...prev, [displayItem.id]: nextStarredAt }))
    toggleStar(displayItem.id)
    toggleItemStar()
    try {
      await setItemStatus(displayItem.id, 'starred')
      toast.success(wasStarred ? '已取消收藏' : '收藏成功')
    } catch {
      setStarOverrides((prev) => ({ ...prev, [displayItem.id]: wasStarred ? displayItem.starred_at || new Date().toISOString() : null }))
      toggleStar(displayItem.id) // revert
      toggleItemStar() // revert
      toast.error('操作失败')
    }
  }, [displayItem, handleClose, toggleStar, toggleItemStar])

  // Measure-before-paint: render hidden → layout → show with animation.
  // List data is enough to show the shell immediately; full detail hydrates in
  // place when the remote item request returns.
  const hasDisplayData = !!item || topEntry?.type === 'action'
  const [ready, setReady] = useState(false)
  useLayoutEffect(() => {
    if (isOpen && !isClosing && hasDisplayData && panelRef.current) {
      panelRef.current.getBoundingClientRect()
      setReady(true)
    }
  })
  // Reset ready when modal closes or item changes
  useEffect(() => {
    if (!isOpen) setReady(false)
  }, [isOpen])
  useEffect(() => {
    setReady(false)
  }, [topEntry?.id])

  if (!isOpen) return null

  const animClass = isClosing
    ? 'animate-modal-out'
    : ready ? 'animate-modal-in' : ''
  const backdropAnimClass = isClosing
    ? 'animate-backdrop-out'
    : ready ? 'animate-backdrop-in' : ''
  const displayReady = topEntry?.type === 'action' || ready
  const detailVariant = topEntry?.type === 'item' && displayItem ? getDetailModalVariant(displayItem) : 'no-media'
  const isItemModal = topEntry?.type === 'item'
  const isActionModal = topEntry?.type === 'action'
  const itemPanelClasses = 'modal-viewport-panel flex w-[calc(100vw-24px)] max-w-[720px] flex-col overflow-hidden rounded-[10px] border border-[var(--modal-border)] bg-[var(--modal-surface)] text-[var(--modal-text)] shadow-[var(--modal-shadow)] pointer-events-auto'
  const actionPanelClasses = itemPanelClasses

  return (
    <>
      {/* Backdrop */}
      <div
        ref={backdropRef}
        className={cn(
          'fixed inset-0 z-[900] bg-black/60',
          backdropAnimClass,
          !displayReady && !isClosing && 'opacity-0',
        )}
        onClick={handleClose}
      />

      {/* Centering container — flexbox centering is height-independent */}
      <div
        className="modal-viewport-shell fixed inset-0 z-[901] flex pointer-events-none"
        onClick={handleClose}
      >
      {/* Panel — invisible until layout measured, then animate in */}
      <div
        ref={panelRef}
        data-testid="detail-panel"
        data-modal-theme="editorial"
        data-detail-variant={detailVariant}
        className={cn(
          isActionModal ? actionPanelClasses : itemPanelClasses,
          animClass,
          !displayReady && !isClosing && 'invisible',
        )}
        style={isItemModal || isActionModal ? paperSurfaceStyle : undefined}
        onClick={(e) => e.stopPropagation()}
      >
        {isActionModal && topEntry ? (
          <ActionModalShell
            actionId={topEntry.id}
            canGoBack={canGoBack}
            goBack={goBack}
            handleClose={handleClose}
          />
        ) : (
          <>
            {displayItem && (
              <ItemModalHeader
                item={displayItem}
                canGoBack={canGoBack}
                goBack={goBack}
                handleClose={handleClose}
              />
            )}

            {/* Scrollable content */}
            <div className="flex-1 overflow-y-auto px-4 pb-7 pt-0 sm:px-10 sm:pb-8">
              {displayItem ? (
            <DetailContent
              item={displayItem}
              contentExpanded={contentExpanded}
              setContentExpanded={setContentExpanded}
              setLightboxSrc={setLightboxSrc}
              setLightboxImages={setLightboxImages}
            />
              ) : loadError ? (
                /* UX-3(B8): 失败态 + 重试,替代无限骨架屏 */
                <div className="flex flex-col items-center justify-center gap-3 py-16 text-center" data-testid="detail-error-state">
                  <p className="text-[14px] text-muted-foreground">{loadError}</p>
                  <button
                    type="button"
                    onClick={() => { if (topEntry?.type === 'item') openItem(topEntry.id) }}
                    className="rounded-[4px] border border-border bg-card px-4 py-2 text-[13px] font-medium text-foreground transition-colors hover:border-[var(--brand-border)]"
                  >
                    重试
                  </button>
                </div>
              ) : (
                <DetailSkeleton />
              )}
            </div>

            {displayItem && (
              <DetailFooterActions item={displayItem} handleStar={handleStar} />
            )}
          </>
        )}
      </div>
      </div>

      {/* Lightbox with prev/next navigation */}
      {lightboxSrc && (
        <div
          className="fixed inset-0 z-[950] flex items-center justify-center bg-black/80"
          onClick={() => setLightboxSrc(null)}
        >
          {lightboxImages.length > 1 && (() => {
            const idx = lightboxImages.indexOf(lightboxSrc)
            return (
              <>
                {idx > 0 && (
                  <button
                    className="absolute left-4 top-1/2 -translate-y-1/2 w-10 h-10 flex items-center justify-center rounded-full bg-white/20 hover:bg-white/40 text-white transition-colors"
                    onClick={(e) => { e.stopPropagation(); setLightboxSrc(lightboxImages[idx - 1]) }}
                  >
                    <ArrowLeft className="w-5 h-5" />
                  </button>
                )}
                {idx < lightboxImages.length - 1 && (
                  <button
                    className="absolute right-4 top-1/2 -translate-y-1/2 w-10 h-10 flex items-center justify-center rounded-full bg-white/20 hover:bg-white/40 text-white transition-colors rotate-180"
                    onClick={(e) => { e.stopPropagation(); setLightboxSrc(lightboxImages[idx + 1]) }}
                  >
                    <ArrowLeft className="w-5 h-5" />
                  </button>
                )}
                <span className="absolute bottom-4 left-1/2 -translate-x-1/2 text-sm text-white/70">
                  {idx + 1} / {lightboxImages.length}
                </span>
              </>
            )
          })()}
          <img
            src={proxiedImageUrl(lightboxSrc)}
            alt=""
            className="max-w-[90vw] max-h-[90vh] object-contain rounded-[6px]"
            referrerPolicy="no-referrer"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </>
  )
}

// ── Detail Content ──────────────────────────

function ItemModalHeader({
  item,
  canGoBack,
  goBack,
  handleClose,
}: {
  item: FeedItem
  canGoBack: boolean
  goBack: () => void
  handleClose: () => void
}) {
  const platformLabel = eventPlatformName(item.platform)
  const time = item.published_at || item.fetched_at
  const sourceLabel = item.author_name?.trim() || '来源'

  return (
    <header
      data-testid="detail-modal-header"
      className="shrink-0 bg-[var(--modal-surface)] px-4 py-5 sm:px-10"
      style={paperSurfaceStyle}
    >
      <div className="flex items-start gap-4">
        {canGoBack && (
          <button
            type="button"
            onClick={goBack}
            aria-label="返回上一条"
            title="返回"
            className="mt-0.5 relative flex h-7 w-7 shrink-0 items-center justify-center rounded-[5px] text-[var(--modal-text-faint)] before:absolute before:-inset-2 before:content-[''] transition-colors hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)]"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
          </button>
        )}

        <div className="min-w-0 flex-1">
          <h2
            id="detail-modal-title"
            data-testid="detail-title"
            className="reading-title line-clamp-2"
            title={item.title}
          >
            {item.title}
          </h2>
          <div
            className="reading-meta mt-2 flex min-w-0 items-center gap-2"
            data-testid="detail-source-line"
          >
            <span
              className={cn(
                'inline-flex h-[20px] w-[20px] shrink-0 items-center justify-center rounded-full border-2 border-[var(--modal-surface)] text-[10px] leading-none shadow-[0_1px_2px_rgba(26,25,23,0.16)]',
                platformClass(item.platform),
              )}
              title={platformLabel}
              aria-hidden="true"
            >
              <PlatformBrandIcon platform={item.platform} className="h-[66%] w-[66%]" />
            </span>
            <span className="min-w-0 truncate">{sourceLabel}</span>
            {time && (
              <>
                <span className="shrink-0 text-[var(--modal-text-subtle)]">·</span>
                <time className="shrink-0 font-mono tabular-nums" dateTime={time}>
                  {relativeTime(time)}
                </time>
              </>
            )}
          </div>
        </div>

        <div className="flex w-8 shrink-0 items-center justify-end" data-testid="detail-header-actions">
          <button
            type="button"
            onClick={handleClose}
            aria-label="关闭"
            title="关闭"
            className="relative flex h-7 w-7 items-center justify-center rounded-[5px] before:absolute before:-inset-2 before:content-[''] text-[var(--modal-text-faint)] transition-colors hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)]"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </header>
  )
}

function ActionModalHeader({
  action,
  canGoBack,
  goBack,
  handleClose,
}: {
  action: ActionItem
  canGoBack: boolean
  goBack: () => void
  handleClose: () => void
}) {
  const priorityLabel = action.priority || ''
  const statusLabel = ACTION_STATUS_LABELS[action.status] || action.status

  return (
    <header
      data-testid="detail-modal-header"
      className="shrink-0 border-b border-[var(--modal-divider)] bg-[var(--modal-surface)] px-6 py-5 sm:px-10"
      style={paperSurfaceStyle}
    >
      <div className="flex items-start gap-4">
        {canGoBack && (
          <button
            type="button"
            onClick={goBack}
            aria-label="返回上一条"
            title="返回"
            className="mt-0.5 relative flex h-7 w-7 shrink-0 items-center justify-center rounded-[5px] text-[var(--modal-text-faint)] before:absolute before:-inset-2 before:content-[''] transition-colors hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)]"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
          </button>
        )}

        <div className="min-w-0 flex-1">
          <h2
            id="detail-modal-title"
            data-testid="action-modal-title"
            className="reading-title line-clamp-2"
            title={action.title}
          >
            {action.title}
          </h2>
          <div
            data-testid="action-modal-meta"
            className="reading-meta mt-3 flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1"
          >
            <span className="font-semibold text-[var(--brand)]">{actionTypeName(action.type)}</span>
            {priorityLabel && (
              <>
                <span className="text-[var(--modal-text-subtle)]" aria-hidden="true">·</span>
                <span className={cn('font-semibold', priorityLabel === 'P0' ? 'text-[#D94B45]' : 'text-[var(--modal-text-muted)]')}>
                  {priorityLabel}
                </span>
              </>
            )}
            <span className="text-[var(--modal-text-subtle)]" aria-hidden="true">·</span>
            <span className="font-semibold text-[var(--brand)]">{statusLabel}</span>
            <span className="text-[var(--modal-text-subtle)]" aria-hidden="true">·</span>
            <span className="inline-flex min-w-0 items-center gap-1.5 text-[var(--modal-text-muted)]">
              <CalendarDays className="h-3.5 w-3.5 shrink-0" />
              <time className="font-mono tabular-nums" dateTime={action.created_at}>
                {formatActionAbsoluteTime(action.created_at)}
              </time>
            </span>
          </div>
        </div>

        <div className="flex w-8 shrink-0 items-center justify-end" data-testid="detail-header-actions">
          <button
            type="button"
            onClick={handleClose}
            aria-label="关闭"
            title="关闭"
            className="relative flex h-8 w-8 items-center justify-center rounded-[5px] before:absolute before:-inset-1.5 before:content-[''] border border-[var(--modal-border-soft)] bg-[var(--modal-surface-soft)] text-[var(--modal-text-faint)] transition-colors hover:border-[var(--brand-border)] hover:bg-[var(--modal-hover-soft)] hover:text-[var(--modal-text)]"
          >
            <X className="h-5 w-5" />
          </button>
        </div>
      </div>
    </header>
  )
}

function compactShareSummary(summary?: string | null): string {
  const normalized = normalizeSummaryText(summary)?.replace(/\s+/g, ' ').trim() || ''
  if (!normalized) return ''
  return normalized.length > 100 ? `${normalized.slice(0, 100)}...` : normalized
}

function buildItemShareText(item: FeedItem): string {
  const title = item.title?.trim() || '一条信息'
  const summary = compactShareSummary(item.ai_summary)
  const itemDeepLink = buildInfoItemShareUrl(item.id)
  return `我正在 info2act 浏览「${title}」：${summary}\n一起看看吧 ${itemDeepLink}`
}

async function copyTextToClipboard(text: string): Promise<void> {
  if (copyTextWithTextarea(text)) return

  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }

  throw new Error('copy failed')
}

function copyTextWithTextarea(text: string): boolean {
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.left = '0'
  textarea.style.top = '0'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.focus()
  textarea.select()
  textarea.setSelectionRange(0, text.length)
  try {
    return Boolean(document.execCommand?.('copy'))
  } finally {
    document.body.removeChild(textarea)
  }
}

function DetailFooterActions({ item, handleStar }: { item: FeedItem; handleStar: () => void }) {
  const bottomActionClass = 'flex h-12 w-full items-center justify-center gap-1.5 text-[13px] font-medium text-[var(--modal-text-muted)] transition-colors hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-inset'
  const handleShare = useCallback(async () => {
    try {
      await copyTextToClipboard(buildItemShareText(item))
      toast.success('分享链接已复制')
    } catch {
      toast.error('分享失败')
    }
  }, [item])

  return (
    <div
      className={cn(
        'modal-safe-footer grid flex-shrink-0 overflow-hidden border-t border-[var(--modal-border-soft)] bg-[var(--modal-surface)]',
        // E2: 无原文链接时用两列均分收藏/分享,不再留中间死区占位
        item.url ? 'grid-cols-3' : 'grid-cols-2',
      )}
      data-testid="detail-bottom-actions"
      style={paperSurfaceStyle}
    >
      <button
        type="button"
        onClick={handleStar}
        data-testid="detail-footer-star-button"
        className={cn(bottomActionClass, item.starred_at && 'text-[var(--brand)]')}
      >
        <Bookmark className={cn('h-3.5 w-3.5', item.starred_at && 'fill-current')} />
        {item.starred_at ? '已收藏' : '收藏'}
      </button>
      {item.url && (
        <a
          href={item.url}
          target="_blank"
          rel="noopener noreferrer"
          data-testid="detail-footer-original-link"
          className={bottomActionClass}
          aria-label="跳转原文"
        >
          <ExternalLink className="h-3.5 w-3.5" />
          跳转原文
        </a>
      )}
      <button type="button" onClick={handleShare} data-testid="detail-footer-share-button" className={bottomActionClass}>
        <Share2 className="h-3.5 w-3.5" />
        分享
      </button>
    </div>
  )
}

interface DetailContentProps {
  item: FeedItem
  contentExpanded: boolean
  setContentExpanded: (v: boolean) => void
  setLightboxSrc: (v: string | null) => void
  setLightboxImages: (v: string[]) => void
}

function DetailContent({
  item,
  contentExpanded,
  setContentExpanded,
  setLightboxSrc,
  setLightboxImages,
}: DetailContentProps) {
  // Collect all images from media_json + cover_url + thumbnail
  const images = collectImages(item)
  // v12.2: 检测是否为视频帖 (media_json 首个 item type === 'video')
  const videoMp4Url = extractVideoMp4Url(item)
  // BF-0419-20: YouTube 走 iframe embed,video_id 来自 item.id (yt_{id})
  const youtubeVideoId = item.platform === 'youtube' && item.id.startsWith('yt_')
    ? item.id.slice(3)
    : null
  const hasVideo = !!videoMp4Url || !!youtubeVideoId
  const content = item.content || item.description || ''
  const TRUNCATE_LEN = 1000
  const needsTruncation = content.length > TRUNCATE_LEN
  const displayContent = contentExpanded ? content : content.slice(0, TRUNCATE_LEN)
  const hasSummaryBlock = !!normalizeSummaryText(item.ai_summary) || !!item.ai_key_points?.length

  // v12.2 ASR state for SummaryUpdatedBadge + failed_summary banner
  const asrSummaryUpdated = useDetailStore((s) => s.asrSummaryUpdated)
  const clearSummaryBadge = useDetailStore((s) => s.clearSummaryBadge)
  const retrySummary = useDetailStore((s) => s.retrySummary)
  const isSummaryFailed = item.asr_status === 'failed_summary'

  return (
    <>
      {/* 1. Media first for v19 single/multi-image modal variants; no-image keeps this area empty. */}
      {videoMp4Url && (
        <VideoPlayer mp4Url={videoMp4Url} itemId={item.id} />
      )}

      {!videoMp4Url && youtubeVideoId && (
        <YoutubePlayer videoId={youtubeVideoId} itemId={item.id} />
      )}

      {hasVideo && (
        <TranscriptPanel itemId={item.id} item={item} />
      )}

      {!hasVideo && images.length > 0 && (
        <ImageGrid images={images} onClickImage={(src) => { setLightboxImages(images); setLightboxSrc(src) }} />
      )}

      {/* 3a. v12.2: SummaryUpdatedBadge (ASR 刚刷新摘要后短暂显示) */}
      {asrSummaryUpdated && (
        <SummaryUpdatedBadge onExpired={clearSummaryBadge} />
      )}

      {/* 3b. v12.2: failed_summary 降级 banner */}
      {isSummaryFailed && (
        <div
          role="alert"
          className="flex items-center justify-between gap-2 px-3 py-2 mb-2 rounded-lg text-[13px]"
          style={{
            background: 'rgb(251 191 36 / 0.1)',
            border: '1px solid rgb(251 191 36 / 0.4)',
            color: 'rgb(180 83 9)',
          }}
        >
          <span>⚠️ 转写已就绪,摘要暂未更新</span>
          <button
            onClick={() => retrySummary(item.id)}
            className="text-primary hover:underline text-sm font-medium"
          >
            重试摘要
          </button>
        </div>
      )}

      <ItemSummaryBlock
        summary={item.ai_summary}
        keyPoints={item.ai_key_points}
      />

      {/* 4. Original content */}
      {content && (
        <div
          className={cn('mb-4', hasSummaryBlock && 'border-t border-[var(--modal-divider)] pt-4')}
          data-testid="detail-body-content"
        >
          <div
            data-testid="detail-body-text"
            className="reading-body space-y-2.5"
            style={{ wordBreak: 'break-word' }}
          >
            {displayContent.split('\n').map((line, i) => (
              <p key={i}>
                {i === 0 && (
                  <strong data-testid="detail-original-label" className="mr-1.5 !text-[var(--brand)]">
                    原文：
                  </strong>
                )}
                {line || '\u00A0'}
              </p>
            ))}
            {needsTruncation && !contentExpanded && <span className="text-[var(--modal-text-faint)]">...</span>}
          </div>
          {needsTruncation && (
            <button
              onClick={() => setContentExpanded(!contentExpanded)}
              className="mt-2 flex items-center gap-1 text-sm font-medium text-[var(--brand)] transition-colors hover:text-[var(--brand)]"
            >
              {contentExpanded ? '收起' : '展开全文'}
              <ChevronDown className={cn('w-3 h-3 transition-transform', contentExpanded && 'rotate-180')} />
            </button>
          )}
        </div>
      )}

    </>
  )
}

// ── Image Grid ──────────────────────────

type KeyPointItem = string | { title: string; points: string[] }

function normalizeSummaryText(summary?: string | null): string | null {
  const value = summary
    ?.replace(/^【精华速览】\s*/, '')
    .replace(/^精华速览[:：]\s*/, '')
    .trim()
  return value || null
}

function paragraphLines(text: string): string[] {
  return text
    .split(/\n{2,}/)
    .map((line) => line.trim())
    .filter(Boolean)
}

function ItemSummaryBlock({
  summary,
  keyPoints,
}: {
  summary?: string | null
  keyPoints?: KeyPointItem[] | null
}) {
  const normalizedSummary = normalizeSummaryText(summary)
  const hasPoints = !!keyPoints?.length
  if (!normalizedSummary && !hasPoints) return null

  return (
    <section
      data-testid="detail-ai-summary"
      className="mb-5 text-[var(--modal-text-soft)]"
    >
      {normalizedSummary && (
        <div
          data-testid="detail-summary-lead"
          className={cn(
            'reading-body pb-5',
            hasPoints && 'border-b border-[var(--modal-divider)]',
          )}
        >
          {paragraphLines(normalizedSummary).map((line, index) => (
            <p key={index} className={index > 0 ? 'mt-2.5' : undefined}>
              {index === 0 && (
                <strong className="mr-1.5 !text-[var(--brand)]">精华速览：</strong>
              )}
              {renderMarkdownInline(line)}
            </p>
          ))}
        </div>
      )}

      {hasPoints && (
        <ul data-testid="detail-key-points" className={cn(
          'reading-bullet space-y-1 py-4 pl-6 sm:pl-[38px]',
        )}>
          {(keyPoints ?? []).map((point, index) => {
            if (typeof point === 'string') {
              return (
                <li key={index} className="relative">
                  <span className="absolute -left-3.5 top-[0.78em] h-1 w-1 rounded-full bg-[var(--modal-text)] sm:-left-4" aria-hidden="true" />
                  {renderMarkdownInline(point)}
                </li>
              )
            }
            return (
              <li key={index} className="relative -ml-6 pb-3.5 last:pb-0 sm:-ml-[38px]">
                <div className="mb-2 flex items-baseline gap-2.5">
                  <span className="reading-section leading-none text-[var(--brand)]">
                    {String(index + 1).padStart(2, '0')}
                  </span>
                  <div className="reading-section min-w-0">
                    {renderMarkdownInline(point.title)}
                  </div>
                </div>
                {point.points?.length > 0 && (
                  <ul className="reading-bullet space-y-1 pl-6 sm:pl-[38px]">
                    {point.points.map((subPoint, subIndex) => (
                      <li key={subIndex} className="relative">
                        <span className="absolute -left-3.5 top-[0.78em] h-1 w-1 rounded-full bg-[var(--modal-text)] sm:-left-4" aria-hidden="true" />
                        {renderMarkdownInline(subPoint)}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

function collectImages(item: FeedItem): string[] {
  const urls: string[] = []
  if (item.cover_url) urls.push(item.cover_url)
  if (item.media_json) {
    for (const m of item.media_json) {
      const u = typeof m === 'string' ? m : m?.url
      if (u && !urls.includes(u)) urls.push(u)
    }
  }
  if (item.thumbnail && !urls.includes(item.thumbnail)) urls.push(item.thumbnail)
  return urls
}

function getDetailModalVariant(item: FeedItem): 'no-media' | 'single-media' | 'multi-media' {
  if (extractVideoMp4Url(item) || (item.platform === 'youtube' && item.id.startsWith('yt_'))) return 'single-media'
  const imageCount = collectImages(item).length
  if (imageCount <= 0) return 'no-media'
  if (imageCount === 1) return 'single-media'
  return 'multi-media'
}

// v12.2: 检测视频帖并返回 mp4 直链 (否则 null)
function extractVideoMp4Url(item: FeedItem): string | null {
  if (!item.media_json) return null
  for (const m of item.media_json) {
    if (typeof m === 'object' && m !== null) {
      const maybeVideo = m as { type?: string; url?: string }
      if (maybeVideo.type === 'video' && maybeVideo.url) return maybeVideo.url
    }
  }
  return null
}

/** v12.3 BF-0418-XIMG: image onError fallback avoids broken browser icons. */
function GridImage({ src }: { src: string }) {
  const [err, setErr] = useState(false)
  if (err) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-[var(--modal-surface-muted)] text-[var(--modal-text-faint)]">
        <ImageOff className="w-6 h-6" />
      </div>
    )
  }
  return (
    <img
      src={src}
      alt=""
      className="h-full w-full cursor-zoom-in object-cover transition-opacity hover:opacity-95"
      onError={() => setErr(true)}
      loading="lazy"
      referrerPolicy="no-referrer"
    />
  )
}

function ImageGrid({ images, onClickImage }: { images: string[]; onClickImage: (src: string) => void }) {
  const primaryUrl = images[0]
  const extraCount = Math.max(0, images.length - 1)

  return (
    <figure className="mb-6">
      <button
        type="button"
        data-testid="detail-media-grid"
        data-media-count={String(images.length)}
        data-media-layout={images.length === 1 ? 'single' : 'stacked'}
        aria-label="放大查看图片"
        onClick={() => onClickImage(primaryUrl)}
        style={mediaCapStyle}
        className="group/media relative block w-full overflow-hidden rounded-[8px] border border-[var(--modal-border)] bg-[var(--modal-surface-muted)] p-0 text-left shadow-[0_1px_0_rgba(255,255,255,0.72)]"
      >
        <GridImage src={proxiedImageUrl(primaryUrl)} />
        {extraCount > 0 && (
          <span className="absolute bottom-2 right-2 rounded-full border border-white/35 bg-black/70 px-2 py-0.5 font-mono text-[11px] font-semibold leading-none text-white backdrop-blur-sm">
            +{extraCount}
          </span>
        )}
      </button>
    </figure>
  )
}

// ── Loading Skeleton ──────────────────────────

function DetailSkeleton() {
  return (
    <div className="space-y-4 animate-pulse">
      {/* Image skeleton */}
      <div className="w-full h-48 rounded-lg bg-muted animate-skeleton" />
      {/* Meta line */}
      <div className="flex gap-2">
        <div className="w-16 h-5 rounded bg-muted animate-skeleton" />
        <div className="w-10 h-5 rounded bg-muted animate-skeleton" />
        <div className="w-20 h-5 rounded bg-muted animate-skeleton" />
      </div>
      {/* Title */}
      <div className="w-3/4 h-7 rounded bg-muted animate-skeleton" />
      {/* Author */}
      <div className="flex items-center gap-3">
        <div className="w-9 h-9 rounded-full bg-muted animate-skeleton" />
        <div className="w-24 h-4 rounded bg-muted animate-skeleton" />
      </div>
      <div className="h-px bg-border" />
      {/* Summary */}
      <div className="w-full h-20 rounded-lg bg-muted animate-skeleton" />
      {/* Content */}
      <div className="space-y-2">
        <div className="w-full h-4 rounded bg-muted animate-skeleton" />
        <div className="w-5/6 h-4 rounded bg-muted animate-skeleton" />
        <div className="w-2/3 h-4 rounded bg-muted animate-skeleton" />
        <div className="w-3/4 h-4 rounded bg-muted animate-skeleton" />
      </div>
    </div>
  )
}

// ── Action Detail Content (for action-type modal) ──────────────────────────

const ACTION_STATUS_LABELS: Record<ActionStatus, string> = {
  pending: '待处理',
  confirmed: '执行中',
  executing: '执行中',
  dispatched: '执行中',
  done: '已完成',
  failed: '失败',
  dismissed: '已忽略',
  ignored: '已忽略',
}

function hasCompleteActionDetailPayload(action: ActionItem | null): boolean {
  if (!action) return false
  const maybeWithSteps = action as ActionItem & { steps?: unknown }
  return (
    Array.isArray(maybeWithSteps.steps) &&
    Array.isArray(action.source_items) &&
    typeof action.source_item_count === 'number'
  )
}

function ActionModalShell({
  actionId,
  canGoBack,
  goBack,
  handleClose,
}: {
  actionId: string
  canGoBack: boolean
  goBack: () => void
  handleClose: () => void
}) {
  const updateActionInStore = useActionStore((s) => s.updateAction)
  const detailAction = useDetailStore((s) => (s.actionDetail?.id === actionId ? s.actionDetail : null))
  const setActionDetail = useDetailStore((s) => s.setActionDetail)
  const [fetchedAction, setFetchedAction] = useState<ActionItem | null>(null)
  const [fetchError, setFetchError] = useState(false)

  const action = fetchedAction ?? (hasCompleteActionDetailPayload(detailAction) ? detailAction : null)

  useEffect(() => {
    let cancelled = false
    setFetchedAction(null)
    setFetchError(false)
    if (hasCompleteActionDetailPayload(detailAction)) {
      return () => {
        cancelled = true
      }
    }
    fetchAction(String(actionId))
      .then((data) => {
        if (!cancelled && data) {
          setFetchedAction(data)
          setActionDetail(data)
          updateActionInStore(data.id, data)
        }
        if (!cancelled && !data) setFetchError(true)
      })
      .catch(() => {
        if (!cancelled) setFetchError(true)
      })
    return () => {
      cancelled = true
    }
  }, [actionId, detailAction, setActionDetail, updateActionInStore])

  const patchAction = useCallback((patch: Partial<ActionItem>) => {
    setFetchedAction((prev) => ({ ...(prev ?? action), ...patch }) as ActionItem)
    if (action) setActionDetail({ ...action, ...patch } as ActionItem)
    updateActionInStore(actionId, patch)
  }, [action, actionId, setActionDetail, updateActionInStore])

  if (!action && fetchError) {
    return (
      <div className="flex min-h-[220px] flex-1 items-center justify-center px-6 py-12 text-sm text-destructive">
        行动点未找到或加载失败
      </div>
    )
  }

  if (!action) return <ActionModalLoading />

  return (
    <>
      <ActionModalHeader
        action={action}
        canGoBack={canGoBack}
        goBack={goBack}
        handleClose={handleClose}
      />
      <div className="flex-1 overflow-y-auto px-6 pb-7 pt-0 sm:px-10 sm:pb-8">
        <ActionDetailContent action={action} />
      </div>
      <ActionFooter action={action} onPatchAction={patchAction} />
    </>
  )
}

function ActionModalLoading() {
  return (
    <div className="flex flex-1 flex-col">
      <div className="shrink-0 border-b border-[var(--modal-divider)] px-6 py-5 sm:px-10">
        <div className="h-6 w-3/4 animate-skeleton rounded bg-[var(--modal-divider)]" />
        <div className="mt-3 h-4 w-1/2 animate-skeleton rounded bg-[var(--modal-hover)]" />
      </div>
      <div className="flex-1 space-y-4 px-6 py-7 sm:px-10">
        <div className="h-4 w-20 animate-skeleton rounded bg-[var(--modal-hover)]" />
        <div className="h-4 w-full animate-skeleton rounded bg-[var(--modal-hover)]" />
        <div className="h-4 w-5/6 animate-skeleton rounded bg-[var(--modal-hover)]" />
        <div className="h-16 w-full animate-skeleton rounded bg-[var(--modal-hover)]" />
      </div>
    </div>
  )
}

/** Parse reasoning: JSON array of {label, text} or plain string */
function ReasoningBlock({ text }: { text: string }) {
  const normalized = text.trim()
  if (normalized.startsWith('[')) {
    try {
      const parsed = JSON.parse(normalized) as Array<{ label?: string; text?: string }>
      if (Array.isArray(parsed) && parsed.length > 0 && parsed[0].text) {
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
    } catch { /* not JSON */ }
  }
  return <p>{renderMarkdownInline(normalized)}</p>
}

function ActionDetailContent({ action }: { action: ActionItem }) {
  const actionPointItems = getActionPointItems(action)
  const sourceItems = getActionSourceItems(action)
  const decisionReason = action.ai_reasoning || action.reason || action.decision_brief || ''

  return (
    <div className="pt-7">
      {actionPointItems.length > 0 && (
        <section data-testid="action-modal-points" className="mb-6">
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

      {decisionReason.trim() && (
        <section data-testid="action-modal-reason" className="mb-6 border-t border-[var(--modal-divider)] pt-4">
          <h3 className="reading-section mb-3 leading-none text-[var(--brand)]">
            决策理由
          </h3>
          <div className="reading-body">
            <ReasoningBlock text={decisionReason} />
          </div>
        </section>
      )}

      {sourceItems.length > 0 && (
        <section data-testid="action-modal-sources" className="border-t border-[var(--modal-divider)] pt-4">
          <h3 className="reading-section mb-3 leading-none text-[var(--brand)]">
            关联信息
          </h3>
          <div className="space-y-2">
            {sourceItems.map((source) => (
              <ActionSourceRow key={source.id} source={source} />
            ))}
          </div>
        </section>
      )}
    </div>
  )
}

function ActionSourceRow({ source }: { source: ActionSourceItem }) {
  const platform = source.platform || 'rss'
  const platformLabel = eventPlatformName(platform)
  const displayTitle = getActionSourceDisplayTitle(source)
  const label = normalizeActionSourceLabel(displayTitle)
  const href = buildInfoItemHref(source.id)
  if (!displayTitle) return null

  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      data-testid="action-modal-source-row"
      className={cn(
        'group block w-full rounded-[7px] border border-[var(--modal-border-soft)] bg-[var(--modal-surface-soft)] px-3 py-2.5 text-left transition-colors',
        'hover:border-[var(--brand-border)] hover:bg-[var(--modal-hover-soft)]',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--modal-surface)]',
      )}
      aria-label={`打开关联信息: ${label}`}
      title="打开关联信息"
    >
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
    </a>
  )
}

function ActionFooter({
  action,
  onPatchAction,
}: {
  action: ActionItem
  onPatchAction: (patch: Partial<ActionItem>) => void
}) {
  const canDispatch = useAuthStore((s) => s.user?.has_discord_token ?? false)
  const [busy, setBusy] = useState<'left' | 'main' | null>(null)
  const isRestorable = action.status === 'dismissed' || action.status === 'ignored' || action.status === 'failed'
  const leftLabel = isRestorable ? '恢复' : '忽略'
  const LeftIcon = isRestorable ? RotateCcw : Ban
  const mainAction = getFooterMainAction(action, canDispatch)

  const runFooterAction = async (kind: 'left' | 'main') => {
    if (busy) return
    setBusy(kind)
    try {
      if (kind === 'left') {
        if (isRestorable) {
          await updateAction(action.id, { status: 'pending' })
          onPatchAction({ status: 'pending' })
          toast.success('已恢复待处理')
        } else {
          await dismissAction(action.id)
          onPatchAction({ status: 'dismissed' })
          toast.success('已忽略')
        }
        return
      }

      if (mainAction.kind === 'dispatch') {
        await dispatchAction(action.id)
        onPatchAction({ status: 'dispatched' })
        toast.success('已进入执行中')
        return
      }
      if (mainAction.kind === 'done') {
        await markActionDone(action.id)
        onPatchAction({ status: 'done' })
        toast.success('已完成')
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '操作失败')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div
      data-testid="action-modal-footer"
      className="modal-safe-footer grid shrink-0 grid-cols-2 overflow-hidden border-t border-[var(--modal-border-soft)] bg-[var(--modal-surface)]"
      style={paperSurfaceStyle}
    >
      <button
        type="button"
        onClick={() => runFooterAction('left')}
        disabled={busy !== null}
        className="flex h-14 w-full items-center justify-center gap-2 text-[15px] font-medium text-[var(--modal-text-muted)] transition-colors hover:bg-[var(--modal-hover)] hover:text-[var(--modal-text)] disabled:cursor-not-allowed disabled:opacity-50"
      >
        <LeftIcon className="h-4 w-4" />
        {busy === 'left' ? '处理中' : leftLabel}
      </button>
      <button
        type="button"
        onClick={() => runFooterAction('main')}
        disabled={busy !== null || mainAction.disabled}
        title={mainAction.title}
        className={cn(
          'flex h-14 w-full items-center justify-center gap-2 border-l border-[var(--modal-border-soft)] text-[15px] font-semibold transition-colors',
          mainAction.disabled
            ? 'cursor-not-allowed text-[var(--modal-text-faint)] opacity-65'
            : 'text-[var(--brand)] hover:bg-[var(--modal-hover-soft)] hover:text-[var(--brand)]',
        )}
      >
        <mainAction.icon className="h-4 w-4" />
        {busy === 'main' ? '处理中' : mainAction.label}
      </button>
    </div>
  )
}

function getFooterMainAction(action: ActionItem, canDispatch: boolean): {
  label: string
  kind: 'dispatch' | 'done' | 'none'
  icon: typeof Send
  disabled: boolean
  title?: string
} {
  if (action.status === 'pending') {
    return {
      label: canDispatch ? '派发' : '配置 Token',
      kind: canDispatch ? 'dispatch' : 'none',
      icon: Send,
      disabled: !canDispatch,
      title: canDispatch ? undefined : '请先配置 Discord Bot Token',
    }
  }
  if (action.status === 'confirmed' || action.status === 'executing' || action.status === 'dispatched') {
    return { label: '完成', kind: 'done', icon: CheckCircle2, disabled: false }
  }
  if (action.status === 'done') {
    return { label: '已完成', kind: 'none', icon: CheckCircle2, disabled: true }
  }
  return { label: '恢复后处理', kind: 'none', icon: RotateCcw, disabled: true }
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

function formatActionAbsoluteTime(value?: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
