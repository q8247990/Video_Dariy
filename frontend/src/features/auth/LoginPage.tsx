import { useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { login } from './api'
import { useAuthStore } from '../../store/authStore'

export function LoginPage() {
  const navigate = useNavigate()
  const setAuth = useAuthStore((state) => state.setAuth)
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('123456')

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
        <h1>家庭监控智能分析后台</h1>
        <p>登录后可管理视频源、模型与任务流程</p>
        <form onSubmit={handleSubmit}>
          <label>
            用户名
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="请输入管理员账号"
              required
            />
          </label>
          <label>
            密码
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="请输入密码"
              required
            />
          </label>
          {mutation.error ? <ApiErrorAlert message={mutation.error.message} /> : null}
          <button type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? '登录中...' : '登录系统'}
          </button>
        </form>
      </div>
    </div>
  )
}
