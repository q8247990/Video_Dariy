import { create } from 'zustand'

const TOKEN_KEY = 'hm_token'
const USERNAME_KEY = 'hm_username'

type AuthState = {
  token: string | null
  username: string | null
  bootstrapped: boolean
  setAuth: (token: string, username: string) => void
  clearAuth: () => void
  loadFromStorage: () => void
}

export const useAuthStore = create<AuthState>((set) => ({
  token: null,
  username: null,
  bootstrapped: false,
  setAuth: (token, username) => {
    localStorage.setItem(TOKEN_KEY, token)
    localStorage.setItem(USERNAME_KEY, username)
    set({ token, username, bootstrapped: true })
  },
  clearAuth: () => {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USERNAME_KEY)
    set({ token: null, username: null, bootstrapped: true })
  },
  loadFromStorage: () => {
    const token = localStorage.getItem(TOKEN_KEY)
    const username = localStorage.getItem(USERNAME_KEY)
    set({ token, username, bootstrapped: true })
  },
}))
