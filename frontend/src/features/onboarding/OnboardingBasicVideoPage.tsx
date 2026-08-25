import { useEffect, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { PageHeader } from '../../components/common/PageHeader'
import type { VideoSourceCreate } from '../../types/api'
import { createOnboardingVideoSource, testVideoSource, validateVideoPath } from './api'
import { useOnboardingDraftStore } from './state'

export function OnboardingBasicVideoPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const video = useOnboardingDraftStore((state) => state.video)
  const setVideo = useOnboardingDraftStore((state) => state.setVideo)
  const hydrate = useOnboardingDraftStore((state) => state.hydrate)
  const [message, setMessage] = useState('')

  useEffect(() => {
    hydrate()
  }, [hydrate])

  const validateMutation = useMutation({
    mutationFn: validateVideoPath,
    onSuccess: (data) => {
      setVideo({ validated: data.valid })
      setMessage(data.message)
    },
    onError: (error) => {
      setVideo({ validated: false })
      setMessage((error as Error).message)
    },
  })

  const createMutation = useMutation({
    mutationFn: createOnboardingVideoSource,
    onSuccess: async (data) => {
      setVideo({ source_id: data.id })
      try {
        const testResult = await testVideoSource(data.id)
        setVideo({ validated: testResult.success })
        setMessage(testResult.message)
        if (testResult.success) {
          navigate('/onboarding/basic/provider')
        }
      } catch (error: unknown) {
        setVideo({ validated: false })
        setMessage(error instanceof Error ? error.message : t('common.load_failed_retry'))
      }
    },
    onError: (error) => {
      setMessage((error as Error).message)
    },
  })

  const onSubmit = () => {
    const payload: VideoSourceCreate = {
      source_name: video.source_name.trim(),
      camera_name: video.camera_name.trim(),
      location_name: video.location_name.trim(),
      description: '',
      prompt_text: '',
      source_type: 'local_directory',
      config_json: { root_path: video.root_path.trim() },
      enabled: true,
    }
    createMutation.mutate(payload)
  }

  const canSubmit =
    video.source_name.trim() &&
    video.camera_name.trim() &&
    video.location_name.trim() &&
    video.root_path.trim() &&
    video.validated

  return (
    <div>
      <PageHeader
        title={t('onboarding.step_basic_video_title')}
        subtitle={t('onboarding.step_basic_video_subtitle')}
      />
      <div className="card config-form">
        {message ? <div className={video.validated ? 'api-ok' : 'api-error'}>{message}</div> : null}
        <label>
          {t('onboarding.form_source_name')}
          <input
            value={video.source_name}
            onChange={(event) => setVideo({ source_name: event.target.value, validated: false })}
          />
        </label>
        <label>
          {t('onboarding.form_camera_name')}
          <input
            value={video.camera_name}
            onChange={(event) => setVideo({ camera_name: event.target.value, validated: false })}
          />
        </label>
        <label>
          {t('onboarding.form_location_name')}
          <input
            value={video.location_name}
            onChange={(event) => setVideo({ location_name: event.target.value, validated: false })}
          />
        </label>
        <label>
          {t('onboarding.form_root_path')}
          <input
            value={video.root_path}
            onChange={(event) => setVideo({ root_path: event.target.value, validated: false })}
            placeholder="/data/videos"
          />
        </label>

        <div className="onboarding-actions">
          <button
            className="ghost"
            onClick={() => validateMutation.mutate(video.root_path.trim())}
            disabled={!video.root_path.trim() || validateMutation.isPending}
          >
            {validateMutation.isPending ? t('onboarding.verify_pending') : t('onboarding.verify_path')}
          </button>
          <button onClick={onSubmit} disabled={!canSubmit || createMutation.isPending}>
            {createMutation.isPending ? t('onboarding.saving') : t('onboarding.save_next')}
          </button>
        </div>
      </div>
    </div>
  )
}
