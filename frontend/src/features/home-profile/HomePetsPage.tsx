import { useMemo, useRef, useState } from 'react'
import type { ChangeEvent, FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { PageHeader } from '../../components/common/PageHeader'
import { StatusTag } from '../../components/common/StatusTag'
import type { HomeEntity, PetPayload } from '../../types/api'
import {
  createPet,
  deleteEntityImage,
  disableEntity,
  generateEntityAppearance,
  getHomeOptions,
  listHomeEntities,
  updateEntity,
  uploadEntityImage,
} from './api'
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
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [form, setForm] = useState<FormState>(() => getInitialState(roles, initialValue))
  const [currentEntity, setCurrentEntity] = useState<HomeEntity | undefined>(initialValue)
  const [imageMessage, setImageMessage] = useState('')
  const [uploading, setUploading] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [generating, setGenerating] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const submitLabel = useMemo(() => (initialValue ? t('home_profile.save_changes') : t('home_profile.add_pet')), [initialValue, t])

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

  const handleUpload = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    if (!file || !currentEntity) return
    setUploading(true)
    setImageMessage('')
    try {
      const updated = await uploadEntityImage(currentEntity.id, file)
      setCurrentEntity(updated)
      queryClient.invalidateQueries({ queryKey: ['home-pets'] })
    } catch (error) {
      setImageMessage((error as Error).message)
    } finally {
      setUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const handleDeleteImage = async () => {
    if (!currentEntity) return
    setDeleting(true)
    setImageMessage('')
    try {
      const updated = await deleteEntityImage(currentEntity.id)
      setCurrentEntity(updated)
      queryClient.invalidateQueries({ queryKey: ['home-pets'] })
    } catch (error) {
      setImageMessage((error as Error).message)
    } finally {
      setDeleting(false)
    }
  }

  const handleGenerate = async () => {
    if (!currentEntity) return
    setGenerating(true)
    setImageMessage('')
    try {
      const updated = await generateEntityAppearance(currentEntity.id)
      setCurrentEntity(updated)
      setForm((old) => ({ ...old, appearance_desc: updated.appearance_desc ?? '' }))
      queryClient.invalidateQueries({ queryKey: ['home-pets'] })
    } catch (error) {
      setImageMessage((error as Error).message)
    } finally {
      setGenerating(false)
    }
  }

  return (
    <form className="dialog-form" onSubmit={handleSubmit}>
      {currentEntity && (
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8, marginBottom: 12 }}>
          {currentEntity.image_url ? (
            <img
              src={currentEntity.image_url}
              alt={currentEntity.name}
              style={{ width: 120, height: 120, objectFit: 'cover', borderRadius: 8 }}
            />
          ) : (
            <div
              style={{
                width: 120,
                height: 120,
                border: '2px dashed #ccc',
                borderRadius: 8,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: '#999',
                fontSize: 12,
              }}
            >
              {t('home_profile.no_image')}
            </div>
          )}
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*"
            style={{ display: 'none' }}
            onChange={handleUpload}
          />
          <div style={{ display: 'flex', gap: 8 }}>
            {currentEntity.image_url ? (
              <>
                <button
                  type="button"
                  className="ghost"
                  disabled={uploading}
                  onClick={() => fileInputRef.current?.click()}
                >
                  {uploading ? t('home_profile.uploading') : t('home_profile.re_upload')}
                </button>
                <button
                  type="button"
                  className="ghost"
                  disabled={deleting}
                  onClick={handleDeleteImage}
                >
                  {deleting ? t('home_profile.deleting') : t('home_profile.delete')}
                </button>
              </>
            ) : (
              <button
                type="button"
                className="ghost"
                disabled={uploading}
                onClick={() => fileInputRef.current?.click()}
              >
                {uploading ? t('home_profile.uploading') : t('home_profile.upload_image')}
              </button>
            )}
          </div>
          {imageMessage && <div style={{ color: '#e53e3e', fontSize: 12 }}>{imageMessage}</div>}
        </div>
      )}
      <label>
        {t('home_profile.field_name_pet')}
        <input value={form.name} required onChange={(event) => setForm((old) => ({ ...old, name: event.target.value }))} />
      </label>
      <label>
        {t('home_profile.field_role_pet')}
        <select value={form.role_type} onChange={(event) => setForm((old) => ({ ...old, role_type: event.target.value }))}>
          {roles.map((role) => (
            <option key={role} value={role}>
              {petRoleLabel(t, role)}
            </option>
          ))}
        </select>
      </label>
      <label>
        {t('home_profile.field_breed')}
        <input value={form.breed} onChange={(event) => setForm((old) => ({ ...old, breed: event.target.value }))} />
      </label>
      <label>
        {t('home_profile.field_appearance')}
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
          <textarea
            style={{ flex: 1 }}
            value={form.appearance_desc}
            onChange={(event) => setForm((old) => ({ ...old, appearance_desc: event.target.value }))}
          />
          {currentEntity && (
            <button
              type="button"
              className="ghost"
              disabled={generating}
              onClick={handleGenerate}
              style={{ whiteSpace: 'nowrap', flexShrink: 0 }}
            >
              {generating ? t('home_profile.generating') : t('home_profile.generate_ai')}
            </button>
          )}
        </div>
      </label>
      <label>
        {t('home_profile.field_personality')}
        <textarea value={form.personality_desc} onChange={(event) => setForm((old) => ({ ...old, personality_desc: event.target.value }))} />
      </label>
      <label>
        {t('home_profile.field_note_pet')}
        <textarea value={form.note} onChange={(event) => setForm((old) => ({ ...old, note: event.target.value }))} />
      </label>
      <div className="inline-fields">
        <label>
          {t('home_profile.field_sort')}
          <input type="number" min={0} value={form.sort_order} onChange={(event) => setForm((old) => ({ ...old, sort_order: event.target.value }))} />
        </label>
        <label className="checkbox-field">
          <input type="checkbox" checked={form.is_enabled} onChange={(event) => setForm((old) => ({ ...old, is_enabled: event.target.checked }))} />
          {t('home_profile.enable_pet')}
        </label>
      </div>
      <div className="dialog-actions">
        <button type="button" className="ghost" onClick={onCancel}>
          {t('home_profile.cancel')}
        </button>
        <button type="submit" disabled={pending}>
          {pending ? t('home_profile.processing') : submitLabel}
        </button>
      </div>
    </form>
  )
}

