import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  getAdminHighlightsFunnel,
  getAdminHighlightsFunnelRows,
  setAdminHighlightOverride,
  submitFeedback,
} from '../api'

function response(body: unknown) {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as Response
}

describe('精选漏斗 API', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('封装 3 天 panorama 与正交筛选参数', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ stations: [], diffs: [], anomalies_count: 0, gate_disabled: false }))
      .mockResolvedValueOnce(response({ granularity: 'cluster', items: [], total: 0, page: 2 }))
    vi.stubGlobal('fetch', fetchMock)

    await getAdminHighlightsFunnel({ days: 3, q: '模型', tag: 'efficiency_tools' })
    await getAdminHighlightsFunnelRows({
      view: 'panorama',
      days: 3,
      q: '模型',
      tag: 'efficiency_tools',
      display: 'hidden',
      stage: 'blocked_display',
      page: 2,
      limit: 100,
    })

    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/admin/highlights/funnel?days=3&q=%E6%A8%A1%E5%9E%8B&tag=efficiency_tools')
    expect(String(fetchMock.mock.calls[1][0])).toBe('/api/admin/highlights/funnel/rows?view=panorama&days=3&q=%E6%A8%A1%E5%9E%8B&tag=efficiency_tools&display=hidden&stage=blocked_display&page=2&limit=100')
  })

  it('item 收录与排除均走语义反馈端点', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ ok: true, active: true }))
    vi.stubGlobal('fetch', fetchMock)

    await submitFeedback('item-1', 'should_drop', '排除营销内容')

    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/feedback')
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      item_id: 'item-1',
      type: 'should_drop',
      text: '排除营销内容',
    })
  })

  it('簇级展示反馈走 admin override 端点', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({
      ok: true,
      manual_display: 'force_show',
      manual_display_at: '2026-07-17T00:00:00Z',
      feedback_kind: 'should_feature',
      feedback_note: '应展示',
    }))
    vi.stubGlobal('fetch', fetchMock)

    await setAdminHighlightOverride(42, 'force_show', '应展示')

    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/admin/highlights/clusters/42/override')
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      action: 'force_show',
      note: '应展示',
    })
  })
})
