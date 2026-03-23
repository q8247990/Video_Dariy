import { apiClient, unwrapApi } from '../../lib/axios'
import type {
  HomeOptions,
  HomeProfile,
  HomeProfilePayload,
  OnboardingStatus,
  PaginatedData,
  Provider,
  ProviderCreate,
  SystemConfig,
  VideoPathValidateResponse,
  VideoSource,
  VideoSourceCreate,
} from '../../types/api'

export async function getOnboardingStatus(): Promise<OnboardingStatus> {
  const response = await apiClient.get('/onboarding/status')
  return unwrapApi<OnboardingStatus>(response)
}

export async function validateVideoPath(path: string): Promise<VideoPathValidateResponse> {
  const response = await apiClient.post('/video-sources/validate-path', { path })
  return unwrapApi<VideoPathValidateResponse>(response)
}

export async function createOnboardingVideoSource(payload: VideoSourceCreate): Promise<VideoSource> {
  const response = await apiClient.post('/video-sources', payload)
  return unwrapApi<VideoSource>(response)
}

export async function testVideoSource(sourceId: number): Promise<{ success: boolean; message: string }> {
  const response = await apiClient.post(`/video-sources/${sourceId}/test`)
  return unwrapApi<{ success: boolean; message: string }>(response)
}

export async function createOnboardingProvider(payload: ProviderCreate): Promise<Provider> {
  const response = await apiClient.post('/providers', payload)
  return unwrapApi<Provider>(response)
}

export async function testOnboardingProvider(providerId: number): Promise<{ success: boolean; message: string }> {
  const response = await apiClient.post(`/providers/${providerId}/test`)
  return unwrapApi<{ success: boolean; message: string }>(response)
}

export async function saveDailySummarySchedule(value: string): Promise<SystemConfig> {
  const response = await apiClient.put('/system-config', { daily_summary_schedule: value })
  return unwrapApi<SystemConfig>(response)
}

export async function getHomeProfile(): Promise<HomeProfile> {
  const response = await apiClient.get('/home-profile')
  return unwrapApi<HomeProfile>(response)
}

export async function getHomeOptions(): Promise<HomeOptions> {
  const response = await apiClient.get('/home-profile/options')
  return unwrapApi<HomeOptions>(response)
}

export async function saveHomeProfile(payload: HomeProfilePayload): Promise<HomeProfile> {
  const response = await apiClient.put('/home-profile', payload)
  return unwrapApi<HomeProfile>(response)
}

export async function getVideoSourcesForOnboarding(): Promise<VideoSource[]> {
  const response = await apiClient.get('/video-sources?page=1&page_size=100')
  const data = unwrapApi<PaginatedData<VideoSource>>(response)
  return data.list
}

export async function updateVideoSourceDescription(id: number, description: string): Promise<VideoSource> {
  const response = await apiClient.put(`/video-sources/${id}`, { description })
  return unwrapApi<VideoSource>(response)
}
