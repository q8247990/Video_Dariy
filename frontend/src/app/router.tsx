import { Suspense, lazy, useEffect } from 'react'
import type { ReactElement } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AuthLayout } from '../layouts/AuthLayout'
import { DashboardLayout } from '../layouts/DashboardLayout'
import { LoadingBlock } from '../components/common/LoadingBlock'
import { applyTheme } from './theme'
const LoginPage = lazy(() => import('../features/auth/LoginPage').then((m) => ({ default: m.LoginPage })))
const DashboardPage = lazy(() =>
  import('../features/dashboard/DashboardPage').then((m) => ({ default: m.DashboardPage }))
)
const VideoSourcesPage = lazy(() =>
  import('../features/video-sources/VideoSourcesPage').then((m) => ({ default: m.VideoSourcesPage }))
)
const ProvidersPage = lazy(() =>
  import('../features/providers/ProvidersPage').then((m) => ({ default: m.ProvidersPage }))
)
const EventsPage = lazy(() =>
  import('../features/events/EventsPage').then((m) => ({ default: m.EventsPage }))
)
const EventDetailPage = lazy(() =>
  import('../features/events/EventDetailPage').then((m) => ({ default: m.EventDetailPage }))
)
const SessionsPage = lazy(() =>
  import('../features/sessions/SessionsPage').then((m) => ({ default: m.SessionsPage }))
)
const ChatPage = lazy(() => import('../features/chat/ChatPage').then((m) => ({ default: m.ChatPage })))
const WebhooksPage = lazy(() =>
  import('../features/webhooks/WebhooksPage').then((m) => ({ default: m.WebhooksPage }))
)
const DailySummariesPage = lazy(() =>
  import('../features/daily-summaries/DailySummariesPage').then((m) => ({ default: m.DailySummariesPage }))
)
const SystemConfigPage = lazy(() =>
  import('../features/system-config/SystemConfigPage').then((m) => ({ default: m.SystemConfigPage }))
)
const SystemStatusPage = lazy(() =>
  import('../features/system-status/SystemStatusPage').then((m) => ({ default: m.SystemStatusPage }))
)
const TasksPage = lazy(() => import('../features/tasks/TasksPage').then((m) => ({ default: m.TasksPage })))
const HomeMembersPage = lazy(() =>
  import('../features/home-profile/HomeMembersPage').then((m) => ({ default: m.HomeMembersPage }))
)
const HomePetsPage = lazy(() =>
  import('../features/home-profile/HomePetsPage').then((m) => ({ default: m.HomePetsPage }))
)
const HomeProfileHubPage = lazy(() =>
  import('../features/home-profile/HomeProfileHubPage').then((m) => ({ default: m.HomeProfileHubPage }))
)
const HomeProfilePage = lazy(() =>
  import('../features/home-profile/HomeProfilePage').then((m) => ({ default: m.HomeProfilePage }))
)
const SettingsPage = lazy(() =>
  import('../features/settings/SettingsPage').then((m) => ({ default: m.SettingsPage }))
)
const OnboardingWelcomePage = lazy(() =>
  import('../features/onboarding/OnboardingWelcomePage').then((m) => ({ default: m.OnboardingWelcomePage }))
)
const OnboardingBasicVideoPage = lazy(() =>
  import('../features/onboarding/OnboardingBasicVideoPage').then((m) => ({ default: m.OnboardingBasicVideoPage }))
)
const OnboardingBasicProviderPage = lazy(() =>
  import('../features/onboarding/OnboardingBasicProviderPage').then((m) => ({ default: m.OnboardingBasicProviderPage }))
)
const OnboardingBasicSummaryTimePage = lazy(() =>
  import('../features/onboarding/OnboardingBasicSummaryTimePage').then((m) => ({ default: m.OnboardingBasicSummaryTimePage }))
)
const OnboardingBasicDonePage = lazy(() =>
  import('../features/onboarding/OnboardingBasicDonePage').then((m) => ({ default: m.OnboardingBasicDonePage }))
)
const OnboardingPersonalizeProfilePage = lazy(() =>
  import('../features/onboarding/OnboardingPersonalizeProfilePage').then((m) => ({
    default: m.OnboardingPersonalizeProfilePage,
  }))
)
const OnboardingPersonalizeCameraNotesPage = lazy(() =>
  import('../features/onboarding/OnboardingPersonalizeCameraNotesPage').then((m) => ({
    default: m.OnboardingPersonalizeCameraNotesPage,
  }))
)
const OnboardingPersonalizeStylePage = lazy(() =>
  import('../features/onboarding/OnboardingPersonalizeStylePage').then((m) => ({
    default: m.OnboardingPersonalizeStylePage,
  }))
)
const OnboardingPersonalizeDonePage = lazy(() =>
  import('../features/onboarding/OnboardingPersonalizeDonePage').then((m) => ({
    default: m.OnboardingPersonalizeDonePage,
  }))
)
import { useAuthStore } from '../store/authStore'
import { useThemeStore } from '../store/themeStore'

