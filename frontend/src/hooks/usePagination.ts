import { useCallback, useState } from 'react'

export type UsePaginationReturn = {
  page: number
  pageInput: string
  setPageInput: (value: string) => void
  jumpToPage: (totalPages: number) => void
  goToPrev: () => void
  goToNext: (totalPages: number) => void
  resetPage: () => void
}

export function usePagination(initialPage = 1): UsePaginationReturn {
  const [page, setPage] = useState(initialPage)
  const [pageInput, setPageInput] = useState(String(initialPage))

  const jumpToPage = useCallback(
    (totalPages: number) => {
      const nextPage = Number(pageInput)
      if (!Number.isFinite(nextPage)) {
        setPageInput(String(page))
        return
      }
      const normalizedPage = Math.min(totalPages, Math.max(1, Math.trunc(nextPage)))
      setPage(normalizedPage)
      setPageInput(String(normalizedPage))
    },
    [page, pageInput],
  )

  const goToPrev = useCallback(() => {
    const nextPage = Math.max(1, page - 1)
    setPage(nextPage)
    setPageInput(String(nextPage))
  }, [page])

  const goToNext = useCallback(
    (totalPages: number) => {
      const nextPage = Math.min(totalPages, page + 1)
      setPage(nextPage)
      setPageInput(String(nextPage))
    },
    [page],
  )

  const resetPage = useCallback(() => {
    setPage(1)
    setPageInput('1')
  }, [])

  return { page, pageInput, setPageInput, jumpToPage, goToPrev, goToNext, resetPage }
}
