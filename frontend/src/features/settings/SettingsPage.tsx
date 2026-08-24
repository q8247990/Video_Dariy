import { useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { PageHeader } from '../../components/common/PageHeader'
import { getHomeProfile } from '../home-profile/api'
import { useThemeStore } from '../../store/themeStore'
import { useLocaleStore } from '../../store/localeStore'

type SettingEntry = {
  key: string
  titleKey: string
  descriptionKey: string
  target: string
}

export function SettingsPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const theme = useThemeStore((state) => state.theme)
  const setTheme = useThemeStore((state) => state.setTheme)
  const locale = useLocaleStore((state) => state.locale)
  const setLocale = useLocaleStore((state) => state.setLocale)

  const homeProfileQuery = useQuery({
    queryKey: ['home-profile'],
    queryFn: getHomeProfile,
  })

  const tangtangEnabled = useMemo(() => {
    const name = homeProfileQuery.data?.assistant_name?.trim() ?? ''
    return name === '小糖' || name === '糖糖'
  }, [homeProfileQuery.data?.assistant_name])

  useEffect(() => {
    if (!homeProfileQuery.isSuccess) {
      return
    }
    if (!tangtangEnabled && theme === 'tangtang') {
      setTheme('light')
    }
  }, [homeProfileQuery.isSuccess, setTheme, tangtangEnabled, theme])

  const commonEntries: SettingEntry[] = [
    {
      key: 'daily-summary',
      titleKey: 'settings.entry_daily_summary_title',
      descriptionKey: 'settings.entry_daily_summary_desc',
      target: '/system-config',
    },
    {
      key: 'onboarding',
      titleKey: 'settings.entry_onboarding_title',
      descriptionKey: 'settings.entry_onboarding_desc',
      target: '/onboarding',
    },
    {
      key: 'chat',
      titleKey: 'settings.entry_chat_title',
      descriptionKey: 'settings.entry_chat_desc',
      target: '/chat',
    },
  ]

  const advancedEntries: SettingEntry[] = [
    {
      key: 'video-sources',
      titleKey: 'settings.entry_video_sources_title',
      descriptionKey: 'settings.entry_video_sources_desc',
      target: '/video-sources',
    },
    {
      key: 'providers',
      titleKey: 'settings.entry_providers_title',
      descriptionKey: 'settings.entry_providers_desc',
      target: '/providers',
    },
    {
      key: 'system-status',
      titleKey: 'settings.entry_system_status_title',
      descriptionKey: 'settings.entry_system_status_desc',
      target: '/system-status',
    },
    {
      key: 'tasks',
      titleKey: 'settings.entry_tasks_title',
      descriptionKey: 'settings.entry_tasks_desc',
      target: '/tasks',
    },
    {
      key: 'webhooks',
      titleKey: 'settings.entry_webhooks_title',
      descriptionKey: 'settings.entry_webhooks_desc',
      target: '/webhooks',
    },
  ]

  return (
    <div>
      <PageHeader title={t('settings.title')} subtitle={t('settings.subtitle')} />

      <article className="card">
        <h3>{t('settings.common_section')}</h3>
        <div className="theme-card">
          <p className="text-muted">{t('settings.theme_title')}</p>
          <div className="theme-switcher">
            <button
              type="button"
              className={theme === 'light' ? 'ghost theme-btn theme-btn-active' : 'ghost theme-btn'}
              onClick={() => setTheme('light')}
            >
              {t('settings.theme_light')}
            </button>
            <button
              type="button"
              className={theme === 'dark' ? 'ghost theme-btn theme-btn-active' : 'ghost theme-btn'}
              onClick={() => setTheme('dark')}
            >
              {t('settings.theme_dark')}
            </button>
            {tangtangEnabled ? (
              <button
                type="button"
                className={theme === 'tangtang' ? 'ghost theme-btn theme-btn-active' : 'ghost theme-btn'}
                onClick={() => setTheme('tangtang')}
              >
                {t('settings.theme_tangtang')}
              </button>
            ) : null}
          </div>
        </div>

        <div className="theme-card settings-block-gap">
          <p className="text-muted">{t('settings.language_title')}</p>
          <div className="theme-switcher">
            <button
              type="button"
              className={locale === 'zh-CN' ? 'ghost theme-btn theme-btn-active' : 'ghost theme-btn'}
              onClick={() => setLocale('zh-CN')}
            >
              {t('settings.language_zh')}
            </button>
            <button
              type="button"
              className={locale === 'en-US' ? 'ghost theme-btn theme-btn-active' : 'ghost theme-btn'}
              onClick={() => setLocale('en-US')}
            >
              {t('settings.language_en')}
            </button>
          </div>
        </div>

        <div className="settings-grid">
          {commonEntries.map((entry) => (
            <button
              key={entry.key}
              type="button"
              className="ghost settings-entry"
              onClick={() => navigate(entry.target)}
            >
              <strong>{t(entry.titleKey)}</strong>
              <span>{t(entry.descriptionKey)}</span>
            </button>
          ))}
        </div>
      </article>

      <article className="card settings-block-gap">
        <h3>{t('settings.advanced_section')}</h3>
        <p className="text-muted">{t('settings.advanced_section_desc')}</p>
        <div className="settings-grid">
          {advancedEntries.map((entry) => (
            <button
              key={entry.key}
              type="button"
              className="ghost settings-entry"
              onClick={() => navigate(entry.target)}
            >
              <strong>{t(entry.titleKey)}</strong>
              <span>{t(entry.descriptionKey)}</span>
            </button>
          ))}
        </div>
      </article>
    </div>
  )
}