export function HomePetsPage() {
  const { t } = useTranslation()
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
      setMessage(t('home_profile.pet_created'))
      queryClient.invalidateQueries({ queryKey: ['home-pets'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: Partial<PetPayload> }) => updateEntity(id, payload),
    onSuccess: () => {
      setEditing(null)
      setMessage(t('home_profile.pet_updated'))
      queryClient.invalidateQueries({ queryKey: ['home-pets'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const disableMutation = useMutation({
    mutationFn: disableEntity,
    onSuccess: () => {
      setMessage(t('home_profile.pet_disabled'))
      queryClient.invalidateQueries({ queryKey: ['home-pets'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  if (optionsQuery.isLoading || listQuery.isLoading) {
    return <LoadingBlock text={t('home_profile.loading_pets')} />
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
    return <ApiErrorAlert message={t('common.load_failed_retry')} />
  }

  return (
    <div>
      <PageHeader
        title={t('home_profile.hub_pets_title')}
        subtitle={t('home_profile.hub_pets_desc_sub')}
        actions={<button onClick={() => setShowCreate(true)}>{t('home_profile.add_pet')}</button>}
      />

      {message ? <div className="api-ok">{message}</div> : null}

      <div className="card tool-row">
        <label className="checkbox-field">
          <input
            type="checkbox"
            checked={includeDisabled}
            onChange={(event) => setIncludeDisabled(event.target.checked)}
          />
          {t('home_profile.show_disabled_pets')}
        </label>
      </div>

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>ID</th>
              <th>{t('home_profile.col_name_pet')}</th>
              <th>{t('home_profile.col_role_pet')}</th>
              <th>{t('home_profile.col_breed')}</th>
              <th>{t('home_profile.col_status')}</th>
              <th>{t('home_profile.col_sort')}</th>
              <th>{t('home_profile.col_actions')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((item) => (
              <tr key={item.id}>
                <td>{item.id}</td>
                <td>{item.name}</td>
                <td>{petRoleLabel(t, item.role_type)}</td>
                <td>{item.breed ?? '-'}</td>
                <td>
                  <StatusTag status={item.is_enabled ? 'enabled' : 'disabled'} />
                </td>
                <td>{item.sort_order}</td>
                <td>
                  <div className="row-actions">
                    <button className="ghost" onClick={() => setEditing(item)}>
                      {t('home_profile.edit')}
                    </button>
                    <button
                      className="ghost"
                      onClick={() => {
                        const confirmed = window.confirm(t('home_profile.confirm_disable_pet', { name: item.name }))
                        if (confirmed) {
                          disableMutation.mutate(item.id)
                        }
                      }}
                    >
                      {t('home_profile.disable')}
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
            <h3>{editing ? t('home_profile.edit_pet') : t('home_profile.add_pet')}</h3>
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
