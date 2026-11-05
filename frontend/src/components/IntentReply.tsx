/** 意图回复卡（坑 98）：非调研输入的就地回复。
 *  输入框下方渲染，role=status + aria-live；3 个示例 chip
 *  点击填入输入框（不自动提交）；「仍然作为调研提交」跳过判定。 */
export function IntentReply({
  reply, suggestions, onPick, onForce, onDismiss,
}: {
  reply: string | null
  suggestions: string[]
  /** 点 chip：填入输入框并聚焦（由调用方做），卡本身也关掉。 */
  onPick: (q: string) => void
  onForce: () => void
  onDismiss: () => void
}) {
  return (
    <div
      role="status" aria-live="polite"
      className="mt-3 rounded-xl border border-line bg-white p-4 text-left shadow-sm"
    >
      {reply && (
        <p className="whitespace-pre-line text-[13px] leading-relaxed text-ink">
          {reply}
        </p>
      )}
      {suggestions.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {suggestions.map((q) => (
            <button key={q} type="button" onClick={() => onPick(q)}
              className="rounded-full border border-line px-3 py-1 text-2xs text-muted transition-colors hover:border-accent hover:text-accent">
              {q}
            </button>
          ))}
        </div>
      )}
      <div className="mt-3 flex items-center gap-3">
        <button type="button" onClick={onForce}
          className="text-2xs text-accent hover:underline">
          仍然作为调研提交
        </button>
        <button type="button" onClick={onDismiss}
          className="text-2xs text-faint hover:text-ink">
          知道了
        </button>
      </div>
    </div>
  )
}
