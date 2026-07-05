export function proxiedImageUrl(rawUrl?: string | null): string {
  const url = rawUrl?.trim()
  if (!url) return ''
  if (url.startsWith('/') || url.startsWith('data:') || url.startsWith('blob:')) return url

  try {
    const parsed = new URL(url)
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      return `/api/media/image-proxy?url=${encodeURIComponent(url)}`
    }
  } catch {
    return url
  }

  return url
}
