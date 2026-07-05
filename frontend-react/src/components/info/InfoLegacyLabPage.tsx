import { useCallback, useEffect, useState } from 'react'
import { fetchClassification } from '../../lib/api'
import { useFeedStore } from '../../store/feedStore'
import { TopBar } from '../layout/TopBar'
import { ChannelsView } from '../channels/ChannelsView'
import { InfoCategoryView } from './InfoCategoryView'
import { InfoSidebar } from './InfoSidebar'
import type { InfoGroupBy } from './InfoGroupByToggle'

/**
 * 5.18 频道页对照版。
 *
 * 这个页面用于恢复 2026-05-18 的信息页「按频道」形态：
 * TopBar + InfoSidebar + ChannelsView。它是独立实验路由，不影响真实 #v=info。
 */
export function InfoLegacyLabPage() {
  const [groupBy, setGroupBy] = useState<InfoGroupBy>('platform')
  const setClassification = useFeedStore((s) => s.setClassification)
  const classification = useFeedStore((s) => s.classification)

  useEffect(() => {
    if (classification) return
    let cancelled = false
    fetchClassification()
      .then((next) => {
        if (!cancelled) setClassification(next)
      })
      .catch((err) => {
        console.error('[InfoLegacyLabPage] failed to load classification', err)
      })
    return () => {
      cancelled = true
    }
  }, [classification, setClassification])

  const handleGroupByChange = useCallback((next: InfoGroupBy) => {
    setGroupBy(next)
  }, [])

  return (
    <div className="min-h-screen bg-background text-foreground" data-testid="info-legacy-lab-page">
      <TopBar activeL1="info" />
      <main className="mx-auto max-w-[1360px] px-4 pt-4" data-testid="info-legacy-lab-channel-shell">
        <InfoSidebar
          groupBy={groupBy}
          onGroupByChange={handleGroupByChange}
        />
        <div className="min-w-0">
          {groupBy === 'platform' ? (
            <ChannelsView embedded />
          ) : (
            <InfoCategoryView embedded />
          )}
        </div>
      </main>
    </div>
  )
}
