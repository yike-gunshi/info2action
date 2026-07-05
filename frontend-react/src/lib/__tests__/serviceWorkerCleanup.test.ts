import { afterEach, describe, expect, it, vi } from 'vitest'
import { retireLegacyServiceWorker } from '../serviceWorkerCleanup'

function setServiceWorker(value: unknown) {
  Object.defineProperty(window.navigator, 'serviceWorker', {
    value,
    configurable: true,
  })
}

function setCaches(value: unknown) {
  Object.defineProperty(window, 'caches', {
    value,
    configurable: true,
  })
}

describe('retireLegacyServiceWorker', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    Reflect.deleteProperty(window.navigator, 'serviceWorker')
    Reflect.deleteProperty(window, 'caches')
  })

  it('does nothing when service workers and cache storage are unavailable', async () => {
    await expect(retireLegacyServiceWorker()).resolves.toBeUndefined()
  })

  it('unregisters existing service workers and deletes only legacy ohmynews caches', async () => {
    const firstRegistration = { unregister: vi.fn().mockResolvedValue(true) }
    const secondRegistration = { unregister: vi.fn().mockResolvedValue(true) }
    const serviceWorker = {
      getRegistrations: vi.fn().mockResolvedValue([firstRegistration, secondRegistration]),
    }
    const caches = {
      keys: vi.fn().mockResolvedValue(['ohmynews-v9.1', 'i2a-runtime', 'ohmynews-api']),
      delete: vi.fn().mockResolvedValue(true),
    }
    setServiceWorker(serviceWorker)
    setCaches(caches)

    await retireLegacyServiceWorker()

    expect(serviceWorker.getRegistrations).toHaveBeenCalledTimes(1)
    expect(firstRegistration.unregister).toHaveBeenCalledTimes(1)
    expect(secondRegistration.unregister).toHaveBeenCalledTimes(1)
    expect(caches.keys).toHaveBeenCalledTimes(1)
    expect(caches.delete).toHaveBeenCalledTimes(2)
    expect(caches.delete).toHaveBeenCalledWith('ohmynews-v9.1')
    expect(caches.delete).toHaveBeenCalledWith('ohmynews-api')
    expect(caches.delete).not.toHaveBeenCalledWith('i2a-runtime')
  })

  it('swallows unregister and cache deletion failures so app startup can continue', async () => {
    const registration = { unregister: vi.fn().mockRejectedValue(new Error('unregister failed')) }
    const serviceWorker = {
      getRegistrations: vi.fn().mockResolvedValue([registration]),
    }
    const caches = {
      keys: vi.fn().mockResolvedValue(['ohmynews-v9.1']),
      delete: vi.fn().mockRejectedValue(new Error('delete failed')),
    }
    setServiceWorker(serviceWorker)
    setCaches(caches)

    await expect(retireLegacyServiceWorker()).resolves.toBeUndefined()
    expect(registration.unregister).toHaveBeenCalledTimes(1)
    expect(caches.delete).toHaveBeenCalledWith('ohmynews-v9.1')
  })
})
