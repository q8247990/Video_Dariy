import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
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
      setMessage('Provider 创建成功')
      queryClient.invalidateQueries({ queryKey: ['providers'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: ProviderUpdate }) => updateProvider(id, payload),
    onSuccess: () => {
      setEditing(null)
      setMessage('Provider 更新成功')
      queryClient.invalidateQueries({ queryKey: ['providers'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const defaultVisionMutation = useMutation({
    mutationFn: setDefaultVisionProvider,
    onSuccess: () => {
      setMessage('视觉 LLM 已更新')
      queryClient.invalidateQueries({ queryKey: ['providers'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const defaultQaMutation = useMutation({
    mutationFn: setDefaultQaProvider,
    onSuccess: () => {
      setMessage('问答 LLM 已更新')
      queryClient.invalidateQueries({ queryKey: ['providers'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const testMutation = useMutation({
    mutationFn: testProvider,
    onSuccess: () => {
      setMessage('连通性测试任务已触发')
      queryClient.invalidateQueries({ queryKey: ['providers'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const deleteMutation = useMutation({
    mutationFn: deleteProvider,
    onSuccess: () => {
      setMessage('Provider 删除成功')
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
      result.set(item.provider_id, item.total_tokens)
    }
    return result
  }, [latestUsageDay])

  if (listQuery.isLoading) {
    return <LoadingBlock text="加载 Provider 中" />
  }

  if (listQuery.error) {
    return <ApiErrorAlert message={(listQuery.error as Error).message} />
  }

  return (
    <div>
      <PageHeader
        title="Provider 管理"
        subtitle="统一管理视觉与问答模型配置"
        actions={
          <button onClick={() => setShowCreate(true)}>新增 Provider</button>
        }
      />

      <div className="card tool-row tool-row-inline">
        <label>
          类型筛选
          <select value={providerType} onChange={(event) => setProviderType(event.target.value)}>
            <option value="">全部</option>
            <option value="vision_provider">视觉模型</option>
            <option value="qa_provider">问答模型</option>
          </select>
        </label>
      </div>

      <div className="card">
        <div>
          <strong>近7天Token统计</strong>
        </div>
        {usageQuery.isLoading ? (
          <div>统计中...</div>
        ) : usageQuery.error ? (
          <div className="api-error">{(usageQuery.error as Error).message}</div>
        ) : latestUsageDay ? (
          <div>
            日期 {latestUsageDay.date} | 总token {latestUsageDay.total_tokens}（prompt{' '}
            {latestUsageDay.prompt_tokens} / completion {latestUsageDay.completion_tokens}）
          </div>
        ) : (
          <div>暂无token统计数据</div>
        )}
      </div>

      {message ? <div className="api-ok">{message}</div> : null}

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>ID</th>
              <th>名称</th>
              <th>能力</th>
              <th>模型</th>
              <th>状态</th>
              <th>当前使用</th>
              <th>最近测试</th>
              <th>可用性</th>
              <th>今日Token</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((item) => (
              <tr key={item.id}>
                <td>{item.id}</td>
                <td>{item.provider_name}</td>
                <td>
                  {item.supports_vision ? '视觉' : ''}
                  {item.supports_vision && item.supports_qa ? ' + ' : ''}
                  {item.supports_qa ? '问答' : ''}
                  {!item.supports_vision && !item.supports_qa ? '-' : ''}
                </td>
                <td>{item.model_name}</td>
                <td>
                  <StatusTag status={item.enabled ? 'enabled' : 'disabled'} />
                </td>
                <td>
                  {item.id === activeVisionProviderId ? '视觉' : ''}
                  {item.id === activeVisionProviderId && item.id === activeQaProviderId ? ' + ' : ''}
                  {item.id === activeQaProviderId ? '问答' : ''}
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
                      编辑
                    </button>
                    <button
                      className={item.id === activeVisionProviderId ? 'ghost role-action-active' : 'ghost'}
                      disabled={!item.supports_vision || defaultVisionMutation.isPending}
                      onClick={() => defaultVisionMutation.mutate(item.id)}
                    >
                      设为视觉LLM
                    </button>
                    <button
                      className={item.id === activeQaProviderId ? 'ghost role-action-active' : 'ghost'}
                      disabled={!item.supports_qa || defaultQaMutation.isPending}
                      onClick={() => defaultQaMutation.mutate(item.id)}
                    >
                      设为问答LLM
                    </button>
                    <button className="ghost" onClick={() => testMutation.mutate(item.id)}>
                      测试
                    </button>
                    <button
                      className="ghost"
                      disabled={deleteMutation.isPending}
                      onClick={() => {
                        if (window.confirm(`确认删除 Provider ${item.provider_name} 吗？`)) {
                          deleteMutation.mutate(item.id)
                        }
                      }}
                    >
                      删除
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
            <h3>{editing ? '编辑 Provider' : '新增 Provider'}</h3>
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
