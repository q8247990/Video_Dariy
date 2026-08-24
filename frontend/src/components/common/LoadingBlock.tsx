import { useTranslation } from 'react-i18next'

type LoadingBlockProps = {
  text?: string
}

export function LoadingBlock({ text }: LoadingBlockProps) {
  const { t } = useTranslation()
  return (
    <div className="loading-block">
      <span className="loader" />
      <span>{text ?? t('common.loading')}</span>
    </div>
  )
}
