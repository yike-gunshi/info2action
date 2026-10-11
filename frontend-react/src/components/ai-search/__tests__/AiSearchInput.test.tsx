import { readFileSync } from 'node:fs'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { AiSearchInput } from '../AiSearchInput'
import { useAuthStore } from '../../../store/authStore'
import { useAiSearchStore } from '../../../store/aiSearchStore'

const submit = vi.fn()
const originalSubmit = useAiSearchStore.getState().submit
const stylesheet = document.createElement('style')
const firstPlaceholder = '输入你的问题，基于站内内容进行 AI 总结'
const secondPlaceholder = 'seedance 2.5 怎么用'
const placeholder = () => document.querySelector<HTMLElement>('.ai-search-placeholder')!
const advance = (ms: number) => act(() => vi.advanceTimersByTime(ms))

beforeAll(() => {
  // Vitest disables CSS imports. jsdom also needs custom properties applied
  // directly to the overlay because it does not inherit them from :root.
  stylesheet.textContent = [
    readFileSync('src/components/ai-search/ai-search.tokens.css', 'utf8').replace(':root', ':root, .ai-search-placeholder'),
    readFileSync('src/components/ai-search/ai-search.css', 'utf8'),
    readFileSync('src/components/ai-search/aiSearchGlow.css', 'utf8'),
  ].join('\n')
  document.head.append(stylesheet)
})
afterAll(() => stylesheet.remove())

