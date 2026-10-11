import { useEffect, useImperativeHandle, useLayoutEffect, useRef, useState, type Ref } from 'react'
import { useAiSearchStore } from '../../store/aiSearchStore'
import { useAuthStore } from '../../store/authStore'
import { QUERY_MAX_CHARS, QUERY_VALIDATION_MESSAGE, QUOTA_EXHAUSTED_MESSAGE } from '../../lib/api/aiSearch'
import { cssTimeMs } from './aiSearchScroll'
import './aiSearchGlow.css'

// The first line is final copy; the remaining four are replaceable examples.
const placeholders = [
  '输入你的问题，基于站内内容进行 AI 总结',
  'seedance 2.5 怎么用',
  '最近有什么新的开源 TTS',
  'GPT-6 Astra 有什么变化',
  '本周值得一看的 AI 视频作品',
]

function RotatingPlaceholder({ active }: { active: boolean }) {
  const element = useRef<HTMLSpanElement>(null)
  const [index, setIndex] = useState(0)
  const [transition, setTransition] = useState<'still' | 'out' | 'gap' | 'in'>('still')

  useEffect(() => {
    if (!active || !element.current) return
    const interval = cssTimeMs(element.current, '--ai-ph-interval')
    const duration = cssTimeMs(element.current, '--ai-ph-dur')
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)')
    let timer: ReturnType<typeof setInterval> | undefined
    let exitTimer: ReturnType<typeof setTimeout> | undefined
    let enterTimer: ReturnType<typeof setTimeout> | undefined
    const stop = () => {
      clearInterval(timer)
      clearTimeout(exitTimer)
      clearTimeout(enterTimer)
      setTransition('still')
    }
    const start = () => {
      stop()
      if (document.hidden || reduced?.matches || interval <= 0 || duration <= 0) return
      timer = setInterval(() => {
        setTransition('out')
        exitTimer = setTimeout(() => {
          setTransition('gap')
          // Keep a blank beat between the outgoing and incoming lines.
          enterTimer = setTimeout(() => {
            setIndex(value => (value + 1) % placeholders.length)
            setTransition('in')
          }, duration)
        }, duration)
      }, interval)
    }
    start()
    document.addEventListener('visibilitychange', start)
    reduced?.addEventListener?.('change', start)
    return () => {
      stop()
      document.removeEventListener('visibilitychange', start)
      reduced?.removeEventListener?.('change', start)
    }
  }, [active])

  return (
    <span ref={element} className="ai-search-placeholder" data-transition={transition} aria-hidden="true">
      <span>{placeholders[index]}</span>
    </span>
  )
}

function resizeTextarea(field: HTMLTextAreaElement) {
  field.style.height = 'auto'
  const minimum = Number.parseFloat(getComputedStyle(field).getPropertyValue('--ai-search-h')
    || getComputedStyle(document.documentElement).getPropertyValue('--ai-search-h')) || 0
  field.style.height = `${Math.max(minimum, field.scrollHeight)}px`
}

