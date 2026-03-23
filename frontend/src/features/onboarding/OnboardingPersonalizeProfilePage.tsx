import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { PageHeader } from '../../components/common/PageHeader'
import { familyTagLabel, focusPointLabel } from '../home-profile/labels'
import { getHomeOptions, getHomeProfile, saveHomeProfile } from './api'
import { useOnboardingDraftStore } from './state'

function toggleValue(values: string[], value: string): string[] {
  if (values.includes(value)) {
    return values.filter((item) => item !== value)
  }
  return [...values, value]
}

export function OnboardingPersonalizeProfilePage() {
  const navigate = useNavigate()
  const homeProfile = useOnboardingDraftStore((state) => state.homeProfile)
  const setHomeProfile = useOnboardingDraftStore((state) => state.setHomeProfile)
  const hydrate = useOnboardingDraftStore((state) => state.hydrate)
  const [message, setMessage] = useState('')

  useEffect(() => {
    hydrate()
  }, [hydrate])

  const profileQuery = useQuery({
    queryKey: ['onboarding-home-profile'],
    queryFn: getHomeProfile,
  })
  const optionsQuery = useQuery({
    queryKey: ['onboarding-home-options'],
    queryFn: getHomeOptions,
  })

  useEffect(() => {
    if (profileQuery.data) {
      setHomeProfile({
        home_name: profileQuery.data.home_name,
        family_tags: profileQuery.data.family_tags,
        focus_points: profileQuery.data.focus_points,
        home_note: profileQuery.data.home_note ?? '',
        system_style: profileQuery.data.system_style,
        assistant_name: profileQuery.data.assistant_name,
        style_preference_text: profileQuery.data.style_preference_text ?? '',
      })
    }
  }, [profileQuery.data, setHomeProfile])

  const saveMutation = useMutation({
    mutationFn: saveHomeProfile,
    onSuccess: () => {
      setMessage('家庭整体档案已保存')
      navigate('/onboarding/personalize/camera-notes')
    },
    onError: (error) => setMessage((error as Error).message),
  })

  if (profileQuery.isLoading || optionsQuery.isLoading) {
    return <LoadingBlock text="加载家庭档案中" />
  }
  if (profileQuery.error) {
    return <ApiErrorAlert message={(profileQuery.error as Error).message} />
  }
  if (optionsQuery.error) {
    return <ApiErrorAlert message={(optionsQuery.error as Error).message} />
  }

  const options = optionsQuery.data
  if (!options) {
    return <ApiErrorAlert message="选项加载失败" />
  }

  const canSave = homeProfile.home_name.trim().length > 0

  return (
    <div>
      <PageHeader title="阶段二 · 家庭整体档案" subtitle="这一步让系统更懂你的家庭背景，成员和宠物可稍后再补充" />
      <div className="card config-form">
        {message ? <div className="api-ok">{message}</div> : null}
        <label>
          家庭名称
          <input
            value={homeProfile.home_name}
            onChange={(event) => setHomeProfile({ home_name: event.target.value })}
          />
        </label>

        <div>
          <p className="text-muted">家庭构成标签</p>
          <div className="inline-fields">
            {options.family_tags.map((item) => (
              <label className="checkbox-field" key={item}>
                <input
                  type="checkbox"
                  checked={homeProfile.family_tags.includes(item)}
                  onChange={() =>
                    setHomeProfile({ family_tags: toggleValue(homeProfile.family_tags, item) })
                  }
                />
                {familyTagLabel(item)}
              </label>
            ))}
          </div>
        </div>

        <div>
          <p className="text-muted">关注重点</p>
          <div className="inline-fields">
            {options.focus_points.map((item) => (
              <label className="checkbox-field" key={item}>
                <input
                  type="checkbox"
                  checked={homeProfile.focus_points.includes(item)}
                  onChange={() =>
                    setHomeProfile({ focus_points: toggleValue(homeProfile.focus_points, item) })
                  }
                />
                {focusPointLabel(item)}
              </label>
            ))}
          </div>
        </div>

        <label>
          家庭补充说明（可选）
          <textarea
            value={homeProfile.home_note}
            onChange={(event) => setHomeProfile({ home_note: event.target.value })}
          />
        </label>

        <div className="onboarding-actions">
          <button className="ghost" onClick={() => navigate('/onboarding/basic/done')}>
            上一步
          </button>
          <button
            onClick={() =>
              saveMutation.mutate({
                home_name: homeProfile.home_name.trim(),
                family_tags: homeProfile.family_tags,
                focus_points: homeProfile.focus_points,
                system_style: homeProfile.system_style,
                style_preference_text: homeProfile.style_preference_text,
                assistant_name: homeProfile.assistant_name,
                home_note: homeProfile.home_note,
              })
            }
            disabled={!canSave || saveMutation.isPending}
          >
            {saveMutation.isPending ? '保存中...' : '保存并下一步'}
          </button>
        </div>
      </div>
    </div>
  )
}
