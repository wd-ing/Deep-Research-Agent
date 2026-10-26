/** 研究档位（C2 前端）：键、文案、默认值的单一来源。
 *  后端 `session.mode` 的合法值与这里一一对应——新增档位两边都要动。 */

export type ResearchMode = 'standard' | 'fast'

export interface ModeDef {
  key: ResearchMode
  label: string
  tagline: string
  /** 预计耗时（来自实测：标准 8.9–10.6 分钟/最长 13.3，快速 2.2–4.8） */
  eta: string
  bullets: string
}

export const MODES: ModeDef[] = [
  {
    key: 'standard',
    label: '标准',
    tagline: '完整研究报告',
    eta: '约 8–13 分钟',
    bullets: '大纲审批 · 分节写作 · 模型审查与返修',
  },
  {
    key: 'fast',
    label: '快速',
    tagline: '检索整理简报',
    eta: '约 2–5 分钟',
    bullets: '无需审批 · 检索一轮后一次成文 · 无模型审查，低质来源需自行甄别',
  },
]

export const DEFAULT_MODE: ResearchMode = 'standard'

const KEY = 'ra.defaultMode'

export function isMode(v: unknown): v is ResearchMode {
  return v === 'standard' || v === 'fast'
}

export function loadDefaultMode(): ResearchMode {
  try {
    const v = localStorage.getItem(KEY)
    return isMode(v) ? v : DEFAULT_MODE
  } catch {
    return DEFAULT_MODE
  }
}

export function saveDefaultMode(mode: ResearchMode): void {
  try {
    localStorage.setItem(KEY, mode)
  } catch {
    // localStorage 不可用（隐私模式/iframe）：不写，用内存默认
  }
}
