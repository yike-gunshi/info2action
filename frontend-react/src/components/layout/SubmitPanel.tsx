import { useState, useEffect, useRef, useCallback } from 'react'
import { Link2, X, Loader2, Check, AlertCircle, ExternalLink } from 'lucide-react'
import { toast } from 'sonner'
import { cn, relativeTime } from '../../lib/utils'
import { fetchFeedItem, fetchSubmitStatus, submitUrl } from '../../lib/api'
import { submitRecordsStorageKey } from '../../lib/submitRecords'
import { useDetailStore } from '../../store/detailStore'
import { useAuthStore } from '../../store/authStore'

interface SubmitRecord {
  url: string
  title?: string
  status: 'pending' | 'done' | 'error' | 'duplicate' | 'invalid'
  itemId?: string
  error?: string
  submittedAt: string
  taskId?: string  // BF-0419-19: 存 task_id 以便刷新后恢复轮询
}

const panelSurfaceClass = 'bg-[color-mix(in_srgb,var(--card)_96%,var(--background))]'
const focusRingClass = 'focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background'

function readSubmitRecords(storageKey: string): SubmitRecord[] {
  try {
    return JSON.parse(localStorage.getItem(storageKey) || '[]')
  } catch {
    return []
  }
}

function submitStatusText(record: SubmitRecord): string {
  if (record.status === 'pending') return '分析中'
  if (record.status === 'done') return '已完成'
  if (record.status === 'duplicate') return '已存在'
  if (record.status === 'invalid') return '已失效'
  return record.error || '失败'
}

/**
 * Submit URL panel — dropdown from topbar "+" button.
 * - Submit URL → background processing → toast on completion
 * - Dropdown shows submission history (latest first)
 */
