import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { PageHeader } from '../../components/common/PageHeader'
import { getOnboardingStatus } from './api'
import { resetOnboardingDraft } from './state'

export function OnboardingPersonalizeDonePage() {
  const navigate = useNavigate()
  const query = useQuery({
    queryKey: ['onboarding-status'],
    queryFn: getOnboardingStatus,
  })

  if (query.isLoading) {
    return <LoadingBlock text="加载状态中" />
  }

  if (query.error) {
    return <ApiErrorAlert message={(query.error as Error).message} />
  }

  const status = query.data
  if (!status) {
    return <ApiErrorAlert message="未获取到引导状态" />
  }

  return (
    <div>
      <PageHeader title="阶段二完成" subtitle="系统已完成个性化设置，可继续完善成员和宠物档案" />
      <div className="card onboarding-summary-card">
        <p>家庭整体档案：{status.steps.home_profile.configured ? '已完成' : '未完成'}</p>
        <p>
          摄像头注意事项：{status.steps.camera_notes.configured_count}/{status.steps.camera_notes.total_count}
        </p>
        <p>系统风格：{status.steps.system_style.configured ? '已设置' : '未设置'}</p>
        <p className="text-muted">当前状态：{status.full_ready ? '完整可运行' : '仍有待完善项'}</p>
        <div className="onboarding-actions">
          <button
            onClick={() => {
              resetOnboardingDraft()
              navigate('/dashboard')
            }}
          >
            进入仪表盘
          </button>
          <button className="ghost" onClick={() => navigate('/home-profile/members')}>
            去完善家庭成员
          </button>
          <button className="ghost" onClick={() => navigate('/home-profile/pets')}>
            去完善宠物档案
          </button>
        </div>
      </div>
    </div>
  )
}
