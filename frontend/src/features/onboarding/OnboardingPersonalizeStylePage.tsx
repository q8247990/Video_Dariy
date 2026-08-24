import { useEffect, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { PageHeader } from '../../components/common/PageHeader'
import { systemStyleLabel } from '../home-profile/labels'
import { saveHomeProfile } from './api'
import { useOnboardingDraftStore } from './state'

const STYLE_OPTIONS = ['concise_summary', 'family_companion', 'focus_alert'] as const

export function OnboardingPersonalizeStylePage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const homeProfile = useOnboardingDraftStore((state) => state.homeProfile)
  const setHomeProfile = useOnboardingDraftStore((state) => state.setHomeProfile)
  const hydrate = useOnboardingDraftStore((state) => state.hydrate)
  const [message, setMessage] = useState('')

  useEffect(() => {
    hydrate()
  }, [hydrate])

  const mutation = useMutation({
    mutationFn: saveHomeProfile,
    onSuccess: () => {
      setMessage(t('onboarding.system_style_save_success'))
      navigate('/onboarding/personalize/done')
    },
    onError: (error) => setMessage((error as Error).message),
  })

  return (
    <div>
      <PageHeader
        title={t('onboarding.step_personalize_style_title')}
        subtitle={t('onboarding.step_personalize_style_subtitle')}
      />
      <div className="card config-form">
        {message ? <div className="api-ok">{message}</div> : null}

        <div className="onboarding-style-grid">
          {STYLE_OPTIONS.map((style) => (
            <button
              type="button"
              key={style}
              className={homeProfile.system_style === style ? 'ghost onboarding-style active' : 'ghost onboarding-style'}
              onClick={() => setHomeProfile({ system_style: style })}
            >
              {systemStyleLabel(style)}
            </button>
          ))}
        </div>

        <label>
          {t('onboarding.form_assistant_name')}
          <input
            value={homeProfile.assistant_name}
            onChange={(event) => setHomeProfile({ assistant_name: event.target.value })}
            placeholder={t('onboarding.form_assistant_name_placeholder')}
          />
        </label>

        <label>
          {t('onboarding.step_personalize_style_label')}
          <textarea
            value={homeProfile.style_preference_text}
            onChange={(event) => setHomeProfile({ style_preference_text: event.target.value })}
          />
        </label>

        <div className="onboarding-actions">
          <button className="ghost" onClick={() => navigate('/onboarding/personalize/camera-notes')}>
            {t('onboarding.step_prev')}
          </button>
          <button
            onClick={() =>
              mutation.mutate({
                home_name: homeProfile.home_name.trim() || t('onboarding.system_style_default_home_name'),
                family_tags: homeProfile.family_tags,
                focus_points: homeProfile.focus_points,
                system_style: homeProfile.system_style,
                style_preference_text: homeProfile.style_preference_text.trim(),
                assistant_name: homeProfile.assistant_name.trim() || t('onboarding.system_style_default_assistant_name'),
                home_note: homeProfile.home_note.trim(),
              })
            }
            disabled={mutation.isPending}
          >
            {mutation.isPending ? t('onboarding.saving') : t('onboarding.save_next')}
          </button>
        </div>
      </div>
    </div>
  )
}
