import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { Toaster } from 'sonner'
import { useUIStore } from './store/uiStore'
import { useFeedStore } from './store/feedStore'
import { useDetailStore } from './store/detailStore'
import { useAuthStore } from './store/authStore'
import { useHash, mapLegacyL1 } from './hooks/useHash'
import { useTheme } from './hooks/useTheme'
import { fetchClassification } from './lib/api'
import { authMe } from './lib/api'
import type { L1View } from './lib/types'

import { TopBar } from './components/layout/TopBar'
import { L2Pills } from './components/layout/L2Pills'
import { HighlightsView } from './components/highlights/HighlightsView'
import { ClusterDetailPanel } from './components/cluster/ClusterDetailPanel'
import { DetailPanel } from './components/detail/DetailPanel'
import { ActionsView } from './components/actions/ActionsView'
import { InfoView } from './components/info/InfoView'
import { InfoImage2LabPage } from './components/info/InfoImage2LabPage'
import { InfoLegacyLabPage } from './components/info/InfoLegacyLabPage'
import { StarredPage } from './components/views/StarredPage'
import { HistoryPage } from './components/views/HistoryPage'
import { LoginPage } from './pages/LoginPage'
import { RegisterPage } from './pages/RegisterPage'
import { VerifyEmailPage } from './pages/VerifyEmailPage'
import { SettingsPage } from './pages/SettingsPage'
import { AdminPage } from './pages/AdminPage'
import { OnboardingPage } from './pages/OnboardingPage'
import { ForgotPasswordPage } from './pages/ForgotPasswordPage'
import { ResetPasswordPage } from './pages/ResetPasswordPage'
import { PrivacyPage } from './pages/PrivacyPage'
import { TermsPage } from './pages/TermsPage'
import { ClusterFullPage } from './pages/ClusterFullPage'

type AuthView = 'login' | 'register' | 'verify-email' | 'forgot-password' | 'reset-password'
// v18.0 nav-merge: starred / history 从 L1 tab 升级为全屏路由（Spec-3 D5）
type AppView = 'settings' | 'admin' | 'privacy' | 'terms' | 'starred' | 'history' | 'info-image2-lab' | 'info-legacy-lab'

// v18.0 nav-merge: collectInitialDetailPrefetchIds + scheduleInitialDetailPrefetch
// 已废弃 — 仅旧推荐 tab loadRecommendSections 使用，3 tab 模式后无 caller。
// 信息 tab 不做提前 detail prefetch（依赖卡片点击触发）。

function AppFrame({ children }: { children: ReactNode }) {
  return (
    <>
      {children}
      <DetailModalHost />
      <Toaster
        position="top-center"
        style={{
          '--width': 'min(calc(100vw - 32px), 560px)',
        } as CSSProperties}
        toastOptions={{
          className: 'text-sm whitespace-nowrap',
          style: {
            left: '50%',
            translate: '-50% 0',
            width: 'fit-content',
            minWidth: 'fit-content',
            maxWidth: 'min(calc(100vw - 32px), 560px)',
            paddingLeft: 18,
            paddingRight: 18,
          },
          duration: 3000,
        }}
      />
    </>
  )
}

function DetailModalHost() {
  const modalStack = useDetailStore((s) => s.modalStack)
  return modalStack.length > 0 ? <DetailPanel /> : null
}

function getAuthView(): AuthView | null {
  const hash = window.location.hash.slice(1).split('?')[0]
  if (hash === 'login') return 'login'
  if (hash === 'register') return 'register'
  if (hash === 'verify-email') return 'verify-email'
  if (hash === 'forgot-password') return 'forgot-password'
  if (hash.startsWith('reset-password')) return 'reset-password'
  return null
}

function getAppView(): AppView | null {
  const hash = window.location.hash.slice(1)
  const params = new URLSearchParams(hash)
  if (params.get('v') === 'info-image2-lab') return 'info-image2-lab'
  if (params.get('v') === 'info-legacy-lab') return 'info-legacy-lab'
  if (hash === 'settings') return 'settings'
  if (hash === 'admin') return 'admin'
  if (hash === 'privacy') return 'privacy'
  if (hash === 'terms') return 'terms'
  // v18.0 nav-merge §Spec-3 D5: 全屏路由
  if (hash === 'starred') return 'starred'
  if (hash === 'history') return 'history'
  return null
}

// v15.0: #cluster=NN → cluster 落地页
function getClusterView(): number | null {
  const hash = window.location.hash.slice(1)
  if (!hash.startsWith('cluster=')) return null
  const raw = hash.slice('cluster='.length).trim()
  const id = parseInt(raw, 10)
  return Number.isFinite(id) && id > 0 ? id : null
}

