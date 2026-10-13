import { Component, type ReactNode } from 'react'

/** 渲染异常兜底。
 *
 *  没有它的话，任何一个组件抛错都是**整页白屏**——用户只剩浏览器控制台
 *  才知道发生了什么，而这个项目是在浏览器里跑整轮研究的（十几分钟）。
 *  白屏对着干瞪眼、只能刷新重来，代价太高。
 *
 *  所以这里做两件事：把错误原文显示出来（可复制，能贴给开发/记进 issue），
 *  以及给一条明确的恢复路径，而不是空白。 */
export class ErrorBoundary extends Component<
  { children: ReactNode },
  { err: Error | null }
> {
  state: { err: Error | null } = { err: null }

  static getDerivedStateFromError(err: Error) {
    return { err }
  }

  componentDidCatch(err: Error, info: unknown) {
    // 留着 `console.error`：这正是它该出现的地方（真出事了）。
    // 之前删掉的那条 `console.log('[ref-click]')` 是调试残留，性质不同。
    console.error('[render]', err, info)
  }

  render() {
    const { err } = this.state
    if (!err) return this.props.children
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-4 bg-gray-50 px-6 text-center">
        <div className="text-sm font-medium text-ink">界面出错了，已停在这里以免显示错乱的数据</div>
        <p className="max-w-md text-xs leading-relaxed text-muted">
          研究任务本身不受影响——它跑在后端，刷新页面就能继续看进度。
        </p>
        <pre className="max-h-64 max-w-2xl overflow-auto whitespace-pre-wrap rounded border
                        border-red-200 bg-white px-3 py-2 text-left text-[11px] text-red-700">
          {err.message}
          {'\n\n'}
          {err.stack?.split('\n').slice(0, 6).join('\n')}
        </pre>
        <button
          onClick={() => this.setState({ err: null })}
          className="rounded border border-line px-3 py-1.5 text-xs text-muted hover:bg-gray-50"
        >
          重试渲染
        </button>
      </div>
    )
  }
}
