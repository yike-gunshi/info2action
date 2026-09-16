import { useState, useCallback, useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import { toast } from 'sonner'
import { useDetailStore } from '../../store/detailStore'
import { useFeedStore } from '../../store/feedStore'
import { setItemStatus } from '../../lib/api'
import { cn } from '../../lib/utils'
import { requireAuth } from '../shared/AuthGate'
import { clearItemDetailHash } from '../../lib/itemDeepLink'
import { ActionModalShell } from './ActionModalShell'
import { DetailContent } from './DetailContent'
import { DetailFooterActions } from './DetailFooterActions'
import { DetailLightbox } from './DetailLightbox'
import { DetailSkeleton } from './DetailSkeleton'
import { ItemModalHeader } from './ItemModalHeader'
import { getDetailModalVariant } from './detailContentUtils'
import { paperSurfaceStyle } from './detailShared'
import { useBodyScrollLock, useDetailHotkeys } from './useDetailHotkeys'
import { useLightboxState } from './useLightboxState'

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

  const [lightboxSrc, setLightboxSrc, lightboxImages, setLightboxImages] = useLightboxState()
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

  useBodyScrollLock(isOpen)

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

  useDetailHotkeys({
    isOpen,
    lightboxSrc,
    setLightboxSrc,
    lightboxImages,
    handleClose,
    navigateAdjacentItem,
  })

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
        <DetailLightbox
          src={lightboxSrc}
          images={lightboxImages}
          onClose={() => setLightboxSrc(null)}
          onNavigate={setLightboxSrc}
        />
      )}
    </>
  )
}
