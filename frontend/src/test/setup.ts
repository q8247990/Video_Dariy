import { afterEach, beforeEach } from 'vitest'

// 每个用例前后清空 Web Storage，保证测试之间的 localStorage/sessionStorage 相互隔离
beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
})

afterEach(() => {
  localStorage.clear()
  sessionStorage.clear()
})
