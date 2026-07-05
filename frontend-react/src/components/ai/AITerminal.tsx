import { useRef, useEffect } from 'react'
import { useAutoScroll } from '../../hooks/useAutoScroll'

interface AITerminalProps {
  lines: string[]
  isStreaming: boolean
  className?: string
}

/**
 * Terminal-style AI thinking process display.
 * 変更24: Terminal 风格思考过程
 * 変更26: 滚动锁定修复
 */
export function AITerminal({ lines, isStreaming, className }: AITerminalProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const { scrollToBottom } = useAutoScroll(containerRef, isStreaming)

  useEffect(() => {
    scrollToBottom()
  }, [lines, scrollToBottom])

  return (
    <div
      ref={containerRef}
      className={`rounded-lg bg-[var(--terminal-bg)] p-3 font-mono text-sm overflow-auto max-h-60 ${className || ''}`}
    >
      {lines.map((line, i) => (
        <div key={i} className="leading-relaxed">
          <span className="text-[var(--terminal-muted)] select-none">{'>'} </span>
          <span className="text-[var(--terminal-text)]">{line}</span>
        </div>
      ))}
      {isStreaming && (
        <div className="leading-relaxed">
          <span className="text-[var(--terminal-muted)] select-none">{'>'} </span>
          <span className="text-[var(--terminal-text)] animate-pulse">_</span>
        </div>
      )}
    </div>
  )
}
