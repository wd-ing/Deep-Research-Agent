import { useEffect, useState } from 'react'
import { api } from '../useSession'

interface ConfigResp {
  path: string
  content: string
  /** 当前生效的 环节 → provider/model 映射（后端已解析好） */
  stages: Record<string, string>
  restart_required: boolean
}

/** 设置页（D1）：查看 + 编辑 models.yaml。
 *
 *  ⚠️ **不做运行时热切换**——LLMClient/ModelRegistry 都是进程级单例，
 *  配置在启动时读一次。「保存即生效」是个谎言，这里明说：保存后重启生效。
 *  （用户定的 v1 范围：查看 + 编辑 + 保存后提示重启。）
 */
export function SettingsView({ onNotify }: { onNotify: (m: string, kind?: 'err' | 'ok') => void }) {
  const [cfg, setCfg] = useState<ConfigResp | null>(null)
  const [text, setText] = useState('')
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<number | null>(null)

  const load = () => {
    api.jget<ConfigResp>('/api/config')
      .then((r) => { setCfg(r); setText(r.content); setLoadErr(null) })
      .catch((e) => setLoadErr((e as Error).message))
  }
  useEffect(load, [])

  const dirty = cfg !== null && text !== cfg.content

  const save = () => {
    setSaving(true)
    void api.jput<{ ok: boolean; note?: string }>('/api/config', { content: text })
      .then(() => {
        setSavedAt(Date.now())
        setCfg((c) => (c ? { ...c, content: text } : c))
        onNotify('已保存 models.yaml——重启后端服务后生效', 'ok')
      })
      .catch((e) => onNotify(`保存失败：${(e as Error).message}`))
      .finally(() => setSaving(false))
  }

  return (
    <div className="mx-auto w-full max-w-3xl px-6 py-8">
      <h2 className="text-lg font-medium text-ink">设置</h2>
      <p className="mt-1 text-sm text-muted">
        模型与环节的映射来自 <code className="text-xs">{cfg?.path ?? 'config/models.yaml'}</code>。
        改动保存后<b>重启后端服务才生效</b>——进程里的配置是启动时读的那一份。
      </p>

      {loadErr && (
        <div className="mt-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          读不到配置：{loadErr}
        </div>
      )}

      {cfg && (
        <>
          <div className="mt-5 rounded-lg border border-line bg-white">
            <div className="border-b border-line px-4 py-2 text-xs font-medium text-muted">
              当前生效的环节 → 模型
            </div>
            <table className="w-full text-xs">
              <tbody>
                {Object.entries(cfg.stages).map(([stage, model]) => (
                  <tr key={stage} className="border-b border-line/60 last:border-0">
                    <td className="px-4 py-1.5 text-ink">{stage}</td>
                    <td className="px-4 py-1.5 text-right text-muted">{model}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="mt-5 rounded-lg border border-line bg-white">
            <div className="flex items-center justify-between border-b border-line px-4 py-2">
              <span className="text-xs font-medium text-muted">models.yaml 原文</span>
              <span className="text-2xs text-muted/70">编辑的是磁盘上的文件本身</span>
            </div>
            <textarea
              value={text}
              onChange={(e) => { setText(e.target.value); setSavedAt(null) }}
              spellCheck={false}
              className="h-[420px] w-full resize-y rounded-b-lg bg-white px-4 py-3 font-mono text-xs leading-5 text-ink outline-none"
            />
          </div>

          <div className="mt-3 flex items-center justify-end gap-2">
            {savedAt && (
              <span className="mr-auto rounded bg-amber-50 px-2 py-1 text-2xs text-amber-800">
                已保存。重启后端服务后生效。
              </span>
            )}
            {dirty && (
              <button
                onClick={() => { setText(cfg.content); setSavedAt(null) }}
                className="btn-ghost"
              >
                还原
              </button>
            )}
            <button onClick={save} disabled={!dirty || saving} className="btn-primary">
              {saving ? '保存中…' : '保存'}
            </button>
          </div>
        </>
      )}
    </div>
  )
}
