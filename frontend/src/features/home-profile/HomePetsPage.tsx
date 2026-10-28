import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { PageHeader } from '../../components/common/PageHeader'
import { StatusTag } from '../../components/common/StatusTag'
import { confirmDialog } from '../../lib/confirm'
import type { HomeEntity, PetPayload } from '../../types/api'
import { createPet, disableEntity, getHomeOptions, listHomeEntities, updateEntity } from './api'
import { EntityProfileForm } from './EntityProfileForm'
import { petRoleLabel } from './labels'
import { useEntityProfileMutations } from './useEntityProfileMutations'

export function HomePetsPage() {
  const { t } = useTranslation()
  const [showCreate, setShowCreate] = useState(false)
  const [editing, setEditing] = useState<HomeEntity | null>(null)
  const [includeDisabled, setIncludeDisabled] = useState(false)

  const optionsQuery = useQuery({ queryKey: ['home-profile-options'], queryFn: getHomeOptions })
  const listQuery = useQuery({
    queryKey: ['home-pets', includeDisabled],
    queryFn: () => listHomeEntities('pet', includeDisabled),
  })

  const { message, createMutation, updateMutation, disableMutation } =
    useEntityProfileMutations<PetPayload>({
      queryKey: 'home-pets',
      create: createPet,
      update: updateEntity,
      disable: disableEntity,
      messages: {
        created: t('home_profile.pet_created'),
        updated: t('home_profile.pet_updated'),
        disabled: t('home_profile.pet_disabled'),
      },
      closeCreate: () => setShowCreate(false),
      closeEdit: () => setEditing(null),
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
                        if (
                          confirmDialog(t('home_profile.confirm_disable_pet', { name: item.name }))
                        ) {
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
        <div
          className="dialog-mask"
          onClick={() => (showCreate ? setShowCreate(false) : setEditing(null))}
        >
          <div className="dialog" onClick={(event) => event.stopPropagation()}>
            <h3>{editing ? t('home_profile.edit_pet') : t('home_profile.add_pet')}</h3>
            <EntityProfileForm<PetPayload>
              roles={options.pet_roles}
              initialValue={editing ?? undefined}
              pending={createMutation.isPending || updateMutation.isPending}
              invalidateQueryKey="home-pets"
              roleLabel={(role) => petRoleLabel(t, role)}
              roleFieldLabel={t('home_profile.field_role_pet')}
              nameLabel={t('home_profile.field_name_pet')}
              noteLabel={t('home_profile.field_note_pet')}
              enableLabel={t('home_profile.enable_pet')}
              addLabel={t('home_profile.add_pet')}
              extraFields={(form, setForm) => (
                <>
                  <label>
                    {t('home_profile.field_breed')}
                    <input
                      value={form.breed}
                      onChange={(event) =>
                        setForm((old) => ({ ...old, breed: event.target.value }))
                      }
                    />
                  </label>
                  <label>
                    {t('home_profile.field_personality')}
                    <textarea
                      value={form.personality_desc}
                      onChange={(event) =>
                        setForm((old) => ({ ...old, personality_desc: event.target.value }))
                      }
                    />
                  </label>
                </>
              )}
              buildPayload={(form) => ({
                name: form.name.trim(),
                role_type: form.role_type,
                breed: form.breed.trim() || undefined,
                appearance_desc: form.appearance_desc.trim() || undefined,
                personality_desc: form.personality_desc.trim() || undefined,
                note: form.note.trim() || undefined,
                sort_order: Number(form.sort_order),
                is_enabled: form.is_enabled,
              })}
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
