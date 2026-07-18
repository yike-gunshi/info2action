import { useCallback, useEffect, useRef, useState } from 'react'
import { Search, Sun, Moon, Settings, Shield, LogOut, LogIn, Star, Clock, CircleUserRound, X } from 'lucide-react'
import { cn } from '../../lib/utils'
import { useUIStore } from '../../store/uiStore'
import { useFeedStore } from '../../store/feedStore'
import { useEventsStore } from '../../store/eventsStore'
import { useAuthStore } from '../../store/authStore'
import { useDetailStore } from '../../store/detailStore'
import { useClusterDetailStore } from '../../store/clusterDetailStore'
import { useTheme } from '../../hooks/useTheme'
import { useHash } from '../../hooks/useHash'
import { authLogout } from '../../lib/api'
import { resetClientSessionState } from '../../store/sessionReset'
import { SubmitPanel } from './SubmitPanel'
import { BrandWordmark } from '../shared/BrandWordmark'
import { PlatformBrandIcon } from '../shared/PlatformIcon'
import type { L1View } from '../../lib/types'

// oss-release v20.0 F1: 开源仓库外链（编译期常量，不走运行时配置）
const GITHUB_REPO_URL = 'https://github.com/yike-gunshi/info2action'

// v18.0 nav-merge §Spec-1: 6 tab → 3 tab；删 requiresAuth 标记（D2 锁定 3 tab 全可见）
const L1_TABS: { key: L1View; label: string }[] = [
  { key: 'highlights', label: '精选' },
  { key: 'info', label: '信息' },
  { key: 'actions', label: '行动' },
]

// G1: 记忆每个 L1 tab 的文档滚动位置。Dashboard 用 display:none 保留 mount,
// 滚动挂在 window;切走前存、切入后恢复,避免精选↔信息往返每次都回顶部。
// 模块级 Map 跨组件重渲染存活;同 tab 再点仍回顶部(常见"点当前 tab 置顶"手势)。
const l1ScrollPositions = new Map<L1View, number>()

export interface TopBarProps {
  /**
   * Utility pages such as #starred / #history reuse TopBar but should not make
   * any dashboard L1 tab look selected.
   */
  activeL1?: L1View | null
}

