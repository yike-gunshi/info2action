export function DetailSkeleton() {
  return (
    <div className="space-y-4 animate-pulse">
      {/* Image skeleton */}
      <div className="w-full h-48 rounded-lg bg-muted animate-skeleton" />
      {/* Meta line */}
      <div className="flex gap-2">
        <div className="w-16 h-5 rounded bg-muted animate-skeleton" />
        <div className="w-10 h-5 rounded bg-muted animate-skeleton" />
        <div className="w-20 h-5 rounded bg-muted animate-skeleton" />
      </div>
      {/* Title */}
      <div className="w-3/4 h-7 rounded bg-muted animate-skeleton" />
      {/* Author */}
      <div className="flex items-center gap-3">
        <div className="w-9 h-9 rounded-full bg-muted animate-skeleton" />
        <div className="w-24 h-4 rounded bg-muted animate-skeleton" />
      </div>
      <div className="h-px bg-border" />
      {/* Summary */}
      <div className="w-full h-20 rounded-lg bg-muted animate-skeleton" />
      {/* Content */}
      <div className="space-y-2">
        <div className="w-full h-4 rounded bg-muted animate-skeleton" />
        <div className="w-5/6 h-4 rounded bg-muted animate-skeleton" />
        <div className="w-2/3 h-4 rounded bg-muted animate-skeleton" />
        <div className="w-3/4 h-4 rounded bg-muted animate-skeleton" />
      </div>
    </div>
  )
}
