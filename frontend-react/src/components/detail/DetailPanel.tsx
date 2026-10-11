import { useState, useCallback, useEffect, useMemo, useRef } from 'react'
import { toast } from 'sonner'
import { useDetailStore } from '../../store/detailStore'
import { useFeedStore } from '../../store/feedStore'
import { setItemStatus } from '../../lib/api'
import { requireAuth } from '../shared/AuthGate'
import { ReadingLightbox } from '../reading/ReadingLightbox'
import {
  ReadingModalBody,
  ReadingModalError,
  ReadingModalLoading,
  ReadingModalShell,
} from '../reading/ReadingModal'
import { clearItemDetailHash } from '../../lib/itemDeepLink'
import { ActionModalShell } from './ActionModalShell'
import { DetailContent } from './DetailContent'
import { DetailFooterActions } from './DetailFooterActions'
import { ItemModalHeader } from './ItemModalHeader'
import { getDetailModalVariant } from './detailContentUtils'
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

  useEffect(() => {
    if (!isOpen || lightboxSrc) return
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'ArrowDown') {
        event.preventDefault()
        navigateAdjacentItem('next')
      } else if (event.key === 'ArrowUp') {
        event.preventDefault()
        navigateAdjacentItem('previous')
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [isOpen, lightboxSrc, navigateAdjacentItem])

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

  if (!isOpen) return null

  const detailVariant = topEntry?.type === 'item' && displayItem ? getDetailModalVariant(displayItem) : 'no-media'
  const isActionModal = topEntry?.type === 'action'

  return (
    <>
      <ReadingModalShell
        ariaLabelledby="detail-modal-title"
        dataAttributes={{ 'data-detail-variant': detailVariant }}
        escapeDisabled={!!lightboxSrc}
        isClosing={isClosing}
        onClose={handleClose}
        panelTestId="detail-panel"
      >
        {isActionModal && topEntry ? (
          <ActionModalShell
            actionId={topEntry.id}
            canGoBack={canGoBack}
            goBack={goBack}
            handleClose={handleClose}
          />
        ) : displayItem ? (
          <>
            <ItemModalHeader
              item={displayItem}
              canGoBack={canGoBack}
              goBack={goBack}
              handleClose={handleClose}
            />

            <ReadingModalBody resetKey={topEntry?.id} testId="detail-modal-scroll">
              <DetailContent
                item={displayItem}
                contentExpanded={contentExpanded}
                setContentExpanded={setContentExpanded}
                setLightboxSrc={setLightboxSrc}
                setLightboxImages={setLightboxImages}
              />
            </ReadingModalBody>

            <DetailFooterActions item={displayItem} handleStar={handleStar} />
          </>
        ) : loadError ? (
          <ReadingModalError
            headerTestId="detail-modal-header"
            message="加载失败，请重试"
            onClose={handleClose}
            onRetry={() => { if (topEntry?.type === 'item') openItem(topEntry.id) }}
            testId="detail-error-state"
            title="信息详情"
            titleId="detail-modal-title"
          />
        ) : (
          <ReadingModalLoading
            headerTestId="detail-modal-header"
            onClose={handleClose}
            testId="detail-modal-loading"
            title="信息详情"
            titleId="detail-modal-title"
          />
        )}
      </ReadingModalShell>

      {lightboxSrc && (
        <ReadingLightbox
          src={lightboxSrc}
          images={lightboxImages}
          onClose={() => setLightboxSrc(null)}
          onNavigate={setLightboxSrc}
          testId="detail-lightbox"
        />
      )}
    </>
  )
}
