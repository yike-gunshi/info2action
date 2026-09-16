import { useMemo } from 'react'
import type { ReactNode } from 'react'
import { cn } from '../../lib/utils'
import { eventCategoryOptionsFromClassification } from '../../lib/eventCategories'
import { useEventsStore } from '../../store/eventsStore'
import { useFeedStore } from '../../store/feedStore'

export function HighlightsFilterTabs({ dateControl }: { dateControl?: ReactNode }) {
  const classification = useFeedStore((s) => s.classification)
  const selectedCategories = useEventsStore((s) => s.filters.categories)
  const setFilters = useEventsStore((s) => s.setFilters)
  const categories = useMemo(
    () => eventCategoryOptionsFromClassification(classification),
    [classification],
  )
  const selectedCategory = selectedCategories[0] ?? null

  const handleSelect = (category: string | null) => {
    const nextCategories = category ? [category] : []
    window.scrollTo({ top: 0 })
    void setFilters({ categories: nextCategories })
  }

  const tabClassName = (selected: boolean) => cn(
    'relative flex h-10 shrink-0 items-center border-b-2 px-0.5 font-event-title text-[16px] font-medium tracking-normal transition-colors',
    'focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-border)] focus-visible:ring-offset-2 focus-visible:ring-offset-background',
    selected
      ? 'border-[var(--brand)] text-[var(--brand)]'
      : 'border-transparent text-muted-foreground hover:text-foreground',
  )

  return (
    <div
      className="highlights-filter-row sticky top-[var(--highlights-l2-top)] z-50 flex min-w-0 bg-background"
      data-testid="highlights-filter-tabs"
    >
      <nav aria-label="精选分类筛选" role="tablist" className="min-w-0 flex-1 overflow-x-auto scrollbar-hide">
      <div className="mx-auto flex h-10 w-full min-w-0 items-center justify-start gap-6 border-b border-border/70 sm:gap-8" data-testid="highlights-filter-tabs-inner">
        <button
          type="button"
          role="tab"
          aria-selected={!selectedCategory}
          className={tabClassName(!selectedCategory)}
          onClick={() => handleSelect(null)}
          data-testid="highlights-filter-tab-all"
        >
          全部
        </button>
        {categories.map((category) => {
          const selected = selectedCategory === category.id
          return (
            <button
              key={category.id}
              type="button"
              role="tab"
              aria-selected={selected}
              className={tabClassName(selected)}
              onClick={() => handleSelect(category.id)}
              data-testid={`highlights-filter-tab-${category.id}`}
            >
              {category.label}
            </button>
          )
        })}
      </div>
      </nav>
      {dateControl}
    </div>
  )
}
