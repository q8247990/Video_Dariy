import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ApiErrorAlert } from './ApiErrorAlert'
import { HlsVideoPlayer } from './HlsVideoPlayer'
import { LoadingBlock } from './LoadingBlock'
import { getSessionPlayback } from '../../features/sessions/api'

type SessionPlaybackModalProps = {
  sessionId: number | null
  open: boolean
  onClose: () => void
}

export function SessionPlaybackModal({ sessionId, open, onClose }: SessionPlaybackModalProps) {
  const { t } = useTranslation()
  const playbackQuery = useQuery({
    queryKey: ['session-playback', sessionId],
    queryFn: () => getSessionPlayback(sessionId as number),
    enabled: open && sessionId !== null,
  })

  if (!open || sessionId === null) {
    return null
  }

  return (
    <div className="dialog-mask" onClick={onClose}>
      <div className="dialog dialog-wide" onClick={(event) => event.stopPropagation()}>
        <div className="playback-head">
          <h3>{t('sessions.playback_title', 'Session #{{id}} 回放', { id: sessionId })}</h3>
          <button className="ghost" onClick={onClose}>
            {t('common.close', '关闭')}
          </button>
        </div>

        {playbackQuery.isLoading ? <LoadingBlock text={t('sessions.loading_playback', '加载回放列表中')} /> : null}
        {playbackQuery.error ? <ApiErrorAlert message={(playbackQuery.error as Error).message} /> : null}

        {!playbackQuery.isLoading && !playbackQuery.error ? (
          <div className="playback-grid">
            {playbackQuery.data?.playback_url ? (
                <article className="playback-item">
                  <h4>{t('sessions.hls_playback', '拼接回放')}</h4>
                  <HlsVideoPlayer
                    src={
                      playbackQuery.data.playback_url.startsWith('/api/v1')
                        ? playbackQuery.data.playback_url
                        : `/api/v1${playbackQuery.data.playback_url}`
                    }
                  />
                </article>
            ) : (
              <div className="empty-cell">{t('sessions.empty_playback', '当前 Session 暂无可播放文件')}</div>
            )}
          </div>
        ) : null}
      </div>
    </div>
  )
}
