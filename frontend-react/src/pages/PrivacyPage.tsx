import { ArrowLeft } from 'lucide-react'

export function PrivacyPage() {
  return (
    <div className="min-h-screen bg-background p-5">
      <div className="max-w-[680px] mx-auto py-10">
        <a
          href="#"
          className="text-primary text-[13px] font-medium hover:underline flex items-center gap-1 mb-8"
        >
          <ArrowLeft className="w-3.5 h-3.5" />
          返回首页
        </a>

        <h1 className="text-2xl font-bold text-foreground mb-6">隐私政策</h1>

        <div className="prose prose-sm text-muted-foreground space-y-4">
          <p className="text-[13px]">最后更新：2026 年 4 月</p>

          <h2 className="text-lg font-semibold text-foreground mt-8 mb-3">1. 信息收集</h2>
          <p className="text-[16px] leading-[1.7]">
            我们收集以下信息以提供服务：邮箱地址、用户名、密码（加密存储）、兴趣偏好设置。
          </p>

          <h2 className="text-lg font-semibold text-foreground mt-8 mb-3">2. 信息使用</h2>
          <p className="text-[16px] leading-[1.7]">
            收集的信息仅用于：账户管理、个性化内容推荐、服务通知。我们不会将你的信息出售给第三方。
          </p>

          <h2 className="text-lg font-semibold text-foreground mt-8 mb-3">3. 数据存储</h2>
          <p className="text-[16px] leading-[1.7]">
            你的数据存储在受保护的服务器上。密码使用 bcrypt 加密存储，我们无法查看你的原始密码。
          </p>

          <h2 className="text-lg font-semibold text-foreground mt-8 mb-3">4. Cookie</h2>
          <p className="text-[16px] leading-[1.7]">
            我们使用 HttpOnly Cookie 管理登录状态。不使用追踪 Cookie 或第三方分析工具。
          </p>

          <h2 className="text-lg font-semibold text-foreground mt-8 mb-3">5. 数据删除</h2>
          <p className="text-[16px] leading-[1.7]">
            你可以随时联系我们删除你的账户和所有相关数据。
          </p>

          <h2 className="text-lg font-semibold text-foreground mt-8 mb-3">6. 联系我们</h2>
          <p className="text-[16px] leading-[1.7]">
            如有隐私相关问题，请联系：privacy@openclaw-center.com
          </p>
        </div>
      </div>
    </div>
  )
}
