import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** Format large numbers: 1000→1k, 10000→1w */
export function formatNumber(n: number): string {
  if (n >= 10000) return `${(n / 10000).toFixed(1)}w`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

/** Platform display name */
export function platformName(platform: string): string {
  const map: Record<string, string> = {
    x: 'X',
    twitter: 'X',
    // v16.0 deprecated: 小红书已下线 (PLATFORM_ORDER 不再含),保留 label 用于兜底渲染历史 item
    xiaohongshu: '小红书',
    bilibili: 'B站',
    reddit: 'Reddit',
    hackernews: 'HN',
    github: 'GitHub',
    youtube: 'YouTube',
    rss: 'RSS',
    lingowhale: '公众号',
    waytoagi: 'AGI之路',
    wechat_mp: '公众号',
    manual: '手动提交',
  }
  return map[platform] || platform
}

/** Event/modal platform display name. */
export function eventPlatformName(platform: string): string {
  return platform === 'lingowhale' ? '公众号' : platformName(platform)
}

/** Platform CSS class for tag color */
export function platformClass(platform: string): string {
  const map: Record<string, string> = {
    x: 'bg-platform-twitter text-white',
    twitter: 'bg-platform-twitter text-white',
    // v16.0 deprecated: 同上
    xiaohongshu: 'bg-platform-xhs text-white',
    bilibili: 'bg-platform-bili text-white',
    reddit: 'bg-platform-reddit text-white',
    hackernews: 'bg-amber text-white',
    github: 'bg-platform-github text-white',
    youtube: 'bg-platform-youtube text-white',
    rss: 'bg-platform-rss text-white',
    lingowhale: 'bg-platform-lingowhale text-white',
    wechat_mp: 'bg-platform-lingowhale text-white',
    waytoagi: 'bg-platform-waytoagi text-white',
  }
  return map[platform] || 'bg-warm-300 text-warm-700'
}


/** Strip markdown/HTML from text */
export function stripMd(s: string): string {
  return s
    .replace(/<[^>]+>/g, '')
    .replace(/[*_~`#]/g, '')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .trim()
}

/** Relative time display */
export function relativeTime(date: string | number | Date): string {
  const now = Date.now()
  const ts = new Date(date).getTime()
  const diff = now - ts

  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}小时前`
  if (diff < 604_800_000) return `${Math.floor(diff / 86_400_000)}天前`

  const d = new Date(ts)
  return `${d.getMonth() + 1}/${d.getDate()}`
}

/** HTML escape */
export function esc(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/** Action type display name */
export function actionTypeName(type: string): string {
  const map: Record<string, string> = {
    research: '调研验证',
    investigate: '调研验证',
    implementation: '动手做',
    implement: '动手做',
    content: '创作内容',
  }
  return map[type] || type
}

/** Action type color classes */
export function actionTypeClass(type: string): string {
  const map: Record<string, string> = {
    research: 'text-amber bg-amber-bg',
    investigate: 'text-amber bg-amber-bg',
    implementation: 'text-primary bg-accent',
    implement: 'text-primary bg-accent',
    content: 'text-emerald bg-emerald-bg',
  }
  return map[type] || 'text-warm-600 bg-warm-200'
}
