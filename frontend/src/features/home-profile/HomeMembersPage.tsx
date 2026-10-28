import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { PageHeader } from '../../components/common/PageHeader'
import { StatusTag } from '../../components/common/StatusTag'
import { confirmDialog } from '../../lib/confirm'
import type { HomeEntity, MemberPayload } from '../../types/api'
import { createMember, disableEntity, getHomeOptions, listHomeEntities, updateEntity } from './api'
import { EntityProfileForm } from './EntityProfileForm'
import { ageGroupLabel, memberRoleLabel } from './labels'
import { useEntityProfileMutations } from './useEntityProfileMutations'

export function HomeMembersPage() {
  const { t } = useTranslation()
  const [showCreate, setShowCreate] = useState(false)
  const [editing, setEditing] = useState<HomeEntity | null>(null)
  const [includeDisabled, setIncludeDisabled] = useState(false)

  const optionsQuery = useQuery({ queryKey: ['home-profile-options'], queryFn: getHomeOptions })
  const listQuery = useQuery({
    queryKey: ['home-members', includeDisabled],
    queryFn: () => listHomeEntities('member', includeDisabled),
  })

  const { message, createMutation, updateMutation, disableMutation } =
    useEntityProfileMutations<MemberPayload>({
      queryKey: 'home-members',
      create: createMember,
      update: updateEntity,
      disable: disableEntity,
      messages: {
        created: t('home_profile.member_created'),
        updated: t('home_profile.member_updated'),
        disabled: t('home_profile.member_disabled'),
      },
      closeCreate: () => setShowCreate(false),
      closeEdit: () => setEditing(null),
    })

  if (optionsQuery.isLoading || listQuery.isLoading) {
    return <LoadingBlock text={t('home_profile.loading_members')} />
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
        title={t('home_profile.hub_members_title')}
        subtitle={t('home_profile.hub_members_subtitle')}
        actions={
          <button onClick={() => setShowCreate(true)}>{t('home_profile.add_member')}</button>
        }
      />

      {message ? <div className="api-ok">{message}</div> : null}

      <div className="card tool-row">
        <label className="checkbox-field">
          <input
            type="checkbox"
            checked={includeDisabled}
            onChange={(event) => setIncludeDisabled(event.target.checked)}
          />
          {t('home_profile.show_disabled_members')}
        </label>
      </div>

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>ID</th>
              <th>{t('home_profile.col_name')}</th>
              <th>{t('home_profile.col_role')}</th>
              <th>{t('home_profile.field_age_group')}</th>
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
                <td>{memberRoleLabel(t, item.role_type)}</td>
                <td>{item.age_group ? ageGroupLabel(t, item.age_group) : '-'}</td>
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
                          confirmDialog(
                            t('home_profile.confirm_disable_member', { name: item.name }),
                          )
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
            <h3>{editing ? t('home_profile.edit_member') : t('home_profile.add_member')}</h3>
            <EntityProfileForm<MemberPayload>
              roles={options.member_roles}
              initialValue={editing ?? undefined}
              pending={createMutation.isPending || updateMutation.isPending}
              invalidateQueryKey="home-members"
              roleLabel={(role) => memberRoleLabel(t, role)}
              roleFieldLabel={t('home_profile.field_role_member')}
              nameLabel={t('home_profile.field_name_member')}
              noteLabel={t('home_profile.field_note_member')}
              enableLabel={t('home_profile.enable_member')}
              addLabel={t('home_profile.add_member')}
              extraFields={(form, setForm) => (
                <label>
                  {t('home_profile.field_age_group')}
                  <select
                    value={form.age_group}
                    onChange={(event) =>
                      setForm((old) => ({ ...old, age_group: event.target.value }))
                    }
                  >
                    <option value="">{t('home_profile.unset')}</option>
                    {options.age_groups.map((item) => (
                      <option key={item} value={item}>
                        {ageGroupLabel(t, item)}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              buildPayload={(form) => ({
                name: form.name.trim(),
                role_type: form.role_type,
                age_group: form.age_group || undefined,
                appearance_desc: form.appearance_desc.trim() || undefined,
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
