import { Children, cloneElement, createElement, isValidElement, useMemo, type ReactNode } from 'react'
import { renderMarkdownLite } from '../../lib/markdown-lite'
import { useAiSearchStore, type AiSearchSummary } from '../../store/aiSearchStore'
import type { AiSearchItem } from '../../lib/api/aiSearch'
import { describeSummaryError } from '../../lib/api/aiSearch'
import { CitationChip } from './CitationChip'
import { SummaryActions } from './SummaryActions'
import './SummaryPane.css'

const skeletonWidths = ['92%', '100%', '96%', null, '88%', '100%', '70%']

function citationText(text: string, key: string, items: AiSearchItem[]): ReactNode[] {
  const nodes: ReactNode[] = []
  let cursor = 0
  let group: number[] = []
  const flush = () => {
    if (!group.length) return
    const chipGroup = <span className="ai-search-chip-group" key={`${key}-${cursor}`}>
      {group.sort((a, b) => a - b).map((n, index) => (
        <span className="ai-search-chip-item" key={index}><CitationChip n={n} /></span>
      ))}
    </span>
    const previousIndex = nodes.length - 1
    const previousText = nodes[previousIndex]
    if (typeof previousText === 'string' && previousText) {
      const characters = Array.from(previousText)
      const lastCharacter = characters.pop()
      nodes[previousIndex] = characters.join('')
      nodes.push(<span className="ai-search-chip-glue" key={`${key}-${cursor}`}>{lastCharacter}{chipGroup}</span>)
    } else {
      nodes.push(chipGroup)
    }
    group = []
  }
  for (const match of text.matchAll(/\[(\d{1,3})\]/g)) {
    const n = Number(match[1])
    const between = text.slice(cursor, match.index)
    if (!items[n - 1]?.id) {
      flush()
      nodes.push(between, match[0])
    } else {
      if (between.trim()) flush()
      const prefix = between.replace(/\s+$/, '')
      if (!group.length && prefix) nodes.push(prefix)
      group.push(n)
    }
    cursor = match.index + match[0].length
  }
  flush()
  nodes.push(text.slice(cursor))
  return nodes
}

// markdown-lite has already escaped input and created safe HTML. Convert its
// inline output to React children so chips remain real, keyboard-usable buttons.
function inlineNodes(nodes: NodeListOf<ChildNode>, key: string, items: AiSearchItem[], allowCitations = true): ReactNode[] {
  return Array.from(nodes, (node, index) => {
    const childKey = `${key}-${index}`
    if (node.nodeType === Node.TEXT_NODE) {
      return allowCitations ? citationText(node.textContent ?? '', childKey, items) : node.textContent
    }
    if (!(node instanceof HTMLElement)) return null
    const tag = node.tagName.toLowerCase()
    const props: Record<string, string> = { key: childKey }
    for (const attr of Array.from(node.attributes)) {
      if (['class', 'href', 'target', 'rel'].includes(attr.name)) {
        props[attr.name === 'class' ? 'className' : attr.name] = attr.value
      }
    }
    if (tag === 'br') return createElement(tag, props)
    return createElement(tag, props, inlineNodes(node.childNodes, childKey, items, allowCitations && !['a', 'button', 'code'].includes(tag)))
  })
}

interface MarkdownNodeProps {
  children?: ReactNode
  className?: string
  dangerouslySetInnerHTML?: { __html: string }
}

function withCitations(node: ReactNode, key: string, items: AiSearchItem[], lastBlock = false): ReactNode {
  if (typeof node === 'string') return citationText(node, key, items)
  if (!isValidElement<MarkdownNodeProps>(node)) return node
  const html = node.props.dangerouslySetInnerHTML?.__html
  if (html !== undefined) {
    const template = document.createElement('template')
    template.innerHTML = html
    const content = template.content
    const nonemptyNodes = Array.from(content.childNodes).filter((child) => child.nodeType !== Node.TEXT_NODE || child.textContent?.trim())
    const isHeading = node.type === 'p' && nonemptyNodes.length > 0 && nonemptyNodes.every(child => child instanceof HTMLElement && child.tagName === 'STRONG')
    if (isHeading) return createElement('h4', { key }, Array.from(content.childNodes, (child, index) =>
      child instanceof HTMLElement ? inlineNodes(child.childNodes, `${key}-${index}`, items) : child.textContent))
    // The model still ends every summary with "基于 N 条内容", but the meta row above
    // already carries that count, so the design drops the duplicate line entirely
    // (user feedback 2026-09-21; the skeleton skips it the same way). Old jobs keep
    // the sentence in their stored markdown, so this has to be filtered at render time.
    const isFoot = lastBlock && node.type === 'p' && /^基于\s*\d+\s*条内容$/.test(content.textContent?.trim() ?? '')
    if (isFoot) return null
    return cloneElement(node, {
      dangerouslySetInnerHTML: undefined,
      className: undefined,
    }, inlineNodes(content.childNodes, key, items))
  }
  // Typography belongs to this page; keep the shared safe Markdown parser and
  // remove its block-level spacing classes before applying the design tokens.
  return cloneElement(node, { className: undefined }, Children.map(node.props.children, (child, index) => withCitations(child, `${key}-${index}`, items)))
}

