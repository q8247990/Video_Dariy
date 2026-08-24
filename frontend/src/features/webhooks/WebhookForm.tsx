import { useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
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
  const { t } = useTranslation()
  const [form, setForm] = useState<FormState>(() => getInitialState(initialValue))
  const submitLabel = useMemo(
    () => (initialValue ? t('webhooks.form_save_changes') : t('webhooks.form_create')),
    [initialValue, t],
  )

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
        {t('webhooks.form_name_label')}
        <input
          required
          value={form.name}
          onChange={(event) => setForm((old) => ({ ...old, name: event.target.value }))}
        />
      </label>

      <label>
        {t('webhooks.form_callback_url')}
        <input
          required
          value={form.url}
          onChange={(event) => setForm((old) => ({ ...old, url: event.target.value }))}
          placeholder={t('webhooks.form_url_placeholder')}
        />
      </label>

      <label>
        {t('webhooks.form_event_filter')}
        <input
          value={form.eventTypesText}
          onChange={(event) => setForm((old) => ({ ...old, eventTypesText: event.target.value }))}
          placeholder="all, daily_summary_generated@1.0, question_answered@1.0"
        />
      </label>

      <label>
        {t('webhooks.form_headers_label')}
        <textarea
          value={form.headersText}
          onChange={(event) => setForm((old) => ({ ...old, headersText: event.target.value }))}
          placeholder={t('webhooks.form_headers_placeholder')}
        />
      </label>

      <label className="checkbox-field">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(event) => setForm((old) => ({ ...old, enabled: event.target.checked }))}
        />
        {t('webhooks.form_enabled_label')}
      </label>

      <div className="dialog-actions">
        <button type="button" className="ghost" onClick={onCancel}>
          {t('webhooks.form_cancel')}
        </button>
        <button type="submit" disabled={pending}>
          {pending ? t('webhooks.form_submitting') : submitLabel}
        </button>
      </div>
    </form>
  )
}
