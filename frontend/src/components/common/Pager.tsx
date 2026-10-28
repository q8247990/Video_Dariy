import { useTranslation } from 'react-i18next'

type PagerProps = {
  page: number
  totalPages: number
  total: number
  onPrev: () => void
  onNext: () => void
  pageInput?: string
  onPageInputChange?: (value: string) => void
  onJumpToPage?: () => void
}

export function Pager({
  page,
  totalPages,
  total,
  onPrev,
  onNext,
  pageInput,
  onPageInputChange,
  onJumpToPage,
}: PagerProps) {
  const { t } = useTranslation()
  const canJump =
    pageInput !== undefined && onPageInputChange !== undefined && onJumpToPage !== undefined
  return (
    <div className="pager">
      <button className="ghost" disabled={page <= 1} onClick={onPrev}>
        {t('common.pager_prev', '上一页')}
      </button>
      <span>
        {t('common.pager_info', '第 {{page}} / {{totalPages}} 页，共 {{total}} 条', {
          page,
          totalPages,
          total,
        })}
      </span>
      {canJump ? (
        <>
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
            {t('common.pager_jump', '跳转')}
          </button>
        </>
      ) : null}
      <button className="ghost" disabled={page >= totalPages} onClick={onNext}>
        {t('common.pager_next', '下一页')}
      </button>
    </div>
  )
}
