import { useEffect, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { PageHeader } from '../../components/common/PageHeader'
import { systemStyleLabel } from '../home-profile/labels'
import { saveHomeProfile } from './api'
import { useOnboardingDraftStore } from './state'

const STYLE_OPTIONS = ['concise_summary', 'family_companion', 'focus_alert'] as const

export function OnboardingPersonalizeStylePage() {
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
      setMessage('系统风格设置已保存')
      navigate('/onboarding/personalize/done')
    },
    onError: (error) => setMessage((error as Error).message),
  })

  return (
    <div>
      <PageHeader title="阶段二 · 系统风格与系统名称" subtitle="定义系统说话风格与称呼" />
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
          系统名称
          <input
            value={homeProfile.assistant_name}
            onChange={(event) => setHomeProfile({ assistant_name: event.target.value })}
            placeholder="家庭助手"
          />
        </label>

        <label>
          风格补充偏好（可选）
          <textarea
            value={homeProfile.style_preference_text}
            onChange={(event) => setHomeProfile({ style_preference_text: event.target.value })}
          />
        </label>

        <div className="onboarding-actions">
          <button className="ghost" onClick={() => navigate('/onboarding/personalize/camera-notes')}>
            上一步
          </button>
          <button
            onClick={() =>
              mutation.mutate({
                home_name: homeProfile.home_name.trim() || '我的家庭',
                family_tags: homeProfile.family_tags,
                focus_points: homeProfile.focus_points,
                system_style: homeProfile.system_style,
                style_preference_text: homeProfile.style_preference_text.trim(),
                assistant_name: homeProfile.assistant_name.trim() || '家庭助手',
                home_note: homeProfile.home_note.trim(),
              })
            }
            disabled={mutation.isPending}
          >
            {mutation.isPending ? '保存中...' : '保存并下一步'}
          </button>
        </div>
      </div>
    </div>
  )
}
