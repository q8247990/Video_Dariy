import { useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { PageHeader } from '../../components/common/PageHeader'
import { StatusTag } from '../../components/common/StatusTag'
import type { HomeEntity, PetPayload } from '../../types/api'
import { createPet, disableEntity, getHomeOptions, listHomeEntities, updateEntity } from './api'
import { petRoleLabel } from './labels'

type PetFormProps = {
  roles: string[]
  initialValue?: HomeEntity
  pending: boolean
  onCancel: () => void
  onSubmit: (payload: PetPayload) => void
}

type FormState = {
  name: string
  role_type: string
  breed: string
  appearance_desc: string
  personality_desc: string
  note: string
  sort_order: string
  is_enabled: boolean
}

function getInitialState(roles: string[], initialValue?: HomeEntity): FormState {
  return {
    name: initialValue?.name ?? '',
    role_type: initialValue?.role_type ?? roles[0] ?? 'cat',
    breed: initialValue?.breed ?? '',
    appearance_desc: initialValue?.appearance_desc ?? '',
    personality_desc: initialValue?.personality_desc ?? '',
    note: initialValue?.note ?? '',
    sort_order: String(initialValue?.sort_order ?? 0),
    is_enabled: initialValue?.is_enabled ?? true,
  }
}

function PetForm({ roles, initialValue, pending, onCancel, onSubmit }: PetFormProps) {
  const [form, setForm] = useState<FormState>(() => getInitialState(roles, initialValue))
  const submitLabel = useMemo(() => (initialValue ? '保存修改' : '新增宠物'), [initialValue])

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    onSubmit({
      name: form.name.trim(),
      role_type: form.role_type,
      breed: form.breed.trim() || undefined,
      appearance_desc: form.appearance_desc.trim() || undefined,
      personality_desc: form.personality_desc.trim() || undefined,
      note: form.note.trim() || undefined,
      sort_order: Number(form.sort_order),
      is_enabled: form.is_enabled,
    })
  }

  return (
    <form className="dialog-form" onSubmit={handleSubmit}>
      <label>
        名字
        <input value={form.name} required onChange={(event) => setForm((old) => ({ ...old, name: event.target.value }))} />
      </label>
      <label>
        宠物类型
        <select value={form.role_type} onChange={(event) => setForm((old) => ({ ...old, role_type: event.target.value }))}>
          {roles.map((role) => (
            <option key={role} value={role}>
              {petRoleLabel(role)}
            </option>
          ))}
        </select>
      </label>
      <label>
        品种
        <input value={form.breed} onChange={(event) => setForm((old) => ({ ...old, breed: event.target.value }))} />
      </label>
      <label>
        外观特征
        <textarea value={form.appearance_desc} onChange={(event) => setForm((old) => ({ ...old, appearance_desc: event.target.value }))} />
      </label>
      <label>
        性格 / 日常风格
        <textarea value={form.personality_desc} onChange={(event) => setForm((old) => ({ ...old, personality_desc: event.target.value }))} />
      </label>
      <label>
        宠物补充说明
        <textarea value={form.note} onChange={(event) => setForm((old) => ({ ...old, note: event.target.value }))} />
      </label>
      <div className="inline-fields">
        <label>
          展示排序
          <input type="number" min={0} value={form.sort_order} onChange={(event) => setForm((old) => ({ ...old, sort_order: event.target.value }))} />
        </label>
        <label className="checkbox-field">
          <input type="checkbox" checked={form.is_enabled} onChange={(event) => setForm((old) => ({ ...old, is_enabled: event.target.checked }))} />
          启用宠物
        </label>
      </div>
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

export function HomePetsPage() {
  const queryClient = useQueryClient()
  const [message, setMessage] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [editing, setEditing] = useState<HomeEntity | null>(null)
  const [includeDisabled, setIncludeDisabled] = useState(false)

  const optionsQuery = useQuery({ queryKey: ['home-profile-options'], queryFn: getHomeOptions })
  const listQuery = useQuery({
    queryKey: ['home-pets', includeDisabled],
    queryFn: () => listHomeEntities('pet', includeDisabled),
  })

  const createMutation = useMutation({
    mutationFn: createPet,
    onSuccess: () => {
      setShowCreate(false)
      setMessage('宠物已创建')
      queryClient.invalidateQueries({ queryKey: ['home-pets'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: Partial<PetPayload> }) => updateEntity(id, payload),
    onSuccess: () => {
      setEditing(null)
      setMessage('宠物已更新')
      queryClient.invalidateQueries({ queryKey: ['home-pets'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const disableMutation = useMutation({
    mutationFn: disableEntity,
    onSuccess: () => {
      setMessage('宠物已停用')
      queryClient.invalidateQueries({ queryKey: ['home-pets'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  if (optionsQuery.isLoading || listQuery.isLoading) {
    return <LoadingBlock text="加载宠物档案中" />
  }

  if (optionsQuery.error) {
    return <ApiErrorAlert message={(optionsQuery.error as Error).message} />
  }

  if (listQuery.error) {
    return <ApiErrorAlert message={(listQuery.error as Error).message} />
  }

  const rows = listQuery.data ?? []
  const options = optionsQuery.data
  if (!options) {
    return <ApiErrorAlert message="未获取到宠物选项" />
  }

  return (
    <div>
      <PageHeader
        title="宠物档案"
        subtitle="维护宠物名称、类型和个体特征"
        actions={<button onClick={() => setShowCreate(true)}>新增宠物</button>}
      />

      {message ? <div className="api-ok">{message}</div> : null}

      <div className="card tool-row">
        <label className="checkbox-field">
          <input
            type="checkbox"
            checked={includeDisabled}
            onChange={(event) => setIncludeDisabled(event.target.checked)}
          />
          显示已停用宠物
        </label>
      </div>

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>ID</th>
              <th>名字</th>
              <th>类型</th>
              <th>品种</th>
              <th>状态</th>
              <th>排序</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((item) => (
              <tr key={item.id}>
                <td>{item.id}</td>
                <td>{item.name}</td>
                <td>{petRoleLabel(item.role_type)}</td>
                <td>{item.breed ?? '-'}</td>
                <td>
                  <StatusTag status={item.is_enabled ? 'enabled' : 'disabled'} />
                </td>
                <td>{item.sort_order}</td>
                <td>
                  <div className="row-actions">
                    <button className="ghost" onClick={() => setEditing(item)}>
                      编辑
                    </button>
                    <button
                      className="ghost"
                      onClick={() => {
                        const confirmed = window.confirm(`确认停用宠物「${item.name}」吗？`)
                        if (confirmed) {
                          disableMutation.mutate(item.id)
                        }
                      }}
                    >
                      停用
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {(showCreate || editing) && (
        <div className="dialog-mask" onClick={() => (showCreate ? setShowCreate(false) : setEditing(null))}>
          <div className="dialog" onClick={(event) => event.stopPropagation()}>
            <h3>{editing ? '编辑宠物' : '新增宠物'}</h3>
            <PetForm
              roles={options.pet_roles}
              initialValue={editing ?? undefined}
              pending={createMutation.isPending || updateMutation.isPending}
              onCancel={() => (editing ? setEditing(null) : setShowCreate(false))}
              onSubmit={(payload) => {
                if (editing) {
                  updateMutation.mutate({ id: editing.id, payload })
                } else {
                  createMutation.mutate(payload)
                }
              }}
            />
          </div>
        </div>
      )}
    </div>
  )
}
