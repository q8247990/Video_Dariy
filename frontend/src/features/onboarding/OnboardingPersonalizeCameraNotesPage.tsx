import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { PageHeader } from '../../components/common/PageHeader'
import { getVideoSourcesForOnboarding, updateVideoSourceDescription } from './api'
import { useOnboardingDraftStore } from './state'

export function OnboardingPersonalizeCameraNotesPage() {
  const navigate = useNavigate()
  const hydrate = useOnboardingDraftStore((state) => state.hydrate)
  const cameraNotes = useOnboardingDraftStore((state) => state.cameraNotes)
  const setCameraNote = useOnboardingDraftStore((state) => state.setCameraNote)
  const [message, setMessage] = useState('')

  useEffect(() => {
    hydrate()
  }, [hydrate])

  const query = useQuery({
    queryKey: ['onboarding-video-sources'],
    queryFn: getVideoSourcesForOnboarding,
  })

  const mutation = useMutation({
    mutationFn: async () => {
      const rows = query.data ?? []
      const updates = rows.map((row) => {
        const text = cameraNotes[row.id] ?? row.description ?? ''
        return updateVideoSourceDescription(row.id, text.trim())
      })
      await Promise.all(updates)
    },
    onSuccess: () => {
      setMessage('摄像头注意事项已保存')
      navigate('/onboarding/personalize/style')
    },
    onError: (error) => setMessage((error as Error).message),
  })

  if (query.isLoading) {
    return <LoadingBlock text="加载视频源中" />
  }
  if (query.error) {
    return <ApiErrorAlert message={(query.error as Error).message} />
  }

  const rows = query.data ?? []

  return (
    <div>
      <PageHeader title="阶段二 · 摄像头注意事项" subtitle="每个摄像头补充一句场景说明，可全部跳过" />
      <div className="card config-form">
        {message ? <div className="api-ok">{message}</div> : null}
        {rows.length === 0 ? <p className="text-muted">当前没有可配置的视频源。</p> : null}
        {rows.map((row) => (
          <label key={row.id}>
            {row.source_name}（{row.camera_name}）
            <textarea
              value={cameraNotes[row.id] ?? row.description ?? ''}
              onChange={(event) => setCameraNote(row.id, event.target.value)}
              placeholder="例如：主要拍摄客厅，忽略电视画面"
            />
          </label>
        ))}

        <div className="onboarding-actions">
          <button className="ghost" onClick={() => navigate('/onboarding/personalize/profile')}>
            上一步
          </button>
          <button className="ghost" onClick={() => navigate('/onboarding/personalize/style')}>
            全部跳过
          </button>
          <button onClick={() => mutation.mutate()} disabled={mutation.isPending}>
            {mutation.isPending ? '保存中...' : '保存并下一步'}
          </button>
        </div>
      </div>
    </div>
  )
}
