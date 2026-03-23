import { useEffect, useRef } from 'react'

type HlsVideoPlayerProps = {
  src: string
}

export function HlsVideoPlayer({ src }: HlsVideoPlayerProps) {
  const videoRef = useRef<HTMLVideoElement | null>(null)

  useEffect(() => {
    const video = videoRef.current
    if (!video) {
      return
    }

    const isHlsSource = src.includes('.m3u8')
    if (!isHlsSource) {
      video.src = src
      return
    }

    if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = src
      return
    }

    let disposed = false
    let cleanup: (() => void) | undefined

    void import('hls.js')
      .then(({ default: Hls }) => {
        if (disposed) {
          return
        }
        if (!Hls.isSupported()) {
          video.src = src
          return
        }

        const hls = new Hls()
        hls.loadSource(src)
        hls.attachMedia(video)
        cleanup = () => hls.destroy()
      })
      .catch(() => {
        video.src = src
      })

    return () => {
      disposed = true
      if (cleanup) {
        cleanup()
      }
    }
  }, [src])

  return <video ref={videoRef} controls preload="metadata" />
}
