import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { PageHeader } from '../../components/common/PageHeader'

export function OnboardingWelcomePage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [showConfirm, setShowConfirm] = useState(false)

  return (
    <div>
      <PageHeader title={t('onboarding.welcome', '初始化引导')} subtitle={t('onboarding.subtitle', '先完成基础接入，再逐步个性化配置')} />

      <div className="card onboarding-welcome">
        <h3>{t('onboarding.step_1_title', '3~5 分钟完成基础可运行')}</h3>
        <p>{t('onboarding.step_1_desc', '阶段一会完成视频接入、Provider 配置和日报时间设置。')}</p>
        <p>{t('onboarding.step_2_desc', '阶段二会补充家庭语境，让系统输出更贴近你的家庭习惯。')}</p>
        <div className="onboarding-actions">
          <button onClick={() => navigate('/onboarding/basic/video')}>{t('onboarding.start_config', '开始配置')}</button>
          <button className="ghost" onClick={() => setShowConfirm(true)}>
            {t('onboarding.skip_onboarding', '跳过引导，进入系统')}
          </button>
        </div>
      </div>

      {showConfirm ? (
        <div className="dialog-mask" onClick={() => setShowConfirm(false)}>
          <div className="dialog" onClick={(event) => event.stopPropagation()}>
            <h3>{t('onboarding.confirm_skip_title', '确认跳过引导？')}</h3>
            <p className="text-muted">{t('onboarding.confirm_skip_desc', '跳过后仍可在系统中手动配置；未完成视频源或 Provider 时，分析、日报和问答能力不可用。')}</p>
            <div className="dialog-actions">
              <button className="ghost" onClick={() => setShowConfirm(false)}>
                {t('common.cancel', '取消')}
              </button>
              <button onClick={() => navigate('/dashboard')}>{t('onboarding.confirm_skip', '确认跳过')}</button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
