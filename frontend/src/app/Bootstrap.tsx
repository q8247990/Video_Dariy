import { useEffect } from 'react'
import { useAuthStore } from '../store/authStore'
import { useLocaleStore } from '../store/localeStore'
import { useThemeStore } from '../store/themeStore'
import { applyTheme } from './theme'

export function Bootstrap() {
  const loadAuth = useAuthStore((state) => state.loadFromStorage)
  const loadLocale = useLocaleStore((state) => state.loadFromStorage)
  const theme = useThemeStore((state) => state.theme)
  const loadTheme = useThemeStore((state) => state.loadFromStorage)

  useEffect(() => {
    loadAuth()
    loadLocale()
    loadTheme()
  }, [loadAuth, loadLocale, loadTheme])

  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  return null
}