export function TopBar({ activeL1 }: TopBarProps = {}) {
  const l1 = useUIStore((s) => s.l1)
  const highlightedL1 = activeL1 === undefined ? l1 : activeL1
  const setL1 = useUIStore((s) => s.setL1)
  const searchQuery = useUIStore((s) => s.searchQuery)
  const setSearchQuery = useUIStore((s) => s.setSearchQuery)
  const { mode, toggle } = useTheme()
  const ThemeIcon = mode === 'dark' ? Moon : Sun
  const { updateHash } = useHash()

  const [localSearch, setLocalSearch] = useState(searchQuery)
  const [searchExpanded, setSearchExpanded] = useState(Boolean(searchQuery.trim()))
  const searchInputRef = useRef<HTMLInputElement>(null)

  const setExpandedKey = useUIStore((s) => s.setExpandedKey)
  const closeModal = useDetailStore((s) => s.closeModal)

  const handleL1Change = (key: L1View) => {
    // G1: 切走前记录当前 tab 滚动位置(切到不同 tab 时);同 tab 再点则回顶部
    if (key !== l1) l1ScrollPositions.set(l1, window.scrollY)
    // BF-0419-6: 切 tab 必须主动关 detail modal + 清 hash 里的 d=,
    // 否则 hashchange 会再次 openItem 把 modal 撑回来
    closeModal()
    setL1(key)
    setExpandedKey(null)
    updateHash({ v: key, s: null, d: null })
    // G1: 切入后恢复目标 tab 上次滚动位置(rAF 等 display:none→block 布局就绪);
    // 同 tab 再点回顶部。内容变短时 scrollTo 会自动 clamp,无害。
    const target = key === l1 ? 0 : (l1ScrollPositions.get(key) ?? 0)
    requestAnimationFrame(() => window.scrollTo({ top: target }))
  }

  const serverSearch = useFeedStore((s) => s.serverSearch)
  const clearSearch = useFeedStore((s) => s.clearSearch)
  // v15: 推荐 tab 同时搜 cluster 区
  const searchClusters = useEventsStore((s) => s.searchClusters)
  const clearClusterSearch = useEventsStore((s) => s.clearSearch)

  useEffect(() => {
    setLocalSearch(searchQuery)
    if (searchQuery.trim()) setSearchExpanded(true)
  }, [searchQuery])

  useEffect(() => {
    if (searchExpanded) searchInputRef.current?.focus()
  }, [searchExpanded])

  const handleSearch = useCallback((query: string) => {
    setLocalSearch(query)
    setSearchQuery(query)
    if (query.trim()) {
      serverSearch(query)
      // v17.0: 精选 tab 触发 cluster 搜索（保留 pill 筛选）
      // v18.0 nav-merge: recommend tab 已删，不再触发 cluster search
      if (l1 === 'highlights') {
        searchClusters(query)
      }
    } else {
      clearSearch()
      clearClusterSearch()
    }
  }, [setSearchQuery, serverSearch, clearSearch, searchClusters, clearClusterSearch, l1])

  const handleClearSearch = () => {
    handleSearch('')
    setSearchExpanded(false)
  }

  const iconButtonClass = 'inline-flex h-9 w-9 items-center justify-center rounded-[4px] text-muted-foreground transition-colors hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background'

  return (
    <header
      className="sticky top-0 z-[550] border-b border-border bg-background"
      data-testid="topbar"
      data-v19-module="topbar"
    >
      <div
        className="mx-auto grid min-h-[84px] max-w-[1440px] grid-cols-[1fr_auto] grid-rows-[48px_36px] items-center gap-x-3 px-4 sm:h-[52px] sm:min-h-0 sm:grid-cols-[minmax(150px,1fr)_auto_minmax(150px,1fr)] sm:grid-rows-1 sm:gap-3 sm:px-5"
        data-testid="topbar-grid"
      >
        {/* Brand */}
        <button
          type="button"
          onClick={() => handleL1Change('highlights')}
          className="col-start-1 row-start-1 justify-self-start rounded-[2px] font-brand text-[26px] font-[700] leading-none tracking-normal text-foreground transition-[filter] hover:brightness-95 focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background sm:col-start-auto sm:row-start-auto sm:text-[28px]"
          data-testid="topbar-logo"
        >
          <span className="sr-only">返回精选</span>
          <BrandWordmark aria-hidden="true" className="pointer-events-none" />
        </button>

        {/* L1 Tabs */}
        <nav
          className="col-span-2 row-start-2 flex h-full w-full items-center justify-center gap-8 justify-self-stretch border-t border-border/80 sm:col-span-1 sm:row-start-auto sm:h-auto sm:w-auto sm:justify-self-center sm:gap-1 sm:border-t-0"
          aria-label="主导航"
          data-testid="topbar-nav"
        >
          {L1_TABS.map((tab) => (
            <button
              key={tab.key}
              type="button"
              onClick={() => handleL1Change(tab.key)}
              aria-current={highlightedL1 === tab.key ? 'page' : undefined}
              data-testid={`topbar-tab-${tab.key}`}
              className={cn(
                'relative h-full px-2 py-1.5 font-event-title text-[16px] font-medium tracking-normal transition-colors sm:h-auto sm:px-4 sm:py-2',
                'focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background',
                highlightedL1 === tab.key
                  ? 'text-[var(--brand)] after:absolute after:inset-x-2 after:bottom-0 after:h-[2px] after:rounded-full after:bg-[var(--brand)]'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {tab.label}
            </button>
          ))}
        </nav>

        <div className="col-start-2 row-start-1 flex items-center justify-self-end gap-2 sm:col-start-auto sm:row-start-auto">
          {/* v24.0 §21.6: <640px 此前无任何搜索入口(功能缺口) — 补 icon 按钮,
              点击后在 TopBar 下方展开输入行(见 header 尾部),复用同一套搜索状态/逻辑 */}
          {!searchExpanded && (
            <button
              type="button"
              onClick={() => setSearchExpanded(true)}
              className={cn(iconButtonClass, 'sm:hidden')}
              aria-label="搜索"
              title="搜索"
              data-testid="topbar-search-mobile-trigger"
            >
              <Search className="h-[19px] w-[19px]" strokeWidth={1.6} />
            </button>
          )}

          {/* Search */}
          <div
            className={cn(
              'hidden h-9 shrink-0 items-center overflow-hidden transition-[width,opacity] duration-300 ease-out sm:flex',
              searchExpanded ? 'w-[180px] md:w-[220px] lg:w-[260px]' : 'w-9',
            )}
            data-testid="topbar-search"
          >
            {searchExpanded ? (
              <div className="flex h-full w-full items-center border-b-2 border-[var(--brand-border)] text-foreground transition-colors focus-within:border-[var(--brand)]">
                <Search className="h-[18px] w-[18px] shrink-0 text-muted-foreground" strokeWidth={1.6} />
                <input
                  ref={searchInputRef}
                  type="text"
                  aria-label="搜索信息"
                  value={localSearch}
                  onChange={(e) => handleSearch(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Escape' && !localSearch.trim()) setSearchExpanded(false)
                  }}
                  placeholder="搜索..."
                  className="min-w-0 flex-1 bg-transparent px-3 font-event-title text-[16px] text-foreground outline-none placeholder:font-body-cjk placeholder:text-sm placeholder:text-muted-foreground"
                />
                <button
                  type="button"
                  onClick={handleClearSearch}
                  className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-[4px] text-muted-foreground transition-colors hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]"
                  aria-label="清除搜索"
                >
                  <X className="h-[17px] w-[17px]" strokeWidth={1.65} />
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setSearchExpanded(true)}
                className={iconButtonClass}
                aria-label="搜索"
                title="搜索"
              >
                <Search className="h-[19px] w-[19px]" strokeWidth={1.6} />
              </button>
            )}
          </div>

          <div className="flex items-center gap-1.5">
            <SubmitPanel />
            <button
              type="button"
              onClick={toggle}
              className={iconButtonClass}
              aria-label="切换主题"
              title="切换主题"
              data-testid="topbar-theme-toggle"
            >
              <ThemeIcon className="h-[19px] w-[19px]" strokeWidth={1.6} />
            </button>
            {/* oss-release v20.0 F1: 面向未登录游客的入口，不能收进登录后头像菜单；
                <sm 时收进 UserMenu（menu-github），桌面平铺在工具组尾部紧邻分隔线 */}
            <a
              href={GITHUB_REPO_URL}
              target="_blank"
              rel="noopener noreferrer"
              className={cn(iconButtonClass, 'hidden sm:inline-flex')}
              aria-label="GitHub 开源仓库"
              title="GitHub 开源仓库"
              data-testid="topbar-github"
            >
              <PlatformBrandIcon platform="github" className="h-[19px] w-[19px]" />
            </a>
          </div>

          <span className="hidden h-5 w-px bg-border sm:block" aria-hidden="true" />

          <UserMenu />
        </div>
      </div>

      {/* v24.0 §21.6: <640px 搜索展开行 — 与桌面同款下划线输入,只做入口不碰搜索语义 */}
      {searchExpanded && (
        <div className="border-t border-border/80 px-4 py-2 sm:hidden" data-testid="topbar-search-mobile">
          <div className="flex h-9 w-full items-center border-b-2 border-[var(--brand-border)] text-foreground transition-colors focus-within:border-[var(--brand)]">
            <Search className="h-[18px] w-[18px] shrink-0 text-muted-foreground" strokeWidth={1.6} />
            <input
              type="text"
              aria-label="搜索信息"
              value={localSearch}
              autoFocus
              onChange={(e) => handleSearch(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape' && !localSearch.trim()) setSearchExpanded(false)
              }}
              placeholder="搜索..."
              className="min-w-0 flex-1 bg-transparent px-3 font-event-title text-[16px] text-foreground outline-none placeholder:font-body-cjk placeholder:text-sm placeholder:text-muted-foreground"
              data-testid="topbar-search-mobile-input"
            />
            <button
              type="button"
              onClick={handleClearSearch}
              className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-[4px] text-muted-foreground transition-colors hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)]"
              aria-label="清除搜索"
            >
              <X className="h-[17px] w-[17px]" strokeWidth={1.65} />
            </button>
          </div>
        </div>
      )}
    </header>
  )
}

