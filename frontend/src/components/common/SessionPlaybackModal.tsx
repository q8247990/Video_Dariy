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
          <h3>{t('sessions.playback_title', { id: sessionId })}</h3>
          <button className="ghost" onClick={onClose}>
            {t('sessions.playback_close')}
          </button>
        </div>

        {playbackQuery.isLoading ? <LoadingBlock text={t('sessions.playback_loading')} /> : null}
        {playbackQuery.error ? <ApiErrorAlert message={(playbackQuery.error as Error).message} /> : null}

        {!playbackQuery.isLoading && !playbackQuery.error ? (
          <div className="playback-grid">
            {playbackQuery.data?.playback_url ? (
                <article className="playback-item">
                  <h4>{t('sessions.merged_playback')}</h4>
                  <HlsVideoPlayer
                    src={playbackQuery.data.playback_url}
                  />
                </article>
            ) : (
              <div className="empty-cell">{t('sessions.playback_empty')}</div>
            )}
          </div>
        ) : null}
      </div>
    </div>
  )
}
