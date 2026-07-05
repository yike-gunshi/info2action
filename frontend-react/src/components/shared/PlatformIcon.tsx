import { cn, eventPlatformName, platformName, platformClass } from '../../lib/utils'

interface PlatformIconProps {
  platform: string
  size?: 'sm' | 'md' | 'lg'
  className?: string
}

export function PlatformBrandIcon({ platform, className }: { platform: string; className?: string }) {
  const normalized = platform.toLowerCase()

  if (normalized === 'twitter' || normalized === 'x') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" className={className}>
        <path
          fill="currentColor"
          d="M13.9 10.5 21.3 2h-1.8l-6.4 7.4L8 2H2l7.8 11.4L2 22h1.8l6.8-7.8 5.4 7.8h6l-8.1-11.5Zm-2.4 2.7-.8-1.1L4.4 3.3h2.8l5 7 .8 1.1 6.6 9.3h-2.8l-5.3-7.5Z"
        />
      </svg>
    )
  }

  if (normalized === 'github') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" className={className}>
        <path
          fill="currentColor"
          d="M12 .8a11.2 11.2 0 0 0-3.5 21.8c.6.1.8-.2.8-.6v-2.1c-3.4.8-4.1-1.4-4.1-1.4-.6-1.4-1.4-1.8-1.4-1.8-1.1-.8.1-.8.1-.8 1.2.1 1.9 1.3 1.9 1.3 1.1 1.9 2.9 1.3 3.6 1 .1-.8.4-1.3.8-1.6-2.7-.3-5.6-1.4-5.6-6.1 0-1.3.5-2.4 1.2-3.3-.1-.3-.5-1.6.1-3.3 0 0 1-.3 3.4 1.2a11.7 11.7 0 0 1 6.2 0C17 3.6 18 3.9 18 3.9c.6 1.7.2 3 .1 3.3.8.9 1.2 2 1.2 3.3 0 4.8-2.9 5.8-5.6 6.1.4.4.8 1.1.8 2.2V22c0 .4.2.7.8.6A11.2 11.2 0 0 0 12 .8Z"
        />
      </svg>
    )
  }

  if (normalized === 'youtube') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" className={className}>
        <path
          fill="currentColor"
          d="M9.2 7.2v9.6L17.4 12 9.2 7.2Z"
        />
      </svg>
    )
  }

  if (normalized === 'rss') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" className={className}>
        <path fill="currentColor" d="M6.2 16.1a2.9 2.9 0 1 1 0 5.8 2.9 2.9 0 0 1 0-5.8Z" />
        <path fill="currentColor" d="M3.3 9.1a11.6 11.6 0 0 1 11.6 11.6H11A7.7 7.7 0 0 0 3.3 13V9.1Z" />
        <path fill="currentColor" d="M3.3 2.2a18.5 18.5 0 0 1 18.5 18.5h-3.9A14.6 14.6 0 0 0 3.3 6.1V2.2Z" />
      </svg>
    )
  }

  if (normalized === 'lingowhale' || normalized === 'wechat_mp') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" className={className}>
        <path
          fill="currentColor"
          d="M9.2 4.1c-4 0-7.2 2.6-7.2 5.8 0 1.8 1.1 3.5 2.8 4.6L4 16.8l2.6-1.3c.8.2 1.7.4 2.6.4 4 0 7.2-2.6 7.2-5.9S13.2 4.1 9.2 4.1ZM6.7 8.7a.9.9 0 1 1 0-1.8.9.9 0 0 1 0 1.8Zm4.9 0a.9.9 0 1 1 0-1.8.9.9 0 0 1 0 1.8Z"
        />
        <path
          fill="currentColor"
          d="M22 13.4c0-2.7-2.5-4.8-5.8-5.2.2.6.3 1.2.3 1.8 0 3.8-3.4 6.8-7.8 7.1 1 1.5 3 2.5 5.3 2.5.8 0 1.5-.1 2.1-.3l2.2 1.1-.6-2c2.5-.8 4.3-2.7 4.3-5Zm-7.5-.8a.8.8 0 1 1 0-1.6.8.8 0 0 1 0 1.6Zm4 0a.8.8 0 1 1 0-1.6.8.8 0 0 1 0 1.6Z"
        />
      </svg>
    )
  }

  if (normalized === 'reddit') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" className={className}>
        <path
          fill="currentColor"
          d="M19.9 10.4a2.1 2.1 0 0 0-3.5-1.6 10 10 0 0 0-3.8-1l.8-3.5 2.5.5a1.7 1.7 0 1 0 .2-1.2L13 3a.7.7 0 0 0-.8.5l-1 4.3a10.4 10.4 0 0 0-3.9 1.1A2.1 2.1 0 1 0 5 12.3c-.1.2-.1.5-.1.8 0 3 3.2 5.4 7.1 5.4s7.1-2.4 7.1-5.4c0-.3 0-.5-.1-.8.6-.4.9-1.1.9-1.9Zm-11 2.1a1.3 1.3 0 1 1 2.6 0 1.3 1.3 0 0 1-2.6 0Zm6.2 3.2c-.9.8-2 .9-3.1.9s-2.2-.1-3.1-.9a.6.6 0 1 1 .8-.9c.5.5 1.3.6 2.3.6s1.8-.1 2.3-.6a.6.6 0 0 1 .8.9Zm-.1-1.9a1.3 1.3 0 1 1 0-2.6 1.3 1.3 0 0 1 0 2.6Z"
        />
      </svg>
    )
  }

  if (normalized === 'bilibili') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" className={className}>
        <path
          fill="currentColor"
          d="M7.7 3.2 10 5.5h4l2.3-2.3 1.2 1.2-1.7 1.7h1.5c1.9 0 3.4 1.5 3.4 3.4v6.9c0 1.9-1.5 3.4-3.4 3.4H6.7a3.4 3.4 0 0 1-3.4-3.4V9.5c0-1.9 1.5-3.4 3.4-3.4h1.5L6.5 4.4l1.2-1.2Zm-.8 5.3c-1 0-1.8.8-1.8 1.8v5.6c0 1 .8 1.8 1.8 1.8h10.2c1 0 1.8-.8 1.8-1.8v-5.6c0-1-.8-1.8-1.8-1.8H6.9Zm1.8 3.3h1.6v2.9H8.7v-2.9Zm5 0h1.6v2.9h-1.6v-2.9Z"
        />
      </svg>
    )
  }

  if (normalized === 'hackernews') {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" className={className}>
        <path fill="currentColor" d="M4 4h16v16H4V4Zm8.9 9 4-6h-2.2L12 11.2 9.3 7H7l4 6v4h1.9v-4Z" />
      </svg>
    )
  }

  return (
    <span aria-hidden="true" className={cn('font-bold leading-none', className)}>
      {eventPlatformName(platform).slice(0, 1)}
    </span>
  )
}

