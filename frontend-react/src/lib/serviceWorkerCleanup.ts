const LEGACY_CACHE_PREFIX = 'ohmynews-'

async function unregisterServiceWorkers(): Promise<void> {
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return

  const registrations = await navigator.serviceWorker.getRegistrations()
  await Promise.all(
    registrations.map(async (registration) => {
      try {
        await registration.unregister()
      } catch {
        // Startup cleanup must never block the public feed.
      }
    }),
  )
}

async function deleteLegacyCaches(): Promise<void> {
  if (typeof window === 'undefined' || !('caches' in window)) return

  const cacheNames = await window.caches.keys()
  await Promise.all(
    cacheNames
      .filter((name) => name.startsWith(LEGACY_CACHE_PREFIX))
      .map(async (name) => {
        try {
          await window.caches.delete(name)
        } catch {
          // Ignore stale cache deletion failures; the next startup can retry.
        }
      }),
  )
}

export async function retireLegacyServiceWorker(): Promise<void> {
  if (typeof window === 'undefined') return

  await Promise.all([
    unregisterServiceWorkers().catch(() => undefined),
    deleteLegacyCaches().catch(() => undefined),
  ])
}
