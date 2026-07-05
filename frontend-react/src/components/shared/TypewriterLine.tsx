import { useEffect, useRef, useState } from 'react'

/**
 * Typewriter line — reveals text char-by-char with cancel+flush support.
 * Extracted from ActionZone (v10.1) so cluster-action UI can reuse the
 * exact same UX (BF-0424-CLUSTER-SSE).
 */
export function TypewriterLine({
  text,
  speed = 15,
  flush = false,
  isLast = false,
  onComplete,
}: {
  text: string
  speed?: number
  flush?: boolean
  isLast?: boolean
  onComplete?: () => void
}) {
  const [displayed, setDisplayed] = useState(0)
  const completedRef = useRef(false)

  useEffect(() => {
    setDisplayed(0)
    completedRef.current = false
  }, [text])

  useEffect(() => {
    if (flush) {
      setDisplayed(text.length)
      return
    }
    if (displayed >= text.length) {
      if (!completedRef.current) {
        completedRef.current = true
        onComplete?.()
      }
      return
    }
    const timer = setTimeout(() => setDisplayed((d) => d + 1), speed)
    return () => clearTimeout(timer)
  }, [displayed, text.length, speed, flush, onComplete])

  const showCursor = isLast && !flush && displayed < text.length
  return (
    <>
      {text.slice(0, displayed)}
      {showCursor ? '▍' : ''}
    </>
  )
}
