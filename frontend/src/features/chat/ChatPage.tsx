import { useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { PageHeader } from '../../components/common/PageHeader'
import { LoadingBlock } from '../../components/common/LoadingBlock'
import { ApiErrorAlert } from '../../components/common/ApiErrorAlert'
import { askQuestion, getChatHistory } from './api'

export function ChatPage() {
  const queryClient = useQueryClient()
  const [question, setQuestion] = useState('')
  const [latestError, setLatestError] = useState('')

  const historyQuery = useQuery({
    queryKey: ['chat-history', { page: 1 }],
    queryFn: () => getChatHistory(1, 20),
  })

  const askMutation = useMutation({
    mutationFn: (content: string) => askQuestion(content),
    onSuccess: () => {
      setLatestError('')
      setQuestion('')
      queryClient.invalidateQueries({ queryKey: ['chat-history'] })
    },
    onError: (error) => setLatestError((error as Error).message),
  })

  const answerCard = useMemo(() => {
    if (!askMutation.data) {
      return null
    }
    return askMutation.data
  }, [askMutation.data])

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const q = question.trim()
    if (!q) {
      return
    }
    askMutation.mutate(q)
  }

  return (
    <div>
      <PageHeader title="问答中心" subtitle="按自然语言检索安防事件" />

      <div className="card chat-ask-card">
        <form onSubmit={handleSubmit}>
          <label>
            请输入问题
            <textarea
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="例如：昨天晚上门口有没有人停留超过5分钟？"
              required
            />
          </label>
          <div className="dialog-actions">
            <button type="submit" disabled={askMutation.isPending}>
              {askMutation.isPending ? '分析中...' : '提交问题'}
            </button>
          </div>
        </form>

        {latestError ? <ApiErrorAlert message={latestError} /> : null}
      </div>

      {answerCard ? (
        <div className="card chat-answer-card">
          <h3>最新回答</h3>
          <div className="chat-answer-main">
            <p className="chat-q">Q：{answerCard.question}</p>
            <p className="chat-a">A：{answerCard.answer_text}</p>
          </div>
          <div>
            <h4>关联事件</h4>
            <ul className="list-simple">
              {(answerCard.referenced_events ?? []).slice(0, 10).map((event) => (
                <li key={event.id}>
                  <p>[#{event.id}] {event.description}</p>
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}

      <div className="card">
        <h3>历史记录</h3>
        {historyQuery.isLoading ? <LoadingBlock text="加载问答历史中" /> : null}
        {historyQuery.error ? <ApiErrorAlert message={(historyQuery.error as Error).message} /> : null}
        {!historyQuery.isLoading && !historyQuery.error ? (
          <ul className="chat-history-list">
            {(historyQuery.data?.list ?? []).map((item) => (
              <li key={item.id}>
                <p className="chat-q">Q：{item.user_question}</p>
                <p className="chat-a">A：{item.answer_text}</p>
                <small>{item.created_at}</small>
              </li>
            ))}
            {(historyQuery.data?.list.length ?? 0) === 0 ? (
              <li className="empty-cell">暂无历史问答记录</li>
            ) : null}
          </ul>
        ) : null}
      </div>
    </div>
  )
}
