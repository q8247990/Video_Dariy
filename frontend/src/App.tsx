import { useEffect } from 'react'
import { AppProviders } from './app/providers'
import { AppRouter } from './app/router'
import { useLocaleStore } from './store/localeStore'

function App() {
  const loadFromStorage = useLocaleStore((state) => state.loadFromStorage)
  
  useEffect(() => {
    loadFromStorage()
  }, [loadFromStorage])

  return (
    <AppProviders>
      <AppRouter />
    </AppProviders>
  )
}

export default App
