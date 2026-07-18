/**
 * ClusterFullPage — cluster 落地页 /#cluster=:id (DESIGN.md §21.5, v24.0 报纸内页)
 *
 * 弹窗=快报,本页=内页深读+分享门面。版式语言延伸精选头版:
 * kicker 行(分类 ✦ · N 个来源 · M 条报道 · 时间) → 28px 衬线标题 → Scotch rule
 * → 双栏(左来源时间线 / 右 AI 速览+行动区)。交互结构(§15.10 双栏独立滚动)保留。
 *
 * merged_into redirect: store.modalState='redirecting' 时 history.replaceState 到新 id。
 */
import { useEffect, useMemo } from 'react'
import { ArrowLeft, ExternalLink } from 'lucide-react'
import { useClusterDetailStore } from '../store/clusterDetailStore'
import { ClusterLeftPanel } from '../components/cluster/ClusterLeftPanel'
import { ClusterRightPanel } from '../components/cluster/ClusterRightPanel'
import { BrandWordmark } from '../components/shared/BrandWordmark'
import { eventCategoryLabel } from '../lib/eventCategories'

interface ClusterFullPageProps {
  clusterId: number
}

function goBackOrClose() {
  const isNewTab = typeof window !== 'undefined' && window.opener != null
  if (isNewTab) {
    try {
      window.close()
      setTimeout(() => {
        if (!window.closed) window.location.hash = ''
      }, 50)
    } catch {
      window.location.hash = ''
    }
  } else if (window.history.length > 1) {
    window.history.back()
  } else {
    window.location.hash = ''
  }
}

