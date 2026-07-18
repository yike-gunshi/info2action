import type { LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'
import { InfoSectionPillBar } from '../shared/InfoSectionPillBar'
import { platformName } from '../../lib/utils'

export function LibraryPageHeader({
  title,
  meta,
}: {
  title: string
  meta: string
}) {
  return (
    <div className="mb-5 px-1">
      <h1 className="font-display text-[28px] font-semibold leading-tight tracking-normal text-foreground">
        {title}
      </h1>
      <p className="mt-1 font-body-cjk text-[13px] text-muted-foreground">
        {meta}
      </p>
    </div>
  )
}

export function LibraryPlatformFilter({
  sectionKey,
  platforms,
  activePlatform,
  onSelect,
}: {
  sectionKey: string
  platforms: string[]
  activePlatform: string | null
  onSelect: (platform: string | null) => void
}) {
  if (platforms.length === 0) return null

  return (
    <InfoSectionPillBar
      sectionKey={`${sectionKey}-platform`}
      items={[
        { key: null, label: '全部' },
        ...platforms.map((platform) => ({
          key: platform,
          label: platformName(platform),
        })),
      ]}
      activeKey={activePlatform}
      onSelect={onSelect}
      className="mb-5"
      data-testid={`${sectionKey}-platform-filter`}
    />
  )
}

export function LibraryDateSectionHeader({
  label,
  count,
}: {
  label: string
  count: number
}) {
  return (
    <div className="mb-3 flex items-baseline gap-2 border-b border-border/70 px-1 pb-2">
      <h2 className="font-display text-[22px] font-semibold leading-none tracking-normal text-foreground">
        {label}
      </h2>
      <span className="font-body-cjk text-[13px] text-muted-foreground">
        {count} 条
      </span>
    </div>
  )
}

export function LibraryEmptyState({
  header,
  icon: Icon,
  title,
  description,
}: {
  header: ReactNode
  icon: LucideIcon
  title: string
  description: string
}) {
  return (
    <div className="px-4 py-16 text-center">
      <div className="mx-auto mb-8 max-w-[1168px] text-left">
        {header}
      </div>
      <div className="mx-auto mb-4 flex h-10 w-10 items-center justify-center rounded-[4px] border border-border bg-card text-muted-foreground">
        <Icon className="h-5 w-5" strokeWidth={1.7} aria-hidden="true" />
      </div>
      <p className="font-body-cjk text-sm font-medium text-foreground">{title}</p>
      <p className="mt-1 font-body-cjk text-sm text-muted-foreground">{description}</p>
    </div>
  )
}
