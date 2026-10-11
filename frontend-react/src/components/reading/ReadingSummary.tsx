import { cn } from '../../lib/utils'
import { renderMarkdownInline, renderMarkdownLite } from '../../lib/markdown-lite'
import { paragraphLines } from './readingUtils'
import type { ReadingKeyPoint } from './readingUtils'

type ReadingSection = { title: string; points: string[] }

interface ReadingSummaryTestIds {
  container: string
  lead?: string
  sections?: string
  keyPoints?: string
  sectionNumber?: string
  bulletDot?: string
}

export function ReadingSummary({
  fallbackBreakdown,
  keyPoints,
  lead,
  sections = [],
  testIds,
}: {
  fallbackBreakdown?: string | null
  keyPoints?: ReadingKeyPoint[] | null
  lead?: string | null
  sections?: ReadingSection[]
  testIds: ReadingSummaryTestIds
}) {
  const leadLines = lead ? paragraphLines(lead) : []
  const hasLead = leadLines.length > 0
  const hasSections = sections.length > 0
  const hasFallback = !!fallbackBreakdown
  const hasKeyPoints = !!keyPoints?.length
  const hasFollowingContent = hasSections || hasFallback || hasKeyPoints
  if (!hasLead && !hasFollowingContent) return null

  const renderBulletList = (points: string[]) => (
    <ul className="reading-bullet space-y-1 pl-6 sm:pl-[38px]">
      {points.map((point, index) => (
        <li key={`${point}-${index}`} className="relative">
          <span
            data-testid={testIds.bulletDot}
            className="absolute -left-3.5 top-[0.78em] h-1 w-1 rounded-full bg-[var(--modal-text)] sm:-left-4"
            aria-hidden="true"
          />
          {renderMarkdownInline(point)}
        </li>
      ))}
    </ul>
  )

  return (
    <section data-testid={testIds.container} className="mb-5 text-[var(--modal-text-soft)]">
      {hasLead && (
        <div
          data-testid={testIds.lead}
          className={cn('reading-body pb-5', hasFollowingContent && 'border-b border-[var(--modal-divider)]')}
        >
          {leadLines.map((line, index) => (
            <p key={index} className={index > 0 ? 'mt-2.5' : undefined}>
              {index === 0 && <strong className="mr-1.5 !text-[var(--brand)]">精华速览：</strong>}
              {renderMarkdownInline(line)}
            </p>
          ))}
        </div>
      )}

      {hasFallback && !hasSections && (
        <div
          data-testid={testIds.sections}
          className="reading-bullet py-4 [&_ul]:my-2.5 [&_ul]:space-y-1"
        >
          {renderMarkdownLite(fallbackBreakdown || '')}
        </div>
      )}

      {hasSections && (
        <div
          data-testid={testIds.sections}
          className="py-4"
        >
          <div className="space-y-3.5">
            {sections.map((section, index) => (
              <article key={`${section.title}-${index}`}>
                <div className="mb-2 flex items-baseline gap-2.5">
                  <span
                    data-testid={testIds.sectionNumber}
                    className="reading-section leading-none text-[var(--brand)]"
                  >
                    {String(index + 1).padStart(2, '0')}
                  </span>
                  <h3 className="reading-section min-w-0">
                    {renderMarkdownInline(section.title)}
                  </h3>
                </div>
                {section.points.length > 0 && renderBulletList(section.points)}
              </article>
            ))}
          </div>
        </div>
      )}

      {!hasSections && !hasFallback && hasKeyPoints && (
        <ul
          data-testid={testIds.keyPoints}
          className="reading-bullet space-y-1 py-4 pl-6 sm:pl-[38px]"
        >
          {(keyPoints ?? []).map((point, index) => {
            if (typeof point === 'string') {
              return (
                <li key={`${point}-${index}`} className="relative">
                  <span
                    data-testid={testIds.bulletDot}
                    className="absolute -left-3.5 top-[0.78em] h-1 w-1 rounded-full bg-[var(--modal-text)] sm:-left-4"
                    aria-hidden="true"
                  />
                  {renderMarkdownInline(point)}
                </li>
              )
            }
            const points = Array.isArray(point.points) ? point.points : []
            return (
              <li key={`${point.title}-${index}`} className="relative -ml-6 pb-3.5 last:pb-0 sm:-ml-[38px]">
                <div className="mb-2 flex items-baseline gap-2.5">
                  <span
                    data-testid={testIds.sectionNumber}
                    className="reading-section leading-none text-[var(--brand)]"
                  >
                    {String(index + 1).padStart(2, '0')}
                  </span>
                  <div className="reading-section min-w-0">{renderMarkdownInline(point.title)}</div>
                </div>
                {points.length > 0 && renderBulletList(points)}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
