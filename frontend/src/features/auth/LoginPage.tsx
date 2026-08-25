import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { login } from './api'
import { useAuthStore } from '../../store/authStore'

export function LoginPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const setAuth = useAuthStore((state) => state.setAuth)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')

  const mutation = useMutation({
    mutationFn: () => login(username.trim(), password),
    onSuccess: (data) => {
      setAuth(data.token, data.user.username)
      navigate('/dashboard', { replace: true })
    },
  })

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    mutation.mutate()
  }

  return (
    <div className="auth-page">
      <div className="auth-card">
        <h1>{t('login.subtitle')}</h1>
        <p>{t('login.logged_in_hint')}</p>
        <form onSubmit={handleSubmit}>
          <label>
            {t('login.username')}
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder={t('login.username_placeholder')}
              required
            />
          </label>
          <label>
            {t('login.password')}
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder={t('login.password_placeholder')}
              required
            />
          </label>
          {mutation.error ? <ApiErrorAlert message={mutation.error.message} /> : null}
          <button type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? t('login.loading') : t('login.submit')}
          </button>
        </form>
      </div>
    </div>
  )
}
