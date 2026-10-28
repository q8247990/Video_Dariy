import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import type { HomeEntity } from '../../types/api'

type EntityProfileMutationsOptions<TPayload> = {
  queryKey: string
  create: (payload: TPayload) => Promise<HomeEntity>
  update: (id: number, payload: Partial<TPayload>) => Promise<HomeEntity>
  disable: (id: number) => Promise<void>
  messages: {
    created: string
    updated: string
    disabled: string
  }
  closeCreate: () => void
  closeEdit: () => void
}

export function useEntityProfileMutations<TPayload extends object>({
  queryKey,
  create,
  update,
  disable,
  messages,
  closeCreate,
  closeEdit,
}: EntityProfileMutationsOptions<TPayload>) {
  const queryClient = useQueryClient()
  const [message, setMessage] = useState('')
  const invalidate = () => queryClient.invalidateQueries({ queryKey: [queryKey] })

  const createMutation = useMutation({
    mutationFn: create,
    onSuccess: () => {
      closeCreate()
      setMessage(messages.created)
      invalidate()
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: Partial<TPayload> }) =>
      update(id, payload),
    onSuccess: () => {
      closeEdit()
      setMessage(messages.updated)
      invalidate()
    },
    onError: (error) => setMessage((error as Error).message),
  })

  const disableMutation = useMutation({
    mutationFn: disable,
    onSuccess: () => {
      setMessage(messages.disabled)
      invalidate()
    },
    onError: (error) => setMessage((error as Error).message),
  })

  return { message, setMessage, createMutation, updateMutation, disableMutation }
}
