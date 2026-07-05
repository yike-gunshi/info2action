import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { TranscriptPanel } from '../TranscriptPanel'
import { useDetailStore } from '../../../store/detailStore'
import type { FeedItem } from '../../../lib/types'

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

vi.mock('../../shared/AuthGate', () => ({
  requireAuth: vi.fn(() => true),
}))

function resetAsrState() {
  useDetailStore.setState({
    modalStack: [],
    itemDetail: null,
    detailCache: new Map(),
    asrStatus: 'idle',
    asrRawStatus: null,
    asrText: null,
    asrDurationSec: null,
    asrProgress: null,
    asrError: null,
    asrRetryCount: 0,
    asrSegments: null,
    asrTextCn: null,
    asrSegmentsCn: null,
    asrCnStatus: 'none',
    asrCostYuan: null,
    asrCurrentTimeMs: 0,
    asrAutoFollow: true,
  })
}

describe('TranscriptPanel v20.7 inline border states', () => {
  beforeEach(() => {
    resetAsrState()
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('idle 态是 68px 内嵌边框条,不展示 ETA 或成本', () => {
    render(<TranscriptPanel itemId="item-1" />)

    const panel = screen.getByRole('region', { name: '视频转写' })
    expect(panel).toHaveAttribute('data-asr-status', 'idle')
    expect(panel.className).toContain('min-h-[68px]')
    expect(panel.className).toContain('border-[var(--modal-border)]')
    expect(panel.className).toContain('shadow-none')
    expect(panel.className).not.toContain('h-[360px]')
    expect(screen.getByRole('button', { name: '开始 AI 转写' })).toHaveTextContent('开始转写')
    expect(screen.queryByText(/约 1-3 分钟|估|¥/)).not.toBeInTheDocument()
  })

  it('running 态只显示刷新图标按钮,不使用进度条/耗时/金额', () => {
    useDetailStore.setState({
      asrStatus: 'running',
      asrRawStatus: 'running',
      asrProgress: {
        phase: 'asr_submit',
        message: '后端处理中',
        percent: 42,
        startedAt: Date.now() - 10000,
      },
    })

    render(<TranscriptPanel itemId="item-1" />)

    const panel = screen.getByRole('region', { name: '视频转写' })
    expect(panel).toHaveAttribute('data-asr-status', 'running')
    expect(panel.className).toContain('min-h-[68px]')
    expect(screen.getByTestId('asr-running-inline')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '转写中' })).toBeDisabled()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(screen.queryByText(/已用|%|¥|估/)).not.toBeInTheDocument()
  })

  it('ready 态不展示成本,聚焦/复制/SRT 使用中性边框,当前段使用橙红高亮和 14px 文本', () => {
    useDetailStore.setState({
      asrStatus: 'ready',
      asrRawStatus: 'success',
      asrText: 'And thank you so much.\nThe last session today of Code with Claude.',
      asrDurationSec: 1980,
      asrCostYuan: 0.98,
      asrCurrentTimeMs: 6000,
      asrSegments: [
        { start_ms: 2000, end_ms: 5000, text: 'And thank you so much.' },
        { start_ms: 5000, end_ms: 12000, text: 'The last session today of Code with Claude.' },
      ],
      asrSegmentsCn: [
        '非常感谢大家。',
        '这是今天 Code with Claude 的最后一个环节。',
      ],
    })

    render(<TranscriptPanel itemId="item-1" />)

    const panel = screen.getByRole('region', { name: '视频转写' })
    expect(panel).toHaveAttribute('data-asr-status', 'ready')
    expect(panel.className).toContain('h-[300px]')
    expect(panel.textContent).toContain('📝 33 min')
    expect(panel.textContent).toContain('字 · 双语')
    expect(panel.textContent).not.toContain('¥0.98')
    expect(panel.innerHTML).not.toContain('indigo')

    const copyButton = screen.getByTestId('asr-copy-button')
    const focusButton = screen.getByTestId('asr-focus-button')
    const srtButton = screen.getByTestId('asr-srt-button')
    expect(focusButton.className).toContain('border-[var(--modal-border)]')
    expect(copyButton.className).toContain('border-[var(--modal-border)]')
    expect(srtButton.className).toContain('border-[var(--modal-border)]')
    expect(focusButton.className).not.toContain('border-[var(--brand)]')
    expect(copyButton.className).not.toContain('border-[var(--brand)]')
    expect(srtButton.className).not.toContain('border-[var(--brand)]')
    expect(focusButton.className).not.toContain('border-primary')
    expect(copyButton.className).not.toContain('border-primary')
    expect(srtButton.className).not.toContain('border-primary')
    const autoFollow = screen.getByLabelText('自动跟随')
    const autoFollowLabel = screen.getByText('自动跟随').closest('label')
    expect(autoFollow).toBeChecked()
    expect(autoFollowLabel).not.toBeNull()
    expect(autoFollowLabel!.compareDocumentPosition(focusButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(focusButton.compareDocumentPosition(copyButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    const currentSegment = panel.querySelector('[data-seg-idx="1"]')
    const currentEnglish = currentSegment?.querySelector('[data-lang="en"]')
    const currentChinese = currentSegment?.querySelector('[data-lang="zh"]')
    expect(currentSegment?.className).toContain('bg-[var(--modal-current-bg)]')
    expect(currentEnglish?.className).toContain('text-[14px]')
    expect(currentEnglish?.className).toContain('text-[var(--brand)]')
    expect(currentChinese?.className).toContain('text-[14px]')
    expect(currentChinese?.className).toContain('text-[var(--modal-current-text)]')
  })

  it('父级 item 已有 asr_text 时首帧直接展示 ready,不露出开始转写按钮', () => {
    const item: FeedItem = {
      id: 'item-ready',
      title: 'ready video',
      platform: 'twitter',
      fetched_at: '2026-05-25T00:00:00Z',
      asr_status: 'success',
      asr_text: 'Ready transcript from item detail.',
      asr_duration_sec: 60,
      asr_segments: [{ start_ms: 0, end_ms: 1000, text: 'Ready transcript from item detail.' }],
    }

    render(<TranscriptPanel itemId="item-ready" item={item} />)

    const panel = screen.getByRole('region', { name: '视频转写' })
    expect(panel).toHaveAttribute('data-asr-status', 'ready')
    expect(screen.queryByRole('button', { name: '开始 AI 转写' })).not.toBeInTheDocument()
    expect(screen.getByText('Ready transcript from item detail.')).toBeInTheDocument()
  })
})
