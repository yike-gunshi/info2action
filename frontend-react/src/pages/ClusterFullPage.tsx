/**
 * v15.0 ClusterFullPage — cluster 落地页 /#cluster=:id (DESIGN.md §15.10)
 *
 * 双栏 grid `1fr minmax(auto, 400px)` @ 1024px+，移动端 <1024px 单列堆叠（左在上）。
 * 复用事件详情双栏心智 + mini-header 后退。
 *
 * merged_into redirect: store.modalState='redirecting' 时 history.replaceState 到新 id。
 */
import { useEffect, useMemo } from 'react'
import { ArrowLeft, ExternalLink } from 'lucide-react'
import { useClusterDetailStore } from '../store/clusterDetailStore'
import { ClusterLeftPanel } from '../components/cluster/ClusterLeftPanel'
import { ClusterRightPanel } from '../components/cluster/ClusterRightPanel'
import { BrandWordmark } from '../components/shared/BrandWordmark'

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
          <h1
            data-testid="cluster-full-topbar-title"
            className="min-w-0 truncate font-event-title text-[18px] font-semibold leading-[1.25] tracking-[0] text-foreground"
          >
            {cluster ? displayTitle : '加载中…'}
          </h1>
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
        {modalState === 'loading' && (
          <div className="grid lg:grid-cols-[minmax(0,1.18fr)_minmax(0,0.82fr)] gap-5" data-testid="cluster-full-loading">
            <div className="h-96 rounded-[8px] bg-muted/55 animate-pulse" />
            <div className="h-96 rounded-[8px] bg-muted/35 animate-pulse" />
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
              className="px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm"
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