/**
 * v18.0 nav-merge §Spec-3 + §Spec-5: UserAvatarMenu
 *
 * 已登录态：头像 popover 含 收藏 / 历史 / 设置 / 管理 / 退出
 * 未登录态：仅显示「登录」按钮，不弹 popover（Spec-3.5）
 *
 * z-index 互斥（Spec-5.3）: 卡片 modal / cluster modal / hash item 弹窗
 *   打开期间，popover 自动关闭并禁用打开（避免 PRD §Spec-5 累犯
 *   memory `feedback_modal_backdrop_occlusion`）。互斥靠 React state，
 *   不靠 z-index 覆盖。
 */
function UserMenu() {
  const user = useAuthStore((s) => s.user)
  const setUser = useAuthStore((s) => s.setUser)
  const [open, setOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  // v18.0 §Spec-5.1/5.3: 监听任何 modal 打开，强制关闭并禁止打开 popover
  const detailModalOpen = useDetailStore((s) => s.modalStack.length > 0)
  // useClusterDetailStore.modalState ∈ 'closed' | 'loading' | 'open' | 'error'
  // 用动态 import 避免循环依赖；模式按 detailStore 同样订阅
  const clusterModalOpen = useClusterModalOpen()
  const anyModalOpen = detailModalOpen || clusterModalOpen

  useEffect(() => {
    if (anyModalOpen && open) setOpen(false)
  }, [anyModalOpen, open])

  // Close on click outside
  useEffect(() => {
    if (!open) return
    function handle(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handle)
    return () => document.removeEventListener('mousedown', handle)
  }, [open])

  // v18.0 §Spec-3.5: 未登录态不弹 popover，仅显示登录入口
  if (!user) {
    return (
      <a
        href="#login"
        className="inline-flex h-9 w-9 items-center justify-center gap-1.5 rounded-[4px] px-0 font-event-title text-[16px] font-normal leading-none text-[var(--brand)] transition-colors hover:text-[color-mix(in_srgb,var(--brand)_82%,#171512)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background sm:w-auto sm:min-w-[56px] sm:px-2"
        aria-label="登录"
        data-testid="topbar-login"
      >
        <CircleUserRound className="h-5 w-5 shrink-0 sm:hidden" />
        <LogIn className="hidden h-4 w-4 shrink-0 sm:block" />
        <span className="hidden h-4 items-center leading-none sm:inline-flex" aria-hidden="true">登录</span>
      </a>
    )
  }

  const initial = (user.username || user.email || '?')[0].toUpperCase()

  async function handleLogout() {
    try {
      await authLogout()
    } catch { /* local cleanup still protects same-browser account switches */ }
    resetClientSessionState()
    setUser(null)
    window.location.hash = 'login'
  }

  function handleToggle() {
    // v18.0 §Spec-5.3: modal 打开期间禁用 popover
    if (anyModalOpen) return
    setOpen((prev) => !prev)
  }

  return (
    <div ref={menuRef} className="relative">
      <button
        onClick={handleToggle}
        className={cn(
          'flex h-9 w-9 items-center justify-center rounded-full transition-colors hover:text-[var(--brand)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background',
          anyModalOpen && 'opacity-50 cursor-not-allowed',
        )}
        aria-label="用户菜单"
        aria-expanded={open}
        disabled={anyModalOpen}
        data-testid="topbar-user-trigger"
      >
        <div className="flex h-8 w-8 items-center justify-center rounded-full bg-[var(--brand-soft)] text-xs font-semibold text-[var(--brand)]">
          {initial}
        </div>
      </button>

      {open && !anyModalOpen && (
        // v18.0 §Spec-5: z-index 800（modal 是 1000，toast 是 1100）
        <div
          className="absolute right-0 top-full mt-2 w-[240px] rounded-[8px] border border-border bg-card py-1 shadow-medium"
          style={{ zIndex: 800 }}
          role="menu"
        >
          {/* User info */}
          <div className="px-4 py-2.5 border-b border-border">
            <p className="text-[13px] font-medium text-foreground truncate">{user.username}</p>
            <p className="text-[11px] text-muted-foreground truncate">{user.email}</p>
          </div>

          {/* v18.0 §Spec-3.2: 收藏 → 全屏路由 /starred (#starred) */}
          <a
            href="#starred"
            onClick={() => setOpen(false)}
            className="flex items-center gap-2.5 px-4 py-2.5 text-[13px] font-medium text-foreground hover:bg-background transition-colors"
            role="menuitem"
          >
            <Star className="w-4 h-4 text-muted-foreground" />
            我的收藏
          </a>

          {/* v18.0 §Spec-3.3: 历史 → 全屏路由 /history (#history) */}
          <a
            href="#history"
            onClick={() => setOpen(false)}
            className="flex items-center gap-2.5 px-4 py-2.5 text-[13px] font-medium text-foreground hover:bg-background transition-colors"
            role="menuitem"
          >
            <Clock className="w-4 h-4 text-muted-foreground" />
            浏览历史
          </a>

          {/* oss-release v20.0 F1: <sm 专用（桌面已有 topbar-github 平铺按钮） */}
          <a
            href={GITHUB_REPO_URL}
            target="_blank"
            rel="noopener noreferrer"
            onClick={() => setOpen(false)}
            className="sm:hidden flex items-center gap-2.5 px-4 py-2.5 text-[13px] font-medium text-foreground hover:bg-background transition-colors"
            role="menuitem"
            data-testid="menu-github"
          >
            <PlatformBrandIcon platform="github" className="w-4 h-4 text-muted-foreground" />
            GitHub 开源仓库
          </a>

          {/* Menu items */}
          <a
            href="#settings"
            onClick={() => setOpen(false)}
            className="flex items-center gap-2.5 px-4 py-2.5 text-[13px] font-medium text-foreground hover:bg-background transition-colors"
            role="menuitem"
          >
            <Settings className="w-4 h-4 text-muted-foreground" />
            设置
          </a>

          {user.role === 'admin' && (
            <a
              href="#admin"
              onClick={() => setOpen(false)}
              className="flex items-center gap-2.5 px-4 py-2.5 text-[13px] font-medium text-foreground hover:bg-background transition-colors"
              role="menuitem"
            >
              <Shield className="w-4 h-4 text-muted-foreground" />
              管理
            </a>
          )}

          <button
            onClick={handleLogout}
            className="w-full flex items-center gap-2.5 px-4 py-2.5 text-[13px] font-medium text-destructive hover:bg-background transition-colors"
            role="menuitem"
          >
            <LogOut className="w-4 h-4" />
            退出登录
          </button>
        </div>
      )}
    </div>
  )
}

/** v18.0 §Spec-5.2: 监听 cluster modal 状态（来自 clusterDetailStore） */
function useClusterModalOpen(): boolean {
  const modalState = useClusterDetailStore((s) => s.modalState)
  return modalState === 'open' || modalState === 'loading'
}
