import { useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import type { VideoSource, VideoSourceCreate } from '../../types/api'

type VideoSourceFormProps = {
  initialValue?: VideoSource
  pending?: boolean
  onCancel: () => void
  onSubmit: (payload: VideoSourceCreate) => void
}

type FormState = {
  source_name: string
  camera_name: string
  location_name: string
  description: string
  prompt_text: string
  source_type: string
  root_path: string
  enabled: boolean
}

function getInitialState(initialValue?: VideoSource): FormState {
  const config = initialValue?.config_json as Record<string, unknown> | null
  const rootPath = typeof config?.root_path === 'string' ? config.root_path : ''

  return {
    source_name: initialValue?.source_name ?? '',
    camera_name: initialValue?.camera_name ?? '',
    location_name: initialValue?.location_name ?? '',
    description: initialValue?.description ?? '',
    prompt_text: initialValue?.prompt_text ?? '',
    source_type: initialValue?.source_type ?? 'local_directory',
    root_path: rootPath,
    enabled: initialValue?.enabled ?? true,
  }
}

export function VideoSourceForm({ initialValue, pending, onCancel, onSubmit }: VideoSourceFormProps) {
  const [form, setForm] = useState<FormState>(() => getInitialState(initialValue))

  const submitLabel = useMemo(() => (initialValue ? '保存修改' : '创建视频源'), [initialValue])

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    onSubmit({
      source_name: form.source_name,
      camera_name: form.camera_name,
      location_name: form.location_name,
      description: form.description,
      prompt_text: form.prompt_text,
      source_type: form.source_type,
      config_json: { root_path: form.root_path },
      enabled: form.enabled,
    })
  }

  return (
    <form className="dialog-form" onSubmit={handleSubmit}>
      <label>
        视频源名称
        <input
          required
          value={form.source_name}
          onChange={(event) => setForm((old) => ({ ...old, source_name: event.target.value }))}
        />
      </label>
      <label>
        摄像头名称
        <input
          required
          value={form.camera_name}
          onChange={(event) => setForm((old) => ({ ...old, camera_name: event.target.value }))}
        />
      </label>
      <label>
        所在位置
        <input
          required
          value={form.location_name}
          onChange={(event) => setForm((old) => ({ ...old, location_name: event.target.value }))}
        />
      </label>
      <label>
        目录路径
        <input
          required
          value={form.root_path}
          onChange={(event) => setForm((old) => ({ ...old, root_path: event.target.value }))}
          placeholder="/data/videos"
        />
      </label>
      <label>
        描述
        <textarea
          value={form.description}
          onChange={(event) => setForm((old) => ({ ...old, description: event.target.value }))}
        />
      </label>
      <label>
        识别提示词
        <textarea
          value={form.prompt_text}
          onChange={(event) => setForm((old) => ({ ...old, prompt_text: event.target.value }))}
        />
      </label>
      <label className="checkbox-field">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(event) => setForm((old) => ({ ...old, enabled: event.target.checked }))}
        />
        启用该视频源
      </label>
      <div className="dialog-actions">
        <button type="button" className="ghost" onClick={onCancel}>
          取消
        </button>
        <button type="submit" disabled={pending}>
          {pending ? '处理中...' : submitLabel}
        </button>
      </div>
    </form>
  )
}
