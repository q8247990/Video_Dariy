import { create } from 'zustand'

export type ThemeMode = 'light' | 'dark' | 'tangtang'

const THEME_KEY = 'hm_theme'

type ThemeState = {
  theme: ThemeMode
  setTheme: (theme: ThemeMode) => void
  loadFromStorage: () => void
}

function isThemeMode(value: string | null): value is ThemeMode {
  return value === 'light' || value === 'dark' || value === 'tangtang'
}

export const useThemeStore = create<ThemeState>((set) => ({
  theme: 'light',
  setTheme: (theme) => {
    localStorage.setItem(THEME_KEY, theme)
    set({ theme })
  },
  loadFromStorage: () => {
    const theme = localStorage.getItem(THEME_KEY)
    if (isThemeMode(theme)) {
      set({ theme })
      return
    }
    set({ theme: 'light' })
  },
}))
