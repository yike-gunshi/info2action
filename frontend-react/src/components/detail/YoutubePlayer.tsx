/**
 * v13.0 BF-0419-20 YouTube iframe player (DetailPanel 子组件)
 *
 * 与 VideoPlayer 等价的交互契约:
 * - timeupdate → store.asrCurrentTimeMs 驱动 TranscriptPanel 高亮
 * - 接收 window 'asr:seek' 事件做 player.seekTo 跳转
 *
 * 走 YouTube IFrame Player API 做编程控制:
 * https://developers.google.com/youtube/iframe_api_reference
 */
import React, { useEffect, useRef, useState } from 'react'
import { useDetailStore } from '../../store/detailStore'

interface Props {
  videoId: string
  itemId: string
}

declare global {
  interface Window {
    YT?: {
      Player: new (el: HTMLElement, opts: Record<string, unknown>) => YtPlayerInstance
      PlayerState: { PLAYING: number; PAUSED: number; ENDED: number }
    }
    onYouTubeIframeAPIReady?: () => void
  }
}

interface YtPlayerInstance {
  seekTo: (seconds: number, allowSeekAhead?: boolean) => void
  getCurrentTime: () => number
  getPlayerState: () => number
  destroy: () => void
}

let apiLoadingPromise: Promise<void> | null = null

function loadYouTubeApi(): Promise<void> {
  if (window.YT?.Player) return Promise.resolve()
  if (apiLoadingPromise) return apiLoadingPromise
  apiLoadingPromise = new Promise<void>((resolve, reject) => {
    const prev = window.onYouTubeIframeAPIReady
    window.onYouTubeIframeAPIReady = () => {
      if (prev) prev()
      resolve()
    }
    const tag = document.createElement('script')
    tag.src = 'https://www.youtube.com/iframe_api'
    tag.async = true
    tag.onerror = () => {
      // Review 修:脚本加载失败(网络/CSP/adblock),重置 promise 让下次重试,外层降级
      apiLoadingPromise = null
      reject(new Error('YouTube IFrame API 加载失败'))
    }
    document.head.appendChild(tag)
  })
  return apiLoadingPromise
}

export function YoutubePlayer({ videoId, itemId }: Props): React.ReactElement {
  const mountRef = useRef<HTMLDivElement | null>(null)
  const playerRef = useRef<YtPlayerInstance | null>(null)
  const pollTimerRef = useRef<number | null>(null)
  const setAsrCurrentTimeMs = useDetailStore((s) => s.setAsrCurrentTimeMs)
  const [apiFailed, setApiFailed] = useState(false)

  useEffect(() => {
    let cancelled = false

    loadYouTubeApi().then(() => {
      if (cancelled || !mountRef.current || !window.YT?.Player) return
      const player = new window.YT.Player(mountRef.current, {
        videoId,
        playerVars: {
          playsinline: 1,
          rel: 0,
          modestbranding: 1,
        },
        events: {
          onStateChange: (ev: { data: number }) => {
            const PLAYING = window.YT?.PlayerState.PLAYING ?? 1
            if (ev.data === PLAYING) {
              if (pollTimerRef.current != null) return
              pollTimerRef.current = window.setInterval(() => {
                try {
                  const t = player.getCurrentTime()
                  setAsrCurrentTimeMs(Math.floor(t * 1000))
                } catch { /* ignore */ }
              }, 250)
            } else {
              if (pollTimerRef.current != null) {
                window.clearInterval(pollTimerRef.current)
                pollTimerRef.current = null
              }
            }
          },
        },
      })
      playerRef.current = player
    }).catch(() => {
      if (!cancelled) setApiFailed(true)
    })

    return () => {
      cancelled = true
      if (pollTimerRef.current != null) {
        window.clearInterval(pollTimerRef.current)
        pollTimerRef.current = null
      }
      try { playerRef.current?.destroy() } catch { /* ignore */ }
      playerRef.current = null
    }
  }, [videoId, setAsrCurrentTimeMs])

  // 监听 TranscriptPanel 的 seek 事件
  useEffect(() => {
    const onSeek = (ev: Event) => {
      const detail = (ev as CustomEvent<{ itemId: string; ms: number }>).detail
      if (!detail || detail.itemId !== itemId) return
      try {
        playerRef.current?.seekTo(Math.max(0, detail.ms / 1000), true)
      } catch { /* ignore */ }
    }
    window.addEventListener('asr:seek', onSeek as EventListener)
    return () => window.removeEventListener('asr:seek', onSeek as EventListener)
  }, [itemId])

  if (apiFailed) {
    return (
      <div className="w-full aspect-video max-h-[360px] sm:max-h-[300px] md:max-h-[360px] bg-muted rounded-lg overflow-hidden mb-3 flex flex-col items-center justify-center gap-2">
        <p className="text-sm text-muted-foreground">YouTube 播放器加载失败</p>
        <a
          href={`https://www.youtube.com/watch?v=${videoId}`}
          target="_blank"
          rel="noopener noreferrer"
          className="text-sm text-primary hover:underline"
        >
          在 YouTube 打开视频 ↗
        </a>
      </div>
    )
  }

  return (
    <div className="w-full aspect-video max-h-[360px] sm:max-h-[300px] md:max-h-[360px] bg-black rounded-lg overflow-hidden mb-3">
      <div ref={mountRef} className="w-full h-full" />
    </div>
  )
}
