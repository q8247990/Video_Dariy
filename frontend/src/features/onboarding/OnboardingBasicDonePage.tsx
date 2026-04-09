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
    return <LoadingBlock text={t('onboarding.loading_status', '加载状态中')} />
  }

  if (query.error) {
    return <ApiErrorAlert message={(query.error as Error).message} />
  }

  const status = query.data
  if (!status) {
    return <ApiErrorAlert message={t('onboarding.error_no_status', '未获取到引导状态')} />
  }

  return (
    <div>
      <PageHeader title={t('onboarding.basic_done_title', '阶段一完成')} subtitle={t('onboarding.basic_done_subtitle', '你已完成基础配置步骤')} />
      <div className="card onboarding-summary-card">
        <p>{t('onboarding.step_video', '视频源：')}{status.steps.video_source.validated ? t('onboarding.status_validated', '已校验通过') : t('onboarding.status_uncompleted', '未完成')}</p>
        <p>{t('onboarding.step_provider', 'Provider：')}{status.steps.provider.tested ? t('onboarding.status_tested', '已测试通过') : t('onboarding.status_uncompleted', '未完成')}</p>
        <p>{t('onboarding.step_summary_time', '日报时间：')}{status.steps.daily_summary.configured ? t('onboarding.status_configured', '已配置') : t('onboarding.status_not_configured', '未配置')}</p>
        <p className="text-muted">{t('onboarding.current_status', '当前状态：')}{status.basic_ready ? t('onboarding.status_basic_ready', '基础可运行') : t('onboarding.status_not_basic_ready', '未完成基础配置')}</p>
        <div className="onboarding-actions">
          <button onClick={() => navigate('/dashboard')}>{t('onboarding.enter_system', '进入系统')}</button>
          <button className="ghost" onClick={() => navigate('/onboarding/personalize/profile')}>
            {t('onboarding.continue_personalize', '继续个性化配置')}
          </button>
        </div>
      </div>
    </div>
  )
}
