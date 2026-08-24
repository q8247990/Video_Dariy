import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { PageHeader } from '../../components/common/PageHeader'
import { getOnboardingStatus } from './api'
import { resetOnboardingDraft } from './state'

export function OnboardingPersonalizeDonePage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const query = useQuery({
    queryKey: ['onboarding-status'],
    queryFn: getOnboardingStatus,
  })

  if (query.isLoading) {
    return <LoadingBlock text={t('onboarding.loading_status')} />
  }

  if (query.error) {
    return <ApiErrorAlert message={(query.error as Error).message} />
  }

  const status = query.data
  if (!status) {
    return <ApiErrorAlert message={t('onboarding.error_no_status')} />
  }

  return (
    <div>
      <PageHeader
        title={t('onboarding.step_personalize_done_title')}
        subtitle={t('onboarding.step_personalize_done_subtitle')}
      />
      <div className="card onboarding-summary-card">
        <p>
          {t('onboarding.step_personalize_done_home_profile')}
          {status.steps.home_profile.configured ? t('system_status.completed') : t('system_status.uncompleted')}
        </p>
        <p>
          {t('onboarding.step_personalize_done_camera_notes')}
          {status.steps.camera_notes.configured_count}/{status.steps.camera_notes.total_count}
        </p>
        <p>
          {t('onboarding.step_personalize_done_system_style')}
          {status.steps.system_style.configured
            ? t('onboarding.step_basic_done_summary_time_configured')
            : t('onboarding.step_basic_done_summary_time_not_configured')}
        </p>
        <p className="text-muted">
          {t('onboarding.step_personalize_done_status_label')}
          {status.full_ready
            ? t('onboarding.step_personalize_done_full_ready')
            : t('onboarding.step_personalize_done_remaining')}
        </p>
        <div className="onboarding-actions">
          <button
            onClick={() => {
              resetOnboardingDraft()
              navigate('/dashboard')
            }}
          >
            {t('onboarding.step_personalize_done_enter_dashboard')}
          </button>
          <button className="ghost" onClick={() => navigate('/home-profile/members')}>
            {t('onboarding.step_personalize_done_go_members')}
          </button>
          <button className="ghost" onClick={() => navigate('/home-profile/pets')}>
            {t('onboarding.step_personalize_done_go_pets')}
          </button>
        </div>
      </div>
    </div>
  )
}