export function describeSummaryOutcome(summary: AiSearchSummary, emptyResult = false): string {
  if (emptyResult) return '站内近 90 天没有找到相关内容，换个说法试试。'
  if (summary.status === 'done') {
    const elapsed = summary.elapsedMs == null ? '—' : summary.elapsedMs < 60000 ? '不足 1 分钟' : `${Math.round(summary.elapsedMs / 60000)} 分`
    return `用时 ${elapsed} · 基于 ${summary.citedCount ?? summary.citedIds.length} 条内容`
  }
  if (summary.status === 'failed' || summary.status === 'unavailable') {
    const error = summary.error || summary.reason
    return describeSummaryError(summary.status === 'unavailable' && error !== 'quota_exhausted'
      ? 'unavailable' : error || 'internal') ?? ''
  }
  return ''
}

export function SummaryMeta() {
  const summary = useAiSearchStore((state) => state.summary)
  const loadingJob = useAiSearchStore((state) => state.loadingJob)
  const itemCount = useAiSearchStore((state) => state.items.length)
  const emptyResult = useAiSearchStore((state) => state.emptyResult)
  const pending = !emptyResult && (summary.status === 'queued' || summary.status === 'running')
  let text = ''
  if (pending && !(loadingJob && itemCount === 0)) {
    text = `正在整理 ${itemCount} 条内容 · 通常需要 8 到 15 分钟`
      + ((summary.position ?? 0) > 0 ? ` · 排在第 ${summary.position} 位` : '')
  } else if (!emptyResult && !loadingJob && summary.status === 'done') {
    text = describeSummaryOutcome(summary)
  }
  return <p className="ai-summary-meta">{text}</p>
}

export function SummaryPane({ onFeedback }: { onFeedback?: (message: string) => void }) {
  const summary = useAiSearchStore((state) => state.summary)
  const items = useAiSearchStore((state) => state.items)
  const loadingJob = useAiSearchStore((state) => state.loadingJob)
  const emptyResult = useAiSearchStore((state) => state.emptyResult)
  const content = useMemo(() => {
    const blocks = renderMarkdownLite(summary.contentMd, { links: false })
    return blocks.map((node, index) => withCitations(node, String(index), items, index === blocks.length - 1))
  }, [summary.contentMd, items])

  if (emptyResult) {
    return (
      <section aria-label="AI 总结" className="ai-summary-state min-w-0">
        <p className="ai-summary-state-title">{describeSummaryOutcome(summary, true)}</p>
      </section>
    )
  }

  // 取既有任务时也走骨架：此时还不知道它是完成还是进行中，
  // 但页面已经确定要放一篇文章，空着会让人以为出错了。
  if (loadingJob || summary.status === 'queued' || summary.status === 'running') {
    return (
      <section aria-label="AI 总结" aria-busy="true" className="min-w-0">
        <div aria-hidden="true" className="ai-summary-skeleton">
          {skeletonWidths.map((width, index) => (
            <div key={index} className={`ai-summary-skeleton-bar animate-pulse motion-reduce:animate-none${width === null ? ' ai-summary-skeleton-heading' : ''}`} style={width === null ? undefined : { width }} />
          ))}
        </div>
      </section>
    )
  }

  if (summary.status === 'done') {
    return (
      <section aria-label="AI 总结" className="min-w-0">
        <div className="ai-summary-article">
          {summary.contentMd ? content : <p className="text-muted-foreground">本次未生成正文</p>}
        </div>
        <SummaryActions onFeedback={onFeedback} />
      </section>
    )
  }

  if (summary.status === 'failed' || summary.status === 'unavailable') {
    return (
      <section aria-label="AI 总结" className="ai-summary-state min-w-0">
        <p className="ai-summary-state-title">{describeSummaryOutcome(summary)}</p>
      </section>
    )
  }

  return null
}
