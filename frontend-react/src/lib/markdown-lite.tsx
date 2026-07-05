/**
 * BF-0420-3: 手写的轻量 Markdown 内联渲染器。
 * BF-0420-13: bold 解析改 stateful parser,处理奇数 `**` / 空 `****` / Python 乘方等
 *             LLM 异常输出;list 识别加 `•` bullet 和 numbered list;成为弹窗 + 独立页
 *             两个入口的唯一实现(原 DetailPanel.SimpleMd 已删)。
 *
 * 支持语法:
 *   **bold**         → <strong>(行内必须配对,奇数个 ** 整行字面回退)
 *   *italic*         → <em>
 *   `code`           → <code>
 *   http(s)://...    → <a target="_blank">
 *   example.com/...  → <a target="_blank">(展示原文,href 自动补 https://)
 *   - item / * item  → <ul><li>(行首匹配,连续行合并为一个列表)
 *   • item           → <ul><li>(同上)
 *   1. item / 1、item → <ol><li>(numbered list)
 *   \n\n             → 分段
 *   \n               → 段内换行
 *
 * 不渲染但优雅降级(避免字面残留):
 *   # ~ ###### 标题   → 转纯加粗段落(剥前缀 `#` + 空格)
 *   ``` 代码块围栏   → 跳过围栏行,内部内容当普通段落
 *   |---|---| 表格分隔 → 跳过(用户视觉无意义)
 *   | a | b | 表格行  → 转段落 "a | b"(保留信息,不渲染表格)
 *   > 引用            → 当普通段落处理(剥前缀)
 *
 * 完全不支持:图片 / 真表格 / 真代码块。
 *
 * 设计:不引入 react-markdown(bundle size + XSS 表面),只做 AI 摘要常见语法。
 * 安全:先 escape HTML 再做语法替换,避免 AI 输出含 <script> 等注入。
 */
import type { ReactNode } from 'react'

