import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fetchDailyDigests } from '../../lib/api'
import { useDailyDigestStore } from '../dailyDigestStore'
import type { DailyDigest, DailyDigestsResponse } from '../../lib/types'

vi.mock('../../lib/api', () => ({
  fetchDailyDigests: vi.fn(),
}))

function digest(date: string): DailyDigest {
  return { date, status: 'final', entries: [], updated_at: `${date}T03:00:00Z` }
}

function deferredResponse() {
  let resolve!: (response: DailyDigestsResponse) => void
  let reject!: (error: Error) => void
  const promise = new Promise<DailyDigestsResponse>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

describe('dailyDigestStore', () => {
  beforeEach(() => {
    vi.mocked(fetchDailyDigests).mockReset()
    useDailyDigestStore.getState().reset()
  })

  afterEach(() => {
    useDailyDigestStore.getState().reset()
  })

  it('按当前时间线日期范围拉取并按日期缓存快照', async () => {
    vi.mocked(fetchDailyDigests).mockResolvedValue({
      digests: [
        { date: '2026-08-02', status: 'rolling', entries: [], updated_at: '2026-08-02T03:00:00Z' },
      ],
    })

    await useDailyDigestStore.getState().loadRange('2026-08-01', '2026-08-02')

    expect(fetchDailyDigests).toHaveBeenCalledWith('2026-08-01', '2026-08-02')
    expect(useDailyDigestStore.getState().digestsByDate['2026-08-02']?.status).toBe('rolling')
  })

  it('已覆盖范围命中缓存，分页扩展范围时重新拉取', async () => {
    vi.mocked(fetchDailyDigests).mockResolvedValue({ digests: [] })

    await useDailyDigestStore.getState().loadRange('2026-08-02', '2026-08-02')
    await useDailyDigestStore.getState().loadRange('2026-08-02', '2026-08-02')
    await useDailyDigestStore.getState().loadRange('2026-08-01', '2026-08-02')

    expect(fetchDailyDigests).toHaveBeenCalledTimes(2)
    expect(fetchDailyDigests).toHaveBeenLastCalledWith('2026-08-01', '2026-08-02')
  })

  it.each([false, true])('跳到不连续的旧日期时补齐中间日期，force=%s', async (force) => {
    const latest = digest('2026-09-07')
    const middle = digest('2026-09-04')
    const oldest = digest('2026-09-01')
    vi.mocked(fetchDailyDigests).mockImplementation(async (start, end) => ({
      digests: [latest, middle, oldest].filter((entry) => entry.date >= start && entry.date <= end),
    }))

    await useDailyDigestStore.getState().loadRange(latest.date, latest.date)
    await useDailyDigestStore.getState().loadRange(oldest.date, oldest.date, force)
    await useDailyDigestStore.getState().loadRange(middle.date, middle.date)

    expect(fetchDailyDigests).toHaveBeenNthCalledWith(2, oldest.date, latest.date)
    expect(fetchDailyDigests).toHaveBeenCalledTimes(2)
    expect(useDailyDigestStore.getState().digestsByDate[middle.date]).toEqual(middle)
    expect(useDailyDigestStore.getState().loadedRange).toEqual({ start: oldest.date, end: latest.date })
  })

  it('向较新日期扩展时重新获取完整并集并清理并集内失效快照', async () => {
    const oldest = digest('2026-09-01')
    const latest = digest('2026-09-07')
    vi.mocked(fetchDailyDigests)
      .mockResolvedValueOnce({ digests: [oldest] })
      .mockResolvedValueOnce({ digests: [latest] })

    await useDailyDigestStore.getState().loadRange(oldest.date, oldest.date)
    await useDailyDigestStore.getState().loadRange(latest.date, latest.date)
    await useDailyDigestStore.getState().loadRange('2026-09-04', '2026-09-04')

    expect(fetchDailyDigests).toHaveBeenNthCalledWith(2, oldest.date, latest.date)
    expect(fetchDailyDigests).toHaveBeenCalledTimes(2)
    expect(useDailyDigestStore.getState().digestsByDate).toEqual({ [latest.date]: latest })
    expect(useDailyDigestStore.getState().loadedRange).toEqual({ start: oldest.date, end: latest.date })
  })

  it('并集首尾相差 31 天时仍符合接口限制，包含首尾共 32 个日期', async () => {
    vi.mocked(fetchDailyDigests).mockResolvedValue({ digests: [] })

    await useDailyDigestStore.getState().loadRange('2026-08-01', '2026-08-01')
    await useDailyDigestStore.getState().loadRange('2026-09-01', '2026-09-01')

    expect(fetchDailyDigests).toHaveBeenNthCalledWith(2, '2026-08-01', '2026-09-01')
    expect(useDailyDigestStore.getState().loadedRange).toEqual({ start: '2026-08-01', end: '2026-09-01' })
  })

  it.each([false, true])('并集超过接口限制时仅获取本次范围，不误报中间覆盖，force=%s', async (force) => {
    const oldest = digest('2026-08-01')
    const latest = digest('2026-09-02')
    vi.mocked(fetchDailyDigests)
      .mockResolvedValueOnce({ digests: [oldest] })
      .mockResolvedValueOnce({ digests: [latest] })
      .mockResolvedValueOnce({ digests: [latest] })

    await useDailyDigestStore.getState().loadRange(oldest.date, oldest.date)
    await useDailyDigestStore.getState().loadRange(latest.date, latest.date, force)

    expect(fetchDailyDigests).toHaveBeenNthCalledWith(2, latest.date, latest.date)
    expect(useDailyDigestStore.getState().loadedRange).toEqual({ start: latest.date, end: latest.date })
    expect(useDailyDigestStore.getState().digestsByDate).toEqual({ [oldest.date]: oldest, [latest.date]: latest })

    await useDailyDigestStore.getState().loadRange('2026-08-17', '2026-08-17')

    expect(fetchDailyDigests).toHaveBeenNthCalledWith(3, '2026-08-17', latest.date)
    expect(useDailyDigestStore.getState().loadedRange).toEqual({ start: '2026-08-17', end: latest.date })
  })

  it('扩展失败保留已有缓存和真实覆盖范围，重试成功后才扩大范围', async () => {
    const latest = digest('2026-09-07')
    const oldest = digest('2026-09-01')
    vi.mocked(fetchDailyDigests)
      .mockResolvedValueOnce({ digests: [latest] })
      .mockRejectedValueOnce(new Error('network unavailable'))
      .mockResolvedValueOnce({ digests: [oldest, latest] })

    await useDailyDigestStore.getState().loadRange(latest.date, latest.date)
    await useDailyDigestStore.getState().loadRange(oldest.date, oldest.date)

    expect(useDailyDigestStore.getState()).toMatchObject({
      digestsByDate: { [latest.date]: latest },
      loadedRange: { start: latest.date, end: latest.date },
      loading: false,
      error: 'network unavailable',
    })

    await useDailyDigestStore.getState().loadRange(oldest.date, oldest.date)

    expect(fetchDailyDigests).toHaveBeenNthCalledWith(3, oldest.date, latest.date)
    expect(useDailyDigestStore.getState()).toMatchObject({
      digestsByDate: { [oldest.date]: oldest, [latest.date]: latest },
      loadedRange: { start: oldest.date, end: latest.date },
      loading: false,
      error: null,
    })
  })

  it('较早请求晚返回时不覆盖较新请求的快照或扩大未覆盖范围', async () => {
    const latest = digest('2026-09-07')
    const middle = digest('2026-09-04')
    const oldest = digest('2026-09-01')
    const olderRequest = deferredResponse()
    const newerRequest = deferredResponse()
    vi.mocked(fetchDailyDigests)
      .mockResolvedValueOnce({ digests: [latest] })
      .mockReturnValueOnce(olderRequest.promise)
      .mockReturnValueOnce(newerRequest.promise)

    await useDailyDigestStore.getState().loadRange(latest.date, latest.date)
    const olderLoad = useDailyDigestStore.getState().loadRange(oldest.date, oldest.date)
    const newerLoad = useDailyDigestStore.getState().loadRange(middle.date, middle.date)
    newerRequest.resolve({ digests: [middle, latest] })
    await newerLoad
    const acceptedState = useDailyDigestStore.getState()
    olderRequest.resolve({ digests: [oldest, latest] })
    await olderLoad

    expect(fetchDailyDigests).toHaveBeenNthCalledWith(3, middle.date, latest.date)
    expect(useDailyDigestStore.getState()).toBe(acceptedState)
    expect(acceptedState.loadedRange).toEqual({ start: middle.date, end: latest.date })
    expect(acceptedState.digestsByDate[oldest.date]).toBeUndefined()
  })

  it('较早请求失败不结束正在进行的新请求或写入过期错误', async () => {
    const olderRequest = deferredResponse()
    const newerRequest = deferredResponse()
    vi.mocked(fetchDailyDigests)
      .mockReturnValueOnce(olderRequest.promise)
      .mockReturnValueOnce(newerRequest.promise)

    const olderLoad = useDailyDigestStore.getState().loadRange('2026-09-01', '2026-09-01')
    const newerLoad = useDailyDigestStore.getState().loadRange('2026-09-04', '2026-09-04')
    olderRequest.reject(new Error('outdated failure'))
    await olderLoad

    expect(useDailyDigestStore.getState()).toMatchObject({ loading: true, error: null, loadedRange: null })

    newerRequest.resolve({ digests: [digest('2026-09-04')] })
    await newerLoad
    expect(useDailyDigestStore.getState()).toMatchObject({
      loading: false,
      error: null,
      loadedRange: { start: '2026-09-04', end: '2026-09-04' },
    })
  })

  it('force 刷新已覆盖的子区间时保留区间外缓存和覆盖边界', async () => {
    const oldest = digest('2026-09-01')
    const middle = digest('2026-09-04')
    const latest = digest('2026-09-07')
    vi.mocked(fetchDailyDigests)
      .mockResolvedValueOnce({ digests: [oldest, middle, latest] })
      .mockResolvedValueOnce({ digests: [] })

    await useDailyDigestStore.getState().loadRange(oldest.date, latest.date)
    await useDailyDigestStore.getState().loadRange(middle.date, middle.date, true)

    expect(fetchDailyDigests).toHaveBeenNthCalledWith(2, middle.date, middle.date)
    expect(useDailyDigestStore.getState().digestsByDate).toEqual({ [oldest.date]: oldest, [latest.date]: latest })
    expect(useDailyDigestStore.getState().loadedRange).toEqual({ start: oldest.date, end: latest.date })
  })

  it('force=true 时刷新已缓存范围并清除该范围内失效快照', async () => {
    vi.mocked(fetchDailyDigests)
      .mockResolvedValueOnce({
        digests: [
          { date: '2026-08-02', status: 'rolling', entries: [], updated_at: '2026-08-02T03:00:00Z' },
        ],
      })
      .mockResolvedValueOnce({ digests: [] })

    await useDailyDigestStore.getState().loadRange('2026-08-02', '2026-08-02')
    await useDailyDigestStore.getState().loadRange('2026-08-02', '2026-08-02', true)

    expect(fetchDailyDigests).toHaveBeenCalledTimes(2)
    expect(useDailyDigestStore.getState().digestsByDate['2026-08-02']).toBeUndefined()
  })
})
