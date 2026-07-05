/**
 * v15.1 Stage 4 双段 summary 解析器（feature-spec R6.1 / R6.2）
 *
 * Stage 4 prompt 输出 summary 必须为：
 *   【精华速览】<段落>\n\n【全文拆解】<分组>
 *
 * 兼容性（R6.2）：
 * - 双段都存在 → 拆出 speedReview / fullBreakdown 两段
 * - 只有【精华速览】 → speedReview = 段后内容, fullBreakdown = null
 * - 只有【全文拆解】 → speedReview = null, fullBreakdown = 段后内容
 * - 都不存在 → speedReview = 整段（不带 markers）, fullBreakdown = null
 *   前端 ClusterSummaryBlock 据此降级为 v15.0 平铺 markdown 渲染
 *
 * 这是纯函数模块，不导出 React 组件 — 配合 Fast Refresh 硬约束
 * (feedback_react_fast_refresh_no_mixed_export)，让 ClusterSummaryBlock.tsx
 * 文件保持只导出组件。
 */

export interface ClusterSummaryParts {
  /** 【精华速览】段内容（trim 后），缺失为 null */
  speedReview: string | null
  /** 【全文拆解】段内容（trim 后），缺失为 null */
  fullBreakdown: string | null
  /** 双段标记是否同时命中（false → 前端走 v15.0 平铺降级） */
  hasDualSections: boolean
}

export interface ClusterBreakdownSection {
  title: string
  points: string[]
}

const SPEED_MARKERS = ['【精华速览】', '【全文速览】']
const BREAKDOWN_MARKER = '【全文拆解】'
const BREAKDOWN_HEADING_RE = /^(?:#{1,4}\s*)?(?:0?(\d{1,2})[.、)：):\-\s]+)(.+)$/
const BREAKDOWN_BULLET_RE = /^(?:[-*•·])\s+(.+)$/
const MARKDOWN_STRONG_RE = /^\*\*(.+)\*\*$/

function findSpeedMarker(text: string): { idx: number; marker: string } | null {
  let found: { idx: number; marker: string } | null = null
  for (const marker of SPEED_MARKERS) {
    const idx = text.indexOf(marker)
    if (idx < 0) continue
    if (!found || idx < found.idx) found = { idx, marker }
  }
  return found
}

export function parseClusterSummary(
  summary: string | null | undefined,
): ClusterSummaryParts {
  if (!summary) {
    return { speedReview: null, fullBreakdown: null, hasDualSections: false }
  }
  const text = summary
  const speedMarker = findSpeedMarker(text)
  const speedIdx = speedMarker?.idx ?? -1
  const breakdownIdx = text.indexOf(BREAKDOWN_MARKER)

  if (speedMarker && breakdownIdx > speedIdx) {
    const speed = text
      .slice(speedIdx + speedMarker.marker.length, breakdownIdx)
      .trim()
    const breakdown = text.slice(breakdownIdx + BREAKDOWN_MARKER.length).trim()
    return {
      speedReview: speed || null,
      fullBreakdown: breakdown || null,
      hasDualSections: !!(speed && breakdown),
    }
  }

  if (speedMarker) {
    const speed = text.slice(speedIdx + speedMarker.marker.length).trim()
    return {
      speedReview: speed || null,
      fullBreakdown: null,
      hasDualSections: false,
    }
  }

  if (breakdownIdx >= 0) {
    const speed = text.slice(0, breakdownIdx).trim()
    const breakdown = text.slice(breakdownIdx + BREAKDOWN_MARKER.length).trim()
    return {
      speedReview: speed || null,
      fullBreakdown: breakdown || null,
      hasDualSections: !!(speed && breakdown),
    }
  }

  // 无 markers → v15.0 平铺降级（R6.2 兼容性）
  return {
    speedReview: text.trim() || null,
    fullBreakdown: null,
    hasDualSections: false,
  }
}

export function parseClusterBreakdownSections(
  fullBreakdown: string | null | undefined,
): ClusterBreakdownSection[] {
  if (!fullBreakdown) return []

  const lines = fullBreakdown
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)

  const numberedSections = parseNumberedBreakdownSections(lines)
  if (numberedSections.length > 0) return numberedSections

  const looseSections = parseLooseHeadingBreakdownSections(lines)
  return looseSections.length >= 2 ? looseSections : []
}

function cleanHeadingTitle(line: string): string {
  const strong = line.match(MARKDOWN_STRONG_RE)
  return (strong?.[1] || line).trim()
}

function parseNumberedBreakdownSections(lines: string[]): ClusterBreakdownSection[] {
  const sections: ClusterBreakdownSection[] = []
  let current: ClusterBreakdownSection | null = null

  for (const line of lines) {
    const heading = line.match(BREAKDOWN_HEADING_RE)
    if (heading?.[2]?.trim()) {
      current = { title: cleanHeadingTitle(heading[2].trim()), points: [] }
      sections.push(current)
      continue
    }

    if (!current) continue

    const bullet = line.match(BREAKDOWN_BULLET_RE)
    current.points.push((bullet?.[1] || line).trim())
  }

  return sections.filter((section) => section.title || section.points.length > 0)
}

function parseLooseHeadingBreakdownSections(lines: string[]): ClusterBreakdownSection[] {
  const sections: ClusterBreakdownSection[] = []
  let current: ClusterBreakdownSection | null = null

  lines.forEach((line, index) => {
    const bullet = line.match(BREAKDOWN_BULLET_RE)
    if (bullet) {
      current?.points.push(bullet[1].trim())
      return
    }

    const nextLine = lines[index + 1]
    const nextIsBullet = !!nextLine?.match(BREAKDOWN_BULLET_RE)
    if (nextIsBullet) {
      current = { title: cleanHeadingTitle(line), points: [] }
      sections.push(current)
      return
    }

    if (current && current.points.length === 0) {
      current.title = `${current.title} ${cleanHeadingTitle(line)}`.trim()
    }
  })

  return sections.filter((section) => section.title && section.points.length > 0)
}
