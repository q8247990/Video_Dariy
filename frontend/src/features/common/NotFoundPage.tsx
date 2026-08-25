import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

export function NotFoundPage() {
  const { t } = useTranslation()

  return (
    <main className="auth-page">
      <section className="auth-card">
        <h1>{t('common.not_found_title')}</h1>
        <p>{t('common.not_found_message')}</p>
        <Link className="ghost" to="/dashboard">
          {t('common.not_found_back')}
        </Link>
      </section>
    </main>
  )
}
