import type { ThemeMode } from '../store/themeStore'

export function applyTheme(mode: ThemeMode): void {
  document.documentElement.setAttribute('data-theme', mode)
}
