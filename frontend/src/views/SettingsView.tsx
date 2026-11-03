import { useEffect, useState } from 'react'
import { api } from '../useSession'
import { MODES, loadDefaultMode, saveDefaultMode, type ResearchMode } from '../lib/mode'

interface ConfigResp {
  path: string
  content: string
  /** 当前生效的 环节 → provider/model 映射（后端已解析好） */
  stages: Record<string, string>
  restart_required: boolean
}

/** 「这个环节是不是换了模型」的判定依据：
 *  stages 里没有写自己的 provider 的环节继承 `defaults.provider`，
 *  模型 = providers 档案里的那条 `model:`。从 yaml 原文里把默认模型
 *  抠出来——它比「多数票」可靠：多数环节同时钉到别的档时默认模型
 *  反而会变成少数。两处任一处抠不出来就返回 null，一个不标，
 *  比一个标错强。 */
function defaultModel(yamlText: string): string | null {
  const defProvider = yamlText.match(/^defaults:\s*\n(?:\s{2,}[^\n]*\n)*?\s{2,}provider:\s*(\S+)/m)?.[1]
  if (!defProvider) return null
  const re = new RegExp(`^\\s{2}${defProvider.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}:\\s*\\n(?:\\s{4,}[^\\n]*\\n)*?\\s{4,}model:\\s*(\\S+)`, 'm')
  return yamlText.match(re)?.[1] ?? null
}

/** 设置页（D1）：查看 + 编辑 models.yaml。
 *
 *  ⚠️ **不做运行时热切换**——LLMClient/ModelRegistry 都是进程级单例，
 *  配置在启动时读一次。「保存即生效」是个谎言，这里明说：保存后重启生效。
 *  （用户定的 v1 范围：查看 + 编辑 + 保存后提示重启。）
 */
export function SettingsView({ onNotify }: { onNotify: (m: string, kind?: 'err' | 'ok') => void }) {
  const [cfg, setCfg] = useState<ConfigResp | null>(null)
  const [defMode, setDefMode] = useState<ResearchMode>(() => loadDefaultMode())
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

  // ⌘S/Ctrl+S 保存：只在 dirty 时拦（否则别抢浏览器默认行为）
  useEffect(() => {
    if (!dirty) return
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 's') { e.preventDefault(); save() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [dirty, text])  // eslint-disable-line react-hooks/exhaustive-deps
  // dirty 时离开页面提示
  useEffect(() => {
    if (!dirty) return
    const h = (e: BeforeUnloadEvent) => { e.preventDefault() }
    window.addEventListener('beforeunload', h)
    return () => window.removeEventListener('beforeunload', h)
  }, [dirty])

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

  // 生效表是后端按**加载时那份**解析的，判定默认模型也必须看同一份
  // （cfg.content），不能用用户正在改、还没存的 text。
  const def = cfg ? defaultModel(cfg.content) : null

  return (
    <div className="mx-auto w-full max-w-3xl px-6 py-8">
      <div className="font-mono text-2xs tracking-[.14em] text-faint">PREFERENCES</div>
      <h2 className="mt-1.5 text-[30px] font-semibold tracking-[-.02em] text-ink">设置</h2>
      <p className="mt-1.5 text-sm text-muted">
        模型与环节的映射来自 <code className="font-mono text-xs">{cfg?.path ?? 'config/models.yaml'}</code>。
      </p>
      <div className="mt-3 rounded-lg bg-black/[.04] px-3.5 py-2.5 text-xs leading-relaxed text-ink/80">
        改动保存后<b>重启后端服务才生效</b>——进程里的配置是启动时读的那一份。
      </div>

      {/* 默认档位是浏览器偏好（localStorage），不进 models.yaml——
          它和「重启生效」的服务端配置不是一个生命周期，放最上面。 */}
      <div className="card mt-5">
        <div className="border-b border-line px-4 py-2.5 text-xs font-medium text-muted">
          <span className="mr-2 font-mono text-faint">01</span>默认研究档位
        </div>
        <div className="px-4 py-3">
          <div className="grid grid-cols-2 gap-2 max-sm:grid-cols-1" role="radiogroup" aria-label="默认研究档位">
            {MODES.map((m) => {
              const on = defMode === m.key
              return (
                <button
                  key={m.key}
                  type="button"
                  role="radio"
                  aria-checked={on}
                  onClick={() => { setDefMode(m.key); saveDefaultMode(m.key) }}
                  className={[
                    'rounded-lg border px-3 py-2 text-left outline-none transition-colors',
                    on ? 'border-accent bg-accent/5' : 'border-line hover:bg-gray-50',
                  ].join(' ')}
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-sm font-medium text-ink">
                      {m.label}
                      <span className="ml-1.5 text-2xs font-normal text-muted">{m.tagline}</span>
                    </span>
                    <span className={['shrink-0 text-2xs tabular-nums', on ? 'text-accent' : 'text-faint'].join(' ')}>
                      {m.eta}
                    </span>
                  </div>
                  <div className="mt-0.5 text-2xs leading-relaxed text-muted">{m.bullets}</div>
                </button>
              )
            })}
          </div>
          <p className="mt-2 text-2xs text-faint">
            新建研究时默认选中的档位，每次仍可在对话框里改。
          </p>
        </div>
      </div>

      {loadErr && (
        <div className="mt-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          读不到配置：{loadErr}
        </div>
      )}

      {cfg && (
        <>
          <div className="card mt-5">
            <div className="border-b border-line px-4 py-2.5 text-xs font-medium text-muted">
              <span className="mr-2 font-mono text-faint">02</span>当前生效的环节 → 模型
            </div>
            <table className="w-full text-xs">
              <tbody>
                {Object.entries(cfg.stages).map(([stage, model]) => {
                  // 「换了模型」= 解析出的模型名跟 defaults 档案的模型不一样。
                  // def 抠不出来就一个都不标（比标错强）。
                  const m = model.split(' / ').pop() ?? model
                  const pinned = def !== null && m !== def
                  return (
                    <tr key={stage} className="border-b border-line last:border-0">
                      <td className="px-4 py-2 font-mono text-[11px] text-ink">{stage}</td>
                      <td className="px-4 py-2 text-right text-muted">
                        {model}
                        {pinned && (
                          <span className="pill ml-2 !h-[18px] px-[7px]" title={`defaults 档案是 ${def}`}>非默认</span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          <div className="card mt-5">
            <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
              <span className="text-xs font-medium text-muted"><span className="mr-2 font-mono text-faint">03</span>models.yaml 原文</span>
              <span className="text-2xs text-faint">编辑的是磁盘上的文件本身</span>
            </div>
            <textarea
              value={text}
              onChange={(e) => { setText(e.target.value); setSavedAt(null) }}
              spellCheck={false}
              className="h-[420px] w-full resize-y rounded-b-2xl bg-white px-4 py-3 font-mono text-xs leading-5 text-ink outline-none"
            />
          </div>

          {savedAt && (
            <div className="mt-3"><span className="pill">已保存。重启后端服务后生效。</span></div>
          )}
          {dirty && (
            <div className="sticky bottom-0 -mx-6 mt-4 flex items-center justify-end gap-2 border-t border-line2 bg-white/95 px-6 py-3 backdrop-blur">
              <button
                onClick={() => { setText(cfg.content); setSavedAt(null) }}
                className="btn-ghost"
              >还原</button>
              <button onClick={save} disabled={saving} className="btn-primary">
                {saving ? '保存中…' : '保存（⌘S）'}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
