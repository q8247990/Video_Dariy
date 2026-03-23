type ApiErrorAlertProps = {
  message: string
}

export function ApiErrorAlert({ message }: ApiErrorAlertProps) {
  return <div className="api-error">{message}</div>
}
