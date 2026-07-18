import {
  cloneElement,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from 'react'
import type {
  FocusEventHandler,
  MouseEventHandler,
  PointerEventHandler,
  ReactElement,
  ReactNode,
  Ref,
  MutableRefObject,
} from 'react'
import { createPortal } from 'react-dom'

type TooltipTriggerProps = {
  ref?: Ref<HTMLElement>
  'aria-describedby'?: string
  onFocus?: FocusEventHandler<HTMLElement>
  onBlur?: FocusEventHandler<HTMLElement>
  onMouseEnter?: MouseEventHandler<HTMLElement>
  onMouseLeave?: MouseEventHandler<HTMLElement>
  onPointerDown?: PointerEventHandler<HTMLElement>
  onPointerUp?: PointerEventHandler<HTMLElement>
  onPointerCancel?: PointerEventHandler<HTMLElement>
  onPointerMove?: PointerEventHandler<HTMLElement>
}

type TooltipProps = {
  children: ReactElement<TooltipTriggerProps>
  content: ReactNode
  delay?: number
  variant?: 'default' | 'rich'
}

type TooltipPosition = {
  left: number
  top: number
  side: 'top' | 'bottom'
}

const VIEWPORT_GAP = 8
const TRIGGER_GAP = 6
const LONG_PRESS_MS = 500

export function Tooltip({ children, content, delay = 150, variant = 'default' }: TooltipProps) {
  const rawId = useId()
  const tooltipId = `tooltip-${rawId.replace(/:/g, '')}`
  const triggerRef = useRef<HTMLElement | null>(null)
  const tooltipRef = useRef<HTMLDivElement | null>(null)
  const hoverTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const longPressTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const focused = useRef(false)
  const hovered = useRef(false)
  const longPressOpened = useRef(false)
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState<TooltipPosition | null>(null)

  function clearHoverTimer() {
    if (!hoverTimer.current) return
    clearTimeout(hoverTimer.current)
    hoverTimer.current = null
  }

  function clearLongPressTimer() {
    if (!longPressTimer.current) return
    clearTimeout(longPressTimer.current)
    longPressTimer.current = null
  }

  function hideIfIdle() {
    clearHoverTimer()
    if (!focused.current && !hovered.current && !longPressOpened.current) setOpen(false)
  }

  useLayoutEffect(() => {
    if (!open) {
      setPosition(null)
      return
    }
    const updatePosition = () => {
      const trigger = triggerRef.current
      const tooltip = tooltipRef.current
      if (!trigger || !tooltip) return
      const triggerRect = trigger.getBoundingClientRect()
      const tooltipRect = tooltip.getBoundingClientRect()
      const roomAbove = triggerRect.top - VIEWPORT_GAP
      const roomBelow = window.innerHeight - triggerRect.bottom - VIEWPORT_GAP
      const side = roomAbove >= tooltipRect.height + TRIGGER_GAP || roomAbove > roomBelow ? 'top' : 'bottom'
      const preferredLeft = triggerRect.left + (triggerRect.width - tooltipRect.width) / 2
      const maxLeft = Math.max(VIEWPORT_GAP, window.innerWidth - tooltipRect.width - VIEWPORT_GAP)
      const left = Math.min(Math.max(preferredLeft, VIEWPORT_GAP), maxLeft)
      const preferredTop = side === 'top'
        ? triggerRect.top - tooltipRect.height - TRIGGER_GAP
        : triggerRect.bottom + TRIGGER_GAP
      const maxTop = Math.max(VIEWPORT_GAP, window.innerHeight - tooltipRect.height - VIEWPORT_GAP)
      const top = Math.min(Math.max(preferredTop, VIEWPORT_GAP), maxTop)
      setPosition({ left, top, side })
    }
    updatePosition()
    window.addEventListener('resize', updatePosition)
    window.addEventListener('scroll', updatePosition, true)
    return () => {
      window.removeEventListener('resize', updatePosition)
      window.removeEventListener('scroll', updatePosition, true)
    }
  }, [open])

  useEffect(() => {
    if (!open || !longPressOpened.current) return
    const closeOnOutsidePress = (event: PointerEvent) => {
      const target = event.target as Node | null
      if (target && triggerRef.current?.contains(target)) return
      longPressOpened.current = false
      setOpen(false)
    }
    document.addEventListener('pointerdown', closeOnOutsidePress)
    return () => document.removeEventListener('pointerdown', closeOnOutsidePress)
  }, [open])

  useEffect(() => () => {
    clearHoverTimer()
    clearLongPressTimer()
  }, [])

  const childProps = children.props
  const describedBy = [childProps['aria-describedby'], tooltipId].filter(Boolean).join(' ')
  const trigger = cloneElement(children, {
    ref: mergeRefs(childProps.ref, (node: HTMLElement | null) => {
      triggerRef.current = node
    }),
    'aria-describedby': describedBy,
    onMouseEnter: composeHandler(childProps.onMouseEnter, () => {
      hovered.current = true
      clearHoverTimer()
      hoverTimer.current = setTimeout(() => {
        hoverTimer.current = null
        setOpen(true)
      }, delay)
    }),
    onMouseLeave: composeHandler(childProps.onMouseLeave, () => {
      hovered.current = false
      hideIfIdle()
    }),
    onFocus: composeHandler(childProps.onFocus, () => {
      focused.current = true
      clearHoverTimer()
      setOpen(true)
    }),
    onBlur: composeHandler(childProps.onBlur, () => {
      focused.current = false
      hideIfIdle()
    }),
    onPointerDown: composeHandler(childProps.onPointerDown, (event) => {
      if (event.pointerType === 'mouse') return
      clearLongPressTimer()
      longPressOpened.current = false
      longPressTimer.current = setTimeout(() => {
        longPressTimer.current = null
        longPressOpened.current = true
        setOpen(true)
      }, LONG_PRESS_MS)
    }),
    onPointerUp: composeHandler(childProps.onPointerUp, clearLongPressTimer),
    onPointerCancel: composeHandler(childProps.onPointerCancel, clearLongPressTimer),
    onPointerMove: composeHandler(childProps.onPointerMove, clearLongPressTimer),
  })

  return (
    <>
      {trigger}
      {open && createPortal(
        <div
          ref={tooltipRef}
          id={tooltipId}
          role="tooltip"
          data-side={position?.side ?? 'top'}
          className={variant === 'rich'
            ? 'pointer-events-none fixed z-[1000] max-w-[300px] rounded-[8px] border border-[var(--brand-border)] bg-card px-3.5 py-3 text-[12px] leading-[1.5] text-foreground shadow-[0_8px_28px_rgba(26,25,23,0.16)]'
            : 'pointer-events-none fixed z-[1000] max-w-[280px] rounded-[4px] bg-foreground px-[10px] py-[6px] text-[12px] leading-[1.5] text-background shadow-sm'}
          style={{
            left: position?.left ?? VIEWPORT_GAP,
            top: position?.top ?? VIEWPORT_GAP,
            visibility: position ? 'visible' : 'hidden',
          }}
        >
          {content}
        </div>,
        document.body,
      )}
    </>
  )
}

function composeHandler<Event>(original: ((event: Event) => void) | undefined, next: (event: Event) => void) {
  return (event: Event) => {
    original?.(event)
    next(event)
  }
}

function mergeRefs<T>(...refs: Array<Ref<T> | undefined>): Ref<T> {
  return (value) => {
    for (const ref of refs) {
      if (typeof ref === 'function') ref(value)
      else if (ref) (ref as MutableRefObject<T | null>).current = value
    }
  }
}
