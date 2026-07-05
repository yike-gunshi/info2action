import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { renderMarkdownInline, renderMarkdownLite } from '../markdown-lite'

/** 断言:渲染结果的 HTML 包含某子串(不严格全匹配,因为 dangerouslySetInnerHTML 输出带类名) */
function assertRenderedContains(text: string, needle: string) {
  const { container } = render(<>{renderMarkdownLite(text)}</>)
  expect(container.innerHTML).toContain(needle)
}

describe('markdown-lite', () => {
  it('**X** 渲染为 <strong>', () => {
    assertRenderedContains('我是 **加粗** 的', '<strong class="font-semibold">加粗</strong>')
  })

  it('*X* 渲染为 <em>', () => {
    assertRenderedContains('this is *italic* text', '<em class="italic">italic</em>')
  })

  it('`X` 渲染为 <code>', () => {
    assertRenderedContains('用 `await` 等待', '<code class="px-1 py-0.5 rounded bg-muted text-xs font-mono">await</code>')
  })

  it('裸 http/https URL 渲染为新 tab 安全链接', () => {
    const { container } = render(<>{renderMarkdownLite('官方文档 https://docs.anthropic.com')}</>)
    const link = container.querySelector('a.content-inline-link')
    expect(link).not.toBeNull()
    expect(link).toHaveAttribute('href', 'https://docs.anthropic.com/')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(link?.textContent).toBe('https://docs.anthropic.com')
  })

  it('裸 GitHub 域名路径自动补 https 并渲染为安全链接', () => {
    const { container } = render(<>{renderMarkdownLite('开源仓库：github.com/superset-sh/superset')}</>)
    const link = container.querySelector('a.content-inline-link')
    expect(link).not.toBeNull()
    expect(link).toHaveAttribute('href', 'https://github.com/superset-sh/superset')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(link?.textContent).toBe('github.com/superset-sh/superset')
  })

  it('裸官网域名自动补 https,中文标点不进入 href', () => {
    const { container } = render(<>{renderMarkdownLite('产品官网 / 试用入口：superset.sh，产品演示视频待补充')}</>)
    const link = container.querySelector('a.content-inline-link')
    expect(link).not.toBeNull()
    expect(link).toHaveAttribute('href', 'https://superset.sh/')
    expect(link?.textContent).toBe('superset.sh')
    expect(container.textContent).toBe('产品官网 / 试用入口：superset.sh，产品演示视频待补充')
  })

  it('注册地址后的裸域名自动补 https 并渲染为安全链接', () => {
    const { container } = render(<>{renderMarkdownLite('Messager 注册地址：wechatobsidian.com')}</>)
    const link = container.querySelector('a.content-inline-link')
    expect(link).not.toBeNull()
    expect(link).toHaveAttribute('href', 'https://wechatobsidian.com/')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(link?.textContent).toBe('wechatobsidian.com')
  })

  it('邮箱中的裸域名不自动转链', () => {
    const { container } = render(<>{renderMarkdownLite('联系邮箱 hello@example.com')}</>)
    expect(container.querySelector('a')).toBeNull()
  })

  it('普通技术名中的点号不自动转链', () => {
    const { container } = render(<>{renderMarkdownLite('这个项目使用 Node.js 运行')}</>)
    expect(container.querySelector('a')).toBeNull()
  })

  it('URL 尾部中文标点不进入 href,但保留在正文里', () => {
    const { container } = render(<>{renderMarkdownLite('项目地址 https://github.com/anthropics/claude-code。')}</>)
    const link = container.querySelector('a.content-inline-link')
    expect(link).toHaveAttribute('href', 'https://github.com/anthropics/claude-code')
    expect(link?.textContent).toBe('https://github.com/anthropics/claude-code')
    expect(container.textContent).toBe('项目地址 https://github.com/anthropics/claude-code。')
  })

  it('不完整域名 URL 保持普通文本,避免生成无法打开的链接', () => {
    const { container } = render(
      <>{renderMarkdownLite('知网AIGC检测专利：https://ap/appsoc/8f4a4e（原文未提供完整链接）')}</>,
    )
    expect(container.querySelector('a')).toBeNull()
    expect(container.textContent).toContain('https://ap/appsoc/8f4a4e（原文未提供完整链接）')
  })

  it('中文括号备注不进入链接,有效短链仍可点击', () => {
    const { container } = render(
      <>{renderMarkdownLite('资源：https://t.co/h64b3IF5Z6（由发帖者提供的配套资源）')}</>,
    )
    const link = container.querySelector('a.content-inline-link')
    expect(link).toHaveAttribute('href', 'https://t.co/h64b3IF5Z6')
    expect(link?.textContent).toBe('https://t.co/h64b3IF5Z6')
    expect(container.textContent).toBe('资源：https://t.co/h64b3IF5Z6（由发帖者提供的配套资源）')
  })

  it('中文逗号和冒号可以分隔多个 URL', () => {
    const { container } = render(
      <>{renderMarkdownLite('开发者入口：https://disputron.ai/developers，公开庭审：https://disputron.ai/live')}</>,
    )
    const links = container.querySelectorAll('a.content-inline-link')
    expect(links).toHaveLength(2)
    expect(links[0]).toHaveAttribute('href', 'https://disputron.ai/developers')
    expect(links[1]).toHaveAttribute('href', 'https://disputron.ai/live')
  })

  it('Markdown 链接残片不会被合并成一个错误 URL', () => {
    const { container } = render(
      <>{renderMarkdownLite('官网 https://www.ctxify.dev](https://www.ctxify.dev/)，可通过')}</>,
    )
    const links = container.querySelectorAll('a.content-inline-link')
    expect(links).toHaveLength(2)
    expect(links[0]).toHaveAttribute('href', 'https://www.ctxify.dev/')
    expect(links[0].textContent).toBe('https://www.ctxify.dev')
    expect(links[1]).toHaveAttribute('href', 'https://www.ctxify.dev/')
    expect(links[1].textContent).toBe('https://www.ctxify.dev/')
  })

  it('省略号占位 URL 不自动转链', () => {
    const { container } = render(
      <>{renderMarkdownLite('原文 https://www.nytimes.com/zh-hans/interactive/...，需要搜索完整链接')}</>,
    )
    expect(container.querySelector('a')).toBeNull()
  })

  it('路径后直接接中文说明时不自动转链', () => {
    const { container } = render(
      <>{renderMarkdownLite('去 https://github.com/搜索 相关仓库')}</>,
    )
    expect(container.querySelector('a')).toBeNull()
    expect(container.textContent).toContain('https://github.com/搜索')
  })

  it('inline 渲染同样支持链接,但 code 内 URL 不自动转链', () => {
    const { container } = render(<>{renderMarkdownInline('看 https://example.com 和 `https://code.example`')}</>)
    expect(container.querySelectorAll('a.content-inline-link')).toHaveLength(1)
    expect(container.querySelector('code')?.textContent).toBe('https://code.example')
    expect(container.querySelector('code a')).toBeNull()
  })

  it('非 http/https 协议保持普通文本', () => {
    const { container } = render(<>{renderMarkdownLite('不要执行 javascript:alert(1)')}</>)
    expect(container.querySelector('a')).toBeNull()
    expect(container.textContent).toContain('javascript:alert(1)')
  })

  it('- X 列表渲染为 <ul><li>', () => {
    const { container } = render(
      <>{renderMarkdownLite('- 第一项\n- 第二项\n- 第三项')}</>,
    )
    expect(container.querySelectorAll('ul')).toHaveLength(1)
    expect(container.querySelectorAll('li')).toHaveLength(3)
    expect(container.querySelectorAll('li')[0].textContent).toBe('第一项')
  })

  it('* X 列表(星号)同样渲染为 <ul><li>', () => {
    const { container } = render(<>{renderMarkdownLite('* a\n* b')}</>)
    expect(container.querySelectorAll('li')).toHaveLength(2)
  })

  it('\\n\\n 分段,每段独立 <p>', () => {
    const { container } = render(<>{renderMarkdownLite('第一段\n\n第二段')}</>)
    expect(container.querySelectorAll('p')).toHaveLength(2)
  })

  it('段内单个 \\n 用 <br />', () => {
    const { container } = render(<>{renderMarkdownLite('一行\n续行')}</>)
    expect(container.querySelectorAll('p')).toHaveLength(1)
    expect(container.querySelector('p')?.innerHTML).toContain('<br>')  // jsdom 小写
  })

  it('先 escape HTML 再 parse,防 XSS', () => {
    const { container } = render(<>{renderMarkdownLite('<script>alert(1)</script>')}</>)
    // 不应该有实际的 <script> 节点
    expect(container.querySelector('script')).toBeNull()
    // 字符串应该被 escape
    expect(container.textContent).toContain('<script>')
  })

  it('code 里的 **X** 不被解析为 bold(inline code 优先)', () => {
    const { container } = render(<>{renderMarkdownLite('`**not bold**`')}</>)
    expect(container.querySelector('strong')).toBeNull()
    expect(container.querySelector('code')?.textContent).toBe('**not bold**')
  })

  it('bold 嵌套 italic:**X *Y* Z**', () => {
    const { container } = render(<>{renderMarkdownLite('**H *i* W**')}</>)
    expect(container.querySelector('strong')).not.toBeNull()
  })

  it('空输入返回空数组', () => {
    expect(renderMarkdownLite('')).toEqual([])
    expect(renderMarkdownLite(null)).toEqual([])
    expect(renderMarkdownLite(undefined)).toEqual([])
  })

  // ── BF-0420-13: bold 解析鲁棒性 ─────────────────────

  it('BF-0420-13 行内大量合法 ** + 1 孤立 → 合法部分照常加粗,末尾孤立删除', () => {
    // 触发样本:LLM 输出 19 个 **,18 个合法配对 + 1 个 Python 乘方误码
    // 期望:18 个合法 → 9 对 strong;末尾孤立 ** 直接删除(不字面保留)
    const { container } = render(
      <>{renderMarkdownLite('看 **A** 和 **B** 和 **C** 多,但有个 ** 残缺')}</>,
    )
    // 3 对合法 → 3 个 strong
    expect(container.querySelectorAll('strong')).toHaveLength(3)
    // 末尾孤立 ** 已删除,文本不应再含 ** 字面
    expect(container.textContent).not.toContain('**')
  })

  it('BF-0420-13 末尾段单 ** 整段被跳过,不渲染空 <p>', () => {
    const { container } = render(
      <>{renderMarkdownLite('正常段落\n\n**')}</>,
    )
    // 末尾 ** 删除后该段为空,toBlocks 跳过 → 只有 1 个 <p>
    expect(container.querySelectorAll('p')).toHaveLength(1)
    expect(container.textContent).toBe('正常段落')
  })

  it('BF-0420-13 行末孤立 ** 删除(LLM 漏写开头 ** 的常见模式)', () => {
    // Image 1/3 的 LLM 输出形态:`关键词**：xxx`
    const { container } = render(
      <>{renderMarkdownLite('不推送 megamerge**，只推送各分支')}</>,
    )
    expect(container.querySelector('strong')).toBeNull()
    expect(container.textContent).toBe('不推送 megamerge，只推送各分支')
  })

  it('BF-0420-13 空 bold **** 跳过,不渲染空 strong 也不留字面', () => {
    const { container } = render(<>{renderMarkdownLite('前 **** 后')}</>)
    expect(container.querySelector('strong')).toBeNull()
    // **** 跳过,不输出空 strong;cursor 推进到 4 后剩下 ' 后'
    // 但 line.slice(cursor)=' 后',前面 '前 ' 也没输出(因为 start=2 时 result += line.slice(0,2) 走在 if 内)
    // 严格说效果:前 后(中间空格保留)
    expect(container.textContent.replace(/\s+/g, ' ')).toContain('前')
    expect(container.textContent.replace(/\s+/g, ' ')).toContain('后')
  })

  it('BF-0420-13 行内多 ** 偶数个时按顺序两两配对', () => {
    const { container } = render(
      <>{renderMarkdownLite('**A** 和 **B** 和 **C**')}</>,
    )
    expect(container.querySelectorAll('strong')).toHaveLength(3)
  })

  it('BF-0420-13 跨段 ** 不扩散:每段独立配对,孤立 ** 各自删除', () => {
    const { container } = render(
      <>{renderMarkdownLite('**未闭合\n\n**已闭合**')}</>,
    )
    const strongs = container.querySelectorAll('strong')
    expect(strongs).toHaveLength(1)
    expect(strongs[0].textContent).toBe('已闭合')
    // 第一段孤立 ** 删除,文本只剩 "未闭合"
    expect(container.textContent).toContain('未闭合')
    expect(container.textContent).not.toContain('**')
  })

  // ── BF-0420-13: list 扩展 ─────────────────────

  it('BF-0420-13 • 也作为 bullet list', () => {
    const { container } = render(
      <>{renderMarkdownLite('• 项一\n• 项二')}</>,
    )
    expect(container.querySelectorAll('ul')).toHaveLength(1)
    expect(container.querySelectorAll('li')).toHaveLength(2)
  })

  it('BF-0420-13 1. 数字列表渲染为 <ol>', () => {
    const { container } = render(
      <>{renderMarkdownLite('1. 第一\n2. 第二\n3. 第三')}</>,
    )
    expect(container.querySelectorAll('ol')).toHaveLength(1)
    expect(container.querySelectorAll('ol > li')).toHaveLength(3)
    expect(container.querySelectorAll('ol > li')[1].textContent).toBe('第二')
  })

  it('BF-0420-13 1、 中文顿号也作为 numbered list', () => {
    // 顿号后必须有空格,符合 LLM 主流输出 (1. xxx / 1、 xxx)
    const { container } = render(<>{renderMarkdownLite('1、 甲\n2、 乙')}</>)
    expect(container.querySelectorAll('ol')).toHaveLength(1)
    expect(container.querySelectorAll('li')).toHaveLength(2)
  })

  it('BF-0420-13 ul 和 ol 混合时各自独立块', () => {
    const { container } = render(
      <>{renderMarkdownLite('- a\n- b\n\n1. x\n2. y')}</>,
    )
    expect(container.querySelectorAll('ul')).toHaveLength(1)
    expect(container.querySelectorAll('ol')).toHaveLength(1)
  })

  // ── BF-0420-13 rev4: # 标题 / 表格 / 代码块 优雅降级 ─────────────────────

  it('BF-0420-13 # 标题转加粗段落,剥 # 前缀', () => {
    const { container } = render(<>{renderMarkdownLite('# 一、核心架构\n正文段')}</>)
    // 标题渲染为 <p><strong>一、核心架构</strong></p>
    const strongs = container.querySelectorAll('strong')
    expect(strongs.length).toBeGreaterThanOrEqual(1)
    expect(strongs[0].textContent).toBe('一、核心架构')
    // 不应有字面 #
    expect(container.textContent).not.toMatch(/^#/)
    expect(container.textContent).toContain('正文段')
  })

  it('BF-0420-13 ## / ### 多级标题都剥前缀', () => {
    const { container } = render(
      <>{renderMarkdownLite('## 二、模块\n### 工具选型')}</>,
    )
    expect(container.textContent).not.toContain('##')
    expect(container.textContent).not.toContain('###')
    expect(container.textContent).toContain('二、模块')
    expect(container.textContent).toContain('工具选型')
  })

  it('BF-0420-13 ``` 代码块围栏行跳过', () => {
    const { container } = render(
      <>{renderMarkdownLite('段前\n```python\ncode line\n```\n段后')}</>,
    )
    expect(container.textContent).not.toContain('```')
    expect(container.textContent).toContain('段前')
    expect(container.textContent).toContain('code line')
    expect(container.textContent).toContain('段后')
  })

  it('BF-0420-13 |---| 表格分隔行跳过,数据行剥两端 |', () => {
    const { container } = render(
      <>{renderMarkdownLite('| 工具 | 用途 |\n|---|---|\n| Zapier | 自动化 |')}</>,
    )
    // 不应有 |---| 字面
    expect(container.textContent).not.toContain('---')
    // 数据行变成 "工具 | 用途",中间 | 保留
    expect(container.textContent).toContain('工具 | 用途')
    expect(container.textContent).toContain('Zapier | 自动化')
  })

  it('BF-0420-13 > 引用剥前缀当普通段落', () => {
    const { container } = render(<>{renderMarkdownLite('> 这是引用')}</>)
    expect(container.textContent).toBe('这是引用')
  })
})
