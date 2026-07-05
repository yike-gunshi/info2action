/**
 * v18.0 nav-merge: 全屏路由 /history 的 Page 包装
 *
 * PRD §Spec-3 D5: 历史从顶级 tab 降级为头像下拉 + 全屏路由。
 * 复用既有 HistoryView 组件，仅加 TopBar + 容器骨架。
 */
import { TopBar } from '../layout/TopBar'
import { HistoryView } from './HistoryView'
import { ClusterDetailPanel } from '../cluster/ClusterDetailPanel'

export function HistoryPage() {
  return (
    <div className="min-h-screen bg-background" style={{ overflowX: 'clip' }}>
      <TopBar activeL1={null} />
      <main className="max-w-[1200px] mx-auto">
        <HistoryView />
      </main>
      <ClusterDetailPanel />
    </div>
  )
}