export function ClusterFullPage({ clusterId }: ClusterFullPageProps) {
  const modalState = useClusterDetailStore((s) => s.modalState)
  const cluster = useClusterDetailStore((s) => s.cluster)
  const sources = useClusterDetailStore((s) => s.sources)
  const actions = useClusterDetailStore((s) => s.actions)
  const error = useClusterDetailStore((s) => s.error)
  const redirectTo = useClusterDetailStore((s) => s.redirectTo)
  const loadFullPage = useClusterDetailStore((s) => s.loadFullPage)
  const displayTitle = cluster?.ai_title || '事件详情'
  // v24.0 §21.5 kicker 行素材: 分类 ✦ · N 个来源 · M 条报道 · 时间
  const categoryLabel = eventCategoryLabel(cluster?.category)
  const sourceCount = cluster?.unique_source_count ?? sources.length
  const kickerTimeRaw = cluster?.last_doc_at || cluster?.first_doc_at
  const kickerTime = kickerTimeRaw
    ? new Date(kickerTimeRaw).toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      })
    : null
  const sortedSources = useMemo(() => {
    return [...sources].sort((a, b) => {
      const aTime = a.published_at ? new Date(a.published_at).getTime() : 0
      const bTime = b.published_at ? new Date(b.published_at).getTime() : 0
      return bTime - aTime
    })
  }, [sources])
  const singleSource = sortedSources.length === 1 ? sortedSources[0] : null

  // 加载数据（包含 click 打点 + actions 列表）
  useEffect(() => {
    loadFullPage(clusterId)
  }, [clusterId, loadFullPage])

  // merged_into redirect → history.replaceState 到新 id（不破坏堆栈）
  useEffect(() => {
    if (modalState === 'redirecting' && redirectTo) {
      const newHash = `cluster=${redirectTo}`
      window.history.replaceState({}, '', `#${newHash}`)
      // 触发 hashchange 让 App.tsx state 更新
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    }
  }, [modalState, redirectTo])

  // document.title 跟随 cluster
  useEffect(() => {
    if (cluster) {
      const prev = document.title
      document.title = `${displayTitle} | Info2Act`
      return () => {
        document.title = prev
      }
    }
  }, [cluster, displayTitle])

  return (
    <main className="min-h-screen bg-background text-foreground" style={{ overflowX: 'clip' }}>
      {/* Mini-header */}
      <header className="h-14 border-b border-border bg-background/92 backdrop-blur flex items-center gap-3 px-5 sticky top-0 z-10">
        <button
          type="button"
          aria-label="返回"
          onClick={goBackOrClose}
          className="w-9 h-9 rounded-[4px] flex items-center justify-center text-warm-700 hover:bg-warm-100 transition-colors"
        >
          <ArrowLeft className="w-4 h-4" />
        </button>
        <BrandWordmark
          aria-hidden="true"
          className="shrink-0 text-[25px] leading-none text-foreground"
        />
        <div className="flex min-w-0 flex-1 items-center gap-2">
          {/* v24.0 §21.5: 页面唯一 h1 移交内页标题区,mini-header 降级为普通文本 */}
          <p
            data-testid="cluster-full-topbar-title"
            className="min-w-0 truncate font-event-title text-[18px] font-semibold leading-[1.25] tracking-[0] text-foreground"
          >
            {cluster ? displayTitle : '加载中…'}
          </p>
          {singleSource?.url && (
            <a
              href={singleSource.url}
              target="_blank"
              rel="noopener noreferrer"
              aria-label={`打开原文: ${singleSource.title}`}
              title="打开原文"
              className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-[4px] text-[16px] leading-none text-warm-600 transition-colors hover:bg-warm-100 hover:text-warm-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]"
              data-testid="cluster-topbar-original-link"
            >
              <ExternalLink className="h-4 w-4" aria-hidden="true" />
            </a>
          )}
        </div>
      </header>

      {/* 正文容器 */}
      <div className="max-w-[1180px] mx-auto px-4 py-5 lg:px-6 lg:py-6">
        {/* v24.0 §21.5-⑦: 两块 384px 灰 pulse → 与真实布局同形骨架(kicker/标题/双线 + 来源行 | 右栏速览) */}
        {modalState === 'loading' && (
          <div
            className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1.18fr)_minmax(0,0.82fr)]"
            data-testid="cluster-full-loading"
            aria-label="加载中"
          >
            <div className="min-w-0 lg:pr-6">
              <div className="h-3 w-44 animate-skeleton rounded bg-muted" />
              <div className="mt-3 h-8 w-11/12 animate-skeleton rounded bg-muted" />
              <div className="mt-2 h-8 w-2/3 animate-skeleton rounded bg-muted" />
              <div
                aria-hidden="true"
                className="mt-4 h-[6px] border-b border-t-2 border-b-border border-t-foreground"
              />
              {[0, 1, 2].map((i) => (
                <div key={i} className="border-b border-border/70 py-4">
                  <div className="h-4 w-3/4 animate-skeleton rounded bg-muted" />
                  <div className="mt-2.5 h-3 w-40 animate-skeleton rounded bg-muted" />
                  <div className="mt-3 space-y-2">
                    <div className="h-3.5 w-full animate-skeleton rounded bg-muted" />
                    <div className="h-3.5 w-5/6 animate-skeleton rounded bg-muted" />
                  </div>
                </div>
              ))}
            </div>
            <div className="min-w-0 lg:border-l lg:border-dashed lg:border-border lg:pl-6">
              <div className="h-4 w-20 animate-skeleton rounded bg-muted" />
              <div className="mt-4 space-y-2.5">
                <div className="h-4 w-full animate-skeleton rounded bg-muted" />
                <div className="h-4 w-11/12 animate-skeleton rounded bg-muted" />
                <div className="h-4 w-4/5 animate-skeleton rounded bg-muted" />
                <div className="h-4 w-3/5 animate-skeleton rounded bg-muted" />
              </div>
              <div className="mt-8 h-24 animate-skeleton rounded-[4px] bg-muted" />
            </div>
          </div>
        )}

        {modalState === 'redirecting' && (
          <div className="py-24 text-center text-sm text-muted-foreground">
            正在跳转到合并后的事件…
          </div>
        )}

        {modalState === 'error' && (
          <div className="py-24 flex flex-col items-center gap-4 text-center">
            <p className="text-base text-foreground">加载失败</p>
            <p className="text-sm text-muted-foreground">{error}</p>
            <button
              type="button"
              onClick={() => loadFullPage(clusterId)}
              className="rounded-[4px] bg-[var(--brand)] px-4 py-2 text-sm font-medium text-[var(--brand-foreground)] transition-opacity hover:opacity-90 focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              重试
            </button>
          </div>
        )}

        {modalState === 'open' && cluster && (
          <>
            <div
              className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1.18fr)_minmax(0,0.82fr)] lg:h-[calc(100dvh-104px)] lg:min-h-[420px] lg:items-stretch lg:overflow-hidden"
              data-testid="cluster-full-grid"
            >
              <section
                className="event-detail-scrollbar min-w-0 lg:min-h-0 lg:overflow-y-auto lg:pr-6"
                data-testid="cluster-source-scroll"
              >
                {/* v24.0 §21.5 内页标题区: kicker(mono 12px) + 28px/1.3 衬线标题 + Scotch rule
                    (复用批次① event-scotch-rule 同款双线),随左栏滚动 */}
                <header className="mb-2" data-testid="cluster-full-title-block">
                  <p
                    className="font-mono text-[12px] leading-none text-muted-foreground"
                    data-testid="cluster-full-kicker"
                  >
                    {categoryLabel && (
                      <span className="font-medium text-[var(--brand)]">
                        {categoryLabel} <span aria-hidden="true">✦</span>
                        {' · '}
                      </span>
                    )}
                    {sourceCount} 个来源 · {cluster.doc_count} 条报道
                    {kickerTime && ` · ${kickerTime}`}
                  </p>
                  <h1 className="mt-2.5 font-event-title text-[24px] font-bold leading-[1.3] tracking-[0] text-foreground sm:text-[28px]">
                    {displayTitle}
                  </h1>
                  <div
                    data-testid="cluster-full-scotch-rule"
                    aria-hidden="true"
                    className="mt-3.5 h-[6px] border-b border-t-2 border-b-border border-t-foreground"
                  />
                </header>
                <ClusterLeftPanel sources={sortedSources} />
              </section>
              <section
                className="event-detail-scrollbar min-w-0 lg:min-h-0 lg:overflow-y-auto lg:border-l lg:border-dashed lg:border-border lg:pl-6"
                data-testid="cluster-summary-scroll"
              >
                <ClusterRightPanel
                  cluster={cluster}
                  sources={sources}
                  actions={actions}
                  showActions
                  className="h-full"
                />
              </section>
            </div>
          </>
        )}
      </div>
    </main>
  )
}

export default ClusterFullPage