function media(reduced = false) {
  const listeners = new Set<() => void>()
  const query = {
    matches: reduced,
    addEventListener: (_event: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_event: string, listener: () => void) => listeners.delete(listener),
  }
  vi.stubGlobal('matchMedia', vi.fn(() => query))
  return (value: boolean) => {
    query.matches = value
    act(() => listeners.forEach(listener => listener()))
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  useAiSearchStore.getState().reset()
  useAuthStore.setState({ user: { id: 'u', username: 'u', email: 'u@example.com', role: 'user' }, isChecked: true, isLoading: false })
  useAiSearchStore.setState({ submit, unavailable: false, searchStatus: null, statusLoading: false })
  submit.mockReset()
  media()
})
afterEach(() => {
  cleanup()
  useAiSearchStore.setState({ submit: originalSubmit })
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('AiSearchInput', () => {
  it('submits Enter through a bubbling, cancelable submit event on Safari without requestSubmit and advertises a search key', () => {
    vi.spyOn(HTMLFormElement.prototype, 'requestSubmit')
    Object.defineProperty(HTMLFormElement.prototype, 'requestSubmit', { configurable: true, value: undefined })
    render(<AiSearchInput />)
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: '旧版 Safari 问题' } })
    const button = screen.getByRole('button', { name: 'AI 搜索' })
    const click = vi.spyOn(button, 'click')
    const submitted = vi.fn()
    input.closest('form')!.addEventListener('submit', submitted)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(click).not.toHaveBeenCalled()
    expect(submitted).toHaveBeenCalledOnce()
    const event = submitted.mock.calls[0][0] as Event
    expect(event.bubbles).toBe(true)
    expect(event.cancelable).toBe(true)
    expect(event.defaultPrevented).toBe(true)
    expect(submit).toHaveBeenCalledOnce()
    expect(submit).toHaveBeenCalledWith('旧版 Safari 问题')
    expect(input).toHaveAttribute('enterkeyhint', 'search')
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
    expect(submit).toHaveBeenCalledOnce()
    expect(submitted).toHaveBeenCalledOnce()
  })

  it.each(['', '   ', '字'.repeat(201)])('validates Enter consistently without requestSubmit, including a disabled empty submit button: %s', value => {
    vi.spyOn(HTMLFormElement.prototype, 'requestSubmit')
    Object.defineProperty(HTMLFormElement.prototype, 'requestSubmit', { configurable: true, value: undefined })
    render(<AiSearchInput />)
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value } })
    if (!value.trim()) expect(screen.getByRole('button', { name: 'AI 搜索' })).toBeDisabled()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(screen.getByRole('status')).toHaveTextContent('请输入 1 到 200 字')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveValue(value)
    expect(submit).not.toHaveBeenCalled()
  })

  it.each(['login_required', 'quota_exhausted'] as const)('keeps submission blocked for %s when the Safari fallback dispatches submit', reason => {
    vi.spyOn(HTMLFormElement.prototype, 'requestSubmit')
    Object.defineProperty(HTMLFormElement.prototype, 'requestSubmit', { configurable: true, value: undefined })
    if (reason === 'login_required') useAuthStore.setState({ user: null })
    useAiSearchStore.setState({ query: '待提交的问题', searchStatus: {
      enabled: true, logged_in: reason !== 'login_required', can_search: false, reason, user_day_limit: 2,
    } })
    render(<AiSearchInput />)
    const input = screen.getByRole('textbox')
    expect(screen.getByRole('button', { name: 'AI 搜索' })).toBeDisabled()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(submit).not.toHaveBeenCalled()
    expect(input).toHaveAttribute('aria-invalid', 'false')
    expect(screen.getByRole('status')).toHaveTextContent(reason === 'login_required' ? 'AI 搜索需要登录' : '当前额度已用完，明天 0 点恢复')
  })
  it.each(['login_required', 'quota_exhausted'] as const)('marks %s for gray disabled styling while leaving the question editable', reason => {
    if (reason === 'login_required') useAuthStore.setState({ user: null })
    useAiSearchStore.setState({ query: '可以修改的问题', searchStatus: {
      enabled: true, logged_in: reason !== 'login_required', can_search: false, reason, user_day_limit: 2,
    } })
    render(<AiSearchInput />)
    const button = screen.getByRole('button', { name: 'AI 搜索' })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('data-blocked', 'true')
    const input = screen.getByRole('textbox')
    expect(input).toBeEnabled()
    fireEvent.change(input, { target: { value: '修改后的问题' } })
    expect(input).toHaveValue('修改后的问题')
    fireEvent.submit(input.closest('form')!)
    expect(submit).not.toHaveBeenCalled()
  })

  it.each([false, true])('submits Enter without inserting a newline, shift=%s', shiftKey => {
    render(<AiSearchInput />)
    const input = screen.getByRole('textbox')
    expect(input.tagName).toBe('TEXTAREA')
    expect(input).toHaveAttribute('rows', '1')
    fireEvent.change(input, { target: { value: '测试问题' } })
    const event = new KeyboardEvent('keydown', { key: 'Enter', shiftKey, bubbles: true, cancelable: true })
    fireEvent(input, event)
    expect(event.defaultPrevented).toBe(true)
    expect(submit).toHaveBeenCalledWith('测试问题')
    expect(input).toHaveValue('测试问题')
  })

  it.each([{ isComposing: true }, { keyCode: 229 }])('leaves IME confirmation Enter alone: %j', options => {
    render(<AiSearchInput />)
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: '中文选字' } })
    const event = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true, ...options })
    fireEvent(input, event)
    expect(event.defaultPrevented).toBe(false)
    expect(submit).not.toHaveBeenCalled()
  })

  it('replaces all pasted newline forms with spaces', () => {
    render(<AiSearchInput />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '第一行\r\n第二行\n第三行\r第四行' } })
    expect(screen.getByRole('textbox')).toHaveValue('第一行 第二行 第三行 第四行')
    expect(useAiSearchStore.getState().query).toBe('第一行 第二行 第三行 第四行')
  })

  it('grows in layout on value and container width changes, then shrinks to the token minimum', () => {
    let resize: ResizeObserverCallback = () => {}
    const disconnect = vi.fn()
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: ResizeObserverCallback) { resize = callback }
      observe = vi.fn()
      disconnect = disconnect
    })
    document.documentElement.style.setProperty('--ai-search-h', '48px')
    try {
      let width = 700
      vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(() => ({ width } as DOMRect))
      const { unmount } = render(<AiSearchInput />)
      const input = screen.getByRole('textbox')
      let height = 24
      vi.spyOn(input, 'scrollHeight', 'get').mockImplementation(() => height)
      fireEvent.change(input, { target: { value: '短问题' } })
      expect(input.style.height).toBe('48px')
      height = 120
      fireEvent.change(input, { target: { value: '长问题'.repeat(50) } })
      expect(input.style.height).toBe('120px')
      height = 144
      width = 600
      act(() => resize([], {} as ResizeObserver))
      expect(input.style.height).toBe('144px')
      height = 24
      fireEvent.change(input, { target: { value: '' } })
      expect(input.style.height).toBe('48px')
      unmount()
      expect(disconnect).toHaveBeenCalled()
    } finally {
      document.documentElement.style.removeProperty('--ai-search-h')
    }
  })

  it('announces action feedback through the existing permanently mounted status line', () => {
    const { rerender } = render(<AiSearchInput />)
    const hint = screen.getByRole('status')
    rerender(<AiSearchInput feedback="已复制正文与参考信息" />)
    expect(screen.getByRole('status')).toBe(hint)
    expect(hint).toHaveTextContent('已复制正文与参考信息')
    expect(hint).not.toHaveClass('ai-search-hint-empty')
    rerender(<AiSearchInput feedback="" />)
    expect(screen.getByRole('status')).toBe(hint)
    expect(hint).toBeEmptyDOMElement()
    expect(hint).toHaveStyle('height: 0px; overflow: hidden; display: block; visibility: visible')
  })

  it('keeps the same live region mounted with zero height while its message is empty', () => {
    render(<AiSearchInput />)
    const input = screen.getByRole('textbox')
    const hint = screen.getByRole('status')
    expect(hint).toBeEmptyDOMElement()
    expect(hint).toHaveStyle('height: 0px; min-height: 0; margin-top: 0px; overflow: hidden; display: block; visibility: visible')
    expect(input).not.toHaveAttribute('aria-describedby')

    fireEvent.submit(input.closest('form')!)
    expect(screen.getByRole('status')).toBe(hint)
    expect(hint).toHaveTextContent('请输入 1 到 200 字')
    expect(hint).toHaveAttribute('class', 'ai-search-hint')
    expect(input).toHaveAttribute('aria-describedby', 'ai-search-hint')

    fireEvent.change(input, { target: { value: 'Seedance 2.5' } })
    expect(screen.getByRole('status')).toBe(hint)
    expect(hint).toBeEmptyDOMElement()
    expect(hint).toHaveStyle('height: 0px; min-height: 0; margin-top: 0px; overflow: hidden; display: block; visibility: visible')
    expect(input).not.toHaveAttribute('aria-describedby')

    act(() => useAiSearchStore.setState({ searchStatus: { enabled: true, logged_in: true, can_search: false, reason: 'quota_exhausted', user_day_limit: 2 } }))
    expect(screen.getByRole('status')).toBe(hint)
    expect(hint).toHaveTextContent('当前额度已用完，明天 0 点恢复')
    expect(hint).not.toHaveClass('ai-search-hint-empty')
    expect(input).toHaveAttribute('aria-describedby', 'ai-search-hint')
  })

  it.each(['', '   ', '字'.repeat(201)])('rejects invalid query length', (value) => {
    render(<AiSearchInput />)
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value } })
    fireEvent.submit(input.closest('form')!)
    expect(screen.getByText('请输入 1 到 200 字')).toBeInTheDocument()
    expect(submit).not.toHaveBeenCalled()
  })

  it('submits a trimmed query with Enter/form submission and keeps the text', () => {
    render(<AiSearchInput />)
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: '  Seedance 2.5  ' } })
    fireEvent.submit(input.closest('form')!)
    expect(submit).toHaveBeenCalledWith('Seedance 2.5')
    expect(input).toHaveValue('  Seedance 2.5  ')
  })

  it('counts Unicode characters rather than UTF-16 units', () => {
    render(<AiSearchInput />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '😀'.repeat(200) } })
    fireEvent.click(screen.getByRole('button', { name: 'AI 搜索' }))
    expect(submit).toHaveBeenCalledOnce()
  })

  it('把「AI 搜索」按钮放在框外，空输入时不可点', () => {
    // 框内圆形箭头把名称挤掉了、只能靠 aria-label 承接；
    // 改回外置文字按钮后名称回到可见位置（用户 2026-09-22）。
    render(<AiSearchInput />)
    const button = screen.getByRole('button', { name: 'AI 搜索' })
    const input = screen.getByRole('textbox', { name: 'AI 搜索' })
    expect(screen.getAllByRole('button')).toHaveLength(1)
    expect(button.closest('.ai-search-input-inner')).toBeNull()   // 在框外
    expect(button).toHaveTextContent('AI 搜索')
    expect(button).toBeDisabled()
    fireEvent.click(button)
    expect(submit).not.toHaveBeenCalled()

    fireEvent.change(input, { target: { value: 'Seedance 2.5' } })
    expect(button).toBeEnabled()
    expect(placeholder()).not.toBeVisible()
    fireEvent.change(input, { target: { value: '   ' } })
    expect(button).toBeDisabled()
    fireEvent.change(input, { target: { value: '' } })
    expect(button).toBeDisabled()
  })

  it('lights an empty unfocused idle field, and goes dark once the field is in use', () => {
    // 光的职责是「邀请你输入」。一旦框拿到焦点，用户已经在打字，
    // 它就该让位，不跟光标和选区抢注意力（用户 2026-09-22 反馈）。
    render(<AiSearchInput />)
    const glow = screen.getByTestId('ai-search-glow')
    const input = screen.getByRole('textbox')
    expect(glow).toHaveStyle('opacity: 1')
    fireEvent.focus(input)
    expect(glow).toHaveStyle('opacity: 0')
    fireEvent.change(input, { target: { value: 'Seedance 2.5' } })
    expect(glow).toHaveStyle('opacity: 0')
    fireEvent.change(input, { target: { value: '' } })
    expect(glow).toHaveStyle('opacity: 0')   // 仍然聚焦
    fireEvent.blur(input)
    expect(glow).toHaveStyle('opacity: 1')   // 失焦且为空，邀请回来
  })

  it.each(['queued', 'running'] as const)('keeps the glow lit throughout a long %s summary with a filled query', status => {
    useAiSearchStore.setState({ phase: 'results', query: 'Seedance 2.5', summary: { ...useAiSearchStore.getState().summary, status } })
    render(<AiSearchInput />)
    const glow = screen.getByTestId('ai-search-glow')
    expect(glow).toHaveStyle('opacity: 1')
    expect(glow.querySelector('.ai-search-blob')).toHaveStyle('animation-duration: var(--ai-glow-loading-ms)')
    advance(15 * 60 * 1000)
    expect(glow).toHaveStyle('opacity: 1')
    expect(screen.getByRole('textbox')).toHaveValue('Seedance 2.5')
    act(() => useAiSearchStore.setState({ summary: { ...useAiSearchStore.getState().summary, status: 'done' } }))
    expect(glow).toHaveStyle('opacity: 0')
  })

  it.each(['done', 'failed', 'unavailable', 'none'] as const)('keeps a terminal %s result unlit, even with an empty field', status => {
    useAiSearchStore.setState({ phase: 'results', summary: { ...useAiSearchStore.getState().summary, status } })
    render(<AiSearchInput />)
    expect(screen.getByTestId('ai-search-glow')).toHaveStyle('opacity: 0')
  })

  it('keeps empty results and failed or limited requests unlit', () => {
    useAiSearchStore.setState({ phase: 'results', emptyResult: true, summary: { ...useAiSearchStore.getState().summary, status: 'queued' } })
    render(<AiSearchInput />)
    const glow = screen.getByTestId('ai-search-glow')
    expect(glow).toHaveStyle('opacity: 0')
    act(() => useAiSearchStore.setState({ phase: 'idle', searchStatus: { enabled: true, logged_in: true, can_search: false, reason: 'quota_exhausted', user_day_limit: 2 } }))
    expect(glow).toHaveStyle('opacity: 0')
    act(() => useAiSearchStore.setState({ searchStatus: null, requestError: '网络异常，请稍后再试' }))
    expect(glow).toHaveStyle('opacity: 0')
  })

  it('shows a spinner during submission, preserves the query, and prevents repeat submissions', () => {
    useAiSearchStore.setState({ phase: 'submitting', query: 'Seedance 2.5' })
    render(<AiSearchInput />)
    const input = screen.getByRole('textbox')
    // 提交中按钮文字变成「AI 搜索中…」，可访问名随之改变
    const button = screen.getByRole('button', { name: 'AI 搜索中…' })
    expect(input).toBeDisabled()
    expect(input).toHaveValue('Seedance 2.5')
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('aria-busy', 'true')
    // 外置按钮用文字表达提交中，不再有框内箭头与旋转图标
    expect(button).toHaveTextContent('AI 搜索中…')
    expect(screen.getByTestId('ai-search-glow')).toHaveStyle('opacity: 1')
    fireEvent.submit(input.closest('form')!)
    expect(submit).not.toHaveBeenCalled()
  })

  it('rotates an aria-hidden overlay with a blank beat and a stable input label', () => {
    render(<AiSearchInput />)
    const overlay = placeholder()
    const input = screen.getByRole('textbox', { name: 'AI 搜索' })
    expect(input).not.toHaveAttribute('placeholder')
    expect(overlay).toHaveAttribute('aria-hidden', 'true')
    expect(overlay).toHaveTextContent(firstPlaceholder)
    advance(2999)
    expect(overlay).toHaveAttribute('data-transition', 'still')
    advance(1)
    expect(overlay).toHaveAttribute('data-transition', 'out')
    expect(overlay).toHaveTextContent(firstPlaceholder)
    advance(300)
    expect(overlay).toHaveAttribute('data-transition', 'gap')
    expect(overlay).toHaveStyle('opacity: 0')
    advance(300)
    expect(overlay).toHaveAttribute('data-transition', 'in')
    expect(overlay).toHaveTextContent(secondPlaceholder)
    expect(input).toHaveAccessibleName('AI 搜索')
    expect(screen.getByRole('status')).toBeEmptyDOMElement()
    advance(3000)
    expect(overlay).toHaveTextContent('最近有什么新的开源 TTS')
    advance(3000)
    expect(overlay).toHaveTextContent('GPT-6 Astra 有什么变化')
    advance(3000)
    expect(overlay).toHaveTextContent('本周值得一看的 AI 视频作品')
    advance(3000)
    expect(overlay).toHaveTextContent(firstPlaceholder)
  })

  it('stops all placeholder timers when the page is hidden, including mid-transition', () => {
    let hidden = false
    vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden)
    render(<AiSearchInput />)
    advance(3000)
    expect(placeholder()).toHaveAttribute('data-transition', 'out')
    hidden = true
    fireEvent(document, new Event('visibilitychange'))
    expect(vi.getTimerCount()).toBe(0)
    advance(10000)
    expect(placeholder()).toHaveTextContent(firstPlaceholder)
    hidden = false
    fireEvent(document, new Event('visibilitychange'))
    advance(3600)
    expect(placeholder()).toHaveTextContent(secondPlaceholder)
  })

  it('stops cycling while typing and cleans up every timer on unmount', () => {
    const { unmount } = render(<AiSearchInput />)
    advance(3000)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'query' } })
    expect(vi.getTimerCount()).toBe(0)
    expect(placeholder()).not.toBeVisible()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '' } })
    advance(3600)
    expect(placeholder()).toHaveTextContent(secondPlaceholder)
    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('respects reduced motion on mount and when the preference changes', () => {
    const changeReduced = media(true)
    render(<AiSearchInput />)
    expect(vi.getTimerCount()).toBe(0)
    expect(placeholder()).toHaveTextContent(firstPlaceholder)
    changeReduced(false)
    advance(3600)
    expect(placeholder()).toHaveTextContent(secondPlaceholder)
    changeReduced(true)
    expect(vi.getTimerCount()).toBe(0)
    advance(10000)
    expect(placeholder()).toHaveTextContent(secondPlaceholder)
  })

  it('取既有任务期间冻结输入与提交', () => {
    // 内容正在被替换，让人打字只会打一半被覆盖（用户 2026-09-22 反馈）。
    useAiSearchStore.setState({ loadingJob: true, phase: 'results' })
    render(<AiSearchInput />)
    expect(screen.getByRole('textbox')).toBeDisabled()
    expect(screen.getByRole('button', { name: 'AI 搜索' })).toBeDisabled()
    expect(screen.getByTestId('ai-search-glow')).toHaveStyle('opacity: 0')
  })
})
