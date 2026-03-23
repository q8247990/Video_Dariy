import { create } from 'zustand'

const STORAGE_KEY = 'onboarding_draft_v1'

export type VideoDraft = {
  source_name: string
  camera_name: string
  location_name: string
  root_path: string
  source_id: number | null
  validated: boolean
}

export type ProviderDraft = {
  api_base_url: string
  api_key: string
  model_name: string
  provider_id: number | null
  tested: boolean
  skipped: boolean
}

export type SummaryDraft = {
  daily_summary_schedule: string
}

export type HomeProfileDraft = {
  home_name: string
  family_tags: string[]
  focus_points: string[]
  home_note: string
  system_style: string
  assistant_name: string
  style_preference_text: string
}

export type OnboardingDraftState = {
  video: VideoDraft
  provider: ProviderDraft
  summary: SummaryDraft
  homeProfile: HomeProfileDraft
  cameraNotes: Record<number, string>
  setVideo: (patch: Partial<VideoDraft>) => void
  setProvider: (patch: Partial<ProviderDraft>) => void
  setSummary: (patch: Partial<SummaryDraft>) => void
  setHomeProfile: (patch: Partial<HomeProfileDraft>) => void
  setCameraNote: (sourceId: number, text: string) => void
  hydrate: () => void
  clear: () => void
}

const defaultState = {
  video: {
    source_name: '',
    camera_name: '',
    location_name: '',
    root_path: '',
    source_id: null,
    validated: false,
  },
  provider: {
    api_base_url: '',
    api_key: '',
    model_name: '',
    provider_id: null,
    tested: false,
    skipped: false,
  },
  summary: {
    daily_summary_schedule: '10:00',
  },
  homeProfile: {
    home_name: '我的家庭',
    family_tags: [],
    focus_points: [],
    home_note: '',
    system_style: 'family_companion',
    assistant_name: '家庭助手',
    style_preference_text: '',
  },
  cameraNotes: {},
}

function saveToStorage(state: Omit<OnboardingDraftState, 'setVideo' | 'setProvider' | 'setSummary' | 'setHomeProfile' | 'setCameraNote' | 'hydrate' | 'clear'>): void {
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      video: state.video,
      provider: state.provider,
      summary: state.summary,
      homeProfile: state.homeProfile,
      cameraNotes: state.cameraNotes,
    }),
  )
}

export const useOnboardingDraftStore = create<OnboardingDraftState>((set) => ({
  ...defaultState,
  setVideo: (patch) => {
    set((state) => {
      const next = { ...state.video, ...patch }
      const newState = { ...state, video: next }
      saveToStorage(newState)
      return { video: next }
    })
  },
  setProvider: (patch) => {
    set((state) => {
      const next = { ...state.provider, ...patch }
      const newState = { ...state, provider: next }
      saveToStorage(newState)
      return { provider: next }
    })
  },
  setSummary: (patch) => {
    set((state) => {
      const next = { ...state.summary, ...patch }
      const newState = { ...state, summary: next }
      saveToStorage(newState)
      return { summary: next }
    })
  },
  setHomeProfile: (patch) => {
    set((state) => {
      const next = { ...state.homeProfile, ...patch }
      const newState = { ...state, homeProfile: next }
      saveToStorage(newState)
      return { homeProfile: next }
    })
  },
  setCameraNote: (sourceId, text) => {
    set((state) => {
      const cameraNotes = { ...state.cameraNotes, [sourceId]: text }
      const newState = { ...state, cameraNotes }
      saveToStorage(newState)
      return { cameraNotes }
    })
  },
  hydrate: () => {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) {
      return
    }
    try {
      const parsed = JSON.parse(raw) as Partial<typeof defaultState>
      set({
        video: { ...defaultState.video, ...(parsed.video ?? {}) },
        provider: { ...defaultState.provider, ...(parsed.provider ?? {}) },
        summary: { ...defaultState.summary, ...(parsed.summary ?? {}) },
        homeProfile: { ...defaultState.homeProfile, ...(parsed.homeProfile ?? {}) },
        cameraNotes: parsed.cameraNotes ?? {},
      })
    } catch {
      localStorage.removeItem(STORAGE_KEY)
    }
  },
  clear: () => {
    localStorage.removeItem(STORAGE_KEY)
    set(defaultState)
  },
}))

export function resetOnboardingDraft(): void {
  useOnboardingDraftStore.getState().clear()
}
