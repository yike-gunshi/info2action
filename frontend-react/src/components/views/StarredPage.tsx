/**
 * v18.0 nav-merge: 全屏路由 /starred 的 Page 包装
 *
 * PRD §Spec-3 D5: 收藏从顶级 tab 降级为头像下拉 + 全屏路由。
 * 复用既有 StarredView 组件（不重写数据层 / API），仅加 TopBar + 容器骨架。
 *
 * Why 不引 react-router 而走 hash-based:
 * - 当前项目无 router 依赖；引入是 v18 范围外的工程债
 * - 沿用 settings/admin/privacy/terms 已有 hash 全屏路由模式
 * - hash 路由与现有 useHash 流程对接成本最低
 */
import { TopBar } from '../layout/TopBar'
import { StarredView } from './StarredView'
import { ClusterDetailPanel } from '../cluster/ClusterDetailPanel'

export function StarredPage() {
  return (
    <div className="min-h-screen bg-background" style={{ overflowX: 'clip' }}>
      <TopBar activeL1={null} />
      <main className="max-w-[1200px] mx-auto">
        <StarredView />
      </main>
      <ClusterDetailPanel />
    </div>
  )
}
