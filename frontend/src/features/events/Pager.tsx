import { useTranslation } from 'react-i18next'

type PagerProps = {
  page: number
  totalPages: number
  total: number
  pageInput: string
  onPageInputChange: (value: string) => void
  onJumpToPage: () => void
  onPrev: () => void
  onNext: () => void
}

export function Pager({
  page,
  totalPages,
  total,
  pageInput,
  onPageInputChange,
  onJumpToPage,
  onPrev,
  onNext,
}: PagerProps) {
  const { t } = useTranslation()
  return (
    <div className="pager">
      <button className="ghost" disabled={page <= 1} onClick={onPrev}>
        {t('events.pager_prev', '上一页')}
      </button>
      <span>
        {t('events.pager_info', '第 {{page}} / {{totalPages}} 页，共 {{total}} 条', { page, totalPages, total })}
      </span>
      <input
        className="pager-input"
        type="number"
        min={1}
        max={totalPages}
        value={pageInput}
        onChange={(event) => onPageInputChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            onJumpToPage()
          }
        }}
      />
      <button className="ghost" onClick={onJumpToPage}>
        {t('events.pager_jump', '跳转')}
      </button>
      <button className="ghost" disabled={page >= totalPages} onClick={onNext}>
        {t('events.pager_next', '下一页')}
      </button>
    </div>
  )
}
