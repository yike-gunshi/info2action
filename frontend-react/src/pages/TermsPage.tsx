import { ArrowLeft } from 'lucide-react'

export function TermsPage() {
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

        <h1 className="text-2xl font-bold text-foreground mb-6">使用条款</h1>

        <div className="prose prose-sm text-muted-foreground space-y-4">
          <p className="text-[13px]">最后更新：2026 年 4 月</p>

          <h2 className="text-lg font-semibold text-foreground mt-8 mb-3">1. 服务说明</h2>
          <p className="text-[16px] leading-[1.7]">
            info2act 是一个信息聚合与行动管理工具，帮助用户从多个平台获取、筛选和处理信息。
          </p>

          <h2 className="text-lg font-semibold text-foreground mt-8 mb-3">2. 账户责任</h2>
          <p className="text-[16px] leading-[1.7]">
            你有责任保管好自己的账户凭证。请勿分享邀请码或允许他人使用你的账户。
          </p>

          <h2 className="text-lg font-semibold text-foreground mt-8 mb-3">3. 可接受使用</h2>
          <p className="text-[16px] leading-[1.7]">
            请勿使用本服务进行违法活动、滥用 API、或干扰其他用户的正常使用。
          </p>

          <h2 className="text-lg font-semibold text-foreground mt-8 mb-3">4. 内容来源</h2>
          <p className="text-[16px] leading-[1.7]">
            本服务聚合的内容来自公开平台，版权归原作者所有。我们不对第三方内容的准确性负责。
          </p>

          <h2 className="text-lg font-semibold text-foreground mt-8 mb-3">5. 服务变更</h2>
          <p className="text-[16px] leading-[1.7]">
            我们保留随时修改或终止服务的权利。重大变更将提前通知注册用户。
          </p>

          <h2 className="text-lg font-semibold text-foreground mt-8 mb-3">6. 免责声明</h2>
          <p className="text-[16px] leading-[1.7]">
            本服务按"现状"提供，不提供任何形式的保证。对于因使用本服务造成的损失，我们不承担责任。
          </p>
        </div>
      </div>
    </div>
  )
}
