import { useMemo, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import type { HomeEntity } from '../../types/api'
import { EntityImageField } from './EntityImageField'

export type EntityFormState = {
  name: string
  role_type: string
  age_group: string
  breed: string
  appearance_desc: string
  personality_desc: string
  note: string
  sort_order: string
  is_enabled: boolean
}

type EntityProfileFormProps<TPayload> = {
  roles: string[]
  initialValue?: HomeEntity
  pending: boolean
  invalidateQueryKey: string
  roleLabel: (role: string) => string
  roleFieldLabel: string
  nameLabel: string
  noteLabel: string
  enableLabel: string
  addLabel: string
  buildPayload: (form: EntityFormState) => TPayload
  extraFields?: (
    form: EntityFormState,
    setForm: (updater: (old: EntityFormState) => EntityFormState) => void,
  ) => ReactNode
  onCancel: () => void
  onSubmit: (payload: TPayload) => void
}

function buildInitialEntityForm(
  roles: string[],
  initialValue?: HomeEntity,
  fallbackRole = 'child',
): EntityFormState {
  return {
    name: initialValue?.name ?? '',
    role_type: initialValue?.role_type ?? roles[0] ?? fallbackRole,
    age_group: initialValue?.age_group ?? '',
    breed: initialValue?.breed ?? '',
    appearance_desc: initialValue?.appearance_desc ?? '',
    personality_desc: initialValue?.personality_desc ?? '',
    note: initialValue?.note ?? '',
    sort_order: String(initialValue?.sort_order ?? 0),
    is_enabled: initialValue?.is_enabled ?? true,
  }
}

export function EntityProfileForm<TPayload>({
  roles,
  initialValue,
  pending,
  invalidateQueryKey,
  roleLabel,
  roleFieldLabel,
  nameLabel,
  noteLabel,
  enableLabel,
  addLabel,
  buildPayload,
  extraFields,
  onCancel,
  onSubmit,
}: EntityProfileFormProps<TPayload>) {
  const { t } = useTranslation()
  const [form, setForm] = useState<EntityFormState>(() =>
    buildInitialEntityForm(roles, initialValue),
  )
  const [currentEntity, setCurrentEntity] = useState<HomeEntity | undefined>(initialValue)
  const submitLabel = useMemo(
    () => (initialValue ? t('home_profile.save_changes') : addLabel),
    [addLabel, initialValue, t],
  )

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    onSubmit(buildPayload(form))
  }

  return (
    <form className="dialog-form" onSubmit={handleSubmit}>
      <EntityImageField
        entity={currentEntity}
        invalidateQueryKey={invalidateQueryKey}
        allowGenerate={Boolean(initialValue)}
        onEntityChange={setCurrentEntity}
        onAppearanceChange={(appearanceDesc) =>
          setForm((old) => ({ ...old, appearance_desc: appearanceDesc }))
        }
      />
      <label>
        {nameLabel}
        <input
          value={form.name}
          required
          onChange={(event) => setForm((old) => ({ ...old, name: event.target.value }))}
        />
      </label>
      <label>
        {roleFieldLabel}
        <select
          value={form.role_type}
          onChange={(event) => setForm((old) => ({ ...old, role_type: event.target.value }))}
        >
          {roles.map((role) => (
            <option key={role} value={role}>
              {roleLabel(role)}
            </option>
          ))}
        </select>
      </label>
      {extraFields?.(form, setForm)}
      <label>
        {t('home_profile.field_appearance')}
        <textarea
          value={form.appearance_desc}
          onChange={(event) => setForm((old) => ({ ...old, appearance_desc: event.target.value }))}
        />
      </label>
      <label>
        {noteLabel}
        <textarea
          value={form.note}
          onChange={(event) => setForm((old) => ({ ...old, note: event.target.value }))}
        />
      </label>
      <div className="inline-fields">
        <label>
          {t('home_profile.field_sort')}
          <input
            type="number"
            min={0}
            value={form.sort_order}
            onChange={(event) => setForm((old) => ({ ...old, sort_order: event.target.value }))}
          />
        </label>
        <label className="checkbox-field">
          <input
            type="checkbox"
            checked={form.is_enabled}
            onChange={(event) => setForm((old) => ({ ...old, is_enabled: event.target.checked }))}
          />
          {enableLabel}
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
