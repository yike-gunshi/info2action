export const INFO2ACT_SHARE_BASE_URL = 'https://www.info2act.com'

export function buildInfoItemHash(itemId: string): string {
  return `v=info&d=${encodeURIComponent(itemId)}`
}

export function buildInfoItemHref(itemId: string): string {
  return `/#${buildInfoItemHash(itemId)}`
}

export function buildInfoItemShareUrl(itemId: string): string {
  return `${INFO2ACT_SHARE_BASE_URL}#${buildInfoItemHash(itemId)}`
}

export function parseLegacyItemHash(rawHash: string): string | null {
  const raw = rawHash.startsWith('#') ? rawHash.slice(1) : rawHash
  if (!raw.startsWith('item=')) return null
  const encoded = raw.slice('item='.length).trim()
  if (!encoded) return null
  try {
    return decodeURIComponent(encoded)
  } catch {
    return encoded
  }
}

export function clearItemDetailHash(itemId?: string): void {
  if (typeof window === 'undefined') return
  const raw = window.location.hash.slice(1)
  if (!raw) return

  const params = new URLSearchParams(raw)
  const currentDetailId = params.get('d')
  if (!currentDetailId) return
  if (itemId && currentDetailId !== itemId) return

  params.delete('d')
  const nextHash = params.toString()
  const nextUrl = `${window.location.pathname}${window.location.search}${nextHash ? `#${nextHash}` : ''}`
  window.history.replaceState({}, '', nextUrl)
}
