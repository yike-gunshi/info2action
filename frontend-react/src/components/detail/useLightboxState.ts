import { useState } from 'react'

export function useLightboxState() {
  const [lightboxSrc, setLightboxSrc] = useState<string | null>(null)
  const [lightboxImages, setLightboxImages] = useState<string[]>([])
  return [lightboxSrc, setLightboxSrc, lightboxImages, setLightboxImages] as const
}
