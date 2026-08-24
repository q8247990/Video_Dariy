import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { PageHeader } from '../../components/common/PageHeader'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { StatusTag } from '../../components/common/StatusTag'
import type { Provider, ProviderCreate, ProviderUpdate } from '../../types/api'
import {
  createProvider,
  deleteProvider,
  getProviderDailyUsage,
  getProviders,
  setDefaultQaProvider,
  setDefaultVisionProvider,
  testProvider,
  updateProvider,
} from './api'
import { ProviderForm } from './ProviderForm'

export function ProvidersPage() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [providerType, setProviderType] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [editing, setEditing] = useState<Provider | null>(null)
  const [message, setMessage] = useState('')

  const queryKey = useMemo(() => ['providers', { providerType }], [providerType])

  const listQuery = useQuery({
    queryKey,
    queryFn: () => getProviders({ page: 1, pageSize: 50, providerType }),
  })

  const usageQuery = useQuery({
    queryKey: ['provider-usage-daily'],
    queryFn: () => getProviderDailyUsage(7),
  })

  const createMutation = useMutation({
    mutationFn: createProvider,
    onSuccess: () => {
      setShowCreate(false)
      setMessage(t('providers.create_success'))
      queryClient.invalidateQueries({ queryKey: ['providers'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: ProviderUpdate }) => updateProvider(id, payload),
    onSuccess: () => {
      setEditing(null)
      setMessage(t('providers.update_success'))
      queryClient.invalidateQueries({ queryKey: ['providers'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const defaultVisionMutation = useMutation({
    mutationFn: setDefaultVisionProvider,
    onSuccess: () => {
      setMessage(t('providers.set_vision_success'))
      queryClient.invalidateQueries({ queryKey: ['providers'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const defaultQaMutation = useMutation({
    mutationFn: setDefaultQaProvider,
    onSuccess: () => {
      setMessage(t('providers.set_qa_success'))
      queryClient.invalidateQueries({ queryKey: ['providers'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const testMutation = useMutation({
    mutationFn: testProvider,
    onSuccess: (data) => {
      setMessage(data.message)
      queryClient.invalidateQueries({ queryKey: ['providers'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const deleteMutation = useMutation({
    mutationFn: deleteProvider,
    onSuccess: () => {
      setMessage(t('providers.delete_success'))
      queryClient.invalidateQueries({ queryKey: ['providers'] })
      queryClient.invalidateQueries({ queryKey: ['provider-usage-daily'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const rows = listQuery.data?.list ?? []
  const activeVisionProviderId =
    rows.find((item) => item.enabled && item.supports_vision && item.is_default_vision)?.id ??
    rows.find((item) => item.enabled && item.supports_vision)?.id
  const activeQaProviderId =
    rows.find((item) => item.enabled && item.supports_qa && item.is_default_qa)?.id ??
    rows.find((item) => item.enabled && item.supports_qa)?.id

  const latestUsageDay = usageQuery.data?.[0]

  const providerTokenMap = useMemo(() => {
    const result = new Map<number, number>()
    if (!latestUsageDay) {
      return result
    }
    for (const item of latestUsageDay.providers) {
      if (item.provider_id !== null) {
        result.set(item.provider_id, item.total_tokens)
      }
    }
    return result
  }, [latestUsageDay])

  if (listQuery.isLoading) {
    return <LoadingBlock text={t('providers.loading')} />
  }

  if (listQuery.error) {
    return <ApiErrorAlert message={(listQuery.error as Error).message} />
  }

  return (
    <div>
      <PageHeader
        title={t('providers.title')}
        subtitle={t('providers.subtitle')}
        actions={
          <button onClick={() => setShowCreate(true)}>{t('providers.add_provider')}</button>
        }
      />

      <div className="card tool-row tool-row-inline">
        <label>
          {t('providers.type_filter_label')}
          <select value={providerType} onChange={(event) => setProviderType(event.target.value)}>
            <option value="">{t('providers.type_filter_all')}</option>
            <option value="vision_provider">{t('providers.type_filter_vision')}</option>
            <option value="qa_provider">{t('providers.type_filter_qa')}</option>
          </select>
        </label>
      </div>

      <div className="card">
        <div>
          <strong>{t('providers.token_usage_7d')}</strong>
        </div>
        {usageQuery.isLoading ? (
          <div>{t('providers.token_usage_loading')}</div>
        ) : usageQuery.error ? (
          <div className="api-error">{(usageQuery.error as Error).message}</div>
        ) : latestUsageDay ? (
          <div>
            {latestUsageDay.date} |{' '}
            {t('providers.token_total', {
              prompt: latestUsageDay.prompt_tokens,
              completion: latestUsageDay.completion_tokens,
            })}
          </div>
        ) : (
          <div>{t('providers.token_usage_empty')}</div>
        )}
      </div>

      {message ? <div className="api-ok">{message}</div> : null}

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>{t('providers.table_col_id')}</th>
              <th>{t('providers.table_col_name')}</th>
              <th>{t('providers.table_col_capability')}</th>
              <th>{t('providers.table_col_model')}</th>
              <th>{t('providers.table_col_status')}</th>
              <th>{t('providers.table_col_in_use')}</th>
              <th>{t('providers.table_col_last_test')}</th>
              <th>{t('providers.table_col_availability')}</th>
              <th>{t('providers.table_col_today_tokens')}</th>
              <th>{t('providers.table_col_actions')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((item) => (
              <tr key={item.id}>
                <td>{item.id}</td>
                <td>{item.provider_name}</td>
                <td>
                  {[
                    item.supports_vision ? t('providers.vision_label') : '',
                    item.supports_qa ? t('providers.qa_label') : '',
                    item.supports_tool_calling ? t('providers.tool_calling_label') : '',
                  ]
                    .filter(Boolean)
                    .join(' + ') || '-'}
                </td>
                <td>{item.model_name}</td>
                <td>
                  <StatusTag status={item.enabled ? 'enabled' : 'disabled'} />
                </td>
                <td>
                  {item.id === activeVisionProviderId ? t('providers.vision_label') : ''}
                  {item.id === activeVisionProviderId && item.id === activeQaProviderId ? ' + ' : ''}
                  {item.id === activeQaProviderId ? t('providers.qa_label') : ''}
                  {item.id !== activeVisionProviderId && item.id !== activeQaProviderId ? '-' : ''}
                </td>
                <td>{item.last_test_status ?? '-'}</td>
                <td>
                  <StatusTag status={item.availability_status} />
                  <div className="text-muted">{item.availability_message || '-'}</div>
                </td>
                <td>{providerTokenMap.get(item.id) ?? 0}</td>
                <td>
                  <div className="row-actions">
                    <button className="ghost" onClick={() => setEditing(item)}>
                      {t('providers.action_edit')}
                    </button>
                    <button
                      className={item.id === activeVisionProviderId ? 'ghost role-action-active' : 'ghost'}
                      disabled={!item.supports_vision || defaultVisionMutation.isPending}
                      onClick={() => defaultVisionMutation.mutate(item.id)}
                    >
                      {t('providers.set_vision')}
                    </button>
                    <button
                      className={item.id === activeQaProviderId ? 'ghost role-action-active' : 'ghost'}
                      disabled={!item.supports_qa || defaultQaMutation.isPending}
                      onClick={() => defaultQaMutation.mutate(item.id)}
                    >
                      {t('providers.set_qa')}
                    </button>
                    <button className="ghost" onClick={() => testMutation.mutate(item.id)}>
                      {t('providers.test_button')}
                    </button>
                    <button
                      className="ghost"
                      disabled={deleteMutation.isPending}
                      onClick={() => {
                        if (window.confirm(t('providers.delete_confirm', { name: item.provider_name }))) {
                          deleteMutation.mutate(item.id)
                        }
                      }}
                    >
                      {t('providers.action_delete')}
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
            <h3>{editing ? t('providers.edit_provider') : t('providers.add_provider')}</h3>
            <ProviderForm
              initialValue={editing ?? undefined}
              pending={createMutation.isPending || updateMutation.isPending}
              onCancel={() => (editing ? setEditing(null) : setShowCreate(false))}
              onSubmit={(payload) => {
                if (editing) {
                  updateMutation.mutate({ id: editing.id, payload: payload as ProviderUpdate })
                } else {
                  createMutation.mutate(payload as ProviderCreate)
                }
              }}
            />
          </div>
        </div>
      )}
    </div>
  )
}