function LazyPage({ children }: { children: ReactElement }) {
  return <Suspense fallback={<LoadingBlock text="页面加载中..." />}>{children}</Suspense>
}

function BootstrapAuth() {
  const loadFromStorage = useAuthStore((state) => state.loadFromStorage)
  useEffect(() => {
    loadFromStorage()
  }, [loadFromStorage])
  return null
}

function BootstrapTheme() {
  const theme = useThemeStore((state) => state.theme)
  const loadFromStorage = useThemeStore((state) => state.loadFromStorage)

  useEffect(() => {
    loadFromStorage()
  }, [loadFromStorage])

  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  return null
}

export function AppRouter() {
  return (
    <BrowserRouter>
      <BootstrapAuth />
      <BootstrapTheme />
      <Routes>
        <Route element={<AuthLayout />}>
          <Route
            path="/login"
            element={
              <LazyPage>
                <LoginPage />
              </LazyPage>
            }
          />
        </Route>

        <Route element={<DashboardLayout />}>
          <Route
            path="/dashboard"
            element={
              <LazyPage>
                <DashboardPage />
              </LazyPage>
            }
          />
          <Route
            path="/video-sources"
            element={
              <LazyPage>
                <VideoSourcesPage />
              </LazyPage>
            }
          />
          <Route
            path="/providers"
            element={
              <LazyPage>
                <ProvidersPage />
              </LazyPage>
            }
          />
          <Route
            path="/events"
            element={
              <LazyPage>
                <EventsPage />
              </LazyPage>
            }
          />
          <Route
            path="/events/:id"
            element={
              <LazyPage>
                <EventDetailPage />
              </LazyPage>
            }
          />
          <Route
            path="/sessions"
            element={
              <LazyPage>
                <SessionsPage />
              </LazyPage>
            }
          />
          <Route
            path="/daily-summaries"
            element={
              <LazyPage>
                <DailySummariesPage />
              </LazyPage>
            }
          />
          <Route
            path="/tasks"
            element={
              <LazyPage>
                <TasksPage />
              </LazyPage>
            }
          />
          <Route
            path="/system-config"
            element={
              <LazyPage>
                <SystemConfigPage />
              </LazyPage>
            }
          />
          <Route
            path="/system-status"
            element={
              <LazyPage>
                <SystemStatusPage />
              </LazyPage>
            }
          />
          <Route
            path="/home-profile"
            element={
              <LazyPage>
                <HomeProfileHubPage />
              </LazyPage>
            }
          />
          <Route
            path="/home-profile/overview"
            element={
              <LazyPage>
                <HomeProfilePage />
              </LazyPage>
            }
          />
          <Route
            path="/home-profile/members"
            element={
              <LazyPage>
                <HomeMembersPage />
              </LazyPage>
            }
          />
          <Route
            path="/home-profile/pets"
            element={
              <LazyPage>
                <HomePetsPage />
              </LazyPage>
            }
          />
          <Route
            path="/onboarding"
            element={
              <LazyPage>
                <OnboardingWelcomePage />
              </LazyPage>
            }
          />
          <Route
            path="/onboarding/basic/video"
            element={
              <LazyPage>
                <OnboardingBasicVideoPage />
              </LazyPage>
            }
          />
          <Route
            path="/onboarding/basic/provider"
            element={
              <LazyPage>
                <OnboardingBasicProviderPage />
              </LazyPage>
            }
          />
          <Route
            path="/onboarding/basic/summary-time"
            element={
              <LazyPage>
                <OnboardingBasicSummaryTimePage />
              </LazyPage>
            }
          />
          <Route
            path="/onboarding/basic/done"
            element={
              <LazyPage>
                <OnboardingBasicDonePage />
              </LazyPage>
            }
          />
          <Route
            path="/onboarding/personalize/profile"
            element={
              <LazyPage>
                <OnboardingPersonalizeProfilePage />
              </LazyPage>
            }
          />
          <Route
            path="/onboarding/personalize/camera-notes"
            element={
              <LazyPage>
                <OnboardingPersonalizeCameraNotesPage />
              </LazyPage>
            }
          />
          <Route
            path="/onboarding/personalize/style"
            element={
              <LazyPage>
                <OnboardingPersonalizeStylePage />
              </LazyPage>
            }
          />
          <Route
            path="/onboarding/personalize/done"
            element={
              <LazyPage>
                <OnboardingPersonalizeDonePage />
              </LazyPage>
            }
          />
          <Route
            path="/chat"
            element={
              <LazyPage>
                <ChatPage />
              </LazyPage>
            }
          />
          <Route
            path="/webhooks"
            element={
              <LazyPage>
                <WebhooksPage />
              </LazyPage>
            }
          />
          <Route
            path="/settings"
            element={
              <LazyPage>
                <SettingsPage />
              </LazyPage>
            }
          />
        </Route>

        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
