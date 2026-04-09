import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { PageHeader } from '../../components/common/PageHeader'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { StatusTag } from '../../components/common/StatusTag'
import type { WebhookConfig, WebhookCreate, WebhookUpdate } from '../../types/api'
import { createWebhook, deleteWebhook, getWebhooks, testWebhook, updateWebhook } from './api'
import { WebhookForm } from './WebhookForm'

const webhookDailySummarySample = {
  event: 'daily_summary_generated',
  version: '1.0',
  generated_at: '2026-03-14T10:00:03+08:00',
  data: {
    date: '2026-03-13',
    summary_title: '2026-03-13 家庭日报',
    overall_summary: '昨天家中整体平稳，爸爸在客厅有活动，门口出现一次短暂停留。',
    subject_sections: [
      {
        subject_name: '爸爸',
        subject_type: 'member',
        summary: '上午和下午均在客厅出现，有短时停留与移动。',
        attention_needed: false,
      },
      {
        subject_name: '布丁',
        subject_type: 'pet',
        summary: '中午在客厅和阳台间活动，整体状态正常。',
        attention_needed: false,
      },
    ],
    attention_items: [
      {
        title: '门口短暂停留',
        summary: '16:20 门口出现未知人员短暂停留，建议继续关注。',
        level: 'medium',
      },
    ],
    event_count: 12,
  },
}

const mcpDailySummarySample = {
  date: '2026-03-13',
  summary_title: '2026-03-13 家庭日报',
  overall_summary: '昨天家中整体平稳，爸爸在客厅有活动，门口出现一次短暂停留。',
  subject_sections: [
    {
      subject_name: '爸爸',
      subject_type: 'member',
      summary: '上午和下午均在客厅出现，有短时停留与移动。',
      attention_needed: false,
    },
  ],
  attention_items: [
    {
      title: '门口短暂停留',
      summary: '16:20 门口出现未知人员短暂停留，建议继续关注。',
      level: 'medium',
    },
  ],
  event_count: 12,
  generated_at: '2026-03-14T10:00:03+08:00',
}

export function WebhooksPage() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [showCreate, setShowCreate] = useState(false)
  const [editing, setEditing] = useState<WebhookConfig | null>(null)
  const [message, setMessage] = useState('')

  const listQuery = useQuery({
    queryKey: ['webhooks', { page: 1 }],
    queryFn: () => getWebhooks(1, 50),
  })

  const createMutation = useMutation({
    mutationFn: createWebhook,
    onSuccess: () => {
      setShowCreate(false)
      setMessage('Webhook 创建成功')
      queryClient.invalidateQueries({ queryKey: ['webhooks'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: WebhookUpdate }) => updateWebhook(id, payload),
    onSuccess: () => {
      setEditing(null)
      setMessage('Webhook 更新成功')
      queryClient.invalidateQueries({ queryKey: ['webhooks'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const deleteMutation = useMutation({
    mutationFn: deleteWebhook,
    onSuccess: () => {
      setMessage('Webhook 删除成功')
      queryClient.invalidateQueries({ queryKey: ['webhooks'] })
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const testMutation = useMutation({
    mutationFn: testWebhook,
    onSuccess: (data) => setMessage(data.message || '测试任务已触发'),
    onError: (error) => setMessage((error as Error).message),
  })

  if (listQuery.isLoading) {
    return <LoadingBlock text="加载 Webhook 配置中" />
  }

  if (listQuery.error) {
    return <ApiErrorAlert message={(listQuery.error as Error).message} />
  }

  const rows = listQuery.data?.list ?? []

  const formatSubscriptions = (item: WebhookConfig): string => {
    const subscriptions = item.event_subscriptions_json ?? []
    const labels = subscriptions
      .map((rule) => {
        const event = (rule.event ?? '').trim()
        const version = (rule.version ?? '').trim()
        if (!event) {
          return ''
        }
        return version ? `${event}@${version}` : event
      })
      .filter((rule) => rule.length > 0)
    return labels.join(', ') || '-'
  }

  return (
    <div>
      <PageHeader
        title={t('webhooks.title')}
        subtitle="配置外部通知推送规则"
        actions={<button onClick={() => setShowCreate(true)}>{t('webhooks.add_webhook')}</button>}
      />

      {message ? <div className="api-ok">{message}</div> : null}

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>ID</th>
              <th>名称</th>
              <th>URL</th>
              <th>事件类型</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((item) => (
              <tr key={item.id}>
                <td>{item.id}</td>
                <td>{item.name}</td>
                <td className="break-all">{item.url}</td>
                <td>{formatSubscriptions(item)}</td>
                <td>
                  <StatusTag status={item.enabled ? 'enabled' : 'disabled'} />
                </td>
                <td>
                  <div className="row-actions">
                    <button className="ghost" onClick={() => setEditing(item)}>
                      编辑
                    </button>
                    <button className="ghost" onClick={() => testMutation.mutate(item.id)}>
                      测试
                    </button>
                    <button
                      className="ghost"
                      onClick={() => {
                        if (window.confirm(`确认删除 Webhook「${item.name}」吗？`)) {
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
            {rows.length === 0 ? (
              <tr>
                <td colSpan={6} className="empty-cell">
                  暂无 Webhook 配置
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <div className="grid-two debug-sample-grid">
        <article className="card debug-sample-card">
          <h3>Webhook 日报样例（结构化）</h3>
          <p className="text-muted">事件类型：daily_summary_generated</p>
          <pre>{JSON.stringify(webhookDailySummarySample, null, 2)}</pre>
        </article>

        <article className="card debug-sample-card">
          <h3>MCP get_daily_summary 样例</h3>
          <p className="text-muted">用于 MCP 客户端联调结构化日报字段</p>
          <pre>{JSON.stringify(mcpDailySummarySample, null, 2)}</pre>
        </article>
      </div>

      {(showCreate || editing) && (
        <div className="dialog-mask" onClick={() => (showCreate ? setShowCreate(false) : setEditing(null))}>
          <div className="dialog" onClick={(event) => event.stopPropagation()}>
            <h3>{editing ? t('webhooks.edit_webhook') : t('webhooks.add_webhook')}</h3>
            <WebhookForm
              initialValue={editing ?? undefined}
              pending={
                createMutation.isPending ||
                updateMutation.isPending ||
                deleteMutation.isPending ||
                testMutation.isPending
              }
              onCancel={() => (editing ? setEditing(null) : setShowCreate(false))}
              onSubmit={(payload) => {
                if (editing) {
                  updateMutation.mutate({ id: editing.id, payload: payload as WebhookUpdate })
                } else {
                  createMutation.mutate(payload as WebhookCreate)
                }
              }}
            />
          </div>
        </div>
      )}
    </div>
  )
}
