import { apiClient, unwrapApi } from '../../lib/axios'
import type {
  HomeEntity,
  HomeOptions,
  HomeProfile,
  HomeProfilePayload,
  MemberPayload,
  PetPayload,
} from '../../types/api'

export async function getHomeProfile(): Promise<HomeProfile> {
  const response = await apiClient.get('/home-profile')
  return unwrapApi<HomeProfile>(response)
}

export async function saveHomeProfile(payload: HomeProfilePayload): Promise<HomeProfile> {
  const response = await apiClient.put('/home-profile', payload)
  return unwrapApi<HomeProfile>(response)
}

export async function listHomeEntities(
  entityType?: 'member' | 'pet',
  includeDisabled = false,
): Promise<HomeEntity[]> {
  const params = new URLSearchParams()
  if (entityType) {
    params.set('entity_type', entityType)
  }
  if (includeDisabled) {
    params.set('include_disabled', 'true')
  }
  const query = params.toString()
  const response = await apiClient.get(`/home-profile/entities${query ? `?${query}` : ''}`)
  return unwrapApi<HomeEntity[]>(response)
}

export async function createMember(payload: MemberPayload): Promise<HomeEntity> {
  const response = await apiClient.post('/home-profile/entities/member', payload)
  return unwrapApi<HomeEntity>(response)
}

export async function createPet(payload: PetPayload): Promise<HomeEntity> {
  const response = await apiClient.post('/home-profile/entities/pet', payload)
  return unwrapApi<HomeEntity>(response)
}

export async function updateEntity(id: number, payload: Partial<MemberPayload & PetPayload>): Promise<HomeEntity> {
  const response = await apiClient.put(`/home-profile/entities/${id}`, payload)
  return unwrapApi<HomeEntity>(response)
}

export async function disableEntity(id: number): Promise<void> {
  await apiClient.delete(`/home-profile/entities/${id}`)
}

export async function getHomeOptions(): Promise<HomeOptions> {
  const response = await apiClient.get('/home-profile/options')
  return unwrapApi<HomeOptions>(response)
}
