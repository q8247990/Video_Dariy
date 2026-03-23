import { useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import type {
  WebhookConfig,
  WebhookCreate,
  WebhookSubscriptionRule,
  WebhookUpdate,
} from '../../types/api'

type WebhookFormProps = {
  initialValue?: WebhookConfig
  pending?: boolean
  onCancel: () => void
  onSubmit: (payload: WebhookCreate | WebhookUpdate) => void
}

type FormState = {
  name: string
  url: string
  headersText: string
  eventTypesText: string
  enabled: boolean
}

function stringifyHeaders(value: Record<string, string> | null): string {
  if (!value) {
    return ''
  }
  return Object.entries(value)
    .map(([key, val]) => `${key}: ${val}`)
    .join('\n')
}

function parseHeaders(text: string): Record<string, string> {
  const lines = text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)

  const result: Record<string, string> = {}
  for (const line of lines) {
    const index = line.indexOf(':')
    if (index <= 0) {
      continue
    }
    const key = line.slice(0, index).trim()
    const value = line.slice(index + 1).trim()
    if (key) {
      result[key] = value
    }
  }
  return result
}

function stringifySubscriptions(value: WebhookSubscriptionRule[] | null): string {
  if (!value) {
    return ''
  }

  return value
    .map((item) => {
      const event = item.event.trim()
      const version = item.version.trim()
      if (!event) {
        return ''
      }
      return version ? `${event}@${version}` : event
    })
    .filter(Boolean)
    .join(', ')
}

function parseSubscriptions(text: string): WebhookSubscriptionRule[] {
  return text
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
    .map((item) => {
      const [eventText, versionText] = item.split('@', 2)
      const event = (eventText ?? '').trim()
      const version = (versionText ?? '').trim()
      return { event, version }
    })
    .filter((item) => item.event.length > 0)
}

function getInitialState(initialValue?: WebhookConfig): FormState {
  return {
    name: initialValue?.name ?? '',
    url: initialValue?.url ?? '',
    headersText: stringifyHeaders(initialValue?.headers_json ?? null),
    eventTypesText: stringifySubscriptions(initialValue?.event_subscriptions_json ?? null),
    enabled: initialValue?.enabled ?? true,
  }
}

export function WebhookForm({ initialValue, pending, onCancel, onSubmit }: WebhookFormProps) {
  const [form, setForm] = useState<FormState>(() => getInitialState(initialValue))
  const submitLabel = useMemo(() => (initialValue ? '保存修改' : '创建 Webhook'), [initialValue])

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()

    const headers = parseHeaders(form.headersText)
    const subscriptions = parseSubscriptions(form.eventTypesText)

    if (initialValue) {
      const payload: WebhookUpdate = {
        name: form.name,
        url: form.url,
        headers_json: headers,
        event_subscriptions_json: subscriptions,
        enabled: form.enabled,
      }
      onSubmit(payload)
      return
    }

    const payload: WebhookCreate = {
      name: form.name,
      url: form.url,
      headers_json: headers,
      event_subscriptions_json: subscriptions,
      enabled: form.enabled,
    }
    onSubmit(payload)
  }

  return (
    <form className="dialog-form" onSubmit={handleSubmit}>
      <label>
        Webhook 名称
        <input
          required
          value={form.name}
          onChange={(event) => setForm((old) => ({ ...old, name: event.target.value }))}
        />
      </label>

      <label>
        回调地址 URL
        <input
          required
          value={form.url}
          onChange={(event) => setForm((old) => ({ ...old, url: event.target.value }))}
          placeholder="https://example.com/webhook"
        />
      </label>

      <label>
        订阅规则（event@version，逗号分隔）
        <input
          value={form.eventTypesText}
          onChange={(event) => setForm((old) => ({ ...old, eventTypesText: event.target.value }))}
          placeholder="all, daily_summary_generated@1.0, question_answered@1.0"
        />
      </label>

      <label>
        自定义请求头（每行 `key: value`）
        <textarea
          value={form.headersText}
          onChange={(event) => setForm((old) => ({ ...old, headersText: event.target.value }))}
          placeholder={'Authorization: Bearer xxx\nX-Source: video-diary'}
        />
      </label>

      <label className="checkbox-field">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(event) => setForm((old) => ({ ...old, enabled: event.target.checked }))}
        />
        启用该 Webhook
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