/**
 * Unified platform icon/badge.
 * Shows platform color + abbreviated name.
 */
export function PlatformIcon({ platform, size = 'sm', className }: PlatformIconProps) {
  const sizeClasses = {
    sm: 'text-[11px] px-1 py-px leading-snug',
    md: 'text-xs px-1.5 leading-snug',
    lg: 'text-sm px-2 py-0.5',
  }

  return (
    <span
      className={cn(
        'inline-flex items-center font-bold rounded',
        sizeClasses[size],
        platformClass(platform),
        className,
      )}
    >
      {platformName(platform)}
    </span>
  )
}

/**
 * Round platform avatar for Tweet Card layout.
 * Uses author avatar or platform icon as fallback.
 */
export function PlatformAvatar({
  platform,
  avatarUrl,
  size = 40,
}: {
  platform: string
  avatarUrl?: string
  size?: number
}) {
  if (avatarUrl) {
    return (
      <img
        src={avatarUrl}
        alt=""
        className="rounded-full object-cover flex-shrink-0"
        style={{ width: size, height: size }}
        loading="lazy"
        referrerPolicy="no-referrer"
      />
    )
  }

  // Fallback: platform color circle with initial
  const name = platformName(platform)
  return (
    <div
      className={cn(
        'rounded-full flex items-center justify-center font-bold text-white flex-shrink-0',
        platformClass(platform),
      )}
      style={{ width: size, height: size, fontSize: size * 0.4 }}
    >
      {name.charAt(0)}
    </div>
  )
}
