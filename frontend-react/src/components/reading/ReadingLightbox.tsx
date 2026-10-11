import { useEffect } from 'react'
import { ChevronLeft, ChevronRight, X } from 'lucide-react'
import { proxiedImageUrl } from '../../lib/media'

export function ReadingLightbox({
  images,
  onClose,
  onNavigate,
  src,
  testId,
}: {
  images: string[]
  onClose: () => void
  onNavigate: (src: string) => void
  src: string
  testId?: string
}) {
  const index = images.indexOf(src)
  const hasNavigation = images.length > 1 && index >= 0

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
      } else if (event.key === 'ArrowLeft' && hasNavigation && index > 0) {
        event.preventDefault()
        onNavigate(images[index - 1])
      } else if (event.key === 'ArrowRight' && hasNavigation && index < images.length - 1) {
        event.preventDefault()
        onNavigate(images[index + 1])
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [hasNavigation, images, index, onClose, onNavigate])

  return (
    <div
      data-testid={testId}
      className="fixed inset-0 z-[950] flex items-center justify-center bg-black/85 p-4"
      onClick={(event) => {
        event.stopPropagation()
        onClose()
      }}
    >
      <button
        type="button"
        aria-label="关闭图片预览"
        className="absolute right-5 top-5 z-10 flex h-9 w-9 items-center justify-center rounded-full bg-black/50 text-white shadow-lg backdrop-blur-sm transition-colors hover:bg-black/65"
        onClick={(event) => {
          event.stopPropagation()
          onClose()
        }}
      >
        <X className="h-[18px] w-[18px]" />
      </button>

      {hasNavigation && index > 0 && (
        <button
          type="button"
          aria-label="上一张图片"
          title="上一张图片"
          className="group absolute left-4 top-1/2 z-10 flex h-14 w-14 -translate-y-1/2 items-center justify-center rounded-full text-white outline-none transition-colors focus-visible:ring-2 focus-visible:ring-white/70"
          onClick={(event) => {
            event.stopPropagation()
            onNavigate(images[index - 1])
          }}
        >
          <span className="flex h-11 w-11 items-center justify-center rounded-full bg-black/50 shadow-lg backdrop-blur-sm transition-colors group-hover:bg-black/65 group-focus-visible:bg-black/65">
            <ChevronLeft className="h-6 w-6" />
          </span>
        </button>
      )}

      {hasNavigation && index < images.length - 1 && (
        <button
          type="button"
          aria-label="下一张图片"
          title="下一张图片"
          className="group absolute right-4 top-1/2 z-10 flex h-14 w-14 -translate-y-1/2 items-center justify-center rounded-full text-white outline-none transition-colors focus-visible:ring-2 focus-visible:ring-white/70"
          onClick={(event) => {
            event.stopPropagation()
            onNavigate(images[index + 1])
          }}
        >
          <span className="flex h-11 w-11 items-center justify-center rounded-full bg-black/50 shadow-lg backdrop-blur-sm transition-colors group-hover:bg-black/65 group-focus-visible:bg-black/65">
            <ChevronRight className="h-6 w-6" />
          </span>
        </button>
      )}

      {hasNavigation && (
        <span className="absolute bottom-4 left-1/2 z-10 -translate-x-1/2 rounded-full bg-black/40 px-3 py-1 font-mono text-[12px] text-white/80">
          {index + 1} / {images.length}
        </span>
      )}

      <img
        src={proxiedImageUrl(src)}
        alt=""
        referrerPolicy="no-referrer"
        className="max-h-[90vh] max-w-[92vw] rounded-[6px] object-contain"
        onClick={(event) => event.stopPropagation()}
      />
    </div>
  )
}
