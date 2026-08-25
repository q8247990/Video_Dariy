import { beforeEach, describe, expect, it, vi } from 'vitest'

const replace = vi.fn()
const clearAuth = vi.fn()
const clearQueries = vi.fn()

vi.mock('../lib/queryClient', () => ({
  queryClient: { clear: clearQueries },
}))

vi.mock('../store/authStore', () => ({
  useAuthStore: {
    getState: () => ({ clearAuth }),
  },
}))

describe('recoverFromUnauthorized', () => {
  beforeEach(() => {
    replace.mockReset()
    clearAuth.mockReset()
    clearQueries.mockReset()
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { pathname: '/dashboard', replace },
    })
  })

  it('clears protected state and redirects only once for concurrent failures', async () => {
    const { recoverFromUnauthorized, resetAuthenticationRecoveryForTests } =
      await import('../lib/authRecovery')
    resetAuthenticationRecoveryForTests()

    recoverFromUnauthorized()
    recoverFromUnauthorized()

    expect(clearAuth).toHaveBeenCalledTimes(1)
    expect(clearQueries).toHaveBeenCalledTimes(1)
    expect(replace).toHaveBeenCalledTimes(1)
    expect(replace).toHaveBeenCalledWith('/login')
  })
})
