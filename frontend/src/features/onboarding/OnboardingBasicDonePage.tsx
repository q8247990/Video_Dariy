import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { PageHeader } from '../../components/common/PageHeader'
import { getOnboardingStatus } from './api'

export function OnboardingBasicDonePage() {
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
        title={t('onboarding.step_basic_done_title')}
        subtitle={t('onboarding.step_basic_done_subtitle')}
      />
      <div className="card onboarding-summary-card">
        <p>
          {t('onboarding.step_basic_done_video_source_passed')}
          {status.steps.video_source.validated
            ? ''
            : `（${t('onboarding.step_basic_done_video_source_uncompleted')}）`}
        </p>
        <p>
          {t('onboarding.step_basic_done_provider_passed')}
          {status.steps.provider.tested
            ? ''
            : `（${t('onboarding.step_basic_done_video_source_uncompleted')}）`}
        </p>
        <p>
          {status.steps.daily_summary.configured
            ? t('onboarding.step_basic_done_summary_time_configured')
            : t('onboarding.step_basic_done_summary_time_not_configured')}
        </p>
        <p className="text-muted">
          {t('onboarding.step_basic_done_status_label')}
          {status.basic_ready
            ? t('onboarding.step_basic_done_status_ready')
            : t('onboarding.step_basic_done_status_not_ready')}
        </p>
        <div className="onboarding-actions">
          <button onClick={() => navigate('/dashboard')}>{t('onboarding.step_basic_done_enter_system')}</button>
          <button className="ghost" onClick={() => navigate('/onboarding/personalize/profile')}>
            {t('onboarding.step_basic_done_continue_personalize')}
          </button>
        </div>
      </div>
    </div>
  )
}
