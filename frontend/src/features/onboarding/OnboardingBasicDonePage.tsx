import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { PageHeader } from '../../components/common/PageHeader'
import { getOnboardingStatus } from './api'

export function OnboardingBasicDonePage() {
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
      <PageHeader title="阶段一完成" subtitle="你已完成基础配置步骤" />
      <div className="card onboarding-summary-card">
        <p>视频源：{status.steps.video_source.validated ? '已校验通过' : '未完成'}</p>
        <p>Provider：{status.steps.provider.tested ? '已测试通过' : '未完成'}</p>
        <p>日报时间：{status.steps.daily_summary.configured ? '已配置' : '未配置'}</p>
        <p className="text-muted">当前状态：{status.basic_ready ? '基础可运行' : '未完成基础配置'}</p>
        <div className="onboarding-actions">
          <button onClick={() => navigate('/dashboard')}>进入系统</button>
          <button className="ghost" onClick={() => navigate('/onboarding/personalize/profile')}>
            继续个性化配置
          </button>
        </div>
      </div>
    </div>
  )
}