export function AiSearchInput({ feedback = '', inputRef }: { feedback?: string; inputRef?: Ref<HTMLTextAreaElement> }) {
  const fieldRef = useRef<HTMLTextAreaElement>(null)
  useImperativeHandle(inputRef, () => fieldRef.current!, [])
  const query = useAiSearchStore((s) => s.query)
  const setQuery = useAiSearchStore((s) => s.setQuery)
  const submit = useAiSearchStore((s) => s.submit)
  const phase = useAiSearchStore((s) => s.phase)
  const pendingSummary = useAiSearchStore((s) => !s.emptyResult && (s.summary.status === 'queued' || s.summary.status === 'running'))
  const searchStatus = useAiSearchStore((s) => s.searchStatus)
  const authReady = useAuthStore((s) => s.isChecked && !s.isLoading)
  const user = useAuthStore((s) => s.user)
  const requestError = useAiSearchStore((s) => s.requestError)
  const loginRequired = authReady && user?.role !== 'admin'
    && (searchStatus ? !searchStatus.logged_in || searchStatus.reason === 'login_required' : !user)
  const capped = user?.role !== 'admin' && searchStatus?.reason === 'quota_exhausted'
  const blocked = !authReady || loginRequired || searchStatus?.enabled === false
    || (user?.role !== 'admin' && searchStatus?.can_search === false)
  const [invalid, setInvalid] = useState(false)
  const [focused, setFocused] = useState(false)
  const submitting = phase === 'submitting'
  // 取既有任务时同样冻结输入：此刻内容正在替换，让人打字只会打一半被覆盖。
  const loadingJob = useAiSearchStore((s) => s.loadingJob)
  const frozen = submitting || loadingJob
  const loading = submitting || (phase === 'results' && pendingSummary)
  const hasText = query.length > 0
  // The glow invites you to type. Once the box has focus you are already typing,
  // so it stops competing with the caret and the selection (user feedback 2026-09-22).
  const glow = loading || (phase === 'idle' && !hasText && !focused && !capped && !requestError)
  const loginText = searchStatus?.user_day_limit === null ? 'AI 搜索需要登录'
    : `AI 搜索需要登录，每人每天可用 ${searchStatus?.user_day_limit ?? 2} 次`
  const hintText = loginRequired ? loginText : capped ? QUOTA_EXHAUSTED_MESSAGE : invalid ? QUERY_VALIDATION_MESSAGE : feedback

  useLayoutEffect(() => {
    if (fieldRef.current) resizeTextarea(fieldRef.current)
  }, [query])

  useLayoutEffect(() => {
    const field = fieldRef.current
    if (!field || typeof ResizeObserver === 'undefined') return
    // Observe the container's width; resizing the field's own height must not
    // cause a ResizeObserver loop.
    let width = field.parentElement!.getBoundingClientRect().width
    const observer = new ResizeObserver(() => {
      const nextWidth = field.parentElement!.getBoundingClientRect().width
      if (nextWidth === width) return
      width = nextWidth
      resizeTextarea(field)
    })
    observer.observe(field.parentElement!)
    return () => observer.disconnect()
  }, [])

  return (
    <form className="ai-search-form" data-loading={loading} data-glow={glow} data-typed={hasText} onSubmit={(event) => {
      event.preventDefault()
      if (frozen || blocked) return
      const value = query.trim()
      if (Array.from(value).length < 1 || Array.from(value).length > QUERY_MAX_CHARS) {
        setInvalid(true)
        return
      }
      setInvalid(false)
      void submit(value)
    }}>
      <div className="ai-search-input-wrap" data-testid="ai-search-input-wrap">
        <span aria-hidden="true" data-testid="ai-search-glow" className="ai-search-orbit">
          <span className="ai-search-blob" />
        </span>
        <div className="ai-search-input-inner">
          <textarea
            ref={fieldRef}
            rows={1}
            enterKeyHint="search"
            aria-label="AI 搜索"
            aria-describedby={hintText ? 'ai-search-hint' : undefined}
            aria-invalid={invalid}
            value={query}
            onChange={(event) => { setQuery(event.target.value.replace(/\r\n|\n|\r/g, ' ')); setInvalid(false) }}
            onKeyDown={(event) => {
              if (event.key !== 'Enter' || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
              event.preventDefault()
              const form = event.currentTarget.form
              if (form?.requestSubmit) form.requestSubmit()
              else form?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
            }}
            disabled={frozen}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            className="ai-search-input disabled:cursor-not-allowed disabled:opacity-60"
          />
          <RotatingPlaceholder active={!hasText && !submitting} />
        </div>
      </div>
      {/* 名称摆在可见位置。框内圆形箭头把「AI 搜索」挤掉了，只能靠 aria-label 承接
          （用户 2026-09-22 改回外置按钮）。 */}
      <button type="submit" aria-busy={frozen} data-blocked={blocked} disabled={frozen || blocked || !query.trim()}
        className="ai-search-submit-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] disabled:cursor-not-allowed">
        {submitting ? 'AI 搜索中…' : 'AI 搜索'}
      </button>
      <p id="ai-search-hint" role="status" className={`ai-search-hint${hintText ? '' : ' ai-search-hint-empty'}${capped ? ' text-[var(--destructive)]' : ''}`}>
        {hintText}{loginRequired ? <> · <a href="#login" className="text-[var(--brand)] hover:underline">登录</a></> : null}
      </p>
    </form>
  )
}
