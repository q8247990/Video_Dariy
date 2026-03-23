import { apiClient, unwrapApi } from '../../lib/axios'
import type { ChatAskResponse, ChatHistoryItem, PaginatedData } from '../../types/api'

export async function askQuestion(question: string): Promise<ChatAskResponse> {
  const response = await apiClient.post('/chat/ask', { question })
  return unwrapApi<ChatAskResponse>(response)
}

export async function getChatHistory(page = 1, pageSize = 20): Promise<PaginatedData<ChatHistoryItem>> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  })
  const response = await apiClient.get(`/chat/history?${params.toString()}`)
  return unwrapApi<PaginatedData<ChatHistoryItem>>(response)
}
