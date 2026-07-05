/**
 * v15.0 ClusterLeftPanel — cluster 落地页左栏 (DESIGN.md §15.10)
 *
 * 来源卡片 + 手风琴展开（最多同时 3 条，展开第 4 条自动收起最早）。
 * 展开按需拉 item 详情，复用 item 原文渲染顺序：媒体 / ASR / 正文。
 * 锚点 id 用 source-{item_id} 供右栏精简索引 scrollIntoView。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { ChevronUp, ExternalLink } from 'lucide-react'
import type { ClusterSource, FeedItem } from '../../lib/types'
import { fetchFeedItem } from '../../lib/api'
import { platformName, platformClass, cn } from '../../lib/utils'
import { renderMarkdownInline } from '../../lib/markdown-lite'
import { ItemLeftPanel } from '../item/ItemLeftPanel'

interface ClusterLeftPanelProps {
  sources: ClusterSource[]
}

const MAX_SIMUL_EXPANDED = 3
const LONG_CONTENT_THRESHOLD = 1200

function OfficialBadge() {
  return (
    <span
      style={{
        backgroundColor: 'var(--badge-official-bg)',
        color: 'var(--badge-official-fg)',
        fontSize: 10,
        padding: '2px 6px',
        borderRadius: 4,
        fontWeight: 500,
      }}
    >
      官方
    </span>
  )
}

export function ClusterLeftPanel({ sources }: ClusterLeftPanelProps) {
  // 展开顺序队列（FIFO，超过 3 条时弹出最早）
  const [expandedQueue, setExpandedQueue] = useState<string[]>([])
  const [itemMap, setItemMap] = useState<Record<string, FeedItem>>({})
  const [errorMap, setErrorMap] = useState<Record<string, string>>({})
  const [loadingMap, setLoadingMap] = useState<Record<string, boolean>>({})
  const [floatingCollapseId, setFloatingCollapseId] = useState<string | null>(null)
  const sourceRefs = useRef<Record<string, HTMLDivElement | null>>({})

  useEffect(() => {
    if (sources.length === 1) {
      setExpandedQueue([sources[0].item_id])
      return
    }
    const validIds = new Set(sources.map((source) => source.item_id))
    setExpandedQueue((queue) => queue.filter((id) => validIds.has(id)))
  }, [sources])

  const collapseItem = useCallback((itemId: string) => {
    setExpandedQueue((queue) => queue.filter((id) => id !== itemId))
  }, [])

  const toggleExpand = useCallback((itemId: string) => {
    setExpandedQueue((queue) => {
      if (queue.includes(itemId)) {
        return queue.filter((id) => id !== itemId)
      }
      const next = [...queue, itemId]
      if (next.length > MAX_SIMUL_EXPANDED) {
        return next.slice(next.length - MAX_SIMUL_EXPANDED)
      }
      return next
    })
  }, [])

  const handleCardKeyDown = useCallback((event: KeyboardEvent<HTMLDivElement>, itemId: string) => {
    if (event.key !== 'Enter' && event.key !== ' ') return
    event.preventDefault()
    toggleExpand(itemId)
  }, [toggleExpand])

  // 展开时按需拉 content
  useEffect(() => {
    expandedQueue.forEach((itemId) => {
      if (itemMap[itemId] != null || errorMap[itemId] != null || loadingMap[itemId]) return
      setLoadingMap((m) => ({ ...m, [itemId]: true }))
      fetchFeedItem(itemId)
        .then((item) => {
          setItemMap((m) => ({ ...m, [itemId]: item }))
          setErrorMap((m) => {
            const next = { ...m }
            delete next[itemId]
            return next
          })
        })
        .catch(() => {
          setErrorMap((m) => ({ ...m, [itemId]: '加载失败' }))
        })
        .finally(() => {
          setLoadingMap((m) => ({ ...m, [itemId]: false }))
        })
    })
  }, [expandedQueue, itemMap, errorMap, loadingMap])

  useEffect(() => {
    if (expandedQueue.length === 0) {
      setFloatingCollapseId(null)
      return
    }

    const check = () => {
      const visibleLongItem = expandedQueue.find((itemId) => {
        const item = itemMap[itemId]
        const content = item?.content || item?.description || ''
        if (content.length < LONG_CONTENT_THRESHOLD) return false
        const el = sourceRefs.current[itemId]
        if (!el) return false
        const rect = el.getBoundingClientRect()
        return rect.top < window.innerHeight && rect.bottom > window.innerHeight
      })
      setFloatingCollapseId(visibleLongItem ?? null)
    }

    check()
    window.addEventListener('scroll', check, { passive: true })
    window.addEventListener('resize', check)
    return () => {
      window.removeEventListener('scroll', check)
      window.removeEventListener('resize', check)
    }
  }, [expandedQueue, itemMap])

  return (
    <div className="space-y-2">
      {sources.map((src) => {
        const isExpanded = expandedQueue.includes(src.item_id)
        const item = itemMap[src.item_id]
        const loading = loadingMap[src.item_id]
        const error = errorMap[src.item_id]
        const isSingleSource = sources.length === 1
        return (
          <div
            key={src.item_id}
            id={`source-${src.item_id}`}
            data-testid="cluster-source-card"
            ref={(el) => { sourceRefs.current[src.item_id] = el }}
            role={isSingleSource ? undefined : 'button'}
            tabIndex={isSingleSource ? undefined : 0}
            aria-expanded={isSingleSource ? undefined : isExpanded}
            aria-label={isSingleSource ? undefined : (isExpanded ? `收起来源: ${src.title}` : `展开来源: ${src.title}`)}
            onClick={isSingleSource ? undefined : () => toggleExpand(src.item_id)}
            onKeyDown={isSingleSource ? undefined : (event) => handleCardKeyDown(event, src.item_id)}
            className={cn(
              'group relative rounded-none border-0 border-b border-border bg-transparent px-0 py-4 outline-none transition-[background-color,border-color] duration-200 focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]',
              !isSingleSource && 'cursor-pointer hover:border-border hover:bg-muted/55',
              !isSingleSource && src.url && 'pr-14',
              isSingleSource && 'cursor-default border-0 pb-0 pt-0 hover:bg-transparent',
            )}
          >
            {!isSingleSource && src.url && (
              <a
                href={src.url}
                target="_blank"
                rel="noopener noreferrer"
                aria-label={`打开原文: ${src.title}`}
                title="打开原文"
                onClick={(event) => event.stopPropagation()}
                onKeyDown={(event) => event.stopPropagation()}
                className="absolute right-3 top-3 inline-flex h-8 w-8 items-center justify-center rounded-md text-warm-500 opacity-80 hover:text-warm-900 hover:bg-warm-100 group-hover:opacity-100 transition-colors"
              >
                <ExternalLink size={15} aria-hidden="true" />
              </a>
            )}
            {!isSingleSource && (
              <div className="flex items-start gap-3">
                <div className="flex-1 min-w-0">
                  <div
                    className="flex items-start gap-2 min-w-0"
                    data-testid="cluster-source-heading-row"
                  >
                    <span
                      className={cn(
                        'mt-[3px] inline-flex shrink-0 items-center px-1.5 rounded text-[10px] font-bold',
                        platformClass(src.platform),
                      )}
                      data-testid="cluster-source-platform-badge"
                    >
                      {platformName(src.platform)}
                    </span>
                    <h3
                      className="min-w-0 flex-1 font-event-title text-[17px] font-bold leading-[1.42] tracking-[0] text-foreground"
                    >
                      {src.title}
                    </h3>
                    {src.authority_badge === 'official' && <OfficialBadge />}
                  </div>
                  <div className="mt-1.5 flex items-center gap-2 text-[12px] text-warm-500">
                    {src.published_at && (
                      <time>
                        {new Date(src.published_at).toLocaleString('zh-CN', {
                          month: '2-digit',
                          day: '2-digit',
                          hour: '2-digit',
                          minute: '2-digit',
                        })}
                      </time>
                    )}
                    {src.author && (
                      <>
                        <span>·</span>
                        <span>{src.author}</span>
                      </>
                    )}
                  </div>
                  {src.snippet && !isExpanded && (
                    <p className="mt-2 font-event-title text-[16px] leading-[1.72] tracking-[0] text-muted-foreground line-clamp-3">
                      {renderMarkdownInline(src.snippet)}
                    </p>
                  )}
                </div>
              </div>
            )}

            {/* 展开后内容 */}
            {isExpanded && (
              <div
                data-testid="cluster-expanded-content"
                className={cn(
                  'font-event-title text-[16px] leading-[1.82] tracking-[0] text-foreground/80',
                  isSingleSource ? 'mt-0' : 'mt-4',
                )}
                style={{ transition: 'max-height 250ms ease-in-out, opacity 250ms ease-in-out' }}
              >
                {loading ? (
                  <div className="text-warm-500">加载中…</div>
                ) : error ? (
                  <div className="text-warm-500">{error}</div>
                ) : item ? (
                  <ItemLeftPanel
                    item={item}
                    showHeader={false}
                    surface="plain"
                    truncateContent={false}
                  />
                ) : (
                  <div className="text-warm-500">暂无正文</div>
                )}
              </div>
            )}
          </div>
        )
      })}
      {floatingCollapseId && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-[90]">
          <button
            type="button"
            aria-label="收起当前展开全文"
            onClick={() => collapseItem(floatingCollapseId)}
            className="flex items-center gap-1.5 px-5 py-2 text-sm font-medium text-foreground bg-card border border-border hover:border-warm-400 shadow-subtle hover:shadow-medium rounded-full transition-all cursor-pointer"
          >
            收起
            <ChevronUp className="w-3.5 h-3.5 text-muted-foreground" />
          </button>
        </div>
      )}
    </div>
  )
}
