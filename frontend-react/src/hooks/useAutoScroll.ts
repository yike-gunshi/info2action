import { useEffect, useCallback, useRef, type RefObject } from 'react'

/**
 * Smart auto-scroll: follows new content unless user manually scrolled up.
 * Resets when streaming ends.
 */
export function useAutoScroll(containerRef: RefObject<HTMLElement | null>, isStreaming: boolean) {
  const userScrolled = useRef(false)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    const onScroll = () => {
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30
      userScrolled.current = !atBottom
    }

    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [containerRef])

  const scrollToBottom = useCallback(() => {
    if (!userScrolled.current && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight
    }
  }, [containerRef])

  // Reset when streaming ends
  useEffect(() => {
    if (!isStreaming) {
      userScrolled.current = false
    }
  }, [isStreaming])

  return { scrollToBottom, userScrolled }
}
