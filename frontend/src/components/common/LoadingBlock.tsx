type LoadingBlockProps = {
  text?: string
}

export function LoadingBlock({ text = '加载中...' }: LoadingBlockProps) {
  return (
    <div className="loading-block">
      <span className="loader" />
      <span>{text}</span>
    </div>
  )
}
