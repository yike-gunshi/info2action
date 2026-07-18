import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'

/**
 * 稳定性加固(2026-07-10): 顶层错误边界。
 *
 * 此前 main.tsx 直接渲染 <App/>,没有任何 ErrorBoundary——任意组件在 render 阶段
 * 抛未捕获异常(比如后端返回了非预期结构、某个 .map 作用在 undefined 上),整棵
 * React 树会被卸载,用户看到纯白屏,等同"服务不可用"。这里兜住渲染异常,给一个
 * 可读的降级页 + 重新加载入口,把"整站白屏"降级成"这一次刷新即可恢复"。
 *
 * 注意:错误边界只捕获渲染/生命周期期间的同步异常,不捕获事件回调、异步(fetch)
 * 里的异常——那些各 store 已有自己的 try/catch 降级。
 */
interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 打到 console 便于线上排查(生产 sourcemap 可还原)。
    console.error('[ErrorBoundary] uncaught render error', error, info.componentStack)
  }

  private handleReload = () => {
    // 硬刷新:清掉可能已损坏的内存态,重新拉取。
    window.location.reload()
  }

  render() {
    if (!this.state.hasError) return this.props.children
    return (
      <div
        role="alert"
        style={{
          minHeight: '100vh',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 16,
          padding: 24,
          textAlign: 'center',
          fontFamily: 'system-ui, -apple-system, sans-serif',
          color: 'var(--color-text, #333)',
          background: 'var(--color-bg, #f5f5f5)',
        }}
      >
        <div style={{ fontSize: 44, lineHeight: 1 }}>😵‍💫</div>
        <div style={{ fontSize: 18, fontWeight: 600 }}>页面出错了</div>
        <div style={{ fontSize: 14, opacity: 0.7, maxWidth: 360 }}>
          刚才渲染时遇到了一个意外错误。重新加载通常就能恢复；如果反复出现，请稍后再试。
        </div>
        <button
          type="button"
          onClick={this.handleReload}
          style={{
            marginTop: 4,
            padding: '10px 28px',
            fontSize: 15,
            border: 'none',
            borderRadius: 8,
            background: '#0066cc',
            color: '#fff',
            cursor: 'pointer',
          }}
        >
          重新加载
        </button>
      </div>
    )
  }
}
