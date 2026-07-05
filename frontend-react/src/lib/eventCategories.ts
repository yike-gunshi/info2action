import type { ClassificationConfig } from './types'

export interface EventCategoryOption {
  id: string
  label: string
  priority: number
}

export const HIDDEN_EVENT_CATEGORY_IDS = new Set(['other', '_uncategorized', '__uncategorized__'])

export const FALLBACK_EVENT_CATEGORY_OPTIONS: EventCategoryOption[] = [
  { id: 'products', label: '产品', priority: 1 },
  { id: 'efficiency_tools', label: '工具', priority: 2 },
  { id: 'coding', label: 'Coding', priority: 3 },
  { id: 'skill', label: 'Skill', priority: 4 },
  { id: 'models', label: '模型', priority: 5 },
  { id: 'eval', label: '评测', priority: 6 },
  { id: 'tech', label: '技术', priority: 7 },
  { id: 'tutorials', label: '教程', priority: 8 },
  { id: 'industry', label: '行业', priority: 9 },
  { id: 'creator', label: '创作', priority: 10 },
  { id: 'investment', label: '投资', priority: 11 },
  { id: 'startup', label: '创业', priority: 12 },
  { id: 'events', label: '活动', priority: 13 },
]

const EVENT_CATEGORY_LABELS = Object.fromEntries(
  FALLBACK_EVENT_CATEGORY_OPTIONS.map((category) => [category.id, category.label]),
) as Record<string, string>

export function eventCategoryLabel(category?: string | null): string | null {
  if (!category || HIDDEN_EVENT_CATEGORY_IDS.has(category)) return null
  return EVENT_CATEGORY_LABELS[category] || category
}

export function eventCategoryOptionsFromClassification(
  classification?: ClassificationConfig | null,
): EventCategoryOption[] {
  if (!classification) return FALLBACK_EVENT_CATEGORY_OPTIONS

  const options = classification.categories
    .filter((category) => category.visible && !HIDDEN_EVENT_CATEGORY_IDS.has(category.id))
    .sort((a, b) => (a.priority ?? 99) - (b.priority ?? 99))
    .map((category) => ({
      id: category.id,
      label: category.name,
      priority: category.priority ?? 99,
    }))

  return options.length > 0 ? options : FALLBACK_EVENT_CATEGORY_OPTIONS
}
