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
      <PageHeader title={t('onboarding.personalize_done_title', '阶段二完成')} subtitle={t('onboarding.personalize_done_subtitle', '系统已完成个性化设置，可继续完善成员和宠物档案')} />
      <div className="card onboarding-summary-card">
        <p>{t('onboarding.step_home_profile', '家庭整体档案：')}{status.steps.home_profile.configured ? t('system_status.completed', '已完成') : t('system_status.uncompleted', '未完成')}</p>
        <p>
          {t('onboarding.step_camera_notes', '摄像头注意事项：')}{status.steps.camera_notes.configured_count}/{status.steps.camera_notes.total_count}
        </p>
        <p>{t('onboarding.step_system_style', '系统风格：')}{status.steps.system_style.configured ? t('onboarding.status_configured', '已设置') : t('onboarding.status_not_configured', '未设置')}</p>
        <p className="text-muted">{t('onboarding.current_status', '当前状态：')}{status.full_ready ? t('system_status.full_ready', '完整可运行') : t('onboarding.status_need_more', '仍有待完善项')}</p>
        <div className="onboarding-actions">
          <button
            onClick={() => {
              resetOnboardingDraft()
              navigate('/dashboard')
            }}
          >
            {t('onboarding.enter_dashboard', '进入仪表盘')}
          </button>
          <button className="ghost" onClick={() => navigate('/home-profile/members')}>
            {t('onboarding.go_members', '去完善家庭成员')}
          </button>
          <button className="ghost" onClick={() => navigate('/home-profile/pets')}>
            {t('onboarding.go_pets', '去完善宠物档案')}
          </button>
        </div>
      </div>
    </div>
  )
}
