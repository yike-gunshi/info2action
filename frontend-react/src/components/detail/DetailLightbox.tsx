import { ArrowLeft } from 'lucide-react'
import { proxiedImageUrl } from '../../lib/media'

export function DetailLightbox({
  src,
  images,
  onClose,
  onNavigate,
}: {
  src: string
  images: string[]
  onClose: () => void
  onNavigate: (src: string) => void
}) {
  return (
    <div
      className="fixed inset-0 z-[950] flex items-center justify-center bg-black/80"
      onClick={onClose}
    >
      {images.length > 1 && (() => {
        const idx = images.indexOf(src)
        return (
          <>
            {idx > 0 && (
              <button
                className="absolute left-4 top-1/2 -translate-y-1/2 w-10 h-10 flex items-center justify-center rounded-full bg-white/20 hover:bg-white/40 text-white transition-colors"
                onClick={(e) => { e.stopPropagation(); onNavigate(images[idx - 1]) }}
              >
                <ArrowLeft className="w-5 h-5" />
              </button>
            )}
            {idx < images.length - 1 && (
              <button
                className="absolute right-4 top-1/2 -translate-y-1/2 w-10 h-10 flex items-center justify-center rounded-full bg-white/20 hover:bg-white/40 text-white transition-colors rotate-180"
                onClick={(e) => { e.stopPropagation(); onNavigate(images[idx + 1]) }}
              >
                <ArrowLeft className="w-5 h-5" />
              </button>
            )}
            <span className="absolute bottom-4 left-1/2 -translate-x-1/2 text-sm text-white/70">
              {idx + 1} / {images.length}
            </span>
          </>
        )
      })()}
      <img
        src={proxiedImageUrl(src)}
        alt=""
        className="max-w-[90vw] max-h-[90vh] object-contain rounded-[6px]"
        referrerPolicy="no-referrer"
        onClick={(e) => e.stopPropagation()}
      />
    </div>
  )
}
