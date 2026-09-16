import { useEffect, useRef, useState } from 'react'
import { getHighlightsReadingProgress, putHighlightsReadingProgress, type HighlightsReadingProgress } from '../lib/api'
import { useAuthStore } from '../store/authStore'

const LOCAL_KEY = 'highlights-reading-progress'

function localProgress(value: string | null): HighlightsReadingProgress | null {
  if (!value) return null
  try {
    const parsed = JSON.parse(value) as Partial<HighlightsReadingProgress>
    const clusterId = Number(parsed.cluster_id)
    if (!Number.isFinite(clusterId) || !parsed.updated_at) return null
    return {
      cluster_id: clusterId,
      resolved_cluster_id: Number(parsed.resolved_cluster_id) || clusterId,
      anchor_sort_at: parsed.anchor_sort_at || parsed.updated_at,
      updated_at: parsed.updated_at,
      cursor: parsed.cursor ?? null,
      resolution: parsed.resolution || 'exact',
    }
  } catch {
    return null
  }
}

export function useHighlightsReadingProgress(root: React.RefObject<HTMLElement | null>, observeKey = '') {
  const user = useAuthStore((s) => s.user)
  const latestId = useRef<number | null>(null)
  const [progress, setProgress] = useState<HighlightsReadingProgress | null>(null)

  useEffect(() => {
    let cancelled = false
    if (user) {
      void getHighlightsReadingProgress()
        .then((result) => { if (!cancelled) setProgress(result.progress) })
        .catch(() => { if (!cancelled) setProgress(null) })
      return () => { cancelled = true }
    }
    setProgress(localProgress(localStorage.getItem(LOCAL_KEY)))
    return () => { cancelled = true }
  }, [user])

  useEffect(() => {
    const container = root.current
    if (!container || typeof IntersectionObserver === 'undefined') return
    let timer: ReturnType<typeof setTimeout> | null = null
    const save = (id: number, keepalive = false) => {
      latestId.current = id
      if (!user) {
        const updatedAt = new Date().toISOString()
        const local = {
          cluster_id: id,
          resolved_cluster_id: id,
          anchor_sort_at: updatedAt,
          updated_at: updatedAt,
          cursor: null,
          resolution: 'exact',
        } satisfies HighlightsReadingProgress
        localStorage.setItem(LOCAL_KEY, JSON.stringify(local))
        return
      }
      void putHighlightsReadingProgress(id, keepalive)
    }
    const visible = new Set<HTMLElement>()
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        const target = entry.target as HTMLElement
        if (entry.isIntersecting) visible.add(target)
        else visible.delete(target)
      })
      const first = [...visible]
        .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)[0]
      const id = Number(first?.dataset.clusterId)
      if (!Number.isFinite(id)) return
      latestId.current = id
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => save(id), 500)
    }, { threshold: 0 })
    container.querySelectorAll<HTMLElement>('[data-cluster-id]').forEach((node) => observer.observe(node))
    const onPageHide = () => { if (latestId.current != null) save(latestId.current, true) }
    window.addEventListener('pagehide', onPageHide)
    return () => { if (timer) clearTimeout(timer); observer.disconnect(); window.removeEventListener('pagehide', onPageHide) }
  }, [observeKey, root, user])
  return progress
}