function getInitialDashboardView(): L1View {
  // v18.0 nav-merge: 通过 mapLegacyL1 把老 view (recommend/channels) 映射为 info；
  // 三件套外（含 starred/history）由 getAppView 处理 / fallback highlights。
  const params = new URLSearchParams(window.location.hash.slice(1))
  const raw = params.get('v')
  return mapLegacyL1(raw)
}

export default function App() {
  const user = useAuthStore((s) => s.user)
  const isLoading = useAuthStore((s) => s.isLoading)
  const isChecked = useAuthStore((s) => s.isChecked)
  const setUser = useAuthStore((s) => s.setUser)
  const setAuthLoading = useAuthStore((s) => s.setLoading)
  const setChecked = useAuthStore((s) => s.setChecked)

  const [authView, setAuthView] = useState<AuthView | null>(getAuthView)
  const [appView, setAppView] = useState<AppView | null>(getAppView)
  const [clusterView, setClusterView] = useState<number | null>(getClusterView)

  useTheme()

  // Check auth on mount
  useEffect(() => {
    authMe()
      .then((u) => setUser(u))
      .catch(() => setUser(null))
      .finally(() => {
        setAuthLoading(false)
        setChecked(true)
      })
  }, [setUser, setAuthLoading, setChecked])

  // Listen for hash changes to detect login/register/settings/admin/item/cluster
  useEffect(() => {
    function onHash() {
      setAuthView(getAuthView())
      setAppView(getAppView())
      setClusterView(getClusterView())
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  // Protected account/admin pages need a definitive auth check; public feed can
  // render while the session probe completes in the background.
  if ((!isChecked || isLoading) && (appView === 'settings' || appView === 'admin')) {
    return (
      <AppFrame>
        <div className="min-h-screen flex items-center justify-center bg-background">
          <div className="w-8 h-8 rounded-lg bg-primary flex items-center justify-center text-primary-foreground font-bold text-sm animate-pulse">
            N
          </div>
        </div>
      </AppFrame>
    )
  }

  // Auth pages (login/register/verify) — always accessible
  if (authView === 'login') {
    return (
      <AppFrame>
        <LoginPage />
      </AppFrame>
    )
  }
  if (authView === 'register') {
    return (
      <AppFrame>
        <RegisterPage />
      </AppFrame>
    )
  }
  if (authView === 'verify-email') {
    return (
      <AppFrame>
        <VerifyEmailPage />
      </AppFrame>
    )
  }
  if (authView === 'forgot-password') {
    return (
      <AppFrame>
        <ForgotPasswordPage />
      </AppFrame>
    )
  }
  if (authView === 'reset-password') {
    return (
      <AppFrame>
        <ResetPasswordPage />
      </AppFrame>
    )
  }

  // App-level pages (require login)
  if (appView === 'settings') {
    if (!user) {
      window.location.hash = 'login'
      return <AppFrame>{null}</AppFrame>
    }
    return (
      <AppFrame>
        <SettingsPage />
      </AppFrame>
    )
  }
  if (appView === 'admin') {
    if (!user || user.role !== 'admin') {
      window.location.hash = 'login'
      return <AppFrame>{null}</AppFrame>
    }
    return (
      <AppFrame>
        <AdminPage />
      </AppFrame>
    )
  }
  if (appView === 'privacy') {
    return (
      <AppFrame>
        <PrivacyPage />
      </AppFrame>
    )
  }
  if (appView === 'terms') {
    return (
      <AppFrame>
        <TermsPage />
      </AppFrame>
    )
  }
  if (appView === 'info-image2-lab') {
    return (
      <AppFrame>
        <InfoImage2LabPage />
      </AppFrame>
    )
  }
  if (appView === 'info-legacy-lab') {
    return (
      <AppFrame>
        <InfoLegacyLabPage />
      </AppFrame>
    )
  }
  // v18.0 nav-merge §Spec-3: 收藏 / 历史降级为全屏路由（D5）
  if (appView === 'starred') {
    if (!user) {
      window.location.hash = 'login'
      return <AppFrame>{null}</AppFrame>
    }
    return (
      <AppFrame>
        <StarredPage />
      </AppFrame>
    )
  }
  if (appView === 'history') {
    if (!user) {
      window.location.hash = 'login'
      return <AppFrame>{null}</AppFrame>
    }
    return (
      <AppFrame>
        <HistoryPage />
      </AppFrame>
    )
  }

  // v15.0: #cluster=NN cluster 落地页(先于 onboarding)
  if (clusterView != null) {
    return (
      <AppFrame>
        <ClusterFullPage clusterId={clusterView} />
      </AppFrame>
    )
  }

  // Onboarding gate: logged-in users who haven't completed onboarding
  if (user && !user.onboarding_completed) {
    return (
      <AppFrame>
        <OnboardingPage
          onComplete={() => {
            // Force re-render by updating user in store
            setUser({ ...user, onboarding_completed: true })
          }}
        />
      </AppFrame>
    )
  }

  // Main dashboard — accessible to everyone (logged in or not)
  return (
    <AppFrame>
      <Dashboard />
    </AppFrame>
  )
}

function Dashboard() {
  const initialDashboardView = useRef<L1View>(getInitialDashboardView())
  const initialViewSynced = useRef(false)
  const l1 = useUIStore((s) => s.l1)
  const setL1 = useUIStore((s) => s.setL1)
  const user = useAuthStore((s) => s.user)
  const setClassification = useFeedStore((s) => s.setClassification)

  // Lazy tab mounting: only render a tab after first visit, then keep mounted
  // v17.0: 默认首页 = highlights；v18.0 nav-merge: 仅 3 tab (highlights/info/actions)
  const skipFirstVisitedEffect = useRef(initialDashboardView.current !== 'highlights')
  const [visited, setVisited] = useState<Set<string>>(() => new Set([initialDashboardView.current]))
  useLayoutEffect(() => {
    if (!initialViewSynced.current && initialDashboardView.current !== l1) {
      setL1(initialDashboardView.current)
    }
    initialViewSynced.current = true
  }, [l1, setL1])
  useEffect(() => {
    if (skipFirstVisitedEffect.current && l1 !== initialDashboardView.current) {
      skipFirstVisitedEffect.current = false
      return
    }
    skipFirstVisitedEffect.current = false
    setVisited((prev) => {
      if (prev.has(l1)) return prev
      const next = new Set(prev)
      next.add(l1)
      return next
    })
  }, [l1])

  useHash()

  const initFetchStatus = useFeedStore((s) => s.initFetchStatus)
  const classificationLoaded = useRef(false)

  // Load light configuration immediately; do not block highlights on the heavy
  // platform sections payload.
  useEffect(() => {
    if (!classificationLoaded.current) {
      classificationLoaded.current = true
      fetchClassification()
        .then(setClassification)
        .catch((err) => {
          console.error('Failed to load classification:', err)
        })
    }
    const fetchStatusTimer = window.setTimeout(() => {
      initFetchStatus()
    }, 20000)
    return () => {
      window.clearTimeout(fetchStatusTimer)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="min-h-screen bg-background" style={{ overflowX: 'clip' }}>
      <TopBar />
      <L2Pills />

      <main className={l1 === 'actions' ? 'mx-auto max-w-none' : 'mx-auto max-w-[1200px]'}>
        <>
          {/* v17.0: 精选 tab — 默认首页，时间线整页 */}
          {/* FE-9(Wave C): 与 info/actions 一致改为 visited 常驻——原先切走即
              卸载,切回全量重建 DOM(全部 EventCard 重挂载+重解析+封面 304 群发) */}
          {visited.has('highlights') && (
            <div style={{ display: l1 === 'highlights' ? 'block' : 'none' }}>
              <HighlightsView />
            </div>
          )}

          {/* v18.0 nav-merge: 信息 tab — 复用 ChannelsView 实现，过滤口径升级到 AI 强制 */}
          {visited.has('info') && (
            <div style={{ display: l1 === 'info' ? 'block' : 'none' }}>
              <InfoView />
            </div>
          )}

          {visited.has('actions') && (
            <div style={{ display: l1 === 'actions' ? 'block' : 'none' }}>
              {user ? <ActionsView /> : <AuthRequiredView label="行动" desc="登录后可将信息转化为行动建议" />}
            </div>
          )}
        </>
      </main>

      {/* v15.0 cluster 弹窗(全局挂载,由 clusterDetailStore.modalState 驱动) */}
      <ClusterDetailPanel />

    </div>
  )
}

function AuthRequiredView({ label, desc }: { label: string; desc: string }) {
  return (
    <div className="px-4 py-24 text-center">
      <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-[4px] border border-border bg-card">
        <svg className="w-8 h-8 text-muted-foreground" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" />
        </svg>
      </div>
      <h3 className="text-lg font-semibold text-foreground mb-1">{label}功能需要登录</h3>
      <p className="text-sm text-muted-foreground mb-6">{desc}</p>
      <a
        href="#login"
        className="inline-flex items-center gap-2 rounded-[4px] bg-[var(--brand)] px-6 py-2.5 text-sm font-medium text-[var(--brand-foreground)] transition-opacity hover:opacity-90"
      >
        去登录
      </a>
    </div>
  )
}
