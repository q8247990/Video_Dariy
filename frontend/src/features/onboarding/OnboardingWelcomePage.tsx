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
      <PageHeader
        title={t('onboarding.welcome_title')}
        subtitle={t('onboarding.welcome_subtitle')}
      />

      <div className="card onboarding-welcome">
        <h3>{t('onboarding.step_1_title')}</h3>
        <p>{t('onboarding.welcome_phase1')}</p>
        <p>{t('onboarding.welcome_phase2')}</p>
        <div className="onboarding-actions">
          <button onClick={() => navigate('/onboarding/basic/video')}>{t('onboarding.start_config')}</button>
          <button className="ghost" onClick={() => setShowConfirm(true)}>
            {t('onboarding.skip_onboarding')}
          </button>
        </div>
      </div>

      {showConfirm ? (
        <div className="dialog-mask" onClick={() => setShowConfirm(false)}>
          <div className="dialog" onClick={(event) => event.stopPropagation()}>
            <h3>{t('onboarding.confirm_skip_title')}</h3>
            <p className="text-muted">{t('onboarding.confirm_skip_desc')}</p>
            <div className="dialog-actions">
              <button className="ghost" onClick={() => setShowConfirm(false)}>
                {t('common.cancel')}
              </button>
              <button onClick={() => navigate('/dashboard')}>{t('onboarding.confirm_skip')}</button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