const AUTO_LINK_RE = /\b(?:https?:\/\/(?:(?!&(?:lt|gt|quot|#39);)[^\s<>"'`[\]{}|^，。！？、；：）】〕》」』（【《「『\u4e00-\u9fff])+|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,}(?:[/?#](?:(?!&(?:lt|gt|quot|#39);)[^\s<>"'`[\]{}|^，。！？、；：）】〕》」』（【《「『\u4e00-\u9fff])*)?)/gi
const TRAILING_URL_PUNCT_RE = /[)\].,;:!?，。！？、；：）】〕》」』]+$/
const CJK_TEXT_RE = /[\u3400-\u9fff]/
const IPV4_HOST_RE = /^\d{1,3}(?:\.\d{1,3}){3}$/
const IPV6_HOST_RE = /:/
const HTTP_PROTOCOL_RE = /^https?:\/\//i
const BARE_DOMAIN_CONTEXT_RE = /(官网|网站|平台|入口|地址|链接|文档|仓库|主页|首页|下载|演示|视频|论文|访问|查看|GitHub|repo|repository|site|homepage|docs?|url)/i

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function escapeHtmlAttr(s: string): string {
  return escapeHtml(s)
}

function decodeUrlEntities(s: string): string {
  return s
    .replace(/&amp;/g, '&')
    .replace(/&#38;/g, '&')
}

function trimUrlDisplayText(raw: string): { urlText: string; trailing: string } {
  let urlText = raw
  let trailing = ''

  while (urlText) {
    const entityMatch = urlText.match(/(?:&quot;|&#39;)+$/i)
    if (entityMatch) {
      urlText = urlText.slice(0, -entityMatch[0].length)
      trailing = entityMatch[0] + trailing
      continue
    }

    const punctMatch = urlText.match(TRAILING_URL_PUNCT_RE)
    if (!punctMatch) break
    urlText = urlText.slice(0, -punctMatch[0].length)
    trailing = punctMatch[0] + trailing
  }

  return { urlText, trailing }
}

function hasProbablyResolvableHost(hostname: string): boolean {
  const lower = hostname.toLowerCase()
  if (lower === 'localhost') return true
  if (IPV4_HOST_RE.test(lower)) return true
  if (IPV6_HOST_RE.test(lower)) return true
  if (CJK_TEXT_RE.test(lower)) return false

  const labels = lower.split('.')
  if (labels.length < 2) return false
  if (labels.some((label) => label.length === 0)) return false

  const tld = labels[labels.length - 1]
  return /^[a-z0-9-]{2,}$/i.test(tld)
}

function isPlaceholderUrl(displayUrl: string): boolean {
  return displayUrl.includes('...')
}

function hasExplicitHttpProtocol(displayUrl: string): boolean {
  return HTTP_PROTOCOL_RE.test(decodeUrlEntities(displayUrl))
}

function hasLinkishContext(precedingText: string): boolean {
  const tail = precedingText.slice(-48)
  const segment = tail.split(/[，。！？、；\n]/).pop() ?? tail
  return BARE_DOMAIN_CONTEXT_RE.test(segment)
}

function shouldLinkBareDomain(displayUrl: string, url: URL, precedingText: string): boolean {
  if (hasExplicitHttpProtocol(displayUrl)) return true
  if (/@$/.test(precedingText)) return false

  const decoded = decodeUrlEntities(displayUrl)
  if (/^www\./i.test(decoded)) return true
  if ((url.pathname && url.pathname !== '/') || url.search || url.hash) return true

  return hasLinkishContext(precedingText)
}

function shouldSkipForFollowingText(url: URL, followingText: string): boolean {
  if (CJK_TEXT_RE.test(followingText[0] || '')) return true

  const isBareOrigin = (url.pathname === '' || url.pathname === '/') && !url.search && !url.hash
  if (!isBareOrigin) return false

  return /^（(?:原文未提供完整链接|链接待补|见原文链接|搜索|对应推文|原始链接需|链接需)/.test(followingText)
}

function toSafeHref(displayUrl: string, followingText: string, precedingText: string): string | null {
  try {
    const decodedUrl = decodeUrlEntities(displayUrl)
    const url = new URL(hasExplicitHttpProtocol(decodedUrl) ? decodedUrl : `https://${decodedUrl}`)
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return null
    if (!hasProbablyResolvableHost(url.hostname)) return null
    if (isPlaceholderUrl(displayUrl)) return null
    if (!shouldLinkBareDomain(displayUrl, url, precedingText)) return null
    if (shouldSkipForFollowingText(url, followingText)) return null
    return url.href
  } catch {
    return null
  }
}

function applyAutoLinks(html: string): string {
  return html.replace(AUTO_LINK_RE, (raw: string, offset: number, input: string) => {
    if (isPlaceholderUrl(raw)) return raw
    const { urlText, trailing } = trimUrlDisplayText(raw)
    const followingText = input.slice(offset + raw.length)
    const precedingText = input.slice(0, offset)
    const href = toSafeHref(urlText, followingText, precedingText)
    if (!href) return raw
    return `<a class="content-inline-link" href="${escapeHtmlAttr(href)}" target="_blank" rel="noopener noreferrer">${urlText}</a>${trailing}`
  })
}

/**
 * stateful bold 解析。扫描行内所有 `**` 位置,贪心两两配对。
 * - 偶数个 `**` → 全部成对渲染为 <strong>
 * - 奇数个 `**` → 前 N-1 个贪心配对 + 末尾孤立 `**` 直接删除(不字面保留)
 *   原因:孤立 `**` 通常是 LLM 输出 noise(漏写开头 `**` / 末尾残留 \n\n**),字面保留视觉很丑;
 *   删除让用户看到干净文本,代价是少加粗一对(可接受)。
 * - 空 bold `****` → 跳过,既不渲染空 <strong> 也不输出字面
 *
 * 已知副作用:LLM 把 Python 乘方 `**` 当 bold marker(如 `**0.5 * m * v ** 2**`,3 个 `**`)
 * 仍会切成 `<strong>0.5 * m * v </strong> 2`(半截加粗,孤立尾 `**` 已删),无完美解。
 */
function applyBold(line: string): string {
  const positions: number[] = []
  let i = 0
  while (i <= line.length - 2) {
    if (line[i] === '*' && line[i + 1] === '*') {
      positions.push(i)
      i += 2
    } else {
      i++
    }
  }

  const usable = positions.length - (positions.length % 2)

  let result = ''
  let cursor = 0
  for (let k = 0; k < usable; k += 2) {
    const start = positions[k]
    const end = positions[k + 1]
    if (end - start <= 2) continue
    result += line.slice(cursor, start)
    result += `<strong class="font-semibold">${line.slice(start + 2, end)}</strong>`
    cursor = end + 2
  }

  let tail = line.slice(cursor)
  if (positions.length % 2 !== 0) {
    const lastOrphan = positions[positions.length - 1]
    if (lastOrphan >= cursor) {
      const localOrphan = lastOrphan - cursor
      tail = tail.slice(0, localOrphan) + tail.slice(localOrphan + 2)
    }
  }
  result += tail
  return result
}

/** 把 escape 后的文本里的 `X` / **X** / *X* 替换为对应 HTML 标签 */
function applyInline(escaped: string): string {
  let out = escaped
  const codePlaceholders: string[] = []
  // 1. inline code 最先处理 — 内部的 * 替换为 HTML entity,防后续 bold/italic 误抢
  out = out.replace(/`([^`\n]+?)`/g, (_, code: string) => {
    const safe = code.replace(/\*/g, '&#42;')
    const key = codePlaceholders.length
    codePlaceholders.push(`<code class="px-1 py-0.5 rounded bg-muted text-xs font-mono">${safe}</code>`)
    return `\uE000${key}\uE001`
  })
  // 2. bold(**X**)— stateful 贪心配对,孤立 ** 删除
  out = applyBold(out)
  // 3. italic(*X*)— 不能跨行;前后不紧邻 *(避开 ** 残留)
  out = out.replace(/(^|[^*])\*([^*\n]+?)\*(?!\*)/g, '$1<em class="italic">$2</em>')
  // 4. bare http(s) URL → safe external link. Code placeholders are restored after linkification.
  out = applyAutoLinks(out)
  out = out.replace(/\uE000(\d+)\uE001/g, (_, key: string) => codePlaceholders[Number(key)] ?? '')
  return out
}

interface ListItem {
  html: string
  subItems: string[]  // v5b: 二级嵌套子项（≥2 空格缩进的 bullet 挂这里）
}

interface RenderedBlock {
  kind: 'p' | 'ul' | 'ol'
  html?: string[]    // 对于 p: 单元素
  items?: ListItem[] // 对于 ul/ol: 每项一个 ListItem（含 subItems）
}

/**
 * 把表格行 `| a | b | c |` 转为段落 "a | b | c"(剥两端 |,保留中间分隔)
 * 表格分隔行 `|---|---|`(只含 |/-/:/空格) → 返回 null,调用方跳过
 */
function normalizeTableRow(line: string): string | null {
  if (!/^\s*\|.*\|\s*$/.test(line)) return line
  if (/^\s*\|[\s\-:|]+\|\s*$/.test(line)) return null
  return line.replace(/^\s*\|\s?/, '').replace(/\s?\|\s*$/, '')
}

/** 把纯文本拆成块级结构(段落 vs 列表),每块应用内联转换 */
function toBlocks(text: string): RenderedBlock[] {
  const lines = text.split('\n')
  const blocks: RenderedBlock[] = []
  let currentList: { kind: 'ul' | 'ol'; items: ListItem[] } | null = null
  let currentPara: string[] | null = null
  let inFence = false

  function flushList() {
    if (currentList && currentList.items.length) {
      blocks.push({ kind: currentList.kind, items: currentList.items })
    }
    currentList = null
  }
  function flushPara() {
    if (currentPara && currentPara.length) {
      const joined = currentPara.join('<br />')
      blocks.push({ kind: 'p', html: [joined] })
    }
    currentPara = null
  }

  for (const raw of lines) {
    let line = raw.trimEnd()

    // 代码块围栏 ``` 单独成行 → 跳过(围栏内行当普通段落)
    if (/^\s*```/.test(line)) {
      inFence = !inFence
      continue
    }

    // 表格行处理(分隔行 |---| → 跳过;数据行 | a | b | → 段落 "a | b")
    const tableNorm = normalizeTableRow(line)
    if (tableNorm === null) continue
    line = tableNorm

    // 标题 # ~ ###### + 空格 → 转加粗段落(强加 ** 让 inline 配对成 strong)
    const headingMatch = line.match(/^\s*(#{1,6})\s+(.*)$/)
    if (headingMatch) {
      flushList()
      flushPara()
      const headText = headingMatch[2].trim()
      if (headText) {
        const wrapped = applyInline(escapeHtml(`**${headText}**`))
        blocks.push({ kind: 'p', html: [wrapped] })
      }
      continue
    }

    // 引用 > → 剥前缀当普通段落
    const quoteMatch = line.match(/^\s*>\s?(.*)$/)
    if (quoteMatch) line = quoteMatch[1]

    // v5b: 捕获前导空格判断嵌套（≥2 空格 = 二级 sub bullet）
    const bulletMatch = line.match(/^(\s*)[-*•]\s+(.*)$/)
    const numberedMatch = line.match(/^(\s*)\d+[.、]\s+(.*)$/)
    if (bulletMatch) {
      const indent = bulletMatch[1].length
      const itemHtml = applyInline(escapeHtml(bulletMatch[2]))
      flushPara()
      if (!currentList || currentList.kind !== 'ul') {
        flushList()
        currentList = { kind: 'ul', items: [] }
      }
      if (indent >= 2 && currentList.items.length > 0) {
        currentList.items[currentList.items.length - 1].subItems.push(itemHtml)
      } else {
        currentList.items.push({ html: itemHtml, subItems: [] })
      }
      continue
    }
    if (numberedMatch) {
      const indent = numberedMatch[1].length
      const itemHtml = applyInline(escapeHtml(numberedMatch[2]))
      flushPara()
      if (!currentList || currentList.kind !== 'ol') {
        flushList()
        currentList = { kind: 'ol', items: [] }
      }
      if (indent >= 2 && currentList.items.length > 0) {
        currentList.items[currentList.items.length - 1].subItems.push(itemHtml)
      } else {
        currentList.items.push({ html: itemHtml, subItems: [] })
      }
      continue
    }
    if (line.trim() === '') {
      flushList()
      flushPara()
      continue
    }
    flushList()
    const inline = applyInline(escapeHtml(line))
    // 删除孤立 ** 后可能整行变空,跳过避免渲染空 <p>
    if (inline.trim() === '') continue
    if (!currentPara) currentPara = []
    currentPara.push(inline)
  }
  flushList()
  flushPara()
  return blocks
}

/**
 * 仅渲染内联 markdown(bold/italic/code),不拆段不加 <p>/<ul>。
 * 适用于:已有容器元素(<li>、<td> 等)里只想给文字做 inline 强调的场景。
 *
 * Usage: <li>{renderMarkdownInline(sub)}</li>
 */
export function renderMarkdownInline(text: string | null | undefined): ReactNode {
  if (!text) return null
  const html = applyInline(escapeHtml(text))
  return <span dangerouslySetInnerHTML={{ __html: html }} />
}

/**
 * 渲染 markdown-lite 内容为 React 节点数组(块级 + 内联)。
 * 每个块是独立元素(<p> / <ul> / <ol>),通过 dangerouslySetInnerHTML 插入已 escape + 标签化的 HTML。
 *
 * Usage: const nodes = renderMarkdownLite(text); return <>{nodes}</>
 */
export function renderMarkdownLite(text: string | null | undefined): ReactNode[] {
  if (!text) return []
  const blocks = toBlocks(text)
  return blocks.map((b, i) => {
    if (b.kind === 'p') {
      return (
        <p
          key={i}
          className="leading-[1.7]"
          dangerouslySetInnerHTML={{ __html: (b.html && b.html[0]) || '' }}
        />
      )
    }
    const items = b.items || []
    if (b.kind === 'ol') {
      return (
        <ol key={i} className="list-decimal pl-5 leading-[1.7] space-y-1">
          {items.map((it, j) => (
            <li key={j}>
              <span dangerouslySetInnerHTML={{ __html: it.html }} />
              {it.subItems.length > 0 && (
                <ul className="list-[circle] pl-5 mt-1 space-y-1">
                  {it.subItems.map((sub, k) => (
                    <li key={k} dangerouslySetInnerHTML={{ __html: sub }} />
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ol>
      )
    }
    return (
      <ul key={i} className="list-disc pl-5 leading-[1.7] space-y-1">
        {items.map((it, j) => (
          <li key={j}>
            <span dangerouslySetInnerHTML={{ __html: it.html }} />
            {it.subItems.length > 0 && (
              <ul className="list-[circle] pl-5 mt-1 space-y-1">
                {it.subItems.map((sub, k) => (
                  <li key={k} dangerouslySetInnerHTML={{ __html: sub }} />
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    )
  })
}
