import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { PageHeader } from '../../components/common/PageHeader'
import type { FocusPointItem, HomeProfile } from '../../types/api'
import { getHomeOptions, getHomeProfile, saveHomeProfile } from './api'
import { familyTagLabel, systemStyleLabel } from './labels'

type FormState = {
  home_name: string
  family_tags: string[]
  focus_points: FocusPointItem[]
  system_style: string
  style_preference_text: string
  assistant_name: string
  home_note: string
}

function toFormState(data: HomeProfile): FormState {
  return {
    home_name: data.home_name,
    family_tags: data.family_tags,
    focus_points: data.focus_points,
    system_style: data.system_style,
    style_preference_text: data.style_preference_text ?? '',
    assistant_name: data.assistant_name,
    home_note: data.home_note ?? '',
  }
}

function isChecked(values: string[], value: string): boolean {
  return values.includes(value)
}

function toggleValue(values: string[], value: string): string[] {
  if (values.includes(value)) {
    return values.filter((item) => item !== value)
  }
  return [...values, value]
}

function nextFocusKey(items: FocusPointItem[]): string {
  const existing = new Set(items.map((item) => item.key))
  let index = items.length + 1
  while (existing.has(`custom_${index}`)) {
    index += 1
  }
  return `custom_${index}`
}

