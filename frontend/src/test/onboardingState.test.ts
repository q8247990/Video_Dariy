import { describe, expect, it } from 'vitest'
import { useOnboardingDraftStore } from '../features/onboarding/state'

describe('onboarding draft storage', () => {
  it('preserves provider connection details but never persists the API key', () => {
    useOnboardingDraftStore.getState().setProvider({
      api_base_url: 'https://example.test/v1',
      api_key: 'secret-provider-key',
      model_name: 'vision-model',
    })

    const stored = localStorage.getItem('onboarding_draft_v1')

    expect(stored).not.toContain('secret-provider-key')
    expect(stored).not.toContain('api_key')
    expect(stored).toContain('https://example.test/v1')
    expect(stored).toContain('vision-model')

    useOnboardingDraftStore.getState().hydrate()

    expect(useOnboardingDraftStore.getState().provider.api_key).toBe('')
  })
})
