import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { PageHeader } from '../../components/common/PageHeader'
import { getVideoSourcesForOnboarding, updateVideoSourceDescription } from './api'
import { useOnboardingDraftStore } from './state'

export function OnboardingPersonalizeCameraNotesPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const hydrate = useOnboardingDraftStore((state) => state.hydrate)
  const cameraNotes = useOnboardingDraftStore((state) => state.cameraNotes)
  const setCameraNote = useOnboardingDraftStore((state) => state.setCameraNote)
  const [message, setMessage] = useState('')

  useEffect(() => {
    hydrate()
  }, [hydrate])

  const query = useQuery({
    queryKey: ['onboarding-video-sources'],
    queryFn: getVideoSourcesForOnboarding,
  })

  const mutation = useMutation({
    mutationFn: async () => {
      const rows = query.data ?? []
      const updates = rows.map((row) => {
        const text = cameraNotes[row.id] ?? row.description ?? ''
        return updateVideoSourceDescription(row.id, text.trim())
      })
      await Promise.all(updates)
    },
    onSuccess: () => {
      setMessage(t('onboarding.camera_notes_save_success'))
      navigate('/onboarding/personalize/style')
    },
    onError: (error) => setMessage((error as Error).message),
  })

  if (query.isLoading) {
    return <LoadingBlock text={t('video_sources.loading')} />
  }
  if (query.error) {
    return <ApiErrorAlert message={(query.error as Error).message} />
  }

  const rows = query.data ?? []

  return (
    <div>
      <PageHeader
        title={t('onboarding.step_personalize_camera_notes_title')}
        subtitle={t('onboarding.step_personalize_camera_notes_subtitle')}
      />
      <div className="card config-form">
        {message ? <div className="api-ok">{message}</div> : null}
        {rows.length === 0 ? <p className="text-muted">{t('onboarding.camera_notes_no_sources')}</p> : null}
        {rows.map((row) => (
          <label key={row.id}>
            {row.source_name}（{row.camera_name}）
            <textarea
              value={cameraNotes[row.id] ?? row.description ?? ''}
              onChange={(event) => setCameraNote(row.id, event.target.value)}
              placeholder={t('onboarding.camera_notes_placeholder')}
            />
          </label>
        ))}

        <div className="onboarding-actions">
          <button className="ghost" onClick={() => navigate('/onboarding/personalize/profile')}>
            {t('onboarding.step_prev')}
          </button>
          <button className="ghost" onClick={() => navigate('/onboarding/personalize/style')}>
            {t('onboarding.camera_notes_skip_all')}
          </button>
          <button onClick={() => mutation.mutate()} disabled={mutation.isPending}>
            {mutation.isPending ? t('onboarding.saving') : t('onboarding.save_next')}
          </button>
        </div>
      </div>
    </div>
  )
}