export function HomeProfilePage() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [message, setMessage] = useState('')

  const profileQuery = useQuery({
    queryKey: ['home-profile'],
    queryFn: getHomeProfile,
  })

  const optionsQuery = useQuery({
    queryKey: ['home-profile-options'],
    queryFn: getHomeOptions,
  })

  const saveMutation = useMutation({
    mutationFn: saveHomeProfile,
    onSuccess: () => {
      setMessage(t('home_profile.profile_saved'))
      queryClient.invalidateQueries({ queryKey: ['home-profile'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  if (profileQuery.isLoading || optionsQuery.isLoading) {
    return <LoadingBlock text={t('home_profile.loading_profile')} />
  }

  if (profileQuery.error) {
    return <ApiErrorAlert message={(profileQuery.error as Error).message} />
  }

  if (optionsQuery.error) {
    return <ApiErrorAlert message={(optionsQuery.error as Error).message} />
  }

  const options = optionsQuery.data
  if (!options) {
    return <ApiErrorAlert message={t('common.load_failed_retry')} />
  }

  return (
    <div>
      <PageHeader title={t('home_profile.title')} subtitle={t('home_profile.hub_profile_subtitle')} />

      {message ? <div className="api-ok">{message}</div> : null}

      <HomeProfileForm
        key={profileQuery.data?.updated_at ?? 'default'}
        initialForm={profileQuery.data ? toFormState(profileQuery.data) : getDefaultForm(t)}
        options={options}
        pending={saveMutation.isPending}
        onSubmit={(form) => {
          saveMutation.mutate({
            home_name: form.home_name.trim(),
            family_tags: form.family_tags,
            focus_points: form.focus_points
              .filter((item) => item.label.trim().length > 0)
              .map((item) => ({
                ...item,
                label: item.label.trim(),
                description: item.description.trim(),
              })),
            system_style: form.system_style,
            style_preference_text: form.style_preference_text.trim(),
            assistant_name: form.assistant_name.trim(),
            home_note: form.home_note.trim(),
          })
        }}
      />
    </div>
  )
}

function getDefaultForm(t: (key: string) => string): FormState {
  return {
    home_name: t('home_profile.default_home_name'),
    family_tags: [],
    focus_points: [],
    system_style: 'family_companion',
    style_preference_text: '',
    assistant_name: t('home_profile.default_assistant_name'),
    home_note: '',
  }
}

type HomeProfileFormProps = {
  initialForm: FormState
  options: NonNullable<Awaited<ReturnType<typeof getHomeOptions>>>
  pending: boolean
  onSubmit: (form: FormState) => void
}

function HomeProfileForm({ initialForm, options, pending, onSubmit }: HomeProfileFormProps) {
  const { t } = useTranslation()
  const [form, setForm] = useState<FormState>(initialForm)

  return (
    <div className="card config-form">
        <label>
          {t('home_profile.field_home_name')}
          <input
            value={form.home_name}
            onChange={(event) => setForm((old) => ({ ...old, home_name: event.target.value }))}
            maxLength={128}
          />
        </label>

        <div>
          <p className="text-muted">{t('home_profile.field_family_tags')}</p>
          <div className="inline-fields">
            {options.family_tags.map((item) => (
              <label className="checkbox-field" key={item}>
                <input
                  type="checkbox"
                  checked={isChecked(form.family_tags, item)}
                  onChange={() =>
                    setForm((old) => ({
                      ...old,
                      family_tags: toggleValue(old.family_tags, item),
                    }))
                  }
                />
                {familyTagLabel(t, item)}
              </label>
            ))}
          </div>
        </div>

        <div>
          <p className="text-muted">{t('home_profile.field_focus_points')}</p>
          <div className="focus-list">
            {form.focus_points.map((item, index) => (
              <div className="focus-row" key={item.key}>
                <input
                  value={item.label}
                  maxLength={20}
                  placeholder={t('home_profile.focus_label_placeholder')}
                  onChange={(event) =>
                    setForm((old) => ({
                      ...old,
                      focus_points: old.focus_points.map((fp, i) =>
                        i === index ? { ...fp, label: event.target.value } : fp,
                      ),
                    }))
                  }
                />
                <input
                  value={item.description}
                  maxLength={100}
                  placeholder={t('home_profile.focus_description_placeholder')}
                  onChange={(event) =>
                    setForm((old) => ({
                      ...old,
                      focus_points: old.focus_points.map((fp, i) =>
                        i === index ? { ...fp, description: event.target.value } : fp,
                      ),
                    }))
                  }
                />
                <label className="checkbox-field">
                  <input
                    type="checkbox"
                    checked={item.attention}
                    onChange={(event) =>
                      setForm((old) => ({
                        ...old,
                        focus_points: old.focus_points.map((fp, i) =>
                          i === index ? { ...fp, attention: event.target.checked } : fp,
                        ),
                      }))
                    }
                  />
                  {t('home_profile.focus_attention')}
                </label>
                <button
                  type="button"
                  className="ghost"
                  onClick={() =>
                    setForm((old) => ({
                      ...old,
                      focus_points: old.focus_points.filter((_, i) => i !== index),
                    }))
                  }
                >
                  {t('home_profile.focus_remove')}
                </button>
              </div>
            ))}
          </div>
          <div className="inline-fields">
            {options.focus_points.map((preset) => (
              <button
                type="button"
                className="ghost"
                key={preset.key}
                disabled={form.focus_points.some((fp) => fp.key === preset.key)}
                onClick={() =>
                  setForm((old) => ({
                    ...old,
                    focus_points: [...old.focus_points, { ...preset }],
                  }))
                }
              >
                {preset.label}
              </button>
            ))}
            <button
              type="button"
              className="ghost"
              onClick={() =>
                setForm((old) => ({
                  ...old,
                  focus_points: [
                    ...old.focus_points,
                    {
                      key: nextFocusKey(old.focus_points),
                      label: '',
                      description: '',
                      enabled: true,
                      attention: false,
                    },
                  ],
                }))
              }
            >
              {t('home_profile.focus_add_custom')}
            </button>
          </div>
        </div>

        <label>
          {t('home_profile.field_system_style')}
          <select
            value={form.system_style}
            onChange={(event) => setForm((old) => ({ ...old, system_style: event.target.value }))}
          >
            {options.system_styles.map((item) => (
              <option key={item} value={item}>
                {systemStyleLabel(t, item)}
              </option>
            ))}
          </select>
        </label>

        <label>
          {t('home_profile.field_style_preference')}
          <textarea
            value={form.style_preference_text}
            onChange={(event) =>
              setForm((old) => ({ ...old, style_preference_text: event.target.value }))
            }
            maxLength={1000}
          />
        </label>

        <label>
          {t('home_profile.field_assistant_name')}
          <input
            value={form.assistant_name}
            onChange={(event) => setForm((old) => ({ ...old, assistant_name: event.target.value }))}
            maxLength={128}
          />
        </label>

        <label>
          {t('home_profile.field_home_note')}
          <textarea
            value={form.home_note}
            onChange={(event) => setForm((old) => ({ ...old, home_note: event.target.value }))}
            maxLength={1000}
          />
        </label>

        <div className="dialog-actions">
          <button onClick={() => onSubmit(form)} disabled={pending}>
            {pending ? t('common.saving') : t('home_profile.save_profile')}
          </button>
        </div>
      </div>
  )
}
