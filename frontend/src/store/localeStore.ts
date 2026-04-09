import { create } from 'zustand'
import i18n from '../i18n/config'

export type LocaleMode = 'zh-CN' | 'en-US'

const LOCALE_KEY = 'i18nextLng'

type LocaleState = {
  locale: LocaleMode
  setLocale: (locale: LocaleMode) => void
  loadFromStorage: () => void
}

function isLocaleMode(value: string | null): value is LocaleMode {
  return value === 'zh-CN' || value === 'en-US'
}

export const useLocaleStore = create<LocaleState>((set) => ({
  locale: 'zh-CN',
  setLocale: (locale) => {
    localStorage.setItem(LOCALE_KEY, locale)
    i18n.changeLanguage(locale)
    set({ locale })
  },
  loadFromStorage: () => {
    const locale = localStorage.getItem(LOCALE_KEY)
    if (isLocaleMode(locale)) {
      set({ locale })
      i18n.changeLanguage(locale)
      return
    }
    const detectedLng = i18n.language
    if (isLocaleMode(detectedLng)) {
       set({ locale: detectedLng })
       return
    }
    set({ locale: 'zh-CN' })
    i18n.changeLanguage('zh-CN')
  },
}))