export function SubmitPanel() {
  const [isOpen, setIsOpen] = useState(false)
  const [inputValue, setInputValue] = useState('')
  const [inputError, setInputError] = useState('')
  const userId = useAuthStore((s) => s.user?.id ?? null)
  const storageKey = submitRecordsStorageKey(userId)
  const [loadedStorageKey, setLoadedStorageKey] = useState(storageKey)
  const [records, setRecords] = useState<SubmitRecord[]>(() => readSubmitRecords(storageKey))
  const panelRef = useRef<HTMLDivElement>(null)
  const openItem = useDetailStore((s) => s.openItem)

  // Review 修:跟踪正在轮询的 taskId,防 StrictMode / HMR / 重复 mount 叠 interval
  const pollingRef = useRef<Map<string, () => void>>(new Map())

  const POLL_TIMEOUT_MS = 180000  // 3 min, 和"任务已过期"文案要同步

  const stopAllPolling = useCallback(() => {
    pollingRef.current.forEach((stop) => stop())
    pollingRef.current.clear()
  }, [])

  useEffect(() => {
    if (storageKey === loadedStorageKey) return
    stopAllPolling()
    setLoadedStorageKey(storageKey)
    setRecords(readSubmitRecords(storageKey))
  }, [loadedStorageKey, storageKey, stopAllPolling])

  useEffect(() => () => stopAllPolling(), [stopAllPolling])

  // Persist records under the current user's namespace.
  useEffect(() => {
    if (loadedStorageKey !== storageKey) return
    localStorage.setItem(storageKey, JSON.stringify(records.slice(0, 20)))
  }, [loadedStorageKey, records, storageKey])

  // Close on outside click
  useEffect(() => {
    if (!isOpen) return
    const handler = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        setIsOpen(false)
      }
    }
    setTimeout(() => document.addEventListener('click', handler), 0)
    return () => document.removeEventListener('click', handler)
  }, [isOpen])

  // BF-0419-19: 抽出轮询函数,handleSubmit + mount 恢复都复用
  const startPolling = useCallback((taskId: string, url: string) => {
    // Review 修:已在跑的 taskId 直接 return,防 StrictMode / HMR / 多次 mount 叠 interval
    if (pollingRef.current.has(taskId)) return
    const stop = () => {
      clearInterval(poll)
      clearTimeout(killer)
      pollingRef.current.delete(taskId)
    }
    pollingRef.current.set(taskId, stop)
    const poll = setInterval(async () => {
      try {
        const data = await fetchSubmitStatus(taskId)
        if (data.status === 'done') {
          stop()
          const item = data.item as Record<string, unknown> | undefined
          const title = (data.title as string) || (item?.title as string) || url
          const itemId = String(item?.id || taskId)
          setRecords((prev) =>
            prev.map((r) =>
              r.taskId === taskId && r.status === 'pending'
                ? { ...r, status: 'done', title, itemId }
                : r,
          ))
          toast.success(`${title.slice(0, 40)} 分析完毕`, {
            action: { label: '查看', onClick: () => openItem(itemId) },
          })
        } else if (data.status === 'error') {
          stop()
          const errorMessage = typeof data.error === 'string' ? data.error : '分析失败'
          setRecords((prev) =>
            prev.map((r) =>
              r.taskId === taskId && r.status === 'pending'
                ? { ...r, status: 'error', error: errorMessage }
                : r,
          ))
          toast.error(errorMessage)
        }
        // fetching/processing → keep polling
      } catch (e) {
        const err = e as Error & { status?: number }
        // 404 = 内存丢 + DB 也没 → 任务真的没了,标记 error
        if (err.status === 404) {
          stop()
          setRecords((prev) =>
            prev.map((r) =>
              r.taskId === taskId && r.status === 'pending'
                ? { ...r, status: 'error', error: '任务已过期(后端重启或超时)' }
                : r,
            ))
        }
      }
    }, 3000)
    const killer = setTimeout(stop, POLL_TIMEOUT_MS)
  }, [openItem])

  // BF-0419-19: 组件 mount 时扫描历史 pending 记录恢复轮询;旧版无 taskId 的 pending 一次性清理
  useEffect(() => {
    const pendings = records.filter(r => r.status === 'pending' && r.taskId)
    pendings.forEach(r => {
      if (r.taskId) startPolling(r.taskId, r.url)
    })
    // 无 taskId 的历史 pending 是旧版遗留 → 直接标记过期,避免红圈一直转
    const orphans = records.some(r => r.status === 'pending' && !r.taskId)
    if (orphans) {
      setRecords(prev => prev.map(r =>
        (r.status === 'pending' && !r.taskId)
          ? { ...r, status: 'error', error: '旧版本记录,无法恢复(请删除或重新提交)' }
          : r,
      ))
    }
    // 只 mount 时跑一次 — records deps 故意省略(避免每次 setRecords 重启轮询)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleSubmit = useCallback(async () => {
    const url = inputValue.trim()
    if (!url) return
    setInputError('')

    // Validate URL
    try {
      const parsed = new URL(url)
      if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error()
    } catch {
      setInputError('请输入有效的链接')
      return
    }

    // Add pending record
    const record: SubmitRecord = {
      url,
      status: 'pending',
      submittedAt: new Date().toISOString(),
    }
    setRecords((prev) => [record, ...prev])
    setInputValue('')

    // Submit → backend returns task_id or existing item
    submitUrl(url)
      .then((res: Record<string, unknown>) => {
        // Case 1: Already exists with full data
        if (res.done) {
          const item = res.item as Record<string, unknown>
          const itemId = String(item?.id || '')
          const title = (item?.title as string) || url
          setRecords((prev) =>
            prev.map((r) =>
              r.url === url && r.status === 'pending'
                ? { ...r, status: 'duplicate', title, itemId }
                : r,
          ))
          toast.info(`${title.slice(0, 40)} 已存在`, {
            action: { label: '查看', onClick: () => openItem(itemId) },
          })
          return
        }

        // Case 2: New submission → poll /api/submit-url/status
        const taskId = res.task_id as string
        if (!taskId) return

        // BF-0419-19: 把 taskId 写入 record,刷新后 mount 时能恢复轮询
        setRecords((prev) =>
          prev.map((r) =>
            r.url === url && r.status === 'pending'
              ? { ...r, taskId }
              : r,
          ))
        startPolling(taskId, url)
      })
      .catch((err) => {
        setRecords((prev) =>
          prev.map((r) =>
            r.url === url && r.status === 'pending'
              ? { ...r, status: 'error', error: err.message }
              : r,
          ),
        )
        toast.error(`提交失败: ${err.message}`)
      })
  }, [inputValue, openItem, startPolling])

  const hasPending = records.some((r) => r.status === 'pending')

  return (
    <div className="relative" ref={panelRef}>
      {/* Submit link trigger — kept as a bare editorial utility icon in TopBar. */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className={cn(
          'relative inline-flex h-9 w-9 items-center justify-center rounded-[4px] text-muted-foreground transition-colors hover:text-foreground',
          focusRingClass,
        )}
        aria-label="提交链接"
        title="提交链接"
      >
        <Link2 className="h-[19px] w-[19px]" strokeWidth={1.6} />
        {hasPending && (
          <span className="absolute right-1 top-1 h-2 w-2 animate-pulse rounded-full bg-[var(--brand)]" />
        )}
      </button>

      {/* Dropdown panel */}
      {isOpen && (
        <div
          data-testid="submit-panel-popover"
          className={cn(
            'fixed left-4 right-4 top-[52px] z-[9999] w-auto max-w-none rounded-[6px] border border-border/90 shadow-[0_10px_30px_rgba(26,25,23,0.08)] dark:shadow-[0_18px_36px_rgba(0,0,0,0.36)] sm:absolute sm:left-auto sm:right-0 sm:top-full sm:mt-2 sm:w-[380px] sm:max-w-[calc(100vw-32px)]',
            panelSurfaceClass,
          )}
        >
          <span
            aria-hidden="true"
            className={cn(
              'absolute -top-[7px] right-[104px] h-3 w-3 rotate-45 border-l border-t border-border/90 sm:right-[14px]',
              panelSurfaceClass,
            )}
          />
          {/* Header */}
          <div className="flex items-center justify-between px-5 pb-3 pt-4">
            <h3 className="font-body-cjk text-[15px] font-semibold leading-none text-foreground">提交链接</h3>
            <button
              onClick={() => setIsOpen(false)}
              className={cn(
                'inline-flex h-8 w-8 items-center justify-center rounded-[4px] text-muted-foreground transition-colors hover:text-foreground',
                focusRingClass,
              )}
              aria-label="关闭提交链接"
            >
              <X className="h-[18px] w-[18px]" strokeWidth={1.6} />
            </button>
          </div>

          {/* Input row */}
          <div className="border-b border-border/80 px-5 pb-4">
            <div className="flex gap-2.5">
              <input
                type="url"
                value={inputValue}
                onChange={(e) => { setInputValue(e.target.value); setInputError('') }}
                onKeyDown={(e) => { if (e.key === 'Enter') handleSubmit() }}
                placeholder="粘贴链接..."
                className="h-10 min-w-0 flex-1 rounded-[4px] border border-input bg-background/70 px-3 font-body-cjk text-[13px] text-foreground transition-[border-color,box-shadow,background-color] placeholder:text-muted-foreground/70 focus:border-[var(--brand)] focus:bg-card focus:outline-none focus:ring-2 focus:ring-[color-mix(in_srgb,var(--brand)_14%,transparent)]"
              />
              <button
                onClick={handleSubmit}
                disabled={!inputValue.trim()}
                className="inline-flex h-10 min-w-[58px] items-center justify-center rounded-[4px] bg-[var(--brand)] px-4 font-body-cjk text-[13px] font-semibold text-[var(--brand-foreground)] transition-colors hover:bg-[color-mix(in_srgb,var(--brand)_88%,#171512)] disabled:cursor-not-allowed disabled:bg-muted disabled:text-muted-foreground disabled:hover:bg-muted"
              >
                提交
              </button>
            </div>
            {inputError && (
              <p className="mt-2 font-body-cjk text-[12px] text-destructive">{inputError}</p>
            )}
          </div>

          {/* History list */}
          <div className="max-h-[304px] overflow-x-hidden overflow-y-auto">
            {records.length === 0 ? (
              <div className="flex min-h-[126px] flex-col items-center justify-center px-5 py-7 text-center">
                <Link2 className="mb-3 h-6 w-6 text-muted-foreground/45" strokeWidth={1.5} />
                <div className="font-body-cjk text-[13px] text-muted-foreground">暂无提交记录</div>
              </div>
            ) : (
              <div className="divide-y divide-border/70">
                {records.map((record) => {
                  const statusText = submitStatusText(record)
                  return (
                    <div
                      key={`${record.url}-${record.submittedAt}`}
                      className={cn(
                        'flex min-h-[56px] min-w-0 items-start gap-3 px-5 py-3 transition-colors',
                        (record.status === 'done' || record.status === 'duplicate') && 'cursor-pointer hover:bg-background/75',
                        record.status === 'invalid' && 'opacity-60',
                      )}
                      onClick={async () => {
                        if ((record.status === 'done' || record.status === 'duplicate') && record.itemId) {
                          try {
                            await fetchFeedItem(record.itemId)
                            openItem(record.itemId)
                            setIsOpen(false)
                          } catch {
                            setRecords((prev) =>
                              prev.map((r) =>
                                r.url === record.url && r.submittedAt === record.submittedAt
                                  ? { ...r, status: 'invalid', error: '内容已不存在' }
                                  : r,
                              ),
                            )
                            toast.error('该内容已不存在或已被清理，可重新提交')
                          }
                        } else if ((record.status === 'done' || record.status === 'duplicate') && record.url) {
                          window.open(record.url, '_blank')
                          setIsOpen(false)
                        } else if (record.status === 'invalid' && record.url) {
                          window.open(record.url, '_blank')
                          setIsOpen(false)
                        }
                      }}
                    >
                      {/* Status indicator */}
                      <div className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center">
                        {record.status === 'pending' && (
                          <Loader2 className="h-[16px] w-[16px] animate-spin text-[var(--brand)]" strokeWidth={1.7} />
                        )}
                        {record.status === 'done' && (
                          <Check className="h-[16px] w-[16px] text-emerald" strokeWidth={1.9} />
                        )}
                        {record.status === 'duplicate' && (
                          <ExternalLink className="h-[16px] w-[16px] text-emerald" strokeWidth={1.8} />
                        )}
                        {record.status === 'error' && (
                          <AlertCircle className="h-[16px] w-[16px] text-destructive" strokeWidth={1.8} />
                        )}
                        {record.status === 'invalid' && (
                          <AlertCircle className="h-[16px] w-[16px] text-muted-foreground" strokeWidth={1.8} />
                        )}
                      </div>

                      {/* Content */}
                      <div className="min-w-0 flex-1">
                        <p className="truncate font-body-cjk text-[13px] leading-5 text-foreground">
                          {record.title || record.url}
                        </p>
                        <div className="mt-0.5 flex min-w-0 items-center gap-2 font-body-cjk text-[12px] leading-4">
                          <span
                            className={cn(
                              'min-w-0 truncate',
                              record.status === 'pending' && 'text-[var(--brand)]',
                              (record.status === 'done' || record.status === 'duplicate') && 'text-emerald',
                              record.status === 'error' && 'text-destructive',
                              record.status === 'invalid' && 'text-muted-foreground',
                            )}
                            title={statusText}
                          >
                            {statusText}
                          </span>
                          <span className="shrink-0 text-muted-foreground">
                            {relativeTime(record.submittedAt)}
                          </span>
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
