import { useEffect, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { PageHeader } from '../../components/common/PageHeader'
import { saveDailySummarySchedule } from './api'
import { useOnboardingDraftStore } from './state'

export function OnboardingBasicSummaryTimePage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const summary = useOnboardingDraftStore((state) => state.summary)
  const setSummary = useOnboardingDraftStore((state) => state.setSummary)
  const hydrate = useOnboardingDraftStore((state) => state.hydrate)
  const [message, setMessage] = useState('')

  useEffect(() => {
    hydrate()
  }, [hydrate])

  const mutation = useMutation({
    mutationFn: saveDailySummarySchedule,
    onSuccess: () => {
      setMessage(t('onboarding.summary_time_saved', '日报时间已保存'))
      navigate('/onboarding/basic/done')
    },
    onError: (error) => setMessage((error as Error).message),
  })

  return (
    <div>
      <PageHeader title={t('onboarding.summary_time_title', '阶段一 · 设置日报时间')} subtitle={t('onboarding.summary_time_subtitle', '默认每天上午 10:00 生成昨天日报')} />
      <div className="card config-form">
        {message ? <div className="api-ok">{message}</div> : null}
        <label>
          {t('onboarding.field_summary_time', '日报生成时间（HH:mm）')}
          <input
            value={summary.daily_summary_schedule}
            onChange={(event) => setSummary({ daily_summary_schedule: event.target.value })}
            placeholder="10:00"
          />
        </label>

        <div className="onboarding-actions">
          <button className="ghost" onClick={() => navigate('/onboarding/basic/provider')}>
            {t('common.prev_step', '上一步')}
          </button>
          <button
            className="ghost"
            onClick={() => {
              setSummary({ daily_summary_schedule: '10:00' })
              mutation.mutate('10:00')
            }}
            disabled={mutation.isPending}
          >
            {t('onboarding.use_default', '使用默认值')}
          </button>
          <button
            onClick={() => mutation.mutate(summary.daily_summary_schedule)}
            disabled={!summary.daily_summary_schedule || mutation.isPending}
          >
            {mutation.isPending ? t('common.saving', '保存中...') : t('common.save_and_next', '保存并下一步')}
          </button>
        </div>
      </div>
    </div>
  )
}
