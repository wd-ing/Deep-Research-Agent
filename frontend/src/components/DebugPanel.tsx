/** debug 后门（B4）：`?debug=1` 时才挂载。
 *
 *  thinking / draft 原始流**默认不渲染**——实测 write_audit 单次思考
 *  25k 字符、M3 的推理风格偏绕，直接铺出来是噪音墙（用户判断过：
 *  环节视图只展示核心产出）。但「当时模型在想什么」偶尔是排查线索，
 *  所以留一个不占正常界面的折叠面板后门。
 *
 *  stage 名原样显示——这是调试视图，受众是开发者不是读者。
 */
export function DebugPanel({
  thinking, draft,
}: {
  thinking: Record<string, string>
  draft: string
}) {
  const stages = Object.entries(thinking)
  if (!stages.length && !draft) return null
  return (
    <div className="border-t border-line bg-gray-50/60 px-5 py-3">
      <div className="text-2xs font-medium uppercase tracking-wide text-muted">
        debug · 原始流（?debug=1）
      </div>
      {stages.map(([stage, text]) => (
        <details key={stage} className="mt-2">
          <summary className="cursor-pointer text-xs text-muted hover:text-ink">
            thinking · {stage}
            <span className="ml-1 text-2xs text-muted/70">（{text.length} 字）</span>
          </summary>
          <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded border border-line bg-white p-2 text-2xs leading-relaxed text-muted">
            {text}
          </pre>
        </details>
      ))}
      {draft && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-muted hover:text-ink">
            draft · write_section
            <span className="ml-1 text-2xs text-muted/70">（{draft.length} 字）</span>
          </summary>
          {/* 全文可能 4 万字，面板里只给尾部——要全文去看报告本体 */}
          <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded border border-line bg-white p-2 text-2xs leading-relaxed text-muted">
            {draft.length > 8000 ? `（只显示尾部 8000 字，全文见报告）\n\n${draft.slice(-8000)}` : draft}
          </pre>
        </details>
      )}
    </div>
  )
}
