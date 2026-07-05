const LEGACY_CACHE_PREFIX = 'ohmynews-';

async function deleteLegacyCaches() {
  if (!self.caches) return;
  const cacheNames = await self.caches.keys();
  await Promise.all(
    cacheNames
      .filter((name) => name.startsWith(LEGACY_CACHE_PREFIX))
      .map((name) => self.caches.delete(name).catch(() => false)),
  );
}

self.addEventListener('install', (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    await self.clients.claim().catch(() => undefined);
    await deleteLegacyCaches().catch(() => undefined);
    await self.registration.unregister().catch(() => false);
  })());
});
