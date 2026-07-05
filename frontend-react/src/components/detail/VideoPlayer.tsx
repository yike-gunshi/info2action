/**
 * v12.2 F50 视频播放器 (DetailPanel 子组件)
 * 容器固定 aspect-ratio 16/9 + max-h-360px, 不自动播放.
 * 设计规范: docs/DESIGN.md 模块 13.3
 *
 * Round 2 方案 B: mp4 走后端代理绕 Twitter CDN Referer 校验,
 * 封面走 ffmpeg 首帧缓存. 详见 src/routes/media.py.
 *
 * v12.3 F51 E2: timeupdate → store.asrCurrentTimeMs 驱动 TranscriptPanel 高亮;
 * 接收 window 'asr:seek' 事件做 video.currentTime 跳转.
 */
import React, { useEffect, useRef } from 'react'
import { useDetailStore } from '../../store/detailStore'

interface Props {
  mp4Url: string
  itemId: string
}

export function VideoPlayer({ mp4Url, itemId }: Props): React.ReactElement {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const setAsrCurrentTimeMs = useDetailStore((s) => s.setAsrCurrentTimeMs)

  const proxiedSrc = `/api/media/twitter-mp4/${itemId}`
  const poster = `/api/media/twitter-poster/${itemId}.jpg`

  // v12.3 E2: 监听 TranscriptPanel 的 seek 事件
  useEffect(() => {
    const onSeek = (ev: Event) => {
      const detail = (ev as CustomEvent<{ itemId: string; ms: number }>).detail
      if (!detail || detail.itemId !== itemId) return
      const v = videoRef.current
      if (!v) return
      v.currentTime = Math.max(0, detail.ms / 1000)
      // 不改变 pause 状态:播中继续播、停中继续停(PRD R10-S2)
    }
    window.addEventListener('asr:seek', onSeek as EventListener)
    return () => window.removeEventListener('asr:seek', onSeek as EventListener)
  }, [itemId])

  const onTimeUpdate: React.ReactEventHandler<HTMLVideoElement> = (e) => {
    setAsrCurrentTimeMs(Math.floor(e.currentTarget.currentTime * 1000))
  }

  return (
    <div className="w-full aspect-video max-h-[360px] sm:max-h-[300px] md:max-h-[360px] bg-black rounded-lg overflow-hidden mb-3">
      <video
        ref={videoRef}
        className="w-full h-full object-contain"
        controls
        preload="metadata"
        src={proxiedSrc}
        poster={poster}
        playsInline
        onTimeUpdate={onTimeUpdate}
        data-original-src={mp4Url}
      />
    </div>
  )
}
