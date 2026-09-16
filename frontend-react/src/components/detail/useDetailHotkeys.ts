import { useEffect } from 'react'
import type { Dispatch, SetStateAction } from 'react'

export function useBodyScrollLock(isOpen: boolean) {
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
}

export function useDetailHotkeys({
  isOpen,
  lightboxSrc,
  setLightboxSrc,
  lightboxImages,
  handleClose,
  navigateAdjacentItem,
}: {
  isOpen: boolean
  lightboxSrc: string | null
  setLightboxSrc: Dispatch<SetStateAction<string | null>>
  lightboxImages: string[]
  handleClose: () => void
  navigateAdjacentItem: (direction: 'previous' | 'next') => void
}) {
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
}
