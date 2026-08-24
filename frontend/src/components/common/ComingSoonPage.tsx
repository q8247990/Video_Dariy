import { useTranslation } from 'react-i18next'
import { PageHeader } from './PageHeader'

type ComingSoonPageProps = {
  title: string
  subtitle: string
}

export function ComingSoonPage({ title, subtitle }: ComingSoonPageProps) {
  const { t } = useTranslation()
  return (
    <div>
      <PageHeader title={title} subtitle={subtitle} />
      <div className="card coming-soon">
        <p>{t('common.coming_soon_subtitle')}</p>
      </div>
    </div>
  )
}
