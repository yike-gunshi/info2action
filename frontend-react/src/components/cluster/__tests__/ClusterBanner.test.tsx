import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ClusterBanner } from '../ClusterBanner'

const openModal = vi.fn()
vi.mock('../../../store/clusterDetailStore', () => ({
  useClusterDetailStore: (selector: (s: { openModal: typeof openModal }) => unknown) =>
    selector({ openModal }),
}))

describe('ClusterBanner', () => {
  afterEach(() => {
    cleanup()
    openModal.mockClear()
  })

  it('clusterId=null 时整组件不渲染（feedback_dont_render_empty_placeholder）', () => {
    const { container } = render(<ClusterBanner clusterId={null} clusterTitle={null} />)
    expect(container.firstChild).toBeNull()
  })

  it('clusterTitle 为空字符串时不渲染', () => {
    const { container } = render(<ClusterBanner clusterId={42} clusterTitle="" />)
    expect(container.firstChild).toBeNull()
  })

  it('文案精确为 【{ai_title}】（中文方括号包裹，无前缀）', () => {
    render(<ClusterBanner clusterId={42} clusterTitle="OpenAI 发布新模型路线更新" />)
    const banner = screen.getByTestId('cluster-banner')
    // 不带 “相关事件:”、“已收录:”、“属于:” 等前缀
    expect(banner.textContent).toMatch(/【OpenAI 发布新模型路线更新】/)
    expect(banner.textContent).not.toMatch(/相关事件/)
    expect(banner.textContent).not.toMatch(/已收录/)
    expect(banner.textContent).not.toMatch(/属于/)
  })

  it('显示 “查看聚合视图” 文字按钮', () => {
    render(<ClusterBanner clusterId={42} clusterTitle="X" />)
    expect(screen.getByText('查看聚合视图')).toBeInTheDocument()
  })

  it('点击触发 openModal(clusterId) — 当前页打开弹窗,不跳路由', async () => {
    const user = userEvent.setup()
    const originalHash = window.location.hash
    render(<ClusterBanner clusterId={99} clusterTitle="hello" />)
    const banner = screen.getByTestId('cluster-banner')
    await user.click(banner)
    expect(openModal).toHaveBeenCalledWith(99)
    // URL 保持不变（R5.3 边界）
    expect(window.location.hash).toBe(originalHash)
  })

  it('background 用 --accent 浅紫底（不用渐变）', () => {
    render(<ClusterBanner clusterId={1} clusterTitle="X" />)
    const banner = screen.getByTestId('cluster-banner')
    expect(banner.style.background).toBe('var(--accent)')
  })
})
