import type { MouseEvent, ReactNode } from 'react'
import { useUIStore } from '../../store/uiStore'
import { BrandWordmark } from './BrandWordmark'

/**
 * 认证页统一外壳(登录页"安静门"设计):顶部品牌条 + 340px 窄卡片 +
 * 卡内居中 wordmark。Register / VerifyEmail / ForgotPassword 复用,
 * 与 LoginPage 视觉一致(LoginPage 自身结构未动,保持其测试稳定)。
 */
export function AuthPageShell({
  children,
  testId,
}: {
  children: ReactNode
  testId?: string
}) {
  const setL1 = useUIStore((s) => s.setL1)

  function handleBrandClick(e: MouseEvent<HTMLAnchorElement>) {
    e.preventDefault()
    setL1('highlights')
    window.location.hash = ''
  }

  return (
    <div className="min-h-screen bg-background">
      <header className="fixed inset-x-0 top-0 z-30 border-b border-border/80 bg-background/95 backdrop-blur-[2px]">
        <div className="mx-auto flex h-14 max-w-[1440px] items-center px-4 sm:px-5">
          <a
            href="#"
            onClick={handleBrandClick}
            className="rounded-[2px] text-[26px] transition-[filter] hover:brightness-95 focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            aria-label="返回精选"
          >
            <BrandWordmark />
          </a>
        </div>
      </header>

      <div className="flex min-h-screen items-center justify-center px-5 pb-8 pt-20">
        <div
          className="w-full max-w-[340px] rounded-[6px] border border-border bg-card px-5 py-6 shadow-none sm:px-6 sm:py-7"
          data-testid={testId}
        >
          <div className="mb-7 text-center">
            <BrandWordmark className="text-[36px]" />
          </div>
          {children}
        </div>
      </div>
    </div>
  )
}
